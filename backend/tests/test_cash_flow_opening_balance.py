"""Opening-balance resolution + provenance (`services.cashflow.resolve_opening_balance`).

Pure / in-process — no DB, no network (the mock payment adapter answers
`get_balance` deterministically).

The load-bearing case here is the **currency guard**: every outflow subtracted
from the opening balance is denominated in the org's reporting currency, so a
provider funding account in a *different* currency must be refused rather than
seeded into the curve — otherwise the running balance is silently a mixture of
two currencies and every figure priced off it (plan proposals, shortfall
alerts) is wrong by the exchange rate. The refusal is visible
(`provider_skipped="currency_mismatch"`) so the fallback can't be mistaken for
"no bank is connected".

The adapter-level `get_balance` capability and the persisted-threshold helpers
are covered in `test_cashflow_balance.py`; the copilot tools' use of this
resolver is covered in `test_cash_flow_copilot.py`.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.cashflow import resolve_opening_balance

# The mock payment adapter's deterministic funding-account balance.
_MOCK_BALANCE = Decimal("250000.00")


def _payments(**overrides) -> dict:
    return {"payments": {"provider": "mock", **overrides}}


# ---------------------------------------------------------------------------
# The chain, in order
# ---------------------------------------------------------------------------


async def test_explicit_wins_over_everything():
    balance = await resolve_opening_balance(
        org_id=None,
        org_settings={
            **_payments(),
            "cashflow": {"opening_balance": "999.00"},
        },
        reporting_currency="USD",
        explicit_opening=Decimal("42.00"),
    )
    assert balance.amount == Decimal("42.00")
    assert balance.source == "explicit"
    assert balance.currency == "USD"
    assert balance.provider is None
    assert balance.provider_skipped is None


async def test_provider_balance_used_when_currency_matches():
    balance = await resolve_opening_balance(
        org_id=None,
        org_settings={**_payments(), "cashflow": {"opening_balance": "999.00"}},
        reporting_currency="USD",
        explicit_opening=None,
    )
    assert balance.amount == _MOCK_BALANCE
    assert balance.source == "provider"
    assert balance.provider == "mock"
    # Opaque account label only — never an account number.
    assert balance.account_ref == "mock-operating"
    assert balance.provider_skipped is None


async def test_falls_through_to_settings_when_no_provider_configured():
    balance = await resolve_opening_balance(
        org_id=None,
        org_settings={"cashflow": {"opening_balance": "1500.25"}},
        reporting_currency="USD",
        explicit_opening=None,
    )
    assert balance.amount == Decimal("1500.25")
    assert balance.source == "settings"
    assert balance.provider_skipped is None


async def test_falls_through_to_zero_when_nothing_configured():
    balance = await resolve_opening_balance(
        org_id=None, org_settings=None, reporting_currency="USD", explicit_opening=None
    )
    assert balance.amount == Decimal("0")
    assert balance.source == "none"
    assert balance.currency == "USD"


async def test_use_provider_false_skips_the_bank_call():
    """The dashboard's `seed_balance=false` equivalent — no provider hit at all,
    and no `currency_mismatch` claim either (we never asked)."""
    balance = await resolve_opening_balance(
        org_id=None,
        org_settings={**_payments(), "cashflow": {"opening_balance": "10.00"}},
        reporting_currency="USD",
        explicit_opening=None,
        use_provider=False,
    )
    assert balance.source == "settings"
    assert balance.amount == Decimal("10.00")
    assert balance.provider_skipped is None


# ---------------------------------------------------------------------------
# The currency guard
# ---------------------------------------------------------------------------


async def test_provider_balance_in_another_currency_is_refused():
    """A EUR funding account must NOT seed a USD-reported curve — the outflows
    subtracted from it are USD, so the running balance would be a silent
    two-currency mixture."""
    balance = await resolve_opening_balance(
        org_id=None,
        org_settings={
            **_payments(balance="9999.99", balance_currency="EUR"),
            "cashflow": {"opening_balance": "1500.25"},
        },
        reporting_currency="USD",
        explicit_opening=None,
    )
    assert balance.amount == Decimal("1500.25")
    assert balance.source == "settings"
    assert balance.currency == "USD"
    # The refusal is visible — otherwise this is indistinguishable from an org
    # with no bank connected at all.
    assert balance.provider_skipped == "currency_mismatch"


async def test_currency_mismatch_falls_all_the_way_to_zero_and_still_reports_it():
    balance = await resolve_opening_balance(
        org_id=None,
        org_settings=_payments(balance="9999.99", balance_currency="GBP"),
        reporting_currency="USD",
        explicit_opening=None,
    )
    assert balance.amount == Decimal("0")
    assert balance.source == "none"
    assert balance.provider_skipped == "currency_mismatch"


async def test_currency_comparison_is_case_and_whitespace_insensitive():
    """`usd` from an adapter and `USD` from org settings are the same currency —
    a formatting difference must not look like a mismatch."""
    balance = await resolve_opening_balance(
        org_id=None,
        org_settings=_payments(balance="777.00", balance_currency=" usd "),
        reporting_currency="usd",
        explicit_opening=None,
    )
    assert balance.source == "provider"
    assert balance.amount == Decimal("777.00")
    assert balance.currency == "USD"


async def test_blank_provider_currency_fails_closed():
    """An adapter that reports no currency is not evidence of a match — refuse
    rather than assume it is the reporting currency."""
    balance = await resolve_opening_balance(
        org_id=None,
        org_settings=_payments(balance="500.00", balance_currency=""),
        reporting_currency="USD",
        explicit_opening=None,
    )
    assert balance.source == "none"
    assert balance.provider_skipped == "currency_mismatch"


# ---------------------------------------------------------------------------
# Robustness — a corrupt settings blob degrades, never raises
# ---------------------------------------------------------------------------


async def test_malformed_persisted_balance_degrades_to_zero():
    balance = await resolve_opening_balance(
        org_id=None,
        org_settings={"cashflow": {"opening_balance": "not-a-number"}},
        reporting_currency="USD",
        explicit_opening=None,
    )
    assert balance.amount == Decimal("0")
    assert balance.source == "none"


async def test_unavailable_provider_falls_through_without_claiming_a_mismatch():
    balance = await resolve_opening_balance(
        org_id=None,
        org_settings={
            **_payments(balance_available=False),
            "cashflow": {"opening_balance": "88.00"},
        },
        reporting_currency="USD",
        explicit_opening=None,
    )
    assert balance.source == "settings"
    assert balance.amount == Decimal("88.00")
    assert balance.provider_skipped is None
