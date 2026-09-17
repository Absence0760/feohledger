"""Pydantic request/response schemas for the purchase-requisitions router.

Money convention (mirrors ``schemas/expense.py`` / ``schemas/contract.py``):
money stays ``Decimal`` on both sides. Request fields are typed
``Decimal | None`` for exactness on the way in; response/list fields are
``MoneyAmount`` / ``OptionalMoneyAmount``, which hold a ``Decimal`` in Python
and make the JSON-number hop once, at serialisation time. The router never
calls ``float(...)`` on an amount, and nothing money-valued is ever typed
``float``.

The requisition ``total`` is always recomputed server-side from the line items
(``sum(quantity * unit_price)``) — a client-sent total is ignored, so the header
total can never drift from its lines.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.api.pagination import PageMeta
from app.models.procurement import RequisitionStatus
from app.schemas.money import MoneyAmount, OptionalMoneyAmount

# ---------------------------------------------------------------------------
# Line items
# ---------------------------------------------------------------------------


class RequisitionLineItemBase(BaseModel):
    line_number: int | None = None
    catalog_item_id: str | None = None
    item_code: str | None = Field(default=None, max_length=100)
    description: str | None = None
    # Digits match `requisition_line_items` — quantity Numeric(12, 4),
    # unit_price Numeric(15, 2). A requisition asks to BUY, so both are
    # non-negative; a credit is a credit memo, not a requisition line.
    quantity: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=4)
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    gl_account_id: str | None = None
    uom: str | None = Field(default=None, max_length=20)


class RequisitionLineItemCreate(RequisitionLineItemBase):
    pass


class RequisitionLineItemResponse(BaseModel):
    id: str
    line_number: int | None
    catalog_item_id: str | None
    item_code: str | None
    description: str | None
    quantity: float | None
    unit_price: OptionalMoneyAmount
    total: OptionalMoneyAmount
    gl_account_id: str | None
    uom: str | None


# ---------------------------------------------------------------------------
# Requisitions
# ---------------------------------------------------------------------------


class RequisitionBase(BaseModel):
    requisition_number: str = Field(..., max_length=50)
    title: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=120)
    needed_by: date | None = None
    justification: str | None = None
    vendor_id: str | None = None
    contract_id: str | None = None
    budget_id: str | None = None
    currency: str = Field(default="USD", max_length=3)
    notes: str | None = None


class RequisitionCreate(RequisitionBase):
    line_items: list[RequisitionLineItemCreate] = Field(default_factory=list)


class RequisitionUpdate(BaseModel):
    """PATCH — every field optional. Allowed on ``draft`` requisitions only.

    ``line_items`` is a full replacement of the requisition's lines (None leaves
    them untouched). ``status`` moves through the dedicated transition routes
    (submit / approve / reject / cancel / convert), never here."""

    requisition_number: str | None = Field(default=None, max_length=50)
    title: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=120)
    needed_by: date | None = None
    justification: str | None = None
    vendor_id: str | None = None
    contract_id: str | None = None
    budget_id: str | None = None
    currency: str | None = Field(default=None, max_length=3)
    notes: str | None = None
    line_items: list[RequisitionLineItemCreate] | None = None


class RequisitionResponse(BaseModel):
    id: str
    requisition_number: str
    title: str | None
    requester_user_id: str
    department: str | None
    status: str
    needed_by: str | None
    justification: str | None
    vendor_id: str | None
    contract_id: str | None
    budget_id: str | None
    total: MoneyAmount
    currency: str
    notes: str | None
    submitted_at: str | None
    approved_at: str | None
    approved_by: str | None
    rejection_reason: str | None
    converted_po_id: str | None
    line_items: list[RequisitionLineItemResponse] = Field(default_factory=list)
    created_at: str
    updated_at: str


class RequisitionListResponse(PageMeta):
    items: list[RequisitionResponse]
    total: int


class RequisitionCurrencyTotal(BaseModel):
    """One currency's slice of the whole-set requisition-value rollup.

    ``total`` is an **exact decimal string** (never ``float``) — the KPI row
    renders it through ``utils/currencyGroups.formatCurrencyTotals``. Requisition
    values are never added across currencies and never FX-converted on a read.
    """

    currency: str
    total: str
    count: int


class RequisitionSummaryResponse(BaseModel):
    """Whole-set KPI rollup for the requisitions list — counterpart of
    ``GET /api/expenses/summary``. Takes the SAME ``status`` / ``search``
    filters as ``GET /api/requisitions`` (via the shared
    ``_requisition_list_filters``) so the KPIs can't contradict the table
    beneath them: ``periodTotal`` / ``pendingCount`` used to reduce over the
    LOADED page only."""

    total: int
    by_status: dict[str, int]
    by_currency: list[RequisitionCurrencyTotal]


# ---------------------------------------------------------------------------
# Transition bodies
# ---------------------------------------------------------------------------


class RequisitionDecision(BaseModel):
    """Optional body for a requisition approve / reject / cancel — carries a
    free-text reason (required-ish for reject; surfaced on the row)."""

    reason: str | None = None


# ---------------------------------------------------------------------------
# Convert-to-PO
# ---------------------------------------------------------------------------


class ConvertToPoResponse(BaseModel):
    """Result of ``POST /requisitions/{id}/convert-to-po``.

    ``created`` is ``False`` on the idempotent replay path (the requisition was
    already converted) so the caller can tell a fresh conversion from a no-op."""

    requisition_id: str
    po_id: str
    po_number: str
    total: MoneyAmount
    created: bool


__all__ = [
    "RequisitionLineItemBase",
    "RequisitionLineItemCreate",
    "RequisitionLineItemResponse",
    "RequisitionBase",
    "RequisitionCreate",
    "RequisitionUpdate",
    "RequisitionResponse",
    "RequisitionListResponse",
    "RequisitionDecision",
    "ConvertToPoResponse",
    "RequisitionStatus",
]
