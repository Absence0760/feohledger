"""International payments orchestration.

Glues together the FX adapter, corridor selector, and banking
validators so `execute_payment_run` (and the explicit
`/api/payments` endpoints) can submit a cross-currency or
cross-border payment without re-implementing the bookkeeping every
time.

Two responsibilities:

  1. `prepare_international_payment` — given an invoice + vendor +
     org FX config, pick the corridor, lock the FX rate, populate
     the Payment row's source/target/fx_* fields. Returns a Payment
     instance (uncommitted — caller flushes).

  2. `compute_fx_gain_loss` — when a foreign-currency invoice
     settles, the rate has usually moved since the invoice booked.
     The difference between the booked accrual and the cash paid
     out is a realized FX gain/loss; finance teams report on it.

Both functions are pure-ish (no DB calls inside them); the caller
hands the rate adapter in. That keeps the orchestration testable
without spinning up an httpx client.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.models.payment import Payment
from app.services.fx_adapters import FXAdapter, FXRate
from app.services.payment_corridor import CorridorChoice, pick_corridor
from app.services.payment_methods import is_international_payment_method
from app.utils.banking import (
    country_from_iban,
    validate_iban,
    validate_swift_bic,
)


class InternationalPaymentError(ValueError):
    """Raised when a payment can't be prepared — bad bank fields,
    unsupported corridor, etc. The orchestrator surfaces this; the
    caller turns it into an HTTPException with the right status."""


@dataclass(frozen=True)
class PreparedPayment:
    """A Payment row plus the corridor and FX evidence it was built
    from. We return both so callers (and tests) can assert on the
    decision the orchestrator made without re-deriving it."""

    payment: Payment
    corridor: CorridorChoice
    fx_rate: FXRate | None


_MONEY_QUANT = Decimal("0.01")

#: Last-resort home currency when an org has never set one. Matches the value
#: every prior call site defaulted to independently.
DEFAULT_HOME_CURRENCY = "USD"


def normalize_currency_code(value: str | None) -> str | None:
    """A currency code as the rest of the system compares it, or ``None``.

    Trimmed and upper-cased; blank / non-string input yields ``None`` so the
    caller can fall back deliberately rather than propagate a whitespace-only
    "code" that equals nothing.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip().upper()
    return trimmed or None


def configured_home_currency(org_settings: dict | None) -> str | None:
    """The home-currency SETTING as written, normalised — or ``None``.

    ``None`` means the tenant has not usably set one (absent, blank, or the
    surrounding JSON is not the shape it should be), which is a *different*
    answer from "their home currency is USD". `resolve_home_currency` collapses
    the two because the payment path has to pick something; the
    reporting-currency chain in `currency_conversion` must not, because
    `settings.payments.home_currency` is only its SECOND rung and an absent
    value has to fall through to the next one rather than short-circuit it.

    This is the only place in `app/` that reads the raw setting out of the
    settings JSON — see `tests/test_payment_home_currency.py`.
    """
    if not isinstance(org_settings, dict):
        return None
    payments = org_settings.get("payments")
    if not isinstance(payments, dict):
        return None
    return normalize_currency_code(payments.get("home_currency"))


def resolve_home_currency(org_settings: dict | None) -> str:
    """The org's HOME currency — ``settings.payments.home_currency``.

    The denomination of `Payment.source_amount`, of every money threshold in
    `settings.compliance`, and of the comparison that decides whether an
    invoice even needs an FX leg. **One owner, because four call sites used to
    normalise it four ways.** `compliance._home_currency` stripped and
    upper-cased; `currency_conversion.resolve_reporting_currency` stripped and
    upper-cased; `prepare_international_payment` only upper-cased what it was
    handed; and `api/payments._execute_single_payment` — the site that decides
    the FX leg — only upper-cased too.

    A trailing space was therefore enough to route **every domestic payment**
    through `international_wire`: `"USD "` != `"USD"`, so the invoice's currency
    looked foreign, `prepare_international_payment` picked a cross-border
    corridor, an FX rate was locked onto the row, and the KYC gate then read a
    different (stripped) value for the same setting. Nothing validates the
    field on write, and the tenant's own settings JSON is where it comes from.

    Never raises and never returns blank: a missing or whitespace-only setting
    degrades to ``DEFAULT_HOME_CURRENCY`` rather than producing a code that
    compares equal to nothing.
    """
    return configured_home_currency(org_settings) or DEFAULT_HOME_CURRENCY


def _quantize_money(value: Decimal) -> Decimal:
    """Round to currency precision (2 dp, banker's rounding off in
    favor of `ROUND_HALF_UP` — matches what auditors expect)."""
    return value.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


