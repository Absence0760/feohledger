"""Real-DB coverage for GET /api/invoices/ids.

The "select all N matching" affordance on the invoices list page resolves
its selection through this endpoint rather than the currently-LOADED page
of `GET /api/invoices` (page_size 20) — the previous "select all" only ever
captured the loaded page, so a bulk action (delete / status-change / export)
silently skipped every row past it with no warning.

These tests pin: the endpoint returns every id matching the same filters as
the list endpoint (not just the first page's worth), `exclude_status` drops
the system-managed statuses a row's checkbox is disabled for, and the
`truncated` flag is honest when the match count exceeds the cap.
"""

from decimal import Decimal

import pytest

from app.models.invoice import Invoice, InvoiceStatus


async def _add_invoices(mk, org_id, status: InvoiceStatus, n: int, *, vendor="Acme") -> list[str]:
    objs: list[Invoice] = []
    async with mk() as s:
        for i in range(n):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"INV-{status.value}-{vendor}-{i}",
                vendor_name=vendor,
                amount=Decimal("100.00"),
                status=status,
            )
            s.add(inv)
            objs.append(inv)
        await s.flush()
        ids = [str(inv.id) for inv in objs]
        await s.commit()
    return ids


@pytest.mark.asyncio
async def test_ids_exceeds_a_single_list_page(realdb):
    """The core bug this endpoint fixes: more matching rows than one
    `GET /api/invoices` page (page_size 20) — every id must come back, not
    just the first page's worth."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    created = await _add_invoices(mk, org_id, InvoiceStatus.new, 45)

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices/ids", params={"status": "new"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 45
    assert body["truncated"] is False
    assert set(body["ids"]) == set(created)


@pytest.mark.asyncio
async def test_ids_honours_search_filter(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    matching = await _add_invoices(mk, org_id, InvoiceStatus.new, 3, vendor="Widgets Co")
    await _add_invoices(mk, org_id, InvoiceStatus.new, 3, vendor="Other Supplier")

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices/ids", params={"search": "Widgets"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert set(body["ids"]) == set(matching)


@pytest.mark.asyncio
async def test_ids_exclude_status_drops_system_managed(realdb):
    """`exclude_status` is how the frontend keeps "select all matching" from
    including rows whose checkbox is disabled (system-managed statuses)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    selectable = await _add_invoices(mk, org_id, InvoiceStatus.new, 2)
    await _add_invoices(mk, org_id, InvoiceStatus.paid, 2)

    excluded = "pending,sending_to_erp,sent_to_erp,posted_in_erp,payment_scheduled,paid,done"
    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices/ids", params={"exclude_status": excluded})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert set(body["ids"]) == set(selectable)


@pytest.mark.asyncio
async def test_ids_truncates_past_the_cap(realdb, monkeypatch):
    """A match count over the cap is reported truncated, never silently
    presented as a complete selection."""
    monkeypatch.setattr("app.api.invoices.MAX_SELECT_ALL_IDS", 5, raising=True)
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_invoices(mk, org_id, InvoiceStatus.new, 8)

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices/ids", params={"status": "new"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 8
    assert len(body["ids"]) == 5
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_ids_empty_tenant(realdb):
    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices/ids")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ids": [], "total": 0, "truncated": False, "versions": {}}


@pytest.mark.asyncio
async def test_ids_returns_each_rows_version_for_a_bound_bulk_approval(realdb):
    """A "select all matching" set reaches rows the page never loaded, so
    `/ids` hands back each one's `updated_at` — the version a bulk approval of
    that set is then bound to (decisions §263)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    created = await _add_invoices(mk, org_id, InvoiceStatus.ready_for_review, 3, vendor="Ver")

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices/ids", params={"status": "ready_for_review"})
        body = resp.json()
        assert set(body["versions"]) == set(body["ids"]) >= set(created)
        for inv_id in created:
            detail = (await c.get(f"/api/invoices/{inv_id}")).json()
            assert body["versions"][inv_id] == detail["updated_at"]
