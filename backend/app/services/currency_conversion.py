"""Reporting-currency conversion for multi-currency rollups.

The international-PAYMENT path (`services/international_payments.py`) already
locks an FX rate on each Payment row at submission and computes the *realized*
gain/loss when a foreign-currency invoice settles. This module is the reporting
counterpart: it converts arbitrary invoice / payment amounts into the org's one
**reporting (base) currency** so analytics and the dashboard roll multi-currency
volume up into a single comparable number, and it computes the *unrealized*
FX gain/loss on open (approved-but-unpaid) foreign-currency invoices.

Design choices that keep the numbers honest:

  - **Money is exact.** Everything is `Decimal`; amounts quantize to 2 dp with
    `ROUND_HALF_UP`. Never `float`.
  - **Rates are locked, not recomputed.** When an invoice's reporting amount is
    *materialized* onto the row (`materialize_reporting_amount`), the rate used
    is persisted (`Invoice.reporting_fx_rate` + `reporting_source_currency` +
    `reporting_fx_locked_at`). A
    later rollup reads the persisted `reporting_amount` and does NOT re-fetch a
    rate — so a market move doesn't retroactively rewrite last quarter's spend.
  - **Same-currency is a no-op fetch.** rate = 1, no adapter call.
  - **Resolution order for the org reporting currency:**
      1. `Organization.settings.reporting_currency`
      2. `Organization.settings.payments.home_currency` (legacy — the field the
         payment path already reads)
      3. `Organization.settings.invoice_defaults.currency`
      4. `settings.reporting_currency_default` (FEOH_REPORTING_CURRENCY_DEFAULT)

The functions here are pure-ish: the FX adapter is handed in, mirroring
`international_payments`, so the rollup logic is unit-testable against the mock
adapter without HTTP. That includes the SQL side — `invoice_reporting_amount_sql`
and the two `*_select` builders return SQLAlchemy expressions and never touch a
session, so a caller runs them on its own tenant connection and the conversion
rule still has exactly one owner.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import ColumnElement, and_, case, func, not_, or_, select

from app.config import settings
from app.models.virtual_card import VirtualCard
from app.services.fx_adapters import FXAdapter, FXRate
from app.services.international_payments import configured_home_currency

_MONEY_QUANT = Decimal("0.01")
_RATE_QUANT = Decimal("0.00000001")


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


def resolve_reporting_currency(org_settings: dict | None) -> str:
    """Resolve the org's reporting (base) currency from its settings.

    See the module docstring for the resolution order. Always returns an
    uppercase 3-letter code; never raises (a misconfigured org degrades to
    the platform default rather than 500-ing a dashboard).
    """
    s = org_settings or {}
    candidates = [
        s.get("reporting_currency"),
        # Rung 2 goes through `international_payments`, the one owner of this
        # setting — this module was the fourth site to reach into the settings
        # JSON for it, and the four readers normalising it four ways is what
        # let a trailing space route every domestic payment internationally.
        # `configured_home_currency`, not `resolve_home_currency`: the latter
        # substitutes the platform default for an unset value and would
        # short-circuit rungs 3 and 4, which an absent setting must fall
        # through to. See `tests/test_payment_home_currency.py`.
        configured_home_currency(s),
        (s.get("invoice_defaults") or {}).get("currency"),
        settings.reporting_currency_default,
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip().upper()
    return "USD"


@dataclass(frozen=True)
class ConvertedAmount:
    """An amount expressed in the reporting currency plus the FX evidence."""

    amount: Decimal  # in reporting currency
    source_currency: str
    reporting_currency: str
    fx_rate: Decimal  # source -> reporting multiplier
    as_of: datetime


async def convert_amount(
    *,
    amount: Decimal,
    source_currency: str,
    reporting_currency: str,
    fx_adapter: FXAdapter | None,
) -> ConvertedAmount:
    """Convert `amount` from `source_currency` into `reporting_currency`.

    `rate` is the multiplier such that `reporting = source_amount * rate`
    (the FXRate contract: rate is units of target per unit of source). For
    same-currency the rate is 1 and no adapter call is made.

    `fx_adapter` may be `None` when the caller has no usable FX provider: a
    same-currency conversion never needs one, and callers rely on that (see
    `expense_currency.lock_expense_conversion`, which must still lock a
    same-currency line at rate 1 for a tenant whose configured provider names no
    registered adapter). A cross-currency conversion with no adapter raises
    rather than returning an unconverted figure that would read as converted.

    Raises `ValueError` if the adapter returns a non-positive rate — a zero /
    negative rate would zero out or invert the converted figure.
    """
    src = (source_currency or reporting_currency).upper()
    tgt = reporting_currency.upper()
    if src == tgt:
        return ConvertedAmount(
            amount=_quantize_money(amount),
            source_currency=src,
            reporting_currency=tgt,
            fx_rate=Decimal("1"),
            as_of=datetime.now().astimezone(),
        )
    if fx_adapter is None:
        raise ValueError(f"no FX rate source available to convert {src}->{tgt}")
    fx: FXRate = await fx_adapter.get_rate(src, tgt)
    if fx.rate <= 0:
        raise ValueError(f"FX provider returned non-positive rate for {src}->{tgt}: {fx.rate}")
    return ConvertedAmount(
        amount=_quantize_money(amount * fx.rate),
        source_currency=src,
        reporting_currency=tgt,
        fx_rate=fx.rate,
        as_of=fx.as_of,
    )


def _lock_pair_matches(invoice, *, src: str, target_currency: str, rate: Decimal) -> bool:
    """Does the persisted rate still describe THIS row's currency pair?

    Two rungs, and the first is exact:

    1. ``reporting_source_currency`` (migration 0086) records **which** currency
       the rate was fetched for. Comparing it to the invoice's current currency
       catches every currency edit, including the one the shape heuristic below
       cannot see: ``EUR -> GBP`` on a USD-reporting org, where the amount is
       unchanged and the rate is still a plausible cross-currency number.
    2. Only when that column is NULL — a row locked before 0086 — fall back to
       the shape heuristic: a same-currency lock is exactly ``1`` and a
       cross-currency lock is not, so a currency edit that CROSSES the reporting
       currency in either direction contradicts the persisted rate. It still
       cannot see a foreign-to-foreign edit; that is the residual blind spot on
       legacy rows only, and it closes the first time such a row re-materializes.
    """
    persisted_src = getattr(invoice, "reporting_source_currency", None)
    if persisted_src:
        return str(persisted_src).upper() == src
    return (src == target_currency) == (Decimal(str(rate)) == Decimal("1"))


def _lock_is_self_consistent(invoice, *, target_currency: str) -> bool:
    """Does the persisted reporting lock still describe THIS row?

    The lock is four persisted values — ``reporting_amount``,
    ``reporting_fx_rate``, ``reporting_currency`` and
    ``reporting_source_currency`` — derived from two mutable ones, ``amount``
    and ``currency``. An AP user correcting a mis-extracted figure (both are
    editable on ``PATCH /api/invoices/{id}`` right up to approval) moves the
    inputs without touching the outputs, and the rollup then reports the stale
    product as a *converted, trustworthy* number.

    Two conditions have to hold, and both are checkable from the row alone —
    no FX call:

    1. **The rate still describes this row's currency pair** — see
       :func:`_lock_pair_matches`.
    2. **The figure reconciles.** ``quantize(amount * rate)`` must equal the
       persisted ``reporting_amount``; if the amount moved, it won't.
    """
    rate = invoice.reporting_fx_rate
    if rate is None or rate <= 0:
        return False
    src = (invoice.currency or target_currency).upper()
    if not _lock_pair_matches(
        invoice, src=src, target_currency=target_currency, rate=Decimal(str(rate))
    ):
        return False
    expected = _quantize_money(Decimal(str(invoice.amount)) * Decimal(str(rate)))
    return expected == _quantize_money(Decimal(str(invoice.reporting_amount)))


async def materialize_reporting_amount(
    invoice,
    *,
    reporting_currency: str,
    fx_adapter: FXAdapter,
    force: bool = False,
) -> bool:
    """Lock the reporting-currency conversion onto an Invoice row.

    Writes `reporting_currency`, `reporting_amount`, `reporting_fx_rate`,
    `reporting_fx_locked_at` in place (caller flushes/commits). The locked rate
    is what every later rollup reads — we never re-fetch a rate for a row whose
    lock still describes it, so historical conversions are stable.

    Re-materializes (re-fetches a fresh rate) when:
      - `force` is True, OR
      - the row has never been materialized (`reporting_amount` is None), OR
      - the org's reporting currency changed since the last lock, OR
      - the invoice's own **currency** changed at all — including between two
        FOREIGN currencies — which makes the persisted rate describe a pair the
        row no longer has (see `_lock_is_self_consistent`).

    **Re-scales at the ALREADY-LOCKED rate — no FX call — when only the
    invoice's `amount` changed.** That is the honest correction: the liability
    was accrued at the rate in force when the invoice was booked, and a later
    correction of the figure does not retroactively re-price it. It also means
    an amount correction always fixes the reporting number even while the FX
    provider is unreachable.

    Returns True if it (re)materialized or re-scaled, False if it left an
    already-current lock untouched (idempotent no-op).
    """
    tgt = reporting_currency.upper()
    src = (invoice.currency or tgt).upper()

    never_locked = (
        invoice.reporting_amount is None
        or invoice.reporting_currency is None
        or invoice.reporting_currency.upper() != tgt
    )

    if not force and not never_locked:
        if _lock_is_self_consistent(invoice, target_currency=tgt):
            return False
        rate = invoice.reporting_fx_rate
        # Re-scale in place when the persisted rate still describes this row's
        # currency pair — only the amount moved.
        if (
            rate is not None
            and rate > 0
            and _lock_pair_matches(invoice, src=src, target_currency=tgt, rate=Decimal(str(rate)))
        ):
            invoice.reporting_amount = _quantize_money(
                Decimal(str(invoice.amount)) * Decimal(str(rate))
            )
            return True

    converted = await convert_amount(
        amount=invoice.amount,
        source_currency=src,
        reporting_currency=tgt,
        fx_adapter=fx_adapter,
    )
    stored_rate = converted.fx_rate.quantize(_RATE_QUANT)
    invoice.reporting_currency = converted.reporting_currency
    # Derive the stored figure from the STORED (8dp) rate, not the provider's
    # full-precision one, so the persisted triple satisfies
    # `amount * rate == reporting_amount` exactly. An auditor can re-derive it,
    # and `_lock_is_self_consistent` above can use that identity to tell a
    # stale lock from a current one. (Both shipped adapters quantize to 6dp, so
    # this is identical to `converted.amount` today.)
    invoice.reporting_amount = _quantize_money(Decimal(str(invoice.amount)) * stored_rate)
    invoice.reporting_fx_rate = stored_rate
    # Record WHICH currency the rate was fetched for. Without it the row states
    # a rate but not its pair, and a later `EUR -> GBP` correction on a
    # USD-reporting org leaves a stale figure the rollup still calls converted.
    invoice.reporting_source_currency = converted.source_currency
    invoice.reporting_fx_locked_at = converted.as_of
    return True


def reporting_amount_for_row(
    *,
    amount: Decimal,
    currency: str | None,
    reporting_currency: str,
    persisted_reporting_currency: str | None,
    persisted_reporting_amount: Decimal | None,
) -> tuple[Decimal, bool]:
    """Best-effort reporting amount for a single row WITHOUT an FX call.

    Used by aggregate rollups that must stay synchronous over many rows. Prefers
    the persisted (rate-locked) `reporting_amount` when it matches the org's
    current reporting currency; otherwise:
      - same-currency rows convert at 1:1, exact.
      - foreign rows with no usable lock fall back to the raw `amount` and are
        flagged `unconverted=True` so the caller can surface "N rows pending
        conversion" rather than silently mixing currencies.

    Returns `(reporting_amount, unconverted)`.
    """
    tgt = reporting_currency.upper()
    cur = (currency or tgt).upper()

    if (
        persisted_reporting_amount is not None
        and persisted_reporting_currency is not None
        and persisted_reporting_currency.upper() == tgt
    ):
        return _quantize_money(Decimal(str(persisted_reporting_amount))), False

    if cur == tgt:
        return _quantize_money(Decimal(str(amount))), False

    # Foreign row with no usable lock — can't fabricate a rate here.
    return _quantize_money(Decimal(str(amount))), True


def reporting_amount_at_locked_rate(
    *,
    amount: Decimal,
    currency: str | None,
    reporting_currency: str,
    persisted_reporting_currency: str | None,
    persisted_reporting_source_currency: str | None,
    persisted_fx_rate: Decimal | None,
) -> tuple[Decimal, bool]:
    """Express an ARBITRARY amount in the reporting currency at the row's LOCKED rate.

    The sibling of `reporting_amount_for_row`, for the case where the figure
    being converted is **not** the row's whole `amount` — a payable netted
    against applied credit memos, for instance. The persisted
    `reporting_amount` prices `amount`, so it can't just be read back; the
    persisted *rate* is what generalises. Still no FX call and no read-time
    rate: a market move must not retroactively change what a control decided.

    Deliberately **stricter about the lock** than `reporting_amount_for_row`.
    That helper feeds display rollups, which prefer a slightly stale figure to
    a missing one; this one feeds a money CONTROL (the payment CFO-approval
    threshold), so the rate is trusted only when `reporting_source_currency`
    (migration 0086) proves it was fetched for the row's *current* currency. A
    row locked before that column existed, or one whose currency has since been
    corrected, reports `unconverted=True` — which the gate reads as fail-closed,
    never as a licence to compare bare numbers across currencies.

    Returns `(reporting_amount, unconverted)`.
    """
    tgt = (reporting_currency or "USD").strip().upper()
    cur = (currency or tgt).strip().upper()

    if cur == tgt:
        return _quantize_money(Decimal(str(amount))), False

    rate = None if persisted_fx_rate is None else Decimal(str(persisted_fx_rate))
    if (
        rate is not None
        and rate > 0
        and isinstance(persisted_reporting_currency, str)
        and persisted_reporting_currency.strip().upper() == tgt
        and isinstance(persisted_reporting_source_currency, str)
        and persisted_reporting_source_currency.strip().upper() == cur
    ):
        return _quantize_money(Decimal(str(amount)) * rate), False

    # No lock we can prove describes this currency pair.
    return _quantize_money(Decimal(str(amount))), True


@dataclass(frozen=True)
class PaymentReportingAmountSql:
    """SQL expressions for "what did this payment move, in the reporting
    currency?" — plus the predicate that says whether that is knowable at all.

    ``amount`` evaluates to the reporting-currency figure, or SQL ``NULL``
    when none can be established. ``is_expressible`` is the two-valued
    predicate for the same condition, so a caller can bucket the rows it must
    leave OUT of a total instead of quietly adding them at face value.
    """

    amount: ColumnElement
    is_expressible: ColumnElement[bool]
    #: The reporting figure IS ``payment_amount``, in the invoice's own currency
    #: (rung 2, or rung 1 restating it exactly). A caller splitting a payment by
    #: parts stated in the payment's own currency (the 1099's rail breakdown,
    #: decisions §396) may apply them only then; any other figure would need a
    #: rate, and none is fetched on a read.
    is_payment_amount: ColumnElement[bool]


def card_currency_sql(reporting_currency: str) -> ColumnElement[str]:
    """The currency a ``virtual_cards`` row is denominated in, as SQL.

    It denominates two different things. Card figures (``amount_limit`` /
    ``amount_charged``) read it directly; **rebate** figures reach it through
    the join, because ``card_rebates`` carries no currency column of its own —
    a rebate's denomination is only knowable through the card it accrued on.

    It exists because six rollups need it and were spelling it themselves — the
    dashboard KPI, ``GET /api/payments/summary``, ``GET /api/cards/dashboard``,
    ``GET /api/cards/rebates``, the analytics rebate-yield numerator, and the
    billing usage meter. Five of those were bare cross-currency ``SUM``s: a
    quantity in no currency at all, several shipped under a response that
    declared one.

    ``UPPER`` is load-bearing: the column is a free-form ``varchar(3)`` while
    ``resolve_reporting_currency`` always returns uppercase, so an
    un-normalised comparison would exclude every row rather than fail loudly.
    The ``COALESCE`` is **not** — ``virtual_cards.currency`` is ``NOT NULL``
    with a Python-side ``default="USD"``, so there is no unstamped row for it to
    catch. It is kept as defensive symmetry with the money columns beside it
    (and would be the right behaviour if the column were ever made nullable),
    but nothing asserts it, because a test of an unreachable branch passes with
    the branch deleted.

    Callers filter with ``== reporting_currency`` and, where the response can
    carry it, count the ``!=`` rows so a single-currency figure says what it
    left out instead of looking complete.
    """
    return func.upper(func.coalesce(VirtualCard.currency, reporting_currency))


def payment_reporting_amount_sql(
    *,
    reporting_currency: str,
    payment_amount: ColumnElement,
    payment_source_amount: ColumnElement,
    payment_source_currency: ColumnElement,
    invoice_currency: ColumnElement,
) -> PaymentReportingAmountSql:
    """Resolve a ``Payment`` row's outflow into the org's reporting currency.

    **``Payment.amount`` is denominated in the INVOICE's currency, not the
    org's home currency** — ``international_payments.prepare_international_payment``
    sets ``amount=invoice.amount`` ("paid in invoice currency") and puts the
    home-currency debit on ``source_amount``/``source_currency``. Any
    aggregate that sums raw ``Payment.amount`` across a book containing one
    foreign invoice is therefore a silent two-currency mixture.

    Two rungs, most authoritative first:

    1. ``source_amount`` when ``source_currency`` IS the reporting currency —
       the exact home-currency cash outflow, at the rate locked onto the row
       when the payment was submitted. This is the figure that actually left
       the bank.
    2. ``amount`` when the invoice's own currency IS the reporting currency —
       the ordinary domestic case, and the only rung a single-currency tenant
       ever reaches, so its numbers are unchanged.

    Otherwise the figure is **not establishable** and both outputs say so.
    Deliberately no third rung that falls back to face value: unlike a spend
    dashboard (``reporting_amount_for_row``, which does fall back and flags
    ``unconverted``), the consumers of this helper file regulated totals, where
    adding 1 000 EUR to a USD figure as though it were 1 000 USD is a wrong
    number on a filed form. Fetching a rate here is not an option either — a
    rate looked up at read time makes a historical total move under the reader
    (``docs/decisions.md`` §18 on locked-not-recomputed rates).

    Pure: builds SQLAlchemy expressions, touches no session and no clock.
    """
    tgt = (reporting_currency or "USD").strip().upper()
    home_leg = and_(
        payment_source_amount.isnot(None),
        func.upper(func.btrim(func.coalesce(payment_source_currency, ""))) == tgt,
    )
    invoice_leg = func.upper(func.btrim(func.coalesce(invoice_currency, tgt))) == tgt
    return PaymentReportingAmountSql(
        amount=case(
            (home_leg, payment_source_amount),
            (invoice_leg, payment_amount),
            else_=None,
        ),
        is_expressible=or_(home_leg, invoice_leg),
        is_payment_amount=and_(
            invoice_leg,
            or_(~home_leg, payment_source_amount == payment_amount),
        ),
    )


@dataclass(frozen=True)
class InvoiceReportingAmountSql:
    """The three expressions an aggregate needs to roll invoices into one currency.

    ``currency_key`` groups, ``amount`` sums, ``unconverted`` counts what the
    sum could not honestly convert.
    """

    currency_key: ColumnElement
    amount: ColumnElement
    unconverted: ColumnElement


def invoice_reporting_amount_sql(
    *,
    reporting_currency: str,
    amount: ColumnElement,
    currency: ColumnElement,
    persisted_reporting_amount: ColumnElement,
    persisted_reporting_currency: ColumnElement,
) -> InvoiceReportingAmountSql:
    """SQL counterpart of `reporting_amount_for_row` — the SAME rule, expressed
    so Postgres can apply it inside a ``GROUP BY`` instead of the caller
    streaming every invoice into Python to apply it a row at a time.

    The rungs mirror that helper exactly:

    1. the persisted, rate-locked ``reporting_amount`` when it is set AND its
       ``reporting_currency`` is the org's target — a figure fixed at lock
       time, so a market move can't rewrite last quarter's spend;
    2. otherwise the face ``amount`` — exact for a row already in the target
       currency, and a **flagged fallback** for a foreign row with no usable
       lock, which ``unconverted`` counts.

    Unlike `payment_reporting_amount_sql` (which refuses to fall back, because
    its consumers file regulated totals) this one does fall back, because its
    consumers are spend displays where a slightly-mixed total that says so
    beats a hole. Every caller must therefore surface ``unconverted`` — a
    fallback nobody reports is just a wrong number (`docs/decisions.md` §35).

    ``persisted_reporting_currency IS NOT NULL`` is stated explicitly rather
    than left to ``UPPER(NULL) = tgt``: SQL three-valued logic would make the
    lock test NULL for a row carrying an amount but no currency code, and
    ``NOT NULL`` is NULL, so such a row would fall through the ``unconverted``
    CASE and be counted as converted. `reporting_amount_for_row` requires both
    columns, and this must not diverge from it.

    Pure: builds SQLAlchemy expressions, touches no session and no clock.
    """
    tgt = (reporting_currency or "USD").strip().upper()
    currency_key = func.upper(func.coalesce(currency, tgt))
    has_lock = and_(
        persisted_reporting_amount.isnot(None),
        persisted_reporting_currency.isnot(None),
        func.upper(persisted_reporting_currency) == tgt,
    )
    return InvoiceReportingAmountSql(
        currency_key=currency_key,
        amount=case((has_lock, persisted_reporting_amount), else_=amount),
        unconverted=case((and_(not_(has_lock), currency_key != tgt), 1), else_=0),
    )


@dataclass(frozen=True)
class CurrencyBreakdownEntry:
    currency: str
    original_amount: Decimal
    reporting_amount: Decimal
    count: int
    unconverted_count: int


@dataclass(frozen=True)
class ReportingRollup:
    reporting_currency: str
    total_reporting_amount: Decimal
    total_count: int
    unconverted_count: int  # rows that fell back to raw amount (no rate lock)
    by_currency: list[CurrencyBreakdownEntry]


def rollup_to_reporting_currency(
    rows: list[dict],
    *,
    reporting_currency: str,
) -> ReportingRollup:
    """Roll a list of currency-tagged amount rows into one reporting total.

    Each row dict carries:
        {"amount": Decimal, "currency": str | None,
         "reporting_amount": Decimal | None, "reporting_currency": str | None}

    Uses the persisted rate-locked `reporting_amount` where available (so the
    total is stable against market moves); same-currency rows convert 1:1;
    foreign rows without a lock fall through at face value and bump
    `unconverted_count`. The per-currency breakdown lets the UI show the
    original-currency split alongside the unified total.
    """
    tgt = reporting_currency.upper()
    total = Decimal("0")
    unconverted = 0
    buckets: dict[str, dict] = {}

    for r in rows:
        amount = Decimal(str(r.get("amount") or 0))
        currency = (r.get("currency") or tgt).upper()
        rep_amt, is_unconverted = reporting_amount_for_row(
            amount=amount,
            currency=currency,
            reporting_currency=tgt,
            persisted_reporting_currency=r.get("reporting_currency"),
            persisted_reporting_amount=r.get("reporting_amount"),
        )
        total += rep_amt
        if is_unconverted:
            unconverted += 1
        b = buckets.setdefault(
            currency,
            {
                "original_amount": Decimal("0"),
                "reporting_amount": Decimal("0"),
                "count": 0,
                "unconverted_count": 0,
            },
        )
        b["original_amount"] += amount
        b["reporting_amount"] += rep_amt
        b["count"] += 1
        if is_unconverted:
            b["unconverted_count"] += 1

    by_currency = [
        CurrencyBreakdownEntry(
            currency=cur,
            original_amount=_quantize_money(b["original_amount"]),
            reporting_amount=_quantize_money(b["reporting_amount"]),
            count=b["count"],
            unconverted_count=b["unconverted_count"],
        )
        for cur, b in sorted(
            buckets.items(), key=lambda kv: kv[1]["reporting_amount"], reverse=True
        )
    ]

    return ReportingRollup(
        reporting_currency=tgt,
        total_reporting_amount=_quantize_money(total),
        total_count=sum(b.count for b in by_currency),
        unconverted_count=unconverted,
        by_currency=by_currency,
    )


@dataclass(frozen=True)
class VendorSpendEntry:
    """One vendor's total spend, converted into the reporting currency."""

    vendor: str
    amount: Decimal  # in reporting currency
    invoice_count: int
    currencies: list[str]  # every distinct original currency this vendor billed in
    # The `vendors` row this spend is attributed to, when the caller grouped by
    # it. Two DISTINCT vendor records can carry the same `vendor_name` (an
    # unmatched extraction beside a verified master record), so a surface that
    # links its rows must key on the id or it merges two suppliers into one.
    # `None` for the surfaces that group by name alone.
    vendor_id: str | None = None
    # How many of this vendor's invoices went into `amount` at FACE value
    # because no rate lock bridged them (`reporting_amount_for_row`'s flagged
    # fallback). Non-zero means the total is part-converted; a consumer that
    # renders `amount` without saying so is publishing a mixed-currency figure
    # labelled with a single currency code. Defaults to 0 so the many
    # single-currency callers and test doubles constructing this by keyword
    # are unaffected.
    unconverted_count: int = 0


