from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from app.schemas.money import MoneyAmount, OptionalMoneyAmount
from app.services.vendor_consolidation import mask_tax_id


def _is_masked_tax_id(value) -> bool:
    """True when a caller echoed back the ``***<last4>`` masked value we return
    in responses. Real tax ids are digits/separators and never start ``***``."""
    return isinstance(value, str) and value.startswith("***")


class InvoiceStatus(StrEnum):
    new = "new"
    pending = "pending"
    ready_for_review = "ready_for_review"
    approved = "approved"
    rejected = "rejected"
    sending_to_erp = "sending_to_erp"
    sent_to_erp = "sent_to_erp"
    posted_in_erp = "posted_in_erp"
    payment_scheduled = "payment_scheduled"
    paid = "paid"
    done = "done"
    failed = "failed"


class InvoiceBase(BaseModel):
    vendor: str = Field(..., max_length=255)
    invoice_number: str = Field(..., max_length=100)
    # Digits match `invoices.amount` Numeric(15, 2).
    amount: Decimal = Field(..., ge=0, max_digits=15, decimal_places=2)
    currency: str = Field(default="USD", max_length=3)
    invoice_date: date | None = None
    received_date: date | None = None
    due_date: date | None = None
    payment_terms: str | None = Field(default=None, max_length=50)
    # NOTE: `status` is deliberately NOT accepted on create. Every invoice must
    # enter the workflow at `new` and reach any later state only through the
    # state machine (validate_transition + segregation + thresholds + the CFO
    # gate + the approval signature + the immutable audit row). Accepting a
    # caller-supplied status here let a POST mint an already-`approved` (or even
    # `paid`) invoice, bypassing every one of those controls. `create_invoice`
    # hardcodes `new`; this field stays off the schema. See InvoiceUpdate for the
    # same reasoning on edits.
    po_number: str | None = Field(default=None, max_length=100)
    subtotal: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    tax_amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    discount_amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    shipping_amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    remit_to_address: str | None = None
    bill_to_address: str | None = None
    vendor_address: str | None = None
    vendor_tax_id: str | None = Field(default=None, max_length=50)
    ship_to_address: str | None = None
    tax_rate: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    payment_method: str | None = Field(default=None, max_length=50)
    reference_number: str | None = Field(default=None, max_length=100)
    description: str | None = None
    notes: str | None = None
    gl_account: str | None = Field(default=None, max_length=100)
    cost_center: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    project: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _guard_masked_tax_id(self):
        # `vendor_tax_id` is returned masked (`***<last4>`) in responses. A UI that
        # round-trips the invoice on save echoes that masked value back — never
        # persist the mask over the stored raw tax id. Null it and drop it from the
        # write set so `model_dump(exclude_unset=True)` skips it on update (leaving
        # the stored value unchanged) and a create stores nothing.
        if _is_masked_tax_id(self.vendor_tax_id):
            self.vendor_tax_id = None
            self.__pydantic_fields_set__.discard("vendor_tax_id")
        return self


class InvoiceCreate(InvoiceBase):
    pass


