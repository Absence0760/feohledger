"""Base ERP adapter interface and shared data types."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


class ErpInvoiceStatus(enum.StrEnum):
    draft = "draft"
    open = "open"
    partially_paid = "partially_paid"
    paid = "paid"
    cancelled = "cancelled"
    unknown = "unknown"


@dataclass
class LineItemPayload:
    line_number: int
    item_code: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    gl_account: str | None = None


@dataclass
class InvoicePayload:
    """Normalized invoice data sent to every ERP adapter."""

    correlation_id: str
    invoice_number: str
    vendor_name: str
    amount: Decimal
    currency: str = "USD"
    vendor_tax_id: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    po_number: str | None = None
    description: str | None = None
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    tax_rate: Decimal | None = None
    discount_amount: Decimal | None = None
    shipping_amount: Decimal | None = None
    gl_account: str | None = None
    cost_center: str | None = None
    payment_terms: str | None = None
    payment_method: str | None = None
    bill_to_address: str | None = None
    remit_to_address: str | None = None
    vendor_address: str | None = None
    line_items: list[LineItemPayload] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Failure reason codes — PII-free, stable across providers
# ---------------------------------------------------------------------------

#: HTTP status → stable, provider-independent reason code for a failed ERP
#: call. Deliberately coarse: the code is a *routing* signal for an operator
#: ("we sent something the ERP rejected" vs "our credentials are stale"), not a
#: diagnosis. The diagnosis lives in the ERP's own error console.
_HTTP_REASON_CODES: dict[int, str] = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_failed",
    429: "rate_limited",
}


def erp_failure_reason(status_code: int) -> str:
    """Map an HTTP status to a stable, PII-free failure reason code.

    Never derived from the response *body*. An ERP's validation error routinely
    echoes the submitted fields back, and :class:`InvoicePayload` carries
    ``vendor_tax_id`` / ``vendor_address`` / ``remit_to_address`` /
    ``bill_to_address``. ``services/erp.py`` raises
    ``RuntimeError(result.message)`` and stores ``str(exc)`` on the
    **append-only** ``invoice.erp_failed`` audit row (migration 0022's
    BEFORE-DELETE trigger) and on ``WorkflowInstance.state_data["last_error"]``;
    ``audit_log_shipper`` then ships that row to CloudWatch / S3 Object Lock,
    where it cannot be redacted either. So the message a caller builds from this
    carries the status code and this code, and nothing the provider wrote.
    """
    if status_code in _HTTP_REASON_CODES:
        return _HTTP_REASON_CODES[status_code]
    if 500 <= status_code < 600:
        return "provider_error"
    if 400 <= status_code < 500:
        return "client_error"
    return "unexpected_status"


def erp_failure_message(provider: str, status_code: int) -> str:
    """Build the PII-free ``ErpPostResult.message`` for a failed ERP call.

    One helper so the three real adapters can't drift back into interpolating
    ``resp.text``. ``provider`` is a fixed literal per adapter, never user input.
    """
    return f"{provider} post failed: HTTP {status_code} ({erp_failure_reason(status_code)})"


@dataclass
class ErpPostResult:
    """Outcome of an ``ErpAdapter.post_invoice`` call.

    ``message`` is **operator-facing and persisted** — ``services/erp.py`` turns
    a failure into ``RuntimeError(result.message)`` and stores it on the
    append-only audit row. Build it with :func:`erp_failure_message`; never
    interpolate the provider's response body into it.

    ``raw_response`` is in-memory only (no caller persists or logs it) and may
    hold the provider's parsed body for debugging in a REPL. Keep it that way —
    the moment something writes it to a log or a row, it needs the same
    treatment ``message`` gets.
    """

    success: bool
    erp_document_id: str | None = None
    erp_document_number: str | None = None
    message: str | None = None
    raw_response: dict | None = None


@dataclass
class PoLinePayload:
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    total: Decimal | None = None
    gl_account: str | None = None


@dataclass
class PoPayload:
    """Normalized purchase order returned by `ErpAdapter.list_pos`.

    `status` is one of {"open", "closed", "cancelled"} — the same set
    `PurchaseOrder.status` accepts. Adapters that get something exotic
    from their ERP should normalize to one of these or default to
    "open" rather than passing the raw vendor string through.
    """

    po_number: str
    vendor_name: str | None = None
    total: Decimal = Decimal("0")
    status: str = "open"
    # Promised / expected delivery date from the ERP, when the upstream PO
    # record carries one. Maps straight onto ``PurchaseOrder.expected_delivery_date``
    # (the on-time-delivery vendor sub-score signal). None when the ERP doesn't
    # supply a promised date — real adapters must NOT fabricate one; the mock
    # adapter emits a deterministic value so local-first dev exercises the path.
    expected_delivery_date: date | None = None
    line_items: list[PoLinePayload] = field(default_factory=list)
    # ISO 4217 code the ERP says `total` is denominated in. Maps onto
    # ``PurchaseOrder.currency`` through ``po_currency_code``. None when the ERP
    # record carries no code — real adapters must NOT fill in a default (a
    # default is a label nobody gave the figure; decisions §197), and the sync
    # then stores NULL, rendered bare.
    currency: str | None = None


@dataclass
class GLAccountPayload:
    """Normalized chart-of-accounts row returned by `ErpAdapter.list_gl_accounts`.

    `account_type` should normalize to one of {"asset", "liability",
    "equity", "revenue", "expense"} — the same vocabulary the rest of
    the app expects. Adapters can pass None when the upstream system
    doesn't classify the account; the API endpoint accepts that.
    `erp_account_id` is the upstream's canonical identifier so future
    pulls can detect renames.
    """

    code: str
    name: str
    account_type: str | None = None
    erp_account_id: str | None = None
    parent_code: str | None = None


@dataclass
class VendorPayload:
    """Normalized vendor record returned by `ErpAdapter.list_vendors`.

    Field shape mirrors what `services.vendor_sync.sync_vendors_from_erp`
    expects on each dict it's handed — `erp_vendor_id` and `name` are the
    only ones a real ERP is guaranteed to supply; the rest are optional
    and a missing one never nulls out an existing local value (see that
    function's docstring).
    """

    erp_vendor_id: str
    name: str
    code: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    tax_id: str | None = None
    payment_terms: str | None = None


class ErpAdapter:
    """Base class for ERP integrations. Subclass and implement all methods."""

    erp_type: str = "base"

    def __init__(self, config: dict):
        self.config = config

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        """Send an invoice to the ERP. Returns the ERP document ID."""
        raise NotImplementedError

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        """Poll the ERP for the current status of a posted invoice."""
        raise NotImplementedError

    async def void_invoice(self, erp_document_id: str) -> bool:
        """Request cancellation of a posted invoice."""
        raise NotImplementedError

    async def list_pos(self) -> list[PoPayload]:
        """Pull purchase orders from the ERP.

        Default implementation returns an empty list so adapters that
        don't yet support PO sync don't 500 the sync endpoint — the
        UI just reports "0 new POs". Adapters that *do* support it
        override this.
        """
        return []

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """Pull the chart of accounts from the ERP.

        Same contract as `list_pos`: default returns an empty list so
        an adapter that doesn't expose a chart endpoint just leaves the
        local chart untouched. The Auto GL Coding pipeline relies on
        this — the prompt only constrains AI suggestions when the org
        has synced a real chart.
        """
        return []

    async def list_vendors(self) -> list[VendorPayload]:
        """Pull vendors from the ERP.

        Same contract as `list_pos` / `list_gl_accounts`: default returns
        an empty list so an adapter that doesn't yet support vendor sync
        just reports "0 new" instead of 500ing the `/api/vendors/sync-erp`
        endpoint. Adapters that *do* support it override this.
        """
        return []

    async def test_connection(self) -> bool:
        """Verify the ERP connection is working."""
        raise NotImplementedError
