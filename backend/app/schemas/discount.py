"""Pydantic schemas for dynamic discounting & early-payment optimization.

Shared contract for the ``/api/discounts`` router and the frontend. Money
fields use the ``MoneyAmount`` annotation (Decimal in Python, JSON number on
the wire); tier/ROI percents stay ``Decimal`` for exactness. See
``backend/docs/dynamic-discounting.md``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.money import MoneyAmount, OptionalExactMoneyInput, OptionalMoneyAmount
from app.schemas.percent import OptionalPercentNumber, PercentNumber


class OfferScope(StrEnum):
    invoice = "invoice"
    vendor = "vendor"


class OfferSource(StrEnum):
    supplier = "supplier"
    system = "system"
    financing = "financing"


class OfferStatus(StrEnum):
    offered = "offered"
    accepted = "accepted"
    captured = "captured"
    declined = "declined"
    expired = "expired"


class DiscountTier(BaseModel):
    """One rung of a sliding-scale offer: pay within `days` for `percent` off."""

    days: int = Field(..., ge=0, le=365)
    percent: PercentNumber = Field(..., gt=0, lt=100)


class DiscountOfferCreate(BaseModel):
    """Create one discount offer.

    `invoice_id` / `vendor_id` are real `UUID`s for the same reason
    `BulkNegotiationRequest.vendor_id` is: the router parsed them with an
    unguarded `uuid.UUID(...)`, so a malformed id raised `ValueError` and
    surfaced as a 500 instead of the 422 a bad request deserves. A valid uuid
    string still coerces, so no caller changes.

    `extra="forbid"`, for the reason its sibling `BulkNegotiationRequest`
    carries it: a silently-dropped key buys an offer nobody asked for. Drop
    `valid_until` and the offer has no end date, so the optimizer cannot rank it
    and it stands indefinitely; drop `currency` and it is stamped from the
    invoice or the org instead of what the caller asserted, which is the exact
    divergence `discount_capture` refuses to capture against (§ Currency is
    checked before amount). 422 is the honest answer to a field we did not
    understand.

    This was held back one round because — unlike `BulkNegotiationRequest`, which
    shipped caller-less — this endpoint has live callers, and forbidding extras
    on a surface in use is a breaking change owed a caller audit first. That
    audit is done: 37 pytest call sites, 5 Playwright call sites, no frontend
    client function (`api/discounts.ts` covers list/accept/decline/optimize/
    bulk-negotiate/roi/dashboard and has no `createOffer`), no mobile caller, and
    `scripts/seed_extras.py` builds `DiscountOffer` ORM rows directly rather than
    going through this schema. The union of every key any of them sends is
    `{scope, invoice_id, vendor_id, tiers, base_amount, currency, valid_from}` —
    a strict subset of the fields declared below, so nothing breaks.

    The nested `DiscountTier` objects stay permissive, deliberately and
    consistently with `BulkNegotiationRequest`: `DiscountTier` is also a
    *response* model (`DiscountOfferResponse.tiers` / `.accepted_tier` are built
    from the `discount_offers.tiers` JSONB), so forbidding there would turn a
    stored row carrying an unexpected key into a 500 on read — a strictly worse
    failure than the one being prevented, and on the wrong side of the request /
    response boundary.
    """

    model_config = ConfigDict(extra="forbid")

    scope: OfferScope = OfferScope.invoice
    invoice_id: UUID | None = None
    vendor_id: UUID | None = None
    source: OfferSource = OfferSource.supplier
    tiers: list[DiscountTier] = Field(..., min_length=1)
    # Digits match `discount_offers.base_amount` Numeric(15, 2).
    base_amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    valid_from: date | None = None
    valid_until: date | None = None
    notes: str | None = Field(default=None, max_length=500)


class AcceptOfferRequest(BaseModel):
    """Accept an offer at a specific tier (defaults to the best one for today)."""

    tier_days: int | None = Field(default=None, ge=0, le=365)


class DiscountOfferResponse(BaseModel):
    id: str
    scope: str
    invoice_id: str | None
    vendor_id: str | None
    source: str
    status: str
    tiers: list[DiscountTier]
    base_amount: MoneyAmount
    currency: str
    valid_from: str | None
    valid_until: str | None
    accepted_tier: DiscountTier | None
    accepted_at: str | None
    captured_amount: OptionalMoneyAmount = None
    captured_at: str | None
    financing_provider: str | None
    notes: str | None
    created_at: str
    updated_at: str | None
    # Joined / derived (best-effort, set by the router when context is loaded).
    vendor_name: str | None = None
    invoice_number: str | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_db(
        cls,
        o,
        *,
        vendor_name: str | None = None,
        invoice_number: str | None = None,
        effective_status: str | None = None,
    ):
        """``effective_status`` overrides the stored column.

        The router passes ``discount_offers.effective_status(o, as_of=today)``,
        which reports a lapsed ``offered`` row as ``expired`` whether or not the
        (kill-switched) auto-capture sweep has ever materialized that onto the
        row. See ``services/discount_offers`` § Expiry is DERIVED, not read.
        """
        accepted = None
        if o.accepted_tier:
            accepted = DiscountTier(
                days=int(o.accepted_tier["days"]), percent=Decimal(str(o.accepted_tier["percent"]))
            )
        return cls(
            id=str(o.id),
            scope=o.scope,
            invoice_id=str(o.invoice_id) if o.invoice_id else None,
            vendor_id=str(o.vendor_id) if o.vendor_id else None,
            source=o.source,
            status=effective_status or o.status,
            tiers=[
                DiscountTier(days=int(t["days"]), percent=Decimal(str(t["percent"])))
                for t in (o.tiers or [])
            ],
            base_amount=o.base_amount,
            currency=o.currency,
            valid_from=o.valid_from.isoformat() if o.valid_from else None,
            valid_until=o.valid_until.isoformat() if o.valid_until else None,
            accepted_tier=accepted,
            accepted_at=o.accepted_at.isoformat() if o.accepted_at else None,
            captured_amount=o.captured_amount,
            captured_at=o.captured_at.isoformat() if o.captured_at else None,
            financing_provider=o.financing_provider,
            notes=o.notes,
            created_at=o.created_at.isoformat() if o.created_at else "",
            updated_at=o.updated_at.isoformat() if o.updated_at else None,
            vendor_name=vendor_name,
            invoice_number=invoice_number,
        )


class DiscountOfferListResponse(BaseModel):
    items: list[DiscountOfferResponse]
    total: int
    page: int
    page_size: int


class DiscountROIResponse(BaseModel):
    """Annualized-return analysis for one early-payment opportunity.

    Every horizon-relative field is nullable, because the horizon itself can be
    unknown: a vendor-scoped bulk offer spans many invoices and has no single
    net due date. `horizon_known` is the explicit marker — when it is `false`,
    `days_accelerated` / `annualized_return_pct` / `opportunity_cost` /
    `net_benefit` are `null` and `worthwhile` is `null` meaning *cannot rank*,
    which is not the same answer as `false` (*ranked, and it loses*).

    They used to be non-nullable, so the router substituted the discount
    deadline for the missing due date. That reports `days_accelerated: 0` —
    hence `annualized_return_pct: 0.00` and `worthwhile: false` beside a
    POSITIVE `net_benefit` — and made every bulk-negotiated offer with no
    `valid_until` permanently unrecommendable.
    """

    base_amount: MoneyAmount
    discount_percent: PercentNumber
    days_accelerated: int | None
    savings: MoneyAmount  # horizon-free — a percentage of the base amount
    annualized_return_pct: OptionalPercentNumber
    cost_of_capital_pct: PercentNumber
    opportunity_cost: OptionalMoneyAmount
    net_benefit: OptionalMoneyAmount
    worthwhile: bool | None
    # False = no net due date to accelerate against; read the nulls above as
    # "unknown", never as zero.
    horizon_known: bool = True


class OptimizerRecommendation(BaseModel):
    """One ranked early-payment recommendation from the optimizer."""

    offer_id: str | None = None
    invoice_id: str | None = None
    vendor_id: str | None = None
    vendor_name: str | None = None
    invoice_number: str | None = None
    tier_days: int
    discount_percent: PercentNumber
    pay_by: str  # ISO date — capture deadline
    roi: DiscountROIResponse
    # The currency THIS row's money is in — `roi.savings` is computed from the
    # offer's own `base_amount`, so it is the OFFER's currency, not the
    # response-level `currency` the totals are summed in. Stated per row
    # because the two differ exactly when `unconvertible` is set, and a client
    # that cannot name a figure's currency has to render it bare.
    currency: str = "USD"
    selected: bool  # True if it fits within the cash budget
    cumulative_outlay: MoneyAmount  # running cash committed through this rank
    # This offer's money is in a currency the totals are NOT in, so it is
    # excluded from every total (and, when a cash budget binds, from selection).
    # Its ROI percentages remain meaningful — a rate is currency-free.
    unconvertible: bool = False


class OptimizerRequest(BaseModel):
    """Request body for ``POST /api/discounts/optimize``.

    This endpoint took a bare ``dict`` and did ``Decimal(str(body["cash_budget"]))``,
    which is exact only by accident: by then ``json.loads`` had already turned a
    JSON number into a ``float``, so the budget the optimizer selected against
    was the rounded double, not what the caller sent. A bare dict on a money
    path also meant a malformed value reached ``Decimal()`` and surfaced as a
    500 rather than a 422.

    ``extra="forbid"`` on purpose: with a free-form dict, a misspelled key
    (``cashBudget``) silently ran the optimizer *unconstrained* and returned a
    plan committing more cash than the caller asked for. A 422 is the honest
    answer to a budget we did not understand.
    """

    model_config = ConfigDict(extra="forbid")

    # Optional — `None` means "no budget", which selects every worthwhile
    # opportunity. Accepted as an exact decimal string; see
    # `schemas/money.py::parse_exact_money`.
    cash_budget: OptionalExactMoneyInput = Field(default=None, ge=0)


class OptimizerResponse(BaseModel):
    cash_budget: OptionalMoneyAmount = None
    # The currency EVERY money total below is denominated in (the org's
    # reporting currency) — stated rather than assumed, because the totals are
    # sums across offers and offers carry their own currencies.
    currency: str = "USD"
    cost_of_capital_pct: PercentNumber
    total_savings_available: MoneyAmount
    total_savings_selected: MoneyAmount
    total_outlay_selected: MoneyAmount
    # Ranked offers left out of the totals because they are in another currency.
    unconvertible_count: int = 0
    recommendations: list[OptimizerRecommendation]
    # Offers with no resolvable net due date. Their `roi.horizon_known` is
    # `false` and their APR / verdict are `null`: they cannot be placed in an
    # APR ranking, so they are carried here instead of being sorted to the
    # bottom of `recommendations` at a fabricated 0.00 %. They contribute to no
    # total and are never selected; each carries a real `roi.savings`.
    unrankable: list[OptimizerRecommendation] = Field(default_factory=list)


class BulkNegotiationRequest(BaseModel):
    """Propose a single early-pay discount across a vendor's open invoices.

    "Bulk" is about the BASE, not the batch: this creates exactly ONE
    vendor-scoped offer whose `base_amount` is the summed open balance of that
    one vendor's invoices. It is not a multi-vendor operation and has no
    per-row skip-and-report result.

    `extra="forbid"` for the same reason `OptimizerRequest` carries it, and the
    key that matters here is `valid_until`. Dropped in silence, a misspelled one
    creates an offer with no end date — which has no net due date, so the
    optimizer cannot rank it at all and carries it on `unrankable` with a null
    APR (§ An unknown horizon is `null`, not `0`). A standing, unrankable
    discount against a vendor's entire open balance is not what a typo should
    buy.

    `vendor_id` is a real `UUID` rather than a bare `str`. The router parsed it
    with an unguarded `uuid.UUID(...)`, so a malformed id raised `ValueError`
    and surfaced as a 500 — a bad request deserves the 422 the type now
    produces, before any query runs.
    """

    model_config = ConfigDict(extra="forbid")

    vendor_id: UUID
    tiers: list[DiscountTier] = Field(..., min_length=1)
    valid_until: date | None = None
    notes: str | None = Field(default=None, max_length=500)


class DiscountDashboard(BaseModel):
    """Captured / missed / projected-savings rollup for the discounts dashboard.

    `capture_rate_pct` is `null` — with `insufficient_data` true — when nothing
    has been decided yet, exactly as its sibling
    `analytics.DiscountCaptureMetrics` reports the identical situation. "No
    offer has been captured or missed yet" and "we captured none of the ones we
    could have" are opposite facts, and `0.00` reads as the bad one
    (`docs/decisions.md` §34).
    """

    captured_count: int
    captured_amount: MoneyAmount
    missed_count: int
    missed_amount: MoneyAmount
    capture_rate_pct: OptionalPercentNumber = None
    #: Nothing has been decided — `captured_count + missed_count == 0` — so
    #: there is no rate to report. Set whenever `capture_rate_pct` is null, and
    #: only then; the two are one fact and a client should branch on either.
    #:
    #: REQUIRED, with no default, exactly as on the sibling
    #: `schemas/dashboard.DiscountCapture`. A `False` default would let a
    #: construction site that forgot the flag ship a response asserting "we
    #: have data" beside a null rate — the reassuring answer nobody stated,
    #: which is the failure `docs/decisions.md` §34 is about.
    insufficient_data: bool
    open_offer_count: int
    projected_savings: MoneyAmount  # net benefit of accepting all worthwhile open offers
    currency: str
    # Open offers excluded from `projected_savings` because they are denominated
    # in a currency other than `currency` — never summed in at face value.
    unconvertible_offer_count: int = 0
    # The same honesty for the two realised figures: `captured_amount` and
    # `missed_amount` count only offers denominated in `currency`, and these say
    # how many were left out. A bare cross-currency SUM presented under one code
    # is not a smaller number than the truth, it is a different quantity.
    excluded_captured_count: int = 0
    excluded_missed_count: int = 0
