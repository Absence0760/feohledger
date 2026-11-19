"""The figures behind `GET /api/dashboard`, one async function per section.

`api/dashboard.get_dashboard` owns auth, scope resolution and the response
shape; everything it reports is computed here. Each section takes a
`DashboardScope` (the tenant session, the selected entity, the UTC date and the
org's reporting currency) and returns that section's data, already in the shape
the response carries.

**Await order is part of the contract.** The handler awaits the sections in a
fixed order and each one issues its queries in a fixed order;
`tests/test_dashboard_aggregations.py` feeds a mocked session its results by
position, so reordering sections or queries breaks it loudly. Change the order
only together with that file's numbered list.

Every Invoice / Payment / Exception query is entity-scoped through the scope's
`inv` / `pay` / `exc` (None = consolidated). Metrics keyed by invoice id
(processing time) inherit the scope from the invoice query they consume;
WorkflowStep- and AuditLog-derived inputs have no entity column and stay
org-wide until the workflow engine is entity-aware.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Date, and_, case, cast, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.models.virtual_card import CardRebate, VirtualCard
from app.services.analytics import (
    OPEN_AP_STATUSES,
    compute_touchless_rate,
)
from app.services.currency_conversion import (
    InvoiceReportingAmountSql,
    card_currency_sql,
    invoice_reporting_amount_sql,
    payment_reporting_amount_sql,
    reporting_amount_for_row,
    rollup_from_grouped_rows,
)
from app.services.open_ap_due import (
    OUTFLOW_STATUSES,
    effective_due_date,
    is_overdue,
    latest_schedule_subquery,
)
from app.services.payment_adapters.base import PENDING_PAYMENT_STATUSES
from app.services.touchless import touchless_counts_select
from app.tenant import apply_entity_scope


# Lightweight row shape the analytics service expects. Defined at module level
# so it is cheap to instantiate per row and trivial to mock in tests.
@dataclass
class SimpleNamespaceStep:
    assigned_to: object | None
    created_at: datetime | None
    assignee_name: str | None


@dataclass(frozen=True, eq=False)
class DashboardScope:
    """What every section reads: the tenant session, the selected entity (None
    = consolidated), the UTC date the request is judged against, and the
    org's reporting currency."""

    db: AsyncSession
    entity_id: uuid.UUID | None
    today: date
    reporting_currency: str
    # The reporting-currency CASE pair every invoice money rollup on the page
    # shares, built once (see `reporting_rollup` for why it is imported, never
    # re-derived).
    rep: InvoiceReportingAmountSql = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rep",
            invoice_reporting_amount_sql(
                reporting_currency=self.reporting_currency,
                amount=Invoice.amount,
                currency=Invoice.currency,
                persisted_reporting_amount=Invoice.reporting_amount,
                persisted_reporting_currency=Invoice.reporting_currency,
            ),
        )

    def inv(self, q):
        return apply_entity_scope(q, Invoice, self.entity_id)

    def pay(self, q):
        return apply_entity_scope(q, Payment, self.entity_id)

    def exc(self, q):
        return apply_entity_scope(q, APException, self.entity_id)


async def organization_has_invoices(
    db: AsyncSession, entity_id: uuid.UUID | None, scoped_count: int
) -> bool:
    """Does the WHOLE organization have an invoice, whichever entity is selected?

    The figures on this page are entity-scoped, so `total_invoices == 0` on a
    new subsidiary says nothing about whether the workspace is new — which is
    how an established tenant came to be welcomed with "upload your first
    invoice" the day it added an entity (#635). Unscoped, the consolidated
    count already answers it and costs no query.
    """
    if entity_id is None or scoped_count > 0:
        return scoped_count > 0
    return (await db.execute(select(Invoice.id).limit(1))).first() is not None


async def invoice_totals(s: DashboardScope) -> tuple[int, Decimal]:
    """`(total_invoices, total_amount)`.

    `total_amount` (the "Total Amount" KPI on the web dashboard) sums EVERY
    invoice regardless of status or date — no `.where()` at all. This is a
    DIFFERENT population from the CFO analytics `total_spend`
    (`GET /api/analytics/cfo` — a trailing `period_days` window that
    excludes only `rejected` invoices): a rejected or brand-new invoice
    counts here but not there, and this figure has no date bound while that
    one does. Both are intentional, but a caller/label that treats them as
    interchangeable will misreport. See backend/docs/analytics.md and
    `tests/test_analytics_rejected_exclusion.py`.
    """
    totals = await s.db.execute(
        s.inv(select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.amount), 0)))
    )
    total_invoices, total_amount = totals.one()
    return total_invoices or 0, Decimal(str(total_amount))


