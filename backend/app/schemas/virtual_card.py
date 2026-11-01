"""Schemas for virtual card endpoints."""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.api.pagination import PageMeta
from app.schemas.money import MoneyAmount, OptionalMoneyAmount


class GenerateCardsRequest(BaseModel):
    invoice_ids: list[str] = Field(..., min_length=1)


class CardResponse(BaseModel):
    id: str
    invoice_id: str
    vendor_id: str | None
    card_provider: str
    last_four: str | None
    # Money is exact: Decimal in Python (never float), serialised to a JSON
    # number for the SPA. The DB columns are Numeric(15, 2).
    amount_limit: MoneyAmount
    amount_charged: OptionalMoneyAmount = None
    currency: str
    status: str
    expires_at: str | None
    sent_at: str | None
    charged_at: str | None
    merchant_name: str | None
    decline_reason: str | None
    created_at: str

    # Joined fields
    vendor_name: str | None = None
    invoice_number: str | None = None

    model_config = {"from_attributes": True}


class CardListResponse(PageMeta):
    items: list[CardResponse]
    total: int


class CardGenerateSkip(BaseModel):
    """An invoice `POST /api/cards/generate` minted nothing for — the
    coded-refusal shape (`code` / English `message` / typed `params`) plus the
    invoice id. Codes: `services/card_issuance.CARD_SKIP_MESSAGES`."""

    invoice_id: str
    code: str
    message: str
    params: dict = Field(default_factory=dict)


class CardGenerateResponse(CardListResponse):
    """`CardListResponse` (``items`` / ``total`` = cards minted by THIS request,
    unchanged) plus why every other requested invoice got none."""

    skipped: list[CardGenerateSkip] = Field(default_factory=list)


class CardDetailsResponse(BaseModel):
    card_number: str
    exp_month: int
    exp_year: int
    cvv: str


class RebateStatusBreakdown(BaseModel):
    """A rebate total split by `CardRebate.status` lifecycle bucket.

    `pending_total` is the processor's ESTIMATE — not yet confirmed by the
    processor's out-of-band settlement, and not yet money in the bank.
    `confirmed_total` is processor-confirmed but not yet disbursed;
    `paid_out_total` has actually landed. Only `confirmed_total +
    paid_out_total` is realized money — see `CardDashboardResponse`.
    """

    pending_total: MoneyAmount
    confirmed_total: MoneyAmount
    paid_out_total: MoneyAmount


class CardDashboardResponse(BaseModel):
    active_cards: int
    active_cards_value: MoneyAmount
    spend_this_month: MoneyAmount
    # REALIZED rebates only — `confirmed` + `paid_out` `CardRebate` rows.
    # A `pending` rebate is the processor's estimate, not yet confirmed by
    # its own out-of-band settlement (see `POST /rebates/{id}/confirm`) and
    # nowhere close to a bank deposit; blending it into "Rebates Earned"
    # used to let 100% of a headline "earned" figure be entirely
    # unconfirmed. See `rebates_this_month_by_status` / `rebates_ytd_by_status`
    # for the full pending/confirmed/paid_out breakdown.
    rebates_this_month: MoneyAmount
    rebates_ytd: MoneyAmount
    # Projected annual = (rebates_ytd / months_elapsed) × 12, off the same
    # REALIZED (confirmed + paid_out) YTD figure above — never `pending`.
    # Falls back to rebates_this_month × 12 in January when YTD is short.
    # Surfaces to the rebate dashboard as the "on track for" headline.
    projected_annual_rebates: MoneyAmount = Decimal("0")
    rebates_this_month_by_status: RebateStatusBreakdown
    rebates_ytd_by_status: RebateStatusBreakdown
    # The single currency every money field above is denominated in. The
    # rollups were bare cross-currency SUMs over `VirtualCard.amount_limit` /
    # `.amount_charged` and `CardRebate.amount`, presented as one figure with no
    # code at all — and `CardRebate` has no currency column of its own, so a
    # rebate's currency is only knowable through its card. A multi-currency card
    # programme therefore produced a headline number that was not a quantity in
    # any currency.
    currency: str = "USD"
    # Rows each figure left out because they are denominated differently.
    # Counts only — a cross-currency remainder has no single total.
    excluded_card_count: int = 0
    excluded_rebate_count: int = 0


class RebateResponse(BaseModel):
    id: str
    virtual_card_id: str
    amount: MoneyAmount
    # The negotiated rebate ratio (e.g. 0.0125 = 1.25%) — a Decimal in the DB;
    # kept exact in Python, serialised to a JSON number like the money fields.
    rate: MoneyAmount
    # What THIS row's `amount` is denominated in. `card_rebates` has no currency
    # column — a rebate's currency is knowable only through the `virtual_cards`
    # row it accrued on — so every path returning a rebate joins that table and
    # resolves the code through `currency_conversion.card_currency_sql`, the one
    # owner of that expression. Before this the shape carried no currency at
    # all, so on a mixed-currency programme the UI could only render bare
    # figures with no code: honest, and useless. Resolved from the card, never
    # defaulted to the reporting currency — stamping that onto a row which is
    # not in it is the mistake the bare rendering existed to avoid.
    currency: str
    status: str
    period: str | None
    created_at: str


class RebateListResponse(PageMeta):
    items: list[RebateResponse]
    # Row COUNT over the whole filtered set, like every other list envelope in
    # `api/pagination.py` — NOT the money figure. `total` used to be the summed
    # rebate amount, which made this the one list endpoint whose `total` meant
    # something different from all the others; the money moved to
    # `total_amount` when the endpoint was paginated.
    total: int
    # Summed rebate amount over the WHOLE filtered set, never the loaded page,
    # in `currency`. Built by the same `_rebate_list_filters` the rows use, so
    # the figure and the set it claims to describe cannot drift.
    total_amount: MoneyAmount
    # What `total_amount` is denominated in, and how many rebates were left out
    # of it for being denominated in something else. Each ROW now states its own
    # currency, so this no longer gates whether a row can be rendered — it is
    # what stops a single-currency total beside a mixed list looking complete.
    currency: str = "USD"
    excluded_rebate_count: int = 0
