"""Dashboard aggregation endpoints — rich KPIs for the main page."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import Date, and_, case, cast, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.user import User
from app.models.virtual_card import CardRebate, VirtualCard
from app.schemas.dashboard import DashboardResponse
from app.services.analytics import (
    OPEN_AP_STATUSES,
    TOUCHLESS_REVIEW_EVIDENCE_STATUSES,
    compute_touchless_rate,
)
from app.services.csv_import import (
    imported_invoice_clause,
    native_invoice_clause,
)
from app.services.currency_conversion import (
    card_currency_sql,
    invoice_reporting_amount_sql,
    payment_reporting_amount_sql,
    reporting_amount_for_row,
    resolve_reporting_currency,
    rollup_from_grouped_rows,
)
from app.tenant import apply_entity_scope, get_entity_id, get_tenant, get_tenant_db
from app.utils.dates import utc_today

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# Lightweight row shapes the analytics service expects. Defined at
# module level so they're cheap to instantiate inside the endpoint
# and trivial to mock in tests.
@dataclass
class SimpleNamespaceTimings:
    id: object
    created_at: datetime | None
    approved_at: datetime | None
    paid_at: datetime | None


@dataclass
class SimpleNamespaceStep:
    assigned_to: object | None
    created_at: datetime | None
    assignee_name: str | None


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    today = utc_today()

    # Every Invoice/Payment/Exception query below is entity-scoped via
    # `_inv` / `_pay` / `_exc` helpers (None = consolidated). Metrics keyed by
    # invoice id (processing time) inherit the scope from the invoice query they
    # consume; WorkflowStep- and AuditLog-derived inputs have no entity column
    # and stay org-wide until the workflow engine is entity-aware (Phase 3).
    def _inv(q):
        return apply_entity_scope(q, Invoice, entity_id)

    def _pay(q):
        return apply_entity_scope(q, Payment, entity_id)

    def _exc(q):
        return apply_entity_scope(q, APException, entity_id)

    # KPIs
    # `total_amount` (the "Total Amount" KPI on the web dashboard) sums EVERY
    # invoice regardless of status or date — no `.where()` at all. This is a
    # DIFFERENT population from the CFO analytics `total_spend`
    # (`GET /api/analytics/cfo` — a trailing `period_days` window that
    # excludes only `rejected` invoices): a rejected or brand-new invoice
    # counts here but not there, and this figure has no date bound while that
    # one does. Both are intentional, but a caller/label that treats them as
    # interchangeable will misreport. See backend/docs/analytics.md and
    # `tests/test_analytics_rejected_exclusion.py`.
    totals = await db.execute(
        _inv(select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.amount), 0)))
    )
    total_invoices, total_amount = totals.one()

    # ----- Multi-currency reporting rollup --------------------------
    # `total_amount` above is a naive SUM that mixes currencies — fine when
    # every invoice is in one currency, wrong the moment an org books a EUR
    # invoice alongside USD ones. Re-roll the whole book into the org's
    # reporting currency using each row's rate-locked `reporting_amount`
    # (foreign rows without a lock fall back to face value and are counted in
    # `unconverted_count`). See backend/docs/multi-currency.md.
    reporting_currency = resolve_reporting_currency(org.settings)
    # Aggregate per currency in SQL instead of streaming every invoice into
    # Python. The CASE expressions come from
    # `currency_conversion.invoice_reporting_amount_sql`, the SQL statement of
    # `reporting_amount_for_row`: a row is "locked" (use its persisted
    # `reporting_amount`) iff that column is set AND its `reporting_currency`
    # equals the org's target; otherwise it falls back to face `amount`, and a
    # foreign row (currency != target) is counted as unconverted.
    # `rollup_from_grouped_rows` reduces these per-currency sums to the exact
    # same `ReportingRollup` the row-at-a-time path produced.
    #
    # This endpoint used to spell those two CASEs itself. They are now imported
    # for the same reason the tile below reuses them rather than re-deriving:
    # a second copy of the conversion rule is how two figures on one page start
    # disagreeing. The imported version is also STRICTER in one respect — it
    # requires `reporting_currency IS NOT NULL` explicitly, where the local copy
    # left that to `UPPER(NULL) = tgt`. Under SQL's three-valued logic that made
    # the lock test NULL for a row carrying a `reporting_amount` but no currency
    # code, `NOT NULL` is NULL, and such a row fell through the `unconverted`
    # CASE and was reported as converted while its FACE value was what got
    # added. See `tests/test_dashboard_vendor_spend.py`.
    _rep = invoice_reporting_amount_sql(
        reporting_currency=reporting_currency,
        amount=Invoice.amount,
        currency=Invoice.currency,
        persisted_reporting_amount=Invoice.reporting_amount,
        persisted_reporting_currency=Invoice.reporting_currency,
    )
    _cur_key = _rep.currency_key
    _rep_expr = _rep.amount
    _unconv_expr = _rep.unconverted
    rollup_rows = await db.execute(
        _inv(
            select(
                _cur_key.label("currency"),
                func.coalesce(func.sum(Invoice.amount), 0),
                func.coalesce(func.sum(_rep_expr), 0),
                func.count(),
                func.coalesce(func.sum(_unconv_expr), 0),
            ).group_by(_cur_key)
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
        reporting_currency=reporting_currency,
    )

    # Pipeline (count per status)
    status_rows = await db.execute(
        _inv(select(Invoice.status, func.count(Invoice.id)).group_by(Invoice.status))
    )
    pipeline = {
        str(row[0].value if hasattr(row[0], "value") else row[0]): row[1]
        for row in status_rows.all()
    }

    # Spend by vendor (top 10) — exclude rejected invoices (they were never
    # real spend), matching the CFO analytics concentration figure. Rolled up
    # into the org's reporting currency (not a naive SUM across currencies) —
    # a vendor billing in more than one currency used to add e.g. USD + EUR
    # as if they were one currency.
    #
    # Grouped in SQL, like the rollup / pipeline / aging / trend blocks either
    # side of it, and through the very same `_rep_expr` CASE — so a vendor's
    # total is built by exactly the rule the whole-book rollup uses. This tile
    # used to select five columns of EVERY non-rejected invoice (all time, no
    # LIMIT) and fold them in Python: a synchronous per-row loop inside an
    # `async def`, occupying the event loop for its whole duration and growing
    # linearly with the invoice table while its four neighbours stayed one
    # aggregate each. See backend/docs/analytics.md § Vendor spend is grouped
    # in SQL for the measured before/after.
    #
    # The ordering rule is (total DESC, vendor ASC). The Python fold sorted on
    # the total alone and Python's sort is stable, so ties fell back to DB scan
    # order — which of two equal-spend vendors took rank 10 (and which fell off
    # the `[:10]`) could differ between two identical requests. The name
    # tiebreak makes the tile reproducible.
    #
    # Each row also carries `unconverted_count` — how many of that vendor's
    # invoices went into `total` at FACE value because no rate lock bridged
    # them. The tile folded them silently before: its `{vendor, amount}` shape
    # had nowhere to say so, so a EUR-billing vendor with no locks ranked
    # against USD vendors on a number that was not in the same currency as the
    # label above it. It is one more `SUM` over a CASE already in this query,
    # and a fallback nobody reports is just a wrong number
    # (`docs/decisions.md` §35).
    _vendor_total = func.coalesce(func.sum(_rep_expr), 0)
    _vendor_unconv = func.coalesce(func.sum(_unconv_expr), 0)
    vendor_rows = await db.execute(
        _inv(
            select(
                Invoice.vendor_name,
                _vendor_total.label("total"),
                _vendor_unconv.label("unconverted_count"),
            )
            .where(
                Invoice.vendor_name.isnot(None),
                Invoice.vendor_name != "",
                Invoice.status != "rejected",
            )
            .group_by(Invoice.vendor_name)
            .order_by(_vendor_total.desc(), Invoice.vendor_name.asc())
            .limit(10)
        )
    )
    # `Invoice.amount` and `Invoice.reporting_amount` are both `Numeric(15, 2)`,
    # so a Postgres SUM over them is already exact at 2dp — summing then
    # quantizing matches the row-at-a-time quantize-then-sum the Python fold
    # did, the same argument `rollup_from_grouped_rows` makes for the whole-book
    # rollup. Stays `Decimal` into the response; `DashboardResponse` does the
    # single float hop at serialization, like every other money figure here.
    vendor_spend = [
        {
            "vendor": vendor,
            "amount": Decimal(str(total)),
            "unconverted_count": int(unconverted or 0),
        }
        for vendor, total, unconverted in vendor_rows.all()
    ]

    # Aging buckets — boundaries are days past the due date:
    # current (not yet due) / 1-30 / 31-60 / 61-90 / 90+.
    # Accumulate in Decimal (money is never summed in float — many small floats
    # drift) and stay Decimal in the response — `DashboardResponse`'s
    # `MoneyAmount` fields do the float hop once, at JSON-serialization time.
    aging_dec = {
        "current": Decimal("0"),
        "days_30": Decimal("0"),
        "days_60": Decimal("0"),
        "days_90": Decimal("0"),
        "days_90_plus": Decimal("0"),
    }
    # Reporting-currency counterpart — `aging_dec` above sums the raw
    # `Invoice.amount` across currencies, same mistake `total_amount` had
    # before `reporting`/`rollup` was added. Reuses `_rep_expr` (defined
    # above for the whole-book rollup): the persisted rate-locked
    # `reporting_amount` when it's locked for the org's target currency, else
    # face `amount` (foreign rows without a lock fall back to face value, same
    # fallback the rest of this endpoint's reporting figures use) — and
    # `aging_reporting_unconverted` counts exactly those fallbacks, so the
    # chart can say its bands are part-converted instead of implying they are
    # all in one currency.
    #
    # ONE count for the whole band set, not five. `AgingBuckets` is a single
    # object holding five sums, so a per-band count would mean five more
    # fields on a schema that two response keys share; the actionable fact —
    # "some of this is unconverted" — is the same either way, and the invoices
    # behind it are listable from `/invoices`. The bare `aging` deliberately
    # gets no count at all: it is a face-value cross-currency sum in its
    # entirety, so "N rows could not be converted" would understate it.
    aging_reporting_dec = {
        "current": Decimal("0"),
        "days_30": Decimal("0"),
        "days_60": Decimal("0"),
        "days_90": Decimal("0"),
        "days_90_plus": Decimal("0"),
    }
    aging_reporting_unconverted = 0
    # Aging covers the same open-payable population as the AP balance so the
    # bands reconcile with it (F-4): approved → payment_scheduled. The AP
    # balance has no due_date filter, so aging must not either — an open
    # invoice missing a due date used to inflate the balance while vanishing
    # from every bucket. A null due_date can't be judged overdue, so it
    # buckets as "current" (the conservative read) rather than being dropped.
    # Bucket + sum in SQL (Postgres `date - date` is integer days) rather than
    # pulling every open row into Python; the band boundaries match the old
    # loop exactly.
    _days_past = today - Invoice.due_date
    _aging_bucket = case(
        (Invoice.due_date.is_(None), "current"),
        (_days_past <= 0, "current"),
        (_days_past <= 30, "days_30"),
        (_days_past <= 60, "days_60"),
        (_days_past <= 90, "days_90"),
        else_="days_90_plus",
    )
    aging_rows = await db.execute(
        _inv(
            select(
                _aging_bucket.label("bucket"),
                func.coalesce(func.sum(Invoice.amount), 0),
                func.coalesce(func.sum(_rep_expr), 0),
                func.coalesce(func.sum(_unconv_expr), 0),
            )
            .where(Invoice.status.in_(OPEN_AP_STATUSES))
            .group_by(_aging_bucket)
        )
    )
    for bucket, total, rep_total, unconverted in aging_rows.all():
        aging_dec[bucket] = Decimal(str(total))
        aging_reporting_dec[bucket] = Decimal(str(rep_total))
        aging_reporting_unconverted += int(unconverted or 0)
    aging = aging_dec
    aging_reporting = {**aging_reporting_dec, "unconverted_count": aging_reporting_unconverted}

    # Monthly trend (last 6 calendar months) — bucket by calendar month in SQL
    # rather than streaming every recent invoice into Python. Summing amounts
    # in the DB (Numeric) also avoids the float-accumulation drift the old
    # per-row `+= float(...)` fold could introduce.
    #
    # The window is anchored to a MONTH BOUNDARY, not `today - 180 days`. A
    # rolling day window against a calendar-month GROUP BY is a mismatch: 180
    # days reaches back into a SEVENTH month on most dates, so the chart drew
    # seven bars whose oldest was a partial slice of a month — a stub that
    # reads as a collapse in spend and shifts every day as the window slides.
    # Anchoring gives exactly six buckets of like-for-like whole months (the
    # newest is the month in progress, which is inherent to a to-date trend).
    _months_back = 5
    _anchor = today.year * 12 + (today.month - 1) - _months_back
    trend_start = today.replace(year=_anchor // 12, month=_anchor % 12 + 1, day=1)
    _month_key = func.to_char(func.date_trunc("month", Invoice.invoice_date), "YYYY-MM")
    trend_rows = await db.execute(
        _inv(
            select(
                _month_key.label("month"),
                func.count(),
                func.coalesce(func.sum(Invoice.amount), 0),
                # Reporting-currency counterpart — same `_rep_expr` CASE as the
                # aging buckets / whole-book rollup above.
                func.coalesce(func.sum(_rep_expr), 0),
                # ...and the same `_unconv_expr`, so each BAR says whether its
                # own `reporting_amount` is fully converted. Per month rather
                # than one figure for the series, because a trend is read bar
                # against bar: a single foreign vendor onboarding in March
                # contaminates March, and a whole-series count would not say
                # which step in the line is the one not to trust.
                func.coalesce(func.sum(_unconv_expr), 0),
            )
            .where(Invoice.invoice_date >= trend_start, Invoice.invoice_date.isnot(None))
            .group_by(_month_key)
            .order_by(_month_key)
        )
    )
    monthly_trend = [
        {
            "month": month,
            "count": count,
            "amount": Decimal(str(amount)),
            "reporting_amount": Decimal(str(rep_amount)),
            "unconverted_count": int(unconverted or 0),
        }
        for month, count, amount, rep_amount, unconverted in trend_rows.all()
    ]

    # Upcoming payments (due within 7 days + overdue)
    week_ahead = today + timedelta(days=7)
    paid_ids = select(Payment.invoice_id).where(Payment.status == "completed").scalar_subquery()
    upcoming_rows = await db.execute(
        _inv(
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
    _upcoming_rows_all = upcoming_rows.all()
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
        for r in _upcoming_rows_all
    ]
    # Sum in Decimal (each r[3] is already a Decimal off the Numeric column)
    # and stay Decimal — never fold the per-item floats above, which is
    # exactly the client-side bug this mirrors (mobile issue #189: summing
    # already-lossy floats accumulates drift). The float hop happens once,
    # at JSON-serialization time, via `DashboardResponse`'s `MoneyAmount`.
    upcoming_total_amount = sum((r[3] for r in _upcoming_rows_all), Decimal("0"))
    # Reporting-currency counterpart — `upcoming_total_amount` above sums the
    # raw per-row `amount`, which mixes currencies the moment one of these ten
    # invoices is foreign. Resolved row-at-a-time via `reporting_amount_for_row`
    # (the same helper `/payments/queue` uses for its own "amount due" total) —
    # a row whose reporting figure can't be established still counts at face
    # value (dropping it would understate what's due) but is tallied on
    # `upcoming_unconverted_count`.
    upcoming_total_amount_reporting = Decimal("0")
    upcoming_unconverted_count = 0
    for r in _upcoming_rows_all:
        rep_amt, unconverted = reporting_amount_for_row(
            amount=r[3],
            currency=r[5],
            reporting_currency=reporting_currency,
            persisted_reporting_currency=r[7],
            persisted_reporting_amount=r[6],
        )
        upcoming_total_amount_reporting += rep_amt
        if unconverted:
            upcoming_unconverted_count += 1

    # Touchless rate — the share of invoices that PASSED REVIEW without a human
    # touching them, out of every invoice that provably finished review. All
    # three legs are defined once, in `services/analytics`
    # (`TOUCHLESS_CLEARED_STATUSES` / `TOUCHLESS_REVIEW_EVIDENCE_STATUSES` /
    # `TOUCHLESS_BOUNCED_STATUSES`), because the hand-written copy that used to
    # live here had drifted.
    #
    # Several statuses the pipeline map reports cannot be classified by status
    # alone — `done` (the `new -> done` shortcut skips approval outright, and
    # it is the CSV importer's default landing state), `paid` (CSV-importable
    # too) and `failed` (reachable from `pending` — extraction failed, never
    # reviewed — as well as from `sending_to_erp`). The durable
    # `Invoice.approval_date` stamp is the positive evidence that review
    # actually happened; only the stamped ones count as cleared, and the rest
    # sit in neither leg.
    #
    # The mirror hole is provenance, not evidence: a CSV-imported `rejected`
    # row sits in the bounced leg as though a reviewer HERE sent it back, and
    # no approval stamp can ever exclude it (nothing writes one on a
    # rejection). So imported rows leave BOTH legs, identified by the marker
    # `services/csv_import` stamps on every row it creates rather than
    # inferred from status — `done`, `paid` and `rejected` are each reachable
    # natively too. A row with no marker is treated as native (the marker is
    # written only going forward and never backfilled), so rows imported
    # before it shipped stay in the population; see `docs/analytics.md`.
    imported_status_rows = await db.execute(
        _inv(
            select(Invoice.status, func.count(Invoice.id))
            .where(imported_invoice_clause())
            .group_by(Invoice.status)
        )
    )
    imported_pipeline = {
        str(row[0].value if hasattr(row[0], "value") else row[0]): row[1]
        for row in imported_status_rows.all()
    }
    review_cleared_q = await db.execute(
        _inv(
            select(func.count()).where(
                Invoice.status.in_(TOUCHLESS_REVIEW_EVIDENCE_STATUSES),
                Invoice.approval_date.isnot(None),
                native_invoice_clause(),
            )
        )
    )
    touchless_rate = compute_touchless_rate(
        pipeline,
        review_cleared_count=int(review_cleared_q.scalar() or 0),
        imported_pipeline=imported_pipeline,
    )

    # Payment totals — separate queries to avoid complex CASE expressions
    paid_q = await db.execute(
        _pay(
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
    from app.api.payments import PENDING_PAYMENT_STATUSES

    pending_q = await db.execute(
        _pay(
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
        reporting_currency=reporting_currency,
        payment_amount=Payment.amount,
        payment_source_amount=Payment.source_amount,
        payment_source_currency=Payment.source_currency,
        invoice_currency=Invoice.currency,
    )
    _reported_countable = reported_payment.is_expressible

    async def _reporting_payment_total(status_clause) -> tuple[Decimal, int]:
        q = _pay(
            select(
                func.coalesce(func.sum(case((_reported_countable, reported_payment.amount))), 0),
                func.count(case((not_(_reported_countable), Payment.id))),
            )
            .select_from(Payment)
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .where(status_clause)
        )
        total, excluded = (await db.execute(q)).one()
        return Decimal(str(total or 0)), int(excluded or 0)

    total_paid_reporting, total_paid_unconverted_count = await _reporting_payment_total(
        Payment.status == "completed"
    )
    total_pending_reporting, total_pending_unconverted_count = await _reporting_payment_total(
        Payment.status.in_(PENDING_PAYMENT_STATUSES)
    )

    # Rebates. CardRebate carries no entity_id of its own — join to
    # VirtualCard (which does, via EntityMixin) so switching the entity
    # selector scopes this KPI like every other one on the page instead of
    # silently staying a whole-org total. The join also carries the CURRENCY:
    # `card_rebates` has no currency column either, so this was a bare
    # cross-currency SUM rendered on the dashboard as one "Rebates Earned"
    # figure — a quantity in no currency at all. `card_currency_sql` is the
    # one owner of that expression (see `services/currency_conversion`), and
    # the `!=` count is what stops a single-currency figure looking complete.
    _rebate_ccy = card_currency_sql(reporting_currency)
    rebate_row = (
        await db.execute(
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
                entity_id,
            )
        )
    ).one()
    total_rebates = Decimal(str(rebate_row[0] or 0))
    excluded_rebate_count = int(rebate_row[1] or 0)

    # Stale approvals (waiting > 3 days)
    stale_date = today - timedelta(days=3)
    stale_q = await db.execute(
        _inv(
            select(func.count()).where(
                Invoice.status == "ready_for_review",
                Invoice.created_at <= stale_date,
            )
        )
    )
    stale_approvals = stale_q.scalar() or 0

    # Open exceptions
    try:
        exc_q = await db.execute(
            _exc(select(func.count()).where(APException.status.in_(["open", "escalated"])))
        )
        open_exceptions = exc_q.scalar() or 0
    except Exception:
        open_exceptions = 0
        await db.rollback()

    # ----- Processing-time metrics ----------------------------------
    # Avg + median + p95 from invoice creation to (a) approval (read
    # from the audit_log row stamped on `invoice.approved`) and (b)
    # completed payment (read from Payment.completed_at). Both legs
    # collapse to 0 when the sample is below the min threshold (see
    # `services/analytics.compute_processing_time_metrics`).
    from app.models.workflow import AuditLog
    from app.services.analytics import compute_processing_time_metrics

    invoice_legs: list = []
    try:
        approval_rows = await db.execute(
            select(AuditLog.entity_id, AuditLog.created_at).where(
                AuditLog.entity_type == "invoice",
                AuditLog.action == "invoice.approved",
            )
        )
        approved_at_by_invoice = {row[0]: row[1] for row in approval_rows.all()}
        paid_rows = await db.execute(
            select(Payment.invoice_id, func.min(Payment.completed_at))
            .where(Payment.status == "completed")
            .group_by(Payment.invoice_id)
        )
        paid_at_by_invoice = {row[0]: row[1] for row in paid_rows.all() if row[1]}
        # Only invoices with an approval or paid timestamp contribute a leg —
        # every other row appends nothing. So fetch just those ids instead of
        # the whole (potentially multi-million-row) invoice table; the entity
        # scope still applies, so an out-of-scope id is dropped exactly as before.
        relevant_ids = set(approved_at_by_invoice) | set(paid_at_by_invoice)
        if relevant_ids:
            inv_rows = await db.execute(
                _inv(select(Invoice.id, Invoice.created_at).where(Invoice.id.in_(relevant_ids)))
            )
            for inv_id, created in inv_rows.all():
                invoice_legs.append(
                    SimpleNamespaceTimings(
                        id=inv_id,
                        created_at=created,
                        approved_at=approved_at_by_invoice.get(inv_id),
                        paid_at=paid_at_by_invoice.get(inv_id),
                    )
                )
    except Exception:  # noqa: BLE001
        # Tenants without the audit_log shipping pipeline enabled can
        # still see the rest of the dashboard. Surface zeros and a
        # note in the response.
        await db.rollback()
        invoice_legs = []

    pt = compute_processing_time_metrics(invoice_legs)

    # ----- Approval bottleneck --------------------------------------
    # Per-approver pending counts + oldest age + average age. Reads
    # from WorkflowStep rows where step_type='approval' and
    # completed_at IS NULL.
    from app.models.workflow import WorkflowStep
    from app.services.analytics import compute_approval_bottleneck

    try:
        step_rows = await db.execute(
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
    except Exception:  # noqa: BLE001
        await db.rollback()
        pending_steps = []
    bottleneck_rows = compute_approval_bottleneck(pending_steps)

    # ----- Discount capture rate ------------------------------------
    # Join Invoice → PaymentSchedule → Payment(completed) and sort every
    # discount-scheduled invoice into captured / missed / still-capturable.
    #
    # Grouped in SQL, like every other figure on this page — three rows come
    # back, one per bucket. This block used to stream EVERY discount-scheduled
    # invoice (`WHERE discount_percent IS NOT NULL`, no LIMIT, no date bound)
    # into a synchronous Python projection inside an `async def`, occupying the
    # event loop for its whole duration and growing without bound as a tenant
    # books more discount terms. It was the last unbounded fold on this page.
    #
    # It resisted the plain per-currency `GROUP BY` its five neighbours got for
    # a real reason: the fold needs a per-ROW verdict, not a per-currency sum.
    # But that verdict is only two comparisons, so it becomes a CASE and the
    # aggregation happens around it. What stays in Python is what was never
    # per-row — the bucket vocabulary, the capture rate, and the
    # `insufficient_data` state — via `discount_capture_from_grouped_rows`, the
    # same reference-implementation-plus-grouped-reducer pair
    # `currency_conversion.rollup_from_grouped_rows` established for the
    # whole-book rollup, with an equivalence test pinning the two together.
    from app.models.payment import PaymentSchedule
    from app.services.analytics import discount_capture_from_grouped_rows

    # The discount is a percentage of the invoice, so the reporting figure is
    # the same percentage of the invoice's REPORTING amount — `_rep_expr`, the
    # rate-locked one when it exists and face value (counted by `_unconv_expr`)
    # when it does not, exactly as every other money rollup on this page.
    #
    # Rounded to cents PER ROW, where the Python fold rounded: quantize-then-sum,
    # not sum-then-quantize. Postgres `round(numeric, 2)` breaks an exact
    # half-cent tie away from zero where `Decimal.quantize`'s default context
    # broke it to even, so a discount landing on exactly x.xx5 now rounds up.
    # That is the project's documented money convention (`ROUND_HALF_UP` —
    # "matches what auditors expect") and it is what `discount_offers.
    # discount_savings` already uses for this very quantity, base × percent ÷
    # 100; this tile was the one place computing it the other way.
    _disc_amount = func.round(Invoice.amount * PaymentSchedule.discount_percent / 100, 2)
    _disc_amount_reporting = func.round(_rep_expr * PaymentSchedule.discount_percent / 100, 2)
    try:
        # Per invoice-and-schedule, with the completed payments collapsed to the
        # earliest `completed_at`. The GROUP BY is the fold's, unchanged: an
        # invoice carrying two schedules on different terms is still two rows.
        eligible = (
            _inv(
                select(
                    _disc_amount.label("discount_amount"),
                    _disc_amount_reporting.label("discount_amount_reporting"),
                    _unconv_expr.label("unconverted"),
                    PaymentSchedule.discount_date.label("discount_date"),
                    func.min(Payment.completed_at).label("paid_at"),
                )
                .join(PaymentSchedule, PaymentSchedule.invoice_id == Invoice.id)
                .outerjoin(
                    Payment,
                    (Payment.invoice_id == Invoice.id) & (Payment.status == "completed"),
                )
                .where(PaymentSchedule.discount_percent.isnot(None))
                .group_by(
                    Invoice.id,
                    Invoice.amount,
                    Invoice.currency,
                    Invoice.reporting_amount,
                    Invoice.reporting_currency,
                    PaymentSchedule.discount_percent,
                    PaymentSchedule.discount_date,
                )
            )
        ).subquery()
        # `completed_at` is `timestamptz`, and asyncpg handed Python a UTC-aware
        # datetime whose `.date()` was therefore the UTC date. A bare `::date`
        # would read the SESSION's TimeZone instead, so the conversion is spelled
        # out: this is a payment-against-deadline comparison, and taking it off
        # the server's local calendar is the class of bug `utils/dates.utc_today`
        # exists to prevent.
        _paid_on = cast(func.timezone("UTC", eligible.c.paid_at), Date)
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
                    _paid_on <= eligible.c.discount_date,
                ),
                "captured",
            ),
            (
                and_(
                    eligible.c.discount_date.isnot(None),
                    eligible.c.discount_date < today,
                ),
                "missed",
            ),
            else_="pending",
        )
        bucket_rows = await db.execute(
            select(
                bucket.label("bucket"),
                func.count(),
                func.coalesce(func.sum(eligible.c.discount_amount), 0),
                func.coalesce(func.sum(eligible.c.discount_amount_reporting), 0),
                func.coalesce(func.sum(eligible.c.unconverted), 0),
            ).group_by(bucket)
        )
        discount_groups = [
            {
                "bucket": row[0],
                "count": row[1],
                "amount": row[2],
                "reporting_amount": row[3],
                "unconverted_count": row[4],
            }
            for row in bucket_rows.all()
        ]
    except Exception:  # noqa: BLE001
        await db.rollback()
        discount_groups = []
    discount = discount_capture_from_grouped_rows(discount_groups)

    return {
        "total_invoices": total_invoices or 0,
        "total_amount": Decimal(str(total_amount)),
        # Currency-aware rollup of the whole invoice book into ONE reporting
        # currency, plus the per-currency split so the UI can show the mix.
        "reporting": {
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
        },
        "total_paid": total_paid,
        "total_pending": total_pending,
        # Reporting-currency counterparts of the two lines above — see the
        # `payment_reporting_amount_sql` comment at their computation.
        "total_paid_reporting": total_paid_reporting,
        "total_pending_reporting": total_pending_reporting,
        "total_paid_unconverted_count": total_paid_unconverted_count,
        "total_pending_unconverted_count": total_pending_unconverted_count,
        "total_rebates": total_rebates,
        # Rebates left out of `total_rebates` for being denominated in another
        # currency, so the KPI can say it describes part of the set rather than
        # looking complete — the same disclosure the two payment counts above
        # provide for their own figures.
        "excluded_rebate_count": excluded_rebate_count,
        "open_exceptions": open_exceptions,
        "touchless_rate": touchless_rate,
        "stale_approvals": stale_approvals,
        "pipeline": pipeline,
        "vendor_spend": vendor_spend,
        "aging": aging,
        # Reporting-currency counterpart of `aging` — see the `_rep_expr`
        # comment above the whole-book rollup.
        "aging_reporting": aging_reporting,
        "monthly_trend": monthly_trend,
        "upcoming_payments": upcoming,
        "upcoming_total_amount": upcoming_total_amount,
        "upcoming_total_amount_reporting": upcoming_total_amount_reporting,
        "upcoming_unconverted_count": upcoming_unconverted_count,
        "processing_time": {
            "avg_upload_to_approval_days": float(pt.avg_upload_to_approval_days),
            "median_upload_to_approval_days": float(pt.median_upload_to_approval_days),
            "p95_upload_to_approval_days": float(pt.p95_upload_to_approval_days),
            "avg_upload_to_paid_days": float(pt.avg_upload_to_paid_days),
            "median_upload_to_paid_days": float(pt.median_upload_to_paid_days),
            "p95_upload_to_paid_days": float(pt.p95_upload_to_paid_days),
            "count_approval_leg": pt.count_approval_leg,
            "count_paid_leg": pt.count_paid_leg,
        },
        "approval_bottleneck": [
            {
                "approver_id": r.approver_id,
                "approver_name": r.approver_name,
                "pending_count": r.pending_count,
                "oldest_pending_days": float(r.oldest_pending_days),
                "avg_pending_days": float(r.avg_pending_days),
            }
            for r in bottleneck_rows[:10]
        ],
        "discount_capture": {
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
            "reporting_currency": reporting_currency,
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
        },
    }
