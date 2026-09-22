"""An invoice's GL code must resolve in the invoice's OWN chart.

``Invoice.gl_account`` / ``InvoiceLineItem.gl_account`` store a code, not a
foreign key (``docs/decisions.md`` §186), and a code means the account it names
in the chart of the entity the invoice belongs to — shared accounts ∪ that
entity's own (``docs/multi-entity.md`` § Chart of accounts). Two subsidiaries
may each hold their own ``6000``.

Before ``services/gl_chart``, every manual write path accepted subsidiary B's
code on a subsidiary-A invoice: the consolidated-view picker offered it, and
nothing server-side checked. The string then resolved against A's chart — a
different account, or none. These tests pin the refusal on every path that
writes the column (create, PATCH, the line-items PUT, approve-with-corrections,
CSV import, a recurring template that will stamp it), the carve-outs that keep
existing rows editable, and the `chart_entity_id` list parameter the pickers
use to offer exactly the codes a write will accept. Bulk re-code already
validated per invoice entity (`tests/test_entity_coa.py`).

Real-Postgres harness (``realdb``), tenant ``a``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import func, select

from app.models.gl_account import GLAccount
from app.models.invoice import Invoice, InvoiceLineItem, InvoiceStatus
from app.models.recurring_invoice import RecurringInvoiceTemplate
from app.models.vendor import Vendor
from app.services.gl_chart import ChartOwnership

TENANT = "a"

# The fixture chart. `A` is the tenant's default entity, `B` a subsidiary.
SHARED = "1000"  # shared — valid for every entity
A_OWN = "7000"  # entity A's own
B_OWN = "6000"  # entity B's own — the code a consolidated picker used to offer
BOTH = "6500"  # A and B each define their own — valid for both
A_RETIRED = "6800"  # A's own, retired — still A's, so not "another entity's"
UNKNOWN = "9999"  # in no chart at all — out of scope here (docs/followups.md)


@pytest_asyncio.fixture
async def chart(realdb):
    """(entity_a, entity_b) as uuids, with the fixture chart seeded."""
    async with realdb.client(key=TENANT, role="admin") as admin:
        r = await admin.post(
            "/api/entities",
            json={"name": "Sub B", "slug": f"sub-b-{uuid.uuid4().hex[:8]}"},
        )
        assert r.status_code == 201, r.text
        entity_b = uuid.UUID(r.json()["id"])
        rows = (await admin.get("/api/entities")).json()
    entity_a = uuid.UUID(next(e["id"] for e in rows if e["is_default"]))

    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        s.add_all(
            [
                GLAccount(organization_id=org_id, code=SHARED, name="Cash", entity_id=None),
                GLAccount(organization_id=org_id, code=A_OWN, name="A Travel", entity_id=entity_a),
                GLAccount(organization_id=org_id, code=B_OWN, name="B Office", entity_id=entity_b),
                GLAccount(organization_id=org_id, code=BOTH, name="A Rent", entity_id=entity_a),
                GLAccount(organization_id=org_id, code=BOTH, name="B Rent", entity_id=entity_b),
                GLAccount(
                    organization_id=org_id,
                    code=A_RETIRED,
                    name="A Old",
                    entity_id=entity_a,
                    is_active=False,
                ),
            ]
        )
        await s.commit()
    return entity_a, entity_b


async def _seed_invoice(
    realdb,
    entity_id,
    *,
    gl: str | None = None,
    status: InvoiceStatus = InvoiceStatus.new,
    number: str | None = None,
) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=realdb.info(TENANT).org_id,
                entity_id=entity_id,
                invoice_number=number or f"GLC-{inv_id.hex[:8]}",
                vendor_name="Chart Vendor",
                amount=Decimal("100.00"),
                currency="USD",
                status=status,
                gl_account=gl,
            )
        )
        await s.commit()
    return inv_id


async def _invoice(realdb, inv_id) -> Invoice:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()


async def _line_codes(realdb, inv_id) -> list[str | None]:
    async with realdb.sessionmaker(TENANT)() as s:
        return list(
            (
                await s.execute(
                    select(InvoiceLineItem.gl_account)
                    .where(InvoiceLineItem.invoice_id == inv_id)
                    .order_by(InvoiceLineItem.line_number)
                )
            )
            .scalars()
            .all()
        )


def _create_body(number: str, gl: str | None) -> dict:
    return {
        "vendor": "Chart Vendor",
        "invoice_number": number,
        "amount": "100.00",
        "gl_account": gl,
    }


# ---------------------------------------------------------------------------
# The rule itself (pure)
# ---------------------------------------------------------------------------


def test_ownership_refuses_only_a_code_that_lives_solely_in_another_chart():
    a, b = uuid.uuid4(), uuid.uuid4()
    own = ChartOwnership(
        shared=frozenset({SHARED}),
        by_entity={a: frozenset({A_OWN, BOTH}), b: frozenset({B_OWN, BOTH})},
    )
    assert own.belongs_elsewhere(B_OWN, a)
    assert not own.belongs_elsewhere(B_OWN, b)
    assert not own.belongs_elsewhere(SHARED, a)
    assert not own.belongs_elsewhere(BOTH, a)
    assert not own.belongs_elsewhere(A_OWN, a)
    # In no chart at all: not this module's refusal.
    assert not own.belongs_elsewhere(UNKNOWN, a)
    # An invoice with no entity sees the shared chart alone, so every
    # entity-owned code is someone else's.
    assert own.belongs_elsewhere(A_OWN, None)
    assert not own.belongs_elsewhere(SHARED, None)


# ---------------------------------------------------------------------------
# POST /api/invoices
# ---------------------------------------------------------------------------


async def test_create_refuses_another_entitys_code(realdb, chart):
    entity_a, _ = chart
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices",
            json=_create_body("GLC-CREATE-B", B_OWN),
            headers={"X-Entity-ID": str(entity_a)},
        )
    assert resp.status_code == 422, resp.text
    assert f"'{B_OWN}'" in resp.json()["detail"]
    async with realdb.sessionmaker(TENANT)() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(Invoice)
                .where(Invoice.invoice_number == "GLC-CREATE-B")
            )
        ).scalar_one()
    assert n == 0, "a refused create must not leave a row behind"


async def test_consolidated_create_is_checked_against_the_default_entitys_chart(realdb, chart):
    """No X-Entity-ID → the invoice lands under the default entity, and that is
    the chart its code must resolve in — not the consolidated union the
    picker used to show."""
    entity_a, _ = chart
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        refused = await c.post("/api/invoices", json=_create_body("GLC-CONS-B", B_OWN))
        ok = await c.post("/api/invoices", json=_create_body("GLC-CONS-A", A_OWN))
    assert refused.status_code == 422, refused.text
    assert ok.status_code == 201, ok.text
    assert ok.json()["entity_id"] == str(entity_a)
    assert ok.json()["gl_account"] == A_OWN


async def test_create_accepts_every_code_in_the_invoices_own_chart(realdb, chart):
    entity_a, _ = chart
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        for i, gl in enumerate([SHARED, A_OWN, BOTH, A_RETIRED, UNKNOWN, None]):
            resp = await c.post(
                "/api/invoices",
                json=_create_body(f"GLC-OK-{i}", gl),
                headers={"X-Entity-ID": str(entity_a)},
            )
            assert resp.status_code == 201, (gl, resp.text)


async def test_the_same_code_is_fine_under_the_entity_that_owns_it(realdb, chart):
    _, entity_b = chart
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices",
            json=_create_body("GLC-B-OWN", B_OWN),
            headers={"X-Entity-ID": str(entity_b)},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["entity_id"] == str(entity_b)


# ---------------------------------------------------------------------------
# PATCH /api/invoices/{id}
# ---------------------------------------------------------------------------


async def test_patch_refuses_another_entitys_code_and_changes_nothing(realdb, chart):
    entity_a, _ = chart
    inv_id = await _seed_invoice(realdb, entity_a, gl=SHARED)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        refused = await c.patch(
            f"/api/invoices/{inv_id}", json={"gl_account": B_OWN, "notes": "recode"}
        )
        ok = await c.patch(f"/api/invoices/{inv_id}", json={"gl_account": A_OWN})
    assert refused.status_code == 422, refused.text
    assert ok.status_code == 200, ok.text
    assert (await _invoice(realdb, inv_id)).gl_account == A_OWN


async def test_patch_is_checked_against_the_invoices_entity_not_the_selection(realdb, chart):
    """The sidebar selection is not the invoice's chart: with B selected, an
    A invoice still may not take B's code — and may take A's own."""
    entity_a, entity_b = chart
    inv_id = await _seed_invoice(realdb, entity_a)
    headers = {"X-Entity-ID": str(entity_b)}
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        refused = await c.patch(
            f"/api/invoices/{inv_id}", json={"gl_account": B_OWN}, headers=headers
        )
        ok = await c.patch(f"/api/invoices/{inv_id}", json={"gl_account": A_OWN}, headers=headers)
    assert refused.status_code == 422, refused.text
    assert ok.status_code == 200, ok.text


async def test_patch_that_echoes_the_stored_code_back_is_not_refused(realdb, chart):
    """An invoice coded to another entity's account BEFORE this check existed
    must stay editable: re-saving its unchanged code is not a coding decision."""
    entity_a, _ = chart
    inv_id = await _seed_invoice(realdb, entity_a, gl=B_OWN)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(
            f"/api/invoices/{inv_id}", json={"gl_account": B_OWN, "notes": "unrelated edit"}
        )
    assert resp.status_code == 200, resp.text
    inv = await _invoice(realdb, inv_id)
    assert inv.notes == "unrelated edit"
    assert inv.gl_account == B_OWN


# ---------------------------------------------------------------------------
# PUT /api/invoices/{id}/line-items
# ---------------------------------------------------------------------------


async def test_line_items_refuse_another_entitys_code(realdb, chart):
    entity_a, _ = chart
    inv_id = await _seed_invoice(realdb, entity_a)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        ok = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[{"description": "Travel", "total": "60.00", "gl_account": A_OWN}],
        )
        refused = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[
                {"description": "Travel", "total": "60.00", "gl_account": A_OWN},
                {"description": "Office", "total": "40.00", "gl_account": B_OWN},
            ],
        )
    assert ok.status_code == 200, ok.text
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"].startswith("Line items: ")
    # The refusal ran before the delete-and-reinsert: the saved lines stand.
    assert await _line_codes(realdb, inv_id) == [A_OWN]


async def test_line_items_may_carry_over_a_code_the_lines_already_had(realdb, chart):
    entity_a, _ = chart
    inv_id = await _seed_invoice(realdb, entity_a)
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            InvoiceLineItem(
                invoice_id=inv_id, line_number=1, description="Legacy", gl_account=B_OWN
            )
        )
        await s.commit()
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[{"description": "Legacy, reworded", "gl_account": B_OWN}],
        )
    assert resp.status_code == 200, resp.text
    assert await _line_codes(realdb, inv_id) == [B_OWN]


# ---------------------------------------------------------------------------
# POST /api/invoices/{id}/approve — a GL correction is a write too
# ---------------------------------------------------------------------------


async def test_approve_refuses_a_gl_correction_from_another_entitys_chart(realdb, chart):
    entity_a, _ = chart
    inv_id = await _seed_invoice(realdb, entity_a, status=InvoiceStatus.ready_for_review)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        refused = await c.post(f"/api/invoices/{inv_id}/approve", json={"gl_account": B_OWN})
    assert refused.status_code == 422, refused.text
    inv = await _invoice(realdb, inv_id)
    assert inv.status == InvoiceStatus.ready_for_review
    assert inv.gl_account is None

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        ok = await c.post(f"/api/invoices/{inv_id}/approve", json={"gl_account": A_OWN})
    assert ok.status_code == 200, ok.text
    inv = await _invoice(realdb, inv_id)
    assert inv.status == InvoiceStatus.approved
    assert inv.gl_account == A_OWN


# ---------------------------------------------------------------------------
# POST /api/invoices/import-csv
# ---------------------------------------------------------------------------


async def test_csv_import_refuses_the_row_and_leaves_nothing_behind(realdb, chart):
    entity_a, _ = chart
    csv_bytes = (
        "invoice_number,vendor_name,amount,status,gl_account\n"
        f"GLC-CSV-1,Csv Kept Vendor,10.00,new,{A_OWN}\n"
        f"GLC-CSV-2,Csv Refused Vendor,20.00,new,{B_OWN}\n"
    ).encode()
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/invoices/import-csv",
            files={"file": ("invoices.csv", csv_bytes, "text/csv")},
            headers={"X-Entity-ID": str(entity_a)},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported"] == 1
    assert [e["row"] for e in body["errors"]] == [3]
    assert f"'{B_OWN}'" in body["errors"][0]["message"]

    async with realdb.sessionmaker(TENANT)() as s:
        numbers = set(
            (
                await s.execute(
                    select(Invoice.invoice_number).where(
                        Invoice.invoice_number.in_(["GLC-CSV-1", "GLC-CSV-2"])
                    )
                )
            )
            .scalars()
            .all()
        )
        refused_vendor = (
            await s.execute(select(func.count()).where(Vendor.name == "Csv Refused Vendor"))
        ).scalar_one()
    assert numbers == {"GLC-CSV-1"}
    # Refused before vendor resolution — no stub vendor for a row that failed.
    assert refused_vendor == 0


# ---------------------------------------------------------------------------
# /api/recurring — the template's code is stamped on every invoice it raises
# ---------------------------------------------------------------------------


def _template_body(gl: str) -> dict:
    return {
        "name": "Chart rent",
        "amount": "1000.00",
        "currency": "USD",
        "cadence": "monthly",
        "day_of_period": 1,
        "start_date": date.today().replace(day=1).isoformat(),
        "gl_account": gl,
    }


async def test_recurring_template_refuses_another_entitys_code(realdb, chart):
    entity_a, _ = chart
    headers = {"X-Entity-ID": str(entity_a)}
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        refused = await c.post("/api/recurring", json=_template_body(B_OWN), headers=headers)
        created = await c.post("/api/recurring", json=_template_body(A_OWN), headers=headers)
        assert created.status_code == 201, created.text
        tid = created.json()["id"]
        patch_refused = await c.patch(
            f"/api/recurring/{tid}", json={"gl_account": B_OWN}, headers=headers
        )
        patch_ok = await c.patch(
            f"/api/recurring/{tid}", json={"gl_account": SHARED}, headers=headers
        )
    assert refused.status_code == 422, refused.text
    assert patch_refused.status_code == 422, patch_refused.text
    assert patch_ok.status_code == 200, patch_ok.text
    async with realdb.sessionmaker(TENANT)() as s:
        tpl = (
            await s.execute(
                select(RecurringInvoiceTemplate).where(
                    RecurringInvoiceTemplate.id == uuid.UUID(tid)
                )
            )
        ).scalar_one()
    assert tpl.gl_account == SHARED


# ---------------------------------------------------------------------------
# GET /api/gl-accounts?chart_entity_id= — what the invoice pickers offer
# ---------------------------------------------------------------------------


async def test_chart_entity_id_returns_that_entitys_chart_whatever_is_selected(realdb, chart):
    entity_a, entity_b = chart
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        # Consolidated view, and with the OTHER entity selected: both answer
        # with A's chart when A's is what was asked for.
        consolidated = await c.get(f"/api/gl-accounts?chart_entity_id={entity_a}")
        other_selected = await c.get(
            f"/api/gl-accounts?chart_entity_id={entity_a}",
            headers={"X-Entity-ID": str(entity_b)},
        )
    for resp in (consolidated, other_selected):
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert sorted(r["code"] for r in rows) == sorted([SHARED, A_OWN, BOTH])
        assert all(r["entity_id"] in (None, str(entity_a)) for r in rows)


async def test_chart_entity_id_refuses_an_entity_this_tenant_does_not_have(realdb, chart):
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.get(f"/api/gl-accounts?chart_entity_id={uuid.uuid4()}")
    assert resp.status_code == 400, resp.text


async def test_invoice_response_carries_its_entity(realdb, chart):
    _, entity_b = chart
    inv_id = await _seed_invoice(realdb, entity_b)
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.get(f"/api/invoices/{inv_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["entity_id"] == str(entity_b)
