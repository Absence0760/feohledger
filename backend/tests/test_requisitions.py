"""Real-DB coverage for the purchase-requisitions router.

Covers ``backend/app/api/requisitions.py`` + ``services/requisition_service.py``
end-to-end against the live test tenants: requisition CRUD with line items,
exact ``Numeric`` total recompute from lines, the approval state machine
(submit / approve / reject / cancel) incl. invalid-state 422s, segregation of
duties (the requester can't approve their own), requisition→PO conversion +
idempotency, RBAC, tenant isolation, and audit rows.

Mirrors the ``realdb`` idioms in ``tests/test_expenses.py`` /
``tests/test_expense_approval.py``. Requisition numbers are uuid-suffixed to
avoid collisions across tests (the table has no unique index, but distinct
numbers keep search assertions clean). DO NOT run this file standalone — the
orchestrator runs the full suite sequentially (the realdb fixture truncates
shared tables).
"""

import uuid
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.models.procurement import PurchaseOrder, PurchaseRequisition
from app.models.workflow import AuditLog


def _num(prefix: str = "REQ") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _payload(number: str, **kw):
    body = {
        "requisition_number": number,
        "title": kw.pop("title", "Laptops for eng"),
        "department": kw.pop("department", "Engineering"),
        "line_items": kw.pop(
            "line_items",
            [
                {"description": "Laptop", "quantity": "2", "unit_price": "1000.00"},
                {"description": "Dock", "quantity": "2", "unit_price": "150.00"},
            ],
        ),
    }
    body.update(kw)
    return body


# ---------------------------------------------------------------------------
# create_all parity
# ---------------------------------------------------------------------------


async def test_procurement_tables_exist(realdb):
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        for t in (
            "purchase_requisitions",
            "requisition_line_items",
            "catalogs",
            "catalog_items",
            "budgets",
            "intake_requests",
        ):
            await s.execute(text(f"SELECT 1 FROM {t} LIMIT 1"))  # raises if missing


# ---------------------------------------------------------------------------
# CRUD + total recompute
# ---------------------------------------------------------------------------


async def test_create_requisition_computes_total(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    number = _num()
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post("/api/requisitions", json=_payload(number))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["requisition_number"] == number
    assert body["status"] == "draft"
    # 2*1000 + 2*150 = 2300.00
    assert body["total"] == 2300.0
    assert len(body["line_items"]) == 2
    assert body["line_items"][0]["total"] == 2000.0

    async with mk() as s:
        req = (await s.execute(select(PurchaseRequisition))).scalar_one()
        assert req.total == Decimal("2300.00")  # exact, not float
        assert req.organization_id == org_id
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_type == "requisition")))
            .scalars()
            .all()
        )
        assert "requisition.created" in actions


