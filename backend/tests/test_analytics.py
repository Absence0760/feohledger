"""Analytics service — pure-function computations.

Each metric in `services/analytics.py` is a self-contained pure
function so it can be unit-tested without a DB. The math itself is
the load-bearing piece: a misread DPO number lands on the CFO's
board deck unchallenged.

Pins:
  - processing_time bands collapse to 0 below the min-sample
    threshold (no wild "avg = 17.3 days" from a 2-row sample)
  - approval_bottleneck rolls up by approver_id, sorts by
    pending_count desc, surfaces an `"unassigned"` synthetic key
    when assigned_to is null
  - discount_capture handles empty input (no eligible rows → no rate
    at all, NOT a reassuring 0% and NOT a ZeroDivisionError)
  - DPO formula AP/COGS×days; zero COGS → 0 (defensive)
  - cash_conversion_cycle returns None when DSO/DIO unavailable
  - working_capital_impact is monotone in days_extended
  - supplier_concentration flags only when largest > threshold;
    empty / zero-total inputs are zero-snapshots, never 500s
  - fraud_rate_trend reads invoice_count + exception_count into
    rate_pct; zero invoices → 0 (not divide-by-zero)
  - rebate_yield exposes annualised run-rate scaled from the
    period (months_in_period normaliser)
  - forecast_variance sign convention: positive = paid out more
    than forecast
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.analytics import (
    ReceivedPO,
    apply_payment_timing_scenario,
    bucket_outflows,
    compute_accruals,
    compute_approval_bottleneck,
    compute_cash_conversion_cycle,
    compute_cash_position,
    compute_discount_capture,
    compute_dpo,
    compute_dpo_trend,
    compute_forecast_variance,
    compute_fraud_rate_trend,
    compute_processing_time_metrics,
    compute_rebate_yield,
    compute_supplier_concentration,
    compute_working_capital_impact,
    detect_threshold_breaches,
    discount_capture_from_grouped_rows,
    value_received_goods,
)

# ---------------------------------------------------------------------------
# Processing time
# ---------------------------------------------------------------------------


def _inv(created, approved=None, paid=None):
    return SimpleNamespace(created_at=created, approved_at=approved, paid_at=paid)


def test_processing_time_collapses_to_zero_below_min_sample():
    """Two invoices is too small a sample — return zeros rather
    than a noisy "avg=11.7 days" derived from one outlier."""
    base = datetime(2026, 5, 1, tzinfo=UTC)
    invs = [
        _inv(base, base + timedelta(days=1), base + timedelta(days=5)),
        _inv(base, base + timedelta(days=12), base + timedelta(days=20)),
    ]
    pt = compute_processing_time_metrics(invs)
    assert pt.avg_upload_to_approval_days == Decimal("0")
    assert pt.p95_upload_to_paid_days == Decimal("0")
    assert pt.count_approval_leg == 2  # sample size still reported
    assert pt.count_paid_leg == 2


def test_processing_time_reports_avg_median_p95_above_threshold():
    """Five-row sample at the threshold — metrics populate. Verify
    avg / median / p95 against a known distribution."""
    base = datetime(2026, 5, 1, tzinfo=UTC)
    # Approval lags: 1, 2, 3, 4, 5 days (avg 3, median 3, p95 ~4.8)
    invs = [_inv(base, base + timedelta(days=d), paid=None) for d in (1, 2, 3, 4, 5)]
    pt = compute_processing_time_metrics(invs)
    assert pt.count_approval_leg == 5
    assert pt.avg_upload_to_approval_days == Decimal("3.0")
    assert pt.median_upload_to_approval_days == Decimal("3.0")
    # p95 of [1..5] sits between 4 and 5; linear interpolation gives 4.8.
    assert pt.p95_upload_to_approval_days == Decimal("4.8")


def test_processing_time_paid_leg_independent_of_approval_leg():
    """Some invoices approved, fewer paid — counts differ; only
    rows with both timestamps contribute to the paid leg."""
    base = datetime(2026, 5, 1, tzinfo=UTC)
    invs = [
        _inv(base, base + timedelta(days=1), base + timedelta(days=5)),
        _inv(base, base + timedelta(days=1), base + timedelta(days=5)),
        _inv(base, base + timedelta(days=1), base + timedelta(days=5)),
        _inv(base, base + timedelta(days=1), base + timedelta(days=5)),
        _inv(base, base + timedelta(days=2), None),  # approved not paid
    ]
    pt = compute_processing_time_metrics(invs)
    assert pt.count_approval_leg == 5
    assert pt.count_paid_leg == 4


# ---------------------------------------------------------------------------
# Approval bottleneck
# ---------------------------------------------------------------------------


def _step(assigned_to, age_days, name=None):
    now = datetime(2026, 5, 10, tzinfo=UTC)
    return SimpleNamespace(
        assigned_to=assigned_to,
        assignee_name=name,
        created_at=now - timedelta(days=age_days),
    )


def test_approval_bottleneck_rolls_up_per_approver():
    now = datetime(2026, 5, 10, tzinfo=UTC)
    steps = [
        _step("alice", 2, name="Alice"),
        _step("alice", 4, name="Alice"),
        _step("bob", 1, name="Bob"),
    ]
    rows = compute_approval_bottleneck(steps, now=now)
    by_id = {r.approver_id: r for r in rows}
    assert by_id["alice"].pending_count == 2
    assert by_id["alice"].oldest_pending_days == Decimal("4.0")
    assert by_id["alice"].avg_pending_days == Decimal("3.0")
    assert by_id["bob"].pending_count == 1
    # Sort: alice wins on count.
    assert rows[0].approver_id == "alice"


def test_approval_bottleneck_groups_unassigned_under_synthetic_key():
    """Unassigned steps roll under the `unassigned` key — surfacing
    them is its own routing-broken signal."""
    now = datetime(2026, 5, 10, tzinfo=UTC)
    steps = [_step(None, 3), _step(None, 5)]
    rows = compute_approval_bottleneck(steps, now=now)
    assert len(rows) == 1
    assert rows[0].approver_id == "unassigned"
    assert rows[0].pending_count == 2


def test_approval_bottleneck_empty_input_returns_empty_list():
    assert compute_approval_bottleneck([]) == []


# ---------------------------------------------------------------------------
# Discount capture
# ---------------------------------------------------------------------------


def _disc(eligible, amount, paid_before, *, elapsed=True):
    """`elapsed` defaults True — an un-captured row is only a MISS once its
    window has closed. See `analytics.DiscountCaptureMetrics`."""
    return SimpleNamespace(
        discount_eligible=eligible,
        discount_amount=amount,
        paid_before_discount_date=paid_before,
        discount_window_elapsed=elapsed,
    )


def test_discount_capture_counts_eligible_and_captured():
    rows = [
        _disc(True, Decimal("100"), True),
        _disc(True, Decimal("50"), False),
        _disc(False, Decimal("999"), False),  # ineligible — skipped
    ]
    d = compute_discount_capture(rows)
    assert d.eligible_count == 2
    assert d.captured_count == 1
    assert d.missed_count == 1
    assert d.pending_count == 0
    assert d.captured_amount == Decimal("100.00")
    assert d.missed_amount == Decimal("50.00")
    assert d.capture_rate_pct == Decimal("50.0")
    assert d.insufficient_data is False


def test_discount_capture_open_window_is_pending_not_missed():
    """An un-captured row whose discount deadline has NOT passed has missed
    nothing yet — counting it as missed reported forgone savings that were
    still on the table, and dragged the capture rate down with them."""
    rows = [
        _disc(True, Decimal("100"), True),
        _disc(True, Decimal("50"), False, elapsed=False),
    ]
    d = compute_discount_capture(rows)
    assert d.eligible_count == 2
    assert d.captured_count == 1
    assert d.missed_count == 0
    assert d.pending_count == 1
    assert d.missed_amount == Decimal("0.00")
    assert d.pending_amount == Decimal("50.00")
    # Rate is over the DECIDED population only.
    assert d.capture_rate_pct == Decimal("100.0")


def test_discount_capture_empty_reports_no_rate_not_a_reassuring_zero():
    """Zero eligible → no rate at all (and no divide-by-zero). `0%` would
    read as "we captured none of the discounts we could have", the opposite
    of "nothing has been decided yet" (`docs/decisions.md` §34)."""
    d = compute_discount_capture([])
    assert d.eligible_count == 0
    assert d.capture_rate_pct is None
    assert d.insufficient_data is True


# ---------------------------------------------------------------------------
# Discount capture — the grouped reducer `api/dashboard` actually calls
# ---------------------------------------------------------------------------
#
# `discount_capture_from_grouped_rows` exists so the dashboard can let Postgres
# do the per-row classification instead of streaming every discount-scheduled
# invoice into Python. `compute_discount_capture` above stays as the reference
# implementation: these tests drive BOTH over the same population and require
# an identical `DiscountCaptureMetrics`, which is what stops the two drifting.


def _group(bucket, count, amount, *, reporting=None, unconverted=0):
    return {
        "bucket": bucket,
        "count": count,
        "amount": Decimal(amount),
        "reporting_amount": Decimal(reporting if reporting is not None else amount),
        "unconverted_count": unconverted,
    }


def _rowwise(bucket, amount, *, reporting=None, unconverted=False):
    """One per-invoice row in the bucket the SQL CASE would assign it."""
    return SimpleNamespace(
        discount_eligible=True,
        discount_amount=Decimal(amount),
        discount_amount_reporting=Decimal(reporting if reporting is not None else amount),
        unconverted=unconverted,
        paid_before_discount_date=bucket == "captured",
        discount_window_elapsed=bucket == "missed",
    )


@pytest.mark.parametrize(
    "rows",
    [
        # Nothing at all — the empty state must survive the port.
        [],
        # Only pending: no window has closed, so there is no rate to report.
        [("pending", "40.00"), ("pending", "60.00")],
        # A decided population, mixed.
        [("captured", "100.00"), ("missed", "50.00"), ("pending", "25.00")],
        # Every bucket populated more than once, so the reducer has to add.
        [
            ("captured", "10.00"),
            ("captured", "0.01"),
            ("missed", "7.77"),
            ("missed", "0.03"),
            ("pending", "1.00"),
        ],
        # Perfect capture — the rate must be 100, not None.
        [("captured", "5.00")],
        # Nothing captured but a window HAS closed: 0 % is the right answer
        # here, and must not be confused with the undecided case above.
        [("missed", "5.00")],
    ],
)
def test_grouped_reducer_matches_the_row_at_a_time_fold(rows):
    per_row = [_rowwise(b, a) for b, a in rows]
    grouped = [
        _group(bucket, sum(1 for b, _ in rows if b == bucket), sum_amount)
        for bucket in ("captured", "missed", "pending")
        if (sum_amount := str(sum((Decimal(a) for b, a in rows if b == bucket), Decimal("0"))))
        and any(b == bucket for b, _ in rows)
    ]
    assert compute_discount_capture(per_row) == discount_capture_from_grouped_rows(grouped)


def test_grouped_reducer_carries_the_reporting_figures_and_unconverted_count():
    """The reporting amounts and the fallback count are per-bucket sums, not
    re-derived here — a bucket whose rows could not be converted must still say
    so, or the tile shows a part-converted total as a converted one
    (`docs/decisions.md` §35)."""
    d = discount_capture_from_grouped_rows(
        [
            _group("captured", 2, "100.00", reporting="120.00", unconverted=1),
            _group("missed", 1, "50.00", reporting="50.00"),
        ]
    )
    assert d.captured_amount == Decimal("100.00")
    assert d.captured_amount_reporting == Decimal("120.00")
    assert d.missed_amount_reporting == Decimal("50.00")
    # Counted across every bucket — the disclosure is about the whole tile.
    assert d.unconverted_count == 1
    assert d.capture_rate_pct == Decimal("66.7")


def test_grouped_reducer_refuses_a_bucket_it_does_not_recognise():
    """The query's CASE is exhaustive, so an unknown key means the caller and
    this function have drifted. Dropping it silently would understate every
    figure in the tile — including the capture rate's denominator."""
    with pytest.raises(ValueError, match="unknown discount-capture bucket"):
        discount_capture_from_grouped_rows([_group("expired", 1, "10.00")])


