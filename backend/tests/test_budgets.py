"""Real-DB coverage for the procurement budgets router + spend-rollup service.

Covers ``backend/app/api/budgets.py`` + ``services/budget_service.py`` against
the live test tenants: budget CRUD, RBAC (read admin/ap_manager/cfo; mutate
admin/cfo), tenant isolation, audit rows, and — critically — the compute-on-read
spend rollup (allocated / committed / actual / remaining / utilization), seeded
with requisitions + POs + invoices and asserted with exact ``Decimal`` math.

DO NOT run this file standalone in a concurrent build — the ``realdb`` fixture
truncates all tables sequentially. The orchestrator runs the suite at the end.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.models.invoice import Invoice
from app.models.procurement import (
    Budget,
    PurchaseOrder,
    PurchaseRequisition,
    RequisitionStatus,
)
from app.models.workflow import AuditLog


def _u() -> str:
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# seed helpers (write directly to the tenant DB — other verticals' create
# endpoints aren't depended on here)
# ---------------------------------------------------------------------------


async def _mk_budget_row(
    realdb,
    key="a",
    *,
    dimension="department",
    dimension_value="Engineering",
    amount="10000.00",
    period="2026",
    currency="USD",
    entity_id=None,
) -> uuid.UUID:
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    bid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Budget(
                id=bid,
                name=f"Budget {_u()}",
                dimension=dimension,
                dimension_value=dimension_value,
                period=period,
                amount=Decimal(amount),
                currency=currency,
                entity_id=entity_id,
                organization_id=org_id,
            )
        )
        await s.commit()
    return bid


async def _mk_requisition(
    realdb,
    key,
    *,
    budget_id,
    total,
    status: RequisitionStatus,
    converted_po_id=None,
    entity_id=None,
    currency="USD",
) -> uuid.UUID:
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    rid = uuid.uuid4()
    async with mk() as s:
        s.add(
            PurchaseRequisition(
                id=rid,
                requisition_number=f"REQ-{_u()}",
                requester_user_id=uuid.uuid4(),
                budget_id=budget_id,
                total=Decimal(total),
                status=status,
                converted_po_id=converted_po_id,
                entity_id=entity_id,
                currency=currency,
                organization_id=org_id,
            )
        )
        await s.commit()
    return rid


async def _mk_po(realdb, key, *, total, status="open", currency="USD") -> uuid.UUID:
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    pid = uuid.uuid4()
    async with mk() as s:
        s.add(
            PurchaseOrder(
                id=pid,
                po_number=f"PO-{_u()}",
                total=Decimal(total),
                # Conversion stamps the requisition's code onto the PO.
                currency=currency,
                status=status,
                organization_id=org_id,
            )
        )
        await s.commit()
    return pid


async def _mk_invoice(
    realdb,
    key,
    *,
    amount,
    status,
    cost_center=None,
    gl_account=None,
    department=None,
    project=None,
    invoice_date=None,
    currency="USD",
    entity_id=None,
) -> uuid.UUID:
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    iid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=iid,
                invoice_number=f"INV-{_u()}",
                vendor_name="Acme Supply",
                amount=Decimal(amount),
                status=status,
                cost_center=cost_center,
                gl_account=gl_account,
                department=department,
                project=project,
                invoice_date=invoice_date,
                currency=currency,
                entity_id=entity_id,
                organization_id=org_id,
            )
        )
        await s.commit()
    return iid


async def _mk_entity(realdb, key="a", *, slug=None) -> uuid.UUID:
    """Create a non-default entity in the tenant and return its id."""
    from app.models.entity import Entity

    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    eid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Entity(
                id=eid,
                name=f"Sub {_u()}",
                slug=slug or f"sub-{_u()}",
                is_default=False,
                is_active=True,
                organization_id=org_id,
            )
        )
        await s.commit()
    return eid


async def _mk_budget_row_dated(
    realdb, *, dimension, dimension_value, amount, period, period_start, period_end
) -> uuid.UUID:
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    bid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Budget(
                id=bid,
                name=f"Budget {_u()}",
                dimension=dimension,
                dimension_value=dimension_value,
                period=period,
                period_start=period_start,
                period_end=period_end,
                amount=Decimal(amount),
                currency="USD",
                organization_id=org_id,
            )
        )
        await s.commit()
    return bid


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def test_create_budget(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post(
            "/api/budgets",
            json={
                "name": "Eng 2026",
                "dimension": "department",
                "dimension_value": "Engineering",
                "period": "2026",
                "amount": "50000.00",
                "currency": "USD",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Eng 2026"
    assert body["dimension"] == "department"
    assert body["amount"] == 50000.0

    async with mk() as s:
        b = (await s.execute(select(Budget).where(Budget.id == uuid.UUID(body["id"])))).scalar_one()
        assert b.amount == Decimal("50000.00")  # exact Numeric round-trip
        assert b.organization_id == org_id
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_type == "budget")))
            .scalars()
            .all()
        )
        assert "budget.created" in actions


async def test_list_filter_search_and_get(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        await c.post(
            "/api/budgets",
            json={
                "name": "Marketing Q1",
                "dimension": "department",
                "dimension_value": "Marketing",
                "period": "2026-Q1",
                "amount": "1000.00",
            },
        )
        eng = (
            await c.post(
                "/api/budgets",
                json={
                    "name": "Eng Proj X",
                    "dimension": "project",
                    "dimension_value": "Project X",
                    "period": "2026",
                    "amount": "2000.00",
                },
            )
        ).json()["id"]

        listing = await c.get("/api/budgets")
        assert listing.status_code == 200
        assert listing.json()["total"] >= 2

        # dimension filter
        proj = await c.get("/api/budgets", params={"dimension": "project"})
        assert all(b["dimension"] == "project" for b in proj.json()["items"])

        # search by name
        found = await c.get("/api/budgets", params={"search": "Proj X"})
        assert any(b["id"] == eng for b in found.json()["items"])

        one = await c.get(f"/api/budgets/{eng}")
        assert one.status_code == 200
        assert one.json()["amount"] == 2000.0


async def test_summary_groups_by_currency_and_honours_filters(realdb):
    """`GET /api/budgets/summary` is the whole-set KPI rollup: it counts every
    matching row (not just the loaded page), groups the allocation BY CURRENCY
    (never a cross-currency sum), and shares the list's dimension/period/search
    filters so the KPI can't contradict the table."""
    await _mk_budget_row(realdb, dimension="department", amount="1000.00", currency="USD")
    await _mk_budget_row(realdb, dimension="department", amount="2500.00", currency="USD")
    await _mk_budget_row(realdb, dimension="project", amount="400.00", currency="EUR")

    async with realdb.client(key="a", role="cfo") as c:
        res = await c.get("/api/budgets/summary")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 3
        by_ccy = {row["currency"]: row for row in body["by_currency"]}
        assert by_ccy["USD"]["total"] == "3500.00"
        assert by_ccy["USD"]["count"] == 2
        assert by_ccy["EUR"]["total"] == "400.00"
        # never a single blended figure
        assert set(by_ccy) == {"USD", "EUR"}

        # the dimension filter narrows the rollup exactly like the list
        filtered = (await c.get("/api/budgets/summary", params={"dimension": "project"})).json()
        assert filtered["total"] == 1
        assert [r["currency"] for r in filtered["by_currency"]] == ["EUR"]


