"""Goods-receipt entry: record a delivery against a PO, or cancel one.

Why it is shaped this way (decisions §253):

* The PO row is locked for the whole write, so receipts on one PO serialise and
  the idempotency replay and the ``GR-<po_number>-<n>`` auto-number are safe.
* Every receipt stamps ``source = manual`` and its recorder: a receipt lifts a
  "billed beyond receipt" hold on its own, and the auto-close refuses one
  recorded by someone implicated in the invoice.
* Invoices citing the PO are re-matched in the same transaction, so the write
  and the holds it raises or lifts land together.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.refusals import coded_refusal
from app.models.procurement import (
    GR_SOURCE_MANUAL,
    GR_STATUS_CANCELLED,
    GoodsReceipt,
    GRLineItem,
    POLineItem,
    PurchaseOrder,
)
from app.services.audit_dispatch import dispatch_audit
from app.services.po_matching import CANCELLED_GR_STATUSES, format_quantity
from app.tenant import apply_entity_scope

#: PO statuses no delivery can be booked against. Compared case-folded; the
#: column is free-form (the ERP sync writes whatever the ERP says), so this is
#: an exclusion list in the shape of ``CANCELLED_GR_STATUSES``.
CANCELLED_PO_STATUSES = frozenset({"cancelled", "canceled", "void", "voided"})

#: How many suffixes the auto-number tries before giving up. Collisions only
#: happen when a receipt from elsewhere already uses ``GR-<po>-<n>``.
_AUTO_NUMBER_ATTEMPTS = 50

# Refusal codes — the web client localizes on these (`api/codedRefusals.ts`).
GR_PO_CANCELLED = "goods_receipt_po_cancelled"
GR_NUMBER_TAKEN = "goods_receipt_number_taken"
GR_LINE_NOT_ON_PO = "goods_receipt_line_not_on_po"
GR_LINE_DUPLICATED = "goods_receipt_line_duplicated"
GR_LINE_REQUIRED = "goods_receipt_line_required"
GR_NOTHING_RECEIVED = "goods_receipt_nothing_received"
GR_IDEMPOTENCY_REUSED = "goods_receipt_idempotency_reused"
GR_NOT_MANUAL = "goods_receipt_not_manual"
GR_ALREADY_CANCELLED = "goods_receipt_already_cancelled"


@dataclass(frozen=True)
class ReceiptLineInput:
    po_line_item_id: uuid.UUID | None
    description: str | None
    quantity_received: Decimal


def _live_receipt_filter():
    return func.lower(func.coalesce(GoodsReceipt.status, "")).notin_(CANCELLED_GR_STATUSES)


async def received_quantities(
    db: AsyncSession, po: PurchaseOrder
) -> tuple[dict[uuid.UUID, Decimal], Decimal]:
    """What has arrived against ``po`` so far, across every live receipt.

    Returns ``(per_line, total)``: ``per_line`` maps a PO line id to the
    quantity received against it on lines that name it; ``total`` is the sum
    over EVERY live receipt line, linked or not — the figure the 3-way leg
    compares with the ordered quantity. Scoped to the PO's own entity, as the
    matcher scopes its receipt lookup.
    """
    rows = (
        await db.execute(
            apply_entity_scope(
                select(GRLineItem.po_line_item_id, func.sum(GRLineItem.quantity_received))
                .join(GoodsReceipt, GRLineItem.gr_id == GoodsReceipt.id)
                .where(GoodsReceipt.po_id == po.id, _live_receipt_filter())
                .group_by(GRLineItem.po_line_item_id),
                GoodsReceipt,
                po.entity_id,
            )
        )
    ).all()
    per_line: dict[uuid.UUID, Decimal] = {}
    total = Decimal("0")
    for line_id, qty in rows:
        qty = Decimal(qty or 0)
        total += qty
        if line_id is not None:
            per_line[line_id] = qty
    return per_line, total


async def _lock_po(db: AsyncSession, po_id: uuid.UUID, entity_id: uuid.UUID | None):
    """The PO, row-locked, within the caller's entity — or the same opaque 404
    ``GET /api/purchase-orders/{id}`` gives an unknown or out-of-scope id."""
    po = (
        await db.execute(
            apply_entity_scope(
                select(PurchaseOrder).where(PurchaseOrder.id == po_id),
                PurchaseOrder,
                entity_id,
            ).with_for_update(of=PurchaseOrder)
        )
    ).scalar_one_or_none()
    if po is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    lines = (await db.execute(select(POLineItem).where(POLineItem.po_id == po.id))).scalars().all()
    return po, list(lines)


async def _number_taken(db: AsyncSession, org_id: uuid.UUID, gr_number: str) -> bool:
    return (
        await db.execute(
            select(GoodsReceipt.id).where(
                GoodsReceipt.organization_id == org_id,
                func.lower(GoodsReceipt.gr_number) == gr_number.lower(),
            )
        )
    ).first() is not None


async def _auto_number(db: AsyncSession, org_id: uuid.UUID, po: PurchaseOrder) -> str:
    """``GR-<po_number>-<n>``: readable, and — under the PO lock — unique for the
    PO. ``n`` starts at one past the PO's receipt count and steps past any
    number a receipt from elsewhere already holds."""
    count = (
        await db.execute(select(func.count()).where(GoodsReceipt.po_id == po.id))
    ).scalar() or 0
    for n in range(count + 1, count + 1 + _AUTO_NUMBER_ATTEMPTS):
        candidate = f"GR-{po.po_number}-{n}"[:100]
        if not await _number_taken(db, org_id, candidate):
            return candidate
    # Fifty collisions in a row means the numbering space is occupied by an
    # external system; make the user supply one rather than guess further.
    raise HTTPException(
        status_code=409,
        detail=coded_refusal(
            GR_NUMBER_TAKEN,
            "Could not generate a free receipt number for this purchase order; "
            "enter the delivery note number instead.",
            grNumber=f"GR-{po.po_number}",
        ),
    )


async def _by_idempotency_key(db: AsyncSession, org_id: uuid.UUID, key: str) -> GoodsReceipt | None:
    return (
        await db.execute(
            select(GoodsReceipt)
            .options(selectinload(GoodsReceipt.line_items))
            .where(GoodsReceipt.organization_id == org_id, GoodsReceipt.idempotency_key == key)
        )
    ).scalar_one_or_none()


def _replay(
    existing: GoodsReceipt,
    po_id: uuid.UUID,
    received_date: date,
    lines: list[ReceiptLineInput],
) -> GoodsReceipt:
    """The earlier receipt a reused key names — if this request is the same
    request. A key reused for a different PO, date or set of lines is refused
    rather than answered with a receipt that says something else."""
    sent = sorted((str(line.po_line_item_id or ""), line.quantity_received) for line in lines)
    stored = sorted(
        (str(li.po_line_item_id or ""), Decimal(li.quantity_received or 0))
        for li in existing.line_items
    )
    if existing.po_id != po_id or existing.received_date != received_date or sent != stored:
        raise HTTPException(
            status_code=409,
            detail=coded_refusal(
                GR_IDEMPOTENCY_REUSED,
                "This request key was already used for a different receipt.",
            ),
        )
    return existing


def _validate_lines(
    lines: list[ReceiptLineInput], po_lines: list[POLineItem]
) -> list[tuple[POLineItem | None, ReceiptLineInput]]:
    """Pair each submitted line with the PO line it receives against.

    A PO that has lines is received line by line: each submitted line must name
    one of THIS PO's lines, at most once. A PO with no lines (an ERP header
    synced without detail) takes free-text lines instead. The description of a
    linked line is always the PO line's own — never the client's.
    """
    by_id = {li.id: li for li in po_lines}
    paired: list[tuple[POLineItem | None, ReceiptLineInput]] = []
    seen: set[uuid.UUID] = set()
    for line in lines:
        if po_lines:
            if line.po_line_item_id is None or line.po_line_item_id not in by_id:
                raise HTTPException(
                    status_code=422,
                    detail=coded_refusal(
                        GR_LINE_NOT_ON_PO,
                        "Every received line must be one of this purchase order's lines.",
                    ),
                )
            if line.po_line_item_id in seen:
                raise HTTPException(
                    status_code=422,
                    detail=coded_refusal(
                        GR_LINE_DUPLICATED,
                        "A purchase-order line can appear only once on a receipt.",
                    ),
                )
            seen.add(line.po_line_item_id)
            paired.append((by_id[line.po_line_item_id], line))
        else:
            if line.po_line_item_id is not None:
                raise HTTPException(
                    status_code=422,
                    detail=coded_refusal(
                        GR_LINE_NOT_ON_PO,
                        "Every received line must be one of this purchase order's lines.",
                    ),
                )
            if not (line.description or "").strip():
                raise HTTPException(
                    status_code=422,
                    detail=coded_refusal(
                        GR_LINE_REQUIRED,
                        "Describe what was received on each line.",
                    ),
                )
            paired.append((None, line))
    if not any(line.quantity_received > 0 for _, line in paired):
        raise HTTPException(
            status_code=422,
            detail=coded_refusal(
                GR_NOTHING_RECEIVED,
                "A receipt must record at least one unit received.",
            ),
        )
    return paired


async def create_goods_receipt(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    po_id: uuid.UUID,
    received_date: date,
    gr_number: str | None,
    lines: list[ReceiptLineInput],
    idempotency_key: str | None,
    org_settings: dict | None,
) -> tuple[GoodsReceipt, bool]:
    """Record a delivery against ``po_id``. Returns ``(receipt, created)``;
    ``created`` is False when ``idempotency_key`` replays an earlier submit."""
    po, po_lines = await _lock_po(db, po_id, entity_id)

    if idempotency_key:
        existing = await _by_idempotency_key(db, org_id, idempotency_key)
        if existing is not None:
            return _replay(existing, po.id, received_date, lines), False

    if (po.status or "").strip().lower() in CANCELLED_PO_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=coded_refusal(
                GR_PO_CANCELLED,
                f"Purchase order {po.po_number} is cancelled; nothing can be received against it.",
                poNumber=po.po_number,
            ),
        )

    paired = _validate_lines(lines, po_lines)

    number = (gr_number or "").strip()
    if number:
        if await _number_taken(db, org_id, number):
            raise HTTPException(
                status_code=409,
                detail=coded_refusal(
                    GR_NUMBER_TAKEN,
                    f"A goods receipt numbered {number} already exists.",
                    grNumber=number,
                ),
            )
    else:
        number = await _auto_number(db, org_id, po)

    receipt = GoodsReceipt(
        gr_number=number,
        po_id=po.id,
        received_date=received_date,
        status="received",
        source=GR_SOURCE_MANUAL,
        recorded_by_user_id=actor_id,
        idempotency_key=idempotency_key or None,
        organization_id=org_id,
        # The PO's entity, not the caller's header: the matcher looks receipts
        # up in the invoice's entity, which is the PO's, so a receipt booked
        # anywhere else would never be read.
        entity_id=po.entity_id,
    )
    try:
        # A SAVEPOINT so the key's unique index can refuse the insert without
        # aborting the caller's transaction. The PO lock serialises a key's
        # replays on ONE PO; the same key sent concurrently for two different
        # POs takes two locks, and this is where the second one lands.
        async with db.begin_nested():
            db.add(receipt)
            await db.flush()
    except IntegrityError:
        if not idempotency_key:
            raise
        existing = await _by_idempotency_key(db, org_id, idempotency_key)
        if existing is None:
            raise
        return _replay(existing, po.id, received_date, lines), False
    for po_line, line in paired:
        db.add(
            GRLineItem(
                gr_id=receipt.id,
                po_line_item_id=po_line.id if po_line is not None else None,
                description=po_line.description if po_line is not None else line.description,
                quantity_received=line.quantity_received,
            )
        )
    await db.flush()
    await db.refresh(receipt, attribute_names=["line_items"])

    received_total = sum((line.quantity_received for _, line in paired), Decimal("0"))
    # Number, PO and counts only — no free text, the PII-lean shape every
    # procurement audit row follows.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=actor_id,
        action="goods_receipt.created",
        entity_type="goods_receipt",
        entity_id=receipt.id,
        details={
            "gr_number": receipt.gr_number,
            "po_number": po.po_number,
            "line_count": len(paired),
            "quantity_received": format_quantity(received_total),
            "received_date": received_date.isoformat(),
        },
    )

    await _rematch(db, org_id, po.po_number, org_settings)
    return receipt, True


async def cancel_goods_receipt(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    gr_id: uuid.UUID,
    org_settings: dict | None,
) -> GoodsReceipt:
    """Cancel a hand-entered receipt. The matcher stops counting it, so the
    invoices on its PO are re-matched — which can raise a hold, never lift one."""
    receipt = (
        await db.execute(
            apply_entity_scope(
                select(GoodsReceipt)
                .options(selectinload(GoodsReceipt.line_items))
                .where(GoodsReceipt.id == gr_id),
                GoodsReceipt,
                entity_id,
            ).with_for_update(of=GoodsReceipt)
        )
    ).scalar_one_or_none()
    if receipt is None:
        raise HTTPException(status_code=404, detail="Goods receipt not found")
    if receipt.source != GR_SOURCE_MANUAL:
        raise HTTPException(
            status_code=409,
            detail=coded_refusal(
                GR_NOT_MANUAL,
                "Only a receipt recorded in FeohLedger can be cancelled here; "
                "this one came from another system.",
            ),
        )
    if (receipt.status or "").strip().lower() in CANCELLED_GR_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=coded_refusal(
                GR_ALREADY_CANCELLED,
                f"Goods receipt {receipt.gr_number} is already cancelled.",
                grNumber=receipt.gr_number,
            ),
        )

    old_status = receipt.status
    receipt.status = GR_STATUS_CANCELLED
    await db.flush()

    po_number = None
    if receipt.po_id is not None:
        po_number = (
            await db.execute(
                select(PurchaseOrder.po_number).where(PurchaseOrder.id == receipt.po_id)
            )
        ).scalar_one_or_none()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=actor_id,
        action="goods_receipt.cancelled",
        entity_type="goods_receipt",
        entity_id=receipt.id,
        details={
            "gr_number": receipt.gr_number,
            "po_number": po_number,
            "old_status": old_status,
            "new_status": receipt.status,
        },
    )

    if po_number:
        await _rematch(db, org_id, po_number, org_settings)
    return receipt


async def _rematch(
    db: AsyncSession, org_id: uuid.UUID, po_number: str, org_settings: dict | None
) -> None:
    from app.services.invoice_warnings import refresh_invoices_citing_pos

    await refresh_invoices_citing_pos(
        db, org_id, {po_number}, org_settings=org_settings, caller="goods-receipts"
    )
