"""Purchase-requisition helpers — kept out of the router so the route handlers
stay thin.

Holds: requisition-number generation, exact line-total + header-total recompute
(all ``Decimal`` math, never float), the status-transition guard (a small state
machine mirroring the expense-report approval shape), and the requisition→PO
conversion (creates a ``PurchaseOrder`` + ``POLineItem`` rows from the
requisition lines). Conversion idempotency is owned by the router (it checks
``converted_po_id`` before calling :func:`convert_requisition_to_po`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, status

from app.models.procurement import (
    POLineItem,
    PurchaseOrder,
    PurchaseRequisition,
    RequisitionLineItem,
    RequisitionStatus,
    po_currency_code,
)

# Allowed source → target requisition status transitions. An invalid source
# status is a 422 at the route boundary (never a silent no-op). ``converted``
# is driven by the convert-to-PO route, not a free-standing transition.
VALID_TRANSITIONS: dict[RequisitionStatus, set[RequisitionStatus]] = {
    RequisitionStatus.draft: {
        RequisitionStatus.pending_approval,
        RequisitionStatus.cancelled,
    },
    RequisitionStatus.submitted: {
        RequisitionStatus.pending_approval,
        RequisitionStatus.cancelled,
    },
    RequisitionStatus.pending_approval: {
        RequisitionStatus.approved,
        RequisitionStatus.rejected,
        RequisitionStatus.cancelled,
    },
    RequisitionStatus.approved: {
        RequisitionStatus.converted,
        RequisitionStatus.cancelled,
    },
    RequisitionStatus.rejected: {
        RequisitionStatus.draft,
    },
    RequisitionStatus.converted: set(),  # terminal
    RequisitionStatus.cancelled: set(),  # terminal
}


def guard_transition(current: RequisitionStatus, target: RequisitionStatus) -> None:
    """Raise 422 if ``current → target`` is not an allowed requisition move."""
    if target not in VALID_TRANSITIONS.get(current, set()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot move a requisition from '{current}' to '{target}'.",
        )


#: The precision every requisition / PO money column actually holds
#: (``Numeric(15, 2)``). ``quantity`` is ``Numeric(12, 4)``, so the raw product
#: can carry six decimal places — a figure no column on either side of the
#: conversion can store.
_MONEY_QUANT = Decimal("0.01")


def line_total(quantity: Decimal | None, unit_price: Decimal | None) -> Decimal | None:
    """``quantity * unit_price``, quantized to the 2 dp the column stores.

    Returns ``None`` when either side is absent (a description-only line carries
    no money).

    **The quantize is load-bearing, not cosmetic.** ``line_items.total`` and
    ``purchase_requisitions.total`` are both ``Numeric(15, 2)`` while the product
    can carry 6 dp, so returning the raw product meant Postgres rounded each
    line on the way in while :func:`recompute_total` summed the UNROUNDED
    values: the header became the rounding of a sum and the lines a sum of
    roundings. Twelve lines of ``1.5 x 10.01`` stored a header of ``180.18``
    against lines summing to ``180.24`` — six cents apart in one response,
    growing linearly with the line count, and carried onto the
    ``PurchaseOrder`` that ``po_matching`` runs its tolerance gate against.
    Rounding here means the figure summed is the figure stored.

    ``ROUND_HALF_UP``, matching every other money quantize in the codebase.
    """
    if quantity is None or unit_price is None:
        return None
    return (Decimal(quantity) * Decimal(unit_price)).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


def recompute_total(req: PurchaseRequisition) -> Decimal:
    """Recompute the requisition header ``total`` from its line items.

    Sums each line's ``total`` (already ``quantity * unit_price``) as ``Decimal``
    so the header total can never drift from its lines. Lines with no money
    (``total is None``) contribute zero."""
    total = Decimal("0")
    for li in req.line_items:
        if li.total is not None:
            total += Decimal(li.total)
    req.total = total
    return total


def build_line_items(rows: list) -> list[RequisitionLineItem]:
    """Build ``RequisitionLineItem`` rows from create/update payload items,
    stamping each line's exact ``total``. ``rows`` are
    ``RequisitionLineItemCreate`` schema instances."""
    out: list[RequisitionLineItem] = []
    for idx, row in enumerate(rows, start=1):
        catalog_item_uuid = _opt_uuid(row.catalog_item_id, "catalog_item_id")
        gl_uuid = _opt_uuid(row.gl_account_id, "gl_account_id")
        out.append(
            RequisitionLineItem(
                line_number=row.line_number if row.line_number is not None else idx,
                catalog_item_id=catalog_item_uuid,
                item_code=row.item_code,
                description=row.description,
                quantity=row.quantity,
                unit_price=row.unit_price,
                total=line_total(row.quantity, row.unit_price),
                gl_account_id=gl_uuid,
                uom=row.uom,
            )
        )
    return out


def next_requisition_number(existing_count: int) -> str:
    """Deterministic-ish requisition number when the client doesn't supply one.

    Format ``REQ-<YYYY>-<seq>`` where ``seq`` is ``existing_count + 1``
    zero-padded. The caller passes the current row count; uniqueness is not
    DB-enforced (the schema has no unique index), so the API requires the client
    to supply ``requisition_number`` — this is only the convenience default."""
    year = datetime.now(UTC).year
    return f"REQ-{year}-{existing_count + 1:05d}"


def convert_requisition_to_po(
    req: PurchaseRequisition,
    *,
    org_id: uuid.UUID,
    po_number: str,
) -> PurchaseOrder:
    """Build a ``PurchaseOrder`` (+ ``POLineItem`` rows) from an approved
    requisition's lines.

    Pure construction — the caller adds the PO to the session, flushes, links
    ``req.converted_po_id`` / ``req.status`` and writes the audit row. The PO
    inherits the requisition's entity, vendor, exact ``total`` and the
    ``currency`` that total is in; each line's ``total`` carries over unchanged
    (already ``Decimal``). The currency is the same rule migration 0099's
    backfill applies to every PO converted before the column existed, so a PO
    converted today and one converted last year carry the same answer."""
    po = PurchaseOrder(
        po_number=po_number,
        vendor_id=req.vendor_id,
        total=Decimal(req.total or 0),
        currency=po_currency_code(req.currency),
        status="open",
        organization_id=org_id,
        entity_id=req.entity_id,
    )
    for li in req.line_items:
        po.line_items.append(
            POLineItem(
                description=li.description,
                quantity=li.quantity,
                unit_price=li.unit_price,
                total=li.total,
            )
        )
    return po


def _opt_uuid(raw: str | None, field: str) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field}")
