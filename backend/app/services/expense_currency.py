"""Expense-report currency conversion — locked FX, never a naive cross-currency sum.

An employee on one trip legitimately incurs expenses in several currencies, so a
report is **not** constrained to a single currency. Instead every line is
converted, and the conversion is **locked** — exactly the convention the invoice
path already uses (``services/currency_conversion.materialize_reporting_amount``
→ ``invoices.reporting_*``) and the payment path uses
(``services/international_payments`` → ``payments.fx_rate`` / ``fx_locked_at``).
A locked rate is persisted on the row and read back verbatim; it is **never**
re-fetched at read time, so a report's total cannot drift with the market
between submission and approval.

Two layers, deliberately named apart because their target currencies differ:

===========================  ===============================  ==========================
Layer                        Columns                          Target currency
===========================  ===============================  ==========================
Line → report                ``expenses.converted_*``         ``ExpenseReport.currency``
Report → org reporting base  ``expense_reports.reporting_*``  ``resolve_reporting_currency``
===========================  ===============================  ==========================

The first makes ``ExpenseReport.total_amount`` a well-defined figure in the
report's own currency. The second makes the CFO-threshold gate non-manipulable:
the threshold (``settings.expense_approval.cfo_threshold``) is a bare number
denominated in the org's reporting currency, so a report filed in a foreign
currency has to be expressed in that currency before it can be compared —
otherwise a 4 900 EUR report would slip under a 5 000 USD threshold.

Fail-closed, both ways:

* A foreign-currency line that cannot be converted is **excluded** from the
  total and counted in ``unconverted_count``; the report cannot be submitted
  while any such line is attached. It is never silently summed at face value
  (that was the bug — issue #157).
* A report whose reporting-currency figure is unavailable is treated by the gate
  as *over* the threshold (CFO sign-off required), never under it.
* A policy threshold that cannot be compared against the expense's amount (the
  two are in different currencies and nothing on the row bridges them) is
  treated by ``services/expense_policy`` as *breached* — a required receipt is
  demanded rather than waived. See ``expense_amount_in_currency`` below.

Money is ``Decimal`` end to end, quantized to 2 dp ``ROUND_HALF_UP``; rates to
8 dp. Never ``float``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.services.currency_conversion import convert_amount
from app.services.fx_adapters import FXAdapter

_MONEY_QUANT = Decimal("0.01")
_RATE_QUANT = Decimal("0.00000001")


class ExpenseConversionError(ValueError):
    """A line could not be converted into the report's currency.

    Carries no PII — only the two currency codes — so the router can put the
    message straight into the HTTP error body, and the codes themselves into
    the coded refusal's params (``expense_fx_rate_unavailable``).
    """

    def __init__(self, message: str, *, source_currency: str, target_currency: str) -> None:
        super().__init__(message)
        self.source_currency = source_currency
        self.target_currency = target_currency


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


def normalize_currency(code: str | None, *, default: str = "USD") -> str:
    """Uppercase ISO code, degrading to ``default`` for empty/blank input."""
    if isinstance(code, str) and code.strip():
        return code.strip().upper()
    return default.upper()


def conversion_requires_rate_source(
    *,
    source_currency: str | None,
    target_currency: str | None,
    fx_adapter: FXAdapter | None,
) -> bool:
    """``True`` when this conversion genuinely needs an FX provider we don't have.

    The *pair* is checked before the provider, deliberately, and this one helper
    is shared by BOTH locking layers so they cannot drift:

    * a same-currency conversion needs no rate at all — ``convert_amount``
      short-circuits at 1 before touching a provider — so an absent adapter is
      irrelevant to it;
    * a cross-currency conversion with no adapter is the fail-closed case each
      caller already has a posture for (refuse the attach at line level; leave
      the reporting figure NULL at report level, which makes the CFO gate
      demand sign-off).

    Asking for the provider first is what made one bad ``settings.fx.provider``
    value break work that has no FX question in it: at line level it 422'd an
    entirely domestic line on a same-currency report (fixed round 15), and at
    report level it left ``reporting_amount`` NULL on a report already filed in
    the org's own reporting currency — a hole in the stored snapshot on a
    tenant that never needed a rate.
    """
    tgt = normalize_currency(target_currency)
    return fx_adapter is None and normalize_currency(source_currency, default=tgt) != tgt


# ---------------------------------------------------------------------------
# Layer 1 — expense line → report currency
# ---------------------------------------------------------------------------


async def lock_expense_conversion(
    expense,
    *,
    target_currency: str,
    fx_adapter: FXAdapter | None,
) -> None:
    """Lock ``expense.amount`` into ``target_currency`` onto the row.

    Writes ``converted_currency`` / ``converted_amount`` / ``converted_fx_rate``
    / ``converted_fx_locked_at`` in place (the caller flushes). Same-currency
    locks at rate ``1`` with **no** adapter call — which is why ``fx_adapter``
    is allowed to be ``None``: a tenant with no usable rate source must still be
    able to build a single-currency report. ``None`` with a currency pair that
    genuinely needs converting raises ``ExpenseConversionError`` like any other
    unavailable rate, so the fail-closed direction is unchanged.

    Called only from write paths that change what needs converting (create with
    a report, amount/currency edit, attach, report-currency change) — never from
    a read path, so the stored figure is stable for the life of the report.

    Raises ``ExpenseConversionError`` when the provider has no usable rate; the
    caller turns that into a 422 rather than attaching an unconvertible line.
    """
    tgt = normalize_currency(target_currency)
    src = normalize_currency(expense.currency, default=tgt)
    try:
        converted = await convert_amount(
            amount=Decimal(str(expense.amount or 0)),
            source_currency=src,
            reporting_currency=tgt,
            fx_adapter=fx_adapter,
        )
    except Exception as exc:  # provider outage, unknown currency, zero rate
        raise ExpenseConversionError(
            f"No FX rate available to convert {src} into the report currency {tgt}.",
            source_currency=src,
            target_currency=tgt,
        ) from exc

    expense.converted_currency = converted.reporting_currency
    expense.converted_amount = converted.amount
    expense.converted_fx_rate = converted.fx_rate.quantize(_RATE_QUANT)
    expense.converted_fx_locked_at = converted.as_of


def clear_expense_conversion(expense) -> None:
    """Drop the lock — used when an expense is detached from its report (the
    conversion is meaningless without a target currency to be converted *into*)."""
    expense.converted_currency = None
    expense.converted_amount = None
    expense.converted_fx_rate = None
    expense.converted_fx_locked_at = None


def line_amount_in_report_currency(
    *,
    amount: Decimal | None,
    currency: str | None,
    converted_amount: Decimal | None,
    converted_currency: str | None,
    report_currency: str,
) -> Decimal | None:
    """The line's contribution to its report's total, or ``None`` if unknown.

    Precedence mirrors ``currency_conversion.reporting_amount_for_row``:

    1. a lock into the report's current currency → the locked figure (stable);
    2. a line already denominated in the report currency → its face amount;
    3. otherwise ``None`` — a foreign line with no usable lock. The caller must
       treat that as *unconverted*, never as face value.
    """
    tgt = normalize_currency(report_currency)
    if converted_amount is not None and normalize_currency(converted_currency, default="") == tgt:
        return _quantize_money(Decimal(str(converted_amount)))
    if normalize_currency(currency, default=tgt) == tgt:
        return _quantize_money(Decimal(str(amount or 0)))
    return None


def expense_amount_in_currency(expense, *, target_currency: str) -> Decimal | None:
    """The expense's ``amount`` expressed in ``target_currency``, or ``None``.

    An ORM-row-shaped wrapper over ``line_amount_in_report_currency`` for
    callers that hold an ``Expense`` rather than a row dict — same precedence
    (locked conversion into the target → face amount when already denominated
    in the target → ``None``), and the same rule that ``None`` means *unknown*,
    never *zero* and never *face value*.

    It performs **no** FX call, deliberately: the only figures it will use are
    ones a write path already locked. Its second consumer is the policy engine
    (``services/expense_policy``), where a threshold is a standing rule rather
    than a transaction and so has no moment at which a rate could honestly be
    locked; ``None`` there means "this comparison cannot be made", which the
    engine resolves conservatively rather than by inventing a rate.
    """
    return line_amount_in_report_currency(
        amount=getattr(expense, "amount", None),
        currency=getattr(expense, "currency", None),
        converted_amount=getattr(expense, "converted_amount", None),
        converted_currency=getattr(expense, "converted_currency", None),
        report_currency=target_currency,
    )


@dataclass(frozen=True)
class CurrencyBucket:
    currency: str
    original_amount: Decimal
    report_amount: Decimal
    count: int
    unconverted_count: int


@dataclass(frozen=True)
class ReportRollup:
    """A report's expenses collapsed into its own currency."""

    currency: str
    total: Decimal
    count: int
    unconverted_count: int
    unconverted_ids: tuple[str, ...]
    by_currency: tuple[CurrencyBucket, ...]