class InvoiceUpdate(BaseModel):
    vendor: str | None = Field(default=None, max_length=255)
    invoice_number: str | None = Field(default=None, max_length=100)
    amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    currency: str | None = Field(default=None, max_length=3)
    invoice_date: date | None = None
    received_date: date | None = None
    due_date: date | None = None
    payment_terms: str | None = Field(default=None, max_length=50)
    # NOTE: `status` is deliberately NOT editable here. A status change is a
    # workflow transition, not an ordinary field edit — it must run through the
    # state machine (validate_transition), segregation-of-duties, approval
    # thresholds, the CFO gate, the approval signature, and the immutable audit
    # trail. Those live on the dedicated transition endpoints
    # (/approve, /reject, /resubmit, /send-to-erp, payments void) via
    # services.review + workflow_engine.transition_invoice. Re-exposing `status`
    # on this PATCH body would let a bare setattr bypass every one of them
    # (e.g. new → paid, or an uploader self-approving). Keep it off.
    po_number: str | None = None
    subtotal: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    tax_amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    discount_amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    shipping_amount: Decimal | None = Field(default=None, ge=0, max_digits=15, decimal_places=2)
    remit_to_address: str | None = None
    bill_to_address: str | None = None
    vendor_address: str | None = None
    vendor_tax_id: str | None = Field(default=None, max_length=50)
    ship_to_address: str | None = None
    tax_rate: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    payment_method: str | None = Field(default=None, max_length=50)
    reference_number: str | None = Field(default=None, max_length=100)
    description: str | None = None
    notes: str | None = None
    gl_account: str | None = Field(default=None, max_length=100)
    cost_center: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    project: str | None = Field(default=None, max_length=100)
    # Optimistic-concurrency guard (If-Unmodified-Since style): the `updated_at`
    # the client read alongside the invoice it's editing. When supplied and it
    # no longer matches the row's CURRENT `updated_at` (checked under a row
    # lock — see `get_invoice_for_update`), the PATCH is refused 409 instead of
    # silently overwriting a change another user made in between. Omitted
    # entirely (the pre-existing default) is unchanged — no check runs,
    # matching every caller that predates this field.
    expected_updated_at: datetime | None = None

    @model_validator(mode="after")
    def _guard_masked_tax_id(self):
        # See InvoiceBase._guard_masked_tax_id — same round-trip protection on edit.
        if _is_masked_tax_id(self.vendor_tax_id):
            self.vendor_tax_id = None
            self.__pydantic_fields_set__.discard("vendor_tax_id")
        return self


class InvoiceResponse(BaseModel):
    """Matches the frontend Invoice TypeScript interface exactly."""

    id: str
    correlation_id: str
    vendor: str
    # Resolved link to the `vendors` row this invoice belongs to. NULL when the
    # vendor could not be established. Not PII (an opaque id) and load-bearing
    # for the UI: the credit-memo apply picker filters on it so a memo is only
    # ever offered against its own vendor's invoices — the same rule the backend
    # enforces fail-closed in app/api/credit_memos.py.
    vendor_id: str | None = None
    invoice_number: str
    amount: MoneyAmount
    currency: str
    invoice_date: str | None
    received_date: str | None
    due_date: str | None
    payment_terms: str | None
    status: InvoiceStatus
    po_number: str
    subtotal: OptionalMoneyAmount = None
    tax_amount: OptionalMoneyAmount = None
    discount_amount: OptionalMoneyAmount = None
    shipping_amount: OptionalMoneyAmount = None
    remit_to_address: str | None
    bill_to_address: str | None
    vendor_address: str | None
    vendor_tax_id: str | None
    ship_to_address: str | None
    tax_rate: float | None
    payment_method: str | None
    reference_number: str | None
    description: str
    notes: str | None
    approval_date: str | None
    approved_by: str | None
    rejected_by: str | None
    assigned_to_id: str | None
    assigned_to: str | None
    gl_account: str | None
    cost_center: str | None
    department: str | None = None
    project: str | None = None
    # Spend-to-contract link (services.contract_spend / contract_compliance).
    # Null = off-contract spend. Set via POST /api/invoices/{id}/link-contract.
    contract_id: str | None = None
    # The subsidiary this invoice belongs to (multi-entity). It decides which
    # chart its GL code resolves against — shared accounts ∪ this entity's own
    # (`services/gl_chart`) — so the GL pickers scope to it rather than to the
    # sidebar selection, which can differ (a deep link, the consolidated view).
    # NULL only on an invoice no entity was ever stamped on.
    entity_id: str | None = None
    # Inter-company routing (multi-entity). `counterparty_entity_id` names the
    # other subsidiary on an inter-company charge; `intercompany_mirror_id` links
    # an origin invoice to its generated mirror payable (and vice-versa). Both
    # null on ordinary invoices. See backend/docs/inter-company.md.
    counterparty_entity_id: str | None = None
    intercompany_mirror_id: str | None = None
    created_at: str
    file_url: str | None
    warnings: list[dict] | None = None
    # Latest 2/3-way PO match result (status, variance, issues). Populated by
    # `services.invoice_warnings.refresh_warnings`. Null when the invoice has
    # no `po_number`. The invoice modal renders this as a PO Match panel.
    po_match: dict | None = None
    # Summary counts from the latest extraction's priors_metadata — feeds the
    # small "priors applied" indicator on the invoice-list row. Null when no
    # extraction ran or no priors fired.
    priors_summary: dict | None = None
    # The row's current `updated_at`, ISO-8601. The invoice-edit UI captures
    # this on load and echoes it back as `expected_updated_at` on the next
    # `PATCH` — the client-side half of the optimistic-concurrency guard.
    updated_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_db(cls, inv) -> "InvoiceResponse":
        return cls(
            id=str(inv.id),
            correlation_id=str(inv.correlation_id),
            vendor=inv.vendor_name,
            vendor_id=str(inv.vendor_id) if inv.vendor_id else None,
            invoice_number=inv.invoice_number,
            amount=inv.amount,
            currency=inv.currency,
            invoice_date=inv.invoice_date.isoformat() if inv.invoice_date else None,
            received_date=inv.received_date.isoformat() if inv.received_date else None,
            due_date=inv.due_date.isoformat() if inv.due_date else None,
            payment_terms=inv.payment_terms,
            status=inv.status,
            po_number=inv.po_number or "",
            subtotal=inv.subtotal,
            tax_amount=inv.tax_amount,
            discount_amount=inv.discount_amount,
            shipping_amount=inv.shipping_amount,
            remit_to_address=inv.remit_to_address,
            bill_to_address=inv.bill_to_address,
            vendor_address=inv.vendor_address,
            # PII: never return the raw extracted tax id — mask to `***<last4>`.
            vendor_tax_id=mask_tax_id(inv.vendor_tax_id),
            ship_to_address=inv.ship_to_address,
            tax_rate=float(inv.tax_rate) if inv.tax_rate is not None else None,
            payment_method=inv.payment_method,
            reference_number=inv.reference_number,
            description=inv.description or "",
            notes=inv.notes,
            approval_date=inv.approval_date.isoformat() if inv.approval_date else None,
            approved_by=inv.approved_by,
            rejected_by=inv.rejected_by,
            assigned_to_id=str(inv.assigned_to_id) if inv.assigned_to_id else None,
            assigned_to=inv.assigned_to,
            gl_account=inv.gl_account,
            cost_center=inv.cost_center,
            department=inv.department,
            project=inv.project,
            contract_id=str(inv.contract_id) if inv.contract_id else None,
            entity_id=str(inv.entity_id) if inv.entity_id else None,
            counterparty_entity_id=(
                str(inv.counterparty_entity_id) if inv.counterparty_entity_id else None
            ),
            intercompany_mirror_id=(
                str(inv.intercompany_mirror_id) if inv.intercompany_mirror_id else None
            ),
            created_at=inv.created_at.isoformat() if inv.created_at else "",
            updated_at=inv.updated_at.isoformat() if inv.updated_at else "",
            file_url=inv.file_url,
            warnings=inv.warnings,
            po_match=inv.po_match,
            priors_summary=_priors_summary(inv),
        )


