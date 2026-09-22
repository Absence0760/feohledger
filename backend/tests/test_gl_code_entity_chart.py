"""An invoice's GL code must resolve in the invoice's OWN chart.

``Invoice.gl_account`` / ``InvoiceLineItem.gl_account`` store a code, not a
foreign key (``docs/decisions.md`` §186), and a code means the account it names
in the chart of the entity the invoice belongs to — shared accounts ∪ that
entity's own (``docs/multi-entity.md`` § Chart of accounts). Two subsidiaries
may each hold their own ``6000``.

``services/gl_chart`` holds every manual write to that rule, in two parts:

* **never another entity's** (§194) — before it, the consolidated-view picker
  offered subsidiary B's code on a subsidiary-A invoice and nothing
  server-side checked; the string then resolved against A's chart as a
  different account, or none;
* **an active account of the invoice's chart whenever that chart has any**
  (§199) — a mistyped code, or a retired account's, was accepted on every
  manual write while extraction and bulk re-code already refused it.

These tests pin both on every path that writes the column (create, PATCH, the
line-items PUT, approve-with-corrections, CSV import, a recurring template that
will stamp it), with an active chart and with an empty one; the carve-outs that
keep existing rows editable (only a code NEW to the row is judged); CSV's
history exemption; and the `chart_entity_id` list parameter the pickers use to
offer exactly the codes a write will accept. Bulk re-code already validated per
invoice entity (`tests/test_entity_coa.py`).

Real-Postgres harness (``realdb``), tenant ``a``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.models.gl_account import GLAccount
from app.models.invoice import Invoice, InvoiceLineItem, InvoiceStatus
from app.models.recurring_invoice import RecurringInvoiceTemplate
from app.models.vendor import Vendor
from app.services.gl_chart import ChartOwnership, ChartRefusal, InvoiceChart

TENANT = "a"

# The fixture chart. `A` is the tenant's default entity, `B` a subsidiary.
SHARED = "1000"  # shared — valid for every entity
A_OWN = "7000"  # entity A's own
B_OWN = "6000"  # entity B's own — the code a consolidated picker used to offer
BOTH = "6500"  # A and B each define their own — valid for both
A_RETIRED = "6800"  # A's own, retired — still A's (not "another entity's"), but not active
UNKNOWN = "9999"  # in no chart at all

# What a write of each code to an entity-A invoice must do, with the fixture
# chart active. `None` = stored; otherwise the word the refusal must carry.
ACTIVE_CHART_VERDICTS: list[tuple[str, str | None]] = [
    (SHARED, None),
    (A_OWN, None),
    (BOTH, None),
    (A_RETIRED, "retired"),
    (UNKNOWN, "not in this invoice's chart"),
    (B_OWN, "another entity's chart"),
]
# ...and with entity A's effective ACTIVE chart empty (`empty_chart`): nothing
# to hold a code to, so only another entity's code is refused.
EMPTY_CHART_VERDICTS: list[tuple[str, str | None]] = [
    (A_RETIRED, None),
    (UNKNOWN, None),
    (B_OWN, "another entity's chart"),
]


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


@pytest_asyncio.fixture
async def empty_chart(realdb):
    """(entity_a, entity_b) where entity A's effective ACTIVE chart is EMPTY —
    no shared accounts, and A's own only retired — while B's is live. The
    tenant a subsidiary lands in before anyone has built its chart."""
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
                GLAccount(organization_id=org_id, code=B_OWN, name="B Office", entity_id=entity_b),
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


def _ownership(a: uuid.UUID, b: uuid.UUID) -> ChartOwnership:
    """The fixture chart, as `load_chart_ownership` would read it."""
    return ChartOwnership(
        shared=frozenset({SHARED}),
        by_entity={a: frozenset({A_OWN, BOTH, A_RETIRED}), b: frozenset({B_OWN, BOTH})},
        active_shared=frozenset({SHARED}),
        active_by_entity={a: frozenset({A_OWN, BOTH}), b: frozenset({B_OWN, BOTH})},
    )


def test_judge_with_an_active_chart_refuses_every_code_that_is_not_an_active_account_of_it():
    a, b = uuid.uuid4(), uuid.uuid4()
    chart = InvoiceChart(ownership=_ownership(a, b), entity_id=a, has_active_accounts=True)
    for code, verdict in ACTIVE_CHART_VERDICTS:
        assert bool(chart.judge([code])) is (verdict is not None), code
    refusal = chart.judge([SHARED, A_RETIRED, UNKNOWN, B_OWN, None, ""])
    assert refusal == ChartRefusal(foreign=(B_OWN,), retired=(A_RETIRED,), unknown=(UNKNOWN,))


def test_judge_with_an_empty_chart_refuses_only_another_entitys_code():
    a, b = uuid.uuid4(), uuid.uuid4()
    chart = InvoiceChart(ownership=_ownership(a, b), entity_id=a, has_active_accounts=False)
    assert chart.judge([A_RETIRED, UNKNOWN]) == ChartRefusal()
    assert chart.judge([B_OWN]) == ChartRefusal(foreign=(B_OWN,))


def test_judge_without_require_active_is_194_alone():
    """CSV history rows: a retired or unknown code is history, another
    entity's is still refused — the active chart does not change that."""
    a, b = uuid.uuid4(), uuid.uuid4()
    chart = InvoiceChart(ownership=_ownership(a, b), entity_id=a, has_active_accounts=True)
    assert chart.judge([A_RETIRED, UNKNOWN], require_active=False) == ChartRefusal()
    assert chart.judge([B_OWN], require_active=False) == ChartRefusal(foreign=(B_OWN,))