async def test_list_filter_search_and_get(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        n1 = _num("FINDME")
        await c.post("/api/requisitions", json=_payload(n1, title="Special widget"))
        await c.post("/api/requisitions", json=_payload(_num(), title="Other"))

        listing = await c.get("/api/requisitions")
        assert listing.status_code == 200
        assert listing.json()["total"] >= 2

        found = await c.get(f"/api/requisitions?search={n1}")
        items = found.json()["items"]
        assert any(i["requisition_number"] == n1 for i in items)

        rid = items[0]["id"]
        one = await c.get(f"/api/requisitions/{rid}")
        assert one.status_code == 200


async def test_search_matches_department_not_just_number_and_title(realdb):
    """`department` is a column the list renders and the page's own search box
    matched client-side. Server-side search covering fewer columns than the UI
    already did is a straight regression — a buyer searching "Facilities" got
    nothing back."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        wanted = _num("DEPT")
        await c.post(
            "/api/requisitions",
            json=_payload(wanted, title="Chairs", department="Facilities Management"),
        )
        await c.post(
            "/api/requisitions",
            json=_payload(_num(), title="Laptops", department="Engineering"),
        )

        found = await c.get("/api/requisitions", params={"search": "facilities"})
        assert found.status_code == 200
        body = found.json()
        numbers = {i["requisition_number"] for i in body["items"]}
        assert wanted in numbers
        assert body["total"] == 1
        assert all(i["department"] == "Facilities Management" for i in body["items"])


async def test_search_department_composes_with_the_status_filter(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        draft = _num("DEPTDRAFT")
        submitted = _num("DEPTSUB")
        await c.post("/api/requisitions", json=_payload(draft, department="Facilities"))
        created = await c.post(
            "/api/requisitions", json=_payload(submitted, department="Facilities")
        )
        await c.post(f"/api/requisitions/{created.json()['id']}/submit")

        resp = await c.get(
            "/api/requisitions", params={"search": "facilities", "status": "pending_approval"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["requisition_number"] == submitted


async def test_summary_is_whole_set_grouped_by_currency_and_status(realdb):
    """`GET /api/requisitions/summary` counts the whole matching set, groups the
    value BY CURRENCY (never a cross-currency sum), and breaks out `by_status`
    — so the page's `pendingCount` / `periodTotal` KPIs stop describing only the
    loaded page. Shares the list's status/search filters."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        tag = uuid.uuid4().hex[:6].upper()
        # 2 USD drafts (1000 + 2000), 1 EUR draft (400), 1 USD submitted→pending.
        await c.post(
            "/api/requisitions",
            json=_payload(
                _num(f"SUM{tag}A"),
                title=f"sum-{tag}",
                line_items=[{"description": "x", "quantity": "1", "unit_price": "1000.00"}],
            ),
        )
        await c.post(
            "/api/requisitions",
            json=_payload(
                _num(f"SUM{tag}B"),
                title=f"sum-{tag}",
                line_items=[{"description": "x", "quantity": "1", "unit_price": "2000.00"}],
            ),
        )
        await c.post(
            "/api/requisitions",
            json=_payload(
                _num(f"SUM{tag}C"),
                title=f"sum-{tag}",
                currency="EUR",
                line_items=[{"description": "x", "quantity": "1", "unit_price": "400.00"}],
            ),
        )
        pend = await c.post(
            "/api/requisitions",
            json=_payload(
                _num(f"SUM{tag}D"),
                title=f"sum-{tag}",
                line_items=[{"description": "x", "quantity": "1", "unit_price": "500.00"}],
            ),
        )
        await c.post(f"/api/requisitions/{pend.json()['id']}/submit")

        body = (await c.get("/api/requisitions/summary", params={"search": f"sum-{tag}"})).json()
        assert body["total"] == 4
        assert body["by_status"]["draft"] == 3
        assert body["by_status"]["pending_approval"] == 1
        by_ccy = {r["currency"]: r for r in body["by_currency"]}
        assert by_ccy["USD"]["total"] == "3500.00"
        assert by_ccy["USD"]["count"] == 3
        assert by_ccy["EUR"]["total"] == "400.00"

        # the status filter narrows the rollup exactly like the list
        pending = (
            await c.get(
                "/api/requisitions/summary",
                params={"search": f"sum-{tag}", "status": "pending_approval"},
            )
        ).json()
        assert pending["total"] == 1
        assert pending["by_currency"][0]["total"] == "500.00"


async def test_update_draft_recomputes_total(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
        resp = await c.patch(
            f"/api/requisitions/{rid}",
            json={
                "title": "Updated title",
                "line_items": [{"description": "Server", "quantity": "1", "unit_price": "5000.00"}],
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Updated title"
    assert resp.json()["total"] == 5000.0
    assert len(resp.json()["line_items"]) == 1

    async with mk() as s:
        req = (
            await s.execute(
                select(PurchaseRequisition).where(PurchaseRequisition.id == uuid.UUID(rid))
            )
        ).scalar_one()
        assert req.total == Decimal("5000.00")


async def test_cannot_edit_non_draft(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
        await c.post(f"/api/requisitions/{rid}/submit")
        resp = await c.patch(f"/api/requisitions/{rid}", json={"title": "nope"})
    assert resp.status_code == 422


async def test_delete_draft(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
        gone = await c.delete(f"/api/requisitions/{rid}")
        assert gone.status_code == 204
        missing = await c.get(f"/api/requisitions/{rid}")
        assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Approval state machine
# ---------------------------------------------------------------------------


async def test_submit_transitions_to_pending(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
        resp = await c.post(f"/api/requisitions/{rid}/submit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending_approval"
    assert resp.json()["submitted_at"]

    async with mk() as s:
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_type == "requisition")))
            .scalars()
            .all()
        )
        assert "requisition.submitted" in actions


async def test_approve_self_blocked_by_segregation(realdb):
    # ap_manager creates (so requester = manager), submits, then tries to approve
    # their own requisition → 403 SoD.
    async with realdb.client(key="a", role="ap_manager") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
        await c.post(f"/api/requisitions/{rid}/submit")
        resp = await c.post(f"/api/requisitions/{rid}/approve")
    assert resp.status_code == 403
    assert "segregation" in resp.json()["detail"].lower()


async def test_different_manager_approves(realdb):
    mk = realdb.sessionmaker("a")
    # clerk requests + submits; a different manager approves.
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
        await c.post(f"/api/requisitions/{rid}/submit")
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/requisitions/{rid}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"
    assert body["approved_by"] == str(realdb.info("a").users["ap_manager"])

    async with mk() as s:
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_type == "requisition")))
            .scalars()
            .all()
        )
        assert "requisition.approved" in actions