# ---------------------------------------------------------------------------
# DPO
# ---------------------------------------------------------------------------


def test_dpo_basic_formula():
    """AP $100k, COGS $1M, 365-day period → 36.5 days payable."""
    assert compute_dpo(
        accounts_payable=Decimal("100000"),
        cogs=Decimal("1000000"),
        period_days=365,
    ) == Decimal("36.5")


def test_dpo_zero_cogs_returns_zero_not_divide_by_zero():
    assert compute_dpo(accounts_payable=Decimal("100000"), cogs=Decimal("0")) == Decimal("0")


def test_dpo_trend_appends_dpo_per_row():
    snapshots = [
        {"month": "2026-04", "accounts_payable": Decimal("20000"), "cogs": Decimal("200000")},
        {"month": "2026-05", "accounts_payable": Decimal("30000"), "cogs": Decimal("200000")},
    ]
    out = compute_dpo_trend(snapshots, period_days=30)
    # (20000 / 200000) * 30 = 3.0; (30000 / 200000) * 30 = 4.5
    assert out[0]["dpo"] == Decimal("3.0")
    assert out[1]["dpo"] == Decimal("4.5")


# ---------------------------------------------------------------------------
# Cash conversion cycle
# ---------------------------------------------------------------------------


def test_ccc_returns_none_when_dso_or_dio_missing():
    """AP-only product — no DSO/DIO data → None, not a misleading
    zero."""
    assert (
        compute_cash_conversion_cycle(dso_days=None, dio_days=Decimal("40"), dpo_days=Decimal("36"))
        is None
    )
    assert (
        compute_cash_conversion_cycle(dso_days=Decimal("45"), dio_days=None, dpo_days=Decimal("36"))
        is None
    )


