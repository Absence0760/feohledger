"""Expense pre-approval requests — a spend pre-approval an employee raises
before incurring an expense, decided by a manager.

Create is open to admin / ap_manager / ap_clerk (the requester); the decision
routes (approve / reject) are admin / ap_manager and enforce segregation of
duties — a user cannot approve their own request. Every mutation is audited and
entity-scoped, mirroring ``api/expenses.py``. The approval engine's
``check_segregation`` is reused so the SoD rule + 403 detail string stay shared
with the invoice approval path.

See ``backend/docs/expense-management.md``.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_roles,
)
from app.api.pagination import PaginationParams, pagination_params
from app.models.expense import ExpensePreapproval, PreapprovalStatus
from app.models.user import User
from app.schemas.expense import (
    ExpensePreapprovalCreate,
    ExpensePreapprovalDecision,
    ExpensePreapprovalListResponse,
    ExpensePreapprovalResponse,
)
from app.services.approval_chain import check_segregation
from app.services.audit_dispatch import dispatch_audit
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant_db,
    get_write_entity_id,
)

router = APIRouter(prefix="/expense-preapprovals", tags=["expense-preapprovals"])


def _to_response(p: ExpensePreapproval) -> ExpensePreapprovalResponse:
    return ExpensePreapprovalResponse(
        id=str(p.id),
        requester_user_id=str(p.requester_user_id),
        title=p.title,
        estimated_amount=p.estimated_amount,
        currency=p.currency,
        category=p.category,
        justification=p.justification,
        status=str(p.status),
        decided_by=str(p.decided_by) if p.decided_by else None,
        decided_at=p.decided_at.isoformat() if p.decided_at else None,
        expense_report_id=str(p.expense_report_id) if p.expense_report_id else None,
        created_at=p.created_at.isoformat() if p.created_at else "",
        updated_at=p.updated_at.isoformat() if p.updated_at else "",
    )


async def _get_preapproval_or_404(
    db: AsyncSession, preapproval_id: uuid.UUID
) -> ExpensePreapproval:
    row = (
        await db.execute(select(ExpensePreapproval).where(ExpensePreapproval.id == preapproval_id))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Pre-approval not found")
    return row


@router.get("", response_model=ExpensePreapprovalListResponse)
async def list_preapprovals(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    requester_user_id: uuid.UUID | None = Query(None),
    pagination: PaginationParams = Depends(pagination_params),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = apply_entity_scope(select(ExpensePreapproval), ExpensePreapproval, entity_id)
    if status_filter:
        base = base.where(ExpensePreapproval.status == status_filter)
    if requester_user_id:
        base = base.where(ExpensePreapproval.requester_user_id == requester_user_id)

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    paged = (
        base.order_by(ExpensePreapproval.created_at.desc(), ExpensePreapproval.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(paged)).scalars().all()
    return ExpensePreapprovalListResponse(
        items=[_to_response(p) for p in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("", response_model=ExpensePreapprovalResponse, status_code=status.HTTP_201_CREATED)
async def create_preapproval(
    body: ExpensePreapprovalCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    # The requester is always the authenticated user — a request can't be
    # raised on someone else's behalf (the body field is ignored for safety so
    # SoD on the decision side stays meaningful).
    preapproval = ExpensePreapproval(
        requester_user_id=user.id,
        title=body.title,
        estimated_amount=body.estimated_amount,
        currency=body.currency,
        category=body.category,
        justification=body.justification,
        status=PreapprovalStatus.pending,
        organization_id=org_id,
        entity_id=entity_id,
    )
    db.add(preapproval)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense_preapproval.created",
        entity_type="expense_preapproval",
        entity_id=preapproval.id,
        details={"title": preapproval.title, "estimated_amount": str(preapproval.estimated_amount)},
    )
    await db.commit()
    fresh = await _get_preapproval_or_404(db, preapproval.id)
    return _to_response(fresh)


@router.get("/{preapproval_id}", response_model=ExpensePreapprovalResponse)
async def get_preapproval(
    preapproval_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    return _to_response(await _get_preapproval_or_404(db, preapproval_id))


async def _decide(
    db: AsyncSession,
    preapproval_id: uuid.UUID,
    user: User,
    org_id: uuid.UUID,
    *,
    new_status: PreapprovalStatus,
    reason: str | None,
) -> ExpensePreapprovalResponse:
    preapproval = await _get_preapproval_or_404(db, preapproval_id)
    if preapproval.status != PreapprovalStatus.pending:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot decide a pre-approval in '{preapproval.status}' state",
        )
    # Segregation of duties — a user can't approve / reject their own request.
    # Reuse the invoice approval helper via a tiny attribute shim so the SoD
    # rule + 403 detail string stay shared with the invoice path.
    check_segregation(
        SimpleNamespace(
            uploaded_by_id=preapproval.requester_user_id,
            # No editor-tracking column on this table, so there is no second
            # implicated actor to name. Stated rather than left absent: a
            # missing attribute on a fraud control reads as an oversight, and
            # `violates_segregation`'s getattr default would silently supply it.
            segregation_actor_ids=None,
        ),
        user.id,
        {"require_segregation": True},
    )

    preapproval.status = new_status
    preapproval.decided_by = user.id
    preapproval.decided_at = datetime.now(UTC)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action=f"expense_preapproval.{new_status}",
        entity_type="expense_preapproval",
        entity_id=preapproval.id,
        details={"reason": reason} if reason else None,
    )
    await db.commit()
    fresh = await _get_preapproval_or_404(db, preapproval.id)
    return _to_response(fresh)


@router.post("/{preapproval_id}/approve", response_model=ExpensePreapprovalResponse)
async def approve_preapproval(
    preapproval_id: uuid.UUID,
    body: ExpensePreapprovalDecision | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    return await _decide(
        db,
        preapproval_id,
        user,
        org_id,
        new_status=PreapprovalStatus.approved,
        reason=body.reason if body else None,
    )


@router.post("/{preapproval_id}/reject", response_model=ExpensePreapprovalResponse)
async def reject_preapproval(
    preapproval_id: uuid.UUID,
    body: ExpensePreapprovalDecision | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    return await _decide(
        db,
        preapproval_id,
        user,
        org_id,
        new_status=PreapprovalStatus.rejected,
        reason=body.reason if body else None,
    )
