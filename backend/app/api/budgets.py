"""Procurement / Requisitions — budgets router (Budget tracking vertical).

Budgets are financial config: a spend allocation for a department / project /
cost-center / GL account over a period. Spend is **computed on read** from
requisitions / POs / invoices (no stored running total) — see
``services/budget_service.py`` for the exact allocated/committed/actual model.

RBAC: read = admin / ap_manager / cfo; mutate = admin / cfo (the CFO owns
budgets). Every mutation writes a ``dispatch_audit`` row; every list/read is
entity-scoped (``X-Entity-ID``) and tenant-isolated (per-tenant DB session).
"""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_roles,
)
from app.api.pagination import PaginationParams, pagination_params
from app.models.procurement import Budget
from app.models.user import User
from app.schemas.budget import (
    BudgetCheckResponse,
    BudgetCreate,
    BudgetCurrencyRollupEntry,
    BudgetCurrencyTotal,
    BudgetListResponse,
    BudgetResponse,
    BudgetRollupResponse,
    BudgetSpendResponse,
    BudgetSummaryResponse,
    BudgetUpdate,
)
from app.services.audit_dispatch import dispatch_audit
from app.services.budget_service import compute_budget_rollup, compute_budget_spend
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.search import ilike_contains

router = APIRouter(prefix="/budgets", tags=["budgets"])

# Fields a PATCH on a budget may touch.
_BUDGET_UPDATABLE_FIELDS = (
    "name",
    "dimension",
    "dimension_value",
    "period",
    "period_start",
    "period_end",
    "amount",
    "currency",
    "notes",
)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _to_response(b: Budget) -> BudgetResponse:
    return BudgetResponse(
        id=str(b.id),
        name=b.name,
        dimension=str(b.dimension),
        dimension_value=b.dimension_value,
        period=b.period,
        period_start=b.period_start.isoformat() if b.period_start else None,
        period_end=b.period_end.isoformat() if b.period_end else None,
        amount=b.amount,
        currency=b.currency,
        notes=b.notes,
        created_at=b.created_at.isoformat() if b.created_at else "",
        updated_at=b.updated_at.isoformat() if b.updated_at else "",
    )


async def _get_budget_or_404(db: AsyncSession, budget_id: uuid.UUID) -> Budget:
    budget = (await db.execute(select(Budget).where(Budget.id == budget_id))).scalar_one_or_none()
    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")
    return budget


# ---------------------------------------------------------------------------
# List + create
# ---------------------------------------------------------------------------


def _budget_list_filters(
    query,
    *,
    dimension: str | None,
    period: str | None,
    search: str | None,
):
    """Apply the budget-list ``dimension`` / ``period`` / free-text filters.

    Shared by ``GET /api/budgets`` and ``GET /api/budgets/summary`` so the KPI
    rollup can never describe a different set than the rows it sits above — the
    page-scoped-KPI drift the summary endpoint closes. Entity scope is applied
    by the caller, because the two build their ``select()`` differently.
    """
    if dimension:
        query = query.where(Budget.dimension == dimension)
    if period:
        query = query.where(Budget.period == period)
    if search and search.strip():
        term = search.strip()
        query = query.where(
            or_(ilike_contains(Budget.name, term), ilike_contains(Budget.dimension_value, term))
        )
    return query


