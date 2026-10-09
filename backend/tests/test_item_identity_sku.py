"""Item identity (SKU) on PO and goods-receipt lines — migration 0111, decisions §268.

A received quantity has to name WHAT arrived for the commerce sync to push it.
Pinned here:

  * every PO construction site stamps the SKU snapshot it has — a requisition
    line's catalog item + item code, a contract line's item code, an ERP line's
    item code;
  * a receipt line copies the PO line's SKU and catalog item, never the
    client's;
  * the PO and receipt reads return both.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models.entity import Entity
from app.models.procurement import (
    Catalog,
    CatalogItem,
    GRLineItem,
    POLineItem,
    PurchaseOrder,
    PurchaseRequisition,
    RequisitionLineItem,
)
from app.services.requisition_service import convert_requisition_to_po
from app.utils.dates import utc_today

TENANT = "a"


def test_requisition_conversion_carries_catalog_item_and_sku():
    catalog_item_id = uuid.uuid4()
    req = PurchaseRequisition(
        requisition_number="REQ-SKU",
        title="Rackets",
        total=Decimal("1000"),
        currency="USD",
        organization_id=uuid.uuid4(),
    )
    req.line_items = [
        RequisitionLineItem(
            description="Wilson Pro Staff",
            quantity=Decimal("10"),
            unit_price=Decimal("100"),
            total=Decimal("1000"),
            catalog_item_id=catalog_item_id,
            item_code=" WPS-97 ",
        ),
        RequisitionLineItem(description="Free text", quantity=Decimal("1"), item_code="  "),
    ]
    po = convert_requisition_to_po(req, org_id=req.organization_id, po_number="PO-SKU")
    first, second = po.line_items
    assert first.catalog_item_id == catalog_item_id
    assert first.sku == "WPS-97"
    assert second.catalog_item_id is None
    assert second.sku is None


async def _po_with_skus(realdb) -> tuple[uuid.UUID, list[uuid.UUID], uuid.UUID]:
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    async with mk() as s:
        entity = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        catalog = Catalog(name=f"Rackets {uuid.uuid4().hex[:6]}", organization_id=org_id)
        s.add(catalog)
        await s.flush()
        item = CatalogItem(
            catalog_id=catalog.id, sku="WPS-97", name="Wilson Pro Staff", organization_id=org_id
        )
        s.add(item)
        await s.flush()
        po = PurchaseOrder(
            po_number=f"PO-SKU-{uuid.uuid4().hex[:6]}",
            total=Decimal("1200"),
            currency="USD",
            status="open",
            organization_id=org_id,
            entity_id=entity,
        )
        s.add(po)
        await s.flush()
        lines = [
            POLineItem(
                po_id=po.id,
                description="Wilson Pro Staff",
                quantity=Decimal("10"),
                catalog_item_id=item.id,
                sku="WPS-97",
            ),
            POLineItem(po_id=po.id, description="Overgrip", quantity=Decimal("20")),
        ]
        s.add_all(lines)
        await s.commit()
        return po.id, [li.id for li in lines], item.id


async def test_po_detail_returns_line_identity(realdb):
    po_id, (sku_line, plain_line), item_id = await _po_with_skus(realdb)
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.get(f"/api/purchase-orders/{po_id}")
    assert resp.status_code == 200, resp.text
    by_id = {li["id"]: li for li in resp.json()["line_items"]}
    assert by_id[str(sku_line)]["sku"] == "WPS-97"
    assert by_id[str(sku_line)]["catalog_item_id"] == str(item_id)
    assert by_id[str(plain_line)]["sku"] is None
    assert by_id[str(plain_line)]["catalog_item_id"] is None


async def test_receipt_lines_inherit_the_po_lines_identity_not_the_clients(realdb):
    po_id, (sku_line, plain_line), item_id = await _po_with_skus(realdb)
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post(
            "/api/goods-receipts",
            json={
                "po_id": str(po_id),
                "received_date": utc_today().isoformat(),
                "lines": [
                    # A client-sent sku is not part of the contract; ignored.
                    {"po_line_item_id": str(sku_line), "quantity_received": "4", "sku": "X"},
                    {"po_line_item_id": str(plain_line), "quantity_received": "2"},
                ],
            },
        )
    assert resp.status_code == 201, resp.text
    by_po_line = {li["po_line_item_id"]: li for li in resp.json()["line_items"]}
    assert by_po_line[str(sku_line)]["sku"] == "WPS-97"
    assert by_po_line[str(sku_line)]["catalog_item_id"] == str(item_id)
    assert by_po_line[str(plain_line)]["sku"] is None

    async with realdb.sessionmaker(TENANT)() as s:
        stored = (
            (
                await s.execute(
                    select(GRLineItem).where(GRLineItem.po_line_item_id.in_([sku_line, plain_line]))
                )
            )
            .scalars()
            .all()
        )
    skus = {li.po_line_item_id: (li.sku, li.catalog_item_id) for li in stored}
    assert skus[sku_line] == ("WPS-97", item_id)
    assert skus[plain_line] == (None, None)