async def reporting_rollup(s: DashboardScope) -> dict:
    """The response's `reporting` block: the whole invoice book re-rolled into
    the org's reporting currency, plus the per-currency split.

    `total_amount` is a naive SUM that mixes currencies — fine when every
    invoice is in one currency, wrong the moment an org books a EUR invoice
    alongside USD ones. This uses each row's rate-locked `reporting_amount`
    (foreign rows without a lock fall back to face value and are counted in
    `unconverted_count`). See backend/docs/multi-currency.md.

    Aggregated per currency in SQL instead of streaming every invoice into
    Python. The CASE expressions come from
    `currency_conversion.invoice_reporting_amount_sql`, the SQL statement of
    `reporting_amount_for_row`: a row is "locked" (use its persisted
    `reporting_amount`) iff that column is set AND its `reporting_currency`
    equals the org's target; otherwise it falls back to face `amount`, and a
    foreign row (currency != target) is counted as unconverted.
    `rollup_from_grouped_rows` reduces these per-currency sums to the exact
    same `ReportingRollup` the row-at-a-time path produced.

    This endpoint used to spell those two CASEs itself. They are imported for
    the same reason the vendor tile reuses them rather than re-deriving: a
    second copy of the conversion rule is how two figures on one page start
    disagreeing. The imported version is also STRICTER in one respect — it
    requires `reporting_currency IS NOT NULL` explicitly, where the local copy
    left that to `UPPER(NULL) = tgt`. Under SQL's three-valued logic that made
    the lock test NULL for a row carrying a `reporting_amount` but no currency
    code, `NOT NULL` is NULL, and such a row fell through the `unconverted`
    CASE and was reported as converted while its FACE value was what got
    added. See `tests/test_dashboard_vendor_spend.py`.
    """
    cur_key = s.rep.currency_key
    rollup_rows = await s.db.execute(
        s.inv(
            select(
                cur_key.label("currency"),
                func.coalesce(func.sum(Invoice.amount), 0),
                func.coalesce(func.sum(s.rep.amount), 0),
                func.count(),
                func.coalesce(func.sum(s.rep.unconverted), 0),
            ).group_by(cur_key)
        )
    )
    rollup = rollup_from_grouped_rows(
        [
            {
                "currency": r[0],
                "original_amount": r[1],
                "reporting_amount": r[2],
                "count": r[3],
                "unconverted_count": r[4],
            }
            for r in rollup_rows.all()
        ],
        reporting_currency=s.reporting_currency,
    )
    return {
        "reporting_currency": rollup.reporting_currency,
        "total_amount": rollup.total_reporting_amount,
        "total_count": rollup.total_count,
        "unconverted_count": rollup.unconverted_count,
        "by_currency": [
            {
                "currency": e.currency,
                "original_amount": e.original_amount,
                "reporting_amount": e.reporting_amount,
                "count": e.count,
                "unconverted_count": e.unconverted_count,
            }
            for e in rollup.by_currency
        ],
    }


async def pipeline(s: DashboardScope) -> dict[str, int]:
    """Invoice count per status."""
    status_rows = await s.db.execute(
        s.inv(select(Invoice.status, func.count(Invoice.id)).group_by(Invoice.status))
    )
    return {
        str(row[0].value if hasattr(row[0], "value") else row[0]): row[1]
        for row in status_rows.all()
    }


async def vendor_spend(s: DashboardScope) -> list[dict]:
    """Spend by vendor (top 10).

    Excludes rejected invoices (they were never real spend), matching the CFO
    analytics concentration figure. Rolled up into the org's reporting
    currency (not a naive SUM across currencies) — a vendor billing in more
    than one currency used to add e.g. USD + EUR as if they were one currency.

    Grouped in SQL, like the rollup / pipeline / aging / trend sections, and
    through the very same reporting CASE — so a vendor's total is built by
    exactly the rule the whole-book rollup uses. This tile used to select five
    columns of EVERY non-rejected invoice (all time, no LIMIT) and fold them in
    Python: a synchronous per-row loop inside an `async def`, occupying the
    event loop for its whole duration and growing linearly with the invoice
    table while its four neighbours stayed one aggregate each. See
    backend/docs/analytics.md § Vendor spend is grouped in SQL for the
    measured before/after.

    The ordering rule is (total DESC, vendor ASC). The Python fold sorted on
    the total alone and Python's sort is stable, so ties fell back to DB scan
    order — which of two equal-spend vendors took rank 10 (and which fell off
    the `[:10]`) could differ between two identical requests. The name
    tiebreak makes the tile reproducible.

    Each row also carries `unconverted_count` — how many of that vendor's
    invoices went into `total` at FACE value because no rate lock bridged
    them. The tile folded them silently before: its `{vendor, amount}` shape
    had nowhere to say so, so a EUR-billing vendor with no locks ranked
    against USD vendors on a number that was not in the same currency as the
    label above it. It is one more `SUM` over a CASE already in this query,
    and a fallback nobody reports is just a wrong number
    (`docs/decisions.md` §35).
    """
    vendor_total = func.coalesce(func.sum(s.rep.amount), 0)
    vendor_unconv = func.coalesce(func.sum(s.rep.unconverted), 0)
    vendor_rows = await s.db.execute(
        s.inv(
            select(
                Invoice.vendor_name,
                vendor_total.label("total"),
                vendor_unconv.label("unconverted_count"),
            )
            .where(
                Invoice.vendor_name.isnot(None),
                Invoice.vendor_name != "",
                Invoice.status != "rejected",
            )
            .group_by(Invoice.vendor_name)
            .order_by(vendor_total.desc(), Invoice.vendor_name.asc())
            .limit(10)
        )
    )
    # `Invoice.amount` and `Invoice.reporting_amount` are both `Numeric(15, 2)`,
    # so a Postgres SUM over them is already exact at 2dp — summing then
    # quantizing matches the row-at-a-time quantize-then-sum the Python fold
    # did, the same argument `rollup_from_grouped_rows` makes for the whole-book
    # rollup. Stays `Decimal` into the response; `DashboardResponse` does the
    # single float hop at serialization, like every other money figure here.
    return [
        {
            "vendor": vendor,
            "amount": Decimal(str(total)),
            "unconverted_count": int(unconverted or 0),
        }
        for vendor, total, unconverted in vendor_rows.all()
    ]