async def prepare_international_payment(
    *,
    invoice,
    vendor,
    org_home_currency: str,
    fx_adapter: FXAdapter,
    invoice_id: uuid.UUID | None = None,
    correlation_id: uuid.UUID | None = None,
    requested_method: str | None = None,
    amount: Decimal | None = None,
) -> PreparedPayment:
    """Build a Payment row for a (potentially) international invoice.

    ``amount`` is what the payment actually moves, in the invoice's currency —
    the booked ``Payment.amount``, which is net of applied credit memos and any
    accepted early-payment discount. It defaults to ``invoice.amount`` for a
    caller pricing the whole invoice. Converting the full invoice amount for a
    payment that moves less would lock a home-currency debit (``source_amount``)
    larger than the money sent.

    Picks the corridor, fetches and locks the FX rate, applies the
    rate to compute the source-side outflow in the org's home
    currency, and validates the vendor's bank fields for the chosen
    corridor.

    `invoice` and `vendor` are duck-typed (SimpleNamespace in tests,
    SQLAlchemy ORM in production). We read:

      - `invoice.id`, `invoice.amount`, `invoice.currency`,
        `invoice.correlation_id`, `invoice.organization_id`
      - `vendor.bank_details` (JSONB dict)
      - `vendor.address_country` (optional; falls back to IBAN
        country, then `None`)

    Raises `InternationalPaymentError` on any structural problem.
    """
    # One normaliser, so a trailing space in the org's setting can no longer
    # make the home currency compare unequal to itself here while comparing
    # equal in the compliance gate. See `resolve_home_currency`.
    source_currency = normalize_currency_code(org_home_currency) or DEFAULT_HOME_CURRENCY
    target_currency = normalize_currency_code(invoice.currency) or source_currency

    bank = vendor.bank_details or {}
    iban = bank.get("iban") or ""
    swift = bank.get("swift_bic") or bank.get("swift") or bank.get("bic") or ""
    target_country = (
        bank.get("country") or getattr(vendor, "address_country", None) or country_from_iban(iban)
    )
    if isinstance(target_country, str):
        target_country = target_country.upper()

    corridor = pick_corridor(
        source_currency=source_currency,
        target_currency=target_currency,
        target_country=target_country,
        requested_method=requested_method,
    )

    # Structural validation for the corridor's required fields. We
    # check what's needed BEFORE doing the rate lookup — a network
    # call to OXR is wasted if we can't submit anyway.
    if corridor.requires_iban:
        if not validate_iban(iban):
            raise InternationalPaymentError(
                f"corridor '{corridor.method}' requires a valid IBAN; "
                f"vendor's bank_details.iban is missing or malformed"
            )
    if corridor.requires_swift:
        if not validate_swift_bic(swift):
            raise InternationalPaymentError(
                f"corridor '{corridor.method}' requires a valid SWIFT/BIC; "
                f"vendor's bank_details.swift_bic is missing or malformed"
            )

    # FX lookup. Skipped when the corridor doesn't need a conversion
    # (domestic / same-currency).
    fx_rate: FXRate | None = None
    pay_amount = invoice.amount if amount is None else amount
    source_amount = pay_amount
    fx_rate_decimal: Decimal | None = None
    fx_locked_at: datetime | None = None

    if corridor.requires_fx:
        fx_rate = await fx_adapter.get_rate(source_currency, target_currency)
        if fx_rate.rate <= 0:
            raise InternationalPaymentError(
                f"FX provider returned non-positive rate for "
                f"{source_currency}→{target_currency}: {fx_rate.rate}"
            )
        # invoice.amount is in target_currency; source_amount = target / rate
        # e.g. invoice = 1000 EUR, USD→EUR = 0.92 → source = 1000 / 0.92 USD
        source_amount = _quantize_money(pay_amount / fx_rate.rate)
        fx_rate_decimal = fx_rate.rate
        fx_locked_at = fx_rate.as_of

    payment = Payment(
        invoice_id=invoice_id or invoice.id,
        correlation_id=correlation_id or invoice.correlation_id,
        # Payment lands in the same entity as the invoice it settles (P2).
        entity_id=invoice.entity_id,
        amount=pay_amount,  # paid in invoice currency
        method=corridor.method,
        status="pending",
        source_currency=source_currency,
        source_amount=source_amount,
        fx_rate=fx_rate_decimal,
        fx_locked_at=fx_locked_at,
        corridor=corridor.method,
        target_country=target_country,
    )

    return PreparedPayment(payment=payment, corridor=corridor, fx_rate=fx_rate)