async def test_summary_read_gate_matches_list(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.get("/api/budgets/summary")).status_code == 200
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.get("/api/budgets/summary")).status_code == 403


async def test_update_budget(realdb):
    mk = realdb.sessionmaker("a")
    bid = await _mk_budget_row(realdb, amount="100.00")
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.patch(f"/api/budgets/{bid}", json={"amount": "555.00", "name": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["amount"] == 555.0
    assert resp.json()["name"] == "Renamed"
    async with mk() as s:
        b = (await s.execute(select(Budget).where(Budget.id == bid))).scalar_one()
        assert b.amount == Decimal("555.00")
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.action == "budget.updated")))
            .scalars()
            .all()
        )
        assert len(actions) >= 1


async def test_delete_budget(realdb):
    bid = await _mk_budget_row(realdb)
    async with realdb.client(key="a", role="cfo") as c:
        gone = await c.delete(f"/api/budgets/{bid}")
        assert gone.status_code == 204
        missing = await c.get(f"/api/budgets/{bid}")
        assert missing.status_code == 404
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.action == "budget.deleted")))
            .scalars()
            .all()
        )
        assert len(actions) >= 1


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


async def test_ap_manager_can_read_but_not_mutate(realdb):
    bid = await _mk_budget_row(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.get("/api/budgets")).status_code == 200
        assert (await c.get(f"/api/budgets/{bid}")).status_code == 200
        # mutate is admin/cfo only
        created = await c.post(
            "/api/budgets",
            json={"name": "X", "dimension": "department", "dimension_value": "Y", "amount": "1.00"},
        )
        assert created.status_code == 403
        patched = await c.patch(f"/api/budgets/{bid}", json={"amount": "2.00"})
        assert patched.status_code == 403
        deleted = await c.delete(f"/api/budgets/{bid}")
        assert deleted.status_code == 403