def test_ccc_combines_three_legs():
    """DSO 45 + DIO 40 − DPO 36 = 49 days."""
    assert compute_cash_conversion_cycle(
        dso_days=Decimal("45"), dio_days=Decimal("40"), dpo_days=Decimal("36")
    ) == Decimal("49.0")


# ---------------------------------------------------------------------------
# Accruals
# ---------------------------------------------------------------------------


def test_accruals_subtracts_unposted_invoices():
    """open_po + received − unposted = total_accrual. Order of
    operations matters; pin it."""
    a = compute_accruals(
        open_po_amount=Decimal("50000"),
        received_amount=Decimal("10000"),
        unposted_invoice_amount=Decimal("8000"),
    )
    assert a.total_accrual == Decimal("52000.00")


def test_value_received_goods_partial_and_full_receipt():
    """Mirrors the seed: po4 = 4 of 5 laptops ($12k → $9.6k) + po5 =
    fully-received freight ($6.3k) → $15,900 received accrual."""
    received = value_received_goods(
        [
            ReceivedPO(
                po_total=Decimal("12000"),
                po_qty_total=Decimal("5"),
                gr_qty_total=Decimal("4"),
            ),
            ReceivedPO(
                po_total=Decimal("6300"),
                po_qty_total=Decimal("1"),
                gr_qty_total=Decimal("1"),
            ),
        ]
    )
    assert received == Decimal("15900.00")


