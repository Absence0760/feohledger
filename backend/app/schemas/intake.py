"""Pydantic request/response schemas for the procurement intake router.

Intake captures a non-PO spend ask (software, services, hardware, other)
*before* a vendor/PO exists, routes it for review, and — once approved —
converts it into a ``PurchaseRequisition``.

Money convention (mirrors ``schemas/expense.py`` / ``schemas/contract.py``):
money stays ``Decimal`` on both sides. Request fields are typed
``Decimal | None`` for exactness on the way in; response/list fields are
``OptionalMoneyAmount``, which holds a ``Decimal`` in Python and makes the
JSON-number hop once, at serialisation time. The router never calls
``float(...)`` on an amount, and nothing money-valued is ever typed ``float``.
The flexible questionnaire payload (``form_data``) is a free-form
``dict | None`` JSONB blob.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.api.pagination import PageMeta
from app.models.procurement import IntakeStatus, IntakeType
from app.schemas.money import OptionalMoneyAmount

# ---------------------------------------------------------------------------
# Intake requests
# ---------------------------------------------------------------------------


class IntakeRequestBase(BaseModel):
    title: str = Field(..., max_length=255)
    request_type: IntakeType = IntakeType.other
    description: str | None = None
    # Digits match `intake_requests.estimated_amount` Numeric(15, 2).
    estimated_amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    currency: str = Field(default="USD", max_length=3)
    vendor_name: str | None = Field(default=None, max_length=255)
    vendor_id: str | None = None
    # Flexible intake questionnaire answers (advisory; no PII).
    form_data: dict | None = None
    needed_by: date | None = None
    justification: str | None = None


class IntakeRequestCreate(IntakeRequestBase):
    # Optional caller-supplied number; the service generates one when omitted.
    request_number: str | None = Field(default=None, max_length=50)
    # Accepted for wire compatibility and then IGNORED — the requester is always
    # the authenticated caller. A converted requisition inherits this id and
    # approval checks segregation of duties against it, so it must not be the
    # creator's own input. Kept on the schema (rather than removed) so an old
    # client sending it gets an intake owned by itself, not a 422.
    requester_user_id: str | None = None


class IntakeRequestUpdate(BaseModel):
    """PATCH — every field optional. Only allowed while the intake is ``open``;
    ``status`` moves through the dedicated submit/approve/reject/cancel routes."""

    title: str | None = Field(default=None, max_length=255)
    request_type: IntakeType | None = None
    description: str | None = None
    # Digits match `intake_requests.estimated_amount` Numeric(15, 2).
    estimated_amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    currency: str | None = Field(default=None, max_length=3)
    vendor_name: str | None = Field(default=None, max_length=255)
    vendor_id: str | None = None
    form_data: dict | None = None
    needed_by: date | None = None
    justification: str | None = None


class IntakeRequestResponse(BaseModel):
    id: str
    request_number: str
    title: str
    request_type: str
    requester_user_id: str
    description: str | None
    estimated_amount: OptionalMoneyAmount
    currency: str
    vendor_name: str | None
    vendor_id: str | None
    status: str
    form_data: dict | None
    needed_by: str | None
    justification: str | None
    converted_requisition_id: str | None
    converted_po_id: str | None
    created_at: str
    updated_at: str


class IntakeRequestListResponse(PageMeta):
    items: list[IntakeRequestResponse]
    total: int


class IntakeRequestSummaryResponse(BaseModel):
    """Whole-set KPI rollup for the intake list — counterpart of
    ``GET /api/expenses/summary``. Takes the SAME ``status`` / ``type`` /
    ``search`` filters as ``GET /api/intake`` (via the shared
    ``_intake_list_filters``). The page's ``openCount`` / ``reviewCount``
    filtered the LOADED page by status, so both contradicted the whole-set
    ``total`` count beside them."""

    total: int
    by_status: dict[str, int]


class IntakeDecision(BaseModel):
    """Optional body for an intake approve/reject/cancel — carries a reason.

    The reason is recorded in the audit row (and, on reject, stamped into the
    intake's ``form_data`` under ``review_reason`` so it survives on the row)."""

    reason: str | None = None


class IntakeConvertRequest(BaseModel):
    """Optional overrides applied when converting an approved intake into a
    requisition. When omitted, the intake's own title / estimated_amount /
    vendor / justification seed the single requisition line."""

    department: str | None = Field(default=None, max_length=120)
    needed_by: date | None = None


class IntakeConvertResponse(BaseModel):
    """The result of a convert-to-requisition call. ``created`` is ``False`` when
    the intake was already converted (idempotent replay) — the same
    ``requisition_id`` is returned either way."""

    intake: IntakeRequestResponse
    requisition_id: str
    requisition_number: str
    created: bool


# Re-exported enums so callers can import status/type from the schema module.
__all__ = [
    "IntakeStatus",
    "IntakeType",
    "IntakeRequestBase",
    "IntakeRequestCreate",
    "IntakeRequestUpdate",
    "IntakeRequestResponse",
    "IntakeRequestListResponse",
    "IntakeDecision",
    "IntakeConvertRequest",
    "IntakeConvertResponse",
]