async def test_ap_clerk_cannot_read_budgets(realdb):
    # ap_clerk is NOT in the read set (financial config).
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.get("/api/budgets")).status_code == 403


async def test_cfo_can_mutate(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post(
            "/api/budgets",
            json={
                "name": "CFO budget",
                "dimension": "cost_center",
                "dimension_value": "CC-100",
                "amount": "9000.00",
            },
        )
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# tenant isolation
# ---------------------------------------------------------------------------


async def test_tenant_isolation(realdb):
    bid = await _mk_budget_row(realdb, key="a")
    async with realdb.client(key="b", role="cfo") as c:
        assert (await c.get(f"/api/budgets/{bid}")).status_code == 404


# ---------------------------------------------------------------------------
# spend rollup — the core math
# ---------------------------------------------------------------------------


async def test_spend_empty_budget(realdb):
    bid = await _mk_budget_row(realdb, amount="10000.00")
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(f"/api/budgets/{bid}/spend")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["allocated"] == 10000.0
    assert body["committed"] == 0.0
    assert body["actual"] == 0.0
    assert body["remaining"] == 10000.0
    assert body["utilization_pct"] == 0.0


async def test_spend_committed_open_requisitions(realdb):
    bid = await _mk_budget_row(realdb, amount="10000.00")
    # Open commitments: submitted + pending_approval + approved all count.
    await _mk_requisition(
        realdb, "a", budget_id=bid, total="1000.00", status=RequisitionStatus.submitted
    )
    await _mk_requisition(
        realdb, "a", budget_id=bid, total="2000.00", status=RequisitionStatus.pending_approval
    )
    await _mk_requisition(
        realdb, "a", budget_id=bid, total="500.00", status=RequisitionStatus.approved
    )
    # Draft / rejected / cancelled must NOT count.
    await _mk_requisition(
        realdb, "a", budget_id=bid, total="9999.00", status=RequisitionStatus.draft
    )
    await _mk_requisition(
        realdb, "a", budget_id=bid, total="8888.00", status=RequisitionStatus.rejected
    )

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    assert body["committed"] == 3500.0  # 1000 + 2000 + 500
    assert body["actual"] == 0.0
    assert body["remaining"] == 6500.0
    assert body["utilization_pct"] == 35.0


async def test_spend_committed_keeps_a_cross_entity_linked_requisition(realdb):
    """`budget_id` is an unambiguous human-declared link, so the committed legs
    are NOT entity-scoped. Layering `apply_entity_scope` on top of the FK could
    only drop deliberately-linked demand — `committed` read 0 and
    `/budgets/check` answered `would_overspend: false` for headroom already
    spoken for. (Contrast the invoice leg, whose attribution is a fuzzy
    free-text dimension match and stays scoped.)"""
    other_entity = await _mk_entity(realdb, "a")
    bid = await _mk_budget_row(realdb, amount="10000.00", entity_id=other_entity)
    # Open requisition explicitly linked to the budget, but stamped with a
    # DIFFERENT entity (unstamped / default).
    await _mk_requisition(
        realdb,
        "a",
        budget_id=bid,
        total="2500.00",
        status=RequisitionStatus.approved,
        entity_id=None,
    )
    # ...and its converted sibling, whose PO rides leg 2 through the same FK.
    po_id = await _mk_po(realdb, "a", total="1500.00")
    await _mk_requisition(
        realdb,
        "a",
        budget_id=bid,
        total="1500.00",
        status=RequisitionStatus.converted,
        converted_po_id=po_id,
        entity_id=None,
    )

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    assert body["committed"] == 4000.0  # 2500 (leg 1) + 1500 (leg 2)


async def test_spend_committed_still_excludes_a_foreign_currency_requisition(realdb):
    """The currency predicate stays — the legs never convert, so summing two
    currencies' face values would be worse than excluding the row. New links
    can't be mismatched (POST/PATCH /requisitions 422s), so this only ever
    bites a row linked before that guard."""
    bid = await _mk_budget_row(realdb, amount="10000.00", currency="USD")
    await _mk_requisition(
        realdb,
        "a",
        budget_id=bid,
        total="700.00",
        status=RequisitionStatus.approved,
        currency="EUR",
    )

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    assert body["committed"] == 0.0


async def test_spend_converted_req_counts_po_not_req(realdb):
    """A converted requisition's commitment is its PO (leg 2), not the req (leg 1)
    — the two must never double-count."""
    bid = await _mk_budget_row(realdb, amount="10000.00")
    po_id = await _mk_po(realdb, "a", total="1500.00", status="open")
    await _mk_requisition(
        realdb,
        "a",
        budget_id=bid,
        total="1400.00",
        status=RequisitionStatus.converted,
        converted_po_id=po_id,
    )
    # A cancelled PO from a converted req must NOT count.
    dead_po = await _mk_po(realdb, "a", total="7777.00", status="cancelled")
    await _mk_requisition(
        realdb,
        "a",
        budget_id=bid,
        total="7000.00",
        status=RequisitionStatus.converted,
        converted_po_id=dead_po,
    )

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    # Only the live converted PO (1500) counts; the req amount (1400) and the
    # cancelled PO (7777) do not.
    assert body["committed"] == 1500.0
    assert body["remaining"] == 8500.0


async def test_spend_committed_po_leg_reads_the_pos_own_currency(realdb):
    """Leg 2 sums the PO's total, so it is the PO's currency that says what the
    figure is in — not the requisition's, which it merely started from. A USD
    requisition whose PO was later re-denominated to EUR must not add EUR to a
    USD budget; it is excluded and counted like any other unpriceable row
    (decisions §197)."""
    bid = await _mk_budget_row(realdb, amount="10000.00", currency="USD")
    po_id = await _mk_po(realdb, "a", total="900.00", currency="EUR")
    await _mk_requisition(
        realdb,
        "a",
        budget_id=bid,
        total="900.00",
        status=RequisitionStatus.converted,
        converted_po_id=po_id,
        currency="USD",
    )

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    assert body["committed"] == 0.0
    assert body["excluded_row_count"] == 1


async def test_spend_actual_cost_center_invoices(realdb):
    bid = await _mk_budget_row(
        realdb, dimension="cost_center", dimension_value="CC-200", amount="10000.00"
    )
    # Realised statuses count; new/rejected do not; wrong cost-center doesn't.
    await _mk_invoice(realdb, "a", amount="300.00", status="paid", cost_center="CC-200")
    await _mk_invoice(realdb, "a", amount="200.00", status="approved", cost_center="CC-200")
    await _mk_invoice(realdb, "a", amount="9999.00", status="new", cost_center="CC-200")
    await _mk_invoice(realdb, "a", amount="8888.00", status="paid", cost_center="CC-OTHER")

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    assert body["actual"] == 500.0  # 300 + 200
    assert body["committed"] == 0.0
    assert body["remaining"] == 9500.0


async def test_spend_actual_department_invoices(realdb):
    """Department budgets now sum realised invoices matched on Invoice.department
    (previously actual read 0 — see budget_service module docstring)."""
    bid = await _mk_budget_row(
        realdb, dimension="department", dimension_value="Engineering", amount="10000.00"
    )
    # Realised statuses count; new does not; wrong department doesn't.
    await _mk_invoice(realdb, "a", amount="300.00", status="paid", department="Engineering")
    await _mk_invoice(realdb, "a", amount="200.00", status="approved", department="Engineering")
    await _mk_invoice(realdb, "a", amount="9999.00", status="new", department="Engineering")
    await _mk_invoice(realdb, "a", amount="8888.00", status="paid", department="Marketing")

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    assert body["actual"] == 500.0  # 300 + 200
    assert body["committed"] == 0.0
    assert body["remaining"] == 9500.0


async def test_spend_actual_project_invoices(realdb):
    """Project budgets now sum realised invoices matched on Invoice.project."""
    bid = await _mk_budget_row(
        realdb, dimension="project", dimension_value="Project X", amount="10000.00"
    )
    await _mk_invoice(realdb, "a", amount="450.00", status="posted_in_erp", project="Project X")
    await _mk_invoice(realdb, "a", amount="50.00", status="done", project="Project X")
    await _mk_invoice(realdb, "a", amount="7777.00", status="rejected", project="Project X")
    await _mk_invoice(realdb, "a", amount="6666.00", status="paid", project="Project Y")

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    assert body["actual"] == 500.0  # 450 + 50
    assert body["committed"] == 0.0
    assert body["remaining"] == 9500.0


async def test_spend_actual_respects_period_window(realdb):
    """A period-bounded budget only counts invoices dated inside its window, so
    two budgets on the same dimension in different periods don't both report
    all-time spend (issue #153)."""
    cc = f"CC-{_u()}"
    q1 = await _mk_budget_row_dated(
        realdb,
        dimension="cost_center",
        dimension_value=cc,
        amount="10000.00",
        period="2026-Q1",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
    )
    q2 = await _mk_budget_row_dated(
        realdb,
        dimension="cost_center",
        dimension_value=cc,
        amount="10000.00",
        period="2026-Q2",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
    )
    # One invoice in each quarter, both realised, same cost center.
    await _mk_invoice(
        realdb, "a", amount="300.00", status="paid", cost_center=cc, invoice_date=date(2026, 2, 15)
    )
    await _mk_invoice(
        realdb, "a", amount="200.00", status="paid", cost_center=cc, invoice_date=date(2026, 5, 15)
    )

    async with realdb.client(key="a", role="cfo") as c:
        q1_body = (await c.get(f"/api/budgets/{q1}/spend")).json()
        q2_body = (await c.get(f"/api/budgets/{q2}/spend")).json()
    assert q1_body["actual"] == 300.0  # only the Feb invoice
    assert q2_body["actual"] == 200.0  # only the May invoice


async def test_spend_actual_excludes_foreign_currency_invoices(realdb):
    """A USD budget never sums a same-dimension invoice denominated in another
    currency — the legs don't convert (issue #154 bug B)."""
    cc = f"CC-{_u()}"
    bid = await _mk_budget_row(
        realdb, dimension="cost_center", dimension_value=cc, amount="10000.00", currency="USD"
    )
    await _mk_invoice(realdb, "a", amount="300.00", status="paid", cost_center=cc, currency="USD")
    await _mk_invoice(realdb, "a", amount="999.00", status="paid", cost_center=cc, currency="EUR")

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    assert body["actual"] == 300.0  # EUR row excluded, not added as 999


async def test_spend_actual_respects_entity_scope(realdb):
    """A budget scoped to one entity never picks up another entity's spend on a
    shared free-text dimension value (issue #154 bug A)."""
    other_entity = await _mk_entity(realdb, "a")
    cc = f"CC-{_u()}"
    # Budget belongs to the non-default entity.
    bid = await _mk_budget_row(
        realdb,
        dimension="cost_center",
        dimension_value=cc,
        amount="10000.00",
        entity_id=other_entity,
    )
    # One invoice in the budget's entity, one in a different (default/None) entity.
    await _mk_invoice(
        realdb, "a", amount="300.00", status="paid", cost_center=cc, entity_id=other_entity
    )
    await _mk_invoice(realdb, "a", amount="999.00", status="paid", cost_center=cc, entity_id=None)

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    assert body["actual"] == 300.0  # sibling-entity row excluded


async def test_spend_combined_committed_plus_actual(realdb):
    bid = await _mk_budget_row(
        realdb, dimension="gl_account", dimension_value="6000", amount="1000.00"
    )
    await _mk_requisition(
        realdb, "a", budget_id=bid, total="400.00", status=RequisitionStatus.approved
    )
    await _mk_invoice(realdb, "a", amount="300.00", status="paid", gl_account="6000")

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    assert body["committed"] == 400.0
    assert body["actual"] == 300.0
    assert body["remaining"] == 300.0
    assert body["utilization_pct"] == 70.0  # (400 + 300) / 1000


async def test_spend_overspend_negative_remaining(realdb):
    bid = await _mk_budget_row(
        realdb, dimension="cost_center", dimension_value="CC-OVER", amount="100.00"
    )
    await _mk_invoice(realdb, "a", amount="250.00", status="paid", cost_center="CC-OVER")
    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}/spend")).json()
    assert body["actual"] == 250.0
    assert body["remaining"] == -150.0
    assert body["utilization_pct"] == 250.0