def vendor_rollup_to_reporting_currency(
    rows: list[dict],
    *,
    reporting_currency: str,
) -> list[VendorSpendEntry]:
    """Per-vendor spend, each vendor's amounts converted into one reporting
    currency before being added together — the per-vendor counterpart to
    `rollup_to_reporting_currency`.

    Groups the SAME per-invoice rows `rollup_to_reporting_currency` takes
    (``{"amount", "currency", "reporting_amount", "reporting_currency"}``, plus
    a ``"vendor"`` key) by vendor and rolls each vendor's rows into the
    reporting currency, so a multi-currency vendor's total is a real converted
    figure rather than the naive `SUM(Invoice.amount)` that added USD + EUR +
    GBP as if they were one currency.

    **No production caller streams rows through this any more.** The four
    per-vendor surfaces (CFO concentration tile, its drill-through, the
    `vendor_spend` CSV export, the emailed scheduled report) go through
    `vendor_spend_grouped_select` + `vendor_rollup_from_grouped_rows`, which
    apply the identical rule inside a `GROUP BY`. This row-at-a-time version is
    kept as the readable statement of that rule and as the reference the
    grouped path is equivalence-tested against. `tests/test_analytics_rollup_sql.py`
    holds both that equivalence and an AST scan that fails if a new call site
    under `app/` reintroduces the fold.

    Returns entries sorted by (converted amount DESC, vendor name ASC) — ready
    to feed straight into `analytics.compute_supplier_concentration` or a CSV
    writer.
    """
    by_vendor: dict[tuple[str, str | None], list[dict]] = {}
    for r in rows:
        by_vendor.setdefault((r["vendor"], r.get("vendor_id")), []).append(r)

    entries = []
    for (vendor, vendor_id), vendor_rows in by_vendor.items():
        rollup = rollup_to_reporting_currency(vendor_rows, reporting_currency=reporting_currency)
        entries.append(
            VendorSpendEntry(
                vendor=vendor,
                amount=rollup.total_reporting_amount,
                invoice_count=rollup.total_count,
                currencies=sorted({e.currency for e in rollup.by_currency}),
                unconverted_count=rollup.unconverted_count,
                vendor_id=vendor_id,
            )
        )
    entries.sort(key=_vendor_sort_key)
    return entries


