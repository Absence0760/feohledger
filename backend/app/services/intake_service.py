"""Intake-form helpers — kept out of the router so the route handlers stay thin.

Holds: intake request-number generation, the status-transition graph (a small
state machine mirroring the expense-report / requisition approval shape), and
the convert-to-requisition logic (creates a ``PurchaseRequisition`` + a single
``RequisitionLineItem`` from the intake). All money math is ``Decimal`` (never
float). Conversion idempotency is owned by the router (it checks
``converted_requisition_id`` before calling :func:`convert_intake_to_requisition`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.procurement import (
    IntakeRequest,
    IntakeStatus,
    PurchaseRequisition,
    RequisitionLineItem,
    RequisitionStatus,
)

# Allowed source → target intake status transitions. An invalid source status
# is a 422 at the route boundary (never a silent no-op). ``converted`` is driven
# by the convert-to-requisition route, not a free-standing transition.
VALID_TRANSITIONS: dict[IntakeStatus, set[IntakeStatus]] = {
    IntakeStatus.open: {
        IntakeStatus.in_review,
        IntakeStatus.cancelled,
    },
    IntakeStatus.in_review: {
        IntakeStatus.approved,
        IntakeStatus.rejected,
        IntakeStatus.cancelled,
    },
    IntakeStatus.approved: {
        IntakeStatus.converted,
        IntakeStatus.cancelled,
    },
    IntakeStatus.rejected: {
        IntakeStatus.open,
    },
    IntakeStatus.converted: set(),  # terminal
    IntakeStatus.cancelled: set(),  # terminal
}


async def generate_request_number(db: AsyncSession) -> str:
    """Generate the next intake request number for this tenant.

    Format ``INTK-<year>-<seq>`` where ``seq`` is a zero-padded per-year counter.
    The count is scoped to the current tenant DB session (tenant isolation is the
    per-tenant DB), so two tenants never collide. The number is display-only
    (not a uniqueness key); callers may also pass an explicit ``request_number``."""
    year = datetime.now(UTC).year
    prefix = f"INTK-{year}-"
    existing = (
        await db.execute(
            select(func.count(IntakeRequest.id)).where(
                IntakeRequest.request_number.like(f"{prefix}%")
            )
        )
    ).scalar_one()
    seq = int(existing or 0) + 1
    return f"{prefix}{seq:04d}"


async def convert_intake_to_requisition(
    db: AsyncSession,
    *,
    intake: IntakeRequest,
    organization_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    requisition_number: str,
    department: str | None = None,
    needed_by: date | None = None,
) -> PurchaseRequisition:
    """Create a ``PurchaseRequisition`` (+ one line) from an approved intake.

    The intake's title / estimated_amount / vendor / justification seed a single
    requisition line; the header ``total`` equals the line total. Money is exact
    ``Decimal`` throughout. The caller (router) is responsible for:

    - gating on ``intake.status == approved`` and idempotency
      (``intake.converted_requisition_id`` already set),
    - flipping ``intake.status`` to ``converted`` + stamping
      ``converted_requisition_id`` after this returns,
    - the audit row + commit.

    This helper only builds the requisition graph and flushes it so the caller
    has the generated ``requisition.id``.
    """
    amount: Decimal | None = (
        Decimal(intake.estimated_amount) if intake.estimated_amount is not None else None
    )

    requisition = PurchaseRequisition(
        requisition_number=requisition_number,
        title=intake.title,
        requester_user_id=requester_user_id,
        department=department,
        status=RequisitionStatus.draft,
        needed_by=needed_by if needed_by is not None else intake.needed_by,
        justification=intake.justification,
        vendor_id=intake.vendor_id,
        total=amount if amount is not None else Decimal("0"),
        currency=intake.currency,
        organization_id=organization_id,
        entity_id=intake.entity_id,
    )
    requisition.line_items = [
        RequisitionLineItem(
            line_number=1,
            description=intake.title,
            quantity=Decimal("1"),
            unit_price=amount,
            total=amount,
        )
    ]
    db.add(requisition)
    await db.flush()
    return requisition