async def test_reject_sets_reason(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
        await c.post(f"/api/requisitions/{rid}/submit")
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/requisitions/{rid}/reject", json={"reason": "over budget"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert resp.json()["rejection_reason"] == "over budget"


async def test_cancel_from_draft(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
        resp = await c.post(f"/api/requisitions/{rid}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


async def test_approve_invalid_state_422(realdb):
    # A draft (never submitted) requisition can't be approved.
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/requisitions/{rid}/approve")
    assert resp.status_code == 422


async def test_reopen_rejected_requisition_returns_it_to_draft(realdb):
    """A rejected requisition must be re-openable for rework.

    ``requisition_service.VALID_TRANSITIONS`` declares ``rejected -> draft`` and
    the lifecycle diagram documents it, but for a long time NO route performed
    the move: submit/cancel both 422 from ``rejected`` and PATCH is draft-only,
    so a rejected requisition was permanently stranded and the buyer's only
    recourse was DELETE + re-key.
    """
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
        await c.post(f"/api/requisitions/{rid}/submit")
    async with realdb.client(key="a", role="ap_manager") as c:
        rejected = await c.post(f"/api/requisitions/{rid}/reject", json={"reason": "over budget"})
    assert rejected.json()["status"] == "rejected"

    async with realdb.client(key="a", role="ap_clerk") as c:
        # Before the fix every exit from `rejected` was a dead end.
        assert (await c.post(f"/api/requisitions/{rid}/submit")).status_code == 422
        assert (await c.post(f"/api/requisitions/{rid}/cancel")).status_code == 422
        assert (
            await c.patch(f"/api/requisitions/{rid}", json={"title": "Reworked"})
        ).status_code == 422

        reopened = await c.post(f"/api/requisitions/{rid}/reopen")
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["status"] == "draft"
        assert reopened.json()["submitted_at"] is None
        # The reviewer's brief survives the reopen.
        assert reopened.json()["rejection_reason"] == "over budget"

        # ...and the row is editable + re-submittable again.
        patched = await c.patch(f"/api/requisitions/{rid}", json={"title": "Reworked"})
        assert patched.status_code == 200
        assert patched.json()["title"] == "Reworked"
        assert (await c.post(f"/api/requisitions/{rid}/submit")).json()["status"] == (
            "pending_approval"
        )

    async with mk() as s:
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_type == "requisition")))
            .scalars()
            .all()
        )
        assert "requisition.reopened" in actions


