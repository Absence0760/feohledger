"""Response schema for ``GET /api/dashboard``.

Money fields use the ``MoneyAmount``/``OptionalMoneyAmount`` annotations
(Decimal in Python, JSON number on the wire) — see ``app/schemas/money.py``.
The endpoint itself keeps every currency figure as ``Decimal`` right up to
the response; this schema is what performs the single float hop, at
JSON-serialization time, matching the pattern used by
``app/schemas/discount.py`` and friends. Day-count / percentage fields
(``processing_time``, ``approval_bottleneck``, ``touchless_rate``,
``capture_rate_pct``) are not currency and stay plain ``float`` — or ``None``
where the figure is genuinely not computable.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.schemas.money import MoneyAmount


class ReportingByCurrency(BaseModel):
    currency: str
    original_amount: MoneyAmount
    reporting_amount: MoneyAmount
    count: int
    unconverted_count: int


class ReportingRollup(BaseModel):
    reporting_currency: str
    total_amount: MoneyAmount
    total_count: int
    unconverted_count: int
    by_currency: list[ReportingByCurrency]


class VendorSpendEntry(BaseModel):
    vendor: str
    amount: MoneyAmount
    #: Invoices summed into `amount` at FACE value because no locked exchange
    #: rate bridged them into the org's reporting currency. A count, not money.
    #: Non-zero means this vendor's total is a part-converted figure — which
    #: matters here beyond the number itself, because the tile RANKS vendors
    #: against each other and an unconverted total is not comparable to a
    #: converted one.
    unconverted_count: int = 0


class AgingBuckets(BaseModel):
    current: MoneyAmount
    days_30: MoneyAmount
    days_60: MoneyAmount
    days_90: MoneyAmount
    days_90_plus: MoneyAmount


class ReportingAgingBuckets(AgingBuckets):
    """`aging_reporting` — the same five bands, in the reporting currency.

    Carries the disclosure the bare `aging` deliberately does not: `aging` is a
    face-value cross-currency sum in its entirety, so "N rows could not be
    converted" would understate it, while these bands are converted apart from
    the rows this counts. One count for the band set rather than five — see
    `api/dashboard.py` for why.
    """

    unconverted_count: int = 0


class MonthlyTrendEntry(BaseModel):
    month: str
    count: int
    amount: MoneyAmount
    reporting_amount: MoneyAmount
    #: Invoices in THIS month summed into `reporting_amount` at face value for
    #: want of a rate lock. Per month, not per series: a trend is read bar
    #: against bar, so which step is part-converted is the useful fact.
    unconverted_count: int = 0


class UpcomingPayment(BaseModel):
    id: str
    invoice_number: str | None
    vendor_name: str | None
    # The invoice's FACE amount, in `currency` below — a per-row figure, not a
    # reporting one. The row carried no code, so the web dashboard labelled it
    # with the org's reporting currency: a EUR invoice read as dollars on a
    # USD-reporting tenant (docs/decisions.md §200).
    amount: MoneyAmount
    currency: str | None
    due_date: str | None
    is_overdue: bool


class ProcessingTimeMetrics(BaseModel):
    """Time-to-X distributions in business days — not money, stays float."""

    avg_upload_to_approval_days: float
    median_upload_to_approval_days: float
    p95_upload_to_approval_days: float
    avg_upload_to_paid_days: float
    median_upload_to_paid_days: float
    p95_upload_to_paid_days: float
    count_approval_leg: int
    count_paid_leg: int


class ApprovalBottleneckEntry(BaseModel):
    """Per-approver pending-approval stats — day counts, not money."""

    approver_id: str
    approver_name: str | None
    pending_count: int
    oldest_pending_days: float
    avg_pending_days: float


class DiscountCapture(BaseModel):
    """Early-pay discount opportunity, split three ways.

    `missed` is only what the window CLOSED on; `pending` is what is still
    capturable. `capture_rate_pct` is null (with `insufficient_data` true)
    when nothing has been decided yet — a 0% capture rate and "no window has
    closed yet" are opposite facts and must not share a value
    (`docs/decisions.md` §34).

    The `*_reporting` figures are in `reporting_currency`; the bare ones are
    face values that mix currencies once an eligible invoice is foreign.
    """

    eligible_count: int
    captured_count: int
    missed_count: int
    pending_count: int
    captured_amount: MoneyAmount
    missed_amount: MoneyAmount
    pending_amount: MoneyAmount
    reporting_currency: str
    captured_amount_reporting: MoneyAmount
    missed_amount_reporting: MoneyAmount
    pending_amount_reporting: MoneyAmount
    unconverted_count: int
    capture_rate_pct: float | None = None
    insufficient_data: bool


class DashboardResponse(BaseModel):
    total_invoices: int
    total_amount: MoneyAmount
    reporting: ReportingRollup
    total_paid: MoneyAmount
    total_pending: MoneyAmount
    # Reporting-currency counterparts of `total_paid` / `total_pending` — see
    # `app/api/dashboard.py`'s `payment_reporting_amount_sql` comment.
    total_paid_reporting: MoneyAmount
    total_pending_reporting: MoneyAmount
    total_paid_unconverted_count: int
    total_pending_unconverted_count: int
    total_rebates: MoneyAmount
    # Rebates left out of `total_rebates` for being denominated in another
    # currency, so the KPI can say it describes part of the set. The handler
    # returned this key before the field existed here, and `response_model`
    # silently DROPPED it (Pydantic's default `extra="ignore"`) — the figure
    # went from wrong-but-complete to right-but-silently-partial over HTTP,
    # which is the worse of the two. See decisions §62.
    excluded_rebate_count: int = 0
    open_exceptions: int
    touchless_rate: float
    stale_approvals: int
    pipeline: dict[str, int]
    vendor_spend: list[VendorSpendEntry]
    aging: AgingBuckets
    aging_reporting: ReportingAgingBuckets
    monthly_trend: list[MonthlyTrendEntry]
    upcoming_payments: list[UpcomingPayment]
    upcoming_total_amount: MoneyAmount
    upcoming_total_amount_reporting: MoneyAmount
    upcoming_unconverted_count: int
    processing_time: ProcessingTimeMetrics
    approval_bottleneck: list[ApprovalBottleneckEntry]
    discount_capture: DiscountCapture
