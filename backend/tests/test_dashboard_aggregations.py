"""Dashboard aggregation — pins the *computation* layer of
`GET /api/dashboard`. The endpoint runs ~12 queries and then folds
the rows into pipeline counts, aging buckets, monthly trend, upcoming
payments, and touchless rate. The folding is pure Python on rows we
own — that's what these tests pin.

Bucket boundaries matter for AR reporting and stay-current
dashboards; getting the inclusive/exclusive ends wrong is a classic
off-by-one. Touchless rate is a board-deck metric — its denominator
must include rejected invoices, or the number lies.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.dashboard import get_dashboard
from app.models.invoice import InvoiceStatus
from app.utils.dates import utc_today


def _r(scalar=None, one=None, all_=None, sub=False):
    """Construct a MagicMock that satisfies one of the result shapes
    the dashboard endpoint uses:
      - `.scalar()` for SUM coalesce
      - `.one()` for the COUNT/SUM tuple
      - `.all()` for the row iterators
      - `.scalar_subquery()` is exercised at query *build* time, but
        in tests we never reach the DB so it doesn't matter.
    """
    m = MagicMock()
    if scalar is not None:
        m.scalar = MagicMock(return_value=scalar)
    if one is not None:
        m.one = MagicMock(return_value=one)
    if all_ is not None:
        m.all = MagicMock(return_value=all_)
    return m


def _user():
    return MagicMock()


def _org(settings=None):
    """Org stub for the dashboard's reporting-currency rollup. Defaults to
    no settings → reporting currency falls back to USD."""
    m = MagicMock()
    m.settings = settings
    return m


def _mk_db(*results):
    """Build an AsyncMock whose execute() cycles through the given
    result mocks in order. Each entry is what we want the executor's
    nth `await db.execute(...)` to return."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


# The dashboard makes queries in this order — pinning here so the
# test layout stays sane. Don't change the order without updating the
# endpoint AND the comments below.
#
# Aging bands, the monthly-trend buckets, the currency rollup and the top-vendor
# tile are now aggregated in SQL (GROUP BY), so those result rows are already
# grouped — the off-by-one band boundaries + month bucketing are exercised by the
# realdb tests in `test_analytics_aging_reconciliation.py`, not here, and the
# vendor grouping/ordering by `test_dashboard_vendor_spend.py`.
#
#   1.  totals (count + sum)                       → .one() → (count, sum)
#   2.  reporting rollup rows (per currency)       → .all() → (ccy, sum_amt, sum_rep, count, unconv)
#   3.  pipeline status rows                       → .all()
#   4.  vendor spend rows — SQL GROUP BY, top 10   → .all() → (vendor, sum_rep)
#   5.  aging rows (bucket, sum, sum_rep) — SQL-bucketed → .all()
#   6.  trend rows (month, count, sum, sum_rep) — SQL group → .all()
#   7.  upcoming payment rows (+ currency/reporting cols)  → .all()
#   7a. CSV-IMPORTED invoice counts per status              → .all()
#       (rows carrying `meta["imported"]` — migrated history the workflow
#       engine never ran on, subtracted from BOTH touchless legs; status
#       cannot identify them, since `done`/`paid`/`rejected` are each
#       reachable natively too)
#   7b. NATIVE invoices in an AMBIGUOUS status carrying an approval stamp
#       → .scalar() (the touchless-rate numerator's evidence — `done`, `paid`
#       and `failed` are each reachable both through review and around it, see
#       `analytics.compute_touchless_rate`)
#   8.  total paid                        → .scalar()
#   9.  total pending                     → .scalar()
#  10.  reporting total paid              → .one() → (amount, unconverted_count)
#  11.  reporting total pending           → .one() → (amount, unconverted_count)
#  12.  rebates                           → .one() → (amount, excluded_count)
#  13.  stale approvals                   → .scalar()
#  14.  open exceptions                   → .scalar()