def compute_fx_gain_loss(
    *,
    invoice_amount: Decimal,
    invoice_currency: str,
    paid_source_amount: Decimal,
    paid_source_currency: str,
    fx_rate_at_invoice: Decimal,
) -> Decimal:
    """Compute the realized FX gain/loss for a foreign-currency
    invoice that has now been paid.

    Booking flow:
      - At invoice approval we accrue a liability of
        `invoice_amount / fx_rate_at_invoice` in the home currency.
      - At payment we actually move `paid_source_amount` (in the
        home currency).
      - Gain (positive) = paid less than accrued.
        Loss (negative) = paid more than accrued.

    Same currency → 0 regardless of rate inputs (defensive against
    a caller passing a stale rate by mistake).

    NOTE the rate convention: `fx_rate_at_invoice` is INVOICE-currency units
    per one home-currency unit, because the accrual divides by it. That is the
    inverse of `Invoice.reporting_fx_rate`, which `currency_conversion`
    persists as reporting units per one invoice-currency unit and MULTIPLIES
    by. Callers holding a persisted row want
    `realized_fx_gain_loss_for_settlement` below, which speaks the persisted
    convention directly instead of inviting a silent inversion.
    """
    if invoice_currency.upper() == paid_source_currency.upper():
        return Decimal("0.00")
    if fx_rate_at_invoice <= 0:
        raise ValueError("fx_rate_at_invoice must be positive")
    accrued = _quantize_money(invoice_amount / fx_rate_at_invoice)
    return _gain_loss(accrued=accrued, paid_home_amount=paid_source_amount)


def _gain_loss(*, accrued: Decimal, paid_home_amount: Decimal) -> Decimal:
    """Positive when we paid LESS than we accrued → a gain.

    Sign convention matches GAAP / IFRS, and matches
    `currency_conversion.compute_unrealized_fx_gain_loss` so the realized and
    unrealized halves of the same exposure never read with opposite signs.
    Shared so the two entry points below can't drift.
    """
    return _quantize_money(accrued - _quantize_money(paid_home_amount))


def realized_fx_gain_loss_for_settlement(
    *,
    invoice_amount: Decimal,
    invoice_currency: str | None,
    reporting_currency: str | None,
    reporting_fx_rate: Decimal | None,
    paid_source_amount: Decimal | None,
    paid_source_currency: str | None,
) -> Decimal | None:
    """Realized FX gain/loss for one settled payment, from persisted row values.

    The production entry point, and the reason this module's realized half is
    no longer documented behaviour over an unwired function. It takes exactly
    what the rows already carry, so it needs no migration and no FX call:

    * `Invoice.reporting_fx_rate` — the rate locked when the invoice's
      reporting amount was materialized, i.e. the rate the liability was
      ACCRUED at. It multiplies (reporting units per invoice-currency unit),
      which is why this function exists rather than letting each caller invert
      it into `compute_fx_gain_loss`'s dividing convention.
    * `Payment.source_amount` — what actually moved in the home currency.

    **The two must be denominated in the same currency, and that is not
    guaranteed.** `Invoice.reporting_currency` comes from
    `currency_conversion.resolve_reporting_currency`, which prefers
    `settings.reporting_currency`; `Payment.source_currency` comes strictly
    from `settings.payments.home_currency`. Both are independently
    admin-settable, so an org reporting in USD while funding vendor payments
    from a GBP account has an accrual in USD and an outflow in GBP. Subtracting
    those would produce a confidently wrong number written onto an immutable
    audit row — so the currencies are compared, and a mismatch returns `None`.

    Returns `None` — not zero — whenever the figure is not meaningful: a
    domestic payment with no FX leg, a missing accrual rate, a same-currency
    settlement, or an accrual and an outflow denominated differently. Zero
    would assert "we measured, and there was no gain or loss", which is a
    different claim from "there is nothing to measure".
    """
    if paid_source_amount is None or reporting_fx_rate is None:
        return None
    if reporting_fx_rate <= 0:
        return None
    if invoice_currency and paid_source_currency:
        if invoice_currency.upper() == paid_source_currency.upper():
            return None
    # The accrual is denominated in the invoice's reporting currency; the
    # outflow in the payment's source currency. Comparing them requires that
    # they be the same currency — we will not bridge them with a rate here,
    # because inventing one is exactly the kind of silent wrongness this
    # function exists to avoid.
    if not reporting_currency or not paid_source_currency:
        return None
    if reporting_currency.strip().upper() != paid_source_currency.strip().upper():
        return None
    accrued = _quantize_money(invoice_amount * reporting_fx_rate)
    return _gain_loss(accrued=accrued, paid_home_amount=paid_source_amount)


def is_international_payment(payment: Payment) -> bool:
    """Row-level predicate: does this Payment have an international leg?

    Used by ``api/payments`` to decide whether to run
    ``prepare_international_payment`` (i.e. whether an FX rate gets locked onto
    the row) before handing off to the processor.

    A payment is international iff:
      - It has an explicit FX rate locked (cross-currency), OR
      - Its corridor OR its method is one of the international rails.

    Both columns are checked because they are written by different paths and
    can legitimately disagree: ``prepare_international_payment`` stamps both,
    but a row created by the standalone ``POST /api/payments`` path (or any row
    predating the corridor column) carries only ``method``. Reading just one
    would miss the other's evidence. The rail set itself lives in
    ``services/payment_methods`` — the same one the corridor selector and the
    KYC gate read, so all three cannot drift.
    """
    if payment.fx_rate is not None and payment.fx_rate > 0:
        return True
    return is_international_payment_method(payment.corridor) or is_international_payment_method(
        payment.method
    )
