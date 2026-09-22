"""Payment endpoints."""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import case, exists, func, not_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_permission,
    require_roles,
)
from app.api.money_filters import snap_lower_bound, snap_upper_bound
from app.api.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_SELECT_ALL_IDS,
    PaginationParams,
    pagination_params,
)
from app.api.permissions import (
    PERM_PAYMENT_EXECUTE,
    PERM_PAYMENT_RUN_APPROVE,
    PERM_PAYMENT_VOID,
)
from app.api.sorting import SortParams, resolve_order_by, sort_params
from app.models.credit_memo import CreditMemo
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun, PaymentSchedule
from app.models.user import User
from app.models.vendor import Vendor
from app.models.virtual_card import CardRebate, VirtualCard
from app.schemas.payment import (
    PaymentCreate,
    PaymentListResponse,
    PaymentResponse,
    PaymentRunListResponse,
    PaymentRunResponse,
)
from app.services.audit_access import log_access
from app.services.card_issuance import card_cancel_disposition
from app.services.currency_conversion import (
    card_currency_sql,
    invoice_reporting_amount_sql,
    payment_reporting_amount_sql,
    reporting_amount_at_locked_rate,
    resolve_reporting_currency,
)
from app.services.exception_lifecycle import record_decision
from app.services.international_payments import (
    is_international_payment,
    normalize_currency_code,
    resolve_home_currency,
)
from app.services.payment_adapters import (
    PaymentAdapter,
    PaymentPayload,
    PaymentStatus,
    UnknownPaymentProviderError,
    get_payment_adapter,
)
from app.services.payment_controls import (
    CFO_REASON_AMOUNT_NOT_EXPRESSIBLE,
    CFO_REASON_THRESHOLD_UNPARSEABLE,
    cfo_approval_decision,
    check_run_segregation,
)
from app.services.payment_runs import (
    CARD_CLAIM_ONLY_METHOD,
    PaymentRunItemInput,
    active_run_payments,
    blocked_invoice_ids,
    blocking_exception_types,
    card_claimed_invoice_ids,
    create_payment_run_for_invoices,
    derive_run_status,
    is_retry_safe,
    net_payable_amount,
    one_currency,
    recompute_run_status,
    rollup_payment_statuses,
    run_refusal_reasons,
    superseded_payment_ids,
)
from app.services.payment_settlement import settlement_coverage
from app.services.payment_settlement_record import (
    SettlementCompletion,
    open_settlement_mismatch_exception,
    record_completion,
)
from app.services.workflow_engine import VALID_TRANSITIONS, transition_invoice
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.dates import utc_today
from app.utils.http import content_disposition_attachment
from app.utils.search import ilike_contains
from app.utils.tenant_urls import tenant_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["payments"])

# The invoice statuses a payment may be created against — i.e. an invoice that
# has cleared AP approval and can directly transition to `payment_scheduled`.
# `sent_to_erp` is excluded (mid-flight ERP push must reach `posted_in_erp`
# first); `new`/`pending`/`ready_for_review`/`rejected`/`failed` are pre-approval
# and must never have money scheduled against them. This is the single source of
# truth shared by the queue, the run builder, and the standalone payment record,
# so a payment can't be booked against an unapproved invoice on any path.
PAYABLE_INVOICE_STATUSES = (
    InvoiceStatus.approved.value,
    InvoiceStatus.posted_in_erp.value,
    InvoiceStatus.payment_scheduled.value,
)

# Of those, the ones that still need the `→ payment_scheduled` transition once a
# payment settles. DERIVED from the state machine rather than restated, so it
# can never name a status `validate_transition` would refuse: a payable status
# is schedulable exactly when `payment_scheduled` is a legal successor.
# (`payment_scheduled` itself is payable — a re-attempt — but is already there,
# and drops out here because it isn't its own successor.) The three dispatch
# legs used to spell this out as a literal that included `sent_to_erp`, which
# `VALID_TRANSITIONS` does NOT allow — and the transition runs AFTER the
# processor accepted the order, so the resulting 409 recorded a settled payment
# as `failed`. `_execute_single_payment` now refuses a non-payable invoice
# before the adapter call; this keeps the two facts from drifting apart again.
SCHEDULABLE_INVOICE_STATUSES = tuple(
    value
    for value in PAYABLE_INVOICE_STATUSES
    if InvoiceStatus.payment_scheduled in VALID_TRANSITIONS.get(InvoiceStatus(value), set())
)

# Exception classes that block an invoice from entering a payment run while
# UNRESOLVED (`open`/`escalated`). Every one of them is an `error`-severity
# financial-integrity flag that approval does NOT gate on — nothing in
# `services/review.py` or `workflow_engine.py` reads warning severity — so
# without this gate each could be approved straight past and paid:
#
#   duplicate            — the same invoice paid a second time
#   fraud_flag           — bank-detail swap, rush payment, stat anomaly, an
#                          altered/never-issued cheque from a Positive Pay return
#   line_total_mismatch  — the header `amount` a run pays openly disagrees with
#                          the invoice's own line items (the header is never
#                          silently recomputed from them — see
#                          `docs/line-total-reconciliation.md`), so paying it
#                          would pay a total the lines don't support
#   payment_reconciliation — the backstop reconciler gave up waiting on a
#                          `submitted` payment past its max age and marked it
#                          `failed`. `failed` is in
#                          `LIVE_PAYMENT_TERMINAL_STATUSES`, so that row stops
#                          holding the invoice's live-payment slot even though
#                          real money may still be in flight at the rail. The
#                          exception is what stops a fresh run paying the same
#                          invoice a second time until a human has reconciled
#                          the rail (see `services/payment_reconciler.py`).
#
# Resolving/dismissing the exception is the human sign-off that clears it.
PAYMENT_BLOCKING_EXCEPTION_TYPES = (
    "duplicate",
    "fraud_flag",
    "line_total_mismatch",
    "payment_reconciliation",
)

# Terminal payment states — a payment in one of these no longer represents a
# LIVE claim on its invoice, so the "one live payment per invoice" idempotency
# invariant (both the app-level guard and the `uq_payments_one_live_per_invoice`
# partial index) excludes them. A void hands the invoice back to `approved` to
# be re-paid; a failed / cancelled attempt must not block a fresh one.
LIVE_PAYMENT_TERMINAL_STATUSES = ("voided", "failed", "cancelled")


def _require_payment_adapter(org: Organization) -> PaymentAdapter:
    """Resolve the org's payment processor, or refuse before anything moves.

    `get_payment_adapter` fails closed on a provider name it has no adapter
    for (see its docstring — the old `mock` fallback reported every payment
    as settled without moving money). Every money-moving entry point resolves
    through here FIRST, so the refusal lands as an actionable 409 with the
    run still in `draft` and no payment dispatched, rather than as a 500 with
    the run stranded `executing`.
    """
    try:
        return get_payment_adapter((org.settings or {}).get("payments") or {})
    except UnknownPaymentProviderError as exc:
        # The provider name is the org's own admin-entered settings value and
        # is bounded by the exception — echoing it is what makes the error
        # actionable ("you typed modern-treasury"). No credential is included.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Payment processor '{exc.provider}' is not a supported provider, so "
                "nothing was dispatched. Fix Settings → Payments before executing."
            ),
        ) from exc


