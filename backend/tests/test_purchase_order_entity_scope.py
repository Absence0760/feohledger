"""`GET /api/purchase-orders/{id}` must not list another subsidiary's invoices.

`linked_invoices` was built by joining on `po_number` alone — no
`apply_entity_scope`, and the route takes no entity dependency at all. But
`po_number` is NOT unique across subsidiaries: `services/po_matching` designs
around exactly that (its own PO lookup is entity-scoped, with two subsidiaries
each numbering from `PO-1001` as the documented case). So a US-scoped viewer
opening a PO saw the UK subsidiary's invoices — number, vendor name and amount
— on the detail panel. Entity isolation is a data-layer invariant, so the join
now carries the PO's OWN entity (∪ unstamped rows).

The same unscoped `po_number` match existed one route away, on the sync-erp
upsert: a sync run under subsidiary B found subsidiary A's PO by number and
overwrote its `total` / `status`. That is the cross-entity WRITE of the same
bug, and it silently re-prices the amount control 3-way match runs A's invoices
against.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.procurement import PurchaseOrder

# Multi-entity is a plan-gated feature (docs/decisions.md §258) and every test
# here stands up a second entity, so the harness orgs run on Scale.
pytestmark = pytest.mark.plan("scale")

TENANT = "a"


async def _entities(client, *, name: str, slug: str) -> tuple[str, str]:
    """Create a second entity; return (default_entity_id, new_entity_id)."""
    r = await client.post("/api/entities", json={"name": name, "slug": slug})
    assert r.status_code == 201, r.text
    other_id = r.json()["id"]
    listing = await client.get("/api/entities")
    default_id = next(e["id"] for e in listing.json() if e["is_default"])
    return default_id, other_id


async def _add_po(mk, org_id, *, po_number: str, entity_id, total="1000.00") -> str:
    async with mk() as s:
        po = PurchaseOrder(
            po_number=po_number,
            total=Decimal(total),
            status="open",
            organization_id=org_id,
            entity_id=entity_id,
        )
        s.add(po)
        await s.commit()
        return str(po.id)


async def _add_invoice(mk, org_id, *, po_number: str, entity_id, number: str) -> str:
    async with mk() as s:
        inv = Invoice(
            invoice_number=number,
            vendor_name="Shared Supplier Ltd",
            amount=Decimal("1000.00"),
            po_number=po_number,
            status=InvoiceStatus.approved,
            organization_id=org_id,
            entity_id=entity_id,
        )
        s.add(inv)
        await s.commit()
        return str(inv.id)


async def _set_org_erp(realdb, org_id: uuid.UUID, erp_config: dict | None) -> None:
    """Patch the control-plane `Organization.settings.erp` for one org.

    The org row lives in this process's per-slot control-plane database, so it
    has to go through `realdb.control_sessionmaker()` — the tenant
    sessionmaker cannot reach it. Mirrors `tests/test_purchase_orders.py`.
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


async def test_linked_invoices_are_scoped_to_the_pos_own_entity(realdb):
    """A sibling subsidiary's same-numbered invoice never reaches the panel.

    Three invoices share one `po_number`: one under the PO's own entity, one
    under a sibling subsidiary, and one unstamped (NULL `entity_id`). Only the
    first two are the PO's business.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-XENT-{uuid.uuid4().hex[:6]}"

    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="PO Scope UK", slug="po-scope-uk")

    po_id = await _add_po(mk, org_id, po_number=number, entity_id=uuid.UUID(default_id))
    own = await _add_invoice(
        mk, org_id, po_number=number, entity_id=uuid.UUID(default_id), number=f"INV-OWN-{number}"
    )
    foreign = await _add_invoice(
        mk, org_id, po_number=number, entity_id=uuid.UUID(other_id), number=f"INV-UK-{number}"
    )
    # Unstamped: pre-multi-entity, or created before the row carried an entity.
    # It must stay visible — under-showing here is silent, and this is the only
    # page that links the invoice to its PO.
    unstamped = await _add_invoice(
        mk, org_id, po_number=number, entity_id=None, number=f"INV-NULL-{number}"
    )

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.get(f"/api/purchase-orders/{po_id}")
    assert resp.status_code == 200, resp.text
    linked = {row["id"] for row in resp.json()["linked_invoices"]}
    assert own in linked
    assert unstamped in linked
    assert foreign not in linked, "a sibling subsidiary's invoice leaked onto the PO detail"


async def test_linked_invoices_scope_is_the_pos_entity_in_the_consolidated_view(realdb):
    """The scope is the PO's entity, not the reader's `X-Entity-ID`.

    The consolidated view (`X-Entity-ID` absent) legitimately reaches every PO,
    so it is the case that proves the panel is keyed on the PO rather than on
    the caller: it must still show each PO only its own subsidiary's invoices,
    never widen to everything the reader can see.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-XSEL-{uuid.uuid4().hex[:6]}"

    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="PO Scope DE", slug="po-scope-de")

    po_id = await _add_po(mk, org_id, po_number=number, entity_id=uuid.UUID(default_id))
    own = await _add_invoice(
        mk, org_id, po_number=number, entity_id=uuid.UUID(default_id), number=f"INV-OWN-{number}"
    )
    foreign = await _add_invoice(
        mk, org_id, po_number=number, entity_id=uuid.UUID(other_id), number=f"INV-DE-{number}"
    )

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        # Consolidated, and the PO's own entity — both reach the PO, and both
        # must report only the default entity's invoice.
        for headers in ({}, {"X-Entity-ID": default_id}):
            resp = await c.get(f"/api/purchase-orders/{po_id}", headers=headers)
            assert resp.status_code == 200, resp.text
            linked = {row["id"] for row in resp.json()["linked_invoices"]}
            assert linked == {own}, (headers, linked)
            assert foreign not in linked