def _empty_aging_bands() -> dict[str, Decimal]:
    return {
        "current": Decimal("0"),
        "days_30": Decimal("0"),
        "days_60": Decimal("0"),
        "days_90": Decimal("0"),
        "days_90_plus": Decimal("0"),
    }


async def aging(s: DashboardScope) -> tuple[dict, dict]:
    """`(aging, aging_reporting)` — open AP by days past due:
    current (not yet due) / 1-30 / 31-60 / 61-90 / 90+.

    Accumulated in Decimal (money is never summed in float — many small floats
    drift) and stays Decimal in the response — `DashboardResponse`'s
    `MoneyAmount` fields do the float hop once, at JSON-serialization time.

    `aging_reporting` is the reporting-currency counterpart — `aging` sums the
    raw `Invoice.amount` across currencies, same mistake `total_amount` had
    before the `reporting` rollup was added. It reuses the reporting CASE: the
    persisted rate-locked `reporting_amount` when it's locked for the org's
    target currency, else face `amount` (foreign rows without a lock fall back
    to face value, same fallback the rest of this endpoint's reporting figures
    use) — and its `unconverted_count` counts exactly those fallbacks, so the
    chart can say its bands are part-converted instead of implying they are
    all in one currency.

    ONE count for the whole band set, not five. `AgingBuckets` is a single
    object holding five sums, so a per-band count would mean five more fields
    on a schema that two response keys share; the actionable fact — "some of
    this is unconverted" — is the same either way, and the invoices behind it
    are listable from `/invoices`. The bare `aging` deliberately gets no count
    at all: it is a face-value cross-currency sum in its entirety, so "N rows
    could not be converted" would understate it.

    Aging covers the same open-payable population as the AP balance so the
    bands reconcile with it (F-4): approved → payment_scheduled. The AP
    balance has no due_date filter, so aging must not either — an open invoice
    missing a due date used to inflate the balance while vanishing from every
    bucket. A null due_date can't be judged overdue, so it buckets as
    "current" (the conservative read) rather than being dropped. Bucket + sum
    in SQL (Postgres `date - date` is integer days) rather than pulling every
    open row into Python; the band boundaries match the old loop exactly.
    """
    aging_dec = _empty_aging_bands()
    aging_reporting_dec = _empty_aging_bands()
    aging_reporting_unconverted = 0
    days_past = s.today - Invoice.due_date
    aging_bucket = case(
        (Invoice.due_date.is_(None), "current"),
        (days_past <= 0, "current"),
        (days_past <= 30, "days_30"),
        (days_past <= 60, "days_60"),
        (days_past <= 90, "days_90"),
        else_="days_90_plus",
    )
    aging_rows = await s.db.execute(
        s.inv(
            select(
                aging_bucket.label("bucket"),
                func.coalesce(func.sum(Invoice.amount), 0),
                func.coalesce(func.sum(s.rep.amount), 0),
                func.coalesce(func.sum(s.rep.unconverted), 0),
            )
            .where(Invoice.status.in_(OPEN_AP_STATUSES))
            .group_by(aging_bucket)
        )
    )
    for bucket, total, rep_total, unconverted in aging_rows.all():
        aging_dec[bucket] = Decimal(str(total))
        aging_reporting_dec[bucket] = Decimal(str(rep_total))
        aging_reporting_unconverted += int(unconverted or 0)
    return aging_dec, {**aging_reporting_dec, "unconverted_count": aging_reporting_unconverted}


