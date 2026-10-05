"""Procurement by-id routes honour `X-Entity-ID` — requisitions, budgets,
intake requests and catalogs (items + punch-out sessions too).

Each router's list and summary were entity-scoped from multi-entity Phase 2,
but its by-id helper (`_get_or_404`, `_get_budget_or_404`,
`_get_intake_or_404`, `_get_catalog_or_404`, `_get_item_or_404`,
`_get_punchout_session_or_404`) resolved on the primary key alone. So a caller
scoped to subsidiary B could read A's rows AND move them — approve or convert a
requisition into a PO, re-denominate or delete a budget, convert an intake,
edit a catalog's prices — by holding the id. Now an out-of-scope id is the same
opaque 404 a missing one gets (the `GET /api/purchase-orders/{id}` shape), and
the row is untouched; the in-scope and consolidated reads still work.

The catalog link resolvers had the matching write-side gap: an item's
`vendor_id` / `gl_account_id` were validated against the org only, so A's
catalog could point at B's supplier record or B's GL account.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models.gl_account import GLAccount
from app.models.procurement import (
    Budget,
    BudgetDimension,
    Catalog,
    CatalogItem,
    CatalogType,
    IntakeRequest,
    IntakeStatus,
    PunchoutSession,
    PurchaseRequisition,
    RequisitionStatus,
)
from app.models.vendor import Vendor
from tests.entity_scope_probe import assert_out_of_scope_404, two_entities

TENANT = "a"


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def _add(mk, row):
    async with mk() as s:
        s.add(row)
        await s.commit()
        return row.id


async def _reload(mk, model, row_id):
    async with mk() as s:
        return (await s.execute(select(model).where(model.id == row_id))).scalar_one_or_none()


async def test_requisition_by_id_routes_are_entity_scoped(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await two_entities(c, slug=f"req-{_tag()}")
        req_id = await _add(
            mk,
            PurchaseRequisition(
                requisition_number=f"REQ-{_tag()}",
                title="Laptops",
                requester_user_id=uuid.uuid4(),
                status=RequisitionStatus.draft,
                total=Decimal("100.00"),
                currency="USD",
                organization_id=org_id,
                entity_id=uuid.UUID(default_id),
            ),
        )
        rid = str(req_id)

        for method, path, body in [
            ("GET", "/api/requisitions/{id}", None),
            ("PATCH", "/api/requisitions/{id}", {"title": "hijacked"}),
            ("POST", "/api/requisitions/{id}/submit", None),
            ("POST", "/api/requisitions/{id}/approve", None),
            ("POST", "/api/requisitions/{id}/reject", {"reason": "x"}),
            ("POST", "/api/requisitions/{id}/cancel", {"reason": "x"}),
            ("POST", "/api/requisitions/{id}/reopen", None),
            ("POST", "/api/requisitions/{id}/convert-to-po", None),
            ("DELETE", "/api/requisitions/{id}", None),
        ]:
            await assert_out_of_scope_404(c, method, path, rid, entity_id=other_id, json=body)

        row = await _reload(mk, PurchaseRequisition, req_id)
        assert row is not None, "an out-of-scope DELETE removed the row"
        assert row.title == "Laptops"
        assert row.status == RequisitionStatus.draft
        assert row.converted_po_id is None

        # Positive controls: the owning entity and the consolidated view.
        assert (
            await c.get(f"/api/requisitions/{rid}", headers={"X-Entity-ID": default_id})
        ).status_code == 200
        assert (await c.get(f"/api/requisitions/{rid}")).status_code == 200
        ok = await c.patch(
            f"/api/requisitions/{rid}",
            json={"title": "Laptops v2"},
            headers={"X-Entity-ID": default_id},
        )
        assert ok.status_code == 200, ok.text


async def test_budget_by_id_routes_are_entity_scoped(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await two_entities(c, slug=f"bud-{_tag()}")
        budget_id = await _add(
            mk,
            Budget(
                name="Engineering FY",
                dimension=BudgetDimension.department,
                dimension_value="Engineering",
                amount=Decimal("5000.00"),
                currency="USD",
                organization_id=org_id,
                entity_id=uuid.UUID(default_id),
            ),
        )
        bid = str(budget_id)

        for method, path, body, params in [
            ("GET", "/api/budgets/{id}", None, None),
            ("GET", "/api/budgets/{id}/spend", None, None),
            ("GET", "/api/budgets/check", None, {"budget_id": "{id}", "amount": "10"}),
            ("PATCH", "/api/budgets/{id}", {"amount": "1.00"}, None),
            ("DELETE", "/api/budgets/{id}", None, None),
        ]:
            await assert_out_of_scope_404(
                c, method, path, bid, entity_id=other_id, json=body, params=params
            )

        row = await _reload(mk, Budget, budget_id)
        assert row is not None, "an out-of-scope DELETE removed the budget"
        assert row.amount == Decimal("5000.00")

        assert (
            await c.get(f"/api/budgets/{bid}/spend", headers={"X-Entity-ID": default_id})
        ).status_code == 200
        assert (await c.get(f"/api/budgets/{bid}")).status_code == 200


async def test_intake_by_id_routes_are_entity_scoped(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await two_entities(c, slug=f"int-{_tag()}")
        intake_id = await _add(
            mk,
            IntakeRequest(
                request_number=f"INTK-{_tag()}",
                title="Figma seats",
                requester_user_id=uuid.uuid4(),
                status=IntakeStatus.open,
                currency="USD",
                organization_id=org_id,
                entity_id=uuid.UUID(default_id),
            ),
        )
        iid = str(intake_id)

        for method, path, body in [
            ("GET", "/api/intake/{id}", None),
            ("PATCH", "/api/intake/{id}", {"title": "hijacked"}),
            ("POST", "/api/intake/{id}/submit", None),
            ("POST", "/api/intake/{id}/approve", None),
            ("POST", "/api/intake/{id}/reject", {"reason": "x"}),
            ("POST", "/api/intake/{id}/cancel", {"reason": "x"}),
            ("POST", "/api/intake/{id}/reopen", None),
            ("POST", "/api/intake/{id}/convert-to-requisition", None),
            ("DELETE", "/api/intake/{id}", None),
        ]:
            await assert_out_of_scope_404(c, method, path, iid, entity_id=other_id, json=body)

        row = await _reload(mk, IntakeRequest, intake_id)
        assert row is not None
        assert row.title == "Figma seats"
        assert row.status == IntakeStatus.open
        assert row.converted_requisition_id is None

        assert (
            await c.get(f"/api/intake/{iid}", headers={"X-Entity-ID": default_id})
        ).status_code == 200
        assert (await c.get(f"/api/intake/{iid}")).status_code == 200


async def test_catalog_item_and_punchout_by_id_routes_are_entity_scoped(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await two_entities(c, slug=f"cat-{_tag()}")
        dflt = uuid.UUID(default_id)
        catalog_id = await _add(
            mk,
            Catalog(
                name="Office supplies",
                catalog_type=CatalogType.punchout,
                punchout_url="https://supplier.example/punchout",
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        item_id = await _add(
            mk,
            CatalogItem(
                catalog_id=catalog_id,
                name="Stapler",
                unit_price=Decimal("9.99"),
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        session_id = await _add(
            mk,
            PunchoutSession(
                catalog_id=catalog_id,
                buyer_cookie=f"cookie-{_tag()}",
                requested_by_user_id=uuid.uuid4(),
                organization_id=org_id,
                entity_id=dflt,
            ),
        )
        cid, itid, sid = str(catalog_id), str(item_id), str(session_id)

        for method, path, row_id, body in [
            ("GET", "/api/catalogs/{id}", cid, None),
            ("PATCH", "/api/catalogs/{id}", cid, {"name": "hijacked"}),
            ("GET", "/api/catalogs/{id}/items", cid, None),
            ("POST", "/api/catalogs/{id}/items", cid, {"name": "Planted"}),
            ("POST", "/api/catalogs/{id}/punchout/start", cid, None),
            ("PATCH", "/api/catalogs/items/{id}", itid, {"unit_price": "0.01"}),
            ("DELETE", "/api/catalogs/items/{id}", itid, None),
            ("GET", "/api/catalogs/punchout/sessions/{id}", sid, None),
            ("POST", "/api/catalogs/punchout/sessions/{id}/convert", sid, None),
            ("DELETE", "/api/catalogs/{id}", cid, None),
        ]:
            await assert_out_of_scope_404(c, method, path, row_id, entity_id=other_id, json=body)

        catalog = await _reload(mk, Catalog, catalog_id)
        assert catalog is not None and catalog.name == "Office supplies"
        item = await _reload(mk, CatalogItem, item_id)
        assert item is not None and item.unit_price == Decimal("9.99")
        async with mk() as s:
            names = (
                (
                    await s.execute(
                        select(CatalogItem.name).where(CatalogItem.catalog_id == catalog_id)
                    )
                )
                .scalars()
                .all()
            )
        assert names == ["Stapler"], "an out-of-scope POST planted an item"

        own = {"X-Entity-ID": default_id}
        assert (await c.get(f"/api/catalogs/{cid}", headers=own)).status_code == 200
        assert (await c.get(f"/api/catalogs/{cid}")).status_code == 200
        assert (
            await c.get(f"/api/catalogs/punchout/sessions/{sid}", headers=own)
        ).status_code == 200
        assert (
            await c.patch(f"/api/catalogs/items/{itid}", json={"name": "Stapler XL"}, headers=own)
        ).status_code == 200


async def test_catalog_links_refuse_another_entitys_vendor_and_gl(realdb):
    """An item's vendor / GL resolve against its catalog's entity ∪ shared."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with realdb.client(key=TENANT, role="admin") as c:
        default_id, other_id = await two_entities(c, slug=f"lnk-{_tag()}")
        dflt, other = uuid.UUID(default_id), uuid.UUID(other_id)
        catalog_id = await _add(
            mk, Catalog(name="Internal", organization_id=org_id, entity_id=dflt)
        )
        foreign_vendor = await _add(
            mk, Vendor(name=f"UK Supplier {_tag()}", organization_id=org_id, entity_id=other)
        )
        own_vendor = await _add(
            mk, Vendor(name=f"US Supplier {_tag()}", organization_id=org_id, entity_id=dflt)
        )
        foreign_gl = await _add(
            mk,
            GLAccount(
                code=f"9{_tag()[:4]}",
                name="UK only",
                organization_id=org_id,
                entity_id=other,
            ),
        )
        shared_gl = await _add(
            mk,
            GLAccount(code=f"8{_tag()[:4]}", name="Shared", organization_id=org_id, entity_id=None),
        )

        path = f"/api/catalogs/{catalog_id}/items"
        r = await c.post(path, json={"name": "x", "vendor_id": str(foreign_vendor)})
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == "Vendor not found"
        r = await c.post(path, json={"name": "x", "gl_account_id": str(foreign_gl)})
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == "GL account not found"

        r = await c.post(
            path,
            json={"name": "ok", "vendor_id": str(own_vendor), "gl_account_id": str(shared_gl)},
        )
        assert r.status_code == 201, r.text
        item_id = r.json()["id"]

        r = await c.patch(f"/api/catalogs/items/{item_id}", json={"vendor_id": str(foreign_vendor)})
        assert r.status_code == 404, r.text
        r = await c.patch(f"/api/catalogs/{catalog_id}", json={"vendor_id": str(foreign_vendor)})
        assert r.status_code == 404, r.text
