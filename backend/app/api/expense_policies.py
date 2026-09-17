"""Expense-policy CRUD — the reimbursement rules (per-diem, mileage, category
limits, receipt + pre-approval thresholds) the WF3 policy engine evaluates.

Reads are open to all four roles; mutations are admin / ap_manager only. Every
mutation writes a ``dispatch_audit`` row and is entity-scoped, mirroring
``api/expenses.py``.

``threshold_currency`` is the unit for every money threshold on the row — it is
what stops the engine comparing a €200 expense to a "100" limit as bare numbers.
Leaving it unset means "the org's reporting currency", resolved at evaluation
time rather than frozen here. The older ``per_diem_currency`` is descriptive
only; it is kept in step with ``threshold_currency`` on write.

See ``backend/docs/expense-management.md``.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
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
from app.models.expense import ExpensePolicy
from app.models.user import User
from app.schemas.expense import (
    ExpensePolicyCreate,
    ExpensePolicyListResponse,
    ExpensePolicyResponse,
    ExpensePolicyUpdate,
)
from app.services.audit_dispatch import dispatch_audit
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant_db,
    get_write_entity_id,
)

router = APIRouter(prefix="/expense-policies", tags=["expense-policies"])

_UPDATABLE_FIELDS = (
    "name",
    "active",
    "category",
    "threshold_currency",
    "per_diem_amount",
    "per_diem_currency",
    "mileage_rate",
    "category_limit",
    "requires_preapproval_above",
    "requires_receipt_above",
    "rules",
)


def _to_response(p: ExpensePolicy) -> ExpensePolicyResponse:
    return ExpensePolicyResponse(
        id=str(p.id),
        name=p.name,
        active=p.active,
        category=p.category,
        threshold_currency=p.threshold_currency,
        per_diem_amount=p.per_diem_amount,
        per_diem_currency=p.per_diem_currency,
        mileage_rate=float(p.mileage_rate) if p.mileage_rate is not None else None,
        category_limit=p.category_limit,
        requires_preapproval_above=p.requires_preapproval_above,
        requires_receipt_above=p.requires_receipt_above,
        rules=p.rules,
        created_at=p.created_at.isoformat() if p.created_at else "",
        updated_at=p.updated_at.isoformat() if p.updated_at else "",
    )


async def _get_policy_or_404(db: AsyncSession, policy_id: uuid.UUID) -> ExpensePolicy:
    policy = (
        await db.execute(select(ExpensePolicy).where(ExpensePolicy.id == policy_id))
    ).scalar_one_or_none()
    if not policy:
        raise HTTPException(status_code=404, detail="Expense policy not found")
    return policy


@router.get("", response_model=ExpensePolicyListResponse)
async def list_policies(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    active: bool | None = Query(None),
    category: str | None = Query(None),
    pagination: PaginationParams = Depends(pagination_params),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = apply_entity_scope(select(ExpensePolicy), ExpensePolicy, entity_id)
    if active is not None:
        base = base.where(ExpensePolicy.active.is_(active))
    if category:
        base = base.where(ExpensePolicy.category == category)

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    paged = (
        base.order_by(ExpensePolicy.created_at.desc(), ExpensePolicy.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(paged)).scalars().all()
    return ExpensePolicyListResponse(
        items=[_to_response(p) for p in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("", response_model=ExpensePolicyResponse, status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: ExpensePolicyCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    policy = ExpensePolicy(
        name=body.name,
        active=body.active,
        category=body.category,
        threshold_currency=body.threshold_currency,
        per_diem_amount=body.per_diem_amount,
        # Keep the legacy per-diem-only field in step with the authoritative
        # one unless the caller named it explicitly (see the model comment).
        per_diem_currency=(
            body.per_diem_currency
            if "per_diem_currency" in body.model_fields_set or body.threshold_currency is None
            else body.threshold_currency
        ),
        mileage_rate=body.mileage_rate,
        category_limit=body.category_limit,
        requires_preapproval_above=body.requires_preapproval_above,
        requires_receipt_above=body.requires_receipt_above,
        rules=body.rules,
        organization_id=org_id,
        entity_id=entity_id,
    )
    db.add(policy)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense_policy.created",
        entity_type="expense_policy",
        entity_id=policy.id,
        details={"name": policy.name, "category": policy.category},
    )
    await db.commit()
    fresh = await _get_policy_or_404(db, policy.id)
    return _to_response(fresh)


@router.get("/{policy_id}", response_model=ExpensePolicyResponse)
async def get_policy(
    policy_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    return _to_response(await _get_policy_or_404(db, policy_id))


@router.patch("/{policy_id}", response_model=ExpensePolicyResponse)
async def update_policy(
    policy_id: uuid.UUID,
    body: ExpensePolicyUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    policy = await _get_policy_or_404(db, policy_id)
    payload = body.model_dump(exclude_unset=True)
    # A threshold-currency change re-denominates every threshold on the row, so
    # the legacy per-diem field follows it unless the caller set that too.
    if payload.get("threshold_currency") and "per_diem_currency" not in payload:
        payload["per_diem_currency"] = payload["threshold_currency"]
    changed: list[str] = []
    for field in _UPDATABLE_FIELDS:
        if field in payload and getattr(policy, field) != payload[field]:
            setattr(policy, field, payload[field])
            changed.append(field)
    if changed:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="expense_policy.updated",
            entity_type="expense_policy",
            entity_id=policy.id,
            details={"fields": changed},
        )
    await db.commit()
    fresh = await _get_policy_or_404(db, policy.id)
    return _to_response(fresh)


@router.delete("/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    policy = await _get_policy_or_404(db, policy_id)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense_policy.deleted",
        entity_type="expense_policy",
        entity_id=policy.id,
        details={"name": policy.name},
    )
    await db.delete(policy)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