async def monthly_trend(s: DashboardScope) -> list[dict]:
    """Monthly trend (last 6 calendar months), bucketed by calendar month in
    SQL rather than streaming every recent invoice into Python. Summing
    amounts in the DB (Numeric) also avoids the float-accumulation drift the
    old per-row `+= float(...)` fold could introduce.

    The window is anchored to a MONTH BOUNDARY, not `today - 180 days`. A
    rolling day window against a calendar-month GROUP BY is a mismatch: 180
    days reaches back into a SEVENTH month on most dates, so the chart drew
    seven bars whose oldest was a partial slice of a month — a stub that reads
    as a collapse in spend and shifts every day as the window slides.
    Anchoring gives exactly six buckets of like-for-like whole months (the
    newest is the month in progress, which is inherent to a to-date trend).
    """
    today = s.today
    months_back = 5
    anchor = today.year * 12 + (today.month - 1) - months_back
    trend_start = today.replace(year=anchor // 12, month=anchor % 12 + 1, day=1)
    month_key = func.to_char(func.date_trunc("month", Invoice.invoice_date), "YYYY-MM")
    trend_rows = await s.db.execute(
        s.inv(
            select(
                month_key.label("month"),
                func.count(),
                func.coalesce(func.sum(Invoice.amount), 0),
                # Reporting-currency counterpart — same reporting CASE as the
                # aging buckets / whole-book rollup.
                func.coalesce(func.sum(s.rep.amount), 0),
                # ...and the same unconverted CASE, so each BAR says whether
                # its own `reporting_amount` is fully converted. Per month
                # rather than one figure for the series, because a trend is
                # read bar against bar: a single foreign vendor onboarding in
                # March contaminates March, and a whole-series count would not
                # say which step in the line is the one not to trust.
                func.coalesce(func.sum(s.rep.unconverted), 0),
            )
            .where(Invoice.invoice_date >= trend_start, Invoice.invoice_date.isnot(None))
            .group_by(month_key)
            .order_by(month_key)
        )
    )
    return [
        {
            "month": month,
            "count": count,
            "amount": Decimal(str(amount)),
            "reporting_amount": Decimal(str(rep_amount)),
            "unconverted_count": int(unconverted or 0),
        }
        for month, count, amount, rep_amount, unconverted in trend_rows.all()
    ]


async def upcoming_payments(s: DashboardScope) -> dict:
    """Upcoming payments (due within 7 days + overdue), capped at ten rows,
    with their totals: `upcoming_payments`, `upcoming_total_amount`,
    `upcoming_total_amount_reporting`, `upcoming_unconverted_count`."""
    today = s.today
    week_ahead = today + timedelta(days=7)
    paid_ids = select(Payment.invoice_id).where(Payment.status == "completed").scalar_subquery()
    upcoming_rows = await s.db.execute(
        s.inv(
            select(
                Invoice.id,
                Invoice.invoice_number,
                Invoice.vendor_name,
                Invoice.amount,
                Invoice.due_date,
                Invoice.currency,
                Invoice.reporting_amount,
                Invoice.reporting_currency,
            )
            .where(
                Invoice.due_date.isnot(None),
                Invoice.due_date <= week_ahead,
                Invoice.id.notin_(paid_ids),
            )
            .order_by(Invoice.due_date)
            .limit(10)
        )
    )
    # Materialize once — the list AND its total are both derived from these
    # same rows, so the total covers exactly the population the client
    # renders (no separate query, no drift between count and total).
    rows = upcoming_rows.all()
    upcoming = [
        {
            "id": str(r[0]),
            "invoice_number": r[1],
            "vendor_name": r[2],
            "amount": r[3],
            # The code `amount` is denominated in — without it a client can
            # only label the row with the org's reporting currency, which is
            # wrong for every foreign invoice in the list.
            "currency": r[5],
            "due_date": r[4].isoformat() if r[4] else None,
            "is_overdue": r[4] < today if r[4] else False,
        }
        for r in rows
    ]
    # Sum in Decimal (each r[3] is already a Decimal off the Numeric column)
    # and stay Decimal — never fold the per-item floats above, which is
    # exactly the client-side bug this mirrors (mobile issue #189: summing
    # already-lossy floats accumulates drift). The float hop happens once,
    # at JSON-serialization time, via `DashboardResponse`'s `MoneyAmount`.
    total_amount = sum((r[3] for r in rows), Decimal("0"))
    # Reporting-currency counterpart — `total_amount` above sums the raw
    # per-row `amount`, which mixes currencies the moment one of these ten
    # invoices is foreign. Resolved row-at-a-time via `reporting_amount_for_row`
    # (the same helper `/payments/queue` uses for its own "amount due" total) —
    # a row whose reporting figure can't be established still counts at face
    # value (dropping it would understate what's due) but is tallied on
    # `upcoming_unconverted_count`.
    total_reporting = Decimal("0")
    unconverted_count = 0
    for r in rows:
        rep_amt, unconverted = reporting_amount_for_row(
            amount=r[3],
            currency=r[5],
            reporting_currency=s.reporting_currency,
            persisted_reporting_currency=r[7],
            persisted_reporting_amount=r[6],
        )
        total_reporting += rep_amt
        if unconverted:
            unconverted_count += 1
    return {
        "upcoming_payments": upcoming,
        "upcoming_total_amount": total_amount,
        "upcoming_total_amount_reporting": total_reporting,
        "upcoming_unconverted_count": unconverted_count,
    }


async def overdue_and_due_soon(s: DashboardScope) -> dict:
    """`overdue_count` / `overdue_total` and `due_soon_count` / `due_soon_total`.

    Counted in SQL over the whole book — not folded from `upcoming_payments`,
    which is capped at ten rows (and, sorted by due date, fills with overdue
    rows first), so a count derived from it stops at ten and a due-soon count
    read off it can reach zero while invoices fall due this week. The
    population, the effective due date (latest payment schedule, else the
    invoice's own) and "overdue" are `services/open_ap_due`'s, the same
    statement the cash-flow forecast reads — so `overdue_count` here and the
    /cfo "Overdue (owed now)" card count the same invoices (decisions §449).
    Due soon is today through seven days out, the window `upcoming_payments`
    uses on its future side.

    Per currency, at FACE value, grouped on the invoice's own code: a count of
    invoices owed now has no business depending on whether each one has a
    rate lock, and a client shows `€1.2K · $3.4K` rather than a converted
    figure that may be part face value. A row with no code groups under
    `null`, which a client renders without a symbol rather than guessing one.
    """
    today = s.today
    week_ahead = today + timedelta(days=7)
    due_latest = latest_schedule_subquery()
    due = effective_due_date(due_latest)
    overdue = is_overdue(due, today)
    due_soon = and_(due >= today, due <= week_ahead)
    due_ccy = func.nullif(func.upper(func.trim(Invoice.currency)), "")
    due_rows = await s.db.execute(
        s.inv(
            select(
                due_ccy.label("currency"),
                func.count(case((overdue, Invoice.id))),
                func.coalesce(func.sum(case((overdue, Invoice.amount))), 0),
                func.count(case((due_soon, Invoice.id))),
                func.coalesce(func.sum(case((due_soon, Invoice.amount))), 0),
            )
            .outerjoin(due_latest, due_latest.c.invoice_id == Invoice.id)
            .where(Invoice.status.in_(OUTFLOW_STATUSES), due.isnot(None), due <= week_ahead)
            .group_by(due_ccy)
            .order_by(due_ccy.asc().nulls_last())
        )
    )
    overdue_total: list[dict] = []
    due_soon_total: list[dict] = []
    for ccy, n_overdue, overdue_sum, n_soon, soon_sum in due_rows.all():
        if n_overdue:
            overdue_total.append(
                {"currency": ccy, "amount": Decimal(str(overdue_sum)), "count": int(n_overdue)}
            )
        if n_soon:
            due_soon_total.append(
                {"currency": ccy, "amount": Decimal(str(soon_sum)), "count": int(n_soon)}
            )
    return {
        "overdue_count": sum(r["count"] for r in overdue_total),
        "overdue_total": overdue_total,
        "due_soon_count": sum(r["count"] for r in due_soon_total),
        "due_soon_total": due_soon_total,
    }


async def touchless_rate(s: DashboardScope) -> float:
    """The share of DECIDED invoices that went straight through: approved
    automatically, no field or line-item correction, no exception a person
    decided. Classified entirely by the shared predicates in
    `services/touchless` (the experiments readout calls the same ones, so the
    two figures cannot disagree), counted in ONE query so the numerator is a
    filter of exactly the denominator. The population rules — the
    evidence-gated `done`/`paid`/`failed` legs and the exclusion of
    CSV-imported history from both legs — live there and in
    `services/analytics`; see `backend/docs/analytics.md` § Touchless rate.
    """
    touchless_count, decided_count = (await s.db.execute(s.inv(touchless_counts_select()))).one()
    # Percentages are not currency: the schema keeps this a float, and this is
    # its single hop from the exact Decimal the helper computes.
    return float(
        compute_touchless_rate(touchless_count=touchless_count, decided_count=decided_count)
    )


async def payment_totals(s: DashboardScope) -> dict:
    """`total_paid` / `total_pending` and their reporting-currency
    counterparts with the counts of payments each one had to leave out."""
    # Separate queries to avoid complex CASE expressions.
    paid_q = await s.db.execute(
        s.pay(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "completed")
        )
    )
    total_paid = Decimal(str(paid_q.scalar() or 0))

    # The canonical in-flight set, imported rather than restated. This list was
    # a hand-copy that had already drifted: `submitted` and `pending_compliance`
    # were missing, so money authorized and moving — or held by the sanctions
    # gate waiting on a human — showed in NEITHER dashboard KPI (not paid, not
    # pending) and simply vanished from the landing page. `/payments/summary`
    # states exactly that reasoning where the tuple is defined.

    pending_q = await s.db.execute(
        s.pay(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status.in_(PENDING_PAYMENT_STATUSES)
            )
        )
    )
    total_pending = Decimal(str(pending_q.scalar() or 0))

    # Reporting-currency counterparts — `total_paid` / `total_pending` above
    # sum the raw `Payment.amount`, which is denominated in the INVOICE's
    # currency, not the org's reporting currency
    # (`currency_conversion.payment_reporting_amount_sql`'s docstring). A book
    # with one foreign-currency payment makes both a silent two-currency
    # mixture. Resolved the same way `GET /api/payments/summary` resolves its
    # own `total_paid` / `total_pending`: the rate-locked home-currency leg
    # when the payment carries one, else the payment's own amount when the
    # invoice is already in the reporting currency; a payment neither rung can
    # establish is excluded and counted rather than added at face value.
    reported_payment = payment_reporting_amount_sql(
        reporting_currency=s.reporting_currency,
        payment_amount=Payment.amount,
        payment_source_amount=Payment.source_amount,
        payment_source_currency=Payment.source_currency,
        invoice_currency=Invoice.currency,
    )
    reported_countable = reported_payment.is_expressible

    async def _reporting_payment_total(status_clause) -> tuple[Decimal, int]:
        q = s.pay(
            select(
                func.coalesce(func.sum(case((reported_countable, reported_payment.amount))), 0),
                func.count(case((not_(reported_countable), Payment.id))),
            )
            .select_from(Payment)
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .where(status_clause)
        )
        total, excluded = (await s.db.execute(q)).one()
        return Decimal(str(total or 0)), int(excluded or 0)

    paid_reporting, paid_unconverted = await _reporting_payment_total(Payment.status == "completed")
    pending_reporting, pending_unconverted = await _reporting_payment_total(
        Payment.status.in_(PENDING_PAYMENT_STATUSES)
    )
    return {
        "total_paid": total_paid,
        "total_pending": total_pending,
        "total_paid_reporting": paid_reporting,
        "total_pending_reporting": pending_reporting,
        "total_paid_unconverted_count": paid_unconverted,
        "total_pending_unconverted_count": pending_unconverted,
    }


