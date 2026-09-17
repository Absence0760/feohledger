"""Pydantic request/response schemas for the contracts router."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.api.pagination import PageMeta
from app.models.contract import ContractType
from app.schemas.money import MoneyAmount, OptionalMoneyAmount

# Bulk lifecycle actions a human may legitimately drive over a hand-picked
# set of contracts — the same three the single-row `POST /{contract_id}/
# activate|terminate|cancel` endpoints already expose (`renew` needs a
# per-contract `end_date` and isn't a good bulk fit). Typed as a Literal so an
# out-of-scope action is a 422 from Pydantic itself.
ContractBulkAction = Literal["activate", "terminate", "cancel"]


class ContractLineItemBase(BaseModel):
    line_number: int | None = None
    item_code: str | None = Field(default=None, max_length=100)
    description: str | None = None
    # Digits match `contract_line_items` — quantity Numeric(12, 4), the money
    # columns Numeric(15, 2). Deliberately NOT `ge=0`: a contract line can
    # carry a credit / rebate, so a negative is real data, not bad input.
    quantity: Decimal | None = Field(default=None, max_digits=12, decimal_places=4)
    unit_price: Decimal | None = Field(default=None, max_digits=15, decimal_places=2)
    total: Decimal | None = Field(default=None, max_digits=15, decimal_places=2)
    gl_account: str | None = Field(default=None, max_length=100)


class ContractLineItemResponse(ContractLineItemBase):
    id: str


class ContractBase(BaseModel):
    contract_number: str = Field(..., max_length=100)
    title: str | None = Field(default=None, max_length=255)
    description: str | None = None
    contract_type: ContractType = ContractType.purchase
    currency: str = Field(default="USD", max_length=3)
    # Digits match `contracts.total_value` / `.spend_limit` Numeric(15, 2). A
    # contract value / spend cap is never negative.
    total_value: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    spend_limit: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    not_to_exceed: bool = False
    start_date: date | None = None
    end_date: date | None = None
    signed_date: date | None = None
    auto_renew: bool = False
    renewal_term_months: int | None = Field(default=None, ge=0)
    renewal_notice_days: int = Field(default=30, ge=0, le=3650)
    payment_terms: str | None = Field(default=None, max_length=100)
    owner_user_id: str | None = None
    terms: dict | None = None


class ContractCreate(ContractBase):
    vendor_id: str
    line_items: list[ContractLineItemBase] = Field(default_factory=list)


class ContractUpdate(BaseModel):
    """PATCH — every field optional. Status changes go through the dedicated
    lifecycle endpoints (activate / terminate / cancel / renew), not here."""

    contract_number: str | None = Field(default=None, max_length=100)
    title: str | None = Field(default=None, max_length=255)
    description: str | None = None
    contract_type: ContractType | None = None
    vendor_id: str | None = None
    currency: str | None = Field(default=None, max_length=3)
    total_value: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    spend_limit: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    not_to_exceed: bool | None = None
    start_date: date | None = None
    end_date: date | None = None
    signed_date: date | None = None
    auto_renew: bool | None = None
    renewal_term_months: int | None = Field(default=None, ge=0)
    renewal_notice_days: int | None = Field(default=None, ge=0, le=3650)
    payment_terms: str | None = Field(default=None, max_length=100)
    owner_user_id: str | None = None
    terms: dict | None = None


class ContractRenew(BaseModel):
    """Extend a contract to a new end date (and optionally bump value)."""

    end_date: date
    total_value: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    spend_limit: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)


class ContractCreatePORequest(BaseModel):
    """Optional overrides when spinning a PO out of a contract.

    Both default to derived values: ``po_number`` is generated from the
    contract number, ``total`` from the contract's line-item totals (falling
    back to ``total_value``)."""

    po_number: str | None = Field(default=None, max_length=100)
    total: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)


class ContractSpendSummary(BaseModel):
    """Spend rolled up against the contract from its linked invoices."""

    invoiced_total: MoneyAmount
    invoice_count: int
    spend_limit: OptionalMoneyAmount
    remaining: OptionalMoneyAmount
    over_limit: bool


class ContractResponse(BaseModel):
    id: str
    contract_number: str
    title: str | None
    description: str | None
    contract_type: str
    status: str
    vendor_id: str
    vendor_name: str | None
    currency: str
    total_value: OptionalMoneyAmount
    spend_limit: OptionalMoneyAmount
    not_to_exceed: bool
    start_date: str | None
    end_date: str | None
    signed_date: str | None
    auto_renew: bool
    renewal_term_months: int | None
    renewal_notice_days: int
    renewal_alert_sent_at: str | None
    payment_terms: str | None
    owner_user_id: str | None
    file_url: str | None
    file_key: str | None
    terms: dict | None
    line_items: list[ContractLineItemResponse] = Field(default_factory=list)
    spend: ContractSpendSummary | None = None
    created_at: str
    updated_at: str


class ContractListResponse(PageMeta):
    items: list[ContractResponse]
    total: int


class ContractBulkStatusRequest(BaseModel):
    """Bulk lifecycle transition over a hand-picked set of contracts — the
    bulk counterpart of `POST /{contract_id}/activate|terminate|cancel`."""

    ids: list[str] = Field(..., min_length=1)
    action: ContractBulkAction


class ContractBulkStatusSkip(BaseModel):
    """One contract `bulk/status` didn't move, and why — a status that isn't
    a legal starting point for the action (mirroring the single-row
    endpoints' 409), or a bad/unresolvable id."""

    id: str
    reason: str


class ContractBulkStatusResponse(BaseModel):
    """Same partial-success contract as the invoice/expense/vendor bulk
    endpoints: each id is resolved independently, a bad one is
    skipped-and-reported rather than rolling back the whole batch."""

    updated: int
    skipped: list[ContractBulkStatusSkip] = Field(default_factory=list)


class ContractBulkExportRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1)