def _full_results(
    *,
    totals=(0, Decimal("0")),
    reporting_rows=(),
    pipeline=(),
    vendor_spend=(),
    aging=(),
    trend=(),
    upcoming=(),
    imported_pipeline=(),
    review_cleared=0,
    paid=Decimal("0"),
    pending=Decimal("0"),
    reporting_paid=(Decimal("0"), 0),
    reporting_pending=(Decimal("0"), 0),
    rebates=(Decimal("0"), 0),
    stale=0,
    open_exc=0,
):
    return [
        _r(one=totals),
        _r(all_=list(reporting_rows)),
        _r(all_=list(pipeline)),
        _r(all_=list(vendor_spend)),
        _r(all_=list(aging)),
        _r(all_=list(trend)),
        _r(all_=list(upcoming)),
        _r(all_=list(imported_pipeline)),
        _r(scalar=review_cleared),
        _r(scalar=paid),
        _r(scalar=pending),
        _r(one=reporting_paid),
        _r(one=reporting_pending),
        _r(one=rebates),
        _r(scalar=stale),
        _r(scalar=open_exc),
    ]


# ---------------------------------------------------------------------------
# Aging buckets — the off-by-one trap. `days_past <= 0` is current;
# 1–30 is days_30; 31–60 is days_60; 61–90 is days_90; 90+ is days_90_plus.
# A 75-days-past-due invoice MUST land in the 61-90 (`days_90`) band, not
# inflate the most-distressed 90+ bucket (BUG 7 regression).
# ---------------------------------------------------------------------------


# The aging band boundaries (the off-by-one trap: 30/31, 60/61, 90/91, future =
# current) are now bucketed in SQL, so they're pinned by
# `test_aging_boundary_bands_realdb` in test_analytics_aging_reconciliation.py —
# a mocked test here could only assert back the bucket labels it fed in.


# ---------------------------------------------------------------------------
# Touchless rate — straight-through-processing share. Numerator = invoices
# that PASSED REVIEW untouched; denominator = numerator + rejected (everything
# that provably finished review). The numerator is a strict subset of the
# denominator, so the rate is ALWAYS in [0, 100] and can never go negative
# (BUG 9 regression — the old formula subtracted rejected from a numerator
# whose base didn't include rejected, yielding e.g. -4900%).
#
# These pin the ARITHMETIC. The numerator's POPULATION — which statuses need
# the `Invoice.approval_date` stamp before they count as cleared — is pinned
# in `test_touchless_rate_population.py`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_touchless_rate_counts_rejected_in_denominator():
    """80 reached a post-approval state, 20 were rejected (manual rework).
    auto_processed = 80
    reviewed_total = 80 + 20 = 100
    rate = 80 / 100 * 100 = 80%

    The 30 `paid`/`done` invoices all carry the approval stamp, so all 80 are
    evidenced — `review_cleared=30`. (Strip that evidence and the numerator
    correctly drops to the 50 that are mid-pipeline; see
    `test_touchless_rate_population.py`.)
    """
    pipeline_rows = [
        (InvoiceStatus.approved, 30),
        (InvoiceStatus.posted_in_erp, 20),
        (InvoiceStatus.paid, 20),
        (InvoiceStatus.done, 10),
        (InvoiceStatus.rejected, 20),
    ]
    db = _mk_db(*_full_results(pipeline=pipeline_rows, review_cleared=30))
    result = await get_dashboard(db=db, org=_org(), user=_user())
    assert result["touchless_rate"] == 80.0
    assert result["pipeline"]["rejected"] == 20


@pytest.mark.asyncio
async def test_touchless_rate_never_goes_negative_with_many_rejections():
    """The exact BUG 9 reproduction: {approved: 1, rejected: 50}. The old
    formula `(1 - 50) / 1 * 100` returned -4900%. The coherent formula is
    `1 / (1 + 50) * 100 ≈ 1.96%` — small, but never negative."""
    pipeline_rows = [
        (InvoiceStatus.approved, 1),
        (InvoiceStatus.rejected, 50),
    ]
    db = _mk_db(*_full_results(pipeline=pipeline_rows))
    result = await get_dashboard(db=db, org=_org(), user=_user())
    assert result["touchless_rate"] >= 0
    assert result["touchless_rate"] == round(1 / 51 * 100, 1)  # ≈ 2.0


@pytest.mark.asyncio
async def test_touchless_rate_is_100_with_no_rejections():
    """Every invoice that finished review was auto-processed → 100%. The five
    `paid` ones carry the approval stamp (`review_cleared=5`)."""
    pipeline_rows = [
        (InvoiceStatus.approved, 5),
        (InvoiceStatus.paid, 5),
    ]
    db = _mk_db(*_full_results(pipeline=pipeline_rows, review_cleared=5))
    result = await get_dashboard(db=db, org=_org(), user=_user())
    assert result["touchless_rate"] == 100.0


