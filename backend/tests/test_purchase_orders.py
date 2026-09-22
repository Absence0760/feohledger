"""Real-Postgres coverage for the purchase-orders router.

Covers list / detail / sync-erp:
- list happy path + search / status / vendor filters + pagination shape
- detail with line items, vendor name, and linked invoices (matched by po_number)
- detail 404 for an unknown id
- RBAC on sync-erp (admin/ap_manager only) + the "no ERP configured" 400
- sync-erp against the mock adapter: creates POs, links vendors, is idempotent
- tenant isolation: rows under tenant A are invisible to tenant B

The list/detail endpoints are auth-gated but role-open (any authenticated
user). sync-erp is gated to ROLE_ADMIN / ROLE_AP_MANAGER via require_roles.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.procurement import POLineItem, PurchaseOrder
from app.models.vendor import Vendor

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _set_org_erp(realdb, org_id: uuid.UUID, erp_config: dict | None) -> None:
    """Patch the control-plane Organization.settings.erp for one org.

    sync-erp reads ``org.settings["erp"]`` off the control DB row, so the
    realdb tenant sessionmaker can't reach it — go through
    ``realdb.control_sessionmaker()`` (not a bare
    ``create_async_engine(cfg.database_url)``): the harness's org lives in
    this process's per-slot control-plane database, not the real, shared one.
    """
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        if erp_config is None:
            settings.pop("erp", None)
        else:
            settings["erp"] = erp_config
        org.settings = settings
        await s.commit()


async def _add_po(
    mk,
    org_id: uuid.UUID,
    *,
    po_number: str,
    total: Decimal,
    status: str = "open",
    vendor_id: uuid.UUID | None = None,
    line_items: list[dict] | None = None,
    currency: str | None = None,
) -> uuid.UUID:
    async with mk() as s:
        po = PurchaseOrder(
            po_number=po_number,
            total=total,
            status=status,
            vendor_id=vendor_id,
            currency=currency,
            organization_id=org_id,
        )
        s.add(po)
        await s.flush()
        for li in line_items or []:
            s.add(POLineItem(po_id=po.id, **li))
        await s.commit()
        return po.id


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_purchase_orders_happy_path(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_po(
        mk,
        org_id,
        po_number="PO-1",
        total=Decimal("1200.50"),
        line_items=[
            {
                "description": "Widgets",
                "quantity": Decimal("10"),
                "unit_price": Decimal("120.05"),
                "total": Decimal("1200.50"),
            }
        ],
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/purchase-orders")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["po_number"] == "PO-1"
    assert item["total"] == 1200.50
    assert item["status"] == "open"
    assert len(item["line_items"]) == 1
    assert item["line_items"][0]["description"] == "Widgets"


async def test_list_purchase_orders_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/purchase-orders")
    assert resp.status_code == 401


async def test_list_purchase_orders_search_and_status_filter(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_po(mk, org_id, po_number="PO-ALPHA", total=Decimal("100"), status="open")
    await _add_po(mk, org_id, po_number="PO-BETA", total=Decimal("200"), status="closed")

    async with realdb.client(key="a", role="ap_manager") as c:
        # search matches po_number ilike
        resp = await c.get("/api/purchase-orders", params={"search": "alpha"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["po_number"] == "PO-ALPHA"

        # status filter (alias "status")
        resp = await c.get("/api/purchase-orders", params={"status": "closed"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["po_number"] == "PO-BETA"


async def test_list_purchase_orders_vendor_filter_and_name(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        vendor = Vendor(name="Office Supplies Co", organization_id=org_id)
        s.add(vendor)
        await s.flush()
        vendor_id = vendor.id
        await s.commit()

    await _add_po(mk, org_id, po_number="PO-V", total=Decimal("500"), vendor_id=vendor_id)
    await _add_po(mk, org_id, po_number="PO-NOV", total=Decimal("600"))

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/purchase-orders", params={"vendor_id": str(vendor_id)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["po_number"] == "PO-V"
    assert item["vendor_id"] == str(vendor_id)
    assert item["vendor_name"] == "Office Supplies Co"


async def test_list_purchase_orders_pagination(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    for i in range(5):
        await _add_po(mk, org_id, po_number=f"PO-{i}", total=Decimal("10"))

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/purchase-orders", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5  # total is the unpaged count
    assert len(body["items"]) == 2


async def test_list_purchase_orders_tenant_isolation(realdb):
    mk_a = realdb.sessionmaker("a")
    await _add_po(mk_a, realdb.info("a").org_id, po_number="PO-SECRET", total=Decimal("999"))

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get("/api/purchase-orders")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


# ---------------------------------------------------------------------------
# counts (status chips)
# ---------------------------------------------------------------------------


async def test_counts_tallies_whole_set_not_the_page(realdb):
    """The reason the endpoint exists: the list's `total` is the ACTIVE
    filter's result set, so a page of results can't tally the other chips."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    for i in range(3):
        await _add_po(mk, org_id, po_number=f"PO-O{i}", total=Decimal("10"), status="open")
    await _add_po(mk, org_id, po_number="PO-C", total=Decimal("20"), status="closed")
    await _add_po(mk, org_id, po_number="PO-X", total=Decimal("30"), status="cancelled")

    async with realdb.client(key="a", role="ap_manager") as c:
        # A page that can only see two rows must not change the tallies.
        resp = await c.get("/api/purchase-orders/counts", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["by_status"] == {"open": 3, "closed": 1, "cancelled": 1}
    assert body["total"] == 5


async def test_counts_is_search_aware(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_po(mk, org_id, po_number="PO-ALPHA", total=Decimal("10"), status="open")
    await _add_po(mk, org_id, po_number="PO-ALPHA-2", total=Decimal("10"), status="closed")
    await _add_po(mk, org_id, po_number="PO-BETA", total=Decimal("10"), status="open")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/purchase-orders/counts", params={"search": "alpha"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["by_status"] == {"open": 1, "closed": 1}
    assert body["total"] == 2


async def test_counts_honours_the_vendor_filter(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        vendor = Vendor(name="Counted Co", organization_id=org_id)
        s.add(vendor)
        await s.flush()
        vendor_id = vendor.id
        await s.commit()

    await _add_po(mk, org_id, po_number="PO-V1", total=Decimal("10"), vendor_id=vendor_id)
    await _add_po(mk, org_id, po_number="PO-V2", total=Decimal("10"), vendor_id=vendor_id)
    await _add_po(mk, org_id, po_number="PO-NOV", total=Decimal("10"))

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/purchase-orders/counts", params={"vendor_id": str(vendor_id)})
    assert resp.status_code == 200
    assert resp.json() == {"total": 2, "by_status": {"open": 2}}


async def test_a_malformed_vendor_id_is_a_client_error_on_both_endpoints(realdb):
    """A garbage filter value must never be the 500 an unguarded
    `uuid.UUID(...)` raises out of a handler.

    This previously asserted a hand-rolled 400 on `/counts` alone — and the
    LIST endpoint, running the same predicate from its own copy, still raised
    that 500. Both now declare `vendor_id: uuid.UUID`, so FastAPI refuses a
    malformed value at the boundary with its own 422 before either handler
    runs. The assertion's intent is unchanged (a client error, not a crash) and
    now covers the endpoint that was actually broken; only the mechanism moved,
    from hand-rolled validation in one handler to the boundary for both.
    """
    async with realdb.client(key="a", role="ap_manager") as c:
        for path in ("/api/purchase-orders", "/api/purchase-orders/counts"):
            resp = await c.get(path, params={"vendor_id": "not-a-uuid"})
            assert resp.status_code == 422, f"{path} returned {resp.status_code}"


async def test_counts_empty_set_is_zero_not_an_error(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get("/api/purchase-orders/counts")
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "by_status": {}}


async def test_counts_readable_by_every_role_that_reads_the_list(realdb):
    mk = realdb.sessionmaker("a")
    await _add_po(mk, realdb.info("a").org_id, po_number="PO-R", total=Decimal("10"))

    for role in ("admin", "ap_manager", "ap_clerk", "cfo"):
        async with realdb.client(key="a", role=role) as c:
            resp = await c.get("/api/purchase-orders/counts")
        assert resp.status_code == 200, role
        assert resp.json()["total"] == 1, role


async def test_counts_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/purchase-orders/counts")
    assert resp.status_code == 401


async def test_counts_tenant_isolation(realdb):
    mk_a = realdb.sessionmaker("a")
    await _add_po(mk_a, realdb.info("a").org_id, po_number="PO-SECRET-C", total=Decimal("999"))

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get("/api/purchase-orders/counts")
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "by_status": {}}


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


async def test_get_purchase_order_detail_with_invoices(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        vendor = Vendor(name="Cloud Services Inc", organization_id=org_id)
        s.add(vendor)
        await s.flush()
        vendor_id = vendor.id
        await s.commit()

    po_id = await _add_po(
        mk,
        org_id,
        po_number="PO-2024-201",
        total=Decimal("15000.00"),
        vendor_id=vendor_id,
        line_items=[
            {
                "description": "SaaS license",
                "quantity": Decimal("1"),
                "unit_price": Decimal("12000.00"),
                "total": Decimal("12000.00"),
            }
        ],
    )

    # An invoice that references the PO number is surfaced under linked_invoices.
    async with mk() as s:
        s.add(
            Invoice(
                invoice_number="INV-1",
                vendor_name="Cloud Services Inc",
                amount=Decimal("12000.00"),
                po_number="PO-2024-201",
                status=InvoiceStatus.approved,
                organization_id=org_id,
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get(f"/api/purchase-orders/{po_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["po_number"] == "PO-2024-201"
    assert body["vendor_name"] == "Cloud Services Inc"
    assert body["total"] == 15000.00
    assert len(body["line_items"]) == 1
    assert len(body["linked_invoices"]) == 1
    assert body["linked_invoices"][0]["invoice_number"] == "INV-1"
    assert body["linked_invoices"][0]["status"] == "approved"


async def test_list_and_detail_serve_each_pos_own_currency(realdb):
    """Every PO figure is labelled from the PO's own code — `null` included.

    The list used to carry no currency at all, so the page stamped the org's on
    every row; a NULL is served as `null` (the client renders the figure bare),
    never replaced with a default (decisions §197). Linked invoices carry their
    OWN currency, since an invoice in another currency than its PO is exactly
    what a reviewer opens the panel to see.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    eur_id = await _add_po(
        mk, org_id, po_number="PO-CCY-EUR", total=Decimal("100.00"), currency="EUR"
    )
    bare_id = await _add_po(mk, org_id, po_number="PO-CCY-NONE", total=Decimal("200.00"))
    async with mk() as s:
        s.add(
            Invoice(
                invoice_number="INV-CCY-1",
                vendor_name="Anyone",
                amount=Decimal("100.00"),
                currency="GBP",
                po_number="PO-CCY-EUR",
                status=InvoiceStatus.approved,
                organization_id=org_id,
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_clerk") as c:
        listed = (await c.get("/api/purchase-orders?search=PO-CCY")).json()["items"]
        eur = (await c.get(f"/api/purchase-orders/{eur_id}")).json()
        bare = (await c.get(f"/api/purchase-orders/{bare_id}")).json()

    assert {row["po_number"]: row["currency"] for row in listed} == {
        "PO-CCY-EUR": "EUR",
        "PO-CCY-NONE": None,
    }
    assert eur["currency"] == "EUR"
    assert bare["currency"] is None
    assert [(i["invoice_number"], i["currency"]) for i in eur["linked_invoices"]] == [
        ("INV-CCY-1", "GBP")
    ]


async def test_get_purchase_order_404(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get(f"/api/purchase-orders/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_get_purchase_order_tenant_isolation(realdb):
    mk_a = realdb.sessionmaker("a")
    po_id = await _add_po(mk_a, realdb.info("a").org_id, po_number="PO-X", total=Decimal("5"))

    # Tenant B's DB has no such row → 404, not a leak.
    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get(f"/api/purchase-orders/{po_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# sync-erp
# ---------------------------------------------------------------------------


async def test_sync_erp_no_config_returns_400(realdb):
    await _set_org_erp(realdb, realdb.info("a").org_id, None)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/purchase-orders/sync-erp")
    assert resp.status_code == 400
    assert "No ERP configured" in resp.json()["detail"]


async def test_sync_erp_rbac_forbidden_roles(realdb):
    # Even with ERP configured, ap_clerk and cfo are not permitted.
    await _set_org_erp(
        realdb, realdb.info("a").org_id, {"type": "mock", "integration_method": "direct"}
    )
    for role in ("ap_clerk", "cfo"):
        async with realdb.client(key="a", role=role) as c:
            resp = await c.post("/api/purchase-orders/sync-erp")
        assert resp.status_code == 403, role


async def test_sync_erp_creates_pos_and_is_idempotent(realdb):
    org_id = realdb.info("a").org_id
    await _set_org_erp(realdb, org_id, {"type": "mock", "integration_method": "direct"})

    # Seed one vendor whose name matches a mock PO so vendor linking exercises.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        s.add(Vendor(name="Office Supplies Co", organization_id=org_id))
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/purchase-orders/sync-erp")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["created"] >= 1
        assert body["skipped"] == 0
        assert body["adapter"] == "mock"
        created_first = body["created"]

        # Second sync: every PO already exists → all skipped, none created.
        resp2 = await c.post("/api/purchase-orders/sync-erp")
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["created"] == 0
        assert body2["skipped"] == created_first

    # The matching-name PO got its vendor_id linked.
    async with mk() as s:
        po_count = (await s.execute(select(func.count()).select_from(PurchaseOrder))).scalar_one()
        assert po_count == created_first
        linked = (
            await s.execute(select(PurchaseOrder).where(PurchaseOrder.po_number == "PO-2024-200"))
        ).scalar_one_or_none()
        assert linked is not None
        assert linked.vendor_id is not None


async def test_sync_erp_stamps_the_currency_the_erp_states(realdb):
    """A synced PO records the code on the ERP record (the mock states USD)."""
    org_id = realdb.info("a").org_id
    await _set_org_erp(realdb, org_id, {"type": "mock", "integration_method": "direct"})
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/purchase-orders/sync-erp")
    assert resp.status_code == 200, resp.text

    async with realdb.sessionmaker("a")() as s:
        codes = (await s.execute(select(PurchaseOrder.currency))).scalars().all()
    assert codes and set(codes) == {"USD"}


async def _sync_with_payloads(realdb, monkeypatch, payloads) -> dict:
    """Run sync-erp against the mock adapter, answering with ``payloads``."""
    from app.services.erp_adapters.mock_adapter import MockAdapter

    async def _list_pos(self):
        return payloads

    monkeypatch.setattr(MockAdapter, "list_pos", _list_pos)
    await _set_org_erp(
        realdb, realdb.info("a").org_id, {"type": "mock", "integration_method": "direct"}
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/purchase-orders/sync-erp")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _currency_of(realdb, po_number: str) -> str | None:
    async with realdb.sessionmaker("a")() as s:
        return (
            await s.execute(
                select(PurchaseOrder.currency).where(PurchaseOrder.po_number == po_number)
            )
        ).scalar_one()


async def test_sync_erp_currency_is_erp_owned_but_silence_never_erases_it(realdb, monkeypatch):
    """On a re-sync the ERP's code wins, like its total; a payload with none
    leaves a recorded code alone; a new PO with none is stored NULL — never a
    default, and a malformed code is no code at all."""
    from app.services.erp_adapters.base import PoPayload

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    await _add_po(mk, org_id, po_number="ERP-NULL", total=Decimal("10.00"))
    await _add_po(mk, org_id, po_number="ERP-KEEP", total=Decimal("10.00"), currency="EUR")
    await _add_po(mk, org_id, po_number="ERP-FIX", total=Decimal("10.00"), currency="EUR")

    body = await _sync_with_payloads(
        realdb,
        monkeypatch,
        [
            PoPayload(po_number="ERP-NULL", total=Decimal("10.00"), currency="gbp"),
            PoPayload(po_number="ERP-KEEP", total=Decimal("10.00"), currency=None),
            PoPayload(po_number="ERP-FIX", total=Decimal("10.00"), currency="USD"),
            PoPayload(po_number="ERP-NEW-NONE", total=Decimal("5.00"), currency=None),
            PoPayload(po_number="ERP-NEW-BAD", total=Decimal("5.00"), currency="US"),
        ],
    )

    assert await _currency_of(realdb, "ERP-NULL") == "GBP"  # back-filled, normalised
    assert await _currency_of(realdb, "ERP-KEEP") == "EUR"  # silence is not a correction
    assert await _currency_of(realdb, "ERP-FIX") == "USD"  # the ERP's code wins
    assert await _currency_of(realdb, "ERP-NEW-NONE") is None
    assert await _currency_of(realdb, "ERP-NEW-BAD") is None
    # A currency change is a change: it is reported, not counted as skipped.
    assert body["updated"] == 2
    assert body["skipped"] == 1
    assert body["created"] == 2


async def test_sync_erp_isolated_per_tenant(realdb):
    """A sync against tenant A leaves tenant B's PO table empty."""
    await _set_org_erp(
        realdb, realdb.info("a").org_id, {"type": "mock", "integration_method": "direct"}
    )
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/purchase-orders/sync-erp")
    assert resp.status_code == 200
    assert resp.json()["created"] >= 1

    mk_b = realdb.sessionmaker("b")
    async with mk_b() as s:
        count_b = (await s.execute(select(func.count()).select_from(PurchaseOrder))).scalar_one()
    assert count_b == 0


# ---------------------------------------------------------------------------
# sync-erp — expected_delivery_date auto-population (migration 0060 signal)
# ---------------------------------------------------------------------------


async def test_sync_erp_populates_expected_delivery_date_on_create(realdb):
    """The mock catalogue carries deterministic promised dates on two of its
    three POs; the sync mapper must persist them onto the new rows (and leave
    the third, which has no ERP date, NULL)."""
    from datetime import date

    org_id = realdb.info("a").org_id
    await _set_org_erp(realdb, org_id, {"type": "mock", "integration_method": "direct"})

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/purchase-orders/sync-erp")
    assert resp.status_code == 200

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        rows = (await s.execute(select(PurchaseOrder))).scalars().all()
        by_number = {p.po_number: p for p in rows}
    assert by_number["PO-2024-200"].expected_delivery_date == date(2024, 6, 15)
    assert by_number["PO-2024-201"].expected_delivery_date == date(2024, 7, 1)
    # The ERP supplied no date for this one → stays NULL, never fabricated.
    assert by_number["PO-2024-202"].expected_delivery_date is None


async def test_sync_erp_does_not_clobber_human_set_expected_delivery_date(realdb):
    """A date already on the row (set via the model/API) wins — a re-sync must
    never overwrite it, even though the ERP supplies its own date for that PO."""
    from datetime import date

    org_id = realdb.info("a").org_id
    await _set_org_erp(realdb, org_id, {"type": "mock", "integration_method": "direct"})

    # Pre-seed PO-2024-200 with a human-chosen expected date that differs from
    # the mock ERP's (2024-06-15).
    human_date = date(2030, 1, 1)
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        po = PurchaseOrder(
            po_number="PO-2024-200",
            total=Decimal("2500.00"),
            status="open",
            expected_delivery_date=human_date,
            organization_id=org_id,
        )
        s.add(po)
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/purchase-orders/sync-erp")
    assert resp.status_code == 200

    async with mk() as s:
        kept = (
            await s.execute(select(PurchaseOrder).where(PurchaseOrder.po_number == "PO-2024-200"))
        ).scalar_one()
    # Human value preserved — the sync neither overwrote it nor created a dup.
    assert kept.expected_delivery_date == human_date


async def test_sync_erp_backfills_missing_expected_delivery_date_on_existing_po(realdb):
    """An existing PO with no expected date gets the ERP's promised date filled
    in on a re-sync (the auto-population back-fill), reported as ``updated``."""
    from datetime import date

    org_id = realdb.info("a").org_id
    await _set_org_erp(realdb, org_id, {"type": "mock", "integration_method": "direct"})

    # Pre-seed PO-2024-201 WITHOUT an expected date (e.g. created before the
    # ERP started supplying one); the mock ERP carries 2024-07-01 for it.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        po = PurchaseOrder(
            po_number="PO-2024-201",
            total=Decimal("15000.00"),
            status="open",
            organization_id=org_id,
        )
        s.add(po)
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/purchase-orders/sync-erp")
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] >= 1

    async with mk() as s:
        filled = (
            await s.execute(select(PurchaseOrder).where(PurchaseOrder.po_number == "PO-2024-201"))
        ).scalar_one()
    assert filled.expected_delivery_date == date(2024, 7, 1)


# ---------------------------------------------------------------------------
# sync-erp — total/status refresh on an already-known PO (#148)
# ---------------------------------------------------------------------------


async def test_sync_erp_refreshes_stale_total_and_status_on_existing_po(realdb):
    """A PO amended (amount changed) or cancelled in the real ERP after we
    first synced it must have its ``total``/``status`` refreshed on the next
    sync — otherwise 3-way match keeps running invoice variance checks
    against a stale amount forever. The mock ERP's PO-2024-200 is
    total=2500.00/status=open; pre-seed a stale copy and confirm the re-sync
    corrects both fields and reports it as ``updated`` (not ``skipped``)."""
    from datetime import date

    org_id = realdb.info("a").org_id
    await _set_org_erp(realdb, org_id, {"type": "mock", "integration_method": "direct"})

    # Pre-seed PO-2024-200 as if it was amended down and later closed in our
    # DB before the ERP's amendment/cancellation caught up — a stale total and
    # a stale status, distinct from the mock ERP's current 2500.00/open.
    # Also carries its own expected_delivery_date so this test isolates the
    # total/status refresh from the separate delivery-date backfill path.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        po = PurchaseOrder(
            po_number="PO-2024-200",
            total=Decimal("999.00"),
            status="closed",
            expected_delivery_date=date(2024, 6, 15),
            organization_id=org_id,
        )
        s.add(po)
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/purchase-orders/sync-erp")
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] >= 1
    assert body["created"] == 2  # the other two mock POs are still new

    async with mk() as s:
        refreshed = (
            await s.execute(select(PurchaseOrder).where(PurchaseOrder.po_number == "PO-2024-200"))
        ).scalar_one()
    assert refreshed.total == Decimal("2500.00")
    assert refreshed.status == "open"
    # The already-present delivery date is untouched by the total/status
    # refresh — same precedence rule as the dedicated backfill test above.
    assert refreshed.expected_delivery_date == date(2024, 6, 15)


async def test_sync_erp_refreshes_total_status_and_backfills_delivery_date_together(realdb):
    """The three existing-PO refresh paths (total, status, delivery-date
    backfill) are independent branches in the same loop iteration — prove
    they all fire together in one sync call, not just in isolation."""
    from datetime import date

    org_id = realdb.info("a").org_id
    await _set_org_erp(realdb, org_id, {"type": "mock", "integration_method": "direct"})

    # PO-2024-201 in the mock ERP is total=15000.00/status=open with a
    # promised delivery date of 2024-07-01. Pre-seed a stale total, a stale
    # status, AND no delivery date at all.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        po = PurchaseOrder(
            po_number="PO-2024-201",
            total=Decimal("500.00"),
            status="cancelled",
            organization_id=org_id,
        )
        s.add(po)
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/purchase-orders/sync-erp")
    assert resp.status_code == 200
    assert resp.json()["updated"] >= 1

    async with mk() as s:
        refreshed = (
            await s.execute(select(PurchaseOrder).where(PurchaseOrder.po_number == "PO-2024-201"))
        ).scalar_one()
    assert refreshed.total == Decimal("15000.00")
    assert refreshed.status == "open"
    assert refreshed.expected_delivery_date == date(2024, 7, 1)