async def test_spend_missing_budget_404(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        assert (await c.get(f"/api/budgets/{uuid.uuid4()}/spend")).status_code == 404


# ---------------------------------------------------------------------------
# check endpoint
# ---------------------------------------------------------------------------


async def test_check_within_budget(realdb):
    bid = await _mk_budget_row(realdb, amount="1000.00")
    await _mk_requisition(
        realdb, "a", budget_id=bid, total="400.00", status=RequisitionStatus.approved
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/budgets/check", params={"budget_id": str(bid), "amount": "500.00"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["remaining"] == 600.0  # 1000 - 400 committed
    assert body["remaining_after"] == 100.0  # 600 - 500
    assert body["would_overspend"] is False


async def test_check_would_overspend(realdb):
    bid = await _mk_budget_row(realdb, amount="1000.00")
    await _mk_requisition(
        realdb, "a", budget_id=bid, total="900.00", status=RequisitionStatus.approved
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/budgets/check", params={"budget_id": str(bid), "amount": "200.00"})
    body = resp.json()
    assert body["remaining"] == 100.0
    assert body["remaining_after"] == -100.0
    assert body["would_overspend"] is True


async def test_check_missing_budget_404(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(
            "/api/budgets/check", params={"budget_id": str(uuid.uuid4()), "amount": "1.00"}
        )
    assert resp.status_code == 404


async def test_check_dates_roundtrip(realdb):
    """period_start/period_end serialise as ISO strings on the response."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    bid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Budget(
                id=bid,
                name=f"Dated {_u()}",
                dimension="department",
                dimension_value="Ops",
                period="2026",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 12, 31),
                amount=Decimal("100.00"),
                organization_id=org_id,
            )
        )
        await s.commit()
    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get(f"/api/budgets/{bid}")).json()
    assert body["period_start"] == "2026-01-01"
    assert body["period_end"] == "2026-12-31"