@router.get("", response_model=BudgetListResponse)
async def list_budgets(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    dimension: str | None = Query(None),
    period: str | None = Query(None),
    search: str | None = Query(None),
    pagination: PaginationParams = Depends(pagination_params),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = _budget_list_filters(
        apply_entity_scope(select(Budget), Budget, entity_id),
        dimension=dimension,
        period=period,
        search=search,
    )

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    paged = (
        base.order_by(Budget.created_at.desc(), Budget.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(paged)).scalars().all()
    return BudgetListResponse(
        items=[_to_response(b) for b in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("", response_model=BudgetResponse, status_code=status.HTTP_201_CREATED)
async def create_budget(
    body: BudgetCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    budget = Budget(
        name=body.name,
        dimension=body.dimension,
        dimension_value=body.dimension_value,
        period=body.period,
        period_start=body.period_start,
        period_end=body.period_end,
        amount=body.amount,
        currency=body.currency,
        notes=body.notes,
        organization_id=org_id,
        entity_id=entity_id,
    )
    db.add(budget)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="budget.created",
        entity_type="budget",
        entity_id=budget.id,
        details={
            "name": budget.name,
            "dimension": str(budget.dimension),
            "dimension_value": budget.dimension_value,
            "amount": str(budget.amount),
        },
    )
    await db.commit()
    fresh = await _get_budget_or_404(db, budget.id)
    return _to_response(fresh)


# ---------------------------------------------------------------------------
# Whole-set KPI rollup — literal `summary` segment declared BEFORE /{budget_id}
# so it isn't captured as a {budget_id} UUID (same ordering as `/check` below).
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=BudgetSummaryResponse)
async def budget_summary(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    dimension: str | None = Query(None),
    period: str | None = Query(None),
    search: str | None = Query(None),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Whole-set count + per-currency allocation totals for the budgets KPI row.

    Takes the SAME filters as ``GET /api/budgets`` and runs them through the
    same ``_budget_list_filters``, so the KPI and the table always describe one
    set. The page's ``totalAllocated`` used to reduce over the LOADED page and
    add across currencies into the org default — so it contradicted the
    whole-set ``total`` count beside it and rendered EUR + USD as one figure.

    Totals are grouped BY CURRENCY and serialised as exact decimal strings —
    never added across currencies, never FX-converted on a read.
    """
    currency_key = func.upper(Budget.currency)
    rows = (
        await db.execute(
            _budget_list_filters(
                apply_entity_scope(
                    select(
                        currency_key,
                        func.coalesce(func.sum(Budget.amount), 0),
                        func.count(),
                    ).select_from(Budget),
                    Budget,
                    entity_id,
                ),
                dimension=dimension,
                period=period,
                search=search,
            )
            .group_by(currency_key)
            .order_by(currency_key)
        )
    ).all()

    by_currency = [
        BudgetCurrencyTotal(
            currency=str(currency or "").upper() or "USD",
            total=str(Decimal(total_amount)),
            count=int(n),
        )
        for currency, total_amount, n in rows
    ]
    return BudgetSummaryResponse(
        total=sum(c.count for c in by_currency),
        by_currency=by_currency,
    )


# ---------------------------------------------------------------------------
# Org-wide budget-vs-actual rollup — literal `rollup` segment, same ordering
# rule as `summary` / `check` above (declared BEFORE /{budget_id}).
# ---------------------------------------------------------------------------


@router.get("/rollup", response_model=BudgetRollupResponse)
async def budget_rollup(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    dimension: str | None = Query(None),
    period: str | None = Query(None),
    search: str | None = Query(None),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Org-wide allocated vs committed vs actual, grouped by currency.

    The CFO counterpart of the per-budget ``GET /{id}/spend``: only that
    endpoint and the standalone ``/budgets`` page existed, so a finance leader
    had no consolidated view and had to open budgets one at a time.

    Compute-on-read, like everything else in this router — ``compute_budget_
    rollup`` folds ``compute_budget_spends`` over the matching budgets; there is
    no stored running total to drift. Same ``dimension`` / ``period`` /
    ``search`` filters as the list, through the SAME ``_budget_list_filters``,
    and the same ``X-Entity-ID`` scope, so the rollup and the table can never
    describe different sets.

    Whole-set by design (a paged rollup presented as an org-wide total is the
    dishonesty this endpoint exists to avoid), so the cost has to scale with
    something other than the budget count: each spend leg runs as ONE grouped
    query keyed on ``Budget.id`` (the invoice leg, one per distinct dimension).
    ``GET /{id}/spend`` reads that same function with a single-budget filter,
    which is what keeps its figures — and its ``excluded_row_count`` — provably
    identical to this endpoint's rather than merely intended to be.

    Money is grouped BY CURRENCY and serialised as exact decimal strings —
    never added across currencies, never FX-converted on a read. Anything the
    spend legs had to refuse rides ``excluded_row_count`` so the reader is told
    the figures are a floor rather than left to assume they are complete.
    """
    base = _budget_list_filters(
        apply_entity_scope(select(Budget), Budget, entity_id),
        dimension=dimension,
        period=period,
        search=search,
    )
    budgets = list((await db.execute(base.order_by(Budget.created_at.desc()))).scalars().all())
    rollup = await compute_budget_rollup(db, budgets)
    return BudgetRollupResponse(
        budget_count=rollup.budget_count,
        by_currency=[
            BudgetCurrencyRollupEntry(
                currency=c.currency,
                budget_count=c.budget_count,
                allocated=str(c.allocated),
                committed=str(c.committed),
                actual=str(c.actual),
                remaining=str(c.remaining),
                utilization_pct=(str(c.utilization_pct) if c.utilization_pct is not None else None),
                over_budget_count=c.over_budget_count,
                excluded_row_count=c.excluded_row_count,
            )
            for c in rollup.by_currency
        ],
        excluded_row_count=rollup.excluded_row_count,
        insufficient_data=rollup.insufficient_data,
    )


# ---------------------------------------------------------------------------
# Spend check — literal `check` segment declared BEFORE /{budget_id} so it
# isn't captured as a {budget_id} UUID (mirrors the expenses /receipt ordering).
# ---------------------------------------------------------------------------


@router.get("/check", response_model=BudgetCheckResponse)
async def check_budget(
    budget_id: uuid.UUID = Query(...),
    amount: Decimal = Query(..., ge=0),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    """Would committing ``amount`` against this budget overspend it?

    The requisition flow calls this before submit. ``remaining`` is the current
    headroom (``allocated - committed - actual``); ``remaining_after`` is what
    would be left once ``amount`` is committed; ``would_overspend`` is
    ``remaining_after < 0``. All Decimal math — never float."""
    budget = await _get_budget_or_404(db, budget_id)
    spend = await compute_budget_spend(db, budget)
    remaining_after = spend.remaining - amount
    return BudgetCheckResponse(
        budget_id=str(budget.id),
        amount=amount,
        allocated=spend.allocated,
        committed=spend.committed,
        actual=spend.actual,
        remaining=spend.remaining,
        remaining_after=remaining_after,
        would_overspend=remaining_after < 0,
        currency=spend.currency,
    )


# ---------------------------------------------------------------------------
# Get / patch / delete / spend
# ---------------------------------------------------------------------------


@router.get("/{budget_id}", response_model=BudgetResponse)
async def get_budget(
    budget_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    return _to_response(await _get_budget_or_404(db, budget_id))


@router.get("/{budget_id}/spend", response_model=BudgetSpendResponse)
async def get_budget_spend(
    budget_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    """Computed allocated vs committed vs actual vs remaining for this budget.

    Read-only display rollup: the SUMs run in Postgres over ``Numeric`` columns
    (exact) and stay ``Decimal`` through the response model, which converts to a
    JSON number only at JSON-write time."""
    budget = await _get_budget_or_404(db, budget_id)
    spend = await compute_budget_spend(db, budget)
    return BudgetSpendResponse(
        budget_id=str(budget.id),
        name=budget.name,
        dimension=str(budget.dimension),
        dimension_value=budget.dimension_value,
        currency=spend.currency,
        allocated=spend.allocated,
        committed=spend.committed,
        actual=spend.actual,
        remaining=spend.remaining,
        utilization_pct=float(spend.utilization_pct),
        excluded_row_count=spend.excluded_row_count,
    )


@router.patch("/{budget_id}", response_model=BudgetResponse)
async def update_budget(
    budget_id: uuid.UUID,
    body: BudgetUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    budget = await _get_budget_or_404(db, budget_id)
    payload = body.model_dump(exclude_unset=True)
    changed: list[str] = []
    for field in _BUDGET_UPDATABLE_FIELDS:
        if field in payload and getattr(budget, field) != payload[field]:
            setattr(budget, field, payload[field])
            changed.append(field)
    if changed:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="budget.updated",
            entity_type="budget",
            entity_id=budget.id,
            details={"fields": changed},
        )
    await db.commit()
    fresh = await _get_budget_or_404(db, budget.id)
    return _to_response(fresh)


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(
    budget_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    budget = await _get_budget_or_404(db, budget_id)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="budget.deleted",
        entity_type="budget",
        entity_id=budget.id,
        details={"name": budget.name, "amount": str(budget.amount)},
    )
    await db.delete(budget)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
