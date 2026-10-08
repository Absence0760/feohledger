"""Goods Receipt endpoints — list, detail, record a delivery, cancel one.

Recording lives in ``services/goods_receipts`` (PO lock, line validation,
recorder stamp, audit, rematch); this router is the HTTP surface. Reads are open
to any authenticated user; recording and cancelling are
``RECEIPT_ENTRY_ROLES``. See ``backend/docs/po-matching.md`` § Recording a
goods receipt.
"""

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    get_current_user,
    get_org_id,
    require_roles,
)
from app.api.pagination import PaginationParams, paginated, pagination_params
from app.models.organization import Organization
from app.models.procurement import GoodsReceipt, PurchaseOrder
from app.models.user import User
from app.schemas.goods_receipt import GoodsReceiptCreate
from app.services.goods_receipts import (
    ReceiptLineInput,
    cancel_goods_receipt,
    create_goods_receipt,
)
from app.tenant import apply_entity_scope, get_entity_id, get_tenant, get_tenant_db
from app.utils.dates import utc_today

#: Who may record or cancel a delivery. Receiving is entry work, so the AP
#: clerk is included alongside the managers (the invoice-entry split,
#: `api/invoice_entry.INVOICE_ENTRY_ROLES`). What keeps a clerk — or anyone —
#: from releasing their own invoice with a receipt is not this list but the
#: recorder stamp the payment hold's auto-close checks (decisions §261).
RECEIPT_ENTRY_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)

#: How far ahead of the server's UTC date a received date may be. One day,
#: because a user east of UTC is already on tomorrow's date for part of theirs;
#: anything further is a typo, and a future receipt would satisfy the 3-way leg
#: for goods that have not arrived.
_RECEIVED_DATE_LEEWAY = timedelta(days=1)

router = APIRouter(prefix="/goods-receipts", tags=["goods-receipts"])


def _line_dict(li) -> dict:
    return {
        "id": str(li.id),
        "po_line_item_id": str(li.po_line_item_id) if li.po_line_item_id else None,
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
                "source": gr.source,
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

    return await _detail(db, gr)


async def _detail(db: AsyncSession, gr: GoodsReceipt) -> dict:
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
        # `manual` = recorded in FeohLedger (and so cancellable here); null =
        # arrived some other way. Who recorded it stays server-side: it is a
        # control input to the payment hold, not something the page needs.
        "source": gr.source,
        "line_items": [_line_dict(li) for li in gr.line_items],
        "created_at": gr.created_at.isoformat() if gr.created_at else "",
    }


@router.post("", status_code=201)
async def record_goods_receipt(
    body: GoodsReceiptCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*RECEIPT_ENTRY_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Record a delivery against a purchase order.

    201 with the receipt; **200 with the same receipt** when
    ``Idempotency-Key`` replays an earlier submit, so a retried click never
    books the delivery twice. The PO is resolved within the caller's entity
    (an out-of-scope PO is the same 404 as a missing one) and the receipt is
    booked to the PO's own entity, where the matcher will look for it.
    """
    if body.received_date > utc_today() + _RECEIVED_DATE_LEEWAY:
        raise HTTPException(status_code=422, detail="The received date cannot be in the future.")

    receipt, created = await create_goods_receipt(
        db,
        org_id=org_id,
        actor_id=user.id,
        entity_id=entity_id,
        po_id=body.po_id,
        received_date=body.received_date,
        gr_number=body.gr_number,
        lines=[
            ReceiptLineInput(
                po_line_item_id=line.po_line_item_id,
                description=line.description,
                quantity_received=line.quantity_received,
            )
            for line in body.lines
        ],
        idempotency_key=(idempotency_key or "").strip() or None,
        org_settings=org.settings or {},
    )
    if not created:
        response.status_code = 200
    return await _detail(db, receipt)


@router.post("/{gr_id}/cancel")
async def cancel_receipt(
    gr_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*RECEIPT_ENTRY_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Cancel a receipt recorded in FeohLedger. The matcher stops counting it
    and the PO's invoices are re-matched — which can raise a payment hold, never
    lift one, so this needs no segregation check of its own."""
    receipt = await cancel_goods_receipt(
        db,
        org_id=org_id,
        actor_id=user.id,
        entity_id=entity_id,
        gr_id=gr_id,
        org_settings=org.settings or {},
    )
    return await _detail(db, receipt)