def test_value_received_goods_no_line_quantities_treated_as_fully_received():
    """A receipted PO with no quantified lines (po_qty_total 0) values at
    the full PO total — the only signal is that goods arrived."""
    received = value_received_goods(
        [ReceivedPO(po_total=Decimal("2000"), po_qty_total=Decimal("0"), gr_qty_total=Decimal("0"))]
    )
    assert received == Decimal("2000.00")


def test_value_received_goods_over_receipt_caps_at_po_total():
    """Receiving more than ordered never inflates the accrual past the
    PO's value — the fraction is capped at 1.0."""
    received = value_received_goods(
        [
            ReceivedPO(
                po_total=Decimal("1000"), po_qty_total=Decimal("10"), gr_qty_total=Decimal("15")
            )
        ]
    )
    assert received == Decimal("1000.00")


def test_value_received_goods_empty_returns_zero():
    assert value_received_goods([]) == Decimal("0")


def test_value_received_goods_returns_decimal_not_float():
    received = value_received_goods(
        [ReceivedPO(po_total=Decimal("100"), po_qty_total=Decimal("3"), gr_qty_total=Decimal("1"))]
    )
    assert isinstance(received, Decimal)
    # 100 × 1/3 = 33.333... quantized to cents (ROUND_HALF_UP)
    assert received == Decimal("33.33")


# ---------------------------------------------------------------------------
# Working-capital impact
# ---------------------------------------------------------------------------


def test_working_capital_impact_monotone_in_days_extended():
    """Extending 10 days unlocks 2× the cash of extending 5 days."""
    base = compute_working_capital_impact(avg_daily_outflow=Decimal("1000"), days_extended=5)
    doubled = compute_working_capital_impact(avg_daily_outflow=Decimal("1000"), days_extended=10)
    assert doubled == base * Decimal("2")


def test_working_capital_impact_zero_or_negative_returns_zero():
    assert compute_working_capital_impact(
        avg_daily_outflow=Decimal("1000"), days_extended=0
    ) == Decimal("0")
    assert compute_working_capital_impact(
        avg_daily_outflow=Decimal("0"), days_extended=10
    ) == Decimal("0")


# ---------------------------------------------------------------------------
# Supplier concentration
# ---------------------------------------------------------------------------


def test_supplier_concentration_flags_when_top_vendor_exceeds_threshold():
    rows = [
        {"vendor": "Big Vendor", "amount": Decimal("700000")},
        {"vendor": "Small Vendor", "amount": Decimal("300000")},
    ]
    c = compute_supplier_concentration(rows)
    # Top vendor share is 70% — well over the default 25% threshold.
    assert c.flagged is True
    assert c.largest_vendor == "Big Vendor"
    assert c.largest_vendor_share_pct == Decimal("70.0")


def test_supplier_concentration_no_flag_when_diversified():
    rows = [{"vendor": f"V{i}", "amount": Decimal("10000")} for i in range(20)]
    c = compute_supplier_concentration(rows)
    assert c.flagged is False
    # Top 10 of 20 equal vendors → exactly 50% share.
    assert c.top_10_share_pct == Decimal("50.0")


def test_supplier_concentration_empty_input_safe():
    c = compute_supplier_concentration([])
    assert c.total_spend == Decimal("0")
    assert c.flagged is False
    assert c.largest_vendor is None