def rollup_report_lines(rows: list[dict], *, report_currency: str) -> ReportRollup:
    """Roll a report's expense lines into one total in the report's currency.

    Each row dict carries ``{"id", "amount", "currency", "converted_amount",
    "converted_currency"}``. No FX call happens here — this runs on read paths
    and on every total recompute, and re-fetching a rate would let the market
    move a report's total.

    Unconvertible lines are excluded from ``total`` and reported in
    ``unconverted_count`` / ``unconverted_ids`` so the caller can block
    submission instead of shipping an understated figure to the CFO gate.
    """
    tgt = normalize_currency(report_currency)
    total = Decimal("0")
    unconverted = 0
    unconverted_ids: list[str] = []
    buckets: dict[str, dict] = {}

    for row in rows:
        amount = Decimal(str(row.get("amount") or 0))
        cur = normalize_currency(row.get("currency"), default=tgt)
        contribution = line_amount_in_report_currency(
            amount=amount,
            currency=cur,
            converted_amount=row.get("converted_amount"),
            converted_currency=row.get("converted_currency"),
            report_currency=tgt,
        )
        bucket = buckets.setdefault(
            cur,
            {
                "original_amount": Decimal("0"),
                "report_amount": Decimal("0"),
                "count": 0,
                "unconverted_count": 0,
            },
        )
        bucket["original_amount"] += amount
        bucket["count"] += 1
        if contribution is None:
            # Count it whether or not the caller supplied an id — the COUNT is
            # what gates submission, so it must never undercount.
            unconverted += 1
            bucket["unconverted_count"] += 1
            rid = row.get("id")
            if rid is not None:
                unconverted_ids.append(str(rid))
        else:
            total += contribution
            bucket["report_amount"] += contribution

    by_currency = tuple(
        CurrencyBucket(
            currency=cur,
            original_amount=_quantize_money(b["original_amount"]),
            report_amount=_quantize_money(b["report_amount"]),
            count=b["count"],
            unconverted_count=b["unconverted_count"],
        )
        for cur, b in sorted(buckets.items(), key=lambda kv: kv[1]["report_amount"], reverse=True)
    )
    return ReportRollup(
        currency=tgt,
        total=_quantize_money(total),
        count=sum(b.count for b in by_currency),
        # The COUNT, not `len(unconverted_ids)`. `unconverted_ids` only collects
        # rows that carried an id, so deriving the count from it silently
        # undercounts an id-less row — and this count is what gates submission
        # (`api/expenses.py` refuses while it is non-zero). A caller that can
        # produce a row is not obliged to produce an id for it.
        unconverted_count=unconverted,
        unconverted_ids=tuple(unconverted_ids),
        by_currency=by_currency,
    )