async def rebates(s: DashboardScope) -> tuple[Decimal, int]:
    """`(total_rebates, excluded_rebate_count)`.

    CardRebate carries no entity_id of its own — join to VirtualCard (which
    does, via EntityMixin) so switching the entity selector scopes this KPI
    like every other one on the page instead of silently staying a whole-org
    total. The join also carries the CURRENCY: `card_rebates` has no currency
    column either, so this was a bare cross-currency SUM rendered on the
    dashboard as one "Rebates Earned" figure — a quantity in no currency at
    all. `card_currency_sql` is the one owner of that expression (see
    `services/currency_conversion`), and the `!=` count is what stops a
    single-currency figure looking complete.
    """
    reporting_currency = s.reporting_currency
    _rebate_ccy = card_currency_sql(reporting_currency)
    rebate_row = (
        await s.db.execute(
            apply_entity_scope(
                select(
                    func.coalesce(
                        func.sum(case((_rebate_ccy == reporting_currency, CardRebate.amount))), 0
                    ),
                    func.count(case((_rebate_ccy != reporting_currency, CardRebate.id))),
                )
                .select_from(CardRebate)
                .join(VirtualCard, CardRebate.virtual_card_id == VirtualCard.id),
                VirtualCard,
                s.entity_id,
            )
        )
    ).one()
    return Decimal(str(rebate_row[0] or 0)), int(rebate_row[1] or 0)