@pytest.mark.asyncio
async def test_touchless_rate_subtracts_imported_rows_from_both_legs():
    """Day-0 migration shape: 20 real invoices (18 cleared, 2 bounced) buried
    under 500 CSV-imported historical rows, 100 of which were rejected in the
    tenant's PREVIOUS system.

    Those 100 used to land in the bounced leg as though a reviewer here had
    sent them back: 18 / (18 + 102) = 15.0%. Subtracting the imported rows
    from both legs gives the honest 18 / 20 = 90.0%. The imported `done` rows
    were already outside the numerator (no approval stamp) and are now outside
    the denominator too.
    """
    pipeline_rows = [
        (InvoiceStatus.approved, 18),
        (InvoiceStatus.rejected, 102),
        (InvoiceStatus.done, 400),
    ]
    imported = [(InvoiceStatus.rejected, 100), (InvoiceStatus.done, 400)]

    # Without the provenance split, the migrated history dominates.
    db = _mk_db(*_full_results(pipeline=pipeline_rows))
    assert (await get_dashboard(db=db, org=_org(), user=_user()))["touchless_rate"] == 15.0

    db = _mk_db(*_full_results(pipeline=pipeline_rows, imported_pipeline=imported))
    result = await get_dashboard(db=db, org=_org(), user=_user())
    assert result["touchless_rate"] == 90.0
    # The rows are excluded from the METRIC, not from the tenant's data — the
    # pipeline card still reports every invoice.
    assert result["pipeline"]["rejected"] == 102
    assert result["pipeline"]["done"] == 400


@pytest.mark.asyncio
async def test_touchless_rate_is_zero_when_no_invoices_processed():
    """No invoices have finished review — touchless rate must be
    0, not a ZeroDivisionError."""
    db = _mk_db(*_full_results(pipeline=[(InvoiceStatus.new, 5)]))
    result = await get_dashboard(db=db, org=_org(), user=_user())
    assert result["touchless_rate"] == 0


# ---------------------------------------------------------------------------
# Monthly trend — must be sorted ascending by month key.
# ---------------------------------------------------------------------------


# Monthly-trend month bucketing + ascending order is now done in SQL
# (date_trunc + GROUP BY + ORDER BY), so it's pinned by
# `test_monthly_trend_buckets_by_month_realdb` in
# test_analytics_aging_reconciliation.py rather than a mock that would just
# echo pre-bucketed rows back.


# ---------------------------------------------------------------------------
# Upcoming payments — `is_overdue` must be true iff due_date < today.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upcoming_payments_flag_overdue_only_when_due_date_before_today():
    """Today's due date is `is_overdue=False` (still on time);
    yesterday is True. A regression on the inequality direction would
    nag the AP team for paying on time."""
    import uuid

    # `utc_today()`, not `date.today()`: every value this test compares
    # against is derived by the app from the UTC date (app/utils/dates.py —
    # no call site under app/ reads the local date). Anchoring the fixtures
    # on the LOCAL date puts them a day off for the window each day where
    # the two disagree, which lands exactly on the boundaries this test
    # exists to pin. CI runs UTC and never saw it.
    today = utc_today()
    upcoming_rows = [
        (
            uuid.uuid4(),
            "INV-1",
            "Acme",
            Decimal("100"),
            today - timedelta(days=1),
            "USD",
            None,
            None,
        ),
        (uuid.uuid4(), "INV-2", "Bravo", Decimal("100"), today, "USD", None, None),  # on time
        (
            uuid.uuid4(),
            "INV-3",
            "Charlie",
            Decimal("100"),
            today + timedelta(days=3),
            "USD",
            None,
            None,
        ),  # future
    ]
    db = _mk_db(*_full_results(upcoming=upcoming_rows))
    result = await get_dashboard(db=db, org=_org(), user=_user())
    flags = {row["invoice_number"]: row["is_overdue"] for row in result["upcoming_payments"]}
    assert flags == {"INV-1": True, "INV-2": False, "INV-3": False}