async def _find_live_payment(db: AsyncSession, invoice_id: uuid.UUID) -> Payment | None:
    """Return the oldest non-terminal (LIVE) payment for an invoice, or None.

    Backs the standalone-payment idempotency guard: a live payment is any that
    isn't in LIVE_PAYMENT_TERMINAL_STATUSES. Deterministically returns the
    earliest such row so a double-POST always resolves to the same payment.
    """
    result = await db.execute(
        select(Payment)
        .where(
            Payment.invoice_id == invoice_id,
            Payment.status.notin_(LIVE_PAYMENT_TERMINAL_STATUSES),
        )
        .order_by(Payment.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_scoped_payment(
    db: AsyncSession,
    payment_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    *,
    for_update: bool = False,
) -> Payment:
    """Fetch one `Payment` **within the caller's selected entity**, or 404.

    Multi-entity Phase 2 scopes reads/writes by the `X-Entity-ID` header, and
    the list / queue / summary / counts endpoints all honour it — but every
    by-id detail and mutation route used to resolve the row on `Payment.id`
    alone. Inside one tenant that let a user with subsidiary A selected void,
    release, or read subsidiary B's payment simply by knowing its id: the
    entity selector became advisory on exactly the routes that move money.

    Mirrors `api/positive_pay.py::_get_scoped_file` on the sibling treasury
    router, including its **opaque 404** — an out-of-scope id is
    indistinguishable from one that doesn't exist, so the response can't be
    used to enumerate another entity's payments. `for_update` keeps the
    existing `SELECT ... FOR UPDATE` row locks on the mutating callers (the
    scope predicate is just another WHERE clause; the lock is unchanged).
    """
    query = apply_entity_scope(select(Payment).where(Payment.id == payment_id), Payment, entity_id)
    if for_update:
        query = query.with_for_update()
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return row


async def _get_scoped_run(
    db: AsyncSession,
    run_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    *,
    for_update: bool = False,
) -> PaymentRun:
    """Fetch one `PaymentRun` within the caller's selected entity, or 404.

    Same rationale (and the same opaque 404) as `_get_scoped_payment`:
    `GET /runs/` is entity-scoped and `POST /runs` stamps the write entity, so
    a run detail / approve / cancel / execute / resume that resolved on
    `PaymentRun.id` alone let one subsidiary's operator CFO-approve and execute
    another subsidiary's run.
    """
    query = apply_entity_scope(
        select(PaymentRun).where(PaymentRun.id == run_id), PaymentRun, entity_id
    )
    if for_update:
        query = query.with_for_update()
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Payment run not found")
    return row


# ── Individual Payments ──────────────────────────────────────────────

# `sort=` allowlist for `GET /payments` — see `api/sorting.py`. `.id` is
# always appended as the final tie-break regardless of which column is
# picked (mirrors the pre-existing `created_at, id` default order below).
PAYMENT_SORTABLE_COLUMNS: dict[str, object] = {
    "created_at": Payment.created_at,
    "amount": Payment.amount,
    "status": Payment.status,
    "method": Payment.method,
}


def _payment_list_filters(
    query,
    *,
    entity_id: uuid.UUID | None,
    status: str | None,
    method: str | None,
    invoice_id: uuid.UUID | None,
    search: str | None,
    amount_min: Decimal | None,
    amount_max: Decimal | None,
    invoice_joined: bool = False,
):
    """Apply the payment-list population filters to ``query``.

    Shared by ``GET /api/payments`` (both its row query and its total), and by
    ``GET /api/payments/counts``, so the History-tab chips describe exactly the
    rows the list would return. ``/counts`` previously declared no parameters at
    all and grouped over the whole entity-scoped set, so a search for one vendor
    left the chips reading the tenant's total over a one-row table — the same
    defect `invoices.py::invoice_counts` closed, on a sibling surface. The list
    endpoint also restated its whole filter block twice (once for the rows, once
    for the fan-out-free count), which is the drift this builder removes.

    ``invoice_joined`` says whether the caller already joined ``Invoice``: the
    row query does (it selects from it), while the count and the tallies select
    from ``Payment`` alone and need the join added for the search leg only.
    Joining unconditionally would fan the count out.

    ``status`` is a normal filter here, but ``/counts`` passes ``None`` for it:
    status is the dimension being tallied, so applying it would zero every other
    chip (`invoices.py::invoice_counts` states the same rule).
    """
    query = apply_entity_scope(query, Payment, entity_id)
    if status:
        statuses = [s.strip() for s in status.split(",")]
        query = query.where(Payment.status.in_(statuses))
    if method:
        query = query.where(Payment.method == method)
    if invoice_id:
        query = query.where(Payment.invoice_id == invoice_id)
    # `Decimal`, not `float`: a bound routed through a float is rounded to the
    # nearest double before any code here sees it. Snapped onto the column's own
    # 2dp grid in the direction of the comparison because SQLAlchemy casts the
    # bind parameter to `NUMERIC(15, 2)`, which would otherwise round an
    # over-precise bound back onto the boundary row it was written to exclude.
    # See `api/money_filters` (project invariant: money is exact).
    if amount_min is not None:
        query = query.where(Payment.amount >= snap_lower_bound(amount_min, Payment.amount))
    if amount_max is not None:
        query = query.where(Payment.amount <= snap_upper_bound(amount_max, Payment.amount))
    if search:
        if not invoice_joined:
            query = query.outerjoin(Invoice, Payment.invoice_id == Invoice.id)
        query = query.where(
            ilike_contains(Invoice.vendor_name, search)
            | ilike_contains(Invoice.invoice_number, search)
            | ilike_contains(Payment.reference, search)
        )
    return query


@router.get("", response_model=PaymentListResponse)
async def list_payments(
    pagination: PaginationParams = Depends(pagination_params),
    status_filter: str | None = Query(None, alias="status"),
    method: str | None = None,
    invoice_id: uuid.UUID | None = None,
    search: str | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    sort: SortParams = Depends(sort_params),
    db: AsyncSession = Depends(get_tenant_db),
    # `payment.execute` OR `payment.void`: the History-tab list a custom-role
    # holder of either needs to REACH the row they'd act on (void a payment, or
    # just see what a run already executed). Exact match to the prior role set:
    # ADMIN holds both by default, AP_MANAGER holds execute-only, CFO holds
    # both; AP_CLERK holds neither — so this reproduces
    # `require_roles(ADMIN, AP_MANAGER, CFO)` exactly for the four system
    # roles and additionally opens it to a custom role granted only one of
    # the two.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    _filters = {
        "entity_id": entity_id,
        "status": status_filter,
        "method": method,
        "invoice_id": invoice_id,
        "search": search,
        "amount_min": amount_min,
        "amount_max": amount_max,
    }
    query = _payment_list_filters(
        select(Payment, Invoice, VirtualCard)
        .outerjoin(Invoice, Payment.invoice_id == Invoice.id)
        .outerjoin(VirtualCard, VirtualCard.payment_id == Payment.id),
        invoice_joined=True,
        **_filters,
    )

    # Count against a plain Payment select — the row query's joins would inflate
    # it via fan-out — but through the SAME builder, so the two cannot drift.
    count_base = _payment_list_filters(select(Payment), **_filters)

    total_q = select(func.count()).select_from(count_base.subquery())
    total = (await db.execute(total_q)).scalar() or 0

    # Paginate. `.id` tie-breaker: bulk-created rows (a payment run) can share
    # `created_at` down to the microsecond, so without it Postgres can order
    # them differently between pages — a row duplicated onto two pages or
    # skipped entirely. `sort=`/`order=` (validated against
    # `PAYMENT_SORTABLE_COLUMNS`) override the default when supplied.
    order_by = resolve_order_by(
        sort,
        PAYMENT_SORTABLE_COLUMNS,
        id_column=Payment.id,
        default=[Payment.created_at.desc(), Payment.id.desc()],
    )
    query = query.order_by(*order_by)
    query = query.offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(query)
    rows = result.all()

    return PaymentListResponse(
        items=[PaymentResponse.from_db(p, inv, card) for p, inv, card in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


# NOTE: /queue and /summary MUST be declared before the /{payment_id} route —
# FastAPI matches paths in declaration order, and "queue"/"summary" otherwise
# get parsed as a UUID and fail with a 422 before ever reaching the handler.


# Match the workflow state machine: only statuses that can directly transition
# to ``payment_scheduled`` belong in the queue. ``sent_to_erp`` is excluded —
# that row is mid-flight in the ERP push and must reach ``posted_in_erp`` (via
# the ERP-confirmation webhook) before a payment can be scheduled against it.
_QUEUE_ORDER = (Invoice.due_date.asc().nulls_last(), Invoice.id.asc())


def _queue_blocking_exists():
    """SQL EXISTS predicate — this invoice carries an unresolved
    payment-blocking exception, the SAME condition
    ``services/payment_runs.blocking_exception_types`` resolves. Expressed for a
    query so the whole-set aggregates and ``/queue/ids`` don't have to stream
    every id into Python. Correlated on ``Invoice.id``."""
    return exists(
        select(1).where(
            APException.invoice_id == Invoice.id,
            APException.exception_type.in_(PAYMENT_BLOCKING_EXCEPTION_TYPES),
            APException.status.notin_(("resolved", "dismissed")),
        )
    )


def _live_payment_invoice_ids():
    """Sub-query: invoice ids already claimed by a LIVE payment — anything not
    in ``LIVE_PAYMENT_TERMINAL_STATUSES``, the SAME definition the run builder's
    ``uq_payments_one_live_per_invoice`` guard (``_live_payment_invoice_numbers``)
    uses.

    The queue used to exclude only ``Payment.status == "completed"``, so an
    invoice with a ``submitted`` / ``processing`` / ``pending`` payment (every
    real rail — ACH settles in 1-3 days) was a selectable queue row that
    ``create_payment_run_for_invoices`` then hard-409'd on the unique index,
    taking the whole select-all batch down with no way to bisect. The queue's
    invariant — *"the queue can never offer a row the run builder then
    refuses"* — requires the two use one predicate.
    """
    return (
        select(Payment.invoice_id)
        .where(Payment.status.notin_(LIVE_PAYMENT_TERMINAL_STATUSES))
        .scalar_subquery()
    )


def _queue_live_card_exists():
    """SQL EXISTS — a live virtual card already claims this invoice, the SAME
    predicate ``services/card_issuance.live_card_invoice_ids`` resolves
    (``status <> 'cancelled'``, i.e. ``uq_virtual_cards_one_live_per_invoice``).

    NOT a block: the run builder refuses a card-claimed invoice on every rail
    EXCEPT ``virtual_card``, which converges onto the existing card rather than
    opening a second outflow. So the row stays OFFERED (with
    ``required_method``) and is only kept out of the set "select all matching"
    resolves, which would stage it on the default rail and 409 the batch.
    """
    return exists(
        select(1).where(
            VirtualCard.invoice_id == Invoice.id,
            VirtualCard.status != "cancelled",
        )
    )


def _queue_fully_credited():
    """SQL predicate — applied credit memos cover the whole invoice, so a run
    would have nothing to move. Mirrors
    ``services/payment_runs.net_payable_amounts`` (``amount`` minus the sum of
    ``applied`` memos), the figure the run builder refuses on.
    """
    applied = (
        select(func.coalesce(func.sum(CreditMemo.amount), 0))
        .where(CreditMemo.invoice_id == Invoice.id, CreditMemo.status == "applied")
        .scalar_subquery()
    )
    return (func.coalesce(Invoice.amount, 0) - applied) <= 0


def _queue_base_where() -> list:
    """What makes an invoice a queue ROW at all: payable, and not already being
    paid. Rows a run would REFUSE are still listed (marked `blocked`) — an
    operator has to see what to go clear — except a live payment, which is not a
    refusal to act on but a payment already in flight."""
    return [
        Invoice.status.in_(PAYABLE_INVOICE_STATUSES),
        Invoice.id.notin_(_live_payment_invoice_ids()),
    ]


def _queue_selectable_where() -> list:
    """Base, minus every row `POST /api/payments/runs` would refuse on the
    DEFAULT rail — the set "select all N matching" resolves, and the set the
    `selectable_total` KPI counts.

    Each clause is the SQL form of one `services/payment_runs.run_refusal_reasons`
    verdict, which is what the per-row `blocked` / `required_method` flags come
    from; `tests/test_payment_queue_blocked.py` compares the two directly rather
    than trusting the restatement. Expressed as SQL because the whole-set
    aggregates and `/queue/ids` must not stream every invoice id into Python.
    """
    return [
        *_queue_base_where(),
        not_(_queue_blocking_exists()),
        not_(_queue_fully_credited()),
        not_(_queue_live_card_exists()),
    ]


def _queue_blocked_on_every_rail():
    """SQL form of the per-row ``blocked`` flag — a refusal no rail escapes.

    Deliberately NOT ``NOT _queue_selectable_where()``: the card claim leaves the
    selectable set (bulk-selecting it would stage the default rail) while leaving
    the row perfectly payable, so subtracting would count a working checkbox as
    something to go and clear.

    The card clause joins this only when ``CARD_CLAIM_ONLY_METHOD`` is ``None`` —
    i.e. a second converging rail was added and the queue can no longer name one
    — which is exactly when ``run_refusal_reasons`` stops setting ``only_method``
    and the per-row flag flips to blocked. Derived from the same constant, so the
    count and the flag cannot disagree.
    """
    clauses = [_queue_blocking_exists(), _queue_fully_credited()]
    if CARD_CLAIM_ONLY_METHOD is None:
        clauses.append(_queue_live_card_exists())
    return or_(*clauses)


async def _payment_queue_rollup(
    db: AsyncSession,
    *,
    entity_id: uuid.UUID | None,
    reporting_currency: str,
    today,
    selectable_only: bool,
) -> dict:
    """Whole-set (not one page) aggregates for the payment queue.

    Grouped by the invoice's own currency and rolled to the org reporting
    currency **in SQL** — the CASE expressions mirror
    ``currency_conversion.reporting_amount_for_row`` exactly as ``api/dashboard``
    does — so a paginated queue never loses its KPI-bar totals or its
    blocked / early-pay-savings banners to "just this page". ``selectable_only``
    excludes rows a payment run would refuse (the set "select all matching"
    resolves).
    """
    # The reporting-amount CASE comes from `currency_conversion`, the one owner
    # of "is this row locked to the reporting currency?", rather than being
    # restated here. The inline copy this replaces tested the lock as
    # `reporting_amount IS NOT NULL AND upper(reporting_currency) = tgt` and
    # left the NULL-currency case to three-valued logic: for a row carrying a
    # `reporting_amount` but NO `reporting_currency`, `has_lock` is NULL, so
    # `NOT has_lock` is NULL too and the row fell through the `unconverted`
    # CASE — counted as CONVERTED while its money came from the unlocked face
    # `amount`. A figure claiming a currency lock it does not have, on the
    # payment queue's own rollup. The shared builder states
    # `reporting_currency IS NOT NULL` explicitly, and matches
    # `reporting_amount_for_row`, which requires both columns.
    conv = invoice_reporting_amount_sql(
        reporting_currency=reporting_currency,
        amount=Invoice.amount,
        currency=Invoice.currency,
        persisted_reporting_amount=Invoice.reporting_amount,
        persisted_reporting_currency=Invoice.reporting_currency,
    )
    cur_key = conv.currency_key
    rep_expr = conv.amount
    unconv_expr = conv.unconverted

    where = _queue_selectable_where() if selectable_only else _queue_base_where()

    # Invoice-only aggregate — no schedule join, so no row fan-out on the
    # money sums / count.
    inv_agg = apply_entity_scope(
        select(
            cur_key.label("cur"),
            func.count(Invoice.id).label("cnt"),
            func.coalesce(func.sum(Invoice.amount), 0).label("amt"),
            func.coalesce(func.sum(rep_expr), 0).label("rep"),
            func.coalesce(func.sum(unconv_expr), 0).label("unconv"),
        )
        .select_from(Invoice)
        .where(*where)
        .group_by(cur_key),
        Invoice,
        entity_id,
    )

    # Early-pay savings — a separate INNER-join aggregate restricted to rows
    # with a live discount window (`discount_date` in the future + a percent
    # set). Rounded per row then summed, matching the per-row `discount_amount`
    # the queue endpoint renders. Postgres' `round(numeric, 2)` is
    # half-away-from-zero; the Python side passes `ROUND_HALF_UP` explicitly to
    # match it, because `Decimal.quantize`'s default (ROUND_HALF_EVEN) made the
    # two disagree on a genuine half-cent.
    disc_where = [
        *where,
        PaymentSchedule.discount_date.isnot(None),
        PaymentSchedule.discount_date >= today,
        PaymentSchedule.discount_percent.isnot(None),
    ]
    disc_agg = apply_entity_scope(
        select(
            cur_key.label("cur"),
            func.coalesce(
                func.sum(func.round(Invoice.amount * PaymentSchedule.discount_percent / 100, 2)),
                0,
            ).label("save"),
            func.coalesce(
                func.sum(func.round(rep_expr * PaymentSchedule.discount_percent / 100, 2)), 0
            ).label("save_rep"),
        )
        .select_from(Invoice)
        .join(PaymentSchedule, PaymentSchedule.invoice_id == Invoice.id)
        .where(*disc_where)
        .group_by(cur_key),
        Invoice,
        entity_id,
    )

    inv_rows = (await db.execute(inv_agg)).all()
    disc_rows = {r.cur: r for r in (await db.execute(disc_agg)).all()}

    q2 = Decimal("0.01")
    total = 0
    total_amount = Decimal("0")
    total_savings = Decimal("0")
    unconverted_count = 0
    by_currency: list[dict] = []
    for cur, cnt, amt, rep, unconv in inv_rows:
        d = disc_rows.get(cur)
        save = Decimal(str(d.save)) if d is not None else Decimal("0")
        save_rep = Decimal(str(d.save_rep)) if d is not None else Decimal("0")
        total += int(cnt)
        total_amount += Decimal(str(rep))
        total_savings += save_rep
        unconverted_count += int(unconv)
        by_currency.append(
            {
                "currency": cur,
                "count": int(cnt),
                "total_amount": str(Decimal(str(amt)).quantize(q2)),
                "total_savings": str(save.quantize(q2)),
            }
        )
    by_currency.sort(key=lambda e: e["currency"])
    return {
        "total": total,
        "total_amount": str(total_amount.quantize(q2)),
        "total_savings": str(total_savings.quantize(q2)),
        "currency": reporting_currency,
        "unconverted_count": unconverted_count,
        "by_currency": by_currency,
    }


@router.get("/queue")
async def payment_queue(
    pagination: PaginationParams = Depends(pagination_params),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """A page of approved invoices ready for payment (no live payment yet).

    Paginated (`page` / `page_size`, `id`-tie-broken order). Each row carries
    the verdict `services/payment_runs.run_refusal_reasons` gives it — the SAME
    predicate set `POST /api/payments/runs` enforces, so the queue can never
    offer a row the run builder then hard-409s (which used to take a whole
    select-all batch down with no way to bisect it):

    * `blocked` / `blocked_reason` — a run refuses this invoice on EVERY rail.
      The reason is a stable, PII-free code (an exception TYPE, or
      `fully_credited`), never a description, which can carry vendor / bank /
      amount detail.
    * `required_method` — a run refuses it on every rail BUT this one (a live
      virtual card already claims the invoice, and `virtual_card` converges onto
      that card rather than opening a second outflow). Not blocked: the row is
      still selectable, pinned to that rail.

    The money totals (`total_amount` / `total_savings` / `by_currency`) and the
    `total` / `selectable_total` / `blocked_total` counts describe the WHOLE
    queue, not the loaded page — a KPI/banner over one page would contradict
    the list. `selectable_total` is the set `GET /api/payments/queue/ids`
    resolves for "select all N matching", so it also excludes a
    `required_method` row (bulk-selecting one would stage it on the default
    rail); `blocked_total` counts only rows whose checkbox is disabled, i.e.
    exactly the `blocked` ones. The two are therefore NOT complementary.
    """
    # Callable as a plain function in tests (`Depends(...)` isn't resolved
    # there); fall back to the canonical first page.
    if not isinstance(pagination, PaginationParams):
        pagination = PaginationParams(page=1, page_size=DEFAULT_PAGE_SIZE)

    reporting_currency = resolve_reporting_currency(org.settings)
    # UTC, not the host's local date — the same calendar question
    # `services/analytics`, `discount_optimizer` and `cash_flow_alerts` answer.
    today = utc_today()

    page_q = (
        apply_entity_scope(
            select(Invoice, PaymentSchedule)
            .outerjoin(PaymentSchedule, PaymentSchedule.invoice_id == Invoice.id)
            .where(*_queue_base_where()),
            Invoice,
            entity_id,
        )
        .order_by(*_QUEUE_ORDER)
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(page_q)).all()

    # Which of THIS PAGE's rows a run would refuse, and why — resolved through
    # the ONE predicate set the run builder itself enforces, so the queue can
    # never offer a row the builder then rejects, and a refusal added to
    # `run_refusal_reasons` reaches this surface for free. No `methods` is
    # passed: the queue does not know which rail the operator will pick, and
    # omitting it is the fail-closed reading (every rail non-converging), which
    # is what surfaces the card claim as `required_method`.
    refusals = await run_refusal_reasons(db, [inv for inv, _ in rows])

    items: list[dict] = []
    for inv, sched in rows:
        refusal = refusals.get(inv.id)
        discount_amount: Decimal | None = None
        discount_eligible = False
        if (
            sched is not None
            and sched.discount_date is not None
            and sched.discount_percent is not None
            and sched.discount_date >= today
        ):
            discount_eligible = True
            # ROUND_HALF_UP, explicitly. `Decimal.quantize` defaults to the
            # context's ROUND_HALF_EVEN, and the whole-set rollup beside this
            # list (`_payment_queue_rollup`) computes the SAME figure in SQL
            # with `round(numeric, 2)` — which Postgres defines as
            # half-away-from-zero. So a genuine half-cent (a 21.00 invoice at
            # 2.50%: 0.525) rendered 0.52 in the row and summed as 0.53 in the
            # banner above it: the same discount, two numbers, on one screen.
            # Half-away-from-zero is the one both sides now use — it is what
            # Postgres does, what every other money quantizer in this codebase
            # passes explicitly (`international_payments._quantize_money`,
            # `payment_settlement._q`, `currency_conversion._quantize_money`),
            # and what auditors expect. Money amounts here are non-negative, so
            # ROUND_HALF_UP and half-away-from-zero are the same rule.
            discount_amount = (inv.amount * sched.discount_percent / Decimal(100)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        items.append(
            {
                "id": str(inv.id),
                "invoice_number": inv.invoice_number,
                "vendor_name": inv.vendor_name,
                # Money serialises as an exact Decimal STRING, never float() —
                # the frontend coerces with Number() at its arithmetic sites.
                "amount": str(inv.amount),
                "currency": inv.currency,
                "due_date": inv.due_date.isoformat() if inv.due_date else None,
                "payment_terms": inv.payment_terms,
                "status": inv.status.value if hasattr(inv.status, "value") else inv.status,
                "is_overdue": inv.due_date is not None and inv.due_date < today,
                "discount_eligible": discount_eligible,
                "discount_date": sched.discount_date.isoformat()
                if sched and sched.discount_date
                else None,
                # discount_percent is a rate, not money — stays a JSON number.
                "discount_percent": float(sched.discount_percent)
                if sched and sched.discount_percent
                else None,
                "discount_amount": str(discount_amount) if discount_amount else None,
                # `blocked` is what the UI disables the row's checkbox on, and
                # is true only for a refusal that holds on EVERY rail — a
                # rail-conditional one carries `required_method` instead and
                # stays selectable, because refusing it outright would break the
                # documented mint-a-card-then-run-it flow.
                #
                # `blocked_reason` is a stable code from a fixed vocabulary (an
                # exception TYPE, or one of `payment_runs`' own reason
                # constants) that the client localises — never a row's
                # description, which can carry vendor / bank / amount detail.
                # It is populated for a `required_method` row too, so the UI can
                # say WHY the rail is pinned.
                "blocked": refusal is not None and refusal.only_method is None,
                "blocked_reason": refusal.reason if refusal else None,
                "required_method": refusal.only_method if refusal else None,
            }
        )

    rollup = await _payment_queue_rollup(
        db,
        entity_id=entity_id,
        reporting_currency=reporting_currency,
        today=today,
        selectable_only=False,
    )
    selectable_total = (
        await db.execute(
            apply_entity_scope(
                select(func.count(Invoice.id))
                .select_from(Invoice)
                .where(*_queue_selectable_where()),
                Invoice,
                entity_id,
            )
        )
    ).scalar() or 0
    # Counted in its own right rather than as `total - selectable_total`.
    # Those two stopped being complementary once a rail-conditional refusal
    # existed: a card-claimed row leaves the SELECTABLE set (bulk-selecting it
    # would stage it on the default rail and 409 the batch) but is not blocked
    # — its checkbox works. Subtracting would report it to the operator as
    # something to go and clear, which is the opposite of the truth.
    blocked_total = (
        await db.execute(
            apply_entity_scope(
                select(func.count(Invoice.id))
                .select_from(Invoice)
                .where(*_queue_base_where(), _queue_blocked_on_every_rail()),
                Invoice,
                entity_id,
            )
        )
    ).scalar() or 0

    return {
        "items": items,
        "total": rollup["total"],
        "page": pagination.page,
        "page_size": pagination.page_size,
        "selectable_total": int(selectable_total),
        "blocked_total": int(blocked_total),
        "total_amount": rollup["total_amount"],
        "total_savings": rollup["total_savings"],
        "currency": rollup["currency"],
        "unconverted_count": rollup["unconverted_count"],
        "by_currency": rollup["by_currency"],
    }


@router.get("/queue/ids")
async def payment_queue_ids(
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """The invoice ids of every SELECTABLE (non-blocked) queue row — the
    resolver behind "select all N matching" on the payments queue.

    Mirrors `GET /api/invoices/ids`: capped at `MAX_SELECT_ALL_IDS`, with
    `truncated` flagging when the cap was hit so a partial selection is never
    presented as complete. `by_currency` carries the same per-currency
    `count` / `total_amount` / `total_savings` breakdown the queue endpoint
    returns, restricted to the selectable set — so the pay-bar's totals and
    its single-currency-per-run guard stay honest for a selection the client
    never loaded row-by-row.
    """
    today = utc_today()

    total = (
        await db.execute(
            apply_entity_scope(
                select(func.count(Invoice.id))
                .select_from(Invoice)
                .where(*_queue_selectable_where()),
                Invoice,
                entity_id,
            )
        )
    ).scalar() or 0

    ids_q = (
        apply_entity_scope(
            select(Invoice.id).where(*_queue_selectable_where()),
            Invoice,
            entity_id,
        )
        .order_by(*_QUEUE_ORDER)
        .limit(MAX_SELECT_ALL_IDS)
    )
    ids = [str(row) for row in (await db.execute(ids_q)).scalars().all()]

    rollup = await _payment_queue_rollup(
        db,
        entity_id=entity_id,
        reporting_currency=resolve_reporting_currency(org.settings),
        today=today,
        selectable_only=True,
    )
    return {
        "ids": ids,
        "total": int(total),
        "truncated": int(total) > len(ids),
        "currency": rollup["currency"],
        "by_currency": rollup["by_currency"],
    }


#: Payment statuses whose money is committed but not yet settled — what
#: `/summary` reports as `total_pending`. `pending_compliance` belongs here:
#: the payment is authorized and waiting on a human, so leaving it out made
#: held money appear in NEITHER KPI (not paid, not pending) — invisible in the
#: one place a treasurer looks for "what is still out there".
PENDING_PAYMENT_STATUSES = ("pending", "processing", "submitted", "pending_compliance")


@router.get("/summary")
async def payment_summary(
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """KPIs for the payments page summary bar. Scoped to the selected entity.

    **Every money total is in the org's reporting currency**, not a raw sum of
    `Payment.amount`. `Payment.amount` is denominated in the INVOICE's currency
    (`international_payments.prepare_international_payment` sets
    `amount=invoice.amount` and puts the home-currency debit on
    `source_amount`), so a book with one foreign invoice in it made these KPIs a
    silent two-currency mixture. `currency_conversion.payment_reporting_amount_sql`
    — the same resolver the 1099 report and the vendor risk score use — takes
    the rate-locked `source_amount` when the payment carries a home-currency
    leg, else `amount` when the invoice is already in that currency; a payment
    neither rung can establish is EXCLUDED and counted on
    `unconverted_payment_count` rather than added at face value. Nothing is
    converted at read time (a rate fetched on a read makes a historical total
    move under the reader — `docs/decisions.md` §18).
    """
    reporting_currency = resolve_reporting_currency(org.settings)
    reported = payment_reporting_amount_sql(
        reporting_currency=reporting_currency,
        payment_amount=Payment.amount,
        payment_source_amount=Payment.source_amount,
        payment_source_currency=Payment.source_currency,
        invoice_currency=Invoice.currency,
    )
    countable = reported.is_expressible

    async def _money_and_excluded(status_clause) -> tuple[Decimal, int]:
        q = apply_entity_scope(
            select(
                func.coalesce(func.sum(case((countable, reported.amount))), Decimal("0")),
                func.count(case((not_(countable), Payment.id))),
            )
            .select_from(Payment)
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .where(status_clause),
            Payment,
            entity_id,
        )
        total, excluded = (await db.execute(q)).one()
        return Decimal(str(total or 0)), int(excluded or 0)

    paid_amount, paid_excluded = await _money_and_excluded(Payment.status == "completed")
    pending_amount, pending_excluded = await _money_and_excluded(
        Payment.status.in_(PENDING_PAYMENT_STATUSES)
    )
    # Money serialises as an exact Decimal STRING, never float().
    total_paid = str(paid_amount)
    total_pending = str(pending_amount)

    count_q = apply_entity_scope(select(func.count()).select_from(Payment), Payment, entity_id)
    payment_count = (await db.execute(count_q)).scalar() or 0

    # CardRebate is a TENANT-scoped table (it lives in the per-tenant DB, not the
    # control plane — an earlier "control plane" comment here was wrong and made
    # this query run against control_db, where the table doesn't exist).
    #
    # Denominated like every other figure in this response. `CardRebate` carries
    # no currency column of its own, so a rebate's currency is only knowable
    # through its card — the same indirection `GET /api/cards/dashboard` uses,
    # and the reason this is a join rather than a bare SUM. It was a
    # cross-currency `func.sum(CardRebate.amount)` shipped under the
    # `"currency": reporting_currency` this response declares two keys below,
    # which is a quantity in no currency at all.
    #
    # Entity-scoped for the same reason the paid/pending/queue figures are: a
    # summary that mixes an entity-scoped outflow with an org-wide rebate can't
    # be reconciled against either. (The comment this replaces claimed to match
    # "the dashboard KPI" org-wide; that dashboard is itself entity-scoped now,
    # so the claim had stopped being true in the direction it was arguing.)
    _rebate_ccy = card_currency_sql(reporting_currency)
    rebate_q = apply_entity_scope(
        select(
            func.coalesce(
                func.sum(case((_rebate_ccy == reporting_currency, CardRebate.amount))), 0
            ),
            func.count(case((_rebate_ccy != reporting_currency, CardRebate.id))),
        )
        .select_from(CardRebate)
        .join(VirtualCard, VirtualCard.id == CardRebate.virtual_card_id),
        VirtualCard,
        entity_id,
    )
    rebate_amount, rebates_excluded = (await db.execute(rebate_q)).one()
    total_rebates = str(Decimal(str(rebate_amount or 0)))

    # `queue_count` MUST describe the same set the queue endpoint offers, or the
    # summary card and the table below it disagree. Same `_queue_base_where()`:
    # a payable status the state machine can advance to `payment_scheduled`
    # (`sent_to_erp` is excluded — it must reach `posted_in_erp` via the ERP
    # webhook first) AND no live payment already claiming the invoice.
    queue_inner = apply_entity_scope(
        select(Invoice.id).where(*_queue_base_where()),
        Invoice,
        entity_id,
    )
    queue_q = select(func.count()).select_from(queue_inner.subquery())
    queue_count = (await db.execute(queue_q)).scalar() or 0

    return {
        "total_paid": total_paid,
        "total_pending": total_pending,
        "payment_count": payment_count,
        "total_rebates": total_rebates,
        "queue_count": queue_count,
        # What the money figures above are denominated in, and how many
        # payments were left out of them because neither rung could establish
        # a reporting-currency figure. Surfaced rather than folded in, exactly
        # as the 1099 report surfaces `unconverted_payment_count`.
        "currency": reporting_currency,
        "unconverted_payment_count": paid_excluded + pending_excluded,
        # Rebates left out of `total_rebates` for being denominated in another
        # currency. Counted separately from `unconverted_payment_count`: that
        # one is a payment whose reporting-currency figure could not be
        # ESTABLISHED, this one is a rebate whose currency is known and simply
        # is not this one. Folding them together would describe neither.
        "excluded_rebate_count": int(rebates_excluded or 0),
    }


class CorridorQuoteRequest(BaseModel):
    """Which payment to price, and how to rank the routes."""

    invoice_id: uuid.UUID
    method: str | None = Field(default=None, max_length=40)
    mode: Literal["cheapest", "fastest"] = "cheapest"


@router.post("/corridor-quotes")
async def compare_corridor_quotes(
    body: CorridorQuoteRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Price one payable invoice across every processor the org has configured.

    **Read-only, advisory, and it moves no money.** Nothing here books a
    `Payment`, claims a run, or touches an invoice: it asks each configured
    adapter's optional `quote_payment` capability what this payment would cost
    and how fast it would settle, ranks them
    (`services/corridor_quotes.compare_quotes`), and returns the ranking plus
    what the winner saves against the next-best route.

    Deliberately NOT an auto-router. Which bank actually moves the money is a
    treasury decision — the rail on the `Payment` row still comes from
    `payment_corridor.pick_corridor` and the org's configured provider, exactly
    as before. This endpoint is what lets a human see the trade-off before
    making that decision, and it is why the optimizer stops being a module
    nothing reaches.

    An adapter that has published no fee schedule reports `available=False`
    (`PaymentAdapter.quote_payment` fails closed rather than fabricating a
    free/instant quote — see its docstring), so it is listed and skipped rather
    than winning on numbers nobody supplied. 409 when no configured provider
    can quote this corridor at all.
    """
    from app.services.corridor_quotes import (
        NoEligibleCorridorError,
        compare_quotes,
        savings_vs_runner_up,
    )

    invoice = (
        await db.execute(
            apply_entity_scope(
                select(Invoice).where(Invoice.id == body.invoice_id), Invoice, entity_id
            )
        )
    ).scalar_one_or_none()
    if invoice is None:
        # Same opaque 404 an out-of-scope id gets everywhere else in this file,
        # so the response can't enumerate another entity's invoices.
        raise HTTPException(status_code=404, detail="Invoice not found")

    vendor_bank: dict | None = None
    if invoice.vendor_id:
        vendor_bank = (
            await db.execute(select(Vendor.bank_details).where(Vendor.id == invoice.vendor_id))
        ).scalar_one_or_none()

    payload = PaymentPayload(
        # A quote is not an order, so this correlation id is never sent as an
        # idempotency key to anything that moves money — the real payment mints
        # its own when it is booked.
        correlation_id=str(invoice.correlation_id or invoice.id),
        invoice_id=str(invoice.id),
        invoice_number=invoice.invoice_number or "",
        vendor_name=invoice.vendor_name or "",
        amount=invoice.amount or Decimal("0"),
        currency=(invoice.currency or "USD").upper(),
        method=(body.method or "ach").strip().lower(),
        description=invoice.description,
        vendor_bank=vendor_bank,
        metadata={"organization_id": str(org.id)},
    )

    try:
        ranking = await compare_quotes(payload, org.settings, mode=body.mode)
    except NoEligibleCorridorError as exc:
        # PII-free by construction: the message names the method, currency,
        # target country and each provider's machine reason — never a bank
        # field. See `corridor_quotes._quote_one`.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _quote(q) -> dict:
        return {
            "provider": q.provider,
            "method": q.method,
            "available": q.available,
            "unavailable_reason": q.unavailable_reason,
            # Money as an exact Decimal STRING, never float. An unavailable
            # quote's cost is `Decimal("Infinity")` by design (so it can never
            # win a min() comparison); that is not a figure to render, so it
            # crosses the boundary as null.
            "total_cost": str(q.total_cost(payload.amount)) if q.available else None,
            "flat_fee": str(q.flat_fee),
            "pct_fee": str(q.pct_fee),
            "eta_business_days": q.eta_business_days,
            "fx_rate": str(q.fx_rate) if q.fx_rate is not None else None,
        }

    return {
        "invoice_id": str(invoice.id),
        "mode": ranking.mode,
        "currency": payload.currency,
        "amount": str(payload.amount),
        "winner": _quote(ranking.winner),
        "runners_up": [_quote(q) for q in ranking.runners_up],
        "savings_vs_runner_up": str(savings_vs_runner_up(ranking, payload.amount)),
        # This endpoint is advisory only — say so in the payload, not just the
        # docstring, so a client can't mistake it for a routing decision.
        "advisory": True,
    }


@router.get("/counts")
async def payment_status_counts(
    method: str | None = None,
    invoice_id: uuid.UUID | None = None,
    search: str | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    # Exactly the list's gate, not the older role set. A custom-role holder of
    # only one of the two permissions could read the History list and got a 403
    # here, at which point the page falls back to the page-scoped tally this
    # endpoint exists to replace — reintroducing the undercount for that user.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Per-status payment tallies for the History-tab filter chips.

    Computed over the WHOLE entity-scoped payment set, not the loaded page, so
    the chip counts (and the "All" count) don't undercount once the history
    list paginates. Declared before the `/{payment_id}` route so the literal
    path isn't parsed as a UUID.

    Takes the list's population filters through the SAME
    `_payment_list_filters` builder as `GET /api/payments`, so the chips
    describe exactly the rows the list would return. Without them a search for
    one vendor left the chips reading the tenant's whole total over a one-row
    table. Deliberately NOT `status`: status is the dimension being tallied, so
    applying it would zero every other chip — the same rule
    `invoices.py::invoice_counts` and `purchase_orders.py` state.
    """
    query = _payment_list_filters(
        select(Payment.status, func.count()).select_from(Payment),
        entity_id=entity_id,
        status=None,
        method=method,
        invoice_id=invoice_id,
        search=search,
        amount_min=amount_min,
        amount_max=amount_max,
    ).group_by(Payment.status)
    rows = (await db.execute(query)).all()
    by_status = {str(s): int(n) for s, n in rows}
    return {"total": sum(by_status.values()), "by_status": by_status}


@router.get("/{payment_id}/remittance")
async def get_payment_remittance(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Return a single-page remittance-advice PDF for the payment.

    Currently includes the one invoice the Payment row points at — when
    we group multiple invoices on a single payment in a future schema
    bump, this endpoint will pick up the rest automatically (the line
    item list is built from the row's invoice_id today, but the PDF
    accepts a list)."""
    from app.services.branding import get_brand_context
    from app.services.remittance_pdf import (
        RemittanceContext,
        RemittanceLine,
        render_remittance_pdf,
    )

    payment = await _get_scoped_payment(db, payment_id, entity_id)
    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == payment.invoice_id))
    ).scalar_one_or_none()

    company = (org.settings or {}).get("company") or {}

    vendor_address: str | None = None
    if invoice and invoice.vendor_id:
        v_result = await db.execute(select(Vendor.address).where(Vendor.id == invoice.vendor_id))
        vendor_address = v_result.scalar_one_or_none()

    ctx = RemittanceContext(
        payer_name=org.name,
        payer_address=company.get("address") or None,
        vendor_name=invoice.vendor_name if invoice else "Unknown vendor",
        vendor_address=vendor_address or (invoice.remit_to_address if invoice else None),
        payment_date=payment.completed_at or payment.submitted_at or payment.created_at,
        payment_method=payment.method or "ach",
        payment_reference=payment.reference,
        payment_amount=payment.amount,
        currency=invoice.currency if invoice else "USD",
        lines=[
            RemittanceLine(
                invoice_number=(invoice.invoice_number if invoice else str(payment.invoice_id)),
                description=invoice.description if invoice else None,
                amount=payment.amount,
            )
        ],
        brand=get_brand_context(org.settings),
    )
    pdf_bytes = await asyncio.to_thread(render_remittance_pdf, ctx)

    # `Payment.reference` is free text the processor supplies, so it reaches
    # here unsanitised: Starlette latin-1-encodes header values, so a non-ASCII
    # reference raised UnicodeEncodeError out of the ASGI app instead of
    # returning the PDF, and a `"` broke out of the quoted string. RFC 6266
    # ASCII fallback + UTF-8 `filename*=`, via the shared helper the invoice and
    # portal downloads already use.
    filename = f"remittance-{payment.reference or str(payment.id)[:8]}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition_attachment(filename)},
    )


@router.get("/{payment_id}", response_model=PaymentResponse)
async def get_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    # Same any-of as the list above — the single-payment companion of a
    # resource a `payment.execute`/`payment.void` custom-role holder can
    # already list.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    p = await _get_scoped_payment(db, payment_id, entity_id)
    inv = (await db.execute(select(Invoice).where(Invoice.id == p.invoice_id))).scalar_one_or_none()

    # SOX access-control auditing: a payment detail is a regulated money record.
    # Record the view (no banking values enter the audit details). Payment rows
    # carry no organization_id column — the org comes from the authed user (the
    # tenant is already resolved by get_tenant_db, so this can't widen scope).
    await log_access(
        db,
        user=user,
        organization_id=user.organization_id,
        entity_type="payment",
        entity_id=p.id,
        correlation_id=p.correlation_id,
    )
    await db.commit()

    return PaymentResponse.from_db(p, inv)


class VoidPaymentRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


async def _cancel_card_for_void(
    db: AsyncSession,
    *,
    payment: Payment,
    org: Organization,
    user: User,
    via: str = "payment_void",
) -> str | None:
    """Kill the virtual card a voided payment issued. Returns an outcome tag
    for the void's audit row (``None`` when the payment isn't a card payment).

    Voiding a card payment has to reach the provider, not just our books. The
    card is bearer-spendable: left live, the vendor can still redeem it while
    the only payment naming it says ``voided``, and — because it still occupies
    the invoice's live-card slot (``uq_virtual_cards_one_live_per_invoice``
    counts every non-``cancelled`` row) — the next payment run rediscovers it.

    Only an **unspent** card can be cancelled. Once it is ``charged`` /
    ``completed`` the funds have moved and the provider cannot un-spend it, so
    the honest outcome is to record ``card_already_charged`` for AP to chase;
    `card_settlement_block` is what then stops a later run from quietly
    "settling" a new payment against that spent card.

    Provider-FIRST, mirroring ``POST /api/cards/{id}/cancel``: the row is only
    marked cancelled once the provider confirms the close. The fail-safe
    direction is "dead at the provider, maybe stale in the DB" — never the
    reverse. A provider failure is recorded, not raised: an outage must not
    block the accounting void (same posture as the payment rail above).

    Shared with the retry exit ``POST /{payment_id}/void/retry-card-cancel``,
    which re-attempts the card leg alone when this one did not confirm a close.
    ``via`` is what separates the two on the ``card.cancelled`` audit row; the
    provider-first ordering, the spent-card refusal and the outcome vocabulary
    are deliberately the same code so the retry cannot drift from the original
    attempt.

    The card row is taken ``FOR UPDATE``. That is what makes a retry idempotent
    under concurrency: two operators retrying at once serialize, and the second
    re-reads ``status == "cancelled"`` and returns ``card_already_cancelled``
    without a second provider call or a second audit row. (The provider leg is
    itself idempotent — every adapter treats an already-closed card as a
    confirmed cancel — so the lock is about our own trail, not about the card.)
    """
    if payment.method != "virtual_card":
        return None

    from app.config import settings as app_settings
    from app.services.card_issuance import CARD_SPENT_STATUSES, cancel_card_at_provider

    # `virtual_cards.payment_id` is NOT unique — a cancel-then-reissue leaves the
    # dead row behind pointing at the same payment. An unordered `LIMIT 1` could
    # therefore hand back the cancelled row and report `card_already_cancelled`
    # while the LIVE card stayed open, which is the exact failure this function
    # exists to prevent. Order live-first (at most one non-cancelled card can
    # exist per invoice — `uq_virtual_cards_one_live_per_invoice`), then newest,
    # so the row picked is deterministic and is always the spendable one.
    card = (
        await db.execute(
            select(VirtualCard)
            .where(VirtualCard.payment_id == payment.id)
            .order_by(
                case((VirtualCard.status == "cancelled", 1), else_=0).asc(),
                VirtualCard.created_at.desc(),
                VirtualCard.id.asc(),
            )
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if card is None:
        return "no_card_linked"
    if card.status == "cancelled":
        return "card_already_cancelled"
    if card.status in CARD_SPENT_STATUSES:
        return "card_already_charged"

    outcome = await cancel_card_at_provider(
        card=card, org_settings=org.settings or {}, app_settings=app_settings
    )
    if outcome != "cancelled":
        return outcome

    prior_status = card.status
    card.status = "cancelled"

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=card.correlation_id or payment.correlation_id or uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="card.cancelled",
        entity_type="virtual_card",
        entity_id=card.id,
        details={
            "last_four": card.last_four,
            "from": prior_status,
            "to": "cancelled",
            "via": via,
            "payment_id": str(payment.id),
        },
    )
    return "card_cancelled"


@router.post("/{payment_id}/void")
async def void_payment(
    payment_id: uuid.UUID,
    body: VoidPaymentRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # Defaults map to admin/cfo (unchanged) — see ROLE_DEFAULT_PERMISSIONS.
    user: User = Depends(require_permission(PERM_PAYMENT_VOID)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Void a completed or in-flight payment.

    Adapter dispatch: when the configured processor exposes
    ``void_payment`` we ask it to reverse upstream. If it doesn't (mock /
    legacy rows), we record the local void only — the AP team is
    expected to chase the rail manually. Either way, the invoice flips
    back to ``approved`` so it re-enters the payment queue.
    """
    # Lock the payment row FOR UPDATE and re-check its status inside the
    # transaction. Two concurrent voids would otherwise both pass a
    # non-locking guard, both call the adapter's `void_payment`, and both
    # write a `payment.voided` audit row (double-void). The row lock
    # serializes them: the first transaction flips the status to `voided`
    # and commits; the second blocks on the lock, then re-reads the now-
    # terminal status and 409s before touching the adapter. The Invoice is
    # fetched separately — Postgres can't `FOR UPDATE` the nullable side of
    # an outer join, and we don't need to lock the invoice here.
    payment = await _get_scoped_payment(db, payment_id, entity_id, for_update=True)

    if payment.status in ("voided", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Payment already {payment.status}")
    if payment.status == "failed":
        raise HTTPException(
            status_code=409,
            detail="Cannot void a failed payment (it never settled)",
        )

    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == payment.invoice_id))
    ).scalar_one_or_none()

    # Capture the status BEFORE mutating so the audit row records the real
    # prior state (any of completed / submitted / processing / pending).
    previous_status = payment.status

    # Adapter side: best-effort. A processor failure here doesn't block
    # the local void — operators can chase the rail manually, but the
    # accounting books should always reflect intent. That includes an
    # unsupported `settings.payments.provider`: the accounting void still
    # lands, and `provider_not_supported` on the audit row is what tells the
    # operator the rail was never asked (rather than the old behaviour, which
    # called `mock.void_payment` — it returns True unconditionally — and
    # recorded a `voided_upstream` that never happened).
    payment_config = (org.settings or {}).get("payments") or {}
    adapter_outcome: str | None = None
    try:
        adapter = get_payment_adapter(payment_config)
    except UnknownPaymentProviderError:
        adapter = None
        adapter_outcome = "provider_not_supported"
    if adapter is not None and payment.provider_payment_id:
        try:
            void_fn = getattr(adapter, "void_payment", None)
            if callable(void_fn):
                ok = await void_fn(payment.provider_payment_id)
                adapter_outcome = "voided_upstream" if ok else "rejected_by_processor"
            else:
                adapter_outcome = "no_adapter_support"
        except Exception as exc:  # noqa: BLE001
            adapter_outcome = f"adapter_error:{exc.__class__.__name__}"

    # Card side: a voided virtual-card payment must also kill the card, or the
    # void doesn't stop the money — the card stays live and spendable at the
    # provider with no payment behind it, and the next run rediscovers it in the
    # invoice's live-card slot. Best-effort like the payment rail above: a card
    # provider outage records the outcome rather than blocking the accounting
    # void. Only an UNSPENT card can be cancelled (see `_cancel_card_for_void`).
    card_outcome = await _cancel_card_for_void(db, payment=payment, org=org, user=user)

    now = datetime.now(UTC)
    payment.status = "voided"
    payment.failure_reason = f"Voided by {user.full_name}: {body.reason}"
    # `completed_at` is the regulated SETTLEMENT timestamp. Voiding a
    # `completed` payment used to overwrite it with the void instant,
    # destroying the only record of when the money actually moved — and the
    # audit row captured `previous_status` but not the previous timestamp, so
    # it was unrecoverable. `/retry-failed` explicitly refuses to overwrite the
    # same two timestamps and says why. A payment that never settled
    # (`pending` / `submitted` / `processing` / `pending_compliance`) has no
    # settlement time to protect, so it still gets a terminal timestamp here;
    # the void instant itself rides the audit row below on every path.
    if payment.completed_at is None:
        payment.completed_at = now

    # Reopen the invoice for re-payment if it was scheduled by this row.
    if invoice and invoice.status in (
        InvoiceStatus.payment_scheduled,
        InvoiceStatus.paid,
    ):
        await transition_invoice(
            db,
            invoice,
            InvoiceStatus.approved,
            actor_id=user.id,
            action_name="invoice.voided_return_to_approved",
            details={"void_reason": body.reason, "payment_id": str(payment.id)},
        )

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=payment.correlation_id or uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="payment.voided",
        entity_type="payment",
        entity_id=payment.id,
        details={
            "reason": body.reason,
            "adapter_outcome": adapter_outcome,
            "card_outcome": card_outcome,
            "amount": str(payment.amount),
            "previous_status": previous_status or "unknown",
            # The void instant, recorded here rather than on `completed_at`
            # (see above). This row is the append-only evidence of when the
            # void happened; the settlement timestamp keeps saying when the
            # money moved.
            "voided_at": now.isoformat(),
            "settled_at": payment.completed_at.isoformat() if payment.completed_at else None,
        },
    )

    await _recompute_parent_run_status(db, payment)

    await db.commit()
    await db.refresh(payment)
    resp = PaymentResponse.from_db(payment, invoice)
    # Surface the two best-effort legs' outcomes so the operator (and the void
    # dialog) can tell whether the rail was reversed and — for a card payment —
    # whether the card was actually closed at the provider. `void_card_disposition`
    # is the verdict the UI branches on; a `not_closed_retryable` one is the
    # prompt to retry via `POST /{payment_id}/void/retry-card-cancel` below.
    resp.void_card_outcome = card_outcome
    resp.void_card_disposition = card_cancel_disposition(card_outcome)
    resp.void_adapter_outcome = adapter_outcome
    return resp


@router.post("/{payment_id}/void/retry-card-cancel", response_model=PaymentResponse)
async def retry_void_card_cancel(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # The SAME gate as the void this completes. Deliberately not the card
    # router's `require_roles(ADMIN, AP_MANAGER, CFO)`: an org that split the
    # duties and withheld `payment.void` from `ap_manager` must not find the
    # reversal's other half reachable behind a bare role.
    user: User = Depends(require_permission(PERM_PAYMENT_VOID)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Re-attempt ONLY the card close for an already-voided card payment.

    The void's card leg is best-effort so a card-provider outage cannot block
    the accounting void — but a leg that did not confirm leaves a live,
    bearer-spendable card behind a payment the books call `voided`. Before this
    route the outcome landed on the `payment.voided` audit row and nothing in
    the app could close that card.

    **The remedy sits on the void, not beside it** (docs/decisions.md §96, §132).
    `POST /api/cards/{id}/cancel` exists and would close the card, but it is
    reachable on a LIVE payment too, where it would kill the card while the
    payment and its invoice still claim money is in flight on a now-dead rail —
    two controls that both close a card and leave the ledger in different
    states. This one refuses anything but an already-`voided` payment, so it can
    only ever *finish* a reversal the books already recorded.

    **Idempotent, and by construction rather than by claim.** It re-runs
    `_cancel_card_for_void`'s exact provider-first path: the card row is taken
    `FOR UPDATE`, an already-`cancelled` row short-circuits to
    `card_already_cancelled` with no provider call and no second
    `card.cancelled` row, and every card adapter treats an already-closed card
    at the provider as a confirmed cancel. A second retry is a no-op, not an
    error.

    **Moves no money and re-touches nothing else.** The payment stays `voided`,
    the invoice is not transitioned, and the payment rail is not re-asked — so
    `void_adapter_outcome` is `None` on this response, because this call did not
    ask it. The *attempt* is recorded on the append-only trail either way: the
    operator needs to see that a retry happened and what the provider said, not
    only the retries that happened to succeed.
    """
    payment = await _get_scoped_payment(db, payment_id, entity_id, for_update=True)

    if payment.status != "voided":
        raise HTTPException(
            status_code=409,
            detail=(
                "Only a voided payment's card close can be retried "
                f"(this payment is '{payment.status}'). Void it first."
            ),
        )
    if payment.method != "virtual_card":
        raise HTTPException(
            status_code=409,
            detail="This payment did not issue a virtual card, so there is nothing to close.",
        )

    card_outcome = await _cancel_card_for_void(
        db, payment=payment, org=org, user=user, via="payment_void_retry"
    )
    disposition = card_cancel_disposition(card_outcome)

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=payment.correlation_id or uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="payment.void_card_cancel_retried",
        entity_type="payment",
        entity_id=payment.id,
        # PII-free: the outcome tag and its verdict only. `card_cancel_error:*`
        # carries the exception TYPE, never the provider's body — a card-provider
        # error string can embed a partial PAN (`card_issuance` guards that at
        # source).
        details={"card_outcome": card_outcome, "card_disposition": disposition},
    )

    await db.commit()
    await db.refresh(payment)

    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == payment.invoice_id))
    ).scalar_one_or_none()
    resp = PaymentResponse.from_db(payment, invoice)
    resp.void_card_outcome = card_outcome
    resp.void_card_disposition = disposition
    return resp


async def _recompute_parent_run_status(db: AsyncSession, payment: Payment) -> None:
    """Re-derive the parent `PaymentRun.status` after a payment's status changed.

    `_dispatch_run_payments`' final rollup is the only writer of the persisted
    value, so every endpoint that moves a payment afterwards
    (`/compliance/release`, `/compliance/dismiss`, `/void`) left the run
    describing an outcome its own payments no longer supported. The reads
    derive it anyway (`payment_runs.derive_run_status`), so this is what keeps
    the stored column honest for anything that reads it directly — an operator
    at `psql`, an export, a future consumer.

    A standalone payment (no run) is a no-op. Never commits — the caller's
    transaction owns that.
    """
    if payment.payment_run_id is None:
        return
    run = (
        await db.execute(select(PaymentRun).where(PaymentRun.id == payment.payment_run_id))
    ).scalar_one_or_none()
    if run is None:
        return
    await recompute_run_status(db, run)


async def _resolve_compliance_hold_exception(
    db: AsyncSession,
    *,
    invoice: Invoice,
    actor_id: uuid.UUID,
    actor_name: str,
    resolution: str,
) -> None:
    """Resolve the open `payment_compliance_hold` exception for an invoice,
    if one exists.

    Delegates to `services/exception_lifecycle.record_decision` — the same
    chokepoint the human queue and the autonomous agents go through — so this
    decision writes the append-only `exception.resolved` row too. Releasing a
    compliance hold is precisely the sign-off that lets held money move, which
    makes it the last decision that should have lived only on the mutable
    `exceptions` row.

    Both callers keep the `resolve` verb (not `dismiss`): a dismissed *payment*
    still means a human cleared the hold, and the queue's status semantics
    predate this. The rationale — `released` vs `dismissed: <reason>` — is what
    distinguishes them, on the immutable row as well as the exception.
    """
    result = await db.execute(
        select(APException).where(
            APException.invoice_id == invoice.id,
            APException.exception_type == "payment_compliance_hold",
            APException.status == "open",
        )
    )
    exc = result.scalar_one_or_none()
    if exc is None:
        return
    await record_decision(
        db,
        exception=exc,
        action="resolve",
        resolution=resolution,
        actor_id=actor_id,
        actor_name=actor_name,
        invoice=invoice,
    )


class DismissComplianceHoldRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)


@router.post("/{payment_id}/compliance/release", response_model=PaymentResponse)
async def release_compliance_hold(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # Releasing dispatches the payment to the processor exactly like
    # /execute — same money-moving permission.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Re-run compliance + dispatch for a payment stuck in `pending_compliance`.

    A `hold` verdict (an AML spend-threshold signal, or a `review_required`
    sanctions match — both everyday, non-fraud events; the trailing-12-month
    AML check is explicitly documented as "does NOT refuse — too many false
    positives") used to leave the payment exactly where it landed with no
    way forward. This re-runs `_execute_single_payment`'s full
    compliance-then-adapter path — the SAME gate a fresh /execute would run,
    never a bypass — so a payment that's genuinely still blocked (the hold
    condition hasn't actually changed) stays `pending_compliance` and the
    response reflects that, rather than silently forcing money to move.
    """
    payment = await _get_scoped_payment(db, payment_id, entity_id, for_update=True)
    if payment.status != "pending_compliance":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Can only release a payment stuck 'pending_compliance', not '{payment.status}'"
            ),
        )

    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == payment.invoice_id))
    ).scalar_one_or_none()

    # Releasing dispatches to the processor exactly like /execute, so it takes
    # the same pre-flight: an unsupported provider refuses here with the
    # payment still `pending_compliance`, never a 500 mid-dispatch.
    adapter = _require_payment_adapter(org)
    now = datetime.now(UTC)
    try:
        await _execute_single_payment(
            db, payment=payment, org=org, adapter=adapter, user=user, now=now
        )
    except Exception as exc:  # noqa: BLE001
        # Same guard `_dispatch_run_payments` puts round this call, for the
        # same reason: a live FX / sanctions / processor adapter can raise
        # anything. Unguarded, the exception unwound the request — FastAPI
        # 500ed, the session rolled back, and the payment reverted to
        # `pending_compliance` with no `provider_payment_id` recorded even if
        # the processor had already accepted the order (and, on the card leg,
        # a rollback after `persist_card` discarded the `VirtualCard` row while
        # a real spendable card existed at the provider). Recording the attempt
        # as `failed` is what keeps that from being invisible; the reused
        # `correlation_id` is the processor's idempotency key, not a substitute
        # for the record.
        #
        # Log the exception TYPE only, never `str(exc)` / `exc_info` — an
        # adapter can embed a partial account number, IBAN or PAN in its error
        # string (PII/banking-data-out-of-logs invariant).
        logger.warning(
            "payment %s raised during compliance release; marking failed: %s",
            payment.id,
            exc.__class__.__name__,
        )
        payment.status = "failed"
        payment.failure_reason = f"unexpected_error:{exc.__class__.__name__}"
        payment.completed_at = now

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=payment.correlation_id or uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="payment.compliance_released",
        entity_type="payment",
        entity_id=payment.id,
        details={
            "new_status": payment.status,
            "amount": str(payment.amount),
        },
    )

    # A human just made the release decision — that IS the sign-off. Only
    # resolve the exception if the hold actually cleared; still-held
    # payments keep it open (re-running /release again does nothing new).
    if payment.status != "pending_compliance" and invoice is not None:
        await _resolve_compliance_hold_exception(
            db,
            invoice=invoice,
            actor_id=user.id,
            actor_name=user.full_name,
            resolution="released",
        )

    await _recompute_parent_run_status(db, payment)

    await db.commit()
    await db.refresh(payment)

    # A release that SETTLES synchronously has to hand off to the ERP sync,
    # exactly as `_dispatch_run_payments` does at the end of `/execute` — that
    # module is the only path that flips an invoice `payment_scheduled → paid`,
    # and nothing re-invokes it for a payment that is already `completed`.
    # Without this, releasing a hold on a rail that confirms instantly (the
    # virtual-card leg always does; so does any adapter returning `completed`)
    # moved the money and left the invoice at `payment_scheduled` forever —
    # under-counting the aging report, the `/dashboard` pipeline, the vendor's
    # payment history and the 1099 YTD totals, while the payment row itself
    # looked perfectly correct.
    #
    # After the commit so the pass sees the settled status (mirrors the
    # webhook handler's ordering). A payment released into `submitted` /
    # `processing` is deliberately NOT dispatched: its own webhook will, once
    # the rail confirms.
    if payment.status == "completed" and payment.payment_run_id:
        from app.services import payment_erp_sync

        await payment_erp_sync.dispatch_payment_sync(payment.payment_run_id, uuid.UUID(str(org.id)))

    return PaymentResponse.from_db(payment, invoice)


@router.post("/{payment_id}/compliance/dismiss", response_model=PaymentResponse)
async def dismiss_compliance_hold(
    payment_id: uuid.UUID,
    body: DismissComplianceHoldRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # Dismissing moves no money (nothing ever settled) — a treasury decision
    # to give up on this payment, same gate as void.
    user: User = Depends(require_permission(PERM_PAYMENT_VOID)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Give up on a payment stuck in `pending_compliance` — flips it to
    `failed` without ever reaching the processor. AP has reviewed the hold
    (e.g. a genuine sanctions match, or a decision to pay this vendor a
    different way) and decided this payment should not proceed as-is."""
    payment = await _get_scoped_payment(db, payment_id, entity_id, for_update=True)
    if payment.status != "pending_compliance":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Can only dismiss a payment stuck 'pending_compliance', not '{payment.status}'"
            ),
        )

    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == payment.invoice_id))
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    payment.status = "failed"
    payment.failure_reason = f"compliance_dismissed by {user.full_name}: {body.reason}"
    payment.completed_at = now

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=payment.correlation_id or uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="payment.compliance_dismissed",
        entity_type="payment",
        entity_id=payment.id,
        details={"reason": body.reason, "amount": str(payment.amount)},
    )

    if invoice is not None:
        await _resolve_compliance_hold_exception(
            db,
            invoice=invoice,
            actor_id=user.id,
            actor_name=user.full_name,
            resolution=f"dismissed: {body.reason}",
        )

    await _recompute_parent_run_status(db, payment)

    await db.commit()
    await db.refresh(payment)
    return PaymentResponse.from_db(payment, invoice)


class AcceptSettlementRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)


@router.post("/{payment_id}/settlement/accept", response_model=PaymentResponse)
async def accept_settlement(
    payment_id: uuid.UUID,
    body: AcceptSettlementRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # Declaring a short settlement final closes the payable out — the same
    # money-state authority as executing or voiding a payment.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Accept a short / unverifiable settlement as final and release the invoice.

    This is the release path that makes the ERP-sync hold safe to ship. When
    the rail settles less than AP authorized (or in a currency we never
    authorized), `settlement_coverage` says the invoice is not discharged and
    `payment_erp_sync` leaves it at `payment_scheduled` rather than reporting
    it settled in full. Without an exit that would be a permanent strand — the
    exact defect that forced the first attempt at this hold to be reverted,
    because nothing re-invokes that sweep once a run's payments are terminal.

    There are two legitimate exits and this is the one that keeps the money
    where it landed:

    * **Accept** (here) — the shortfall is agreed with the vendor, or the
      remainder is being handled outside this invoice. The invoice moves to
      `paid` and the `reason` is recorded on the immutable trail.
    * **Void** (`POST /api/payments/{id}/void`) — the settlement is wrong. The
      invoice returns to `approved` to be re-paid correctly. That path already
      accepts a `payment_scheduled` invoice, so it works while held.

    Deliberately does NOT resolve the `fraud_flag` the settlement discrepancy
    raised. Unlike `payment_compliance_hold` — a type only the compliance path
    ever raises — `fraud_flag` is shared with Positive Pay's altered-cheque
    detection, so clearing "the open one" here could silently close an
    unrelated fraud finding. The exception queue stays the separate human
    sign-off, and it writes its own append-only row when someone makes it.
    """
    payment = await _get_scoped_payment(db, payment_id, entity_id, for_update=True)

    # Only a settlement that actually happened can be accepted.
    if payment.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Can only accept the settlement of a 'completed' payment, not '{payment.status}'"
            ),
        )

    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == payment.invoice_id))
    ).scalar_one_or_none()

    coverage = settlement_coverage(
        settled_amount=payment.settled_amount,
        settled_currency=payment.settled_currency,
        target_amount=payment.amount,
        target_currency=(invoice.currency if invoice else None),
        source_amount=payment.source_amount,
        source_currency=payment.source_currency,
        settled_amount_unstorable=payment.settled_amount_unstorable,
    )
    # Nothing to accept. Refusing rather than no-op'ing keeps this endpoint
    # from becoming a general-purpose "force the invoice to paid" lever: a
    # fully-covered payment reaches `paid` through the ordinary sync.
    if coverage.completes_invoice:
        raise HTTPException(
            status_code=409,
            detail=(
                "This payment's settlement already covers the invoice; there is nothing to accept."
            ),
        )

    # Nothing to release — checked BEFORE the audit dispatch so a repeat call
    # never even stages a second `payment.settlement_accepted` row. Refusing
    # keeps a retry from letting an operator re-justify the same acceptance
    # indefinitely on the immutable trail; the first call's row is the record.
    if invoice is None or invoice.status.value != "payment_scheduled":
        raise HTTPException(
            status_code=409,
            detail="No held invoice to release for this payment.",
        )

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=payment.correlation_id or uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="payment.settlement_accepted",
        entity_type="payment",
        entity_id=payment.id,
        details={
            "reason": body.reason,
            "coverage": coverage.state,
            # Money as exact decimal strings, never float. PII-free — amounts,
            # currency codes and the verdict only.
            "shortfall": (None if coverage.shortfall is None else str(coverage.shortfall)),
            "authorized_amount": str(payment.amount),
            "settled_amount": (
                None if payment.settled_amount is None else str(payment.settled_amount)
            ),
            "settled_currency": payment.settled_currency,
        },
    )

    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.paid,
        actor_id=user.id,
        action_name="invoice.paid_via_settlement_acceptance",
        details={"payment_id": str(payment.id), "coverage": coverage.state},
    )

    await db.commit()
    await db.refresh(payment)
    return PaymentResponse.from_db(payment, invoice)