def _priors_summary(inv) -> dict | None:
    """Flatten the latest extraction result's priors_metadata into counts."""
    results = getattr(inv, "extraction_results", None) or []
    if not results:
        return None
    latest = max(results, key=lambda r: r.created_at or 0)
    meta = latest.priors_metadata or {}
    cache = len(meta.get("vendor_cache_applied") or [])
    rag = len(meta.get("rag_neighbors") or [])
    if cache == 0 and rag == 0:
        return None
    return {"cache": cache, "rag": rag}


class InvoiceListResponse(BaseModel):
    items: list[InvoiceResponse]
    total: int
    page: int
    page_size: int


class InvoiceCountsResponse(BaseModel):
    """Per-status invoice tallies for the list-page filter chips.

    `counts` maps each status that has at least one invoice to its count;
    `total` is the sum across every status. Computed with a single
    GROUP BY so the chips stay accurate past the page-1 result window.
    """

    counts: dict[str, int]
    total: int


class BulkDeleteRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1)


class BulkDeleteResponse(BaseModel):
    deleted: int
    skipped: list[str] = []


class BulkStatusRequest(BaseModel):
    """Bulk status change over a hand-picked set of invoices.

    `status` is deliberately typed as the full `InvoiceStatus` enum rather than a
    narrower Literal so an out-of-scope target gets a *named* 422 from the
    endpoint's own whitelist (`api/invoices.BULK_STATUS_TARGETS`) explaining which
    endpoint owns that transition, instead of a generic enum-mismatch error. The
    whitelist — not this schema — is the gate.
    """

    ids: list[str] = Field(..., min_length=1)
    status: InvoiceStatus
    # Required when `status` is `rejected`: the endpoint routes rejections
    # through `review.reject_invoice`, which records the reason on the audit row
    # and on the `review_rejected` exception. Ignored for every other target.
    reason: str | None = Field(default=None, max_length=1000)