def _vendor_sort_key(entry: VendorSpendEntry) -> tuple[Decimal, str]:
    """Order vendor entries by (converted total DESC, vendor name ASC).

    The name tiebreak is not cosmetic. Sorting on the total alone leaves equal-
    spend vendors in whatever order they were accumulated, so which of two took
    the last slot of a `[:10]` / `[:limit]` cut could differ between two
    identical requests. Same rule the dashboard's top-vendor tile adopted in
    `docs/decisions.md` §103, so the two surfaces break ties alike.

    ``or ""`` because this is a public helper over caller-supplied dicts: every
    real caller filters `vendor_name IS NOT NULL` first, but a `None` slipping
    through would make the tuple uncomparable and raise `TypeError` out of a
    sort the caller never sees.
    """
    return (-entry.amount, entry.vendor or "")


def vendor_rollup_from_grouped_rows(
    groups: list[dict],
    *,
    reporting_currency: str,
) -> list[VendorSpendEntry]:
    """Same result as `vendor_rollup_to_reporting_currency`, but from rows the
    DB has already aggregated per ``(vendor, currency)`` — the per-vendor
    counterpart to `rollup_from_grouped_rows`, and the reason no caller has to
    stream a period's invoices into Python to group them.

    Each group dict carries what `vendor_spend_grouped_select` selects:
        {"vendor": str, "currency": str, "original_amount": Decimal,
         "reporting_amount": Decimal, "count": int, "unconverted_count": int}

    The reduction here is over ``vendors x currencies`` rows, not invoices, so
    it stays flat as the invoice table grows. Equivalence with the row-at-a-time
    path is pinned by `tests/test_analytics_rollup_sql.py`.
    """
    by_vendor: dict[tuple[str, str | None], list[dict]] = {}
    for g in groups:
        vendor_id = g.get("vendor_id")
        key = (g["vendor"] or "", str(vendor_id) if vendor_id is not None else None)
        by_vendor.setdefault(key, []).append(g)

    entries = []
    for (vendor, vendor_id), vendor_groups in by_vendor.items():
        rollup = rollup_from_grouped_rows(vendor_groups, reporting_currency=reporting_currency)
        entries.append(
            VendorSpendEntry(
                vendor=vendor,
                amount=rollup.total_reporting_amount,
                invoice_count=rollup.total_count,
                currencies=sorted({e.currency for e in rollup.by_currency}),
                unconverted_count=rollup.unconverted_count,
                vendor_id=vendor_id,
            )
        )
    entries.sort(key=_vendor_sort_key)
    return entries


