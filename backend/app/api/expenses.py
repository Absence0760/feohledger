"""Expense management endpoints — out-of-pocket / card expenses and the reports
that group them for approval + reimbursement.

WF1 (foundation) wires two routers: ``/expenses`` (CRUD + receipt upload /
download proxy) and ``/expense-reports`` (CRUD + attach/detach with
total-amount recompute). Policies, pre-approvals, and card-transaction
reconciliation land in later workflows (WF2-4); their models + schemas already
exist. See ``backend/docs/expense-management.md``.
"""

import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_current_user,
    get_org_id,
    require_roles,
)
from app.api.pagination import (
    MAX_SELECT_ALL_IDS,
    MatchingIdsResponse,
    PaginationParams,
    pagination_params,
)
from app.api.sorting import SortParams, resolve_order_by, sort_params
from app.models.expense import (
    CorporateCardTransaction,
    Expense,
    ExpensePolicy,
    ExpensePreapproval,
    ExpenseReport,
    ExpenseReportStatus,
    ExpenseStatus,
    PreapprovalStatus,
)
from app.models.gl_account import GLAccount
from app.models.organization import Organization
from app.models.user import User
from app.schemas.expense import (
    ExpenseBulkGlCode,
    ExpenseBulkGlCodeResponse,
    ExpenseBulkGlCodeSkip,
    ExpenseCreate,
    ExpenseCurrencyTotal,
    ExpenseListResponse,
    ExpenseReportAttach,
    ExpenseReportCreate,
    ExpenseReportDecision,
    ExpenseReportListResponse,
    ExpenseReportResponse,
    ExpenseReportSummary,
    ExpenseReportUpdate,
    ExpenseResponse,
    ExpenseSummaryResponse,
    ExpenseUpdate,
)
from app.schemas.money import json_money
from app.services.approval_chain import check_segregation
from app.services.audit_dispatch import dispatch_audit
from app.services.currency_conversion import resolve_reporting_currency
from app.services.expense_currency import (
    ExpenseConversionError,
    ReportRollup,
    clear_expense_conversion,
    clear_report_reporting_amount,
    conversion_requires_rate_source,
    lock_expense_conversion,
    lock_report_reporting_amount,
    normalize_currency,
    report_amount_for_gate,
    rollup_report_lines,
)
from app.services.expense_policy import (
    blocking_violations,
    evaluate_expense,
    evaluate_report,
)
from app.services.fx_adapters import UnknownFxProviderError, get_fx_adapter
from app.services.storage import get_file, upload_expense_receipt
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.dates import utc_today
from app.utils.search import ilike_contains

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/expenses", tags=["expenses"])
reports_router = APIRouter(prefix="/expense-reports", tags=["expense-reports"])

# Fields a PATCH on an expense may touch (status excluded — owned by later flows).
_EXPENSE_UPDATABLE_FIELDS = (
    "expense_date",
    "merchant",
    "category",
    "description",
    "amount",
    "currency",
    "payment_method",
    "reimbursable",
    "mileage_miles",
)
_REPORT_UPDATABLE_FIELDS = (
    "report_number",
    "title",
    "currency",
    "notes",
)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _receipt_url(file_key: str | None) -> str | None:
    return f"/api/expenses/receipt/{file_key}" if file_key else None


def _to_response(e: Expense) -> ExpenseResponse:
    return ExpenseResponse(
        id=str(e.id),
        report_id=str(e.report_id) if e.report_id else None,
        expense_date=e.expense_date.isoformat() if e.expense_date else "",
        merchant=e.merchant,
        category=e.category,
        description=e.description,
        amount=e.amount,
        currency=e.currency,
        # Rate-locked expression of `amount` in the owning report's currency —
        # exact decimal strings.
        converted_amount=str(e.converted_amount) if e.converted_amount is not None else None,
        converted_currency=e.converted_currency,
        converted_fx_rate=str(e.converted_fx_rate) if e.converted_fx_rate is not None else None,
        converted_fx_locked_at=(
            e.converted_fx_locked_at.isoformat() if e.converted_fx_locked_at else None
        ),
        gl_account_id=str(e.gl_account_id) if e.gl_account_id else None,
        receipt_file_key=e.receipt_file_key,
        receipt_url=_receipt_url(e.receipt_file_key),
        payment_method=str(e.payment_method),
        card_transaction_id=str(e.card_transaction_id) if e.card_transaction_id else None,
        policy_violations=e.policy_violations,
        status=str(e.status),
        reimbursable=e.reimbursable,
        mileage_miles=float(e.mileage_miles) if e.mileage_miles is not None else None,
        created_at=e.created_at.isoformat() if e.created_at else "",
        updated_at=e.updated_at.isoformat() if e.updated_at else "",
    )


def _report_to_response(r: ExpenseReport) -> ExpenseReportResponse:
    return ExpenseReportResponse(
        id=str(r.id),
        report_number=r.report_number,
        title=r.title,
        employee_user_id=str(r.employee_user_id),
        status=str(r.status),
        submitted_at=r.submitted_at.isoformat() if r.submitted_at else None,
        approved_at=r.approved_at.isoformat() if r.approved_at else None,
        approved_by=str(r.approved_by) if r.approved_by else None,
        total_amount=r.total_amount,
        total_amount_exact=str(r.total_amount),
        currency=r.currency,
        # Total expressed in the org reporting currency at the rate locked on
        # submit — the figure the CFO threshold gate compares (issue #157).
        reporting_amount=str(r.reporting_amount) if r.reporting_amount is not None else None,
        reporting_currency=r.reporting_currency,
        reporting_fx_rate=str(r.reporting_fx_rate) if r.reporting_fx_rate is not None else None,
        reporting_fx_locked_at=(
            r.reporting_fx_locked_at.isoformat() if r.reporting_fx_locked_at else None
        ),
        notes=r.notes,
        expenses=[_to_response(e) for e in sorted(r.expenses, key=lambda x: x.expense_date)],
        created_at=r.created_at.isoformat() if r.created_at else "",
        updated_at=r.updated_at.isoformat() if r.updated_at else "",
    )


async def _get_expense_or_404(db: AsyncSession, expense_id: uuid.UUID) -> Expense:
    expense = (
        await db.execute(select(Expense).where(Expense.id == expense_id))
    ).scalar_one_or_none()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")
    return expense


async def _get_report_or_404(db: AsyncSession, report_id: uuid.UUID) -> ExpenseReport:
    report = (
        await db.execute(
            select(ExpenseReport)
            .where(ExpenseReport.id == report_id)
            # populate_existing so a report already in the identity map has its
            # `expenses` collection refreshed from the DB — without it, a second
            # fetch in the same session (e.g. after an attach mutation +
            # re-fetch) would return the stale, previously-loaded collection.
            .execution_options(populate_existing=True)
            .options(selectinload(ExpenseReport.expenses))
        )
    ).scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Expense report not found")
    return report


