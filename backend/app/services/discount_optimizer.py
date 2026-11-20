"""Early-payment optimizer — rank discount opportunities by ROI and pick which
to capture under a cash-budget constraint.

This is the "AI-optimized payment timing" brain of dynamic discounting: given a
set of open early-payment opportunities (each a single tier of a
:class:`~app.models.discount.DiscountOffer`, already reduced to "pay this much
by this date for this discount"), it answers *which* discounts to capture when
cash is finite.

Economic model — greedy by annualized return
---------------------------------------------
Every early-payment discount is a short-term *investment*: parting with cash
``days_accelerated`` early to capture a percentage off. The yield of that
investment is its annualized return (see
:func:`app.services.discount_roi.annualized_return` —
``discount% / (100 - discount%) * 365 / days``). The optimizer ranks every
worthwhile opportunity by that APR **descending** and selects greedily until
the cash budget is exhausted.

Why greedy-by-APR is the right rule here: APR already normalizes "return per
dollar per day of cash committed", so taking the highest-APR discount first
captures the most savings per unit of the scarce resource (cash). When the
budget binds, this maximizes total savings; when it doesn't (``cash_budget is
None``), every worthwhile opportunity is selected and ordering is purely
cosmetic. It is deliberately *not* a 0/1-knapsack solve — discounts are
near-divisible in practice (many small invoices), the inputs are advisory, and
a transparent "highest yield first" ranking is what a treasurer reasons about.

Purity
------
No DB, no network, no clock. ``today`` is passed in; all money is ``Decimal``
and the ROI math is delegated to :mod:`app.services.discount_roi` (never
duplicated here). The router maps the returned dataclasses onto the Pydantic
``OptimizerResponse``. See ``backend/docs/dynamic-discounting.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.services.discount_roi import DiscountROI, compute_roi, days_between
from app.utils.currency_code import iso_currency_or_none

_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class OfferOpportunity:
    """One capturable early-payment opportunity (a single tier of an offer).

    ``base_amount`` — the amount the discount applies to.
    ``currency`` — the currency ``base_amount`` is denominated in
    (``DiscountOffer.currency``), or ``None`` when that code is missing or
    malformed — always unconvertible, whatever ``reporting_currency`` is.
    Carried because every money figure this module produces is a SUM across
    opportunities, and a sum is only a number
    when its terms share a currency — see ``optimize``'s ``reporting_currency``.
    ``discount_percent`` — the tier's discount (e.g. ``Decimal("2.00")``).
    ``pay_by`` — the discount/tier deadline (latest pay date that still earns
    the tier).
    ``due_date`` — the invoice's net due date (the baseline you'd otherwise pay
    on), or ``None`` when there isn't one to resolve. Days accelerated is
    ``days_between(pay_by, due_date)`` — the textbook cost-of-forgoing-discount
    horizon (pay on the deadline instead of at net), NOT the discount period
    itself. An opportunity whose ``pay_by`` has already elapsed
    (``today > pay_by``) is no longer capturable and is never selected.

    ``due_date=None`` means the horizon is genuinely unknown — a vendor-scoped
    bulk offer spans many invoices and has no single net due date. The caller
    must NOT substitute ``pay_by`` for it: that makes ``days_accelerated`` zero,
    which is a *measurement* ("the return is nil"), not "we cannot say". Such an
    opportunity is reported on ``OptimizationResult.unrankable`` rather than
    ranked at the bottom of a list it cannot be placed in.
    """

    offer_id: str
    invoice_id: str | None
    vendor_id: str | None
    vendor_name: str | None
    invoice_number: str | None
    base_amount: Decimal
    currency: str | None
    tier_days: int
    discount_percent: Decimal
    pay_by: date  # discount/tier deadline — latest pay date that still earns the tier
    due_date: date | None  # invoice net due date; None == horizon unknown


@dataclass(frozen=True)
class Recommendation:
    """One ranked opportunity + its ROI + whether it was selected."""

    opportunity: OfferOpportunity
    roi: DiscountROI
    selected: bool
    cumulative_outlay: Decimal  # running discounted cash committed through this rank
    # This opportunity's money is in a currency the caller's totals are NOT in,
    # so its `savings` / outlay are excluded from every total below. Its ROI is
    # still meaningful (a percentage is currency-free) — it is the SUMS that
    # aren't. See `optimize`'s `reporting_currency`.
    unconvertible: bool = False


@dataclass
class OptimizationResult:
    """Outcome of one optimization pass.

    Every money figure here is a SUM, and therefore denominated in the
    ``reporting_currency`` the caller passed. Opportunities in any other
    currency contribute to NONE of them and are counted on
    ``unconvertible_count`` instead."""

    cost_of_capital_pct: Decimal
    total_savings_available: Decimal  # sum of savings across *all* worthwhile opps
    total_savings_selected: Decimal  # sum of savings across selected opps
    total_outlay_selected: Decimal  # discounted cash committed to selected opps
    recommendations: list[Recommendation] = field(default_factory=list)
    # How many ranked opportunities were left out of the totals above because
    # their currency isn't the one the totals are in.
    unconvertible_count: int = 0
    # Opportunities with no resolvable net due date. Their ROI has no horizon,
    # so they cannot be placed in `recommendations` (which is an APR ranking)
    # and are carried here instead — with `roi.annualized_return_pct is None`
    # and `roi.worthwhile is None`, never a fabricated 0.00 / False pair. They
    # contribute to no total and are never selected. `savings` on each is real.
    unrankable: list[Recommendation] = field(default_factory=list)

    @property
    def unrankable_count(self) -> int:
        return len(self.unrankable)


def optimize(
    opportunities: list[OfferOpportunity],
    *,
    cash_budget: Decimal | None,
    cost_of_capital_pct: Decimal,
    today: date,
    reporting_currency: str | None = None,
) -> OptimizationResult:
    """Rank ``opportunities`` by annualized return and greedily select the
    highest-yield worthwhile ones that fit within ``cash_budget``.

    With ``cash_budget=None`` every worthwhile opportunity is selected. Only
    opportunities whose ROI is ``worthwhile`` (APR above the org's cost of
    capital) are ever eligible to be selected; non-worthwhile ones still appear
    in ``recommendations`` (ranked, ``selected=False``) for transparency.

    ``reporting_currency`` — the currency the CALLER's money figures are in
    (the org's reporting currency; also the currency of ``cash_budget``). Every
    total this function returns is a sum across opportunities, and every caller
    labels those totals with that one currency, so an opportunity denominated
    in another currency is flagged ``unconvertible`` and:

      * contributes to NONE of the three money totals (a €1,000 offer and a
        $1,000 offer were being added into a single figure the copilot then
        reported as "$1,960 committed, $40 saved"), and
      * is never selected **when a cash budget binds** — the budget is a
        reporting-currency number and its outlay cannot be measured against it.

    It IS still selectable under ``cash_budget=None``, because that decision
    involves no cross-currency arithmetic at all: the offer either clears the
    cost of capital or it doesn't, and an APR is currency-free. Excluding a
    genuinely worthwhile foreign discount from an unconstrained recommendation
    would be a functional loss, not a safety gain. Nothing is converted here —
    a rate fetched at read time would make a ranking move under the reader
    (``docs/decisions.md`` §18).

    ``reporting_currency=None`` disables the guard entirely (single-currency
    callers and the pure unit tests), which is why every production caller
    passes it.

    An opportunity with ``due_date is None`` has no horizon, so it has no APR
    and cannot be placed in an APR ranking. It goes to
    ``OptimizationResult.unrankable`` with an ROI whose horizon-dependent fields
    are ``None`` — never a fabricated ``0.00`` APR beside a positive
    ``net_benefit``. It is never selected and contributes to no total. Keeping
    it out of ``recommendations`` rather than sorting it to the bottom is what
    lets every downstream consumer of that list keep assuming a real number
    there.

    Returns a deterministic :class:`OptimizationResult`.
    """
    cost_of_capital_pct = Decimal(cost_of_capital_pct)
    # A reporting currency that is set but not a code becomes "", which no
    # offer matches: every offer is then unconvertible (fail closed), never
    # read as "no reporting currency" (which converts nothing).
    target_currency = (
        (iso_currency_or_none(reporting_currency) or "") if reporting_currency else None
    )

    def _unconvertible(opp: OfferOpportunity) -> bool:
        code = iso_currency_or_none(opp.currency)
        # An offer in no stated currency is in none of the totals' currency,
        # even for a caller that passed no reporting currency (fail closed).
        if code is None:
            return True
        return target_currency is not None and code != target_currency

    # Score every opportunity with the shared ROI primitive.
    scored: list[tuple[OfferOpportunity, DiscountROI, bool, bool]] = []
    unrankable: list[Recommendation] = []
    for opp in opportunities:
        if opp.due_date is None:
            # No horizon → no APR → no place in a ranking BY APR. Withheld
            # rather than defaulted: substituting `pay_by` for the missing due
            # date yields `days_accelerated == 0`, and the resulting object is
            # self-contradictory — `annualized_return_pct: 0.00` and
            # `worthwhile: False` sitting beside a POSITIVE `net_benefit`
            # (the opportunity cost of holding cash for zero days is zero
            # too). Every bulk-negotiated offer without a `valid_until` scored
            # that way and was therefore permanently unrecommendable.
            unrankable.append(
                Recommendation(
                    opportunity=opp,
                    roi=compute_roi(
                        base_amount=opp.base_amount,
                        discount_percent=opp.discount_percent,
                        days_accelerated=None,
                        cost_of_capital_pct=cost_of_capital_pct,
                    ),
                    selected=False,
                    cumulative_outlay=_ZERO,
                    unconvertible=_unconvertible(opp),
                )
            )
            continue
        roi = compute_roi(
            base_amount=opp.base_amount,
            discount_percent=opp.discount_percent,
            days_accelerated=days_between(opp.pay_by, opp.due_date),
            cost_of_capital_pct=cost_of_capital_pct,
        )
        # Capturable only while the discount deadline has not elapsed.
        capturable = today <= opp.pay_by
        scored.append((opp, roi, capturable, _unconvertible(opp)))

    # Rank by annualized return desc — highest yield per day of cash first.
    # Tie-break on larger savings, then offer_id for a stable deterministic order.
    scored.sort(
        key=lambda quad: (
            -quad[1].annualized_return_pct,
            -quad[1].savings,
            quad[0].offer_id,
        )
    )

    recommendations: list[Recommendation] = []
    total_savings_available = _ZERO
    total_savings_selected = _ZERO
    cumulative_outlay = _ZERO  # running discounted cash across *selected* opps only
    unconvertible_count = 0

    for opp, roi, capturable, unconvertible in scored:
        # Eligible = a worthwhile discount we can still capture (deadline open).
        # `bool(...)` because `worthwhile` is tri-state (None == unrankable);
        # nothing unrankable reaches this loop, but the coercion keeps the
        # invariant local rather than depending on the filter above.
        eligible = bool(roi.worthwhile) and capturable
        if unconvertible:
            unconvertible_count += 1
        elif eligible:
            total_savings_available += roi.savings

        # Discounted cash outlay for this opportunity (what actually leaves the
        # bank if captured): base_amount - savings.
        outlay = roi.base_amount - roi.savings

        selected = False
        if eligible:
            if cash_budget is None:
                # No budget to measure against → no cross-currency arithmetic.
                selected = True
            elif not unconvertible and cumulative_outlay + outlay <= cash_budget:
                selected = True

        # The running outlay — and the totals — stay in ONE currency, so an
        # unconvertible selection contributes to neither.
        if selected and not unconvertible:
            cumulative_outlay += outlay
            total_savings_selected += roi.savings

        recommendations.append(
            Recommendation(
                opportunity=opp,
                roi=roi,
                selected=selected,
                cumulative_outlay=cumulative_outlay,
                unconvertible=unconvertible,
            )
        )

    return OptimizationResult(
        cost_of_capital_pct=cost_of_capital_pct,
        total_savings_available=total_savings_available,
        total_savings_selected=total_savings_selected,
        total_outlay_selected=cumulative_outlay,
        recommendations=recommendations,
        unconvertible_count=unconvertible_count,
        unrankable=unrankable,
    )