def invoice_currency_rollup_select(*, reporting_currency: str):
    """The per-currency `GROUP BY` behind every whole-population invoice rollup.

    Selects exactly the five columns `rollup_from_grouped_rows` consumes, over
    the shared `invoice_reporting_amount_sql` rule. Callers add their own
    population with `.where(...)` and their own entity scope — the grouping and
    the conversion are what must not be respelled per surface.

    Use it instead of selecting four columns of every invoice and folding them
    through `rollup_to_reporting_currency`: that fold is a synchronous per-row
    loop inside an `async def` whose cost grows with the invoice table, and on
    the `/analytics/by-entity` path it ran once per entity.
    """
    from app.models.invoice import Invoice

    rep = invoice_reporting_amount_sql(
        reporting_currency=reporting_currency,
        amount=Invoice.amount,
        currency=Invoice.currency,
        persisted_reporting_amount=Invoice.reporting_amount,
        persisted_reporting_currency=Invoice.reporting_currency,
    )
    return select(
        rep.currency_key.label("currency"),
        func.coalesce(func.sum(Invoice.amount), 0).label("original_amount"),
        func.coalesce(func.sum(rep.amount), 0).label("reporting_amount"),
        func.count().label("count"),
        func.coalesce(func.sum(rep.unconverted), 0).label("unconverted_count"),
    ).group_by(rep.currency_key)


