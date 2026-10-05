"""Goods Receipt endpoints — list + detail (linked PO + line items)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.api.pagination import PaginationParams, paginated, pagination_params
from app.models.procurement import GoodsReceipt, PurchaseOrder
from app.models.user import User
from app.tenant import apply_entity_scope, get_entity_id, get_tenant_db

router = APIRouter(prefix="/goods-receipts", tags=["goods-receipts"])


def _line_dict(li) -> dict:
    return {
        "id": str(li.id),
        "description": li.description,
        # `is not None`, not truthiness: a line that received NOTHING (`0`) is a
        # recorded short-shipment, and `null` would read as "not recorded".
        "quantity_received": (
            float(li.quantity_received) if li.quantity_received is not None else None
        ),
    }


@router.get("")
async def list_goods_receipts(
    # A `uuid.UUID`, so FastAPI rejects a malformed value at the boundary with a
    # 422 — parsed by hand in the handler it was an unhandled `ValueError` (a
    # 500). Same fix `GET /api/purchase-orders`' `vendor_id` filter carries.
    po_id: uuid.UUID | None = None,
    status_filter: str | None = Query(None, alias="status"),
    pagination: PaginationParams = Depends(pagination_params),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = apply_entity_scope(select(GoodsReceipt), GoodsReceipt, entity_id)
    if po_id:
        base = base.where(GoodsReceipt.po_id == po_id)
    if status_filter:
        base = base.where(GoodsReceipt.status == status_filter)

    total_q = await db.execute(select(func.count()).select_from(base.subquery()))
    total = int(total_q.scalar() or 0)

    paged = (
        base.options(selectinload(GoodsReceipt.line_items))
        .order_by(GoodsReceipt.received_date.desc().nullslast(), GoodsReceipt.created_at.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    result = await db.execute(paged)
    grs = result.scalars().all()

    # Look up PO numbers for the rendered set so the table can show
    # "GR-123 → PO-2024-005" without a separate fetch per row.
    po_ids = {gr.po_id for gr in grs if gr.po_id}
    po_numbers: dict[str, str] = {}
    if po_ids:
        po_q = await db.execute(select(PurchaseOrder).where(PurchaseOrder.id.in_(po_ids)))
        for po in po_q.scalars().all():
            po_numbers[str(po.id)] = po.po_number

    return paginated(
        [
            {
                "id": str(gr.id),
                "gr_number": gr.gr_number,
                "po_id": str(gr.po_id) if gr.po_id else None,
                "po_number": po_numbers.get(str(gr.po_id)) if gr.po_id else None,
                "received_date": gr.received_date.isoformat() if gr.received_date else None,
                "status": gr.status,
                "line_count": len(gr.line_items),
                "created_at": gr.created_at.isoformat() if gr.created_at else "",
            }
            for gr in grs
        ],
        total,
        pagination,
    )


@router.get("/{gr_id}")
async def get_goods_receipt(
    gr_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Single GR with line items + the PO it's against.

    Resolved within the caller's selected entity, like the list beside it and
    like ``GET /api/purchase-orders/{id}`` (``_get_scoped_po``). On the primary
    key alone, a viewer scoped to one subsidiary could read a sibling's receipt
    — its received lines and the PO number it was booked against — by holding
    the id. An out-of-scope id gets the SAME 404 a missing one does, so the
    route can't enumerate another subsidiary's receipts; the consolidated view
    (``entity_id is None``) still reaches every row, which is what it means.
    """
    result = await db.execute(
        apply_entity_scope(
            select(GoodsReceipt)
            .options(selectinload(GoodsReceipt.line_items))
            .where(GoodsReceipt.id == gr_id),
            GoodsReceipt,
            entity_id,
        )
    )
    gr = result.scalar_one_or_none()
    if not gr:
        raise HTTPException(status_code=404, detail="Goods receipt not found")

    po_number: str | None = None
    if gr.po_id:
        po_q = await db.execute(select(PurchaseOrder.po_number).where(PurchaseOrder.id == gr.po_id))
        po_number = po_q.scalar_one_or_none()

    return {
        "id": str(gr.id),
        "gr_number": gr.gr_number,
        "po_id": str(gr.po_id) if gr.po_id else None,
        "po_number": po_number,
        "received_date": gr.received_date.isoformat() if gr.received_date else None,
        "status": gr.status,
        "line_items": [_line_dict(li) for li in gr.line_items],
        "created_at": gr.created_at.isoformat() if gr.created_at else "",
    }
