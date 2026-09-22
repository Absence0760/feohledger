"""Real-DB coverage for the `vendor_id` filter on `GET /api/invoices`, its
`/counts` chip tally and its `/ids` select-all resolver.

`GET /api/invoices` could only narrow by vendor through the free-text `vendor`
leg — an `ILIKE` over the invoice's own `vendor_name`, which matches "Acme" and
"Acme Holdings" alike and says nothing about the resolved `Invoice.vendor_id`
link a credit memo, a payment or the portal actually keys on. `vendor_id` is
the exact counterpart: an equality on the link, applied through the shared
`_invoice_list_filters` builder so the list, its chip tallies and the "select
all N matching" resolver describe one set (decisions §48, §202).
"""

import uuid
from decimal import Decimal

import pytest

from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor


async def _add_vendor(mk, org_id, name: str, *, entity_id: uuid.UUID | None = None) -> str:
    async with mk() as s:
        v = Vendor(organization_id=org_id, name=name, entity_id=entity_id)
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return str(v.id)


async def _add_invoices(
    mk,
    org_id,
    *,
    vendor_id: str | None,
    vendor_name: str,
    n: int = 1,
    status: InvoiceStatus = InvoiceStatus.new,
    prefix: str = "INV",
    entity_id: uuid.UUID | None = None,
) -> list[str]:
    objs: list[Invoice] = []
    async with mk() as s:
        for i in range(n):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"{prefix}-{vendor_name}-{status.value}-{i}",
                vendor_name=vendor_name,
                vendor_id=uuid.UUID(vendor_id) if vendor_id else None,
                amount=Decimal("100.00"),
                status=status,
                entity_id=entity_id,
            )
            s.add(inv)
            objs.append(inv)
        await s.flush()
        ids = [str(inv.id) for inv in objs]
        await s.commit()
    return ids


@pytest.mark.asyncio
async def test_list_filters_by_the_resolved_vendor_link_not_the_name(realdb):
    """The whole point of the leg: an exact id, where `vendor=` is a substring.

    "Acme Holdings" contains "Acme", so the name filter cannot tell the two
    suppliers apart; an invoice with the right NAME but no resolved link (NULL)
    is not that vendor's either — it matches no vendor id at all.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    acme = await _add_vendor(mk, org_id, "Acme")
    holdings = await _add_vendor(mk, org_id, "Acme Holdings")

    mine = await _add_invoices(mk, org_id, vendor_id=acme, vendor_name="Acme", n=2)
    await _add_invoices(mk, org_id, vendor_id=holdings, vendor_name="Acme Holdings", n=3)
    await _add_invoices(mk, org_id, vendor_id=None, vendor_name="Acme", prefix="UNLINKED")

    async with realdb.client(key="a") as c:
        by_id = await c.get("/api/invoices", params={"vendor_id": acme})
        by_name = await c.get("/api/invoices", params={"vendor": "Acme"})

    assert by_id.status_code == 200, by_id.text
    body = by_id.json()
    assert body["total"] == 2
    assert {row["id"] for row in body["items"]} == set(mine)
    assert all(row["vendor_id"] == acme for row in body["items"])
    # The contrast that makes the leg necessary: the name filter sweeps up the
    # other supplier and the unlinked row.
    assert by_name.json()["total"] == 6


@pytest.mark.asyncio
async def test_malformed_vendor_id_is_a_422_on_every_surface_that_takes_it(realdb):
    """A UUID-typed query param, like `assigned_to_id` beside it: a bad value is
    the caller's error (422), never an unhandled parse failure (500)."""
    async with realdb.client(key="a") as c:
        for path in ("/api/invoices", "/api/invoices/counts", "/api/invoices/ids"):
            resp = await c.get(path, params={"vendor_id": "not-a-uuid"})
            assert resp.status_code == 422, (path, resp.text)
            assert resp.json()["detail"][0]["loc"] == ["query", "vendor_id"], path