def vendor_currency_rollup_select(*, reporting_currency: str, include_vendor_id: bool = False):
    """The per-``(vendor, currency)`` `GROUP BY` behind every per-vendor rollup.

    The sibling of `invoice_currency_rollup_select`, one grouping level finer.
    It states no population — the caller adds its own `.where(...)` and entity
    scope — because the four AP spend surfaces and the assistant's
    `get_vendor_spend` tool answer deliberately different questions ("spend,
    excluding rejected" vs "committed spend"). What must NOT be respelled per
    surface is the grouping and the conversion, and that is what lives here.

    ``include_vendor_id`` adds `vendors.id` to the key. Two distinct vendor
    records can share a `vendor_name` — an unmatched extraction beside a
    verified master record — so a surface that links its rows must group by the
    id or it silently merges two suppliers; a surface that only labels them
    groups by name, which is what makes its top-N read as one row per supplier
    name. `vendor_rollup_from_grouped_rows` reduces either shape.

    Selects exactly the columns that reducer consumes, plus the key.
    """
    from app.models.invoice import Invoice

    rep = invoice_reporting_amount_sql(
        reporting_currency=reporting_currency,
        amount=Invoice.amount,
        currency=Invoice.currency,
        persisted_reporting_amount=Invoice.reporting_amount,
        persisted_reporting_currency=Invoice.reporting_currency,
    )
    key_columns = [Invoice.vendor_name.label("vendor")]
    if include_vendor_id:
        key_columns.append(Invoice.vendor_id.label("vendor_id"))
    return select(
        *key_columns,
        rep.currency_key.label("currency"),
        func.coalesce(func.sum(Invoice.amount), 0).label("original_amount"),
        func.coalesce(func.sum(rep.amount), 0).label("reporting_amount"),
        func.count().label("count"),
        func.coalesce(func.sum(rep.unconverted), 0).label("unconverted_count"),
    ).group_by(
        *(c.element if hasattr(c, "element") else c for c in key_columns),
        rep.currency_key,
    )