@pytest.mark.asyncio
async def test_upcoming_payment_rows_carry_their_own_invoice_currency():
    """Each upcoming row's `amount` is the invoice's FACE amount, so the row has
    to say what currency that is. Without it the web dashboard labelled every
    row with the org's reporting currency — a EUR invoice read as dollars on a
    USD-reporting tenant (docs/decisions.md §200). Asserted through
    `DashboardResponse` too, since a field the dict carries but the response
    model drops never reaches a client."""
    import uuid

    from app.schemas.dashboard import DashboardResponse

    today = utc_today()
    upcoming_rows = [
        (uuid.uuid4(), "INV-US", "Acme", Decimal("100"), today, "USD", None, None),
        (uuid.uuid4(), "INV-EU", "Bravo", Decimal("200"), today, "EUR", None, None),
    ]
    db = _mk_db(*_full_results(upcoming=upcoming_rows))
    result = await get_dashboard(db=db, org=_org(), user=_user())
    by_number = {row["invoice_number"]: row["currency"] for row in result["upcoming_payments"]}
    assert by_number == {"INV-US": "USD", "INV-EU": "EUR"}

    wire = DashboardResponse.model_validate(result).model_dump(mode="json")
    assert [row["currency"] for row in wire["upcoming_payments"]] == ["USD", "EUR"]


@pytest.mark.asyncio
async def test_upcoming_total_amount_sums_in_decimal_not_accumulated_float():
    """`upcoming_total_amount` is a server-computed aggregate the mobile
    dashboard consumes directly instead of folding the per-row floats itself
    (mobile issue #189). Regression case: three amounts whose classic binary
    float representations don't add up cleanly (0.1 + 0.2 style drift) must
    still sum to an exact total when accumulated in Decimal.

    The endpoint itself now stays Decimal end to end (issue #279) — the
    float hop happens exactly once, at JSON-serialization time, via
    `DashboardResponse`'s `MoneyAmount` fields, not inside this function."""
    import uuid

    today = utc_today()
    upcoming_rows = [
        (uuid.uuid4(), "INV-1", "Acme", Decimal("10.10"), today, "USD", None, None),
        (uuid.uuid4(), "INV-2", "Bravo", Decimal("20.20"), today, "USD", None, None),
        (uuid.uuid4(), "INV-3", "Charlie", Decimal("30.30"), today, "USD", None, None),
    ]
    db = _mk_db(*_full_results(upcoming=upcoming_rows))
    result = await get_dashboard(db=db, org=_org(), user=_user())
    assert isinstance(result["upcoming_total_amount"], Decimal)
    # Exact to the cent, in Decimal — no binary-float accumulation artifact
    # like 60.599999999999994.
    assert result["upcoming_total_amount"] == Decimal("60.60")


@pytest.mark.asyncio
async def test_upcoming_total_amount_is_zero_when_no_upcoming_invoices():
    db = _mk_db(*_full_results())
    result = await get_dashboard(db=db, org=_org(), user=_user())
    assert result["upcoming_total_amount"] == Decimal("0")


# ---------------------------------------------------------------------------
# Money sums — every aggregate stays `Decimal` all the way out of this
# function (issue #279); the float hop for JSON happens once, at the wire
# boundary, via `DashboardResponse`'s `MoneyAmount`/`OptionalMoneyAmount`
# annotations — never inside `get_dashboard` itself.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payment_totals_stay_decimal():
    """Money fields coming from SQLAlchemy (`Numeric` columns) must stay
    `Decimal` all the way through the function — never cast to `float` at
    the dict-build step (that's the project's `float`-for-currency
    anti-pattern, see docs/decisions.md's money invariant). A regression
    that reintroduced a bare `float(...)` cast would flip these
    `isinstance` checks."""
    db = _mk_db(
        *_full_results(
            paid=Decimal("12345.67"),
            pending=Decimal("8900.50"),
            reporting_paid=(Decimal("12345.67"), 0),
            reporting_pending=(Decimal("8900.50"), 0),
            rebates=(Decimal("123.45"), 0),
        )
    )
    result = await get_dashboard(db=db, org=_org(), user=_user())
    assert isinstance(result["total_paid"], Decimal)
    assert isinstance(result["total_pending"], Decimal)
    assert isinstance(result["total_rebates"], Decimal)
    assert isinstance(result["total_paid_reporting"], Decimal)
    assert isinstance(result["total_pending_reporting"], Decimal)
    assert result["total_paid"] == Decimal("12345.67")
    assert result["total_pending"] == Decimal("8900.50")
    assert result["total_rebates"] == Decimal("123.45")
    assert result["total_paid_reporting"] == Decimal("12345.67")
    assert result["total_pending_reporting"] == Decimal("8900.50")