def test_a_code_retired_in_its_own_chart_is_retired_not_foreign_even_when_live_elsewhere():
    a, b = uuid.uuid4(), uuid.uuid4()
    own = ChartOwnership(
        shared=frozenset(),
        by_entity={a: frozenset({"6000"}), b: frozenset({"6000"})},
        active_shared=frozenset(),
        active_by_entity={b: frozenset({"6000"})},
    )
    chart = InvoiceChart(ownership=own, entity_id=a, has_active_accounts=True)
    assert chart.judge(["6000"]) == ChartRefusal(retired=("6000",))


def test_refusal_detail_names_each_code_with_its_reason():
    assert ChartRefusal(foreign=(B_OWN,)).detail() == (
        f"GL account '{B_OWN}' belongs to another entity's chart of accounts, not this "
        "invoice's. Choose a code from the invoice's own chart — the shared accounts plus "
        "its entity's own."
    )
    both = ChartRefusal(retired=("6800", "6900"), unknown=(UNKNOWN,)).detail(where="Line items")
    assert both == (
        "Line items: GL accounts '6800', '6900' are retired in this invoice's chart; "
        f"GL account '{UNKNOWN}' is not in this invoice's chart of accounts. Choose an "
        "active code from the invoice's own chart — the shared accounts plus its entity's own."
    )


# ---------------------------------------------------------------------------
# Every write path × every kind of code
#
# One writer per path. Each writes `code` to a fresh entity-A row through the
# real endpoint and reports (status, refusal text or None, what was stored).
# ---------------------------------------------------------------------------


async def _write_create(c, realdb, entity_id, code):
    resp = await c.post(
        "/api/invoices",
        json=_create_body(f"GLW-C-{uuid.uuid4().hex[:8]}", code),
        headers={"X-Entity-ID": str(entity_id)},
    )
    if resp.status_code != 201:
        return resp.status_code, resp.json()["detail"], None
    return 201, None, resp.json()["gl_account"]


async def _write_patch(c, realdb, entity_id, code):
    inv_id = await _seed_invoice(realdb, entity_id)
    resp = await c.patch(f"/api/invoices/{inv_id}", json={"gl_account": code, "notes": "coded"})
    stored = (await _invoice(realdb, inv_id)).gl_account
    return resp.status_code, resp.json().get("detail"), stored