def vendor_spend_grouped_select(
    *,
    reporting_currency: str,
    period_start: date,
    entity_id: uuid.UUID | None = None,
):
    """The ONE query every per-vendor AP spend surface runs.

    Four call sites — the CFO supplier-concentration tile, its drill-through,
    the `vendor_spend` CSV export and the emailed scheduled report — each
    selected five columns of every invoice in the period and folded them in
    Python through `vendor_rollup_to_reporting_currency`: a synchronous
    per-row loop inside an `async def`, growing with the invoice table, the
    shape `docs/decisions.md` §103 removed from the dashboard's own tile.

    This groups in SQL instead, through `invoice_reporting_amount_sql` so the
    conversion rule has one owner and a vendor's total is built by exactly the
    rule the whole-book rollup uses. It returns **every** vendor, deliberately
    un-`LIMIT`ed: `analytics.compute_supplier_concentration` derives its
    denominator from what it is handed and takes its own top-10/top-50 cuts, so
    a pre-sliced list turns `total_spend` into that slice's subtotal and
    inflates every share. Slice for display *after* rolling up.

    Grouping by ``(vendor, currency)`` rather than vendor alone is what keeps
    `VendorSpendEntry.currencies` (the CSV's `currencies` column) and
    `unconverted_count` derivable without a second query; the extra rows are
    vendors x currencies, not invoices.

    Population: the period, a non-empty vendor name, and not `rejected` —
    rejected invoices were never real spend, and every one of the four sites
    must agree on this or a drill-through disagrees with the tile it was
    clicked from.
    """
    from app.models.invoice import Invoice, InvoiceStatus
    from app.tenant import apply_entity_scope

    stmt = vendor_currency_rollup_select(reporting_currency=reporting_currency).where(
        Invoice.invoice_date >= period_start,
        Invoice.vendor_name.isnot(None),
        Invoice.vendor_name != "",
        Invoice.status != InvoiceStatus.rejected.value,
    )
    return apply_entity_scope(stmt, Invoice, entity_id)


