"""Pydantic schemas for Positive Pay / payment-fraud files.

Shared contract for the ``/api/positive-pay`` router and the frontend
``/positive-pay`` route. Money fields use the ``MoneyAmount`` /
``OptionalMoneyAmount`` annotations (Decimal in Python, JSON number on the
wire); IDs are strings on the wire, parsed to UUID in the router.

PII discipline: no full account / routing number ever appears in these
schemas. ``account_last4`` is the only account detail exposed on a response;
``PresentedItemIn`` carries only a check number + amount. See
``backend/docs/positive-pay.md``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.money import MoneyAmount, OptionalMoneyAmount


class GenerateCheckIssueRequest(BaseModel):
    """Generate a check-issue Positive Pay file for a payment run."""

    bank_format: str = Field(default="csv", max_length=30)


class GenerateAchAuthorizationRequest(BaseModel):
    """Generate a standalone ACH debit-authorization file for the org."""

    bank_format: str = Field(default="csv", max_length=30)


class PresentedItemIn(BaseModel):
    """One item the bank reports as presented for payment, for return
    processing. Only a check number + amount — never an account number."""

    check_number: str | None = Field(default=None, max_length=100)
    # A cheque amount — the same quantity as `payments.amount` Numeric(15, 2).
    amount: OptionalMoneyAmount = Field(default=None, max_digits=15, decimal_places=2)


class ProcessReturnRequest(BaseModel):
    """The bank's return: the items it saw presented against an issued file."""

    presented_items: list[PresentedItemIn] = Field(default_factory=list)


class PositivePayFileResponse(BaseModel):
    id: str
    file_type: str
    bank_format: str
    status: str
    payment_run_id: str | None
    item_count: int
    total_amount: MoneyAmount
    # Currency ``total_amount`` is denominated in — for a check-issue file the
    # one currency its cheques' invoices agree on, stamped at generation.
    # ``None`` for legacy rows and for cheques that disagree: NOT a licence to
    # substitute a default, so the UI renders the total bare (decisions §196).
    currency: str | None
    account_last4: str | None
    file_key: str | None
    created_at: str
    updated_at: str | None
    meta: dict | None = None


class PositivePayListResponse(BaseModel):
    items: list[PositivePayFileResponse]
    total: int
    page: int
    page_size: int


class PositivePaySummaryResponse(BaseModel):
    """Whole-set KPI rollup for the positive-pay file list — counterpart of
    ``GET /api/expenses/summary``. Takes the SAME ``file_type`` / ``status``
    filters as the list. The page's ``itemsExported`` / ``returnsFlagged``
    reduced over the LOADED page while "Files" showed the server's whole-set
    ``total``.

    ``items_exported`` = SUM of ``item_count``; ``returns_flagged`` = SUM of
    each file's ``meta.return_summary.amount_mismatches + .not_on_file`` — the
    exact figures the page's reduces produced, whole-set."""

    total: int
    by_status: dict[str, int]
    items_exported: int
    returns_flagged: int


class ProcessReturnResponse(BaseModel):
    presented_count: int
    matched_ok: int
    amount_mismatches: int
    not_on_file: int
    exceptions_created: int
    file: PositivePayFileResponse
