import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.api.pagination import PageMeta
from app.schemas.expense import normalize_iso_currency
from app.schemas.money import MoneyAmount


def _normalize_optional_currency(value: str | None) -> str | None:
    """Blank means "not asserted"; anything else must be a real ISO 4217 shape.

    `max_length=3` alone accepted `""`, `"e"` and `"us"`, and a memo stamped
    with a code no invoice carries can never pass the currency guard on either
    application path — it is born unappliable. Blank stays the "resolve it for
    me" signal `api/credit_memos.create_credit_memo` documents.
    """
    if value is None or not value.strip():
        return None
    return normalize_iso_currency(value)


#: Shared by create and PATCH so the two cannot validate a memo differently.
MemoNumber = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
#: Digits match `credit_memos.amount` Numeric(15, 2); strictly positive, since a
#: zero or negative credit is not a credit.
MemoAmount = Annotated[Decimal, Field(gt=0, max_digits=15, decimal_places=2)]
OptionalMemoCurrency = Annotated[str | None, AfterValidator(_normalize_optional_currency)]
#: A NOT NULL column's PATCH type: shape-checked, never blank.
MemoCurrency = Annotated[str, AfterValidator(normalize_iso_currency)]


class CreditMemoCreate(BaseModel):
    memo_number: MemoNumber
    # `uuid.UUID`, not `str`: the handler used to call `uuid.UUID(body.vendor_id)`
    # itself, so a malformed id raised an unhandled ValueError — a 500 for what
    # is a 422.
    vendor_id: uuid.UUID
    amount: MemoAmount
    # Optional, and NOT defaulted to "USD" here. A hardcoded default dead-ends
    # every non-USD tenant: `POST /api/credit-memos` then stamps USD onto the
    # row, and both application paths 409 a currency mismatch against the
    # invoice. `api/credit_memos.create_credit_memo` resolves the currency
    # instead: the named invoice's own currency when one is named, otherwise
    # the org's reporting currency, and only then the platform default.
    currency: OptionalMemoCurrency = None
    issued_date: date | None = None
    reason: str | None = None
    invoice_id: uuid.UUID | None = None  # optional: link (and apply) at creation time


class CreditMemoUpdate(BaseModel):
    """`PATCH /api/credit-memos/{id}` — correct a mis-keyed OPEN memo.

    Every field is optional and only the ones sent are changed. The four
    NOT NULL fields are typed without `None` and defaulted to it, so leaving
    one out is "unchanged" while sending an explicit `null` is a 422 — there is
    no way to blank a memo's number, vendor, amount or currency.

    `extra="forbid"` is load-bearing: `invoice_id` and `status` are NOT
    editable here. Linking a memo to an invoice IS applying it, which has its
    own endpoint, its own guards and its own audit action; a PATCH that
    silently ignored `invoice_id` would let a caller believe it had applied a
    credit it had not.
    """

    model_config = ConfigDict(extra="forbid")

    memo_number: MemoNumber = Field(default=None)  # type: ignore[assignment]
    vendor_id: uuid.UUID = Field(default=None)  # type: ignore[assignment]
    amount: MemoAmount = Field(default=None)  # type: ignore[assignment]
    currency: MemoCurrency = Field(default=None)  # type: ignore[assignment]
    issued_date: date | None = None
    reason: str | None = None


class CreditMemoApply(BaseModel):
    invoice_id: uuid.UUID


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