def rollup_from_grouped_rows(
    groups: list[dict],
    *,
    reporting_currency: str,
) -> ReportingRollup:
    """Same result as `rollup_to_reporting_currency`, but from rows the DB has
    already aggregated per currency — so an aggregate caller (the dashboard)
    doesn't have to stream every invoice into Python just to sum it.

    Each group dict carries the per-currency SUM/COUNT the DB computed:
        {"currency": str, "original_amount": Decimal, "reporting_amount": Decimal,
         "count": int, "unconverted_count": int}
    where `reporting_amount` already applied the per-row rule (locked
    `reporting_amount` when it matches the target currency, else face `amount`)
    and `unconverted_count` counted the foreign rows that fell back to face
    value — i.e. the CASE expressions in the query mirror
    `reporting_amount_for_row`. Because invoice money columns are 2dp, summing
    then quantizing here matches the row-at-a-time quantize-then-sum above.
    """
    tgt = reporting_currency.upper()
    by_currency = [
        CurrencyBreakdownEntry(
            currency=(g["currency"] or tgt).upper(),
            original_amount=_quantize_money(Decimal(str(g["original_amount"] or 0))),
            reporting_amount=_quantize_money(Decimal(str(g["reporting_amount"] or 0))),
            count=int(g["count"] or 0),
            unconverted_count=int(g["unconverted_count"] or 0),
        )
        for g in groups
    ]
    by_currency.sort(key=lambda e: e.reporting_amount, reverse=True)
    return ReportingRollup(
        reporting_currency=tgt,
        total_reporting_amount=_quantize_money(
            sum((e.reporting_amount for e in by_currency), Decimal("0"))
        ),
        total_count=sum(e.count for e in by_currency),
        unconverted_count=sum(e.unconverted_count for e in by_currency),
        by_currency=by_currency,
    )