async def _write_line_items(c, realdb, entity_id, code):
    inv_id = await _seed_invoice(realdb, entity_id)
    resp = await c.put(
        f"/api/invoices/{inv_id}/line-items",
        json=[{"description": "Line", "total": "100.00", "gl_account": code}],
    )
    codes = await _line_codes(realdb, inv_id)
    detail = resp.json().get("detail") if resp.status_code != 200 else None
    return resp.status_code, detail, codes[0] if codes else None


async def _write_approve(c, realdb, entity_id, code):
    inv_id = await _seed_invoice(realdb, entity_id, status=InvoiceStatus.ready_for_review)
    resp = await c.post(f"/api/invoices/{inv_id}/approve", json={"gl_account": code})
    stored = (await _invoice(realdb, inv_id)).gl_account
    return resp.status_code, resp.json().get("detail"), stored


async def _write_recurring_create(c, realdb, entity_id, code):
    resp = await c.post(
        "/api/recurring", json=_template_body(code), headers={"X-Entity-ID": str(entity_id)}
    )
    if resp.status_code != 201:
        return resp.status_code, resp.json()["detail"], None
    return 201, None, resp.json()["gl_account"]


async def _write_recurring_patch(c, realdb, entity_id, code):
    headers = {"X-Entity-ID": str(entity_id)}
    body = {**_template_body(code), "gl_account": None}
    created = await c.post("/api/recurring", json=body, headers=headers)
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    resp = await c.patch(f"/api/recurring/{tid}", json={"gl_account": code}, headers=headers)
    return resp.status_code, resp.json().get("detail"), resp.json().get("gl_account")


async def _import_csv(c, entity_id, rows: list[tuple[str, str, str]]):
    """rows: (invoice_number, status, gl_account)."""
    lines = ["invoice_number,vendor_name,amount,status,gl_account"]
    lines += [f"{n},Csv Vendor {n},10.00,{st},{gl}" for n, st, gl in rows]
    return await c.post(
        "/api/invoices/import-csv",
        files={"file": ("invoices.csv", ("\n".join(lines) + "\n").encode(), "text/csv")},
        headers={"X-Entity-ID": str(entity_id)},
    )


async def _stored_by_number(realdb, number: str) -> str | None:
    async with realdb.sessionmaker(TENANT)() as s:
        return (
            await s.execute(select(Invoice.gl_account).where(Invoice.invoice_number == number))
        ).scalar_one_or_none()


async def _write_csv_new(c, realdb, entity_id, code):
    number = f"GLW-CSV-{uuid.uuid4().hex[:8]}"
    resp = await _import_csv(c, entity_id, [(number, "new", code)])
    assert resp.status_code == 200, resp.text
    errors = resp.json()["errors"]
    if errors:
        return 422, errors[0]["message"], await _stored_by_number(realdb, number)
    return 200, None, await _stored_by_number(realdb, number)


WRITERS = {
    "create": _write_create,
    "patch": _write_patch,
    "line_items": _write_line_items,
    "approve_correction": _write_approve,
    "recurring_create": _write_recurring_create,
    "recurring_patch": _write_recurring_patch,
    "csv_new_row": _write_csv_new,
}


async def _assert_verdicts(realdb, entity_id, write, verdicts):
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        for code, refused_because in verdicts:
            status, detail, stored = await write(c, realdb, entity_id, code)
            if refused_because is None:
                assert status in (200, 201), (code, detail)
                assert stored == code, code
            else:
                assert status == 422, (code, status, detail)
                assert f"'{code}'" in detail and refused_because in detail, (code, detail)
                assert stored is None, f"a refused {code!r} must not be stored"