async def test_po_detail_is_entity_scoped(realdb):
    """The by-id route resolved on the primary key alone.

    The list and the counts beside it were entity-scoped from the day
    multi-entity Phase 2 landed, so the selector was advisory on exactly the
    route that hands over a subsidiary's order and its line items. Closed with
    `_get_scoped_po`, the shape `api/payments.py::_get_scoped_payment` uses —
    including its opaque 404, so an out-of-scope id is indistinguishable from a
    missing one and can't be used to enumerate another subsidiary's POs.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-DETAIL-{uuid.uuid4().hex[:6]}"

    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="PO Detail IE", slug="po-detail-ie")

    po_id = await _add_po(mk, org_id, po_number=number, entity_id=uuid.UUID(default_id))

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        own = await c.get(f"/api/purchase-orders/{po_id}", headers={"X-Entity-ID": default_id})
        assert own.status_code == 200, own.text

        # The consolidated view still sees every entity's rows — that is what
        # it means, and the list behaves the same way.
        assert (await c.get(f"/api/purchase-orders/{po_id}")).status_code == 200

        blocked = await c.get(f"/api/purchase-orders/{po_id}", headers={"X-Entity-ID": other_id})
        assert blocked.status_code == 404, blocked.text
        # Byte-identical to the missing-row body: no enumeration oracle.
        missing = await c.get(
            f"/api/purchase-orders/{uuid.uuid4()}", headers={"X-Entity-ID": other_id}
        )
        assert missing.status_code == 404
        assert blocked.json() == missing.json()


async def test_unstamped_po_still_sees_every_invoice(realdb):
    """A NULL `entity_id` on the PO is a passthrough — single-entity tenants
    and pre-multi-entity rows are unchanged by the scoping."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-NOENT-{uuid.uuid4().hex[:6]}"

    po_id = await _add_po(mk, org_id, po_number=number, entity_id=None)
    inv = await _add_invoice(
        mk, org_id, po_number=number, entity_id=None, number=f"INV-NOENT-{number}"
    )

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.get(f"/api/purchase-orders/{po_id}")
    assert resp.status_code == 200, resp.text
    assert {row["id"] for row in resp.json()["linked_invoices"]} == {inv}


async def test_sync_erp_does_not_overwrite_another_entitys_po(realdb):
    """The sync-erp upsert matches `po_number` within the entity it syncs into.

    Pre-fix the `(po_number, org)` lookup found subsidiary B's `PO-2024-200`
    from a sync run under the default entity and rewrote its `total` to the
    ERP's 2500.00 — a cross-entity write that silently re-prices the amount
    control B's invoices are matched against.
    """
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await _entities(c, name="PO Sync FR", slug="po-sync-fr")

    # A PO under the SIBLING subsidiary carrying a number the mock ERP also
    # publishes, at a deliberately different total.
    foreign_po = await _add_po(
        mk, org_id, po_number="PO-2024-200", entity_id=uuid.UUID(other_id), total="1.00"
    )
    await _set_org_erp(realdb, org_id, {"type": "mock", "integration_method": "direct"})

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/purchase-orders/sync-erp", headers={"X-Entity-ID": default_id})
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        foreign = (
            await s.execute(select(PurchaseOrder).where(PurchaseOrder.id == uuid.UUID(foreign_po)))
        ).scalar_one()
        assert foreign.total == Decimal("1.00"), "sync overwrote a sibling subsidiary's PO"
        assert foreign.entity_id == uuid.UUID(other_id)

        # The ERP's own row landed under the entity being synced into.
        mine = (
            (
                await s.execute(
                    select(PurchaseOrder).where(
                        PurchaseOrder.po_number == "PO-2024-200",
                        PurchaseOrder.entity_id == uuid.UUID(default_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(mine) == 1
        assert mine[0].total == Decimal("2500.00")