@dataclass(frozen=True)
class UnrealizedFXEntry:
    currency: str
    open_original_amount: Decimal
    booked_reporting_amount: Decimal  # at the locked rate when materialized
    current_reporting_amount: Decimal  # at today's rate
    unrealized_gain_loss: Decimal  # booked - current (gain when liability shrank)
    #: Open invoices in this currency carrying no usable reporting lock. They
    #: are EXCLUDED from every figure above — see `compute_unrealized_fx_gain_loss`.
    unconverted_count: int = 0


@dataclass(frozen=True)
class UnrealizedFXResult:
    reporting_currency: str
    total_unrealized_gain_loss: Decimal
    by_currency: list[UnrealizedFXEntry]
    #: Open foreign invoices left out of the exposure entirely because no rate
    #: was ever locked on them (`decisions §35`).
    unconverted_count: int = 0


async def compute_unrealized_fx_gain_loss(
    open_invoices: list[dict],
    *,
    reporting_currency: str,
    fx_adapter: FXAdapter,
) -> UnrealizedFXResult:
    """Unrealized FX gain/loss on open (unsettled) foreign-currency invoices.

    For each foreign-currency open invoice we hold a liability booked at the
    rate locked when its reporting amount was materialized. The mark-to-market
    value is that same original-currency amount converted at *today's* rate. The
    difference is unrealized — it becomes realized only when the invoice is paid
    (which is what `international_payments.realized_fx_gain_loss_for_settlement`
    measures, on the settlement audit row). This
    is the reporting-layer companion: same sign convention (a positive number is
    a gain — the liability in reporting terms shrank since booking).

    Each row dict: {"amount": Decimal, "currency": str,
                    "reporting_amount": Decimal | None,
                    "reporting_currency": str | None}.

    Same-currency invoices carry no FX exposure and are skipped. One FX call per
    distinct foreign currency (today's rate), not per row.

    **A row with no usable reporting lock is EXCLUDED and counted**, never
    booked at face value — `decisions §35`. The booked leg is what the row was
    recorded at in the reporting currency; `reporting_amount_for_row`'s
    face-value fallback returns the amount in the row's OWN currency, which is
    right for a spend rollup (an approximate total with a caveat beats a blank
    panel) and wrong here, because the mark-to-market leg then converts the same
    original amount at today's rate and the arithmetic reports the *conversion
    itself* as a gain or loss. A single EUR 1 000 invoice whose materialization
    failed once produced an $87 unrealized LOSS on an exposure that never moved.
    `invoice_warnings._refresh_reporting_amount` is best-effort and documents
    leaving the fields NULL on an FX blip, and the `/cfo` query applies no
    `IS NOT NULL` filter, so this is a live path rather than a theoretical one.
    """
    tgt = reporting_currency.upper()

    # Group open foreign exposure by currency.
    by_cur: dict[str, dict] = {}
    for inv in open_invoices:
        cur = (inv.get("currency") or tgt).upper()
        if cur == tgt:
            continue
        amount = Decimal(str(inv.get("amount") or 0))
        rep_amt, unconverted = reporting_amount_for_row(
            amount=amount,
            currency=cur,
            reporting_currency=tgt,
            persisted_reporting_currency=inv.get("reporting_currency"),
            persisted_reporting_amount=inv.get("reporting_amount"),
        )
        b = by_cur.setdefault(
            cur,
            {"open_original": Decimal("0"), "booked_reporting": Decimal("0"), "unconverted": 0},
        )
        if unconverted:
            # No booked reporting figure exists for this row, so it can be on
            # NEITHER side of the comparison. Counted so the omission is visible.
            b["unconverted"] += 1
            continue
        b["open_original"] += amount
        b["booked_reporting"] += rep_amt

    entries: list[UnrealizedFXEntry] = []
    total = Decimal("0")
    unconverted_total = 0
    for cur, b in by_cur.items():
        unconverted_total += b["unconverted"]
        if b["open_original"] == 0 and b["booked_reporting"] == 0:
            # Every open row in this currency is unconverted. There is no
            # exposure to mark, and no rate to fetch — but the currency still
            # appears, carrying its count, rather than vanishing from the report.
            entries.append(
                UnrealizedFXEntry(
                    currency=cur,
                    open_original_amount=Decimal("0.00"),
                    booked_reporting_amount=Decimal("0.00"),
                    current_reporting_amount=Decimal("0.00"),
                    unrealized_gain_loss=Decimal("0.00"),
                    unconverted_count=b["unconverted"],
                )
            )
            continue
        fx = await fx_adapter.get_rate(cur, tgt)
        if fx.rate <= 0:
            raise ValueError(f"FX provider returned non-positive rate for {cur}->{tgt}: {fx.rate}")
        current_reporting = _quantize_money(b["open_original"] * fx.rate)
        booked = _quantize_money(b["booked_reporting"])
        gain_loss = _quantize_money(booked - current_reporting)
        total += gain_loss
        entries.append(
            UnrealizedFXEntry(
                currency=cur,
                open_original_amount=_quantize_money(b["open_original"]),
                booked_reporting_amount=booked,
                current_reporting_amount=current_reporting,
                unrealized_gain_loss=gain_loss,
                unconverted_count=b["unconverted"],
            )
        )

    entries.sort(key=lambda e: abs(e.unrealized_gain_loss), reverse=True)
    return UnrealizedFXResult(
        reporting_currency=tgt,
        total_unrealized_gain_loss=_quantize_money(total),
        by_currency=entries,
        unconverted_count=unconverted_total,
    )
