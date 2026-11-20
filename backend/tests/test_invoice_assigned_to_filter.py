"""Real-DB coverage for the `assigned_to_id` filter on `GET /api/invoices`
and `GET /api/invoices/ids`.

Backs the frontend's "Assigned to" filter and "My Approvals" quick view —
an approver narrowing the queue to invoices assigned to them (or to a
teammate) needs the backend to filter server-side; scanning the whole
queue client-side is unusable at any real team size. `assigned_to_id` is
an exact match on `Invoice.assigned_to_id`, shared by both endpoints via
`_invoice_list_filters` so "select all matching" under the same filter
can't silently widen past what's on screen.
"""

from decimal import Decimal

import pytest

from app.models.invoice import Invoice, InvoiceStatus


async def _add_invoice(mk, org_id, *, assigned_to_id=None, assigned_to=None, n=1) -> list[str]:
    objs: list[Invoice] = []
    async with mk() as s:
        for i in range(n):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"INV-assign-{assigned_to_id}-{i}",
                vendor_name="Acme",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.ready_for_review,
                assigned_to_id=assigned_to_id,
                assigned_to=assigned_to,
            )
            s.add(inv)
            objs.append(inv)
        await s.flush()
        ids = [str(inv.id) for inv in objs]
        await s.commit()
    return ids


@pytest.mark.asyncio
async def test_list_filters_by_assigned_to_id(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    manager_id = realdb.info("a").users["ap_manager"]
    cfo_id = realdb.info("a").users["cfo"]

    mine = await _add_invoice(mk, org_id, assigned_to_id=manager_id, assigned_to="AP Manager", n=2)
    await _add_invoice(mk, org_id, assigned_to_id=cfo_id, assigned_to="CFO", n=3)
    await _add_invoice(mk, org_id, n=1)  # unassigned

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices", params={"assigned_to_id": str(manager_id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    returned_ids = {row["id"] for row in body["items"]}
    assert returned_ids == set(mine)
    # The response already carries the display name — no separate lookup
    # needed to render an assignee column/cell.
    assert all(row["assigned_to"] == "AP Manager" for row in body["items"])


@pytest.mark.asyncio
async def test_list_assigned_to_id_excludes_unassigned_and_other_users(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    manager_id = realdb.info("a").users["ap_manager"]
    cfo_id = realdb.info("a").users["cfo"]

    await _add_invoice(mk, org_id, assigned_to_id=cfo_id, n=2)
    await _add_invoice(mk, org_id, n=2)  # unassigned

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices", params={"assigned_to_id": str(manager_id)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_ids_endpoint_honours_assigned_to_id(realdb):
    """Same filter set as the list endpoint, so "select all matching" under
    an assignee filter resolves the identical set the table shows."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    manager_id = realdb.info("a").users["ap_manager"]
    cfo_id = realdb.info("a").users["cfo"]

    mine = await _add_invoice(mk, org_id, assigned_to_id=manager_id, n=3)
    await _add_invoice(mk, org_id, assigned_to_id=cfo_id, n=2)

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices/ids", params={"assigned_to_id": str(manager_id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert set(body["ids"]) == set(mine)


@pytest.mark.asyncio
async def test_assigned_to_id_composes_with_status_filter(realdb):
    """`assigned_to_id` is one more AND clause, not a replacement — the
    "My Approvals" view still respects an active status chip."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    manager_id = realdb.info("a").users["ap_manager"]

    ready = await _add_invoice(mk, org_id, assigned_to_id=manager_id, n=1)
    async with mk() as s:
        approved = Invoice(
            organization_id=org_id,
            invoice_number="INV-assign-approved",
            vendor_name="Acme",
            amount=Decimal("50.00"),
            currency="USD",
            status=InvoiceStatus.approved,
            assigned_to_id=manager_id,
        )
        s.add(approved)
        await s.commit()

    async with realdb.client(key="a") as c:
        resp = await c.get(
            "/api/invoices",
            params={"assigned_to_id": str(manager_id), "status": "ready_for_review"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == ready[0]