async def stale_approvals(s: DashboardScope) -> int:
    """Invoices waiting in review for more than three days."""
    stale_date = s.today - timedelta(days=3)
    stale_q = await s.db.execute(
        s.inv(
            select(func.count()).where(
                Invoice.status == "ready_for_review",
                Invoice.created_at <= stale_date,
            )
        )
    )
    return stale_q.scalar() or 0


async def open_exceptions(s: DashboardScope) -> int:
    """Exceptions still open or escalated."""
    exc_q = await s.db.execute(
        s.exc(select(func.count()).where(APException.status.in_(["open", "escalated"])))
    )
    return exc_q.scalar() or 0


async def processing_time(s: DashboardScope) -> dict:
    """Avg + median + p95 from invoice creation to (a) its FIRST approval (the
    earliest `invoice.approved` audit row) and (b) its first completed payment
    (`MIN(Payment.completed_at)`). Both legs collapse to 0 below the min
    sample (`services/analytics.processing_time_from_day_counts`).

    Set-based: each leg comes back as (days rounded to 0.1, how many
    invoices) — a few thousand rows at most, bounded by the SPAN of the
    history, never by the number of invoices in it. This block used to stream
    every approval audit row, every completed payment and then every matching
    invoice into Python, and the last step bound one parameter per invoice
    id: asyncpg refuses a statement with more than 32 767, a broad `except`
    around the block swallowed the error, and every tenant past ~33k approved
    invoices saw a processing-time tile of zeros. Rounding happens in SQL
    (`round(numeric, 1)`, half away from zero) exactly as
    `analytics._decimal_days` rounds, so the figures are the ones the
    row-at-a-time path produced. `docs/decisions.md` §218.
    """
    from app.models.workflow import AuditLog
    from app.services.analytics import processing_time_from_day_counts

    first_approval = (
        select(
            AuditLog.entity_id.label("invoice_id"),
            func.min(AuditLog.created_at).label("at"),
        )
        .where(AuditLog.entity_type == "invoice", AuditLog.action == "invoice.approved")
        .group_by(AuditLog.entity_id)
        .subquery("first_approval")
    )
    first_paid = (
        select(
            Payment.invoice_id.label("invoice_id"),
            func.min(Payment.completed_at).label("at"),
        )
        .where(Payment.status == "completed", Payment.completed_at.isnot(None))
        .group_by(Payment.invoice_id)
        .subquery("first_paid")
    )

    async def _leg_day_counts(milestone) -> list[tuple[Decimal, int]]:
        # Entity scope rides the invoice side of the join, as it did when the
        # invoice ids were filtered by `inv(...)` after the fact.
        days = func.round(
            (func.extract("epoch", milestone.c.at) - func.extract("epoch", Invoice.created_at))
            / 86400,
            1,
        )
        q = s.inv(
            select(days, func.count())
            .select_from(Invoice)
            .join(milestone, milestone.c.invoice_id == Invoice.id)
            .where(Invoice.created_at.isnot(None))
            .group_by(days)
        )
        return [(Decimal(d), int(n)) for d, n in (await s.db.execute(q)).all()]

    pt = processing_time_from_day_counts(
        await _leg_day_counts(first_approval), await _leg_day_counts(first_paid)
    )
    return {
        "avg_upload_to_approval_days": float(pt.avg_upload_to_approval_days),
        "median_upload_to_approval_days": float(pt.median_upload_to_approval_days),
        "p95_upload_to_approval_days": float(pt.p95_upload_to_approval_days),
        "avg_upload_to_paid_days": float(pt.avg_upload_to_paid_days),
        "median_upload_to_paid_days": float(pt.median_upload_to_paid_days),
        "p95_upload_to_paid_days": float(pt.p95_upload_to_paid_days),
        "count_approval_leg": pt.count_approval_leg,
        "count_paid_leg": pt.count_paid_leg,
    }