# ---------------------------------------------------------------------------
# Layer 2 — report total → org reporting currency (the CFO-threshold gate)
# ---------------------------------------------------------------------------


async def lock_report_reporting_amount(
    report,
    *,
    reporting_currency: str,
    fx_adapter: FXAdapter | None,
) -> bool:
    """Lock the report's ``total_amount`` into the org's reporting currency.

    Called at **submit** — the moment the report becomes a fixed approval object
    — so the CFO gate at approval time reads a figure that was fixed when the
    employee submitted, not one that moves with the market between submit and
    approve.

    Best-effort by design: an FX outage returns ``False`` and leaves the columns
    ``NULL`` rather than blocking a submission. That direction is safe because
    ``report_amount_for_gate`` treats a missing figure as *over* the threshold.

    ``fx_adapter`` may be ``None`` — the same allowance ``lock_expense_conversion``
    makes, and for the same reason: a report already denominated in the org's
    reporting currency locks at rate ``1`` with no provider call, so a tenant
    with no usable rate source still gets a complete stored snapshot instead of
    four NULL columns and a ``reporting_total: null`` in its own audit row. The
    caller decides whether a rate source is needed at all via
    ``conversion_requires_rate_source``; a genuinely cross-currency report with
    ``None`` here still fails closed through the ``except`` below.
    """
    tgt = normalize_currency(reporting_currency)
    src = normalize_currency(report.currency, default=tgt)
    try:
        converted = await convert_amount(
            amount=Decimal(str(report.total_amount or 0)),
            source_currency=src,
            reporting_currency=tgt,
            fx_adapter=fx_adapter,
        )
    except Exception:
        clear_report_reporting_amount(report)
        return False
    report.reporting_currency = converted.reporting_currency
    report.reporting_amount = converted.amount
    report.reporting_fx_rate = converted.fx_rate.quantize(_RATE_QUANT)
    report.reporting_fx_locked_at = converted.as_of
    return True


def clear_report_reporting_amount(report) -> None:
    """Invalidate the report-level lock — the composition or currency changed,
    so the stored reporting figure no longer describes the total."""
    report.reporting_currency = None
    report.reporting_amount = None
    report.reporting_fx_rate = None
    report.reporting_fx_locked_at = None


def report_amount_for_gate(report, *, reporting_currency: str) -> Decimal | None:
    """The report total expressed in the org's reporting currency, for the CFO
    threshold comparison — or ``None`` when it cannot be established.

    ``None`` is the fail-closed signal: the caller must then require CFO/admin
    sign-off. Under-gating (letting a foreign-currency report slip below a
    threshold denominated in another currency) is the dangerous direction, so a
    missing figure escalates rather than waves through.
    """
    tgt = normalize_currency(reporting_currency)
    locked = getattr(report, "reporting_amount", None)
    locked_cur = normalize_currency(getattr(report, "reporting_currency", None), default="")
    if locked is not None and locked_cur == tgt:
        return _quantize_money(Decimal(str(locked)))
    if normalize_currency(report.currency, default=tgt) == tgt:
        return _quantize_money(Decimal(str(report.total_amount or 0)))
    return None