@pytest.mark.asyncio
async def test_vendor_id_composes_with_status_and_search(realdb):
    """One more AND clause through the shared builder, not a replacement."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    acme = await _add_vendor(mk, org_id, "Acme")
    other = await _add_vendor(mk, org_id, "Globex")

    await _add_invoices(mk, org_id, vendor_id=acme, vendor_name="Acme", n=2, prefix="KEEP")
    ready = await _add_invoices(
        mk,
        org_id,
        vendor_id=acme,
        vendor_name="Acme",
        status=InvoiceStatus.ready_for_review,
        prefix="KEEP",
    )
    await _add_invoices(
        mk,
        org_id,
        vendor_id=acme,
        vendor_name="Acme",
        status=InvoiceStatus.ready_for_review,
        prefix="DROP",
    )
    await _add_invoices(
        mk,
        org_id,
        vendor_id=other,
        vendor_name="Globex",
        status=InvoiceStatus.ready_for_review,
        prefix="KEEP",
    )

    async with realdb.client(key="a") as c:
        resp = await c.get(
            "/api/invoices",
            params={"vendor_id": acme, "status": "ready_for_review", "search": "KEEP"},
        )
    assert resp.status_code == 200, resp.text
    assert [row["id"] for row in resp.json()["items"]] == ready


@pytest.mark.asyncio
async def test_counts_and_ids_resolve_the_same_set_as_the_list(realdb):
    """The chips and "select all N matching" must describe exactly the rows the
    table shows under the filter — the §48 contract, per leg."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    acme = await _add_vendor(mk, org_id, "Acme")
    other = await _add_vendor(mk, org_id, "Globex")

    new = await _add_invoices(mk, org_id, vendor_id=acme, vendor_name="Acme", n=3)
    approved = await _add_invoices(
        mk, org_id, vendor_id=acme, vendor_name="Acme", n=2, status=InvoiceStatus.approved
    )
    await _add_invoices(mk, org_id, vendor_id=other, vendor_name="Globex", n=4)

    async with realdb.client(key="a") as c:
        listing = await c.get("/api/invoices", params={"vendor_id": acme, "page_size": 100})
        counts = await c.get("/api/invoices/counts", params={"vendor_id": acme})
        ids = await c.get("/api/invoices/ids", params={"vendor_id": acme})

    listed = {row["id"] for row in listing.json()["items"]}
    assert listed == set(new) | set(approved)
    assert counts.json() == {"counts": {"new": 3, "approved": 2}, "total": 5}
    assert set(ids.json()["ids"]) == listed
    assert ids.json()["total"] == 5


@pytest.mark.asyncio
async def test_vendor_id_does_not_widen_the_entity_scope_or_the_role_gate(realdb):
    """A new narrowing leg must not become a way around either existing one.

    `X-Entity-ID` still confines the rows (naming a vendor id does not reach
    another subsidiary's invoices), and the list keeps its role-open read — an
    ap_clerk gets the same answer as an admin.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="admin") as c:
        made = await c.post("/api/entities", json={"name": "VID Sub", "slug": "vid-sub"})
        assert made.status_code == 201, made.text
        other_entity = made.json()["id"]
        default_entity = next(
            e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"]
        )

    sub_vendor = await _add_vendor(mk, org_id, "Sub Supplier", entity_id=uuid.UUID(other_entity))
    sub_invoices = await _add_invoices(
        mk,
        org_id,
        vendor_id=sub_vendor,
        vendor_name="Sub Supplier",
        n=2,
        entity_id=uuid.UUID(other_entity),
    )

    async with realdb.client(key="a", role="admin") as c:
        c.headers["X-Entity-ID"] = default_entity
        elsewhere = await c.get("/api/invoices", params={"vendor_id": sub_vendor})
        elsewhere_counts = await c.get("/api/invoices/counts", params={"vendor_id": sub_vendor})
        c.headers["X-Entity-ID"] = other_entity
        home = await c.get("/api/invoices", params={"vendor_id": sub_vendor})
    assert elsewhere.status_code == 200 and elsewhere.json()["total"] == 0
    assert elsewhere_counts.json()["total"] == 0
    assert {row["id"] for row in home.json()["items"]} == set(sub_invoices)

    async with realdb.client(key="a", role="ap_clerk") as c:
        clerk = await c.get("/api/invoices", params={"vendor_id": sub_vendor})
    assert clerk.status_code == 200, clerk.text
    assert clerk.json()["total"] == 2