async def test_reopen_is_422_from_any_non_rejected_state(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
        # draft -> draft is not a declared transition.
        assert (await c.post(f"/api/requisitions/{rid}/reopen")).status_code == 422
        await c.post(f"/api/requisitions/{rid}/submit")
        assert (await c.post(f"/api/requisitions/{rid}/reopen")).status_code == 422


# ---------------------------------------------------------------------------
# Convert to PO + idempotency
# ---------------------------------------------------------------------------


async def _approved_requisition(realdb, **payload_kw) -> str:
    """Create (clerk) → submit → approve (manager). Returns the requisition id."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num(), **payload_kw))).json()["id"]
        await c.post(f"/api/requisitions/{rid}/submit")
    async with realdb.client(key="a", role="ap_manager") as c:
        await c.post(f"/api/requisitions/{rid}/approve")
    return rid


async def test_convert_creates_po(realdb):
    mk = realdb.sessionmaker("a")
    rid = await _approved_requisition(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/requisitions/{rid}/convert-to-po")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["total"] == 2300.0
    po_id = body["po_id"]

    # Requisition flips to converted + carries the PO link.
    async with realdb.client(key="a", role="ap_clerk") as c:
        req = (await c.get(f"/api/requisitions/{rid}")).json()
    assert req["status"] == "converted"
    assert req["converted_po_id"] == po_id

    async with mk() as s:
        po = (
            await s.execute(
                select(PurchaseOrder)
                .where(PurchaseOrder.id == uuid.UUID(po_id))
                .options(selectinload(PurchaseOrder.line_items))
            )
        ).scalar_one()
        assert po.total == Decimal("2300.00")  # exact carry-over
        assert len(po.line_items) == 2
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(AuditLog.action == "requisition.converted_to_po")
                )
            )
            .scalars()
            .all()
        )
        assert len(actions) >= 1


async def test_convert_stamps_the_requisitions_currency_on_the_po(realdb):
    """The PO is denominated in the currency its requisition was raised in.

    `purchase_orders` had no currency column, so a PO converted from a EUR
    requisition was booked with no record that it was EUR, and every surface
    labelled it with the org's currency instead (decisions §197). The code is
    normalised on the way across — the requisition schema admits lower case.
    """
    mk = realdb.sessionmaker("a")
    rid = await _approved_requisition(realdb, currency="eur")
    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.post(f"/api/requisitions/{rid}/convert-to-po")
        replay = await c.post(f"/api/requisitions/{rid}/convert-to-po")
        assert first.status_code == 200, first.text
        po_id = first.json()["po_id"]
        detail = await c.get(f"/api/purchase-orders/{po_id}")

    assert first.json()["currency"] == "EUR"
    # The idempotent replay reports the same PO in the same currency.
    assert replay.json()["created"] is False
    assert replay.json()["currency"] == "EUR"
    assert detail.json()["currency"] == "EUR"
    async with mk() as s:
        po = (
            await s.execute(select(PurchaseOrder).where(PurchaseOrder.id == uuid.UUID(po_id)))
        ).scalar_one()
    assert po.currency == "EUR"


async def test_convert_is_idempotent(realdb):
    mk = realdb.sessionmaker("a")
    rid = await _approved_requisition(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.post(f"/api/requisitions/{rid}/convert-to-po")
        second = await c.post(f"/api/requisitions/{rid}/convert-to-po")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    # Same PO returned — no second PO created.
    assert first.json()["po_id"] == second.json()["po_id"]

    async with mk() as s:
        po_count = (await s.execute(select(PurchaseOrder))).scalars().all()
        assert len(po_count) == 1


async def test_convert_unapproved_422(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/requisitions/{rid}/convert-to-po")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# RBAC + tenant isolation
# ---------------------------------------------------------------------------


async def test_cfo_cannot_create(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/requisitions", json=_payload(_num()))
    assert resp.status_code == 403


async def test_cfo_can_read(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        await c.post("/api/requisitions", json=_payload(_num()))
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/requisitions")
    assert resp.status_code == 200


async def test_clerk_cannot_convert(realdb):
    rid = await _approved_requisition(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/requisitions/{rid}/convert-to-po")
    assert resp.status_code == 403


async def test_tenant_isolation(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
    async with realdb.client(key="b", role="ap_manager") as c:
        assert (await c.get(f"/api/requisitions/{rid}")).status_code == 404


# ---------------------------------------------------------------------------
# Optional FK links are validated at write time
# ---------------------------------------------------------------------------


async def _mk_budget(realdb, key="a", *, currency="USD") -> str:
    from app.models.procurement import Budget

    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    bid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Budget(
                id=bid,
                name=f"Budget {uuid.uuid4().hex[:8]}",
                dimension="department",
                dimension_value="Engineering",
                period="2026",
                amount=Decimal("10000.00"),
                currency=currency,
                organization_id=org_id,
            )
        )
        await s.commit()
    return str(bid)


async def test_create_rejects_an_unknown_budget_id(realdb):
    """A well-formed but non-existent id used to be stored verbatim and reach
    an FK violation at flush — a 500 for input the caller got wrong."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post("/api/requisitions", json=_payload(_num(), budget_id=str(uuid.uuid4())))
    assert resp.status_code == 404, resp.text
    assert "Budget" in resp.json()["detail"]


async def test_create_rejects_an_unknown_vendor_or_contract(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        v = await c.post("/api/requisitions", json=_payload(_num(), vendor_id=str(uuid.uuid4())))
        k = await c.post("/api/requisitions", json=_payload(_num(), contract_id=str(uuid.uuid4())))
    assert v.status_code == 404, v.text
    assert k.status_code == 404, k.text


async def test_create_rejects_a_cross_currency_budget_link(realdb):
    """`budget_id` is what `services/budget_service` sums `committed` over, and
    the legs never convert — a EUR requisition linked to a USD budget would be
    silently dropped from the rollup, so `/budgets/check` answers
    `would_overspend: false` for headroom already spoken for."""
    bid = await _mk_budget(realdb, currency="USD")
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/requisitions", json=_payload(_num(), budget_id=bid, currency="EUR")
        )
    assert resp.status_code == 422, resp.text
    assert "USD" in resp.json()["detail"]


async def test_create_accepts_a_matching_budget_link(realdb):
    bid = await _mk_budget(realdb, currency="USD")
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/requisitions", json=_payload(_num(), budget_id=bid, currency="USD")
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["budget_id"] == bid


async def test_patch_currency_alone_cannot_orphan_an_existing_budget_link(realdb):
    """Changing `currency` on a requisition that already names a budget would
    leave a link the rollup drops — re-check the pair rather than accept it."""
    bid = await _mk_budget(realdb, currency="USD")
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (
            await c.post("/api/requisitions", json=_payload(_num(), budget_id=bid, currency="USD"))
        ).json()["id"]
        resp = await c.patch(f"/api/requisitions/{rid}", json={"currency": "EUR"})
    assert resp.status_code == 422, resp.text


async def test_patch_rejects_an_unknown_budget_id(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/requisitions", json=_payload(_num()))).json()["id"]
        resp = await c.patch(f"/api/requisitions/{rid}", json={"budget_id": str(uuid.uuid4())})
    assert resp.status_code == 404, resp.text