class BulkStatusSkip(BaseModel):
    """One invoice `bulk/status` didn't move, and why.

    `bulk_status_change` routes `approved`/`rejected` through the same
    `review.approve_invoice` / `reject_invoice` the single-invoice endpoint
    uses, so a skip there can be a segregation-of-duties refusal, a CFO-gate
    refusal, or a max-amount cap — not just an immutable status. A bare id
    list can't distinguish "this needs a different signer" from "this is
    frozen"; `reason` carries the real cause (the underlying service's own
    `HTTPException.detail`) so the caller isn't misled into treating an
    authorization refusal as a data problem.
    """

    id: str
    reason: str


class BulkStatusResponse(BaseModel):
    updated: int
    skipped: list[BulkStatusSkip] = []


class BulkExportRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1)
    format: str = Field(default="csv", pattern=r"^(csv|json|xml)$")


class BulkRecodeGLRequest(BaseModel):
    """Bulk GL re-code filter. All filters are optional and AND-combined.

    `dry_run` defaults to True so an accidentally-fired request reports
    what would change without writing. `include_ai_fallback` is opt-in
    because it bills the org per invoice.
    """

    from_date: date | None = None
    to_date: date | None = None
    vendor_ids: list[str] = Field(default_factory=list)
    include_ai_fallback: bool = False
    dry_run: bool = True


class InvoiceLineItemResponse(BaseModel):
    id: str
    line_number: int | None
    item_code: str | None
    description: str | None
    quantity: float | None
    # Money stays `Decimal` in Python and serialises to the same JSON number —
    # matching what `GET /api/invoices/{id}/line-items` actually emits, which
    # builds its dict through `schemas.money.json_money`. `quantity` above is a
    # count, not money, so it is honestly a float.
    unit_price: OptionalMoneyAmount
    tax: OptionalMoneyAmount
    total: OptionalMoneyAmount
    gl_account: str | None

    model_config = {"from_attributes": True}


class RouteIntercompanyRequest(BaseModel):
    """Body for POST /api/invoices/{id}/route-intercompany.

    `counterparty_entity_id` names the OTHER subsidiary (an `entities` row in
    this tenant) that the inter-company charge is billed to. The service
    generates the mirror payable under that entity. See
    backend/docs/inter-company.md.
    """

    counterparty_entity_id: str


class AuditSummaryResponse(BaseModel):
    """Cached, LLM- or template-generated one-paragraph audit-log summary for
    the invoice detail modal. `stale` is always False today (the endpoint
    regenerates when the fingerprint moves before returning) but is part of the
    contract so a future async-regeneration path can surface staleness."""

    text: str
    confidence_context: str | None = None
    generated_at: str | None = None
    stale: bool = False


# ---------------------------------------------------------------------------
# Supplier chat (AP side) — datetimes serialized as ISO 8601 strings, matching
# the rest of invoice.py. See backend/docs/supplier-chat.md.
# ---------------------------------------------------------------------------


class ChatMessageCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=10_000)
    mention_user_ids: list[str] = []  # control-plane users.id strings
    template_key: str | None = None  # one of the CHAT_TEMPLATES keys


class ChatAttachmentOut(BaseModel):
    file_url: str
    filename: str
    content_type: str
    size: int


class ChatMessageResponse(BaseModel):
    id: str
    thread_id: str
    author_role: str  # "ap_team" | "supplier" | "system"
    author_user_id: str | None
    author_name: str | None
    body: str
    mention_user_ids: list[str] = []  # maps from model `mentions`
    template_key: str | None = None
    attachments: list[ChatAttachmentOut] = []
    created_at: str  # ISO 8601


class ChatThreadResponse(BaseModel):
    id: str | None  # None when not yet lazy-created
    invoice_id: str
    status: str  # "open" | "resolved"
    resolved_at: str | None = None  # ISO 8601 or None
    resolved_by: str | None = None
    messages: list[ChatMessageResponse] = []


class ChatTemplate(BaseModel):
    key: str  # "missing_po" | "amount_mismatch" | "payment_status"
    label: str
    body: str