@router.post("", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
async def create_payment(
    body: PaymentCreate,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # Recording a standalone payment moves money exactly like executing a run,
    # so it gates on the same splittable SoD permission — not bare roles. An org
    # that strips payment.execute from a custom role must not retain a back door
    # to book money here. (System roles resolve identically via the default map.)
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    # Verify invoice exists and has cleared approval. Recording a payment
    # against a pre-approval invoice (new/pending/ready_for_review/rejected/
    # failed) would book money against something nobody signed off on.
    #
    # Lock the invoice FOR UPDATE so two concurrent / double-clicked POSTs for
    # the same invoice serialize here: the first books the payment and commits,
    # the second blocks on this lock, then re-reads and returns the payment
    # already booked instead of creating a duplicate full-amount one. Without
    # the lock the idempotency check below is a non-atomic read→check→write that
    # a concurrent POST races through, booking a second payment (a real double-
    # pay with no audit distinction). The `uq_payments_one_live_per_invoice`
    # partial index (migration 0074) is the DB-level backstop for any path the
    # row lock can't cover (e.g. an overlapping payment run).
    #
    # The lookup is entity-scoped like every other by-id money route on this
    # router: with subsidiary A selected, an invoice belonging to subsidiary B
    # is the same opaque 404 as one that doesn't exist — booking a payment
    # against it would put A's operator on B's money.
    inv_result = await db.execute(
        apply_entity_scope(
            select(Invoice).where(Invoice.id == uuid.UUID(body.invoice_id)),
            Invoice,
            entity_id,
        ).with_for_update()
    )
    invoice = inv_result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status not in PAYABLE_INVOICE_STATUSES:
        raise HTTPException(status_code=409, detail="Invoice is not approved for payment")

    # Financial-integrity gate — the SAME one `POST /api/payments/runs` and
    # `/retry-failed` run, via the same shared helper so the three can't drift.
    # `PAYMENT_BLOCKING_EXCEPTION_TYPES` (duplicate / fraud_flag /
    # line_total_mismatch) are `error`-severity flags that invoice APPROVAL does
    # not gate on, so every path that books money has to re-check them —
    # otherwise an invoice the run path refuses with a 409 can be paid by
    # posting it here instead, which is exactly what this endpoint did. A
    # settlement-amount mismatch, a Positive Pay altered cheque and a BEC
    # bank-detail swap all land as `fraud_flag`; resolving or dismissing it is
    # the human sign-off.
    # Same wording rule as the run builder: name the type that actually blocked
    # this invoice, not a hardcoded list of causes that drifts the moment
    # `PAYMENT_BLOCKING_EXCEPTION_TYPES` grows a member (it already had).
    _blocking = await blocking_exception_types(db, [invoice.id])
    if _blocking:
        raise HTTPException(
            status_code=409,
            detail=(
                "Invoice has an unresolved payment-blocking exception "
                f"({_blocking[invoice.id]}) and can't be paid until it's cleared: "
                f"{invoice.invoice_number}"
            ),
        )

    # A live virtual card is already paying this invoice. Same gate the run
    # builder runs, through the same shared helper, so the run's refusal can't
    # be walked around by posting here instead — the reasoning the
    # financial-integrity exception gate above already documents. `virtual_card`
    # is exempt: that rail converges on the existing card.
    if await card_claimed_invoice_ids(
        db, [(invoice.id, body.method.value if body.method else None)]
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Invoice already has a live virtual card issued against it — pay it by "
                f"card, or cancel the card first: {invoice.invoice_number}"
            ),
        )

    # The payment amount is the invoice amount NET OF APPLIED CREDIT MEMOS —
    # never a caller-supplied value. Trusting `body.amount` let an actor book a
    # $99,999 payment against a $500 approved invoice, so the figure is bound
    # server-side; but binding it to the raw `invoice.amount` made this path
    # ignore credit memos entirely, paying the vendor the full pre-credit
    # amount AND 422ing the correct net figure if a caller tried to submit it.
    # `net_payable_amount` is the same helper the run builder uses, so the two
    # money paths can't disagree about what an invoice is worth.
    net_amount = await net_payable_amount(db, invoice)
    if net_amount <= 0:
        raise HTTPException(
            status_code=409,
            detail="Invoice is fully covered by applied credit memos — nothing to pay",
        )
    if body.amount is not None and Decimal(str(body.amount)) != net_amount:
        raise HTTPException(
            status_code=422,
            detail=(
                "Payment amount must equal the approved invoice amount net of applied credit memos"
            ),
        )

    # CFO sign-off gate — the same `payments.cfo_approval_above` threshold the
    # run-based path enforces (issue #129). A standalone payment has no
    # separate /execute step to gate the way a run does (requires_cfo_approval
    # lives on PaymentRun, not Payment), so an above-threshold amount must
    # clear CFO sign-off at CREATION time instead: only a CFO may book one
    # directly here; anyone else routes an above-threshold payment through a
    # payment run, which carries the full requires_cfo_approval / /approve
    # workflow. This is a structural close of the gap, not a reaction to a
    # live exploit — see the issue's severity note. Mirrors
    # create_payment_run's `requires_cfo` computation exactly (same threshold
    # setting, same fail-closed handling of a corrupted/unparseable value).
    #
    # The comparison is against the NET amount — the money that actually moves
    # — **expressed in the org's reporting currency**, exactly as
    # create_payment_run compares its credit-netted total. The threshold is a
    # bare number in that currency, so comparing a foreign-currency payable at
    # face value made the gate fail OPEN below it (a GBP 9,000 invoice slipping
    # under a USD 10,000 threshold). No FX call: the rate was locked onto the
    # invoice row when it was last saved, and a row we can't price fails closed.
    pmt_cfg = (org.settings or {}).get("payments") or {}
    reporting_currency = resolve_reporting_currency(org.settings)
    reporting_net, reporting_unconverted = reporting_amount_at_locked_rate(
        amount=net_amount,
        currency=invoice.currency,
        reporting_currency=reporting_currency,
        persisted_reporting_currency=invoice.reporting_currency,
        persisted_reporting_source_currency=invoice.reporting_source_currency,
        persisted_fx_rate=invoice.reporting_fx_rate,
    )
    cfo = cfo_approval_decision(
        payment_config=pmt_cfg,
        reporting_amount=reporting_net,
        reporting_currency=reporting_currency,
        unconverted=reporting_unconverted,
    )
    requires_cfo = cfo.required
    if cfo.reason == CFO_REASON_THRESHOLD_UNPARSEABLE:
        logger.error(
            "payments.cfo_approval_above is unparseable (%r) for org %s; "
            "requiring CFO sign-off on this standalone payment (fail-closed)",
            pmt_cfg.get("cfo_approval_above"),
            org.id,
        )
    elif cfo.reason == CFO_REASON_AMOUNT_NOT_EXPRESSIBLE:
        logger.warning(
            "standalone payment for org %s could not be expressed in %s (no locked "
            "rate on the invoice); requiring CFO sign-off (fail-closed)",
            org.id,
            reporting_currency,
        )

    if requires_cfo:
        has_cfo = any(r.name == ROLE_CFO for r in (user.roles or ()))
        if not has_cfo:
            raise HTTPException(
                status_code=403,
                detail=(
                    "This payment requires CFO sign-off — book it through a payment "
                    "run for CFO approval, or have a CFO create it directly"
                ),
            )

    # Idempotency guard: at most one LIVE payment per invoice. A retried or
    # double-clicked POST must not book a second payment — return the existing
    # live one instead of creating a duplicate. Terminal states don't count as
    # live (see LIVE_PAYMENT_TERMINAL_STATUSES).
    existing = await _find_live_payment(db, invoice.id)
    if existing is not None:
        return PaymentResponse.from_db(existing, invoice)

    payment = Payment(
        invoice_id=invoice.id,
        # Payment follows the invoice's entity (multi-entity Phase 2).
        entity_id=invoice.entity_id,
        amount=net_amount,
        method=body.method.value if body.method else None,
        reference=body.reference,
        # Always standalone — `payment_run_id` is deliberately not a request
        # field (see `schemas/payment.PaymentCreate`). A run stamps this FK on
        # the payments it creates itself.
        payment_run_id=None,
        correlation_id=uuid.uuid4(),
    )
    # Insert inside a savepoint so the DB-level unique index (the backstop for a
    # race the row lock can't serialize — e.g. an overlapping run booking a live
    # payment for the same invoice between our check and flush) surfaces as an
    # IntegrityError we recover from, returning the winning payment rather than
    # 500ing.
    try:
        async with db.begin_nested():
            db.add(payment)
            await db.flush()
    except IntegrityError:
        existing = await _find_live_payment(db, invoice.id)
        if existing is not None:
            return PaymentResponse.from_db(existing, invoice)
        raise

    # Append-only audit trail for the money-booking event. Every sibling money
    # handler (void_payment, create_payment_run, execute_payment_run) writes an
    # audit row; this standalone path was the only one that didn't. PII-free:
    # ids, the Decimal amount as a string, method, and reference only — no
    # bank/routing numbers, no PAN.
    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=payment.correlation_id or uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="payment.created",
        entity_type="payment",
        entity_id=payment.id,
        details={
            "invoice_id": str(invoice.id),
            "amount": str(payment.amount),
            "method": payment.method,
            "reference": payment.reference,
            "payment_run_id": str(payment.payment_run_id) if payment.payment_run_id else None,
        },
    )

    await db.refresh(payment)
    return PaymentResponse.from_db(payment, invoice)


