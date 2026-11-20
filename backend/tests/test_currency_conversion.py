"""Reporting-currency conversion service + aggregate rollups.

Covers the multi-currency reporting layer that sits ON TOP of the existing
payment-level FX (international_payments). Pins:

  resolve_reporting_currency
    - explicit settings.reporting_currency wins
    - falls back to payments.home_currency, then invoice_defaults.currency,
      then the platform default
    - always uppercased; empty degrades to the next rung, a set malformed rung answers "" (§473)

  convert_amount
    - same currency → rate 1, no adapter call, exact
    - cross currency → amount * rate, quantized 2dp
    - non-positive provider rate → ValueError (no silent zero)

  materialize_reporting_amount
    - locks reporting_* onto a row, rate persisted
    - idempotent: a second call with the same reporting currency is a no-op
    - re-materializes when the reporting currency changes
    - never recomputes an already-locked row (historical stability)

  rollup_to_reporting_currency
    - sums rate-locked reporting_amount where present
    - same-currency rows convert 1:1
    - foreign rows with no lock fall back to face value + flagged unconverted
    - per-currency breakdown

  compute_unrealized_fx_gain_loss
    - gain when the open foreign liability is worth less at today's rate
    - loss when it's worth more
    - same-currency invoices contribute nothing
    - one FX call per distinct currency

The mock FX adapter is fed pinned rates so all arithmetic is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

import pytest

from app.services.currency_conversion import (
    compute_unrealized_fx_gain_loss,
    convert_amount,
    materialize_reporting_amount,
    reporting_amount_for_row,
    resolve_reporting_currency,
    rollup_from_grouped_rows,
    rollup_to_reporting_currency,
    vendor_rollup_to_reporting_currency,
)
from app.services.fx_adapters.mock_adapter import MockFXAdapter

# ---------------------------------------------------------------------------
# resolve_reporting_currency
# ---------------------------------------------------------------------------


def test_resolve_explicit_reporting_currency_wins():
    assert (
        resolve_reporting_currency(
            {
                "reporting_currency": "gbp",
                "payments": {"home_currency": "USD"},
                "invoice_defaults": {"currency": "EUR"},
            }
        )
        == "GBP"
    )


def test_resolve_falls_back_to_home_currency():
    assert resolve_reporting_currency({"payments": {"home_currency": "eur"}}) == "EUR"


def test_resolve_falls_back_to_invoice_defaults_currency():
    assert resolve_reporting_currency({"invoice_defaults": {"currency": "cad"}}) == "CAD"


def test_resolve_defaults_to_platform_default_when_unset():
    # The platform default is "USD" (config.reporting_currency_default).
    assert resolve_reporting_currency({}) == "USD"
    assert resolve_reporting_currency(None) == "USD"


def test_resolve_ignores_blank_values():
    assert resolve_reporting_currency({"reporting_currency": "   ", "payments": {}}) == "USD"


# ---------------------------------------------------------------------------
# convert_amount
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_convert_same_currency_is_rate_one_no_adapter_call():
    fx = MockFXAdapter()
    fx.get_rate = _exploding_get_rate()
    out = await convert_amount(
        amount=Decimal("1234.56"),
        source_currency="USD",
        reporting_currency="USD",
        fx_adapter=fx,
    )
    assert out.fx_rate == Decimal("1")
    assert out.amount == Decimal("1234.56")


@pytest.mark.asyncio
async def test_convert_cross_currency_applies_rate():
    # EUR->USD: mock USD->EUR=0.92, so EUR->USD = 1/0.92 ≈ 1.086957.
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    out = await convert_amount(
        amount=Decimal("1000.00"),
        source_currency="EUR",
        reporting_currency="USD",
        fx_adapter=fx,
    )
    # 1000 * (1/0.92) = 1086.9565… → 1086.96
    assert out.amount == Decimal("1086.96")
    assert out.source_currency == "EUR"
    assert out.reporting_currency == "USD"


@pytest.mark.asyncio
async def test_convert_refuses_non_positive_rate():
    fx = MockFXAdapter()
    fx.get_rate = _fixed_rate(Decimal("0"))
    with pytest.raises(ValueError, match="non-positive"):
        await convert_amount(
            amount=Decimal("100"),
            source_currency="EUR",
            reporting_currency="USD",
            fx_adapter=fx,
        )


# ---------------------------------------------------------------------------
# materialize_reporting_amount
# ---------------------------------------------------------------------------


def _invoice_row(*, amount=Decimal("1000.00"), currency="EUR"):
    return SimpleNamespace(
        amount=amount,
        currency=currency,
        reporting_currency=None,
        reporting_amount=None,
        reporting_fx_rate=None,
        reporting_source_currency=None,
        reporting_fx_locked_at=None,
    )


@pytest.mark.asyncio
async def test_materialize_locks_rate_onto_row():
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice_row()
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert changed is True
    assert inv.reporting_currency == "USD"
    assert inv.reporting_amount == Decimal("1086.96")
    # The mock adapter returns the cross-rate quantized to 6dp (1/0.92 →
    # 1.086957), which we persist (re-quantized to 8dp). We assert the exact
    # rate the adapter handed us rather than the full-precision quotient.
    assert inv.reporting_fx_rate == Decimal("1.086957").quantize(Decimal("0.00000001"))
    assert inv.reporting_fx_locked_at is not None


@pytest.mark.asyncio
async def test_materialize_is_idempotent_for_same_currency_and_does_not_refetch():
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice_row()
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    locked_at = inv.reporting_fx_locked_at
    locked_amount = inv.reporting_amount

    # Market moves, but a second materialize for the SAME reporting currency
    # must NOT refetch — historical stability.
    fx2 = MockFXAdapter({"mock_rates": {"EUR": "0.80"}})
    fx2.get_rate = _exploding_get_rate()
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx2)
    assert changed is False
    assert inv.reporting_amount == locked_amount
    assert inv.reporting_fx_locked_at == locked_at


@pytest.mark.asyncio
async def test_materialize_refetches_when_reporting_currency_changes():
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92", "GBP": "0.79"}})
    inv = _invoice_row()
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    # Org switches reporting currency to GBP → must re-lock.
    changed = await materialize_reporting_amount(inv, reporting_currency="GBP", fx_adapter=fx)
    assert changed is True
    assert inv.reporting_currency == "GBP"
    # EUR->GBP = 0.79/0.92 ≈ 0.858696; 1000 * that = 858.70
    assert inv.reporting_amount == Decimal("858.70")


@pytest.mark.asyncio
async def test_materialize_same_currency_invoice_locks_rate_one():
    fx = MockFXAdapter()
    fx.get_rate = _exploding_get_rate()
    inv = _invoice_row(currency="USD")
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert changed is True
    assert inv.reporting_amount == Decimal("1000.00")
    assert inv.reporting_fx_rate == Decimal("1").quantize(Decimal("0.00000001"))


# --- the persisted lock must keep describing the row it was locked onto -----
# `amount` and `currency` are both editable on PATCH /api/invoices/{id} right up
# to approval, and `refresh_warnings` re-runs this function afterwards. Before
# these, the lock survived any edit untouched and the rollup reported the stale
# figure as CONVERTED (unconverted=False) — a corrected invoice never reached
# the dashboard / CFO reporting totals.


@pytest.mark.asyncio
async def test_materialize_rescales_at_the_locked_rate_when_the_amount_is_corrected():
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice_row()
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    locked_rate = inv.reporting_fx_rate
    locked_at = inv.reporting_fx_locked_at

    # AP corrects a mis-extracted amount. No FX call is legitimate here — the
    # liability was accrued at the booking rate — so an exploding adapter must
    # not be reached.
    inv.amount = Decimal("5000.00")
    fx_dead = MockFXAdapter()
    fx_dead.get_rate = _exploding_get_rate()
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx_dead)

    assert changed is True
    # 5000 * 1.086957 = 5434.785 → ROUND_HALF_UP → 5434.79
    assert inv.reporting_amount == Decimal("5434.79")
    assert inv.reporting_fx_rate == locked_rate  # historical rate preserved
    assert inv.reporting_fx_locked_at == locked_at


@pytest.mark.asyncio
async def test_materialize_rollup_sees_the_corrected_amount_as_converted():
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice_row()
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    inv.amount = Decimal("5000.00")
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)

    rollup = rollup_to_reporting_currency(
        [
            {
                "amount": inv.amount,
                "currency": inv.currency,
                "reporting_amount": inv.reporting_amount,
                "reporting_currency": inv.reporting_currency,
            }
        ],
        reporting_currency="USD",
    )
    assert rollup.total_reporting_amount == Decimal("5434.79")
    assert rollup.unconverted_count == 0


@pytest.mark.asyncio
async def test_materialize_refetches_when_the_invoice_currency_leaves_the_reporting_currency():
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice_row(currency="USD")
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert inv.reporting_fx_rate == Decimal("1").quantize(Decimal("0.00000001"))

    # Currency corrected USD -> EUR: the persisted rate of 1 no longer
    # describes the pair, so a fresh rate is required.
    inv.currency = "EUR"
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert changed is True
    assert inv.reporting_amount == Decimal("1086.96")
    assert inv.reporting_fx_rate != Decimal("1")


@pytest.mark.asyncio
async def test_materialize_refetches_when_the_invoice_currency_becomes_the_reporting_currency():
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice_row()  # EUR
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert inv.reporting_amount == Decimal("1086.96")

    inv.currency = "USD"
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert changed is True
    assert inv.reporting_amount == Decimal("1000.00")
    assert inv.reporting_fx_rate == Decimal("1").quantize(Decimal("0.00000001"))


@pytest.mark.asyncio
async def test_materialize_persists_a_self_consistent_triple():
    """amount * reporting_fx_rate == reporting_amount, exactly."""
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice_row(amount=Decimal("1234.56"))
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    expected = (inv.amount * inv.reporting_fx_rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    assert inv.reporting_amount == expected


# ---------------------------------------------------------------------------
# reporting_amount_for_row
# ---------------------------------------------------------------------------


def test_row_uses_persisted_lock_when_currency_matches():
    amt, unconv = reporting_amount_for_row(
        amount=Decimal("1000.00"),
        currency="EUR",
        reporting_currency="USD",
        persisted_reporting_currency="USD",
        persisted_reporting_amount=Decimal("1086.96"),
    )
    assert amt == Decimal("1086.96")
    assert unconv is False


def test_row_same_currency_converts_one_to_one_without_lock():
    amt, unconv = reporting_amount_for_row(
        amount=Decimal("500.00"),
        currency="USD",
        reporting_currency="USD",
        persisted_reporting_currency=None,
        persisted_reporting_amount=None,
    )
    assert amt == Decimal("500.00")
    assert unconv is False


def test_row_foreign_without_lock_flagged_unconverted():
    amt, unconv = reporting_amount_for_row(
        amount=Decimal("1000.00"),
        currency="EUR",
        reporting_currency="USD",
        persisted_reporting_currency=None,
        persisted_reporting_amount=None,
    )
    assert amt == Decimal("1000.00")  # falls back to face value
    assert unconv is True


def test_row_stale_lock_for_different_currency_is_not_used():
    # Persisted lock is in GBP but org reports in USD → can't reuse it.
    amt, unconv = reporting_amount_for_row(
        amount=Decimal("1000.00"),
        currency="EUR",
        reporting_currency="USD",
        persisted_reporting_currency="GBP",
        persisted_reporting_amount=Decimal("858.70"),
    )
    assert unconv is True


# ---------------------------------------------------------------------------
# rollup_to_reporting_currency
# ---------------------------------------------------------------------------


def test_rollup_mixes_locked_and_same_currency_rows():
    rows = [
        # USD invoice, no lock needed
        {
            "amount": Decimal("1000.00"),
            "currency": "USD",
            "reporting_amount": None,
            "reporting_currency": None,
        },
        # EUR invoice with a locked USD reporting amount
        {
            "amount": Decimal("1000.00"),
            "currency": "EUR",
            "reporting_amount": Decimal("1086.96"),
            "reporting_currency": "USD",
        },
    ]
    rollup = rollup_to_reporting_currency(rows, reporting_currency="USD")
    assert rollup.reporting_currency == "USD"
    assert rollup.total_reporting_amount == Decimal("2086.96")
    assert rollup.total_count == 2
    assert rollup.unconverted_count == 0
    currencies = {e.currency: e for e in rollup.by_currency}
    assert currencies["USD"].reporting_amount == Decimal("1000.00")
    assert currencies["EUR"].reporting_amount == Decimal("1086.96")


def test_rollup_flags_foreign_rows_without_lock():
    rows = [
        {
            "amount": Decimal("500.00"),
            "currency": "USD",
            "reporting_amount": None,
            "reporting_currency": None,
        },
        {
            "amount": Decimal("300.00"),
            "currency": "GBP",
            "reporting_amount": None,
            "reporting_currency": None,
        },  # no lock
    ]
    rollup = rollup_to_reporting_currency(rows, reporting_currency="USD")
    assert rollup.unconverted_count == 1
    # GBP falls through at face value.
    assert rollup.total_reporting_amount == Decimal("800.00")
    gbp = next(e for e in rollup.by_currency if e.currency == "GBP")
    assert gbp.unconverted_count == 1


def test_rollup_empty_is_zero_snapshot():
    rollup = rollup_to_reporting_currency([], reporting_currency="EUR")
    assert rollup.total_reporting_amount == Decimal("0.00")
    assert rollup.total_count == 0
    assert rollup.by_currency == []


# ---------------------------------------------------------------------------
# rollup_from_grouped_rows — the aggregate (DB-side GROUP BY) path used by the
# dashboard must produce the EXACT same ReportingRollup as the row-at-a-time
# path. This pins the equivalence the dashboard perf fix relies on.
# ---------------------------------------------------------------------------


def _group_like_sql(rows: list[dict], *, reporting_currency: str) -> list[dict]:
    """Reduce per-row dicts to per-currency group dicts exactly the way the
    dashboard's SQL GROUP BY does (the CASE expressions mirror
    `reporting_amount_for_row`), so the two rollup entry points can be compared
    on identical inputs."""
    tgt = reporting_currency.upper()
    groups: dict[str, dict] = {}
    for r in rows:
        amount = Decimal(str(r["amount"]))
        cur = (r["currency"] or tgt).upper()
        rep_cur = r["reporting_currency"]
        rep_amt = r["reporting_amount"]
        locked = rep_amt is not None and rep_cur is not None and rep_cur.upper() == tgt
        g = groups.setdefault(
            cur,
            {
                "currency": cur,
                "original_amount": Decimal("0"),
                "reporting_amount": Decimal("0"),
                "count": 0,
                "unconverted_count": 0,
            },
        )
        g["original_amount"] += amount
        g["reporting_amount"] += Decimal(str(rep_amt)) if locked else amount
        g["count"] += 1
        if not locked and cur != tgt:
            g["unconverted_count"] += 1
    return list(groups.values())


def test_rollup_from_grouped_matches_row_path():
    def _row(amount, currency, rep_amt=None, rep_cur=None):
        return {
            "amount": Decimal(amount),
            "currency": currency,
            "reporting_amount": Decimal(rep_amt) if rep_amt is not None else None,
            "reporting_currency": rep_cur,
        }

    rows = [
        _row("1000.00", "USD"),  # same-currency, no lock
        _row("250.50", "USD"),  # second USD row → exercises per-currency grouping
        _row("1000.00", "EUR", "1086.96", "USD"),  # locked to USD
        _row("400.00", "EUR", "435.00", "USD"),  # second locked EUR row
        _row("300.00", "GBP"),  # foreign, no lock → unconverted
        _row("99.00", "CHF", "120.00", "EUR"),  # stale lock (wrong ccy) → face + unconverted
    ]
    row_based = rollup_to_reporting_currency(rows, reporting_currency="USD")
    grouped = rollup_from_grouped_rows(
        _group_like_sql(rows, reporting_currency="USD"), reporting_currency="USD"
    )
    assert grouped == row_based


def test_rollup_from_grouped_empty_is_zero_snapshot():
    grouped = rollup_from_grouped_rows([], reporting_currency="EUR")
    assert grouped == rollup_to_reporting_currency([], reporting_currency="EUR")


# ---------------------------------------------------------------------------
# vendor_rollup_to_reporting_currency — issue #127: a vendor billing in more
# than one currency was previously summed with a naive SQL SUM(amount),
# silently adding face values across currencies (e.g. 1000 USD + 1000 EUR =
# "2000"). Each vendor's rows must instead be converted into the reporting
# currency before summing.
# ---------------------------------------------------------------------------


def _vs_row(amount, currency, vendor="Acme Co", rep_amt=None, rep_cur=None):
    return {
        "vendor": vendor,
        "amount": Decimal(amount),
        "currency": currency,
        "reporting_amount": Decimal(rep_amt) if rep_amt is not None else None,
        "reporting_currency": rep_cur,
    }


def test_vendor_rollup_converts_mixed_currency_invoices_for_same_vendor():
    # Acme billed once in USD and once in EUR (locked to a real USD rate) —
    # the naive bug would report 1000.00 + 1000.00 = 2000.00 regardless of
    # the EUR row's true USD value.
    rows = [
        _vs_row("1000.00", "USD"),
        _vs_row("1000.00", "EUR", rep_amt="1086.96", rep_cur="USD"),
    ]
    entries = vendor_rollup_to_reporting_currency(rows, reporting_currency="USD")
    assert len(entries) == 1
    acme = entries[0]
    assert acme.vendor == "Acme Co"
    # Correctly converted total, not the naive face-value sum of 2000.00.
    assert acme.amount == Decimal("2086.96")
    assert acme.invoice_count == 2
    assert acme.currencies == ["EUR", "USD"]


def test_vendor_rollup_keeps_vendors_separate_and_sorts_by_amount_desc():
    rows = [
        _vs_row("500.00", "USD", vendor="Small Vendor"),
        _vs_row("1000.00", "USD", vendor="Big Vendor"),
        _vs_row("250.00", "USD", vendor="Big Vendor"),
    ]
    entries = vendor_rollup_to_reporting_currency(rows, reporting_currency="USD")
    assert [e.vendor for e in entries] == ["Big Vendor", "Small Vendor"]
    big = next(e for e in entries if e.vendor == "Big Vendor")
    assert big.amount == Decimal("1250.00")
    assert big.invoice_count == 2
    assert big.currencies == ["USD"]


def test_vendor_rollup_empty_is_empty_list():
    assert vendor_rollup_to_reporting_currency([], reporting_currency="USD") == []


# ---------------------------------------------------------------------------
# compute_unrealized_fx_gain_loss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrealized_gain_when_foreign_liability_worth_less_today():
    # Booked a 1000 EUR invoice at EUR->USD = 1/0.90 = 1.1111 → $1111.11.
    # Today EUR weakened: EUR->USD = 1/0.92 = 1.0870 → mark-to-market $1086.96.
    # Liability shrank in USD terms → unrealized GAIN ≈ +24.15.
    inv = {
        "amount": Decimal("1000.00"),
        "currency": "EUR",
        "reporting_amount": Decimal("1111.11"),
        "reporting_currency": "USD",
    }
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    result = await compute_unrealized_fx_gain_loss([inv], reporting_currency="USD", fx_adapter=fx)
    entry = result.by_currency[0]
    assert entry.currency == "EUR"
    assert entry.booked_reporting_amount == Decimal("1111.11")
    assert entry.current_reporting_amount == Decimal("1086.96")
    assert entry.unrealized_gain_loss == Decimal("24.15")
    assert result.total_unrealized_gain_loss == Decimal("24.15")


@pytest.mark.asyncio
async def test_unrealized_loss_when_foreign_liability_worth_more_today():
    # Booked at 1/0.90 = $1111.11; today EUR strengthened to 1/0.85 = $1176.47.
    # Liability grew → unrealized LOSS ≈ -65.36.
    inv = {
        "amount": Decimal("1000.00"),
        "currency": "EUR",
        "reporting_amount": Decimal("1111.11"),
        "reporting_currency": "USD",
    }
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.85"}})
    result = await compute_unrealized_fx_gain_loss([inv], reporting_currency="USD", fx_adapter=fx)
    assert result.total_unrealized_gain_loss == Decimal("-65.36")


@pytest.mark.asyncio
async def test_unrealized_skips_same_currency_invoices():
    inv = {
        "amount": Decimal("1000.00"),
        "currency": "USD",
        "reporting_amount": Decimal("1000.00"),
        "reporting_currency": "USD",
    }
    fx = MockFXAdapter()
    fx.get_rate = _exploding_get_rate()  # must not be called
    result = await compute_unrealized_fx_gain_loss([inv], reporting_currency="USD", fx_adapter=fx)
    assert result.total_unrealized_gain_loss == Decimal("0.00")
    assert result.by_currency == []


@pytest.mark.asyncio
async def test_unrealized_one_fx_call_per_distinct_currency():
    calls: list[tuple[str, str]] = []

    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92", "GBP": "0.79"}})
    real_get_rate = fx.get_rate

    async def _counting(src, tgt):
        calls.append((src, tgt))
        return await real_get_rate(src, tgt)

    fx.get_rate = _counting
    invs = [
        {
            "amount": Decimal("1000.00"),
            "currency": "EUR",
            "reporting_amount": Decimal("1111.11"),
            "reporting_currency": "USD",
        },
        {
            "amount": Decimal("500.00"),
            "currency": "EUR",
            "reporting_amount": Decimal("555.56"),
            "reporting_currency": "USD",
        },
        {
            "amount": Decimal("200.00"),
            "currency": "GBP",
            "reporting_amount": Decimal("253.16"),
            "reporting_currency": "USD",
        },
    ]
    await compute_unrealized_fx_gain_loss(invs, reporting_currency="USD", fx_adapter=fx)
    # Two EUR rows + one GBP row → exactly two FX calls (one per currency).
    assert sorted(calls) == [("EUR", "USD"), ("GBP", "USD")]


@pytest.mark.asyncio
async def test_unrealized_excludes_and_counts_a_row_with_no_locked_rate():
    """An open foreign invoice whose reporting materialization never succeeded
    has no booked figure, so it can be on NEITHER side of the comparison.

    `reporting_amount_for_row` falls back to the row's face value in its OWN
    currency — right for a spend rollup, wrong here: the mark-to-market leg
    converts the same original amount at today's rate, so the arithmetic
    reports the *conversion itself* as a gain or loss. Dropping the helper's
    `unconverted` flag made one EUR 1,000 invoice produce an $87 unrealized
    LOSS on an exposure that never moved. `decisions §35`.
    """
    inv = {
        "amount": Decimal("1000.00"),
        "currency": "EUR",
        "reporting_amount": None,
        "reporting_currency": None,
    }
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    result = await compute_unrealized_fx_gain_loss([inv], reporting_currency="USD", fx_adapter=fx)
    assert result.total_unrealized_gain_loss == Decimal("0.00")
    assert result.unconverted_count == 1
    # The currency still appears, carrying its count — excluded, not vanished.
    entry = result.by_currency[0]
    assert entry.currency == "EUR"
    assert entry.unconverted_count == 1
    assert entry.open_original_amount == Decimal("0.00")
    assert entry.booked_reporting_amount == Decimal("0.00")
    assert entry.current_reporting_amount == Decimal("0.00")


@pytest.mark.asyncio
async def test_unrealized_mixes_converted_and_unconverted_rows_in_one_currency():
    """The converted rows still produce a real gain/loss; the unconverted one is
    left out of BOTH legs (not just the booked one) so the exposure stays
    internally consistent."""
    invs = [
        {
            "amount": Decimal("1000.00"),
            "currency": "EUR",
            "reporting_amount": Decimal("1111.11"),
            "reporting_currency": "USD",
        },
        {
            "amount": Decimal("9999.00"),
            "currency": "EUR",
            "reporting_amount": None,
            "reporting_currency": None,
        },
    ]
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    result = await compute_unrealized_fx_gain_loss(invs, reporting_currency="USD", fx_adapter=fx)
    entry = result.by_currency[0]
    # Identical to the single-converted-row case — the 9,999 row changed nothing.
    assert entry.open_original_amount == Decimal("1000.00")
    assert entry.booked_reporting_amount == Decimal("1111.11")
    assert entry.current_reporting_amount == Decimal("1086.96")
    assert entry.unrealized_gain_loss == Decimal("24.15")
    assert result.total_unrealized_gain_loss == Decimal("24.15")
    assert result.unconverted_count == 1


@pytest.mark.asyncio
async def test_unrealized_makes_no_fx_call_for_a_wholly_unconverted_currency():
    """No exposure to mark means no rate to fetch — and an FX outage must not
    be what decides whether the omission is reported."""
    inv = {
        "amount": Decimal("1000.00"),
        "currency": "GBP",
        "reporting_amount": None,
        "reporting_currency": None,
    }
    fx = MockFXAdapter()
    fx.get_rate = _exploding_get_rate()  # must not be called
    result = await compute_unrealized_fx_gain_loss([inv], reporting_currency="USD", fx_adapter=fx)
    assert result.unconverted_count == 1
    assert result.total_unrealized_gain_loss == Decimal("0.00")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _exploding_get_rate():
    async def _boom(src, tgt):
        raise AssertionError(f"FX adapter must not be called ({src}->{tgt})")

    return _boom


def _fixed_rate(rate: Decimal):
    async def _rate(src, tgt):
        return SimpleNamespace(
            source=src, target=tgt, rate=rate, as_of=datetime.now(UTC), provider="mock"
        )

    return _rate


# ---------------------------------------------------------------------------
# The lock records WHICH currency its rate was for (migration 0086)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_records_the_source_currency_of_the_locked_rate():
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice_row()  # EUR
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert inv.reporting_source_currency == "EUR"

    same = _invoice_row(currency="USD")
    await materialize_reporting_amount(same, reporting_currency="USD", fx_adapter=fx)
    assert same.reporting_source_currency == "USD"


@pytest.mark.asyncio
async def test_materialize_refetches_on_a_foreign_to_foreign_currency_correction():
    """EUR -> GBP on a USD-reporting org, amount unchanged.

    Neither pre-0086 check could see this: the figure still reconciles
    (`amount * rate == reporting_amount`) and the rate still has the *shape* of
    a cross-currency rate (not exactly 1). The row recorded the rate but not
    which currency it was for, so the stale EUR-derived figure kept being
    reported as a converted, trustworthy number for a GBP invoice.
    """
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92", "GBP": "0.79"}})
    inv = _invoice_row()  # EUR 1000.00
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    eur_figure = inv.reporting_amount
    eur_rate = inv.reporting_fx_rate
    assert eur_figure == Decimal("1086.96")

    inv.currency = "GBP"  # correction only — the amount did not move
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)

    assert changed is True
    assert inv.reporting_source_currency == "GBP"
    assert inv.reporting_fx_rate != eur_rate
    assert inv.reporting_amount != eur_figure
    assert inv.reporting_amount == (inv.amount * inv.reporting_fx_rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


@pytest.mark.asyncio
async def test_an_unchanged_foreign_row_is_still_an_idempotent_no_op():
    """The exact pair check must not make every read re-fetch."""
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice_row()
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert changed is False


@pytest.mark.asyncio
async def test_amount_correction_still_rescales_at_the_locked_rate():
    """The pair still matches, so no FX call — the liability was accrued at the
    rate in force when the invoice was booked."""
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice_row()
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    locked_rate = inv.reporting_fx_rate

    inv.amount = Decimal("2000.00")
    fx_dead = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    fx_dead.get_rate = _fixed_rate(Decimal("0"))  # any FX call would raise
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx_dead)

    assert changed is True
    assert inv.reporting_fx_rate == locked_rate
    assert inv.reporting_amount == (Decimal("2000.00") * locked_rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


@pytest.mark.asyncio
async def test_legacy_row_without_the_column_falls_back_to_the_shape_heuristic():
    """A row locked before migration 0086 has no `reporting_source_currency`.

    It keeps the old inferential check — which still catches an amount edit and
    a currency edit that crosses the reporting currency — rather than being
    treated as unlocked and re-fetched on every read.
    """
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    inv = _invoice_row()
    await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    inv.reporting_source_currency = None  # simulate a pre-0086 lock

    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert changed is False  # unchanged row: still a no-op

    inv.currency = "USD"  # crosses the reporting currency — the heuristic sees it
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert changed is True
    assert inv.reporting_source_currency == "USD"  # and the column is now filled


# ---------------------------------------------------------------------------
# An invoice with no known currency (decisions §473): never the reporting
# currency, never locked, always counted as unconverted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", [None, "", "   ", "US"])
async def test_materialize_never_locks_an_unknown_currency(currency):
    fx = MockFXAdapter({})
    inv = _invoice_row(currency=currency)
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert changed is False
    assert inv.reporting_amount is None
    assert inv.reporting_currency is None
    assert inv.reporting_fx_rate is None


@pytest.mark.asyncio
async def test_materialize_clears_a_made_up_lock_on_an_unknown_currency():
    """A row locked 1:1 before §473 (the blank read as the reporting currency)
    loses that lock, so no rollup trusts it."""
    fx = MockFXAdapter({})
    inv = _invoice_row(currency=None)
    inv.reporting_currency = "USD"
    inv.reporting_amount = Decimal("1000.00")
    inv.reporting_fx_rate = Decimal("1")
    inv.reporting_source_currency = "USD"
    inv.reporting_fx_locked_at = datetime.now(UTC)
    changed = await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=fx)
    assert changed is True
    assert (
        inv.reporting_currency,
        inv.reporting_amount,
        inv.reporting_fx_rate,
        inv.reporting_source_currency,
        inv.reporting_fx_locked_at,
    ) == (None, None, None, None, None)


def test_row_with_unknown_currency_is_unconverted_even_with_a_lock():
    amt, unconverted = reporting_amount_for_row(
        amount=Decimal("100.00"),
        currency=None,
        reporting_currency="USD",
        persisted_reporting_currency="USD",
        persisted_reporting_amount=Decimal("100.00"),
    )
    assert (amt, unconverted) == (Decimal("100.00"), True)


def test_rollup_puts_an_unknown_currency_row_in_its_own_unconverted_group():
    rollup = rollup_to_reporting_currency(
        [
            {"amount": Decimal("100.00"), "currency": "USD"},
            {
                "amount": Decimal("40.00"),
                "currency": None,
                "reporting_amount": Decimal("40.00"),
                "reporting_currency": "USD",
            },
        ],
        reporting_currency="USD",
    )
    assert rollup.unconverted_count == 1
    groups = {e.currency: e for e in rollup.by_currency}
    assert set(groups) == {"USD", None}
    assert groups["USD"].unconverted_count == 0
    assert groups[None].unconverted_count == 1
    assert groups[None].count == 1


def test_rollup_from_grouped_rows_keeps_the_unknown_group_unlabelled():
    rollup = rollup_from_grouped_rows(
        [
            {
                "currency": "USD",
                "original_amount": Decimal("100"),
                "reporting_amount": Decimal("100"),
                "count": 1,
                "unconverted_count": 0,
            },
            {
                "currency": None,
                "original_amount": Decimal("40"),
                "reporting_amount": Decimal("40"),
                "count": 1,
                "unconverted_count": 1,
            },
        ],
        reporting_currency="USD",
    )
    assert rollup.unconverted_count == 1
    assert {e.currency for e in rollup.by_currency} == {"USD", None}


@pytest.mark.asyncio
async def test_unrealized_counts_an_unknown_currency_row_without_valuing_it():
    fx = MockFXAdapter({"mock_rates": {"EUR": "0.92"}})
    result = await compute_unrealized_fx_gain_loss(
        [{"amount": Decimal("500.00"), "currency": None}],
        reporting_currency="USD",
        fx_adapter=fx,
    )
    assert result.unconverted_count == 1
    assert result.by_currency == []
    assert result.total_unrealized_gain_loss == Decimal("0.00")