def test_supplier_concentration_zero_total_safe():
    """Vendors listed but with zero amounts → don't divide by zero."""
    rows = [{"vendor": "A", "amount": Decimal("0")}]
    c = compute_supplier_concentration(rows)
    assert c.total_spend == Decimal("0")


# ---------------------------------------------------------------------------
# Fraud rate trend
# ---------------------------------------------------------------------------


def test_fraud_rate_trend_zero_invoices_is_not_computable():
    """One month had zero invoices — the rate is UNKNOWN, not zero, and not a
    divide-by-zero. `0.0` is the most reassuring value on the chart and it was
    being reported for the one shape that carries no information at all."""
    rows = [{"month": "2026-05", "invoice_count": 0, "exception_count": 0}]
    out = compute_fraud_rate_trend(rows)
    assert out[0]["rate_pct"] is None
    assert out[0]["insufficient_data"] is True


def test_fraud_rate_trend_computes_rate_per_month():
    rows = [
        {"month": "2026-04", "invoice_count": 100, "exception_count": 5},
        {"month": "2026-05", "invoice_count": 200, "exception_count": 20},
    ]
    out = compute_fraud_rate_trend(rows)
    assert out[0]["rate_pct"] == Decimal("5.0")
    assert out[1]["rate_pct"] == Decimal("10.0")


# ---------------------------------------------------------------------------
# Rebate yield
# ---------------------------------------------------------------------------


def test_rebate_yield_basic_share_and_annualised():
    y = compute_rebate_yield(
        rebates_total=Decimal("5000"),
        total_spend=Decimal("500000"),
        months_in_period=6,
    )
    # 5000 / 500000 × 100 = 1.0%
    assert y["yield_pct"] == Decimal("1.00")
    # Annualised: rebates × 12/months = 5000 × 2 = 10000
    assert y["annualised_rebates"] == Decimal("10000.00")


def test_rebate_yield_zero_spend_returns_zero():
    y = compute_rebate_yield(
        rebates_total=Decimal("0"),
        total_spend=Decimal("0"),
    )
    assert y["yield_pct"] == Decimal("0")


# ---------------------------------------------------------------------------
# Forecast variance
# ---------------------------------------------------------------------------


def test_forecast_variance_positive_when_actual_exceeds_forecast():
    """Forecast $100k, actual $120k → +$20k variance, +20%."""
    out = compute_forecast_variance(
        [{"month": "2026-05", "forecast": "100000", "actual": "120000"}]
    )
    assert out[0]["variance"] == Decimal("20000.00")
    assert out[0]["variance_pct"] == Decimal("20.0")


def test_forecast_variance_negative_when_under_budget():
    out = compute_forecast_variance([{"month": "2026-05", "forecast": "100000", "actual": "80000"}])
    assert out[0]["variance"] == Decimal("-20000.00")
    assert out[0]["variance_pct"] == Decimal("-20.0")


def test_forecast_variance_zero_forecast_returns_zero_pct():
    """Zero forecast — variance computes (just the actual), but
    variance_pct returns 0 (avoid divide-by-zero)."""
    out = compute_forecast_variance([{"month": "2026-05", "forecast": "0", "actual": "5000"}])
    assert out[0]["variance"] == Decimal("5000.00")
    assert out[0]["variance_pct"] == Decimal("0")


# ---------------------------------------------------------------------------
# Predictive cash-flow forecasting — bucket_outflows
# ---------------------------------------------------------------------------


def _commit(due, amount, committed=True, discount_date=None, discount_percent=None):
    return {
        "due_date": due,
        "amount": Decimal(str(amount)),
        "committed": committed,
        "discount_date": discount_date,
        "discount_percent": discount_percent,
    }


def test_bucket_outflows_groups_by_week():
    """Two invoices in the same ISO week land in one bucket; one in the
    next week lands in its own. Week is Monday-anchored."""
    rows = [
        _commit(date(2026, 6, 1), "100"),  # Monday
        _commit(date(2026, 6, 3), "50"),  # Wednesday, same week
        _commit(date(2026, 6, 8), "25"),  # next Monday
    ]
    out = bucket_outflows(rows, granularity="week")
    assert len(out) == 2
    assert out[0]["period"] == "2026-06-01"
    assert out[0]["scheduled_amount"] == Decimal("150.00")
    assert out[0]["count"] == 2
    assert out[1]["period"] == "2026-06-08"
    assert out[1]["scheduled_amount"] == Decimal("25.00")


