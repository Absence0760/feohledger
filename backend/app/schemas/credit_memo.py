from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.api.pagination import PageMeta
from app.schemas.money import MoneyAmount


class CreditMemoCreate(BaseModel):
    memo_number: str = Field(..., max_length=100)
    vendor_id: str
    # Digits match `credit_memos.amount` Numeric(15, 2).
    amount: Decimal = Field(..., gt=0, max_digits=15, decimal_places=2)
    # Optional, and NOT defaulted to "USD" here. A hardcoded default dead-ends
    # every non-USD tenant: `POST /api/credit-memos` then stamps USD onto the
    # row, and both application paths 409 a currency mismatch against the
    # invoice — with no PATCH on credit memos, the memo can never be applied or
    # corrected. `api/credit_memos.create_credit_memo` resolves the currency
    # instead: the named invoice's own currency when one is named, otherwise
    # the org's reporting currency, and only then the platform default.
    currency: str | None = Field(default=None, max_length=3)
    issued_date: date | None = None
    reason: str | None = None
    invoice_id: str | None = None  # optional: link at creation time


class CreditMemoApply(BaseModel):
    invoice_id: str


class CreditMemoResponse(BaseModel):
    id: str
    memo_number: str
    vendor_id: str
    vendor_name: str | None = None
    invoice_id: str | None = None
    invoice_number: str | None = None
    # Decimal in Python (money-is-exact); serialises to a JSON number on the
    # wire — same shape the frontend already parses, no precision loss.
    amount: MoneyAmount
    currency: str
    issued_date: str | None = None
    reason: str | None = None
    status: str
    applied_at: str | None = None
    applied_by: str | None = None
    created_at: str


class CreditMemoListResponse(PageMeta):
    items: list[CreditMemoResponse]
    total: int


class CreditMemoSummaryResponse(BaseModel):
    """Per-status tallies for the `/credit-memos` filter chips.

    `by_status` always carries every known status (a zero is a real answer, not
    an absent key) plus any status this build does not know about yet, so a
    new lifecycle state shows up as a count instead of vanishing from the sum.
    """

    total: int
    by_status: dict[str, int]