@pytest.mark.asyncio
async def test_totals_default_to_zero_when_db_returns_zero_rows():
    """Empty tenant — count 0, sum None coalesced to 0. Verify the
    dashboard returns a fully-shaped response with zeros, not nulls
    (the frontend would crash on a null in the KPI tile)."""
    db = _mk_db(*_full_results())
    result = await get_dashboard(db=db, org=_org(), user=_user())
    assert result["total_invoices"] == 0
    assert result["total_amount"] == Decimal("0")
    assert result["total_paid"] == Decimal("0")
    assert result["total_pending"] == Decimal("0")
    assert result["pipeline"] == {}
    assert result["vendor_spend"] == []
    assert result["upcoming_payments"] == []
    assert result["monthly_trend"] == []
    assert result["touchless_rate"] == 0
    # Reporting rollup is present even on an empty tenant.
    assert result["reporting"]["reporting_currency"] == "USD"
    assert result["reporting"]["total_amount"] == Decimal("0")
    assert result["reporting"]["by_currency"] == []


# ---------------------------------------------------------------------------
# Multi-currency reporting rollup — the dashboard must collapse mixed-currency
# invoices into ONE reporting-currency total using each row's rate-locked
# reporting_amount, not the naive cross-currency SUM. See
# backend/docs/multi-currency.md.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reporting_rollup_collapses_mixed_currencies_into_one_total():
    """USD row (no lock needed) + EUR row with a locked USD reporting amount
    add up correctly in the reporting block, while `total_amount` stays the
    legacy naive SUM. Org reports in USD."""
    # totals = naive SUM across currencies (1000 USD + 1000 EUR face = 2000).
    # The rollup query now GROUP BYs in SQL, so rows are per-currency:
    # (currency, sum(amount), sum(reporting), count, unconverted_count).
    reporting_rows = [
        ("USD", Decimal("1000.00"), Decimal("1000.00"), 1, 0),
        ("EUR", Decimal("1000.00"), Decimal("1086.96"), 1, 0),
    ]
    db = _mk_db(*_full_results(totals=(2, Decimal("2000.00")), reporting_rows=reporting_rows))
    org = _org(settings={"reporting_currency": "USD"})
    result = await get_dashboard(db=db, org=org, user=_user())

    assert result["total_amount"] == Decimal("2000.00")  # legacy naive SUM unchanged
    rep = result["reporting"]
    assert rep["reporting_currency"] == "USD"
    # 1000 (USD 1:1) + 1086.96 (EUR locked) = 2086.96
    assert rep["total_amount"] == Decimal("2086.96")
    assert rep["total_count"] == 2
    assert rep["unconverted_count"] == 0
    by_cur = {e["currency"]: e for e in rep["by_currency"]}
    assert by_cur["EUR"]["reporting_amount"] == Decimal("1086.96")
    assert by_cur["USD"]["reporting_amount"] == Decimal("1000.00")


@pytest.mark.asyncio
async def test_reporting_rollup_flags_foreign_rows_without_a_rate_lock():
    """A foreign invoice with no materialized reporting amount falls back to
    face value and is counted in `unconverted_count` so the UI can warn rather
    than silently mixing currencies."""
    # GBP group has no rate lock → falls back to face value and is counted
    # in unconverted_count (the SQL CASE already applied the fallback).
    reporting_rows = [
        ("USD", Decimal("500.00"), Decimal("500.00"), 1, 0),
        ("GBP", Decimal("300.00"), Decimal("300.00"), 1, 1),
    ]
    db = _mk_db(*_full_results(totals=(2, Decimal("800.00")), reporting_rows=reporting_rows))
    result = await get_dashboard(db=db, org=_org(), user=_user())
    rep = result["reporting"]
    assert rep["unconverted_count"] == 1
    assert rep["total_amount"] == Decimal("800.00")  # GBP falls through at face value