def test_bucket_outflows_groups_by_month_and_day():
    rows = [
        _commit(date(2026, 6, 1), "100"),
        _commit(date(2026, 6, 30), "200"),
        _commit(date(2026, 7, 1), "300"),
    ]
    by_month = bucket_outflows(rows, granularity="month")
    assert [b["period"] for b in by_month] == ["2026-06", "2026-07"]
    assert by_month[0]["scheduled_amount"] == Decimal("300.00")

    by_day = bucket_outflows(rows, granularity="day")
    assert [b["period"] for b in by_day] == ["2026-06-01", "2026-06-30", "2026-07-01"]


def test_bucket_outflows_splits_committed_and_pending():
    rows = [
        _commit(date(2026, 6, 1), "100", committed=True),
        _commit(date(2026, 6, 2), "40", committed=False),
    ]
    out = bucket_outflows(rows, granularity="week")
    assert out[0]["committed_amount"] == Decimal("100.00")
    assert out[0]["pending_amount"] == Decimal("40.00")
    assert out[0]["scheduled_amount"] == Decimal("140.00")


def test_bucket_outflows_discount_eligible_amount():
    """Only rows with a positive discount_percent AND a discount window that is
    STILL OPEN count toward discount_eligible_amount."""
    rows = [
        _commit(
            date(2026, 6, 1), "100", discount_date=date(2026, 5, 25), discount_percent=Decimal("2")
        ),
        _commit(date(2026, 6, 2), "50", discount_date=None, discount_percent=None),
    ]
    out = bucket_outflows(rows, granularity="week", today=date(2026, 5, 20))
    assert out[0]["discount_eligible_amount"] == Decimal("100.00")


def test_bucket_outflows_excludes_an_elapsed_discount_window():
    """An in-horizon invoice on 2/10-net-60 terms routinely arrives with a
    `discount_date` that passed weeks ago — `_commitment_rows` bounds only the
    DUE date. Counting it reported a saving nobody can still take, and told a
    CFO to fund a payment run against it. Same rule the discount optimizer and
    the `early` what-if scenario already apply."""
    rows = [
        _commit(
            date(2026, 6, 1), "100", discount_date=date(2026, 5, 25), discount_percent=Decimal("2")
        ),
    ]
    out = bucket_outflows(rows, granularity="week", today=date(2026, 5, 26))
    assert out[0]["scheduled_amount"] == Decimal("100.00")
    assert out[0]["discount_eligible_amount"] == Decimal("0")


def test_bucket_outflows_drops_rows_without_due_date():
    rows = [_commit(None, "100"), _commit(date(2026, 6, 1), "50")]
    out = bucket_outflows(rows, granularity="week")
    assert len(out) == 1
    assert out[0]["scheduled_amount"] == Decimal("50.00")


def test_bucket_outflows_counts_unconvertible_rows():
    """`_commitment_rows` includes a foreign invoice with no usable FX lock at
    FACE VALUE and flags it `unconverted`. The flag was computed on every row
    and read by nobody, so a ¥10,000,000 invoice silently entered a USD curve
    as 10,000,000 — enough to drag a $250k opening balance to a projected
    −$9.75M that the shortfall sweep then emails the CFO about. The count is
    what makes it visible."""
    rows = [
        {**_commit(date(2026, 6, 10), "10000000"), "unconverted": True},
        {**_commit(date(2026, 6, 11), "1000"), "unconverted": False},
        _commit(date(2026, 6, 12), "500"),  # no flag at all → not unconverted
    ]
    out = bucket_outflows(rows, granularity="week")
    assert len(out) == 1
    assert out[0]["count"] == 3
    assert out[0]["unconverted_count"] == 1
    # The amount is unchanged — dropping the row would understate the outflow.
    assert out[0]["scheduled_amount"] == Decimal("10001500.00")


def test_cash_position_carries_the_unconvertible_count_through():
    """The caveat has to reach the curve, not stop at the buckets — the closing
    balance carries forward, so one unconvertible row poisons every later
    period."""
    periods = bucket_outflows(
        [{**_commit(date(2026, 6, 10), "10000000"), "unconverted": True}],
        granularity="week",
    )
    rows = compute_cash_position(Decimal("250000"), periods)
    assert rows[0]["unconverted_count"] == 1


def test_cash_position_defaults_the_count_for_hand_built_periods():
    """A caller that builds period dicts itself (the older tests, the alert
    sweep's fixtures) must not KeyError."""
    rows = compute_cash_position(
        Decimal("1000"),
        [
            {
                "period": "2026-06-01",
                "period_start": date(2026, 6, 1),
                "period_end": date(2026, 6, 7),
                "scheduled_amount": Decimal("300"),
            }
        ],
    )
    assert rows[0]["unconverted_count"] == 0


