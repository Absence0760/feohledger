"""The canonical list-pagination contract (``app/api/pagination.py``).

Every paginated list endpoint shares one envelope — ``items`` / ``total`` /
``page`` / ``page_size`` — one default page size, and one upper bound. These
tests pin that contract against representative endpoints so a future endpoint
can't quietly drift onto a different default or cap, and assert the deliberate
exception: ``/gl-accounts`` is a bounded reference collection whose consumers
all need every row, and stays unpaginated.
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.api.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.models.gl_account import GLAccount
from app.models.invoice import Invoice, InvoiceStatus


async def _add_invoices(mk, org_id, n: int, *, prefix: str, created_at=None) -> None:
    async with mk() as s:
        s.add_all(
            [
                Invoice(
                    organization_id=org_id,
                    invoice_number=f"{prefix}-{i:03d}",
                    vendor_name="Pagey Vendor",
                    amount=Decimal("100.00"),
                    status=InvoiceStatus.new,
                    **({"created_at": created_at} if created_at is not None else {}),
                )
                for i in range(n)
            ]
        )
        await s.commit()


async def _add_gl_accounts(mk, org_id, n: int, *, prefix: str) -> None:
    async with mk() as s:
        s.add_all(
            [
                GLAccount(
                    organization_id=org_id,
                    code=f"{prefix}{i:03d}",
                    name=f"Account {i}",
                    account_type="expense",
                )
                for i in range(n)
            ]
        )
        await s.commit()


async def test_default_envelope_and_page_size(realdb):
    """A bare list call returns DEFAULT_PAGE_SIZE rows in the canonical
    envelope, and total reflects the full (unpaged) count."""
    org_id = realdb.info("a").org_id
    await _add_invoices(realdb.sessionmaker("a"), org_id, DEFAULT_PAGE_SIZE + 5, prefix="PAGE-A")

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get("/api/invoices")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"items", "total", "page", "page_size"}
    assert body["page"] == 1
    assert body["page_size"] == DEFAULT_PAGE_SIZE
    assert len(body["items"]) == DEFAULT_PAGE_SIZE
    assert body["total"] >= DEFAULT_PAGE_SIZE + 5


async def test_second_page_returns_remainder(realdb):
    org_id = realdb.info("b").org_id
    await _add_invoices(realdb.sessionmaker("b"), org_id, DEFAULT_PAGE_SIZE + 3, prefix="PAGE-B")

    async with realdb.client(key="b", role="ap_clerk") as c:
        first = (await c.get("/api/invoices?page=1")).json()
        second = (await c.get("/api/invoices?page=2")).json()

    assert second["page"] == 2
    # No overlap between the two pages (ids are disjoint).
    ids1 = {it["id"] for it in first["items"]}
    ids2 = {it["id"] for it in second["items"]}
    assert ids1.isdisjoint(ids2)
    assert len(ids1) == DEFAULT_PAGE_SIZE


async def test_tied_created_at_paginates_by_unique_tiebreak(realdb):
    """Regression: rows sharing an identical ``created_at`` (bulk/seed inserts)
    must paginate in a deterministic *total* order. With ``created_at`` as the
    sole ORDER BY key Postgres returns ties in arbitrary heap order, so OFFSET
    pages could overlap — page 2 re-returning a page-1 row, which crashed the
    frontend's keyed ``{#each}`` on "Load more".

    The endpoint tie-breaks on the unique PK (``id DESC``). Because the PKs are
    random UUIDs, heap order is unrelated to id order — so asserting the swept
    sequence is exactly id-descending fails on the un-tiebroken query and only
    passes with the deterministic ordering in place. As a corollary the pages
    are disjoint and the sweep is complete (no row repeated or skipped)."""
    org_id = realdb.info("a").org_id
    tied = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    n = DEFAULT_PAGE_SIZE * 2 + 7
    await _add_invoices(realdb.sessionmaker("a"), org_id, n, prefix="TIED", created_at=tied)

    swept: list[str] = []
    async with realdb.client(key="a", role="ap_clerk") as c:
        total = (await c.get("/api/invoices?page=1")).json()["total"]
        pages = (total + DEFAULT_PAGE_SIZE - 1) // DEFAULT_PAGE_SIZE
        for p in range(1, pages + 1):
            items = (await c.get(f"/api/invoices?page={p}")).json()["items"]
            swept.extend(it["id"] for it in items)

    # All ties → the entire sweep must be ordered by the unique PK tie-break.
    assert swept == sorted(swept, reverse=True)
    # Corollary: every row appears exactly once (no overlap, no gap).
    assert len(swept) == len(set(swept)) == total


async def test_page_size_is_capped(realdb):
    """page_size above MAX_PAGE_SIZE is rejected (422) — the bound lives in
    one place and applies everywhere."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        too_big = await c.get(f"/api/invoices?page_size={MAX_PAGE_SIZE + 1}")
        at_max = await c.get(f"/api/invoices?page_size={MAX_PAGE_SIZE}")
    assert too_big.status_code == 422
    assert at_max.status_code == 200
    assert at_max.json()["page_size"] == MAX_PAGE_SIZE


async def test_previously_divergent_endpoints_carry_page_meta(realdb):
    """purchase-orders / goods-receipts / credit-memos / exceptions / cards
    used to omit page/page_size (or not paginate at all) — all now echo the
    canonical meta."""
    async with realdb.client(key="a", role="ap_manager") as c:
        for path in (
            "/api/purchase-orders",
            "/api/goods-receipts",
            "/api/credit-memos",
            "/api/exceptions",
            "/api/cards",
            "/api/payments/runs/",
        ):
            resp = await c.get(path)
            assert resp.status_code == 200, path
            body = resp.json()
            assert body["page"] == 1, path
            assert body["page_size"] == DEFAULT_PAGE_SIZE, path
            assert "total" in body and "items" in body, path


async def test_workflows_list_is_paginated_envelope(realdb):
    """The workflows list used to return a bare array; it now returns the
    canonical envelope (and still auto-creates the default on first read)."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        body = (await c.get("/api/workflows")).json()
    assert body["page"] == 1
    assert body["page_size"] == DEFAULT_PAGE_SIZE
    assert body["total"] >= 1
    assert any(w["is_default"] for w in body["items"])


async def test_gl_accounts_stays_unpaginated(realdb):
    """The chart of accounts is a bounded reference list, returned in full as a
    bare array with no page/page_size and no truncation at DEFAULT_PAGE_SIZE.

    Both of its consumers need every row, so this is not an oversight waiting
    to be normalised onto the envelope. The invoice / expense / requisition /
    catalog GL pickers could otherwise not offer a code living past page 1,
    which is a coding defect rather than a paging one; and the `/gl-accounts`
    list page states the count it actually holds instead of a server total it
    only partly fetched. Changing the shape breaks five call sites at once.
    """
    org_id = realdb.info("b").org_id
    await _add_gl_accounts(realdb.sessionmaker("b"), org_id, DEFAULT_PAGE_SIZE + 7, prefix="PG")

    async with realdb.client(key="b", role="ap_clerk") as c:
        resp = await c.get("/api/gl-accounts")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len([a for a in body if a["code"].startswith("PG")]) == DEFAULT_PAGE_SIZE + 7