async def approval_bottleneck(s: DashboardScope) -> list[dict]:
    """Per-approver pending counts + oldest age + average age (top ten). Reads
    WorkflowStep rows where step_type='approval' and completed_at IS NULL."""
    from app.models.workflow import WorkflowStep
    from app.services.analytics import compute_approval_bottleneck

    step_rows = await s.db.execute(
        select(
            WorkflowStep.assigned_to,
            WorkflowStep.created_at,
        ).where(
            WorkflowStep.step_type == "approval",
            WorkflowStep.completed_at.is_(None),
        )
    )
    pending_steps = [
        SimpleNamespaceStep(
            assigned_to=row[0],
            created_at=row[1],
            assignee_name=None,
        )
        for row in step_rows.all()
    ]
    return [
        {
            "approver_id": r.approver_id,
            "approver_name": r.approver_name,
            "pending_count": r.pending_count,
            "oldest_pending_days": float(r.oldest_pending_days),
            "avg_pending_days": float(r.avg_pending_days),
        }
        for r in compute_approval_bottleneck(pending_steps)[:10]
    ]


async def discount_capture(s: DashboardScope) -> dict:
    """Every discount-scheduled invoice sorted into captured / missed /
    still-capturable.

    Joins Invoice → its CURRENT PaymentSchedule → Payment(completed). The
    schedule side is `payment_schedules.latest_schedules()`, never the table:
    an invoice is one discount, on the terms of its current schedule, so a
    superseded row must not count it a second time (it did, while this joined
    every row).

    Grouped in SQL, like every other figure on this page — three rows come
    back, one per bucket. This block used to stream EVERY discount-scheduled
    invoice (`WHERE discount_percent IS NOT NULL`, no LIMIT, no date bound)
    into a synchronous Python projection inside an `async def`, occupying the
    event loop for its whole duration and growing without bound as a tenant
    books more discount terms. It was the last unbounded fold on this page.

    It resisted the plain per-currency `GROUP BY` its five neighbours got for
    a real reason: the fold needs a per-ROW verdict, not a per-currency sum.
    But that verdict is only two comparisons, so it becomes a CASE and the
    aggregation happens around it. What stays in Python is what was never
    per-row — the bucket vocabulary, the capture rate, and the
    `insufficient_data` state — via `discount_capture_from_grouped_rows`, the
    same reference-implementation-plus-grouped-reducer pair
    `currency_conversion.rollup_from_grouped_rows` established for the
    whole-book rollup, with an equivalence test pinning the two together.
    """
    from app.services.analytics import discount_capture_from_grouped_rows
    from app.services.payment_schedules import latest_schedules

    sched = latest_schedules()

    # The discount is a percentage of the invoice, so the reporting figure is
    # the same percentage of the invoice's REPORTING amount — the rate-locked
    # one when it exists and face value (counted as unconverted) when it does
    # not, exactly as every other money rollup on this page.
    #
    # Rounded to cents PER ROW, where the Python fold rounded: quantize-then-sum,
    # not sum-then-quantize. Postgres `round(numeric, 2)` breaks an exact
    # half-cent tie away from zero where `Decimal.quantize`'s default context
    # broke it to even, so a discount landing on exactly x.xx5 now rounds up.
    # That is the project's documented money convention (`ROUND_HALF_UP` —
    # "matches what auditors expect") and it is what `discount_offers.
    # discount_savings` already uses for this very quantity, base × percent ÷
    # 100; this tile was the one place computing it the other way.
    disc_amount = func.round(Invoice.amount * sched.discount_percent / 100, 2)
    disc_amount_reporting = func.round(s.rep.amount * sched.discount_percent / 100, 2)
    # One row per invoice, with the completed payments collapsed to the
    # earliest `completed_at`. The schedule columns ride in the GROUP BY only
    # because they are selected; `latest_schedules()` already gives each
    # invoice exactly one schedule, so an invoice that carries a superseded
    # schedule on different terms is still ONE row, on its current terms.
    eligible = (
        s.inv(
            select(
                disc_amount.label("discount_amount"),
                disc_amount_reporting.label("discount_amount_reporting"),
                s.rep.unconverted.label("unconverted"),
                sched.discount_date.label("discount_date"),
                func.min(Payment.completed_at).label("paid_at"),
            )
            .join(sched, sched.invoice_id == Invoice.id)
            .outerjoin(
                Payment,
                (Payment.invoice_id == Invoice.id) & (Payment.status == "completed"),
            )
            .where(sched.discount_percent.isnot(None))
            .group_by(
                Invoice.id,
                Invoice.amount,
                Invoice.currency,
                Invoice.reporting_amount,
                Invoice.reporting_currency,
                sched.discount_percent,
                sched.discount_date,
            )
        )
    ).subquery()
    # `completed_at` is `timestamptz`, and asyncpg handed Python a UTC-aware
    # datetime whose `.date()` was therefore the UTC date. A bare `::date`
    # would read the SESSION's TimeZone instead, so the conversion is spelled
    # out: this is a payment-against-deadline comparison, and taking it off
    # the server's local calendar is the class of bug `utils/dates.utc_today`
    # exists to prevent.
    paid_on = cast(func.timezone("UTC", eligible.c.paid_at), Date)
    # An invoice whose discount deadline has NOT passed has missed nothing
    # yet — it is still capturable, and the four other consumers of these
    # economics all gate on that. This surface was the one that didn't, so
    # the dashboard reported a growing pile of "missed" savings that were in
    # fact still on the table. A row with no `discount_date` has no window
    # that can be shown to have elapsed, so it is undecided too, never a
    # miss. The second leg is `analytics.discount_window_open` negated; the
    # first is the paid-before-the-deadline test — both in SQL.
    bucket = case(
        (
            and_(
                eligible.c.paid_at.isnot(None),
                eligible.c.discount_date.isnot(None),
                paid_on <= eligible.c.discount_date,
            ),
            "captured",
        ),
        (
            and_(
                eligible.c.discount_date.isnot(None),
                eligible.c.discount_date < s.today,
            ),
            "missed",
        ),
        else_="pending",
    )
    bucket_rows = await s.db.execute(
        select(
            bucket.label("bucket"),
            func.count(),
            func.coalesce(func.sum(eligible.c.discount_amount), 0),
            func.coalesce(func.sum(eligible.c.discount_amount_reporting), 0),
            func.coalesce(func.sum(eligible.c.unconverted), 0),
        ).group_by(bucket)
    )
    discount = discount_capture_from_grouped_rows(
        [
            {
                "bucket": row[0],
                "count": row[1],
                "amount": row[2],
                "reporting_amount": row[3],
                "unconverted_count": row[4],
            }
            for row in bucket_rows.all()
        ]
    )
    return {
        "eligible_count": discount.eligible_count,
        "captured_count": discount.captured_count,
        "missed_count": discount.missed_count,
        # Still-capturable windows — NOT missed. See
        # `analytics.DiscountCaptureMetrics`.
        "pending_count": discount.pending_count,
        "captured_amount": discount.captured_amount,
        "missed_amount": discount.missed_amount,
        "pending_amount": discount.pending_amount,
        # Reporting-currency counterparts of the three lines above.
        "reporting_currency": s.reporting_currency,
        "captured_amount_reporting": discount.captured_amount_reporting,
        "missed_amount_reporting": discount.missed_amount_reporting,
        "pending_amount_reporting": discount.pending_amount_reporting,
        "unconverted_count": discount.unconverted_count,
        # `None`, never 0.0, when nothing has been decided yet — 0% reads
        # as "we captured none of them", the opposite of the truth.
        "capture_rate_pct": (
            float(discount.capture_rate_pct) if discount.capture_rate_pct is not None else None
        ),
        "insufficient_data": discount.insufficient_data,
    }