def test_whatif_reports_the_unconvertible_count():
    """Re-timing a row doesn't make it convertible. Comparing `early` against
    `late` totals only means something once this is 0."""
    rows = [
        {
            **_commit(
                date(2026, 6, 10),
                "10000000",
                discount_date=date(2026, 5, 30),
                discount_percent=Decimal("2"),
            ),
            "unconverted": True,
        },
        {**_commit(date(2026, 6, 11), "1000"), "unconverted": False},
    ]
    for scenario in ("early", "on_time", "late"):
        out = apply_payment_timing_scenario(rows, scenario=scenario, today=date(2026, 5, 20))
        assert out["unconverted_count"] == 1, scenario


def test_bucket_outflows_money_is_decimal_not_float():
    rows = [_commit(date(2026, 6, 1), "100.10")]
    out = bucket_outflows(rows, granularity="week")
    assert isinstance(out[0]["scheduled_amount"], Decimal)
    assert isinstance(out[0]["committed_amount"], Decimal)
    assert isinstance(out[0]["discount_eligible_amount"], Decimal)


# ---------------------------------------------------------------------------
# What-if payment-timing scenarios
# ---------------------------------------------------------------------------


def test_whatif_early_captures_discount():
    """Early pays on the discount date and reduces the outflow by the
    discount percent; the captured discount is reported separately."""
    today = date(2026, 5, 20)
    rows = [
        _commit(
            date(2026, 6, 10),
            "1000",
            discount_date=date(2026, 5, 30),
            discount_percent=Decimal("2"),
        ),
    ]
    out = apply_payment_timing_scenario(rows, scenario="early", today=today)
    # 2% of 1000 = 20 captured; net outflow 980.
    assert out["total_discount_captured"] == Decimal("20.00")
    assert out["total_outflow"] == Decimal("980.00")


def test_whatif_on_time_takes_no_discount():
    today = date(2026, 5, 20)
    rows = [
        _commit(
            date(2026, 6, 10),
            "1000",
            discount_date=date(2026, 5, 30),
            discount_percent=Decimal("2"),
        ),
    ]
    out = apply_payment_timing_scenario(rows, scenario="on_time", today=today)
    assert out["total_discount_captured"] == Decimal("0.00")
    assert out["total_outflow"] == Decimal("1000.00")


def test_whatif_late_shifts_pay_date_and_forfeits_discount():
    """Late pays due_date + grace_days, no discount. The weighted avg
    days-to-pay is larger than on-time for the same rows."""
    today = date(2026, 5, 20)
    rows = [
        _commit(
            date(2026, 6, 10),
            "1000",
            discount_date=date(2026, 5, 30),
            discount_percent=Decimal("2"),
        ),
    ]
    on_time = apply_payment_timing_scenario(rows, scenario="on_time", today=today)
    late = apply_payment_timing_scenario(rows, scenario="late", grace_days=15, today=today)
    assert late["total_discount_captured"] == Decimal("0.00")
    assert late["total_outflow"] == Decimal("1000.00")
    assert late["weighted_avg_pay_date_days"] > on_time["weighted_avg_pay_date_days"]


def test_whatif_early_without_discount_falls_back_to_due_date():
    today = date(2026, 5, 20)
    rows = [_commit(date(2026, 6, 10), "1000")]
    out = apply_payment_timing_scenario(rows, scenario="early", today=today)
    assert out["total_discount_captured"] == Decimal("0.00")
    assert out["total_outflow"] == Decimal("1000.00")


def test_whatif_early_ignores_an_elapsed_discount_window():
    """A discount whose deadline already passed is NOT capturable.

    The commitment rows are bounded on their DUE date only, so an in-horizon
    invoice on `2/10 net 60` terms arrives with a `discount_date` weeks in the
    past. Claiming that discount overstated the savings AND timed the outflow
    on a date before `today` — bucketing cash out in a period that has already
    closed and reporting a NEGATIVE weighted average days-to-pay.
    """
    today = date(2026, 5, 20)
    rows = [
        _commit(
            date(2026, 6, 10),
            "1000",
            discount_date=date(2026, 5, 10),  # window shut 10 days ago
            discount_percent=Decimal("2"),
        ),
    ]
    out = apply_payment_timing_scenario(rows, scenario="early", today=today)
    # No savings are claimed, and the full amount leaves on the due date.
    assert out["total_discount_captured"] == Decimal("0.00")
    assert out["total_outflow"] == Decimal("1000.00")
    # Cash leaves in the FUTURE, and every bucket sits at or after today.
    assert out["weighted_avg_pay_date_days"] > 0
    assert out["periods"]
    assert all(p["period_end"] >= today for p in out["periods"])