# Report states whose ``total_amount`` is locked in for an approval decision —
# the CFO-threshold gate + approval signature were evaluated against that total,
# so its composition/amount must not change afterwards. ``draft`` is freely
# editable; ``rejected`` / ``cancelled`` are terminal and their (draft-status)
# expenses may be detached to be re-reported elsewhere.
_LOCKED_REPORT_STATUSES = frozenset(
    {
        ExpenseReportStatus.submitted,
        ExpenseReportStatus.pending_approval,
        ExpenseReportStatus.approved,
        ExpenseReportStatus.reimbursed,
    }
)


def _require_draft_report(report: ExpenseReport) -> None:
    """A report can only be *built up* (attach new expenses) while it's a draft;
    a report already submitted/approved/terminal never accepts new lines (issue
    #155)."""
    if report.status != ExpenseReportStatus.draft:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot modify a report in '{report.status}' state; it has left draft.",
        )


def _require_report_unlocked(report: ExpenseReport) -> None:
    """Reject any amount-/composition-changing mutation to a report whose total
    is locked in for an approval decision (issue #155) — editing / deleting /
    moving an expense off a submitted-or-approved report would silently bypass
    the CFO gate and stale-sign the approval. Terminal (rejected/cancelled)
    reports stay editable so their expenses can be corrected + re-reported."""
    if report.status in _LOCKED_REPORT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot modify a report in '{report.status}' state; its total is locked.",
        )


async def _report_rollup(db: AsyncSession, report: ExpenseReport) -> ReportRollup:
    """Roll the report's attached expenses into its own currency (no FX call).

    Every line contributes its *rate-locked* ``converted_amount`` (or its face
    ``amount`` when it is already denominated in the report currency). A foreign
    line with no usable lock contributes NOTHING and is counted as unconverted —
    silently summing it at face value across currencies was issue #157.
    """
    rows = (
        await db.execute(
            select(
                Expense.id,
                Expense.amount,
                Expense.currency,
                Expense.converted_amount,
                Expense.converted_currency,
            ).where(Expense.report_id == report.id)
        )
    ).all()
    return rollup_report_lines(
        [
            {
                "id": r.id,
                "amount": r.amount,
                "currency": r.currency,
                "converted_amount": r.converted_amount,
                "converted_currency": r.converted_currency,
            }
            for r in rows
        ],
        report_currency=report.currency,
    )


async def _recompute_report_total(db: AsyncSession, report: ExpenseReport) -> ReportRollup:
    """Recompute ``total_amount`` from the report's currently-attached expenses.

    Money stays exact: every figure is ``Decimal`` (never float), quantized to
    2 dp. The composition just changed, so any report-level reporting-currency
    lock is invalidated — ``submit`` re-locks it against the new total.
    """
    rollup = await _report_rollup(db, report)
    report.total_amount = rollup.total
    clear_report_reporting_amount(report)
    return rollup


def _fx_adapter_for(org: Organization):
    """FX adapter from the org's config, or ``None`` when there is no usable
    rate source.

    ``mock`` (deterministic, no network) unless the org names a provider —
    local-first, so a fresh clone converts multi-currency expense reports with
    no cloud account. A provider name we have no adapter for now raises at the
    dispatcher instead of silently resolving to ``mock``'s hardcoded rate table
    (see `fx_adapters.dispatcher`); ``None`` is how that reaches the two
    callers, each of which already has a documented posture for "no FX
    available" — refuse the attach, or leave the report figure NULL so the CFO
    gate fails closed. Returning `None` keeps both, where letting the raise
    escape would 500 an ordinary expense save.
    """
    try:
        return get_fx_adapter((org.settings or {}).get("fx"))
    except UnknownFxProviderError:
        logger.warning("expense FX conversion unavailable: org names an unsupported provider")
        return None


async def _lock_line_conversion(expense: Expense, report: ExpenseReport, org: Organization) -> None:
    """Lock ``expense`` into ``report``'s currency, or 422.

    Fail-closed: rather than attaching a line we cannot express in the report's
    currency (which would understate the total the CFO gate reads), the write is
    rejected. The message carries only currency codes — no PII."""
    fx_adapter = _fx_adapter_for(org)
    if conversion_requires_rate_source(
        source_currency=expense.currency,
        target_currency=report.currency,
        fx_adapter=fx_adapter,
    ):
        # Same fail-closed direction as an unconvertible currency pair: refuse
        # the attach rather than understate the total. No provider name — this
        # message is read by any expense user, not just the admin who owns the
        # setting.
        #
        # The pair-first rule lives in `conversion_requires_rate_source`, shared
        # with the report-level lock below so the two levels cannot drift: a
        # same-currency conversion needs no rate at all, and demanding an
        # adapter for one turned a bad `settings.fx.provider` value into an
        # outage on work that has no FX question in it.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No FX rate source is configured, so this line cannot be converted.",
        )
    try:
        await lock_expense_conversion(
            expense, target_currency=report.currency, fx_adapter=fx_adapter
        )
    except ExpenseConversionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


async def _active_policies(db: AsyncSession, entity_id: uuid.UUID | None) -> list[ExpensePolicy]:
    """The active policies that govern a row belonging to ``entity_id``.

    **Entity-scoped.** ``ExpensePolicy`` carries `EntityMixin`, the CRUD router
    lists and stamps it per-entity, and `receipt_required` / `preapproval_required`
    are BLOCKING codes — but this read had no scope at all, so a subsidiary's
    reimbursement table governed every *other* subsidiary's expenses. One
    entity's ``requires_receipt_above: 1.00`` blocked the whole tenant's
    submissions, and its looser limits silently sanctioned spend it never
    approved. Same class as the fix in `services/vendor_matching`.

    ``include_shared=True``: a NULL ``entity_id`` is an *unstamped* row (the API
    always stamps one via `get_write_entity_id`, so NULL means seeded or written
    around it), and dropping it would silently switch a live policy OFF — the
    fail-OPEN direction. `apply_entity_scope` is a no-op when ``entity_id`` is
    itself NULL, which keeps an unstamped expense on the old whole-tenant
    behaviour rather than un-policing it.

    Ordered so the engine's "first applicable policy carries the mileage rate"
    rule is deterministic — an unordered SELECT let the same expense flag or not
    flag between runs whenever two applicable policies both set a rate."""
    return list(
        (
            await db.execute(
                apply_entity_scope(
                    select(ExpensePolicy).where(ExpensePolicy.active.is_(True)),
                    ExpensePolicy,
                    entity_id,
                    include_shared=True,
                ).order_by(ExpensePolicy.created_at, ExpensePolicy.id)
            )
        )
        .scalars()
        .all()
    )


