"""``list_pending_approvals`` tool — invoices awaiting approval.

Invoices in ``ready_for_review`` joined to their active approval ``WorkflowStep``
(``step_type='approval'``, ``completed_at IS NULL``). ``assignee='me'`` filters
to the current user's queue. Entity-scoped.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import WorkflowInstance, WorkflowStep
from app.services.assistant.tools.schemas import (
    PendingApprovalRow,
    PendingApprovalsParams,
    PendingApprovalsResult,
)
from app.tenant import apply_entity_scope


async def list_pending_approvals(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    current_user_id: uuid.UUID,
    params: PendingApprovalsParams,
    control_db: AsyncSession | None = None,
) -> PendingApprovalsResult:
    stmt = (
        select(Invoice, WorkflowStep.assigned_to, WorkflowStep.created_at)
        .join(WorkflowInstance, WorkflowInstance.invoice_id == Invoice.id)
        .join(WorkflowStep, WorkflowStep.instance_id == WorkflowInstance.id)
        .where(Invoice.status == InvoiceStatus.ready_for_review.value)
        .where(WorkflowStep.step_type == "approval")
        .where(WorkflowStep.completed_at.is_(None))
    )
    if params.assignee == "me":
        stmt = stmt.where(WorkflowStep.assigned_to == current_user_id)
    stmt = apply_entity_scope(stmt, Invoice, entity_id)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    rows = (
        await db.execute(
            stmt.order_by(WorkflowStep.created_at.asc().nullslast()).limit(params.limit)
        )
    ).all()

    items = [
        PendingApprovalRow(
            invoice_id=str(inv.id),
            invoice_number=inv.invoice_number,
            vendor_name=inv.vendor_name,
            amount=inv.amount,
            currency=inv.currency or None,
            assigned_to_id=str(assigned_to) if assigned_to else None,
            waiting_since=created_at.date() if created_at else None,
        )
        for inv, assigned_to, created_at in rows
    ]
    return PendingApprovalsResult(items=items, total=int(total))
