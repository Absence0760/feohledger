"""Real-DB coverage for GET /api/invoices/counts.

The list-page status chips ("All" + per-status) read this endpoint. It must
tally the *whole* tenant via a server-side GROUP BY — the previous
implementation counted client-side over the first page of results, so a
tenant with more invoices than the page window undercounted every chip.

These tests pin: an empty tenant returns zeros, mixed statuses tally per
status, and a status with more rows than the list page_size (25) is counted
in full — the exact case the old page-1 tally got wrong.
"""

from decimal import Decimal

import pytest

from app.models.invoice import Invoice, InvoiceStatus


async def _add_invoices(
    mk,
    org_id,
    status: InvoiceStatus,
    n: int,
    *,
    vendor_name: str = "Acme",
    number_prefix: str | None = None,
) -> None:
    async with mk() as s:
        for i in range(n):
            s.add(
                Invoice(
                    organization_id=org_id,
                    invoice_number=f"{number_prefix or 'INV'}-{status.value}-{i}",
                    vendor_name=vendor_name,
                    amount=Decimal("100.00"),
                    currency="USD",
                    status=status,
                )
            )
        await s.commit()


@pytest.mark.asyncio
async def test_counts_empty_tenant(realdb):
    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices/counts")
    assert resp.status_code == 200
    assert resp.json() == {"counts": {}, "total": 0}


@pytest.mark.asyncio
async def test_counts_group_by_status_full_tenant(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    # 30 'new' exceeds the list page_size (25): the old client-side tally
    # over page 1 would have reported at most 25 here.
    await _add_invoices(mk, org_id, InvoiceStatus.new, 30)
    await _add_invoices(mk, org_id, InvoiceStatus.approved, 4)
    await _add_invoices(mk, org_id, InvoiceStatus.paid, 2)

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices/counts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"] == {"new": 30, "approved": 4, "paid": 2}
    assert body["total"] == 36


@pytest.mark.asyncio
async def test_counts_honour_the_list_population_filters(realdb):
    """The chips must describe the SAME rows the table shows. `search` (and the
    advanced filters) run through the same `_invoice_list_filters` builder as
    `GET /api/invoices`, so searching a vendor no longer leaves the chips
    reading the whole tenant over a filtered table."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_invoices(mk, org_id, InvoiceStatus.new, 5, vendor_name="Globex Corp")
    await _add_invoices(mk, org_id, InvoiceStatus.approved, 3, vendor_name="Globex Corp")
    await _add_invoices(mk, org_id, InvoiceStatus.new, 7, vendor_name="Initech LLC")

    async with realdb.client(key="a") as c:
        unfiltered = (await c.get("/api/invoices/counts")).json()
        assert unfiltered["counts"] == {"new": 12, "approved": 3}
        assert unfiltered["total"] == 15

        # `search=globex` → only that vendor's rows, tallied per status.
        searched = (await c.get("/api/invoices/counts", params={"search": "globex"})).json()
        assert searched["counts"] == {"new": 5, "approved": 3}
        assert searched["total"] == 8

        # `vendor=` advanced filter, same result.
        by_vendor = (await c.get("/api/invoices/counts", params={"vendor": "Initech"})).json()
        assert by_vendor["counts"] == {"new": 7}
        assert by_vendor["total"] == 7


@pytest.mark.asyncio
async def test_counts_are_whole_set_within_a_filter_not_just_within_the_tenant(realdb):
    """The two halves of this defect compose, and each hides the other.

    Before PR #352 the chips ignored the filters; before that they were tallied
    client-side over page 1. A *filtered* population that is itself larger than
    the list window is the only case that fails if EITHER half regresses:
    a page-scoped tally caps at the window, and a filter-blind tally reports
    the whole tenant.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    # 26 matching rows (past the 20-row list window) + 9 that must not be counted.
    await _add_invoices(mk, org_id, InvoiceStatus.new, 18, vendor_name="Globex Corp")
    await _add_invoices(
        mk, org_id, InvoiceStatus.approved, 8, vendor_name="Globex Corp", number_prefix="GLB2"
    )
    await _add_invoices(mk, org_id, InvoiceStatus.new, 9, vendor_name="Initech LLC")

    async with realdb.client(key="a") as c:
        page = (await c.get("/api/invoices", params={"search": "globex"})).json()
        counts = (await c.get("/api/invoices/counts", params={"search": "globex"})).json()

    # The filtered set really does paginate …
    assert len(page["items"]) == 20 < page["total"] == 26
    # … and the chips describe all 26 of it, not the 20 loaded and not all 35.
    assert counts["counts"] == {"new": 18, "approved": 8}
    assert counts["total"] == 26


@pytest.mark.asyncio
async def test_counts_ignore_a_status_param(realdb):
    """`status` is the dimension being tallied — passing it (an inline-chip
    toggle re-uses the same param builder) must NOT zero the other chips."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_invoices(mk, org_id, InvoiceStatus.new, 4)
    await _add_invoices(mk, org_id, InvoiceStatus.approved, 2)

    async with realdb.client(key="a") as c:
        resp = await c.get("/api/invoices/counts", params={"status": "approved"})

    assert resp.json()["counts"] == {"new": 4, "approved": 2}


@pytest.mark.asyncio
async def test_counts_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/invoices/counts")
    assert resp.status_code == 401