# ── Payment Runs ─────────────────────────────────────────────────────


async def _run_currencies(
    db: AsyncSession, run_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str | None]:
    """{run id: its one currency} for several runs, in ONE grouped query.

    The list endpoint's counterpart to
    :func:`~app.services.payment_runs.one_currency`, which it applies —
    never a lookup per run. A run id absent from the result (no payments at all)
    reads as ``None`` at the call site's ``.get``.
    """
    if not run_ids:
        return {}

    rows = await db.execute(
        select(Payment.payment_run_id, func.upper(Invoice.currency))
        .outerjoin(Invoice, Payment.invoice_id == Invoice.id)
        .where(Payment.payment_run_id.in_(run_ids))
        .distinct()
    )
    seen: dict[uuid.UUID, list[str | None]] = {}
    for run_id, code in rows.all():
        seen.setdefault(run_id, []).append(code)

    return {rid: one_currency(codes) for rid, codes in seen.items()}


@router.get("/runs/", response_model=PaymentRunListResponse)
async def list_payment_runs(
    pagination: PaginationParams = Depends(pagination_params),
    status_filter: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_tenant_db),
    # The Runs tab a `payment.execute` holder needs to REACH a draft run's
    # RunDetailModal (where the Execute button lives). Exact match: default
    # holders are ADMIN, AP_MANAGER, CFO — the same set `require_roles`
    # granted, AP_CLERK excluded either way.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    query = apply_entity_scope(select(PaymentRun), PaymentRun, entity_id)

    if status_filter:
        statuses = [s.strip() for s in status_filter.split(",")]
        query = query.where(PaymentRun.status.in_(statuses))

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # `.id` tie-breaker: same fix as the sibling payment list above.
    query = query.order_by(PaymentRun.created_at.desc(), PaymentRun.id.desc())
    query = query.offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(query)
    runs = result.scalars().all()

    # Per-run outcome tallies in ONE grouped query, not a count per run (this
    # was an N+1). The rollup is what makes a `partial` row actionable in the
    # list — "3 of 12 failed" instead of a bare status word.
    run_ids = [run.id for run in runs]
    per_run: dict[uuid.UUID, list[str | None]] = {rid: [] for rid in run_ids}
    if run_ids:
        # Exclude any attempt a later retry on these runs superseded, matching
        # `services/payment_runs.active_run_payments` (the run detail and the
        # dispatcher's own rollup both go through it). Expressed as a subquery
        # rather than in Python so this stays the single grouped query it was
        # built to be, not an N+1.
        superseded_q = select(Payment.retry_of_payment_id).where(
            Payment.payment_run_id.in_(run_ids),
            Payment.retry_of_payment_id.isnot(None),
        )
        status_rows = await db.execute(
            select(Payment.payment_run_id, Payment.status, func.count())
            .where(
                Payment.payment_run_id.in_(run_ids),
                Payment.id.notin_(superseded_q),
            )
            .group_by(Payment.payment_run_id, Payment.status)
        )
        for pay_run_id, pay_status, n in status_rows.all():
            per_run[pay_run_id].extend([pay_status] * int(n))

    # The currency each `total_amount` is denominated in, resolved in one more
    # grouped query rather than folded into the rollup above — the rollup
    # excludes superseded retry attempts, and a run's currency is a property of
    # its invoices regardless of which attempt is live.
    currencies = await _run_currencies(db, run_ids)

    items = []
    for run in runs:
        rollup = rollup_payment_statuses(per_run.get(run.id, ()))
        # Report the status the run's own payments support, not the one the
        # dispatcher happened to persist at the end of its last pass — nothing
        # rewrites `PaymentRun.status` when a webhook, the reconciler or
        # `/compliance/{release,dismiss}` moves a payment afterwards. See
        # `payment_runs.derive_run_status`.
        run.status = derive_run_status(run.status, rollup)
        items.append(
            PaymentRunResponse.from_db(
                run,
                rollup.total,
                completed=rollup.completed,
                failed=rollup.failed,
                in_flight=rollup.in_flight,
                pending=rollup.pending,
                currency=currencies.get(run.id),
            )
        )

    return PaymentRunListResponse(
        items=items,
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


class CreatePaymentRunItem(BaseModel):
    invoice_id: str
    method: str = "ach"  # ach, wire, check, virtual_card


class CreatePaymentRunRequest(BaseModel):
    items: list[CreatePaymentRunItem] = Field(..., min_length=1)


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def create_payment_run(
    body: CreatePaymentRunRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # SoD-splittable: creating (approving) a payment run is the gate before
    # execution. Defaults map to admin/ap_manager/cfo (unchanged); a custom role
    # can be granted run-approval WITHOUT execution, and vice versa.
    user: User = Depends(require_permission(PERM_PAYMENT_RUN_APPROVE)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
    # The SELECTED entity (nullable — `None` is the consolidated view), kept
    # separate from the write entity above: it is what the invoice lookup is
    # filtered by, so a run staged with subsidiary B selected can't pull in
    # subsidiary A's invoices. Same split `POST /api/payments` uses.
    scope_entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Create a payment run from selected invoices.

    The validation + creation gates (payable-status, the financial-integrity
    exception block, credit-memo netting, the CFO-approval threshold, and the
    `uq_payments_one_live_per_invoice` idempotency backstop) live in
    `services.payment_runs.create_payment_run_for_invoices` — shared verbatim
    with the AI Cash-Flow Copilot's draft-run enact route
    (`POST /api/cash-flow/plans/{plan_id}/draft-run`) so the two can never
    diverge on what counts as a legitimate run.
    """
    items = [
        PaymentRunItemInput(invoice_id=uuid.UUID(item.invoice_id), method=item.method)
        for item in body.items
    ]
    result = await create_payment_run_for_invoices(
        db,
        org=org,
        org_id=org_id,
        entity_id=entity_id,
        scope_entity_id=scope_entity_id,
        user=user,
        items=items,
    )
    await db.commit()

    run = result.run
    # The run's own currency, read off the invoices `create_payment_run_for_invoices`
    # has just proven share exactly one. The confirmation message used to hardcode
    # a dollar sign in front of the total — a symbol nobody had established, on the
    # response of the very call that stages the money.
    currency = (await _run_currencies(db, [run.id])).get(run.id)
    total = f"{result.total_amount:,.2f}"
    return {
        "id": str(run.id),
        "status": run.status,
        # Money serialises as an exact Decimal STRING, never float().
        "total_amount": str(result.total_amount),
        # `None` where it cannot be proven; the client renders the bare figure
        # rather than a guessed code (`docs/decisions.md` §79/§82).
        "currency": currency,
        "payment_count": result.payment_count,
        "requires_cfo_approval": run.requires_cfo_approval,
        "message": (
            f"Payment run created with {result.payment_count} payments totaling "
            + (f"{total} {currency}" if currency else total)
            + (" (CFO approval required)" if run.requires_cfo_approval else "")
        ),
    }


@router.get("/runs/{run_id}")
async def get_payment_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    # `RunDetailModal.svelte` fetches this on open — it's the load-bearing
    # read that puts the Execute button (itself gated on `payment.execute`)
    # on screen at all. Same exact-match reasoning as the list above.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Get a payment run with its individual payments.

    Each payment carries its own `failure_reason` (plus `provider` and the two
    lifecycle timestamps) and the run carries the per-outcome rollup, so a
    `partial` run explains itself: which payments failed, and why. Before this,
    the counts existed only in the transient response of the `/execute` call
    that produced them and `failure_reason` never left the database — a reload
    lost both, and the operator's only recourse was the server log.
    """
    run = await _get_scoped_run(db, run_id, entity_id)

    # Get payments in this run with invoice details
    pay_result = await db.execute(
        select(Payment, Invoice)
        .outerjoin(Invoice, Payment.invoice_id == Invoice.id)
        .where(Payment.payment_run_id == run_id)
        .order_by(Payment.created_at.asc())
    )
    rows = pay_result.all()
    payments = [
        {
            "id": str(p.id),
            "invoice_id": str(p.invoice_id),
            "invoice_number": inv.invoice_number if inv else None,
            "vendor_name": inv.vendor_name if inv else None,
            # Money serialises as an exact Decimal STRING, never float().
            "amount": str(p.amount),
            # What `amount` is denominated in — the invoice's own currency, off
            # the row already joined above. `None` where the invoice carries
            # none; the client renders the bare figure rather than a guessed
            # code (`docs/decisions.md` §79/§82).
            "currency": (inv.currency.upper() if inv is not None and inv.currency else None),
            "method": p.method,
            "status": p.status,
            "reference": p.reference,
            "provider": p.provider,
            "failure_reason": p.failure_reason,
            "submitted_at": p.submitted_at.isoformat() if p.submitted_at else None,
            "completed_at": p.completed_at.isoformat() if p.completed_at else None,
        }
        for p, inv in rows
    ]
    # The rollup counts the LATEST attempt per invoice — a superseded retry
    # attempt is still listed above (an operator has to be able to see that an
    # invoice took two goes) but must not be counted twice, or a fully
    # recovered run would read `partial` forever.
    active = active_run_payments(p for p, _ in rows)
    rollup = rollup_payment_statuses(p.status for p in active)

    return {
        "id": str(run.id),
        # Derived from the run's ACTIVE payments (see the list endpoint) so the
        # status, the counts and `retryable_failures` below can't contradict
        # each other — a run reporting `submitted` with `retryable_failures: 1`
        # offered a retry button that `/retry-failed` then 409ed.
        "status": derive_run_status(run.status, rollup),
        # Money serialises as an exact Decimal STRING, never float().
        "total_amount": str(run.total_amount) if run.total_amount else "0",
        # What `total_amount` is denominated in, derived from the same rows the
        # payments list above was built from rather than a second query. `None`
        # where the run's legs disagree or carry no currency at all — see
        # `_run_currencies`.
        "currency": one_currency(
            (inv.currency.upper() if inv is not None and inv.currency else None) for _, inv in rows
        ),
        "initiated_by": str(run.initiated_by) if run.initiated_by else None,
        "executed_at": run.executed_at.isoformat() if run.executed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "requires_cfo_approval": run.requires_cfo_approval,
        "cfo_approved_by": str(run.cfo_approved_by) if run.cfo_approved_by else None,
        "cfo_approved_at": run.cfo_approved_at.isoformat() if run.cfo_approved_at else None,
        "payment_count": rollup.total,
        "payments_completed": rollup.completed,
        "payments_failed": rollup.failed,
        "payments_in_flight": rollup.in_flight,
        "payments_pending": rollup.pending,
        # Failures `/runs/{id}/retry-failed` will ACTUALLY re-attempt — what
        # the retry button gates on. A failure we can't prove never reached the
        # processor (`is_retry_safe`) is excluded: the endpoint would only skip
        # it as `needs_reconciliation`, and offering a button that can't act is
        # how an operator ends up hunting for a second way to force the payment.
        "retryable_failures": sum(
            1 for p in active if p.status in RETRYABLE_PAYMENT_STATUSES and is_retry_safe(p)
        ),
        "payments": payments,
    }


@router.post("/runs/{run_id}/approve")
async def approve_payment_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # Deliberately NOT `require_permission(PERM_PAYMENT_RUN_APPROVE)`, unlike
    # its sibling `POST /runs` (create). That permission is admin/ap_manager/
    # cfo by default — fine for creating a draft run, but `requires_cfo_approval`
    # exists specifically to force a genuine CFO signature above the org's
    # dollar threshold; granting admin or ap_manager the same authority here
    # defeats the control entirely (they may be the same person who created or
    # will execute the run). A prior round migrated this to the shared
    # permission on a false-consistency reading of the two endpoints and it
    # regressed exactly that — an admin/ap_manager could sign off a run above
    # threshold, caught by `tests-e2e/payments/cfo-approval.spec.ts` and
    # `tests-e2e/auth/rbac-api.spec.ts`. A custom role CAN still be granted
    # this specific sign-off without the full CFO title — just not via the
    # same catalog entry `POST /runs` uses; that would need a distinct
    # permission (e.g. `payment_run.cfo_signoff`) if ever wanted.
    user: User = Depends(require_roles(ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """CFO sign-off on a draft run. Only valid from `draft` AND
    `requires_cfo_approval=True`. After this lands, /execute will accept
    the run from any actor with the standard payments role set."""
    # Row-lock the run: two concurrent CFO approvals both read
    # cfo_approved_at=None, both pass the guards, and both commit — last writer
    # wins cfo_approved_by and a duplicate `payment_run.cfo_approved` audit row
    # lands, breaking non-repudiation of the money-control gate. The lock
    # serialises them so the second sees the first's commit and 409s.
    run = await _get_scoped_run(db, run_id, entity_id, for_update=True)
    if run.status != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"Can only approve 'draft' runs, not '{run.status}'",
        )
    if not run.requires_cfo_approval:
        raise HTTPException(
            status_code=409,
            detail="This run does not require CFO approval",
        )
    if run.cfo_approved_at is not None:
        raise HTTPException(status_code=409, detail="Run is already CFO-approved")
    # Maker-checker: the user who created the run cannot also sign it off — a
    # self-approval defeats the entire purpose of the CFO gate.
    check_run_segregation(
        run.initiated_by,
        user.id,
        (org.settings or {}).get("payments"),
        action="approve",
    )

    now = datetime.now(UTC)
    run.cfo_approved_by = user.id
    run.cfo_approved_at = now

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="payment_run.cfo_approved",
        entity_type="payment_run",
        entity_id=run.id,
        details={"total_amount": str(run.total_amount or Decimal("0"))},
    )
    await db.commit()
    return {
        "id": str(run.id),
        "status": run.status,
        "cfo_approved_by": str(run.cfo_approved_by),
        "cfo_approved_at": run.cfo_approved_at.isoformat(),
        "message": "Run approved by CFO",
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_payment_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Cancel a draft run before it executes. Only valid from `draft`;
    flips the run to `cancelled` and removes its child payment rows so
    the invoices return to the queue. Audit-logged."""
    # Row-lock the run, exactly like /approve, /execute and /resume. This one
    # DELETES the child Payment rows, so an unlocked read is the worst version
    # of the race: /execute takes the lock, flips the run to `executing`,
    # commits (releasing it) and starts handing payments to the processor —
    # while a /cancel that read `draft` before any of that proceeds to delete
    # the very rows being dispatched. Real money then moves against rows that
    # no longer exist, under a run that reads `cancelled`. With the lock the
    # canceller blocks until /execute commits, re-reads `executing`, and 409s
    # before deleting anything.
    run = await _get_scoped_run(db, run_id, entity_id, for_update=True)
    if run.status != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"Can only cancel 'draft' runs, not '{run.status}'",
        )

    pay_result = await db.execute(select(Payment).where(Payment.payment_run_id == run_id))
    payments = pay_result.scalars().all()
    invoice_ids = [p.invoice_id for p in payments]

    # Drop the placeholder payment rows so the invoices re-enter the queue.
    # The run itself stays in the table for history; status flips to
    # `cancelled` so list filters can exclude it without losing the audit
    # trail.
    for p in payments:
        await db.delete(p)
    run.status = "cancelled"

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="payment_run.cancelled",
        entity_type="payment_run",
        entity_id=run.id,
        details={
            "invoice_ids": [str(i) for i in invoice_ids],
            "payment_count": len(payments),
            "total_amount": str(run.total_amount or Decimal("0")),
        },
    )
    await db.commit()

    return {
        "id": str(run.id),
        "status": run.status,
        "released_invoices": len(invoice_ids),
        "message": (f"Draft run cancelled; {len(invoice_ids)} invoice(s) returned to the queue."),
    }


async def _open_compliance_hold_exception(
    db: AsyncSession,
    *,
    payment: Payment,
    invoice: Invoice | None,
    org: Organization,
) -> None:
    """Surface a `pending_compliance` payment in the Exceptions queue.

    `check_payment_compliance`'s own docstring promises a hold "opens an
    exception for AP review" — until this, none of the four call sites that
    set `payment.status = "pending_compliance"` actually did, so a held
    payment was invisible everywhere except its own `failure_reason` field.
    Dedupes on `(invoice_id, "payment_compliance_hold", "open")`: a payment
    without a screenable vendor can be re-dispatched (e.g. by /resume) and
    hit the same hold repeatedly, and `uq_payments_one_live_per_invoice`
    means at most one live payment exists per invoice at a time, so an
    invoice-scoped dedupe is equivalent to a payment-scoped one.
    """
    if invoice is None:
        return
    from app.services.exception_service import create_exception

    existing = await db.execute(
        select(APException.id).where(
            APException.invoice_id == invoice.id,
            APException.exception_type == "payment_compliance_hold",
            APException.status == "open",
        )
    )
    if existing.scalar_one_or_none() is not None:
        return
    await create_exception(
        db,
        exception_type="payment_compliance_hold",
        description=payment.failure_reason,
        organization_id=org.id,
        severity="error",
        invoice=invoice,
        # The sanctions/KYC screening verdict raised this, not the operator who
        # dispatched the payment — and the four call sites include the
        # unattended `/resume` and retry paths. Holding the executor responsible
        # for a screening result would bar whoever runs payments from clearing
        # holds they did not cause.
        raised_by_user_id=None,
    )


async def _capture_discount_offers(
    db: AsyncSession,
    *,
    org: Organization,
    payment: Payment,
    actor_id: uuid.UUID | None,
    now: datetime,
    invoice: Invoice | None = None,
) -> None:
    """Recognize `payment` settling its invoice at a discounted payoff and
    capture any matching `accepted` `DiscountOffer` — the wiring
    `discount_offers.mark_captured` was missing (issue #280): without a
    caller, `captured_amount`/`captured_at` never got set and the
    captured-savings KPI always read 0 even when discounts were genuinely
    accepted and paid at the discounted amount.

    Called from every path a `Payment` reaches `completed` — the synchronous
    adapter/card leg (`_execute_single_payment`) and the async webhook-driven
    completion (`payment_webhook`) — so a discount is recognized whether the
    rail confirms instantly or days later. Both callers already hold the
    `Invoice` and pass it in; `invoice=None` falls back to resolving it from
    `payment.invoice_id` for any future caller that doesn't. No invoice found
    is a no-op (nothing to match against); a payment amount that doesn't
    match a discounted payoff exactly is also a no-op (see
    `discount_capture.capture_offers_for_settled_payment`).

    NOT called when the settlement verifier flagged a discrepancy — the
    payoff match runs against OUR authorized amount, which a divergent
    settlement has just contradicted. See the call site in `payment_webhook`.

    Best-effort, like the vendor card-notify email below: this labels a
    payment that already, definitely settled with a bookkeeping fact
    (realized discount savings) — it must never be the reason a payment
    that DID move money fails to record that it moved (or, on the webhook
    path, the reason a webhook delivery 5xxs and gets needlessly retried).
    The invoice lookup lives INSIDE the try for exactly that reason. A
    failure here is logged (class only — no invoice/vendor PII) and
    swallowed rather than propagated, mirroring `notify_vendor_of_card`'s
    own safety net.
    """
    try:
        if invoice is None:
            inv_result = await db.execute(select(Invoice).where(Invoice.id == payment.invoice_id))
            invoice = inv_result.scalar_one_or_none()
        if invoice is None:
            return

        from app.services.discount_capture import capture_offers_for_settled_payment

        captured = await capture_offers_for_settled_payment(
            db,
            invoice_id=invoice.id,
            payment_amount=payment.amount,
            invoice_currency=invoice.currency,
            now=now,
        )
        if not captured:
            return

        from app.services.audit_dispatch import dispatch_audit

        for offer in captured:
            await dispatch_audit(
                db,
                correlation_id=payment.correlation_id or invoice.id,
                organization_id=org.id,
                actor_id=actor_id,
                action="discount_offer.captured",
                entity_type="discount_offer",
                entity_id=offer.id,
                details={
                    "invoice_id": str(invoice.id),
                    "payment_id": str(payment.id),
                    "captured_amount": str(offer.captured_amount),
                },
            )
    except Exception as exc:  # noqa: BLE001
        # Log the exception CLASS only, never the message (PII-out-of-logs
        # invariant — mirrors payment_erp_sync.py's own discipline). Never
        # `payment.invoice_id` either — best-effort but still no PII risk.
        logger.warning(
            "discount-offer capture failed for payment=%s: %s; payment settlement unaffected",
            payment.id,
            exc.__class__.__name__,
        )


async def _execute_single_payment(
    db: AsyncSession,
    *,
    payment: Payment,
    org: Organization,
    adapter,
    user: User,
    now: datetime,
) -> None:
    """Dispatch ONE payment to its processor (or the card adapter), mutating
    it to a terminal or in-flight status in place.

    Extracted out of the `execute_payment_run` loop so each payment can be
    committed durably right after this call returns (see the caller) — a
    problem with payment N (including this raising) must only ever affect
    payment N, never roll back payments the loop already committed earlier.
    """
    # Resolve invoice + vendor for the payload
    inv_result = await db.execute(select(Invoice).where(Invoice.id == payment.invoice_id))
    invoice = inv_result.scalar_one_or_none()

    if invoice is None:
        # No invoice behind this payment. Every gate below — the credit-memo
        # re-check, the FX rate lock, and the ENTIRE sanctions/KYC compliance
        # gate — is written `if invoice is not None`, so an invoice-less
        # payment used to fall straight through to `adapter.create_payment`
        # with an empty `invoice_number` and `vendor_name`: money moving to a
        # payee nobody screened, priced at a rate nobody locked, for an amount
        # nobody re-verified. That is the exact inverse of the two
        # carefully-argued "no screenable vendor → hold, never pay unscreened"
        # branches below. Fail closed instead. Refused BEFORE the adapter is
        # called, so no order exists at the processor.
        payment.status = "failed"
        payment.failure_reason = "invoice_missing: cannot verify payee, amount or compliance"
        payment.completed_at = now
        return

    # Is the invoice still PAYABLE now, immediately before the adapter call?
    #
    # The run was built against `PAYABLE_INVOICE_STATUSES`, but nothing freezes
    # the invoice between booking and dispatch: `POST /api/invoices/{id}/send-to-erp`
    # happily walks an invoice holding a `pending` run payment
    # `approved → sending_to_erp → sent_to_erp`, and `sent_to_erp` can only
    # advance to `posted_in_erp` / `done` — `payment_scheduled` is NOT a legal
    # successor (`workflow_engine.VALID_TRANSITIONS`).
    #
    # Without this guard the mismatch surfaced in the worst possible place: the
    # `transition_invoice` call sits AFTER `adapter.create_payment` returned and
    # `provider_payment_id` was assigned, so `validate_transition`'s 409 unwound
    # into `_dispatch_run_payments`' generic `except`, which recorded
    # `failed / unexpected_error:HTTPException` on a payment the processor had
    # already accepted. `classify_payment_failure` then read the populated
    # `provider_payment_id` as IN_DOUBT (correctly — `/retry-failed` must not
    # re-send), the webhook refuses to advance an already-terminal payment and
    # the reconciler only polls `submitted`/`processing`, so nothing ever
    # corrected it: the money moved and no surface said so.
    #
    # Refusing HERE — before any order exists at the processor — turns that into
    # a named, retry-safe refusal (`invoice_not_payable:<status>`), exactly like
    # the `net_amount_changed` guard below. A fresh run re-derives the payment
    # once the ERP push completes (`posted_in_erp` is payable).
    if invoice.status.value not in PAYABLE_INVOICE_STATUSES:
        payment.status = "failed"
        payment.failure_reason = f"invoice_not_payable:{invoice.status.value}"
        payment.completed_at = now
        return

    # A payment-blocking exception raised AFTER the run was built must stop
    # dispatch. `create_payment_run_for_invoices` refuses `duplicate` /
    # `fraud_flag` / `line_total_mismatch` / `payment_reconciliation` at
    # creation, but nothing freezes the invoice while a draft run waits for CFO
    # sign-off or a payment sits `pending_compliance` — and the single sharpest
    # case is an approved BEC bank-detail swap, which raises a `fraud_flag`
    # ("Vendor bank details changed; verify before payment") and whose new
    # `Vendor.bank_details` this function then re-reads two blocks down.
    # `/retry-failed` already re-runs this gate before a days-later re-send; so
    # must `/execute`, `/resume` and `/compliance/release`. Same shared
    # predicate (`blocking_exception_types`), refused BEFORE the adapter call so
    # it is retry-safe and a fresh run re-derives the payment once the human
    # clears the flag.
    _blocked = await blocking_exception_types(db, [invoice.id])
    if invoice.id in _blocked:
        payment.status = "failed"
        payment.failure_reason = f"invoice_blocked:{_blocked[invoice.id]}"
        payment.completed_at = now
        return

    # A live virtual card minted for this invoice since the run was built claims
    # it on a rail this payment isn't using (`card_claimed_invoice_ids` returns
    # nothing for a `virtual_card` payment, which legitimately CONVERGES on that
    # card). Paying now on any other rail is a second, independent outflow —
    # the same gate the run builder and `/retry-failed` run.
    if await card_claimed_invoice_ids(db, [(invoice.id, payment.method)]):
        payment.status = "failed"
        payment.failure_reason = "invoice_has_live_card"
        payment.completed_at = now
        return

    # What the invoice is worth NOW, immediately before the adapter call.
    # `payment.amount` was netted against applied credit memos when the row was
    # booked (`payment_runs.net_payable_amount`), but `credit_memos.py` gates an
    # application on neither invoice status nor an existing payment — so a
    # credit recorded between booking and dispatch (a run sitting `draft`
    # awaiting CFO sign-off, a payment held `pending_compliance`) leaves the
    # row's amount stale and would overpay the vendor by the credit. That
    # window is not hypothetical: `docs/dynamic-discounting.md` documents
    # recording a credit memo as THE way to take an early-pay discount.
    #
    # The amount is never silently adjusted here — re-pricing money nobody
    # re-approved is its own defect — so refuse and let a fresh run re-derive
    # it through the full gate set. This mirrors `/retry-failed`'s
    # `net_amount_changed` skip exactly, and is a refusal made BEFORE the
    # adapter is called, hence retry-safe (`_RETRY_SAFE_FAILURE_PREFIXES`).
    if invoice is not None:
        from app.services.payment_runs import net_payable_amount as _net_payable_amount

        current_net = await _net_payable_amount(db, invoice)
        if current_net != payment.amount:
            payment.status = "failed"
            payment.failure_reason = "net_amount_changed"
            payment.completed_at = now
            return

    vendor_bank: dict | None = None
    if invoice and invoice.vendor_id:
        v_result = await db.execute(
            select(Vendor.bank_details).where(Vendor.id == invoice.vendor_id)
        )
        vendor_bank = v_result.scalar_one_or_none()

    # Virtual-card method: skip the payment adapter and mint a card
    # via the card adapter instead. The Payment row still settles
    # locally — the rebate flow runs off VirtualCard webhooks, not
    # the payment status.
    if payment.method == "virtual_card" and invoice is not None:
        from app.config import settings as app_settings
        from app.services.card_issuance import (
            card_settlement_block,
            find_live_card_for_invoice,
            issue_card_for_invoice,
            notify_vendor_of_card,
            persist_card,
        )

        # Issuing a virtual card moves money just like an ACH/wire, so the
        # same compliance gate applies: a blocked / sanctioned vendor must
        # not receive a card. Refuse outright; a review-hold leaves the
        # payment in pending_compliance for AP (no card minted).
        from app.services.compliance import check_payment_compliance

        v_card = None
        if invoice.vendor_id:
            v_card = (
                await db.execute(select(Vendor).where(Vendor.id == invoice.vendor_id))
            ).scalar_one_or_none()
        if v_card is None:
            # No screenable vendor (invoice never matched a Vendor, or the
            # row was deleted). We cannot run sanctions/KYC against a payee
            # we don't have — and an AI-extracted / email-intake invoice can
            # reach here with vendor_id NULL. Minting a card anyway would put
            # funds on a card for an unscreened name, defeating the gate.
            # Fail-safe: hold for AP to attach + verify a vendor (mirrors the
            # ACH/wire leg); never mint a card unscreened.
            payment.status = "pending_compliance"
            payment.failure_reason = "compliance_hold: no screenable vendor on invoice"
            await _open_compliance_hold_exception(db, payment=payment, invoice=invoice, org=org)
            return
        card_decision = await check_payment_compliance(
            db,
            vendor=v_card,
            # The home-currency leg when an FX rate was locked, else the
            # invoice-currency amount tagged with its own currency — the gate
            # fails closed when that isn't the threshold's currency.
            payment_amount=payment.source_amount or payment.amount,
            payment_currency=(
                payment.source_currency
                if payment.source_amount is not None
                else (invoice.currency if invoice is not None else None)
            ),
            payment_method=payment.method,
            org_settings=org.settings or {},
            organization_id=org.id,
            correlation_id=payment.correlation_id,
        )
        if card_decision.verdict == "refuse":
            payment.status = "failed"
            payment.failure_reason = "compliance_refusal: " + "; ".join(card_decision.reasons)
            payment.completed_at = now
            return
        if card_decision.verdict == "hold":
            payment.status = "pending_compliance"
            payment.failure_reason = "compliance_hold: " + "; ".join(card_decision.reasons)
            await _open_compliance_hold_exception(db, payment=payment, invoice=invoice, org=org)
            return

        # Idempotency pre-check, mirroring the batch `/api/cards/generate` leg:
        # an invoice can already hold a LIVE card (minted there, or by a
        # concurrent payment run). `uq_virtual_cards_one_live_per_invoice`
        # would reject a second one anyway — but only AFTER the provider had
        # already minted a real, separately-spendable card, orphaning it. Skip
        # the provider entirely and converge on the card that already pays this
        # invoice.
        card = await find_live_card_for_invoice(db, invoice.id)
        minted = False
        if card is None:
            issue = await issue_card_for_invoice(
                db=db,
                invoice=invoice,
                organization_id=org.id,
                org_settings=org.settings or {},
                app_settings=app_settings,
                payment_id=payment.id,
                amount=payment.amount,
            )
            if not issue.success or issue.card is None:
                payment.status = "failed"
                payment.failure_reason = issue.failure_reason or "card_issuance_failed"
                payment.completed_at = now
                return
            # Savepoint-guarded flush (we need card.id for the reveal-token
            # row). A racer that committed the invoice's live card between the
            # pre-check and here trips the unique index; containing that in a
            # savepoint keeps THIS transaction usable, so the dispatch loop can
            # still write its audit row and commit the payment.
            if await persist_card(db, issue.card):
                card = issue.card
                minted = True
            else:
                # Lost the race. Both racers derive the SAME provider
                # idempotency key from the invoice, so the winner's row is the
                # same provider card ours would have been — adopt it.
                card = await find_live_card_for_invoice(db, invoice.id)

        if card is None:
            # The live-card slot was contended and is now empty (the winner
            # cancelled its card between our flush and this re-read). Don't
            # guess — surface it for AP rather than silently retry the provider.
            payment.status = "failed"
            payment.failure_reason = "card_issuance_conflict"
            payment.completed_at = now
            return

        if not minted:
            # Converging marks this payment `completed` — money moved. Only do
            # that against a card that can actually be what moved it (unspent,
            # and big enough). See `card_settlement_block`.
            block = card_settlement_block(card, payment.amount)
            if block is not None:
                payment.status = "failed"
                payment.failure_reason = block
                payment.completed_at = now
                return
            # Link the card to THIS payment when nothing else owns it (a card
            # from `POST /api/cards/generate` carries no payment_id). The
            # payments list resolves a row's card via
            # `VirtualCard.payment_id == Payment.id`, so without this the UI
            # shows no card on a converged payment whose reference says
            # `CARD-…`. Never re-point a card that already names another
            # payment — that payment is live and the link is its badge.
            if card.payment_id is None:
                card.payment_id = payment.id

        payment.status = "completed"
        payment.provider = card.card_provider
        payment.completed_at = now
        payment.submitted_at = now
        payment.reference = f"CARD-{card.card_provider.upper()}-{card.last_four or '????'}"
        await _capture_discount_offers(
            db, org=org, invoice=invoice, payment=payment, actor_id=user.id, now=now
        )
        if invoice.status.value in SCHEDULABLE_INVOICE_STATUSES:
            await transition_invoice(
                db,
                invoice,
                InvoiceStatus.payment_scheduled,
                actor_id=user.id,
                action_name="invoice.card_payment_scheduled",
                details={"payment_id": str(payment.id)},
            )

        # SOX trail for the card itself. `card.generated` matches the batch
        # endpoint so a card-lifecycle query (entity_type=virtual_card,
        # entity_id=card.id) shows a creation event on BOTH mint paths, not just
        # later webhook rows; `card.reused` records that this payment settled
        # against a card it did not mint, which an auditor reconciling the run
        # would otherwise have to infer from timestamps. Both PII-free — ids,
        # last four, and the exact amount as a string; never the PAN.
        from app.services.audit_dispatch import dispatch_audit

        await dispatch_audit(
            db,
            correlation_id=payment.correlation_id or invoice.correlation_id or uuid.uuid4(),
            organization_id=org.id,
            actor_id=user.id,
            action="card.generated" if minted else "card.reused",
            entity_type="virtual_card",
            entity_id=card.id,
            details={
                "invoice_id": str(invoice.id),
                "payment_id": str(payment.id),
                "last_four": card.last_four,
                "amount": str(payment.amount),
            },
        )

        if not minted:
            # The vendor was already emailed a reveal link when this card was
            # minted; a second one would mint a second single-use token for the
            # same card and confuse the supplier. Notify on a fresh mint only.
            return

        # Best-effort vendor notification — single-use reveal
        # link emailed to the vendor's contact address.
        try:
            await notify_vendor_of_card(
                db,
                card=card,
                invoice=invoice,
                org_name=org.name,
                # Already resolved here (per-org vanity host, else the global
                # template) so the card-reveal link lands on the same host as
                # every other tenant link.
                public_url_template=tenant_base_url(org.slug, org.settings),
            )
        except Exception:  # noqa: BLE001
            # `notify_vendor_of_card` already swallows known
            # failures; this catch is the safety net for the
            # "the email path raised before the function could
            # log" edge case. Card issuance itself is committed.
            pass
        return

    # International leg: if the invoice's currency isn't the org's
    # home currency (or the payment was already prepared via
    # prepare_international_payment), lock an FX rate before the
    # adapter call and persist the source-side outflow + rate on
    # the row. The corridor lookup also decides whether the row
    # needs to flip to `sepa` / `international_wire`.
    # Both codes come through the ONE normaliser
    # (`international_payments.resolve_home_currency` /
    # `normalize_currency_code`). This site only upper-cased, while the KYC gate
    # and the reporting rollup stripped as well, so a trailing space in the
    # tenant's own `settings.payments.home_currency` made `"USD "` != `"USD"`
    # here and routed EVERY domestic payment down the international leg — a
    # cross-border corridor, a locked FX rate on the row, and a KYC gate reading
    # a different value for the same setting.
    invoice_currency = normalize_currency_code(invoice.currency if invoice else None) or "USD"
    org_home_currency = resolve_home_currency(org.settings)
    has_intl_bank_fields = bool(
        vendor_bank and (vendor_bank.get("iban") or vendor_bank.get("swift_bic"))
    )
    if (
        invoice is not None
        and (
            invoice_currency != org_home_currency
            # The rail set is NOT restated here — `is_international_payment`
            # owns it (via `services/payment_methods`), so this gate, the
            # corridor selector's override check and the compliance KYC
            # threshold can't drift apart when a rail is added.
            or is_international_payment(payment)
            or has_intl_bank_fields
        )
        and payment.fx_rate is None  # not already prepared
    ):
        from app.services.fx_adapters import UnknownFxProviderError, get_fx_adapter
        from app.services.international_payments import (
            InternationalPaymentError,
            prepare_international_payment,
        )

        v_for_corridor = SimpleNamespace(
            bank_details=vendor_bank or {},
            address_country=getattr(invoice, "vendor_country", None),
        )
        fx_cfg = (org.settings or {}).get("fx") or {}
        try:
            fx_adapter = get_fx_adapter(fx_cfg)
        except UnknownFxProviderError:
            # Fail the payment rather than lock a rate we can't source. The
            # rate is written once onto the row and never re-fetched, so a
            # fabricated one silently mis-prices the outflow forever — see
            # `fx_adapters.dispatcher`. The provider name is not echoed here:
            # `failure_reason` is surfaced to every AP user, not just the
            # admin who owns the setting.
            payment.status = "failed"
            payment.failure_reason = "fx_provider_unsupported"
            payment.completed_at = now
            return
        try:
            prepared = await prepare_international_payment(
                invoice=invoice,
                vendor=v_for_corridor,
                org_home_currency=org_home_currency,
                fx_adapter=fx_adapter,
                requested_method=payment.method,
            )
        except InternationalPaymentError as exc:
            payment.status = "failed"
            payment.failure_reason = f"international_payment_error: {exc}"
            payment.completed_at = now
            return

        payment.method = prepared.corridor.method
        payment.source_currency = prepared.payment.source_currency
        payment.source_amount = prepared.payment.source_amount
        payment.fx_rate = prepared.payment.fx_rate
        payment.fx_locked_at = prepared.payment.fx_locked_at
        payment.corridor = prepared.payment.corridor
        payment.target_country = prepared.payment.target_country

    # Compliance gate: run sanctions / KYC / AML checks against the
    # vendor + the resolved corridor BEFORE the adapter call. This runs
    # for EVERY rail (domestic ACH / wire / check as well as the
    # international leg above) — the sticky `payments_blocked` block and
    # a sanctions `match` must refuse a payment no matter the corridor,
    # so this gate must NOT be nested under the international-leg `if`
    # (a blocked vendor paid via domestic ACH would otherwise slip
    # through unscreened). A refusal fails the payment outright; a hold
    # leaves it in pending_compliance for AP review.
    if invoice is not None:
        from app.services.compliance import check_payment_compliance

        v_full = None
        if invoice.vendor_id:
            v_result = await db.execute(select(Vendor).where(Vendor.id == invoice.vendor_id))
            v_full = v_result.scalar_one_or_none()
        if v_full is None:
            # No screenable vendor (invoice never matched a Vendor, or the
            # row was deleted). We CANNOT run sanctions/KYC against a payee
            # we don't have — and an AI-extracted / email-intake invoice can
            # reach here with vendor_id NULL. Paying anyway would route money
            # to an unscreened name, defeating the gate. Fail-safe: hold for
            # AP to attach + verify a vendor, never pay unscreened.
            payment.status = "pending_compliance"
            payment.failure_reason = "compliance_hold: no screenable vendor on invoice"
            await _open_compliance_hold_exception(db, payment=payment, invoice=invoice, org=org)
            return
        decision = await check_payment_compliance(
            db,
            vendor=v_full,
            # See the card leg above: the KYC threshold is a home-currency
            # figure, so hand the gate the home-currency leg (locked by the FX
            # step just above) and let it fail closed when it isn't available.
            payment_amount=payment.source_amount or payment.amount,
            payment_currency=(
                payment.source_currency
                if payment.source_amount is not None
                else (invoice.currency if invoice is not None else None)
            ),
            payment_method=payment.method,
            org_settings=org.settings or {},
            organization_id=org.id,
            correlation_id=payment.correlation_id,
        )
        if decision.verdict == "refuse":
            payment.status = "failed"
            payment.failure_reason = "compliance_refusal: " + "; ".join(decision.reasons)
            payment.completed_at = now
            return
        if decision.verdict == "hold":
            payment.status = "pending_compliance"
            payment.failure_reason = "compliance_hold: " + "; ".join(decision.reasons)
            # Hold doesn't flip the invoice — money hasn't moved.
            await _open_compliance_hold_exception(db, payment=payment, invoice=invoice, org=org)
            return

    payload = PaymentPayload(
        correlation_id=str(payment.correlation_id or payment.id),
        invoice_id=str(payment.invoice_id),
        invoice_number=invoice.invoice_number if invoice else "",
        vendor_name=invoice.vendor_name if invoice else "",
        amount=payment.amount,
        currency=invoice.currency if invoice else "USD",
        method=payment.method or "ach",
        description=invoice.description if invoice else None,
        vendor_bank=vendor_bank,
        metadata={"organization_id": str(org.id)},
        source_currency=payment.source_currency,
        source_amount=payment.source_amount,
        fx_rate=payment.fx_rate,
        target_country=payment.target_country,
    )

    result_obj = await adapter.create_payment(payload)
    payment.provider = adapter.provider_name
    payment.provider_payment_id = result_obj.provider_payment_id
    payment.reference = result_obj.reference or payment.reference
    payment.submitted_at = now

    if result_obj.status == PaymentStatus.completed:
        payment.status = "completed"
        payment.completed_at = now
        await _capture_discount_offers(
            db, org=org, invoice=invoice, payment=payment, actor_id=user.id, now=now
        )
        if invoice and invoice.status.value in SCHEDULABLE_INVOICE_STATUSES:
            await transition_invoice(
                db,
                invoice,
                InvoiceStatus.payment_scheduled,
                actor_id=user.id,
                action_name="invoice.payment_scheduled",
                details={"payment_id": str(payment.id), "result": "completed"},
            )
    elif result_obj.status in (PaymentStatus.submitted, PaymentStatus.processing):
        # Real money in flight; webhook will finalize.
        payment.status = result_obj.status.value
        if invoice and invoice.status.value in SCHEDULABLE_INVOICE_STATUSES:
            await transition_invoice(
                db,
                invoice,
                InvoiceStatus.payment_scheduled,
                actor_id=user.id,
                action_name="invoice.payment_scheduled",
                details={"payment_id": str(payment.id), "result": result_obj.status.value},
            )
    else:
        # failed or cancelled
        payment.status = result_obj.status.value
        payment.failure_reason = result_obj.failure_reason
        payment.completed_at = now


async def _dispatch_run_payments(
    db: AsyncSession,
    *,
    run: PaymentRun,
    run_id: uuid.UUID,
    org: Organization,
    user: User,
    adapter: PaymentAdapter,
) -> dict:
    """Dispatch every still-`pending` payment on `run`, committing durably
    after each one, then roll up the run's final status across ALL its
    payments (not just the ones touched in this pass) and return the
    response payload.

    Shared by `execute_payment_run` (fresh `draft` claim) and
    `resume_payment_run` (an `executing` run stuck after a crash) — both
    already hold (and released) the row lock and have decided it's this
    call's turn to run the loop.

    Each payment is committed right after it's dispatched: a problem with
    payment N — including an uncaught error from a live FX/sanctions/processor
    adapter — can only roll back payment N's own (still-open) attempt, never
    the payments already recorded before it. That durability is what makes an
    `executing` run resumable instead of permanently stuck with real money
    moved but no local record of it.

    `adapter` is resolved by the caller via `_require_payment_adapter` BEFORE
    it claims the run, so an unsupported `settings.payments.provider` refuses
    with the run untouched instead of stranding it `executing`.
    """
    now = datetime.now(UTC)

    from app.services.audit_dispatch import dispatch_audit

    # Only payments this run hasn't attempted yet. Every payment starts
    # `pending`; on a resume, anything already `completed` / `failed` /
    # `submitted` / `processing` / `pending_compliance` from an earlier
    # (crashed) pass is left untouched — never re-dispatched to the processor.
    pay_result = await db.execute(
        select(Payment).where(Payment.payment_run_id == run_id, Payment.status == "pending")
    )
    pending_payments = pay_result.scalars().all()

    for payment in pending_payments:
        # Re-lock and re-check immediately before dispatching, mirroring the
        # reconciler's claim pattern (payment_reconciler.py). The bulk read
        # above is a plain SELECT with no lock — held across this whole loop
        # it would be released early anyway by the per-payment commit below —
        # so without this, two concurrent callers (two /resume calls, or a
        # /resume racing an in-flight /execute) both load the same pending
        # row and both dispatch it to the adapter. This is what makes a
        # second concurrent caller see the row already claimed and skip it
        # instead of double-charging the processor.
        await db.refresh(payment, with_for_update=True)
        if payment.status != "pending":
            continue
        try:
            await _execute_single_payment(
                db, payment=payment, org=org, adapter=adapter, user=user, now=now
            )
        except Exception as exc:  # noqa: BLE001
            # A live FX / sanctions / processor adapter can raise anything on
            # a network or API hiccup (bare RuntimeError, httpx errors, ...).
            # Recording THIS payment as failed — instead of letting the
            # exception unwind the whole request — is what keeps the other
            # payments in this run from being lost to a rollback.
            #
            # Log the exception TYPE only, never `str(exc)` / `exc_info` — a
            # live FX/sanctions/processor adapter can embed a partial account
            # number, IBAN, or PAN in its error string, and that must never
            # reach the log sink or this row (PII/banking-data-out-of-logs
            # invariant). Mirrors `card_issuance.py` / `payment_erp_sync.py`.
            logger.warning(
                "payment %s raised during payment-run dispatch; marking failed: %s",
                payment.id,
                exc.__class__.__name__,
            )
            payment.status = "failed"
            payment.failure_reason = f"unexpected_error:{exc.__class__.__name__}"
            payment.completed_at = now

        # Append-only audit trail for the money-movement event (project
        # invariant: every payment status transition writes a log row, and a
        # change that touches a regulated timestamp like `completed_at` is
        # Critical without one). PII-free: only ids, status, and the Decimal
        # amount as a string ever enter `details` — never bank/account values.
        await dispatch_audit(
            db,
            correlation_id=payment.correlation_id or run.id,
            organization_id=org.id,
            actor_id=user.id,
            action=f"payment.{payment.status}",
            entity_type="payment",
            entity_id=payment.id,
            details={
                "status": payment.status,
                "method": payment.method,
                "amount": str(payment.amount),
                "reference": payment.reference,
                "payment_run_id": str(run.id),
            },
        )

        # Durable per-payment commit. A crash or exception on the NEXT
        # payment can only roll back ITS OWN still-open transaction — this
        # one is already safely on disk.
        await db.commit()

    # Roll up over EVERY payment on the run — not just this pass — so a
    # resumed run's final status/counts reflect the whole run, not only the
    # subset that was still pending when this call started.
    all_result = await db.execute(select(Payment).where(Payment.payment_run_id == run_id))
    # A retried invoice carries several rows on this run; only the newest
    # attempt describes its outcome (see `active_run_payments`).
    all_payments = active_run_payments(all_result.scalars().all())
    # Bucketing + the status precedence live in `services/payment_runs` so the
    # status this PERSISTS and the rollup the run-detail / runs-list reads
    # REPORT can't drift apart.
    rollup = rollup_payment_statuses(p.status for p in all_payments)
    completed, failed, in_flight = rollup.completed, rollup.failed, rollup.in_flight
    cards_issued = sum(
        1 for p in all_payments if p.method == "virtual_card" and p.status == "completed"
    )

    run.status = rollup.run_status
    run.executed_at = now

    await dispatch_audit(
        db,
        correlation_id=run.id,
        organization_id=org.id,
        actor_id=user.id,
        action="payment_run.executed",
        entity_type="payment_run",
        entity_id=run.id,
        details={
            "status": run.status,
            "provider": adapter.provider_name,
            "payments_completed": completed,
            "payments_in_flight": in_flight,
            "payments_failed": failed,
            "cards_issued": cards_issued,
            "total_amount": str(run.total_amount or Decimal("0")),
        },
    )
    await db.commit()

    # ERP sync only fires for payments we believe settled — pending ones
    # will sync when their webhook lands.
    if completed:
        from app.services.payment_erp_sync import dispatch_payment_sync

        await dispatch_payment_sync(run_id, uuid.UUID(str(run.organization_id)))

    return {
        "id": str(run.id),
        "status": run.status,
        "provider": adapter.provider_name,
        "payments_completed": completed,
        "payments_in_flight": in_flight,
        "payments_failed": failed,
        "cards_issued": cards_issued,
        "message": _execute_message(
            adapter.provider_name, completed, in_flight, failed, cards_issued
        ),
    }


@router.post("/runs/{run_id}/execute")
async def execute_payment_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # The money-moving end of the payment SoD split. Defaults map to
    # admin/ap_manager/cfo (unchanged); split from run-approval / bank-change.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Execute a draft payment run via the configured payment adapter.

    Each payment is dispatched to the org's configured processor (Modern
    Treasury for prod, mock for dev). The adapter returns either a
    `submitted`/`processing` status (real money in flight, terminal status
    arrives via webhook) or `completed`/`failed` immediately (mock). See
    `POST /runs/{run_id}/resume` if a run gets stuck in `executing` (e.g. a
    worker crashed mid-run) — this endpoint stays `draft`-only so a plain
    concurrent double-click can never race a still-genuinely-running
    execution (see the row-lock comment below).

    Run status:
      - `completed` — every payment reached `completed`
      - `partial`   — at least one succeeded, at least one failed
      - `failed`    — every payment failed
      - `submitted` — at least one payment is in flight (waiting on webhook)
    """
    # Lock the run row FOR UPDATE and atomically flip it out of `draft`
    # BEFORE the adapter loop. Without this, two concurrent /execute calls
    # both read `status == "draft"`, both pass the guard, and both dispatch
    # every payment to the processor — the adapter is charged twice for the
    # same rows (double-pay). The row lock serializes the two requests: the
    # first acquires the lock, re-checks `draft`, flips the run to
    # `executing`, and commits; the second blocks on the lock, then re-reads
    # the now-`executing` status and 409s before any money moves. (The
    # adapter call itself is also idempotency-keyed via
    # `PaymentPayload.correlation_id` — defense in depth for processors that
    # honor it, e.g. Modern Treasury / Column.)
    run = await _get_scoped_run(db, run_id, entity_id, for_update=True)
    if run.status != "draft":
        raise HTTPException(
            status_code=409, detail=f"Can only execute 'draft' runs, not '{run.status}'"
        )
    # Maker-checker: the user who created the run cannot also execute it (the
    # money-movement step). Default-on; per-org opt-out for single-operator
    # accounts. The role/permission split can't enforce this — one user holding
    # both perms (the default ap_manager) would otherwise run the whole payment
    # lifecycle solo.
    check_run_segregation(
        run.initiated_by,
        user.id,
        (org.settings or {}).get("payments"),
        action="execute",
    )
    if run.requires_cfo_approval and run.cfo_approved_at is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "This run exceeds the org's CFO-approval threshold and is awaiting "
                "sign-off from a user with the CFO role."
            ),
        )

    # Resolve the processor BEFORE claiming the run. An unsupported
    # `settings.payments.provider` can dispatch nothing, so refusing here
    # leaves the run in `draft` and re-runnable once settings are fixed —
    # rather than stranding it `executing` behind a 500.
    adapter = _require_payment_adapter(org)

    # Claim the run: flip it to an in-flight status and commit so the lock
    # releases and any concurrent caller blocked above wakes to a non-draft
    # run (→ 409). The final rollup status overwrites `executing` at the end.
    run.status = "executing"
    await db.commit()

    return await _dispatch_run_payments(
        db, run=run, run_id=run_id, org=org, user=user, adapter=adapter
    )


@router.post("/runs/{run_id}/resume")
async def resume_payment_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # Resuming can dispatch real payments exactly like /execute — same gate.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Resume a payment run stuck in `executing` — e.g. the worker process
    crashed partway through `execute_payment_run`'s per-payment loop.

    Because that loop commits durably after every payment, a run left in
    `executing` has every payment up to the crash point already safely
    recorded with its real outcome; only the still-`pending` ones (never
    attempted) get re-dispatched here — nothing already `completed` /
    `failed` / `submitted` / `processing` / `pending_compliance` is touched
    or re-sent to the processor.

    Deliberately a SEPARATE endpoint from `/execute` (which stays
    `draft`-only): a run that is still genuinely mid-execution is ALSO
    `executing`, and a bare "accept `executing` too" guard on `/execute`
    would let a concurrent call race an active run instead of only a
    crashed one. An operator calls this endpoint only after confirming the
    run is actually stuck (no progress for an implausible amount of time),
    not as a matter of course.
    """
    run = await _get_scoped_run(db, run_id, entity_id, for_update=True)
    if run.status != "executing":
        raise HTTPException(
            status_code=409,
            detail=f"Can only resume a run stuck 'executing', not '{run.status}'",
        )
    # Resuming dispatches real payments exactly like /execute — same
    # maker-checker gate. Without this, the run's own initiator could wait
    # for (or force) it into `executing` and resume-execute their own run
    # solo, after already being refused at /execute.
    check_run_segregation(
        run.initiated_by,
        user.id,
        (org.settings or {}).get("payments"),
        action="execute",
    )

    # Same pre-flight as /execute: refuse an unsupported processor before the
    # loop rather than 500-ing partway through it.
    adapter = _require_payment_adapter(org)

    # Release the row lock before the (potentially slow) per-payment loop —
    # no status change needed here, the run is already `executing`.
    await db.commit()

    return await _dispatch_run_payments(
        db, run=run, run_id=run_id, org=org, user=user, adapter=adapter
    )


@router.post("/runs/{run_id}/sync-erp")
async def retry_run_erp_sync(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # Re-running the sync can flip invoices `payment_scheduled → paid` — the
    # same money-state authority as executing a run or accepting a short
    # settlement, so the same gate.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Re-run the ERP sync-back for a run whose settled payments didn't land.

    This is the **exit for a stranded invoice**. `services/payment_erp_sync` is
    the only path that flips `payment_scheduled → paid`, and it is dispatched
    exactly once per terminal event (run execute, payment webhook) —
    fire-and-forget onto a detached thread. If a leg fails there, the money has
    already moved but the invoice never advances, the ERP is never told, and
    **nothing re-invokes the sync**, because the reconciler backstop only
    re-dispatches payments it moves out of `submitted`/`processing`.

    Voiding is not an exit for that state: `POST /{payment_id}/void` returns the
    invoice to `approved`, which invites a second payment for money that already
    left. So the strand gets an explicit, audited re-run instead.

    A failed leg now also opens a de-duped `erp_reconciliation` exception naming
    this endpoint, so the operator reaches it from the queue rather than having
    to know the run id. Deliberately does NOT resolve that exception on success
    — `erp_reconciliation` is shared with the ERP-void path in
    `api/erp_webhook`, so auto-closing "the open one" could silently clear an
    unrelated reconciliation (the same reasoning `POST /{id}/settlement/accept`
    documents for `fraud_flag`). The human closes it after confirming.

    **Idempotent by construction, not by a claim**: the pass skips every payment
    that isn't `completed` and every invoice that isn't `payment_scheduled`, so
    a repeat call after a successful re-run writes no second transition. It
    moves no money — it only reports money that already moved.

    Read `transitioned`, not `synced`, to answer "did this recover anything".
    `synced` counts legs whose ERP-facing work completed, which stays TRUE for a
    settled payment whose invoice was already `paid` — so a repeat call reports
    the same `synced` count and `transitioned: 0`.

    Unlike the two dispatch sites this one AWAITS the pass, so the response
    carries the real per-leg counts instead of "queued".
    """
    from app.services.audit_dispatch import dispatch_audit
    from app.services.payment_erp_sync import _sync_payments

    run = await _get_scoped_run(db, run_id, entity_id)
    settled = (
        await db.execute(
            select(func.count())
            .select_from(Payment)
            .where(Payment.payment_run_id == run.id, Payment.status == "completed")
        )
    ).scalar() or 0
    if settled == 0:
        raise HTTPException(
            status_code=409,
            detail="This run has no settled payments to sync to the ERP.",
        )

    await dispatch_audit(
        db,
        correlation_id=run.id,
        organization_id=org.id,
        actor_id=user.id,
        action="payment_run.erp_sync_retried",
        entity_type="payment_run",
        entity_id=run.id,
        details={"status": run.status, "settled_payments": settled},
    )
    # Read what the pass needs off the ORM row BEFORE committing, so this
    # doesn't quietly depend on the session's `expire_on_commit=False` — an
    # expired attribute read from async SQLAlchemy is an implicit lazy load,
    # i.e. a MissingGreenlet rather than a clean failure.
    run_org_id = uuid.UUID(str(run.organization_id))
    run_ref = str(run.id)
    # Commit (and release the request session's transaction) BEFORE the pass —
    # it opens its own tenant session and updates the same invoice rows.
    await db.commit()

    result = await _sync_payments(run_id, run_org_id)
    return {
        "id": run_ref,
        "synced": result.synced,
        "transitioned": result.transitioned,
        "skipped": result.skipped,
        "held": result.held,
        "failed": result.failed,
    }


# Statuses a payment must be in for `/retry-failed` to consider re-attempting
# it. Both are terminal *failures*, and both are excluded from
# `uq_payments_one_live_per_invoice` — which is what lets the retry book a
# SECOND payment row against the same invoice at all. Being in one of these is
# necessary but nowhere near sufficient: `classify_payment_failure` still has to
# prove no order was created at the processor (see the endpoint docstring).
RETRYABLE_PAYMENT_STATUSES = ("failed", "cancelled")

# The run statuses that can carry a failed payment worth re-attempting. A
# `draft` run has never been dispatched (use `/execute`), an `executing` one is
# either mid-flight or crashed (use `/resume`), and a `cancelled` one has no
# payments left at all.
RETRYABLE_RUN_STATUSES = ("partial", "failed")


@router.post("/runs/{run_id}/retry-failed")
async def retry_failed_payments(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # Re-attempting dispatches real payments exactly like /execute — same gate.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Re-attempt the safely-retryable FAILED payments of a `partial`/`failed` run.

    A payment run's failures were previously a dead end: the run settled on
    `partial`, and the only way forward was to hand-build a second run.

    **A retry books a NEW payment, it never re-arms the old one.**
    `Payment.correlation_id` is the PROCESSOR's idempotency key (see
    `payment_adapters/base.py`), so a re-attempt is genuinely a new order and
    needs a new one. Minting it onto the failed row — as this endpoint first
    did — also meant clearing `failure_reason`, `provider_payment_id`,
    `submitted_at` and `completed_at`, destroying the only handles anyone had
    for reconciling attempt #1 with the processor and overwriting two regulated
    money timestamps. Attempt #1 is therefore never written to at all; attempt
    #2 is an INSERT on the same run carrying `retry_of_payment_id`, and the run
    rollup counts the latest attempt per invoice
    (`services/payment_runs.active_run_payments`). `failed`/`cancelled` are
    outside `uq_payments_one_live_per_invoice`, so the second row is free to
    claim the invoice's live-payment slot.

    **What is re-attempted, and what is not.** Anything already `completed`,
    `submitted`, `processing` or `pending_compliance` is left exactly as it is
    and is never re-sent — the same guarantee `/resume` makes. A payment in
    `RETRYABLE_PAYMENT_STATUSES` is *skipped* (never re-sent, never mutated),
    with the reason surfaced in `skip_reasons`, when:

    - `invoice_not_payable` — the invoice is voided, re-rejected or already
      `done`, so paying it would move money against something nobody currently
      approves;
    - `invoice_has_blocking_exception` — an unresolved
      `PAYMENT_BLOCKING_EXCEPTION_TYPES` flag (duplicate / fraud_flag /
      line_total_mismatch). Run creation refuses these outright; this endpoint
      re-dispatches money days or weeks later, so a `fraud_flag` raised in the
      interim (a BEC bank-detail swap, an altered cheque off a Positive Pay
      return) has to stop the re-send here too. Same shared query
      (`payment_runs.blocked_invoice_ids`) so the two can't drift;
    - `needs_reconciliation` — we cannot prove the processor never accepted the
      original order (`classify_payment_failure`): a populated
      `provider_payment_id`, an `unexpected_error:*` / `*_transport_error:*` /
      `*_api_error:*` / `reconciler_max_age_exceeded*` reason, or any reason
      we don't recognise. Re-sending these under a fresh idempotency key is how
      one invoice gets paid twice. A human voids or reconciles first;
    - `net_amount_changed` — a credit memo applied since the run was built
      means the failed row's `amount` is no longer what the vendor is owed
      (`payment_runs.net_payable_amount`). The amount is never silently
      adjusted — a fresh run re-derives it through the full gate set;
    - `invoice_has_live_payment` — the invoice has since acquired another live
      payment. The savepoint around each insert is the backstop for the same
      conflict arriving as a race.

    **Idempotency.** The run is row-locked and claimed (`→ executing`) before
    anything is booked, so a double-click / concurrent retry blocks on the lock
    and then 409s against the non-retryable status — it can't produce a second
    dispatch pass. `_dispatch_run_payments` then re-locks each payment and skips
    any that is no longer `pending`.
    """
    run = await _get_scoped_run(db, run_id, entity_id, for_update=True)
    # Gate on what the run's payments actually say, not on the value the last
    # dispatch pass persisted. Nothing rewrites `PaymentRun.status` when a
    # webhook, the reconciler or `/compliance/{release,dismiss}` moves one of
    # its payments afterwards — so a run that rolled up `submitted` (one
    # payment held `pending_compliance`) and then had that payment dismissed
    # reported a retryable failure that this endpoint refused to retry, with
    # `/execute` and `/resume` 409ing too: a dead end, and exactly the "button
    # that can't act" `retryable_failures` exists to prevent. The read
    # endpoints derive the same way (`payment_runs.derive_run_status`), so the
    # status an operator sees and the status this gates on cannot diverge.
    effective_status = derive_run_status(
        run.status,
        rollup_payment_statuses(
            p.status
            for p in active_run_payments(
                (await db.execute(select(Payment).where(Payment.payment_run_id == run_id)))
                .scalars()
                .all()
            )
        ),
    )
    if effective_status not in RETRYABLE_RUN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                "Can only retry a run that finished with failures "
                f"({' or '.join(RETRYABLE_RUN_STATUSES)}), not '{effective_status}'"
            ),
        )
    # Re-attempting moves money exactly like /execute — same maker-checker gate
    # and the same CFO threshold. Skipping either here would turn `/retry-failed`
    # into a way around both.
    check_run_segregation(
        run.initiated_by,
        user.id,
        (org.settings or {}).get("payments"),
        action="execute",
    )
    if run.requires_cfo_approval and run.cfo_approved_at is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "This run exceeds the org's CFO-approval threshold and is awaiting "
                "sign-off from a user with the CFO role."
            ),
        )

    # Same pre-flight as /execute and /resume: a run whose processor can't be
    # resolved retries nothing, so refuse before booking any retry attempt row.
    adapter = _require_payment_adapter(org)

    all_run_payments = (
        (await db.execute(select(Payment).where(Payment.payment_run_id == run_id))).scalars().all()
    )
    # An earlier attempt a previous retry already replaced is history — its
    # successor is what describes where the invoice stands.
    superseded = superseded_payment_ids(all_run_payments)
    failed_payments = [
        p
        for p in all_run_payments
        if p.status in RETRYABLE_PAYMENT_STATUSES and p.id not in superseded
    ]

    # Which of those invoices can legitimately take another attempt. Every gate
    # `create_payment_run_for_invoices` applies at run-creation time is re-run
    # here, because this endpoint dispatches real money days or weeks later and
    # nothing guarantees the world stayed still.
    invoice_ids = [p.invoice_id for p in failed_payments]
    invoices: dict[uuid.UUID, Invoice] = {}
    payable_ids: set[uuid.UUID] = set()
    blocked_ids: set[uuid.UUID] = set()
    card_claimed_ids: set[uuid.UUID] = set()
    occupied_ids: set[uuid.UUID] = set()
    if invoice_ids:
        invoices = {
            inv.id: inv
            for inv in (await db.execute(select(Invoice).where(Invoice.id.in_(invoice_ids))))
            .scalars()
            .all()
        }
        payable_ids = {
            iid for iid, inv in invoices.items() if inv.status.value in PAYABLE_INVOICE_STATUSES
        }
        blocked_ids = await blocked_invoice_ids(db, invoice_ids)
        # A live virtual card minted since the run was built claims the invoice
        # on a rail this retry isn't using. Same shared gate the run builder and
        # the standalone endpoint run — a retry dispatches real money days or
        # weeks later, so it re-checks everything they check.
        card_claimed_ids = await card_claimed_invoice_ids(
            db, ((p.invoice_id, p.method) for p in failed_payments)
        )
        occupied_ids = set(
            (
                await db.execute(
                    select(Payment.invoice_id).where(
                        Payment.invoice_id.in_(invoice_ids),
                        Payment.status.notin_(LIVE_PAYMENT_TERMINAL_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )

    retried = 0
    skipped: list[str] = []
    for payment in failed_payments:
        if payment.invoice_id not in payable_ids:
            skipped.append("invoice_not_payable")
            continue
        if payment.invoice_id in blocked_ids:
            skipped.append("invoice_has_blocking_exception")
            continue
        if payment.invoice_id in card_claimed_ids:
            skipped.append("invoice_has_live_card")
            continue
        if not is_retry_safe(payment):
            # We can't prove the processor never took the original order. A
            # fresh idempotency key here is a second real payment, not a retry.
            skipped.append("needs_reconciliation")
            continue
        if payment.invoice_id in occupied_ids:
            skipped.append("invoice_has_live_payment")
            continue
        # What the invoice is worth NOW. A credit memo applied while this sat
        # `failed` (credit_memos.py gates on neither invoice status nor an
        # existing payment) makes the failed row's amount stale; pay it and the
        # vendor is overpaid by the credit.
        net_amount = await net_payable_amount(db, invoices[payment.invoice_id])
        if net_amount != payment.amount:
            skipped.append("net_amount_changed")
            continue

        retry_payment = Payment(
            invoice_id=payment.invoice_id,
            entity_id=payment.entity_id,
            payment_run_id=run.id,
            amount=net_amount,
            method=payment.method,
            status="pending",
            # A new order at the processor, so a new idempotency key. The
            # failed attempt keeps its own — it is the only handle anyone has
            # for reconciling what that attempt did.
            correlation_id=uuid.uuid4(),
            retry_of_payment_id=payment.id,
        )
        try:
            # Savepoint per insert: the live-payment unique index is the
            # backstop for a conflict that appeared between the pre-check above
            # and this flush. Containing it keeps the outer transaction usable
            # so the remaining payments can still be re-attempted.
            async with db.begin_nested():
                db.add(retry_payment)
                await db.flush()
        except IntegrityError:
            db.expunge(retry_payment)
            skipped.append("invoice_has_live_payment")
            continue
        retried += 1

        from app.services.audit_dispatch import dispatch_audit

        # Links BOTH rows: `entity_id` is attempt #1 (the row whose meaning
        # changed — it is now superseded) and `retry_payment_id` names its
        # successor, so an auditor can walk the chain from either end. PII-free:
        # ids, the Decimal amount as a string, the rail, and the previous
        # failure reason (a processor error code, never account data).
        await dispatch_audit(
            db,
            correlation_id=retry_payment.correlation_id or run.id,
            organization_id=org.id,
            actor_id=user.id,
            action="payment.retried",
            entity_type="payment",
            entity_id=payment.id,
            details={
                "payment_run_id": str(run.id),
                "payment_id": str(payment.id),
                "retry_payment_id": str(retry_payment.id),
                "amount": str(retry_payment.amount),
                "method": retry_payment.method,
                "previous_failure_reason": payment.failure_reason,
            },
        )

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=run.id,
        organization_id=org.id,
        actor_id=user.id,
        action="payment_run.retried",
        entity_type="payment_run",
        entity_id=run.id,
        details={
            "payments_retried": retried,
            "payments_skipped": len(skipped),
            "skip_reasons": sorted(set(skipped)),
            "previous_status": run.status,
        },
    )

    # Claim the run before dispatching, exactly like /execute: the commit
    # releases the row lock, and any concurrent retry/execute/resume that was
    # blocked on it wakes to `executing` and 409s.
    run.status = "executing"
    await db.commit()

    result = await _dispatch_run_payments(
        db, run=run, run_id=run_id, org=org, user=user, adapter=adapter
    )
    result["payments_retried"] = retried
    result["payments_skipped"] = len(skipped)
    result["skip_reasons"] = sorted(set(skipped))
    return result


def _execute_message(
    provider: str, completed: int, in_flight: int, failed: int, cards_issued: int = 0
) -> str:
    parts: list[str] = []
    if completed:
        parts.append(f"{completed} completed")
    if in_flight:
        parts.append(f"{in_flight} in flight")
    if failed:
        parts.append(f"{failed} failed")
    if cards_issued:
        parts.append(f"{cards_issued} card(s) issued")
    body = ", ".join(parts) or "0 payments"
    return f"Payment run executed via {provider}: {body}."


# ── Provider webhook ────────────────────────────────────────────────


@router.post("/webhook/{tenant_slug}/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def payment_webhook(tenant_slug: str, provider: str, request: Request):
    """Receive a payment-status webhook from the configured processor.

    Auth is by the processor's HMAC signature (verified inside
    `adapter.parse_webhook`), not a JWT. The tenant is encoded in the URL
    path — each tenant configures its own webhook URL with the processor,
    so a leaked URL only affects that one tenant. Bad signatures, unknown
    events, and unknown tenants all return 204 silently — leaking the
    distinction would help an attacker probe for the right secret. Audit
    log captures the rejection.

    A verified signature authenticates the SENDER, not the CONTENT: a
    `completed` event proves the processor is talking to us about a payment
    we know, never that it moved the amount AP authorized. Every completion
    therefore runs `services/payment_settlement.verify_settlement` against
    the reported amount + currency, and a divergence opens a
    payment-blocking `fraud_flag` instead of being treated as a clean
    settlement. See `backend/docs/payments.md` § Settlement-amount
    verification.

    URL shape (configure in the processor's dashboard):
        https://app.com/api/payments/webhook/{tenant_slug}/{provider}
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import settings as app_settings
    from app.database import control_session_factory, get_tenant_engine

    # Bound the body BEFORE buffering it. The HMAC check happens inside
    # adapter.parse_webhook, well after this point, so an unauthenticated
    # attacker could otherwise POST an arbitrarily large payload and have it
    # read fully into memory before anything rejects it (memory-exhaustion
    # DoS on a public route). Reject on the declared Content-Length when
    # present, and re-check the actual read in case the header lied / was
    # absent (chunked). Processor status payloads are small JSON; cap
    # defaults to a few MB.
    max_bytes = app_settings.payment_webhook_max_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                logger.warning("Payment webhook rejected: body exceeds size cap")
                return
        except ValueError:
            logger.warning("Payment webhook rejected: invalid content-length")
            return

    body = await request.body()
    if len(body) > max_bytes:
        logger.warning("Payment webhook rejected: body exceeds size cap")
        return
    headers = {k: v for k, v in request.headers.items()}

    # The `mock` adapter's `parse_webhook` performs NO signature verification
    # (it exists only so test fixtures can simulate a status flip by calling the
    # adapter directly) and `mock` is the default provider for any tenant that
    # hasn't configured a real processor — seeded demo tenants and fresh signups
    # both land there. Serving it on this public, unauthenticated route would
    # accept forged status transitions. Mock never actually delivers webhooks, so
    # reject it here outright rather than relying on the downstream terminal-state
    # guard. Mirrors `cards.card_webhook`'s hardcoded `lithic`/`nium` allowlist
    # and the boot-time `mock` refusal on the billing webhook route.
    if provider == "mock":
        return

    # Resolve tenant from the URL path (no JWT, no X-Tenant-Slug header).
    async with control_session_factory() as ctrl_db:
        org_result = await ctrl_db.execute(
            select(Organization).where(Organization.slug == tenant_slug)
        )
        org = org_result.scalar_one_or_none()
    if org is None:
        return

    payment_config = (org.settings or {}).get("payments") or {}
    if payment_config.get("provider") != provider:
        return  # wrong adapter for this tenant

    try:
        adapter = get_payment_adapter(payment_config)
    except UnknownPaymentProviderError:
        # The tenant's configured provider name matches this URL but names a
        # processor we have no adapter for. Before `get_payment_adapter` failed
        # closed, this resolved to `mock` — whose `parse_webhook` verifies no
        # signature at all — so a single settings typo silently re-opened the
        # very hole the `provider == "mock"` early-return above exists to
        # close, under any other name. 204 like every other rejection path.
        logger.warning("Payment webhook rejected: tenant's configured provider is not supported")
        return
    event = adapter.parse_webhook(headers, body)
    if event is None:
        return  # bad signature, unrecognised event, or no-op

    # Dedup by the processor's event id. Webhook providers retry on any
    # non-2xx delivery; without this guard the same event could flip a
    # payment to `completed` twice and re-fire the ERP-sync dispatch.
    from app.services.webhook_security import (
        is_event_already_processed,
        release_event_claim,
    )

    claimed_event: str | None = None
    if event.event_id:
        if await is_event_already_processed(provider, event.event_id):
            return
        # Track the claim so the tenant-DB block below can release it if that
        # block raises: the Redis dedup claim is only durable once the status
        # transition commits, so a claim left set over a rolled-back txn would
        # dedup the provider's retry away for the full TTL — the payment would
        # then never reach terminal status. (Mirrors api/cards.py's discipline.)
        claimed_event = event.event_id
    else:
        # A provider (or a future adapter) that stopped populating event_id
        # can't be Redis-deduped. Make that an explicit, logged branch rather
        # than a silent short-circuit of the check: we proceed WITHOUT the
        # first-line dedup, and the terminal-state allowlist below (only
        # pending/submitted/processing are overwritable, under the FOR UPDATE
        # row lock) is the backstop that keeps a re-delivery from
        # double-completing a payment. If this warning ever fires in
        # production it means an adapter's parse_webhook needs an event_id.
        logger.warning(
            "[payment-webhook] empty event_id from provider=%s; skipping Redis "
            "dedup — relying on the terminal-state allowlist backstop",
            provider,
        )

    # Open a tenant-DB session to look up + update the Payment row. The whole
    # block is wrapped so that if anything below raises AFTER we claimed the
    # Redis dedup slot, we release the claim (see the `except` tail) and let the
    # exception propagate — the provider then retries (a 5xx), and the released
    # claim lets that retry actually reprocess instead of being deduped away for
    # the full TTL. Without this the transition would be dropped and the payment
    # would never reach terminal status.
    engine = get_tenant_engine(org.db_name)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            # Lock the row for the read-check-write below: the terminal-state
            # allowlist guard is a non-atomic read→check→write, and the
            # reconciler (a separate code path that doesn't share the Redis event
            # dedup) or a second concurrent delivery could otherwise interleave
            # between the check and the UPDATE. FOR UPDATE serialises them.
            pay_result = await db.execute(
                select(Payment)
                .where(Payment.provider_payment_id == event.provider_payment_id)
                .with_for_update()
            )
            payment = pay_result.scalar_one_or_none()
            if not payment:
                return  # late retry of a payment we don't have

            # Only a genuinely in-flight payment may be advanced by a webhook.
            # Webhooks arrive out of order and re-deliveries can land hours after
            # a status already settled, so we use an allowlist of overwritable
            # states rather than a blocklist of terminal ones — a blocklist
            # silently lets through any state it forgot to name. In particular a
            # late `completed` must NOT resurrect a `voided` / `cancelled` /
            # `pending_compliance` payment (which would flip money back on with
            # no audit row). `completed` / `failed` are already terminal too.
            if payment.status not in ("pending", "submitted", "processing"):
                return

            previous_status = payment.status
            payment.status = event.status.value
            if event.reference:
                payment.reference = event.reference
            if event.failure_reason:
                payment.failure_reason = event.failure_reason
            if payment.status in ("completed", "failed", "cancelled"):
                payment.completed_at = datetime.now(UTC)

            # Settlement-amount verification. A `provider_payment_id` proves
            # WHICH payment this event is about; it does not prove the
            # processor moved the amount on the instruction. Only `completed`
            # is checked — a `failed` / `cancelled` event moved no money, so
            # whatever figure it echoes reconciles against nothing.
            #
            # The invoice is loaded once here and reused for the discount
            # capture below (which used to fetch it itself): the target leg is
            # denominated in the INVOICE's currency, which the Payment row
            # doesn't carry, and a discrepancy needs the invoice to hang its
            # queue entry on.
            completion: SettlementCompletion | None = None
            settled_invoice: Invoice | None = None
            if payment.status == "completed":
                settled_invoice = (
                    await db.execute(select(Invoice).where(Invoice.id == payment.invoice_id))
                ).scalar_one_or_none()
                # A rail whose event body carries no figure (Dwolla's bare
                # `{id, topic, resourceId}` envelope) gets one more chance
                # before we record a blind spot: ask the processor. This is
                # the async re-fetch `parse_webhook` deliberately cannot do —
                # that path is synchronous and on the signature-verification
                # line, so it must not make a network call.
                #
                # Guarded on every axis. An adapter without the capability
                # returns `available=False` from the base implementation, and
                # any failure at all leaves the settlement exactly where it
                # was — `unverified` — because a settlement fetch must never
                # break the webhook that is recording money movement.
                #
                # `record_completion` owns the fetch-fallback, the verdict,
                # the column writes AND the realized FX gain/loss, and the
                # reconciler backstop calls the SAME function — the two paths a
                # payment can reach `completed` on must not disagree about what
                # "verified" means, nor about what a foreign-currency
                # settlement books. They used to on both counts: the backstop
                # persisted a figure and skipped the verdict entirely, and
                # realized FX was computed inline right here, so every
                # cross-currency payment the backstop recovered lost it.
                completion = await record_completion(
                    db,
                    payment=payment,
                    adapter=adapter,
                    invoice=settled_invoice,
                    reported_amount=event.amount,
                    reported_currency=event.currency,
                )

            # Append-only audit trail for the webhook-driven status transition.
            # This is the production money-movement event — the processor's
            # webhook is what flips a real payment to `completed`/`failed` and
            # sets the regulated `completed_at`. Per the project invariant, a
            # status change touching `completed_at` without an audit row is
            # Critical. Actor is None (system-initiated by the processor, not a
            # user). PII-free: only ids, status, the Decimal amount as a string,
            # and the reference ever enter `details`.
            from app.services.audit_dispatch import dispatch_audit

            await dispatch_audit(
                db,
                correlation_id=payment.correlation_id or uuid.uuid4(),
                organization_id=org.id,
                actor_id=None,
                action=f"payment.{payment.status}",
                entity_type="payment",
                entity_id=payment.id,
                details={
                    "status": payment.status,
                    "previous_status": previous_status or "unknown",
                    "method": payment.method,
                    "amount": str(payment.amount),
                    "reference": payment.reference,
                    "source": "webhook",
                    "provider": provider,
                    "payment_run_id": (
                        str(payment.payment_run_id) if payment.payment_run_id else None
                    ),
                    # The settlement verdict and the realized FX gain/loss
                    # ride the SAME append-only row that records the money
                    # moving, built by the ONE shared helper the reconciler
                    # backstop also uses. The exception row below is mutable
                    # and gets resolved; this is the WORM-shipped evidence of
                    # what the processor said it settled, and it is written on
                    # every completion — matched, mismatched, and unverified
                    # alike — so a rail that reports no amount is a visible
                    # blind spot rather than a silent one. Money is an exact
                    # decimal string, never a float; the FX key is absent (not
                    # zero) when there is no exposure to measure.
                    **(completion.as_audit_details() if completion else {}),
                },
            )

            # A real (non-mock) rail typically confirms via THIS webhook, not
            # the synchronous leg of `_execute_single_payment` — ACH/wire sit
            # `submitted`/`processing` until the processor calls back. This is
            # the settlement moment for those payments, so it's also where an
            # accepted discount offer paid at its discounted payoff gets
            # recognized (mirrors the synchronous completion leg's call) —
            # unless the settlement itself didn't reconcile.
            if payment.status == "completed":
                if completion is not None and completion.is_discrepancy:
                    # Flag, and do NOT capture the discount: the payoff match
                    # runs against `payment.amount` — OUR authorized figure,
                    # which the rail has just contradicted — so capturing here
                    # would permanently mark savings realized on a number in
                    # dispute and misreport them to the CFO.
                    #
                    # The payment itself stays `completed`: money moved, and
                    # refusing to record that does not un-move it. The control
                    # is the blocking exception (mirroring how positive_pay
                    # flags an altered cheque and bank_reconciliation flags a
                    # divergent debit — both record and flag, neither rewrites
                    # the settlement).
                    await open_settlement_mismatch_exception(
                        db,
                        payment=payment,
                        invoice=settled_invoice,
                        org=org,
                        verification=completion.verification,
                    )
                else:
                    await _capture_discount_offers(
                        db,
                        org=org,
                        payment=payment,
                        actor_id=None,
                        now=payment.completed_at or datetime.now(UTC),
                        invoice=settled_invoice,
                    )

            run_id = payment.payment_run_id if payment.status == "completed" else None
            await db.commit()
    except Exception:
        # The dedup claim guards a side effect that just rolled back — release
        # it so the provider's retry can reprocess (otherwise the money-state
        # transition is dropped for the full TTL). Re-raise so the provider
        # actually retries. Mirrors api/cards.py's card_webhook.
        if claimed_event is not None:
            await release_event_claim(provider, claimed_event)
        raise

    # ERP sync runs after the DB commit so it sees the latest status.
    if run_id:
        from app.services.payment_erp_sync import dispatch_payment_sync

        await dispatch_payment_sync(run_id, uuid.UUID(str(org.id)))