def test_whatif_early_still_captures_a_window_open_today():
    """The boundary is inclusive — a deadline of exactly `today` is live."""
    today = date(2026, 5, 20)
    rows = [
        _commit(
            date(2026, 6, 10),
            "1000",
            discount_date=today,
            discount_percent=Decimal("2"),
        ),
    ]
    out = apply_payment_timing_scenario(rows, scenario="early", today=today)
    assert out["total_discount_captured"] == Decimal("20.00")
    assert out["total_outflow"] == Decimal("980.00")


def test_whatif_early_mixes_live_and_elapsed_windows():
    """Only the live window contributes savings; the elapsed one pays in full."""
    today = date(2026, 5, 20)
    rows = [
        _commit(
            date(2026, 6, 10),
            "1000",
            discount_date=date(2026, 5, 30),  # still open
            discount_percent=Decimal("2"),
        ),
        _commit(
            date(2026, 6, 20),
            "2000",
            discount_date=date(2026, 5, 1),  # elapsed
            discount_percent=Decimal("3"),
        ),
    ]
    out = apply_payment_timing_scenario(rows, scenario="early", today=today)
    assert out["total_discount_captured"] == Decimal("20.00")
    assert out["total_outflow"] == Decimal("2980.00")


# ---------------------------------------------------------------------------
# Cash position + threshold breaches
# ---------------------------------------------------------------------------


def test_cash_position_running_balance_carries_forward():
    """Closing of period N equals opening of period N+1."""
    periods = [
        {
            "period": "2026-06-01",
            "period_start": date(2026, 6, 1),
            "period_end": date(2026, 6, 7),
            "scheduled_amount": Decimal("300"),
        },
        {
            "period": "2026-06-08",
            "period_start": date(2026, 6, 8),
            "period_end": date(2026, 6, 14),
            "scheduled_amount": Decimal("200"),
        },
    ]
    rows = compute_cash_position(Decimal("1000"), periods)
    assert rows[0]["opening"] == Decimal("1000.00")
    assert rows[0]["closing"] == Decimal("700.00")
    assert rows[1]["opening"] == Decimal("700.00")
    assert rows[1]["closing"] == Decimal("500.00")


def test_cash_position_flags_threshold_breach():
    periods = [
        {
            "period": "2026-06-01",
            "period_start": date(2026, 6, 1),
            "period_end": date(2026, 6, 7),
            "scheduled_amount": Decimal("800"),
        },
    ]
    rows = compute_cash_position(Decimal("1000"), periods, min_balance_threshold=Decimal("500"))
    # closing = 200 < 500 → flagged.
    assert rows[0]["below_threshold"] is True
    breaches = detect_threshold_breaches(rows, min_balance_threshold=Decimal("500"))
    assert len(breaches) == 1
    assert breaches[0]["shortfall"] == Decimal("300.00")


def test_cash_position_no_threshold_no_flag():
    periods = [
        {
            "period": "2026-06-01",
            "period_start": date(2026, 6, 1),
            "period_end": date(2026, 6, 7),
            "scheduled_amount": Decimal("800"),
        },
    ]
    rows = compute_cash_position(Decimal("1000"), periods)
    assert rows[0]["below_threshold"] is False


def test_cash_position_money_is_decimal():
    periods = [
        {
            "period": "2026-06-01",
            "period_start": date(2026, 6, 1),
            "period_end": date(2026, 6, 7),
            "scheduled_amount": Decimal("123.45"),
        },
    ]
    rows = compute_cash_position(Decimal("1000"), periods)
    assert isinstance(rows[0]["opening"], Decimal)
    assert isinstance(rows[0]["closing"], Decimal)
    assert isinstance(rows[0]["outflow"], Decimal)


def test_accruals_by_currency_net_each_currency_within_itself():
    """A currency present in any leg gets a row with zeros for the legs it is
    absent from; each row's total is its own open + received − unposted; the
    unknown-currency row (None) sorts last (decisions §197)."""
    from app.services.analytics import compute_accruals_by_currency

    rows = compute_accruals_by_currency(
        open_po_amounts={"USD": Decimal("5000"), None: Decimal("700"), "EUR": Decimal("3000")},
        received_amounts={"EUR": Decimal("1500")},
        unposted_invoice_amounts={"USD": Decimal("1000"), "GBP": Decimal("40.5")},
    )
    assert [r.currency for r in rows] == ["EUR", "GBP", "USD", None]
    by_code = {r.currency: r for r in rows}
    assert by_code["EUR"].total_accrual == Decimal("4500.00")
    assert by_code["GBP"].open_po_amount == Decimal("0.00")
    assert by_code["GBP"].total_accrual == Decimal("-40.50")
    assert by_code["USD"].total_accrual == Decimal("4000.00")
    assert by_code[None].total_accrual == Decimal("700.00")
    assert (
        compute_accruals_by_currency(
            open_po_amounts={}, received_amounts={}, unposted_invoice_amounts={}
        )
        == []
    )