async def _approved_preapproval_amount(db: AsyncSession, expense: Expense) -> Decimal | None:
    """Largest approved pre-approval estimate covering this expense.

    A pre-approval covers an expense when it's approved, denominated in the
    SAME currency, and either linked to the expense's report or (loosely)
    matching its category. Returns the largest matching estimate so the policy
    engine can compare it against the amount.

    The currency match is load-bearing (issue #157): the engine compares
    ``estimated_amount >= expense.amount`` as bare numbers, so without it a
    €500 EUR pre-approval would silently satisfy the pre-approval requirement
    for a $500 USD expense. We deliberately do NOT convert here — this is an
    advisory, best-effort path with no FX budget, and "not covered" is the
    fail-closed answer (it raises a blocking violation rather than waving the
    expense through).

    Scoped to the expense's own entity for the same reason the currency is
    matched: a pre-approval is a specific authorization, and one subsidiary's
    approved request has no standing to clear another subsidiary's blocking
    `preapproval_required`. Unlike the policy read this scopes STRICTLY (no
    `include_shared`) — excluding an unstamped row leaves the violation raised,
    which is the fail-closed direction here."""
    if expense.report_id is None and expense.category is None:
        return None
    conditions = []
    if expense.report_id is not None:
        conditions.append(ExpensePreapproval.expense_report_id == expense.report_id)
    if expense.category is not None:
        conditions.append(ExpensePreapproval.category == expense.category)
    if not conditions:
        return None
    rows = (
        (
            await db.execute(
                apply_entity_scope(
                    select(ExpensePreapproval.estimated_amount),
                    ExpensePreapproval,
                    expense.entity_id,
                ).where(
                    ExpensePreapproval.status == PreapprovalStatus.approved,
                    func.upper(func.coalesce(ExpensePreapproval.currency, "USD"))
                    == normalize_currency(expense.currency),
                    or_(*conditions),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    return max(Decimal(r) for r in rows)


async def _refresh_policy_violations(db: AsyncSession, expense: Expense, org: Organization) -> None:
    """Recompute ``expense.policy_violations`` from the active policies.

    ``org`` supplies the reporting currency, which is the unit a policy's
    thresholds are read in when the policy itself names none — without it the
    engine would compare a foreign-currency expense to a bare threshold number.

    Best-effort: any failure leaves the stored value untouched and never breaks
    the surrounding write (the violations are advisory)."""
    try:
        policies = await _active_policies(db, expense.entity_id)
        covered = await _approved_preapproval_amount(db, expense)
        violations = evaluate_expense(
            expense,
            policies,
            approved_preapproval_amount=covered,
            default_threshold_currency=resolve_reporting_currency(org.settings),
        )
        expense.policy_violations = violations or None
    except Exception:  # pragma: no cover — advisory, never break the write
        pass


# ---------------------------------------------------------------------------
# Expenses — list + create
# ---------------------------------------------------------------------------


def _expense_list_filters(
    query,
    *,
    status_filter: str | None,
    report_id: uuid.UUID | None,
    search: str | None,
):
    """Apply the expense-list status / report / free-text filters to ``query``.

    Shared by ``GET /api/expenses`` and ``GET /api/expenses/summary`` so the
    rollup can never describe a different set than the rows it sits above — the
    exact drift that made the KPI row contradict itself. Entity scope is
    applied by the caller, because the two build their ``select()`` differently.
    """
    if status_filter:
        query = query.where(Expense.status == status_filter)
    if report_id:
        query = query.where(Expense.report_id == report_id)
    if search and search.strip():
        term = search.strip()
        query = query.where(
            or_(
                ilike_contains(Expense.merchant, term),
                ilike_contains(Expense.description, term),
                ilike_contains(Expense.category, term),
            )
        )
    return query


# `sort=` allowlist for `GET /expenses` — see `api/sorting.py`. `.id` is
# always appended as the final tie-break regardless of which column is
# picked (mirrors the pre-existing `created_at, id` default order below).
EXPENSE_SORTABLE_COLUMNS: dict[str, object] = {
    "created_at": Expense.created_at,
    "expense_date": Expense.expense_date,
    "amount": Expense.amount,
    "merchant": Expense.merchant,
    "status": Expense.status,
    "category": Expense.category,
}


@router.get("", response_model=ExpenseListResponse)
async def list_expenses(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    report_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None),
    sort: SortParams = Depends(sort_params),
    pagination: PaginationParams = Depends(pagination_params),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Paginated, entity-scoped expense list.

    `search` matches the three free-text columns the row actually shows —
    merchant, description, category. It composes with `status` / `report_id`
    and with the entity scope: without it the page could only filter the rows
    it had already loaded, so a term matching an expense past the first page
    read as "nothing matched".
    """
    base = _expense_list_filters(
        apply_entity_scope(select(Expense), Expense, entity_id),
        status_filter=status_filter,
        report_id=report_id,
        search=search,
    )

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    # `.id` tie-breaker: bulk-imported expenses can share `created_at`, so
    # without it Postgres can order them differently between pages — a row
    # duplicated onto two pages or skipped entirely. `sort=`/`order=`
    # (validated against `EXPENSE_SORTABLE_COLUMNS`) override the default
    # when supplied.
    order_by = resolve_order_by(
        sort,
        EXPENSE_SORTABLE_COLUMNS,
        id_column=Expense.id,
        default=[Expense.created_at.desc(), Expense.id.desc()],
    )
    paged = base.order_by(*order_by).offset(pagination.offset).limit(pagination.limit)
    rows = (await db.execute(paged)).scalars().all()
    return ExpenseListResponse(
        items=[_to_response(e) for e in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


# Registered BEFORE the parametric `/{expense_id}` route — same reason
# `/export` / `/summary` / `/bulk-gl-code` sit above it.
@router.get("/ids", response_model=MatchingIdsResponse)
async def list_expense_ids(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    report_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Every expense id matching the caller's list filters — the resolver
    behind "select all N matching" on the expenses list page. Same filters
    (and the same shared `_expense_list_filters`) as `GET /expenses`, so the
    two describe the same set."""
    base = _expense_list_filters(
        apply_entity_scope(select(Expense.id), Expense, entity_id),
        status_filter=status_filter,
        report_id=report_id,
        search=search,
    )
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    query = base.order_by(Expense.created_at.desc(), Expense.id.desc()).limit(MAX_SELECT_ALL_IDS)
    ids = [str(row) for row in (await db.execute(query)).scalars().all()]
    return MatchingIdsResponse(ids=ids, total=total, truncated=total > len(ids))


@router.post("", response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
async def create_expense(
    body: ExpenseCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
    org: Organization = Depends(get_tenant),
):
    gl_uuid: uuid.UUID | None = None
    if body.gl_account_id:
        try:
            gl_uuid = uuid.UUID(body.gl_account_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid gl_account_id")

    report_uuid: uuid.UUID | None = None
    if body.report_id:
        try:
            report_uuid = uuid.UUID(body.report_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid report_id")
        report = await _get_report_or_404(db, report_uuid)
        # Creating an expense with `report_id` already set IS an attach, and it
        # must gate exactly like the two other attach paths
        # (`POST /expense-reports/{id}/expenses` and a `PATCH` that moves an
        # expense onto a report). Without this a clerk could add a line to an
        # APPROVED report: the recompute below would move `total_amount` past
        # the CFO threshold the approval was granted under and, worse, null the
        # locked `reporting_*` figure that approval and its audit row were
        # derived from. Same rule as `attach_expenses` — a report only takes new
        # lines while it is a draft.
        _require_draft_report(report)
    else:
        report = None

    expense = Expense(
        report_id=report_uuid,
        expense_date=body.expense_date,
        merchant=body.merchant,
        category=body.category,
        description=body.description,
        amount=body.amount,
        currency=body.currency,
        gl_account_id=gl_uuid,
        payment_method=body.payment_method,
        reimbursable=body.reimbursable,
        mileage_miles=body.mileage_miles,
        organization_id=org_id,
        entity_id=entity_id,
    )
    db.add(expense)
    await db.flush()

    if report is not None:
        # Lock the line into the report's currency BEFORE the total recompute —
        # an unlocked foreign line would be excluded from the total (issue #157).
        await _lock_line_conversion(expense, report, org)
        await db.flush()
        await _recompute_report_total(db, report)

    await _refresh_policy_violations(db, expense, org)

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense.created",
        entity_type="expense",
        entity_id=expense.id,
        details={"amount": str(expense.amount), "category": expense.category},
    )
    await db.commit()
    fresh = await _get_expense_or_404(db, expense.id)
    return _to_response(fresh)


# ---------------------------------------------------------------------------
# Receipt upload + download proxy — declared BEFORE /{expense_id} so the
# literal `receipt` segment isn't captured as an {expense_id} UUID.
# ---------------------------------------------------------------------------


@router.get("/receipt/{file_key:path}")
async def get_expense_receipt(
    file_key: str,
    user: User = Depends(get_current_user),
):
    """Proxy a stored expense receipt from S3.

    Keys are stamped ``<org_id>/expenses/<expense_id>/<filename>`` at upload.
    The caller must belong to the org in the first segment — same 404 for
    wrong-org and missing-file so the response can't enumerate prefixes
    (mirrors the invoice / contract file endpoints).
    """
    prefix = file_key.split("/", 1)[0]
    if prefix != str(user.organization_id):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content, content_type = await get_file(file_key)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(content=content, media_type=content_type)


@router.post("/{expense_id}/receipt", response_model=ExpenseResponse)
async def upload_receipt(
    expense_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
    org: Organization = Depends(get_tenant),
):
    expense = await _get_expense_or_404(db, expense_id)
    try:
        file_key, _file_url = await upload_expense_receipt(org_id, expense.id, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    expense.receipt_file_key = file_key
    # A newly-attached receipt can clear a receipt_required violation.
    await _refresh_policy_violations(db, expense, org)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense.receipt_uploaded",
        entity_type="expense",
        entity_id=expense.id,
        details={"file_key": file_key},
    )
    await db.commit()
    fresh = await _get_expense_or_404(db, expense.id)
    return _to_response(fresh)


# ---------------------------------------------------------------------------
# Expense register CSV export + bulk GL re-code — both literal-prefixed
# segments declared BEFORE /{expense_id} so they aren't captured as a UUID
# (mirrors the /receipt route ordering above).
# ---------------------------------------------------------------------------


@router.get("/export")
async def export_expenses(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    category: str | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    report_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Stream the filtered expense register as ``text/csv``.

    Joins ``GLAccount`` (for the GL code) and ``ExpenseReport`` (for the report
    number) with outer joins so an uncoded / unattached expense still emits a
    row. Entity-scoped; no pagination (dumps the full filtered set, mirroring
    the analytics export).

    `status` / `report_id` / `search` run through the SAME
    `_expense_list_filters` the list and the KPI rollup use, so "export what I
    am looking at" means the rows on screen. The export used to restate the
    status and report clauses inline and declare no `search` leg at all — and
    FastAPI drops an undeclared query param silently, so a CSV taken mid-search
    covered the whole status-filtered set with nothing to say it had. The
    export-only filters (`category`, the date range) stay here: they are not on
    the list surface, and the CSV is the place a period is sliced."""
    from app.services.report_export import EXPORTERS

    base = _expense_list_filters(
        apply_entity_scope(
            select(Expense, ExpenseReport.report_number, GLAccount.code)
            .outerjoin(GLAccount, GLAccount.id == Expense.gl_account_id)
            .outerjoin(ExpenseReport, ExpenseReport.id == Expense.report_id),
            Expense,
            entity_id,
        ),
        status_filter=status_filter,
        report_id=report_id,
        search=search,
    )
    if category:
        base = base.where(Expense.category == category)
    if date_from:
        base = base.where(Expense.expense_date >= date_from)
    if date_to:
        base = base.where(Expense.expense_date <= date_to)
    base = base.order_by(Expense.expense_date.desc())

    rows = (await db.execute(base)).all()
    payload = EXPORTERS["expense_register"](rows)
    filename = f"expenses_{utc_today().isoformat()}.csv"
    return Response(
        content=payload,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/bulk-gl-code", response_model=ExpenseBulkGlCodeResponse)
async def bulk_gl_code(
    body: ExpenseBulkGlCode,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Set ``gl_account_id`` on many expenses at once (``None`` clears it).

    Each id is resolved within the entity scope (an out-of-scope / cross-tenant
    id is a 404), then one ``dispatch_audit`` row is written per expense so the
    SOX trail records every coded row.

    Same partial-success contract as the sibling invoice bulk endpoints
    (``api/invoices.py::bulk_status_change``): a malformed or unresolvable
    expense id is skipped-and-reported, not a reason to roll back the whole
    batch — a batch of 500 shouldn't lose its other 499 rows because one id
    is stale."""
    gl_uuid: uuid.UUID | None = None
    if body.gl_account_id is not None:
        try:
            gl_uuid = uuid.UUID(body.gl_account_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid gl_account_id")
        gl = (
            await db.execute(
                select(GLAccount).where(
                    GLAccount.id == gl_uuid,
                    GLAccount.organization_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if gl is None:
            raise HTTPException(status_code=404, detail="GL account not found")

    updated = 0
    skipped: list[ExpenseBulkGlCodeSkip] = []
    for raw in body.expense_ids:
        try:
            eid = uuid.UUID(raw)
        except ValueError:
            skipped.append(ExpenseBulkGlCodeSkip(id=raw, reason="invalid id format"))
            continue

        expense = (
            await db.execute(
                apply_entity_scope(select(Expense), Expense, entity_id).where(Expense.id == eid)
            )
        ).scalar_one_or_none()
        if expense is None:
            skipped.append(ExpenseBulkGlCodeSkip(id=raw, reason="not found"))
            continue

        expense.gl_account_id = gl_uuid
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="expense.bulk_gl_coded",
            entity_type="expense",
            entity_id=expense.id,
            details={"gl_account_id": str(gl_uuid) if gl_uuid else None},
        )
        updated += 1

    await db.commit()
    return ExpenseBulkGlCodeResponse(updated=updated, skipped=skipped)


# ---------------------------------------------------------------------------
# Expenses — get / patch / delete
# ---------------------------------------------------------------------------


# Declared BEFORE the parametric `/{expense_id}` route so the literal `/summary`
# path isn't swallowed by `expense_id` (which would 422 on the non-UUID
# segment). FastAPI matches routes in declaration order — the same ordering
# constraint `/export` and `/bulk-gl-code` above already live under.
@router.get("/summary", response_model=ExpenseSummaryResponse)
async def expense_summary(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    report_id: uuid.UUID | None = Query(None),
    search: str | None = Query(None),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Whole-set status counts + per-currency totals for the expenses KPI row.

    Takes the SAME filters as `GET /api/expenses` and runs them through the same
    `_expense_list_filters`, so the rollup and the table always describe one set.
    The read gate matches the list's (all four roles) — a rollup exposes strictly
    less than the rows it summarises.

    Totals are grouped BY CURRENCY and serialised as exact decimal strings. They
    are never added across currencies and never FX-converted: an FX rate fetched
    on a read would make the figure non-deterministic, and a cross-currency SUM
    is a number denominated in nothing.
    """
    status_rows = (
        await db.execute(
            _expense_list_filters(
                apply_entity_scope(
                    select(Expense.status, func.count()).select_from(Expense),
                    Expense,
                    entity_id,
                ),
                status_filter=status_filter,
                report_id=report_id,
                search=search,
            ).group_by(Expense.status)
        )
    ).all()
    by_status = {str(row_status): int(n) for row_status, n in status_rows}

    # GROUP BY the UPPERCASED code, not the stored one. The write schemas
    # normalize now, but a row written before they did can still hold `usd`
    # while its neighbour holds `USD` — two group keys that the response then
    # relabelled identically, so the KPI row showed the same currency twice with
    # the money split between the two entries.
    currency_key = func.upper(Expense.currency)
    currency_rows = (
        await db.execute(
            _expense_list_filters(
                apply_entity_scope(
                    select(
                        currency_key,
                        func.coalesce(func.sum(Expense.amount), 0),
                        func.count(),
                    ).select_from(Expense),
                    Expense,
                    entity_id,
                ),
                status_filter=status_filter,
                report_id=report_id,
                search=search,
            )
            .group_by(currency_key)
            .order_by(currency_key)
        )
    ).all()

    return ExpenseSummaryResponse(
        total=sum(by_status.values()),
        by_status=by_status,
        by_currency=[
            ExpenseCurrencyTotal(
                currency=str(currency or "").upper(),
                # `Decimal` in, exact string out — the money never touches a float.
                total=str(Decimal(total_amount)),
                count=int(n),
            )
            for currency, total_amount, n in currency_rows
        ],
    )


@router.get("/{expense_id}", response_model=ExpenseResponse)
async def get_expense(
    expense_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    return _to_response(await _get_expense_or_404(db, expense_id))


@router.patch("/{expense_id}", response_model=ExpenseResponse)
async def update_expense(
    expense_id: uuid.UUID,
    body: ExpenseUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
    org: Organization = Depends(get_tenant),
):
    expense = await _get_expense_or_404(db, expense_id)
    payload = body.model_dump(exclude_unset=True)

    # gl_account_id / report_id need UUID coercion; handle them explicitly.
    affected_reports: set[uuid.UUID] = set()
    if "gl_account_id" in payload:
        raw = payload.pop("gl_account_id")
        gl_uuid: uuid.UUID | None = None
        if raw is not None:
            try:
                gl_uuid = uuid.UUID(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid gl_account_id")
        if expense.gl_account_id != gl_uuid:
            expense.gl_account_id = gl_uuid
    if "report_id" in payload:
        raw = payload.pop("report_id")
        new_report: uuid.UUID | None = None
        if raw is not None:
            try:
                new_report = uuid.UUID(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid report_id")
            await _get_report_or_404(db, new_report)
        if expense.report_id != new_report:
            if expense.report_id:
                affected_reports.add(expense.report_id)
            if new_report:
                # Moving an expense ONTO a report is an attach, and the target
                # has to gate like one. `_require_report_unlocked` (applied to
                # every affected report below) only refuses the four
                # locked-for-approval states — so this path was the one attach
                # of the three that would still add a line to a `rejected` /
                # `cancelled` report, which `POST /{id}/expenses` and
                # `POST /api/expenses` both refuse. A terminal report cannot be
                # resubmitted, so the line just disappeared onto a dead row.
                # Detaching (`report_id: null`) stays allowed from a terminal
                # report — that is how its expenses get re-reported.
                _require_draft_report(await _get_report_or_404(db, new_report))
                affected_reports.add(new_report)
            expense.report_id = new_report

    changed: list[str] = []
    for field in _EXPENSE_UPDATABLE_FIELDS:
        if field in payload and getattr(expense, field) != payload[field]:
            setattr(expense, field, payload[field])
            changed.append(field)

    # An amount / currency change ripples into the owning report's total — the
    # locked conversion describes the OLD amount+currency, so it must be re-locked.
    if ("amount" in changed or "currency" in changed) and expense.report_id:
        affected_reports.add(expense.report_id)

    # Any report whose total this edit would move (an amount change or a
    # membership change) must not be locked — otherwise the edit bypasses the
    # CFO gate / stale-signs an already-approved report (issue #155).
    for rid in affected_reports:
        _require_report_unlocked(await _get_report_or_404(db, rid))

    # Re-lock (or clear) the line's conversion whenever what it converts, or
    # what it converts INTO, changed (issue #157). Done before the recompute so
    # the report total reads the fresh figure.
    if affected_reports:
        if expense.report_id:
            await _lock_line_conversion(
                expense, await _get_report_or_404(db, expense.report_id), org
            )
        else:
            clear_expense_conversion(expense)

    await db.flush()
    for rid in affected_reports:
        report = await _get_report_or_404(db, rid)
        await _recompute_report_total(db, report)

    # Amount / category / receipt may have changed — recompute violations.
    await _refresh_policy_violations(db, expense, org)

    if changed or affected_reports:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="expense.updated",
            entity_type="expense",
            entity_id=expense.id,
            details={"fields": changed or ["report_id"]},
        )
    await db.commit()
    fresh = await _get_expense_or_404(db, expense.id)
    return _to_response(fresh)


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_expense(
    expense_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    expense = await _get_expense_or_404(db, expense_id)
    owning_report = expense.report_id
    # Deleting an expense off a locked report would silently shrink its total
    # below the total the CFO gate / approval signature ran against (issue #155).
    if owning_report:
        _require_report_unlocked(await _get_report_or_404(db, owning_report))
    # A card-reconciled expense is the target of a real FK
    # (`corporate_card_transactions.matched_expense_id`), so Postgres refused
    # the DELETE and it surfaced as an unhandled `ForeignKeyViolationError` — a
    # bare 500 on an ordinary user action. Refuse it here instead, exactly the
    # way `/corporate-card-transactions/{id}/ignore` refuses a matched
    # transaction: unmatch first. Doing the unmatch implicitly would mutate the
    # card feed as an invisible side effect of a DELETE on the other side of
    # the link; naming the transaction id makes the 409 actionable instead.
    linked_txn_id = (
        (
            await db.execute(
                select(CorporateCardTransaction.id).where(
                    CorporateCardTransaction.matched_expense_id == expense.id
                )
            )
        )
        .scalars()
        .first()
    )
    if linked_txn_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Expense is reconciled to card transaction {linked_txn_id}; "
                "unmatch it before deleting."
            ),
        )
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense.deleted",
        entity_type="expense",
        entity_id=expense.id,
        details={"amount": str(expense.amount)},
    )
    await db.delete(expense)
    await db.flush()
    if owning_report:
        report = await _get_report_or_404(db, owning_report)
        await _recompute_report_total(db, report)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Expense reports
# ---------------------------------------------------------------------------


@reports_router.get("", response_model=ExpenseReportListResponse)
async def list_reports(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    status_filter: str | None = Query(None, alias="status"),
    pagination: PaginationParams = Depends(pagination_params),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = apply_entity_scope(select(ExpenseReport), ExpenseReport, entity_id)
    if status_filter:
        base = base.where(ExpenseReport.status == status_filter)

    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0)
    # `.id` tie-breaker: see the same fix on the sibling expense list above.
    paged = (
        base.options(selectinload(ExpenseReport.expenses))
        .order_by(ExpenseReport.created_at.desc(), ExpenseReport.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(paged)).scalars().all()
    return ExpenseReportListResponse(
        items=[_report_to_response(r) for r in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@reports_router.post("", response_model=ExpenseReportResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    body: ExpenseReportCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    # The employee is always the authenticated caller — a report can't be raised
    # on someone else's behalf, and the body field is ignored for safety (the
    # same rule `expense_preapprovals.create_preapproval` states for its own
    # requester). `employee_user_id` is the ONLY value report SoD compares
    # against at approve time, so accepting it from the creator let one user
    # raise a report "for" an arbitrary uuid and then approve it themselves —
    # dual control on reimbursement gone, with no accomplice and no second role.
    employee_uuid = user.id

    report = ExpenseReport(
        report_number=body.report_number,
        title=body.title,
        employee_user_id=employee_uuid,
        currency=body.currency,
        notes=body.notes,
        total_amount=Decimal("0"),
        organization_id=org_id,
        entity_id=entity_id,
    )
    db.add(report)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense_report.created",
        entity_type="expense_report",
        entity_id=report.id,
        details={"report_number": report.report_number},
    )
    await db.commit()
    fresh = await _get_report_or_404(db, report.id)
    return _report_to_response(fresh)


@reports_router.get("/{report_id}", response_model=ExpenseReportResponse)
async def get_report(
    report_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    return _report_to_response(await _get_report_or_404(db, report_id))


@reports_router.get("/{report_id}/summary", response_model=ExpenseReportSummary)
async def report_summary(
    report_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    """Aggregate the report's attached expenses: grand total + count plus
    per-category, per-status and per-CURRENCY rollups.

    Every figure is expressed in the report's own ``currency`` using each line's
    rate-locked ``converted_amount`` — never a naive cross-currency ``SUM`` of
    ``Expense.amount`` (issue #157). A foreign line with no usable lock is
    excluded and surfaced via ``unconverted_count`` /
    ``by_currency[].unconverted_count`` so the UI can say "N lines pending
    conversion" instead of showing a number that quietly mixes dollars and
    euros. All arithmetic is ``Decimal``: the ``total`` fields stay ``Decimal``
    to the JSON boundary and the ``*_exact`` fields carry decimal strings."""
    report = await _get_report_or_404(db, report_id)
    rows = (
        await db.execute(
            select(
                Expense.id,
                Expense.amount,
                Expense.currency,
                Expense.category,
                Expense.status,
                Expense.converted_amount,
                Expense.converted_currency,
            ).where(Expense.report_id == report.id)
        )
    ).all()

    def _line(r) -> dict:
        return {
            "id": r.id,
            "amount": r.amount,
            "currency": r.currency,
            "converted_amount": r.converted_amount,
            "converted_currency": r.converted_currency,
        }

    def _grouped(group_key) -> list[tuple[object, ReportRollup]]:
        grouped: dict[object, list[dict]] = {}
        for r in rows:
            grouped.setdefault(group_key(r), []).append(_line(r))
        return [
            (key, rollup_report_lines(items, report_currency=report.currency))
            for key, items in grouped.items()
        ]

    overall = rollup_report_lines([_line(r) for r in rows], report_currency=report.currency)

    return ExpenseReportSummary(
        total=overall.total,
        total_exact=str(overall.total),
        currency=overall.currency,
        count=overall.count,
        unconverted_count=overall.unconverted_count,
        by_category=[
            {
                "category": cat,
                "count": roll.count,
                "total": json_money(roll.total),
                "total_exact": str(roll.total),
                "unconverted_count": roll.unconverted_count,
            }
            for cat, roll in _grouped(lambda r: r.category)
        ],
        by_status=[
            {
                "status": str(st),
                "count": roll.count,
                "total": json_money(roll.total),
                "total_exact": str(roll.total),
                "unconverted_count": roll.unconverted_count,
            }
            for st, roll in _grouped(lambda r: r.status)
        ],
        by_currency=[
            {
                "currency": b.currency,
                "count": b.count,
                "original_amount": str(b.original_amount),
                "report_amount": str(b.report_amount),
                "unconverted_count": b.unconverted_count,
            }
            for b in overall.by_currency
        ],
    )


@reports_router.patch("/{report_id}", response_model=ExpenseReportResponse)
async def update_report(
    report_id: uuid.UUID,
    body: ExpenseReportUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
    org: Organization = Depends(get_tenant),
):
    report = await _get_report_or_404(db, report_id)
    # Report-level fields (currency in particular) reinterpret a locked total —
    # only editable while the report isn't locked in for approval (issue #155).
    _require_report_unlocked(report)
    payload = body.model_dump(exclude_unset=True)
    changed: list[str] = []
    for field in _REPORT_UPDATABLE_FIELDS:
        if field in payload and getattr(report, field) != payload[field]:
            setattr(report, field, payload[field])
            changed.append(field)

    # Changing the report's currency re-denominates every attached line, so each
    # one gets a fresh lock into the new currency and the total is recomputed
    # (issue #157). Without this the total would keep summing figures locked
    # into the OLD currency.
    if "currency" in changed:
        for child in report.expenses:
            await _lock_line_conversion(child, report, org)
        await db.flush()
        await _recompute_report_total(db, report)

    if changed:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="expense_report.updated",
            entity_type="expense_report",
            entity_id=report.id,
            details={"fields": changed},
        )
    await db.commit()
    fresh = await _get_report_or_404(db, report.id)
    return _report_to_response(fresh)


@reports_router.post("/{report_id}/expenses", response_model=ExpenseReportResponse)
async def attach_expenses(
    report_id: uuid.UUID,
    body: ExpenseReportAttach,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
    org: Organization = Depends(get_tenant),
):
    """Attach (or detach) expenses on a report and recompute its total.

    Tenant isolation comes from the per-tenant DB session — each id is looked
    up in this tenant's ``expenses`` table; an unknown / cross-tenant id is a
    404. Detaching nulls ``report_id`` (the expense outlives the report).

    A line in a different currency from the report is NOT rejected — one trip
    legitimately spans currencies — but it is converted at a rate locked onto
    the row here, so the report's total is a real figure in the report's
    currency instead of a nonsense cross-currency sum (issue #157). A line we
    cannot convert is refused (422) rather than attached at face value."""
    report = await _get_report_or_404(db, report_id)
    # The target report's composition can only change while it's a draft.
    _require_draft_report(report)

    expense_uuids: list[uuid.UUID] = []
    for raw in body.expense_ids:
        try:
            expense_uuids.append(uuid.UUID(raw))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid expense id: {raw}")

    # Track every *other* report an expense is moving off of, so its
    # total_amount is recomputed too — otherwise reassigning an expense from
    # report A to report B leaves A's Numeric total stale (matches the
    # affected-reports handling in update_expense).
    affected_reports: set[uuid.UUID] = set()
    attached: list[Expense] = []
    for eid in expense_uuids:
        expense = await _get_expense_or_404(db, eid)
        if body.detach:
            if expense.report_id == report.id:
                expense.report_id = None
                # The lock is an expression in THIS report's currency; once the
                # line leaves, it no longer describes anything.
                clear_expense_conversion(expense)
        else:
            if expense.report_id and expense.report_id != report.id:
                affected_reports.add(expense.report_id)
            expense.report_id = report.id
            attached.append(expense)

    # Moving an expense off a locked report would silently drop its total —
    # block that too (the source report also loses composition).
    for rid in affected_reports:
        _require_report_unlocked(await _get_report_or_404(db, rid))

    # Lock each newly-attached line into the report's currency before totalling.
    for expense in attached:
        await _lock_line_conversion(expense, report, org)

    await db.flush()
    await _recompute_report_total(db, report)
    for rid in affected_reports:
        other = await _get_report_or_404(db, rid)
        await _recompute_report_total(db, other)

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense_report.expenses_attached",
        entity_type="expense_report",
        entity_id=report.id,
        details={"count": len(expense_uuids), "detach": body.detach},
    )
    await db.commit()
    fresh = await _get_report_or_404(db, report.id)
    return _report_to_response(fresh)


# ---------------------------------------------------------------------------
# Report approval workflow (WF3) — submit / approve / reject
#
# Allowed source→target transitions are declared explicitly; an invalid source
# status is a 422 (never a silent no-op). Submit gates on BLOCKING policy
# violations (missing required receipt, absent required pre-approval). Approve
# enforces segregation of duties (approver ≠ submitting employee) and a
# CFO-threshold role gate. Every transition is audited.
# ---------------------------------------------------------------------------

# Platform default CFO threshold; per-org override in
# Organization.settings.expense_approval.cfo_threshold.
_DEFAULT_CFO_THRESHOLD = Decimal("5000")


@reports_router.post("/{report_id}/submit", response_model=ExpenseReportResponse)
async def submit_report(
    report_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK)),
    org_id: uuid.UUID = Depends(get_org_id),
    org: Organization = Depends(get_tenant),
):
    """Submit a draft report for approval: ``draft → submitted``.

    Runs the policy engine over the report's expenses first; if any BLOCKING
    violation is present (missing required receipt, or a required pre-approval
    that is absent) the submission is rejected with 422 and the violation list,
    and the report does NOT transition. On success the report is stamped
    ``submitted_at`` and every child expense moves to ``submitted``.

    Two currency guards run here (issue #157): the total is re-derived and the
    submission refused if any attached line has no usable conversion into the
    report's currency (a legacy row predating the locked-FX columns), and the
    total is then locked into the ORG REPORTING currency so the CFO gate at
    approval time compares a figure fixed at submission."""
    report = await _get_report_or_404(db, report_id)
    if report.status != ExpenseReportStatus.draft:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot submit a report in '{report.status}' state",
        )

    # Fail closed on an un-summable line rather than submitting an understated
    # total into the approval chain.
    rollup = await _recompute_report_total(db, report)
    if rollup.unconverted_count:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": (
                    f"{rollup.unconverted_count} expense(s) have no exchange rate locked "
                    f"into the report currency {rollup.currency} and cannot be totalled. "
                    "Re-attach them to lock a rate."
                ),
                "expense_ids": list(rollup.unconverted_ids),
            },
        )

    cover: dict[str, Decimal] = {}
    for child in report.expenses:
        amount = await _approved_preapproval_amount(db, child)
        if amount is not None:
            cover[str(child.id)] = amount

    # Policies are per-entity, so each line is judged against ITS OWN entity's
    # rule set — `evaluate_report` applies every policy handed to it to every
    # expense, so one shared list would re-introduce the cross-entity bleed at
    # the report level. A report is normally single-entity (one pass); a mixed
    # one gets one pass per entity rather than letting one subsidiary's table
    # govern another's line.
    lines_by_entity: dict[uuid.UUID | None, list[Expense]] = {}
    for child in report.expenses:
        lines_by_entity.setdefault(child.entity_id, []).append(child)

    reporting_currency_for_policies = resolve_reporting_currency(org.settings)
    violations: list[dict] = []
    for line_entity_id, group in lines_by_entity.items():
        violations.extend(
            evaluate_report(
                report,
                group,
                await _active_policies(db, line_entity_id),
                preapproval_amount_by_expense=cover,
                default_threshold_currency=reporting_currency_for_policies,
            )
        )
    blocking = blocking_violations(violations)
    if blocking:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Report has blocking policy violations and cannot be submitted.",
                "violations": blocking,
            },
        )

    report.status = ExpenseReportStatus.submitted
    report.submitted_at = datetime.now(UTC)
    for child in report.expenses:
        child.status = ExpenseStatus.submitted

    # Lock the total into the org reporting currency — the figure the CFO gate
    # reads at approval. Best-effort: an FX outage leaves it NULL and the gate
    # then fails CLOSED (CFO required), so a failure here never lets a report
    # through with less scrutiny.
    reporting_currency = resolve_reporting_currency(org.settings)
    submit_fx_adapter = _fx_adapter_for(org)
    if conversion_requires_rate_source(
        source_currency=report.currency,
        target_currency=reporting_currency,
        fx_adapter=submit_fx_adapter,
    ):
        # No usable rate source for a pair that genuinely needs one is the same
        # outcome as an FX outage, which `lock_report_reporting_amount` already
        # models: clear the figure and let `report_amount_for_gate` treat the
        # missing number as OVER the threshold, so the CFO gate fails closed.
        clear_report_reporting_amount(report)
    else:
        # Same pair-first rule as `_lock_line_conversion`, shared rather than
        # restated. A report already filed in the org's reporting currency locks
        # at rate 1 with no provider call, so a tenant whose `settings.fx.provider`
        # names no registered adapter now gets a complete stored snapshot instead
        # of NULL columns and a `reporting_total: null` in this endpoint's own
        # audit row. Nothing about the gate changes — `report_amount_for_gate`
        # already fell back to `total_amount` for that pair — this closes the
        # hole in what is RECORDED.
        await lock_report_reporting_amount(
            report, reporting_currency=reporting_currency, fx_adapter=submit_fx_adapter
        )

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense_report.submitted",
        entity_type="expense_report",
        entity_id=report.id,
        details={
            "total": str(report.total_amount),
            "currency": normalize_currency(report.currency),
            "reporting_total": (
                str(report.reporting_amount) if report.reporting_amount is not None else None
            ),
            "reporting_currency": report.reporting_currency,
            "expense_count": len(report.expenses),
        },
    )
    await db.commit()
    fresh = await _get_report_or_404(db, report.id)
    return _report_to_response(fresh)


@reports_router.post("/{report_id}/approve", response_model=ExpenseReportResponse)
async def approve_report(
    report_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
    org: Organization = Depends(get_tenant),
):
    """Approve a submitted report: ``submitted → approved``.

    Segregation of duties: the approver must differ from the report's submitting
    employee (reuses ``check_segregation`` → 403). CFO gate: when the report
    total exceeds ``Organization.settings.expense_approval.cfo_threshold``
    (default ``5000``), only ``cfo`` / ``admin`` may approve. On success the
    report is stamped ``approved_at`` / ``approved_by`` and every child expense
    moves to ``approved``."""
    report = await _get_report_or_404(db, report_id)
    if report.status != ExpenseReportStatus.submitted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot approve a report in '{report.status}' state",
        )

    # SoD — approver ≠ submitting employee. Reuse the invoice helper via a tiny
    # attribute shim so the rule + 403 detail stay shared with the invoice path.
    check_segregation(
        SimpleNamespace(
            uploaded_by_id=report.employee_user_id,
            # No editor-tracking column on this table, so there is no second
            # implicated actor to name. Stated rather than left absent: a
            # missing attribute on a fraud control reads as an oversight, and
            # `violates_segregation`'s getattr default would silently supply it.
            segregation_actor_ids=None,
        ),
        user.id,
        {"require_segregation": True},
    )

    # CFO-threshold role gate (Decimal math, never float). `cfo_gate_applies` is
    # the shared fail-CLOSED parse: a malformed `cfo_threshold` demands CFO/admin
    # sign-off rather than raising an InvalidOperation that would 500 the approval.
    from app.services.approval_chain import _to_decimal, cfo_gate_applies

    expense_cfg = (org.settings or {}).get("expense_approval") or {}
    cfo_threshold_raw = expense_cfg.get("cfo_threshold", _DEFAULT_CFO_THRESHOLD)

    # The threshold is a bare number denominated in the ORG REPORTING currency,
    # so the comparison uses the report total expressed in that currency — the
    # figure locked at submit — not the report's own-currency total (issue
    # #157). Without this a 4 900 EUR report slips under a 5 000 USD threshold.
    # `None` means we could not establish it → fail CLOSED (gate applies).
    reporting_currency = resolve_reporting_currency(org.settings)
    gate_total = report_amount_for_gate(report, reporting_currency=reporting_currency)
    gate_applies = gate_total is None or cfo_gate_applies(cfo_threshold_raw, gate_total)
    if gate_applies:
        held = {r.name for r in user.roles} if user.roles else set()
        if ROLE_CFO not in held and ROLE_ADMIN not in held:
            threshold_dec = _to_decimal(cfo_threshold_raw)
            limit = f"{threshold_dec}" if threshold_dec is not None else "the configured limit"
            if gate_total is None:
                detail = (
                    f"Report total cannot be expressed in {reporting_currency} "
                    f"(no rate from {normalize_currency(report.currency)}), so it cannot be "
                    f"cleared against the {limit} limit. CFO approval required."
                )
            else:
                detail = (
                    f"Report total {gate_total} {reporting_currency} exceeds {limit}. "
                    "CFO approval required."
                )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

    report.status = ExpenseReportStatus.approved
    report.approved_at = datetime.now(UTC)
    report.approved_by = user.id
    for child in report.expenses:
        child.status = ExpenseStatus.approved

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense_report.approved",
        entity_type="expense_report",
        entity_id=report.id,
        details={
            "total": str(report.total_amount),
            "currency": normalize_currency(report.currency),
            # The exact figure the CFO gate compared, so an auditor can replay
            # the decision without re-deriving an FX rate.
            "gate_total": str(gate_total) if gate_total is not None else None,
            "gate_currency": reporting_currency,
        },
    )
    await db.commit()
    fresh = await _get_report_or_404(db, report.id)
    return _report_to_response(fresh)


@reports_router.post("/{report_id}/reject", response_model=ExpenseReportResponse)
async def reject_report(
    report_id: uuid.UUID,
    body: ExpenseReportDecision | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Reject a submitted report: ``submitted → rejected``.

    The child expenses are returned to ``draft`` so they can be corrected and
    re-reported. ``rejected`` is terminal for this report row."""
    report = await _get_report_or_404(db, report_id)
    if report.status != ExpenseReportStatus.submitted:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot reject a report in '{report.status}' state",
        )

    report.status = ExpenseReportStatus.rejected
    for child in report.expenses:
        child.status = ExpenseStatus.draft

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="expense_report.rejected",
        entity_type="expense_report",
        entity_id=report.id,
        details={"reason": body.reason} if body and body.reason else None,
    )
    await db.commit()
    fresh = await _get_report_or_404(db, report.id)
    return _report_to_response(fresh)