@pytest.mark.parametrize("path", list(WRITERS))
async def test_every_write_path_requires_an_active_account_of_the_invoices_chart(
    realdb, chart, path
):
    entity_a, _ = chart
    await _assert_verdicts(realdb, entity_a, WRITERS[path], ACTIVE_CHART_VERDICTS)


@pytest.mark.parametrize("path", list(WRITERS))
async def test_every_write_path_with_an_empty_chart_refuses_only_another_entitys_code(
    realdb, empty_chart, path
):
    entity_a, _ = empty_chart
    await _assert_verdicts(realdb, entity_a, WRITERS[path], EMPTY_CHART_VERDICTS)


# ---------------------------------------------------------------------------
# Only a code NEW to the row is judged — an existing row stays editable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stored", [A_RETIRED, UNKNOWN, B_OWN])
async def test_an_edit_that_leaves_an_unacceptable_stored_code_alone_goes_through(
    realdb, chart, stored
):
    """An invoice coded before its account was retired (or before either rule
    existed) must stay editable: an edit that does not touch the GL code, or
    echoes it back unchanged as the web form does, is not a coding decision."""
    entity_a, _ = chart
    inv_id = await _seed_invoice(realdb, entity_a, gl=stored)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        untouched = await c.patch(f"/api/invoices/{inv_id}", json={"notes": "unrelated"})
        echoed = await c.patch(
            f"/api/invoices/{inv_id}", json={"gl_account": stored, "notes": "echoed"}
        )
        recoded = await c.patch(f"/api/invoices/{inv_id}", json={"gl_account": A_OWN})
    assert untouched.status_code == 200, untouched.text
    assert echoed.status_code == 200, echoed.text
    assert recoded.status_code == 200, recoded.text
    assert (await _invoice(realdb, inv_id)).gl_account == A_OWN


@pytest.mark.parametrize("stored", [A_RETIRED, UNKNOWN])
async def test_approve_does_not_judge_a_stored_code_it_is_not_changing(realdb, chart, stored):
    entity_a, _ = chart
    inv_id = await _seed_invoice(realdb, entity_a, gl=stored, status=InvoiceStatus.ready_for_review)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{inv_id}/approve", json={"gl_account": stored, "cost_center": "CC"}
        )
    assert resp.status_code == 200, resp.text
    inv = await _invoice(realdb, inv_id)
    assert inv.status == InvoiceStatus.approved
    assert inv.gl_account == stored


async def test_recurring_patch_does_not_judge_a_stored_code_it_is_not_changing(realdb, chart):
    entity_a, _ = chart
    headers = {"X-Entity-ID": str(entity_a)}
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        created = await c.post("/api/recurring", json=_template_body(A_OWN), headers=headers)
        assert created.status_code == 201, created.text
        tid = created.json()["id"]
    # The account is retired after the template was written.
    async with realdb.sessionmaker(TENANT)() as s:
        acct = (await s.execute(select(GLAccount).where(GLAccount.code == A_OWN))).scalar_one()
        acct.is_active = False
        await s.commit()
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        # The recurring form re-sends every field.
        resp = await c.patch(
            f"/api/recurring/{tid}",
            json={**_template_body(A_OWN), "name": "Renamed rent"},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed rent"
    assert resp.json()["gl_account"] == A_OWN


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


@pytest.mark.parametrize("legacy", [B_OWN, A_RETIRED, UNKNOWN])
async def test_line_items_may_carry_over_a_code_the_lines_already_had(realdb, chart, legacy):
    """The PUT is a delete-and-reinsert of every line, so a line whose code has
    since been retired (or was never valid) comes back on every save of its
    siblings — that is not a coding decision, and refusing it would freeze
    the invoice's lines. A NEW line still has to name an acceptable code."""
    entity_a, _ = chart
    inv_id = await _seed_invoice(realdb, entity_a)
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            InvoiceLineItem(
                invoice_id=inv_id, line_number=1, description="Legacy", gl_account=legacy
            )
        )
        await s.commit()
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[
                {"description": "Legacy, reworded", "gl_account": legacy},
                {"description": "New line", "gl_account": A_OWN},
            ],
        )
        refused = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[
                {"description": "Legacy, reworded", "gl_account": legacy},
                {"description": "New line", "gl_account": A_OWN},
                {"description": "Mistyped", "gl_account": "9998"},
            ],
        )
    assert resp.status_code == 200, resp.text
    assert refused.status_code == 422, refused.text
    assert "'9998'" in refused.json()["detail"]
    assert await _line_codes(realdb, inv_id) == [legacy, A_OWN]


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


async def test_csv_history_rows_may_carry_a_gone_account_but_live_rows_may_not(realdb, chart):
    """A `done` / `paid` row is history: the account it was booked to may be
    long retired, or never have been created in this chart, so only §194's
    another-entity refusal applies to it. `new` and `rejected` rows reach
    approval (`rejected` through resubmission) and take the full rule."""
    entity_a, _ = chart
    rows = [
        ("GLH-DONE-RET", "done", A_RETIRED),
        ("GLH-DONE-UNK", "done", UNKNOWN),
        ("GLH-PAID-RET", "paid", A_RETIRED),
        ("GLH-PAID-UNK", "paid", UNKNOWN),
        ("GLH-DONE-FOREIGN", "done", B_OWN),
        ("GLH-PAID-FOREIGN", "paid", B_OWN),
        ("GLH-NEW-RET", "new", A_RETIRED),
        ("GLH-NEW-UNK", "new", UNKNOWN),
        ("GLH-REJ-RET", "rejected", A_RETIRED),
        ("GLH-REJ-UNK", "rejected", UNKNOWN),
        ("GLH-REJ-OK", "rejected", A_OWN),
    ]
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await _import_csv(c, entity_a, rows)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    imported = {"GLH-DONE-RET", "GLH-DONE-UNK", "GLH-PAID-RET", "GLH-PAID-UNK", "GLH-REJ-OK"}
    assert body["imported"] == len(imported)
    by_row = {e["row"]: e["message"] for e in body["errors"]}
    refused_rows = {i + 2: r for i, r in enumerate(rows) if r[0] not in imported}
    assert set(by_row) == set(refused_rows)
    for row_no, (_, _, code) in refused_rows.items():
        assert f"'{code}'" in by_row[row_no], by_row[row_no]
    assert "another entity's chart" in by_row[6] and "another entity's chart" in by_row[7]
    assert "retired" in by_row[8] and "not in this invoice's chart" in by_row[9]

    for number in imported:
        code = next(r[2] for r in rows if r[0] == number)
        assert await _stored_by_number(realdb, number) == code, number
    async with realdb.sessionmaker(TENANT)() as s:
        stub_vendors = (
            await s.execute(
                select(func.count())
                .select_from(Vendor)
                .where(Vendor.name.in_([f"Csv Vendor {r[0]}" for r in refused_rows.values()]))
            )
        ).scalar_one()
    assert stub_vendors == 0, "a refused row must not leave a vendor stub behind"


async def test_csv_row_with_an_unimportable_status_leaves_no_vendor_stub(realdb, chart):
    """The status is now read before the vendor is resolved (the GL rule
    depends on it), so a row refused for its status leaves nothing behind
    either — it used to mint an `unverified` vendor on its way to the error."""
    entity_a, _ = chart
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await _import_csv(
            c, entity_a, [("GLH-BAD-1", "approved", A_OWN), ("GLH-BAD-2", "bogus", A_OWN)]
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 0
    assert len(resp.json()["errors"]) == 2
    async with realdb.sessionmaker(TENANT)() as s:
        stub_vendors = (
            await s.execute(
                select(func.count())
                .select_from(Vendor)
                .where(Vendor.name.in_(["Csv Vendor GLH-BAD-1", "Csv Vendor GLH-BAD-2"]))
            )
        ).scalar_one()
    assert stub_vendors == 0


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
