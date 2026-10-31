"""Base ERP adapter interface and shared data types."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.erp_call_budget import CallBudget


class ErpInvoiceStatus(enum.StrEnum):
    """What ``get_invoice_status`` read about a posted bill.

    ``paid`` is a claim that money left the customer: an adapter returns it only
    on the ERP's own paid status or a payment the ERP records against the bill,
    never on a zero balance alone (docs/decisions.md §303). A credit note, a
    write-off or a zero-total bill also leaves nothing owing, and recording one
    of those as a payment would book money that never moved.
    ``settled_no_payment`` is that case: nothing is owing, but the read shows no
    payment, so a caller must flag it for a person rather than record a
    payment. (QuickBooks' status sync-back reads bills through ``bill_report``,
    which judges the same question from the balance, §295.)
    """

    draft = "draft"
    open = "open"
    partially_paid = "partially_paid"
    paid = "paid"
    settled_no_payment = "settled_no_payment"
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
    #: The ERP's own id for ``gl_account`` (``gl_accounts.erp_account_id``),
    #: resolved from the invoice entity's chart. None when the line has no
    #: account or the account was never synced from this ERP.
    gl_account_erp_id: str | None = None


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
    #: The ERP's own vendor id (``vendors.erp_vendor_id``). Every real ERP posts
    #: a bill against this, never a name. **A direct adapter refuses a payload
    #: without it** (``ErpPostResult(success=False, message=...
    #: "vendor_not_linked")``) and never falls back to a name lookup, which
    #: picks the wrong "Acme" the first time two vendors share one.
    vendor_erp_id: str | None = None
    #: The ERP's own id for the header ``gl_account``; see ``LineItemPayload``.
    gl_account_erp_id: str | None = None
    line_items: list[LineItemPayload] = field(default_factory=list)
    #: The ERP background job an earlier attempt queued but could not confirm
    #: (:attr:`ErpPostResult.pending_job_id`, persisted by ``services/erp``).
    #: An adapter with asynchronous creates checks this job before queueing
    #: another, so a retry while the first is still running cannot post twice.
    pending_job_id: str | None = None
    #: The invoice dates earlier attempts of this push were sent with, when
    #: they differ from ``invoice_date`` (persisted by ``services/erp``). An
    #: adapter whose duplicate lookup narrows by date searches these too, so
    #: a bill an earlier attempt created is found after the date was edited.
    prior_invoice_dates: list[date] = field(default_factory=list)


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


#: The 4xx answers that can change on a re-send, and so stay retryable:
#:
#: * 401 — an expired or rotated token; each adapter refreshes and the next
#:   attempt carries a fresh one.
#: * 408 / 425 / 429 — timed out, too early, rate limited: the ERP said "later".
#: * 409 / 423 — a concurrent edit or a lock (Business Central answers a stale
#:   etag with 409); the next attempt re-reads.
#:
#: Every other 4xx is the ERP's answer to the bill itself (400 bad request,
#: 403 forbidden, 404, 422 validation): sending the same bill again gets the
#: same answer, only later and with another call against the ERP's quota.
_RETRYABLE_CLIENT_STATUSES = frozenset({401, 408, 409, 423, 425, 429})


def erp_status_retryable(status_code: int) -> bool:
    """Whether a failed ERP call that answered ``status_code`` may succeed if
    ``services/erp`` sends the same bill again (``ErpPostResult.retryable``).

    5xx and anything that is not an HTTP error at all stay retryable; a 4xx
    does only when it is one of :data:`_RETRYABLE_CLIENT_STATUSES`.
    """
    if 400 <= status_code < 500:
        return status_code in _RETRYABLE_CLIENT_STATUSES
    return True


def erp_http_failure(
    provider: str,
    status_code: int,
    *,
    erp_document_id: str | None = None,
    raw_response: dict | None = None,
) -> ErpPostResult:
    """The failed :class:`ErpPostResult` for an ERP call answered with
    ``status_code``: the PII-free :func:`erp_failure_message`, retryable only
    when :func:`erp_status_retryable` says a re-send could change the answer.

    Every adapter's HTTP failure goes through here, so none of them spends
    ``services/erp``'s backoff budget re-sending a bill the ERP refused.
    """
    return ErpPostResult(
        success=False,
        erp_document_id=erp_document_id,
        message=erp_failure_message(provider, status_code),
        raw_response=raw_response,
        retryable=erp_status_retryable(status_code),
    )


#: Stable reason code: the payload has no ``vendor_erp_id`` — the invoice's
#: vendor was never linked to a record in this ERP (run the vendor sync, or the
#: invoice has no resolved ``vendor_id`` at all). Every direct adapter refuses
#: such a payload before it makes a single HTTP call, and never falls back to
#: a name lookup.
VENDOR_NOT_LINKED = "vendor_not_linked"

#: Stable reason code: a line (or the header, for a header-only bill) is coded
#: to a GL account that has no ``gl_account_erp_id`` in the invoice's chart, or
#: — for an ERP whose bill line requires an account — is not coded at all.
#: Refused rather than posted against the code's text or dropped, since either
#: books the expense somewhere the approver never saw.
ACCOUNT_NOT_LINKED = "account_not_linked"

#: Stable reason code: an OAuth ERP with no usable connection (never connected,
#: revoked, or past its refresh lifetime). Retrying cannot help.
NOT_CONNECTED = "not_connected"

#: Stable reason code: the bill's lines do not add up to the approved
#: ``payload.amount`` (tax-inclusive, or tax-exclusive plus ``tax_amount``).
#: The header amount is never recomputed from lines, so the bill is refused.
AMOUNT_MISMATCH = "amount_mismatch"

#: Stable reason code: a line has neither a ``total`` nor a ``quantity`` and
#: ``unit_price`` to derive one from. Never filled from the header.
LINE_AMOUNT_MISSING = "line_amount_missing"

#: Stable reason code: the ERP computed the bill's total itself and it differs
#: from the approved ``payload.amount`` (a default tax code added tax, say).
POSTED_TOTAL_MISMATCH = "posted_total_mismatch"

#: Stable reason code: the ERP created the bill but did not report the total it
#: booked, so we cannot confirm it equals ``payload.amount``. Never success.
POSTED_TOTAL_UNCONFIRMED = "posted_total_unconfirmed"


def erp_refusal_message(provider: str, reason: str) -> str:
    """Build the PII-free ``ErpPostResult.message`` for a payload an adapter
    refused BEFORE calling the ERP.

    The sibling of :func:`erp_failure_message` for the pre-flight case: no HTTP
    status exists, so the message carries the provider literal and a stable
    reason code (:data:`VENDOR_NOT_LINKED`, :data:`ACCOUNT_NOT_LINKED`, or an
    adapter's own) and nothing from the payload — it is persisted on the same
    append-only ``invoice.erp_failed`` audit row. ``provider`` and ``reason``
    are fixed literals per call site, never user input.
    """
    return f"{provider} post refused: {reason}"


#: Every reason code an adapter can refuse a push with (:func:`erp_refusal`),
#: plus the two non-retryable post-create outcomes that carry a code too
#: (``posted_total.posted_total_failure`` / ``previous_bill_removed``). The
#: browser states each in the reader's language from this code
#: (``frontend/src/lib/api/erpRefusals.ts``), so a code missing here — or one
#: here with no message there — fails ``tests/test_erp_refusal_codes.py`` and
#: ``erpRefusals.test.ts``. A literal set rather than an import of each
#: adapter's constant: ``base`` is imported by every adapter, so it cannot
#: import them back.
ERP_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        # shared (`base`, `bill_allocation`, `posted_total`)
        "vendor_not_linked",
        "account_not_linked",
        "not_connected",
        "amount_mismatch",
        "line_amount_missing",
        "posted_total_mismatch",
        "posted_total_unconfirmed",
        "previous_bill_removed",
        "tax_not_itemised",
        "tax_rate_unresolved",
        "missing_dates",
        "duplicate_document_number",
        # QuickBooks Online
        "doc_number_too_long",
        "currency_unknown",
        "currency_not_enabled",
        # QuickBooks Online: coded from the Fault it answers a bill create with
        "period_closed",
        "vendor_inactive",
        "account_inactive",
        "reference_invalid",
        "company_subscription_inactive",
        "intuit_fault",
        # Sage Business Cloud Accounting
        "idempotency_lookup_incomplete",
        # Business Central
        "existing_invoice_not_open",
        "line_amount_precision",
        # SYSPRO / Sage Accounting ZA / Blackbaud FE NXT
        "duplicate_invoice_number",
        # Sage Accounting ZA
        "currency_not_supported",
        "foreign_currency_supplier",
        "correlation_total_mismatch",
        "tax_type_not_resolved",
        "tax_mismatch",
        "tax_not_stated",
        # Blackbaud FE NXT
        "subscription_key_missing",
        "ap_account_not_configured",
        "currency_not_configured",
        "currency_mismatch",
        "invoice_date_missing",
        "due_date_missing",
        "amount_not_positive",
        "job_unconfirmed",
        "job_failed",
        # `services/erp_call_budget`: the shared SKY call quota (decisions §322)
        "erp_call_budget_exhausted",
        # `services/erp` itself: the sealed ERP credentials could not be opened
        "erp_credentials_unavailable",
        # `services/erp_export_gate`: an open duplicate / fraud exception (§286)
        "exception_blocks_erp_export",
        # `services/erp_export_gate`: FeohLedger is already paying it (§309)
        "payment_blocks_erp_export",
        # `services/erp` itself: the invoice is due before it is dated
        "invoice_due_before_date",
        # A payment written back to the ERP (`post_payment`, decisions §295)
        "payment_account_not_mapped",
        "bill_not_in_erp",
        "payment_exceeds_balance",
        "bill_payment_voided",
    }
)


def erp_refusal(provider: str, reason: str) -> ErpPostResult:
    """The non-retryable :class:`ErpPostResult` for a payload refused pre-flight.

    Carries ``reason`` and ``provider`` as data as well as in ``message``, so
    ``services/erp`` can record the refusal as a code the browser localizes
    rather than only as the English sentence.
    """
    return ErpPostResult(
        success=False,
        message=erp_refusal_message(provider, reason),
        retryable=False,
        refusal_code=reason,
        refusal_provider=provider,
    )


def erp_preflight_refusal(provider: str, reason: str) -> ErpPostResult:
    """:func:`erp_refusal` for a refusal made before the adapter's first
    network call, which therefore left nothing in the ERP
    (``ErpPostResult.not_posted``). Use it ONLY there: a refusal after any
    request — a lookup that found our own earlier bill, a lost read-back — may
    stand beside a document, and must stay the fail-closed :func:`erp_refusal`.
    """
    result = erp_refusal(provider, reason)
    result.not_posted = True
    return result


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
    #: False when retrying cannot change the outcome — the adapter refused the
    #: payload before calling the ERP (:func:`erp_refusal`). ``services/erp``
    #: then fails the invoice at once instead of spending its backoff budget
    #: re-sending the same refused bill.
    retryable: bool = True
    #: The id of an ERP background job this attempt queued but could not see
    #: finish. ``services/erp`` persists it and hands it back as
    #: :attr:`InvoicePayload.pending_job_id` on the next attempt; None clears it.
    pending_job_id: str | None = None
    #: The stable reason code of a non-retryable refusal (one of
    #: :data:`ERP_REFUSAL_CODES`) and the provider literal that refused it.
    #: Set by :func:`erp_refusal` and the ``posted_total`` outcomes; None for an
    #: HTTP failure, whose code lives in ``message`` (:func:`erp_failure_message`).
    refusal_code: str | None = None
    refusal_provider: str | None = None
    #: Extra PII-free params of the refusal beyond ``provider`` — the numeric
    #: code the ERP answered with (``intuit_code``), the GL ``accounts`` it
    #: named. ``services/erp`` merges them into the recorded refusal's params.
    refusal_params: dict | None = None
    #: True only when the adapter KNOWS this attempt left no document in the
    #: ERP — the ERP's own create answered a deterministic refusal (a 4xx that
    #: names why: books closed, an inactive vendor). ``services/erp`` then does
    #: not count the attempt as one whose outcome is unknown. Never set it on a
    #: path that may have created something first (a failed read-back, a
    #: timeout, a 5xx): the default False is the fail-closed answer.
    not_posted: bool = False
    #: For :meth:`ErpAdapter.post_payment`: what the ERP still shows open on
    #: the bill after this payment (None = not read). A positive figure means
    #: the bill stays partly open there — a discount or credit taken in
    #: FeohLedger that the ERP does not know about (``payment_erp_sync``).
    bill_balance_after: Decimal | None = None


#: Stable reason code: a payment written back to the ERP (``post_payment``)
#: needs the ERP account it was paid from, and the org has mapped none for the
#: payment's rail (``settings.erp.bill_payment_bank_account`` /
#: ``bill_payment_card_account``, GL codes), or the code is not linked to an
#: account in the connected company. Never guessed.
PAYMENT_ACCOUNT_NOT_MAPPED = "payment_account_not_mapped"

#: Stable reason code: the bill the payment would be applied to is not (or no
#: longer) in the ERP — deleted there, or never confirmed.
BILL_NOT_IN_ERP = "bill_not_in_erp"

#: Stable reason code: the payment is larger than what the ERP still shows
#: open on the bill. Posting it would overpay the bill in the ERP.
PAYMENT_EXCEEDS_BALANCE = "payment_exceeds_balance"

#: Stable reason code: the ERP payment this payment id already created was
#: voided in the ERP by someone there. Posting it again would overrule them,
#: so the write-back stops and a person decides.
BILL_PAYMENT_VOIDED = "bill_payment_voided"


class ErpBillState(enum.StrEnum):
    """Where a bill stands in the ERP, as the status sync reads it (§295)."""

    open = "open"
    partially_paid = "partially_paid"
    paid = "paid"
    #: Gone from the ERP: deleted, or voided where the ERP voids.
    deleted = "deleted"
    unknown = "unknown"


@dataclass
class ErpBillReport:
    """One bill as the ERP reports it, for ``services/erp_status_sync``.

    PII-free by construction: ids, a state, a date and a rail. Never the
    vendor, the memo text, or an amount beyond what decides the state.
    """

    bill_id: str
    state: ErpBillState
    #: The correlation id our ``post_invoice`` wrote onto the bill (QuickBooks:
    #: the ``PrivateNote`` marker), or None when the bill carries none — a bill
    #: someone keyed in the ERP by hand, or one we can no longer read.
    correlation_id: str | None = None
    #: The date of the latest payment the ERP applied that FeohLedger did NOT
    #: post itself, and that payment's rail as a ``Payment.method`` value
    #: (None = not stated; it then counts as 1099-reportable).
    paid_on: date | None = None
    payment_method: str | None = None
    #: The ERP's own ids of those payments (``external``), and whether every
    #: payment on the bill is one FeohLedger posted (``post_payment``). A bill
    #: paid only by our own write-back is not news to us.
    external_payment_ids: tuple[str, ...] = ()
    paid_only_by_us: bool = False
    #: True when the bill was taken down by MORE than FeohLedger's own
    #: write-backs applied to it — judged from the balance, so a vendor credit
    #: or a journal entry counts as well as a person's payment. This, not the
    #: payment list, is what says "settled outside FeohLedger".
    reduced_outside: bool = False
    #: True when that outside reduction was (at least partly) a vendor credit,
    #: or nothing readable paid it in cash: the bill was settled without the
    #: money leaving, so it must not be booked as a cash payment.
    settled_by_credit: bool = False


@dataclass
class PaymentPayload:
    """A FeohLedger payment written back to the ERP against its bill (§295)."""

    #: Our ``Payment.id`` — the idempotency key (QuickBooks ``requestid`` and
    #: the marker in the bill payment's ``PrivateNote``).
    payment_id: str
    #: The ERP bill the invoice was posted as (``erp_reference``).
    bill_id: str
    amount: Decimal
    currency: str
    paid_on: date
    #: The payment's rail (``Payment.method``).
    method: str | None = None
    #: True when the rail is a payment card (``payment_methods.
    #: CARD_PAYMENT_METHODS``): booked from a credit-card account, not a bank.
    card: bool = False
    #: The ERP's id for the account the payment is booked from — the org's
    #: mapped bank or card account (``settings.erp.bill_payment_bank_account``
    #: / ``bill_payment_card_account``, our GL codes) resolved through the
    #: chart under the current connection realm. None = not mapped or not
    #: linked; the adapter refuses ``payment_account_not_mapped``.
    pay_from_account_erp_id: str | None = None
    vendor_erp_id: str | None = None
    #: The remittance reference, when the rail has one (a cheque number).
    reference: str | None = None


@dataclass
class PoLinePayload:
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    total: Decimal | None = None
    gl_account: str | None = None
    #: The ERP's item number / SKU for the line, when it has one. Stored as the
    #: PO line's ``sku`` snapshot (migration 0111) for the commerce sync.
    item_code: str | None = None


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
    # The supplier's id in the ERP — the same id `list_vendors` reports as
    # `VendorPayload.erp_vendor_id`. The sync links the PO to the vendor
    # holding it before falling back to `vendor_name`, which a rename on either
    # side breaks. None when the adapter does not read it.
    vendor_erp_id: str | None = None
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
    # Numbers an earlier build of this adapter stored the same PO under (Merge
    # keyed a PO by its record id before it read `purchase_order_number`). When
    # no row holds `po_number` yet, the sync re-keys the row found under one of
    # these instead of creating a duplicate the old row's total is summed
    # beside (decisions §328).
    legacy_po_numbers: tuple[str, ...] = ()


class PoSkipReason(enum.StrEnum):
    """Why an adapter's PO listing left a purchase order out (``SkippedPo``).

    Coded, never the ERP's own text. Every reason means the same thing to the
    sync: the ERP has this PO, and the adapter could not state its total
    exactly, so whatever total FeohLedger holds for that number may no longer
    be the ERP's (decisions §323).
    """

    #: The total cannot be computed exactly from what the ERP returned (a
    #: discount, a price unit of measure, a charge or price of unknown
    #: currency, a missing price, no total field).
    unpriceable = "unpriceable"
    #: The PO's lines could not be read whole.
    lines_unread = "lines_unread"
    #: The ERP returned the header and no lines.
    no_lines = "no_lines"
    #: The PO is labelled with a currency the adapter cannot place against the
    #: company's local currency, because that could not be read.
    local_currency_unknown = "local_currency_unknown"
    #: Stamped by the sync, not an adapter: the ERP amended lines goods were
    #: already received against, and a received line has no ERP line it can be
    #: paired with, so the stored lines cannot be refreshed safely.
    lines_changed = "lines_changed"


@dataclass(frozen=True)
class SkippedPo:
    """A PO the ERP has and ``list_pos`` deliberately did not return."""

    po_number: str
    reason: PoSkipReason


@dataclass
class PoListing:
    """``ErpAdapter.list_pos_report``: the POs, and the ones left out.

    ``skipped`` is what lets the sync tell "the ERP no longer reports a usable
    total for this PO" apart from "this PO was never here": without it a PO the
    adapter starts skipping keeps the total an earlier sync stored, and the PO
    match keeps trusting it.
    """

    pos: list[PoPayload]
    skipped: list[SkippedPo] = field(default_factory=list)


@dataclass
class GLAccountPayload:
    """Normalized chart-of-accounts row returned by `ErpAdapter.list_gl_accounts`.

    `account_type` should normalize to one of {"asset", "liability",
    "equity", "revenue", "expense"} — the same vocabulary the rest of
    the app expects. Adapters can pass None when the upstream system
    doesn't classify the account; the API endpoint accepts that.
    `erp_account_id` is the upstream's canonical identifier so future
    pulls can detect renames.

    **`code` must be a key the ERP itself keeps unique** across the whole
    chart — an account number, a fully-qualified path, the record id — never
    a display name the ERP lets two accounts share. The sync refuses any code
    two accounts in one pull resolve to rather than link either
    (`services/gl_account_sync`, docs/decisions.md §284), so a repeatable key
    leaves accounts unlinked; it never merges them.

    `legacy_codes` are codes an earlier version of the adapter gave the same
    account (QuickBooks' bare `Name`, before §284). Only the sync's ambiguity
    check reads them: an existing row still keyed by one of them that two
    accounts claim is unlinked rather than trusted.
    """

    code: str
    name: str
    account_type: str | None = None
    erp_account_id: str | None = None
    parent_code: str | None = None
    #: The parent's ``erp_account_id``, for an adapter that knows it. The sync
    #: then names the parent by OUR code for that account — the row linked to
    #: it — rather than the ERP's, which differ whenever a row kept a code the
    #: ERP has since changed (a numbered parent: ours "Office Expenses", the
    #: ERP's "6020"). ``parent_code`` is the fallback for a parent we hold no
    #: row for.
    parent_erp_account_id: str | None = None
    #: True when the adapter states this account's parentage outright, so a
    #: None parent means "top-level" and the sync CLEARS a stale one. False
    #: (the default) reads None as "not reported" and leaves ours alone.
    reports_parent: bool = False
    legacy_codes: tuple[str, ...] = ()
    #: False for an account the ERP has made inactive. An adapter EMITS such
    #: accounts rather than dropping them: their ids are still the ERP's, so a
    #: row linked to one is not "dead", and the sync marks the row inactive in
    #: our chart (it never creates a row for one).
    active: bool = True


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
    #: False for a vendor the ERP has made inactive — emitted, not dropped, for
    #: the same reason as ``GLAccountPayload.active``. The sync never creates
    #: or links a payee for one, and marks a linked vendor ``inactive``.
    active: bool = True
    #: A value an earlier version of the adapter wrote into ``code`` that was
    #: never our identifier (QuickBooks' ``AcctNum``: our account number AT
    #: the vendor). The sync clears it from an ERP-created vendor's ``code``.
    legacy_code: str | None = None


#: How many rows a chart or vendor listing reads before it gives up as
#: incomplete (an adapter whose own page cap already reaches further, such as
#: QuickBooks' 50 x 1000, keeps it). Generous on purpose: past it the listing
#: RAISES (:class:`ErpListingIncomplete`) instead of returning the first N rows,
#: so it has to sit above any chart or vendor master a sync can sensibly read
#: in one request.
SYNC_LISTING_MAX_ROWS = 10_000


class ListingGap(enum.StrEnum):
    """Why a listing could not be proven complete."""

    #: A page (the first or a later one) could not be read.
    fetch_failed = "fetch_failed"
    #: The walk stopped at :data:`SYNC_LISTING_MAX_ROWS` with more to read.
    truncated = "truncated"


class ErpListingIncomplete(RuntimeError):
    """``list_gl_accounts`` / ``list_vendors`` could not read the whole collection.

    The chart and vendor syncs read ABSENCE as evidence — a link whose id is not
    in the pull, or whose id now holds something else, is dropped
    (``services/gl_account_sync``, ``services/vendor_sync``) — and they decide
    whether a code or a name is unique by counting it in the pull. Both are
    only true of the whole collection. A listing that returned what it had read
    before a failure, or the first N rows of a longer one, would unlink every
    good link the missing pages held and could link a name that is not unique
    (docs/decisions.md §284). So a listing either returns everything or raises
    this, and the sync endpoints answer 502 without syncing anything.

    An adapter's ``list_pos_report`` raises it too: nothing judges a link by a
    PO's absence, but its ``skipped`` report covers only the part it read, so a
    failed or capped read would leave a no-longer-priceable PO's stored total
    trusted (decisions §323).

    PII-free by construction: the provider literal, the collection and the
    :class:`ListingGap` — never a response body, URL or credential.
    """

    def __init__(self, provider: str, collection: str, gap: ListingGap):
        self.provider = provider
        self.collection = collection
        self.gap = gap
        super().__init__(f"{provider} {collection} listing incomplete: {gap.value}")


#: Stable reason code: the adapter refused to spend an API call because the
#: org's share of a shared provider quota is used, the whole subscription's
#: quota is, or the provider asked us to slow down (``services/erp_call_budget``,
#: decisions §322). ``params.reason`` says which and ``params.retry_after`` when
#: (ISO-8601 UTC). Nothing was sent.
ERP_CALL_BUDGET_EXHAUSTED = "erp_call_budget_exhausted"


class ErpCallBudgetExhausted(RuntimeError):
    """An ERP read refused before its call by ``services/erp_call_budget``.

    Raised by a sync listing or status read; a push returns the coded
    :data:`ERP_CALL_BUDGET_EXHAUSTED` result instead. PII-free: the provider
    literal, a reason word and a UTC timestamp.
    """

    def __init__(self, provider: str, reason: str, retry_after: datetime):
        self.provider = provider
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(f"{provider} call budget exhausted: {reason}")


def sync_listing_pages(page_size: int) -> int:
    """Pages a listing of ``page_size`` rows a page may read (rounded up)."""
    return -(-SYNC_LISTING_MAX_ROWS // page_size)


class ErpAdapter:
    """Base class for ERP integrations. Subclass and implement all methods."""

    erp_type: str = "base"
    #: True for an adapter whose bills' payment status flows back to us
    #: (``bill_report`` / ``changed_bills``) AND whose payments we write back
    #: (``post_payment``). Only then may a confirmed push leave the invoice
    #: payable at ``posted_in_erp`` (``services/erp_status_sync``, §295).
    supports_status_sync: bool = False

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

    def call_budget(self) -> CallBudget | None:
        """The shared-quota budget this connection's calls are counted
        against (``services/erp_call_budget``, decisions §322), or None for an
        ERP whose API quota is not shared across tenants. Read by the
        connection status endpoint to show the org its usage (§324).
        """
        return None

    #: Placeholder PO numbers an earlier build of this adapter wrote for POs it
    #: could not number, several POs into one row. The sync marks such a row
    #: unverifiable so its merged total is never matched or summed (§328).
    retired_po_numbers: tuple[str, ...] = ()

    async def list_pos(self) -> list[PoPayload]:
        """Pull purchase orders from the ERP.

        Default implementation returns an empty list so adapters that
        don't yet support PO sync don't 500 the sync endpoint — the
        UI just reports "0 new POs". Adapters that *do* support it
        override this.
        """
        return []

    async def list_pos_report(self) -> PoListing:
        """:meth:`list_pos` plus the POs it deliberately left out.

        What the PO sync calls. The default wraps :meth:`list_pos` and reports
        nothing skipped, so an adapter that never skips a PO needs no change.
        An adapter that DOES leave a PO out because it cannot state its total
        exactly overrides this and names each one (:class:`SkippedPo`), so the
        sync can mark a total synced earlier as no longer verifiable instead of
        leaving it for the PO match to trust (decisions §323). Such an adapter
        keeps :meth:`list_pos` returning ``(await self.list_pos_report()).pos``.
        """
        return PoListing(pos=await self.list_pos())

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """Pull the WHOLE chart of accounts from the ERP, or raise.

        An adapter that reads a chart returns every account (inactive ones
        included, ``active=False``) or raises — :class:`ErpListingIncomplete`
        for a page it could not read or a chart past
        :data:`SYNC_LISTING_MAX_ROWS` — never what it had read so far: the
        sync reads absence as evidence (see that class).

        The default returns an empty list so an adapter that doesn't expose a
        chart endpoint leaves the local chart untouched; the sync treats an
        empty pull as proving nothing and changes no link on it.
        """
        return []

    async def list_vendors(self) -> list[VendorPayload]:
        """Pull EVERY vendor from the ERP, or raise.

        Same contract as :meth:`list_gl_accounts`: an adapter that reads
        vendors returns all of them (inactive ones ``active=False``) or raises
        :class:`ErpListingIncomplete`, and skips a record with no id of the
        kind its ``post_invoice`` posts against — never substituting a number
        or a name for it. The default returns an empty list, on which the sync
        changes no link.
        """
        return []

    async def test_connection(self) -> bool:
        """Verify the ERP connection is working."""
        raise NotImplementedError

    # -- status sync-back + payment write-back (decisions §295) -------------
    #
    # Every caller checks ``supports_status_sync`` first, and an adapter without
    # it keeps the old behaviour (the push closes the invoice at ``done``;
    # ``payment_erp_sync`` writes nothing back). The defaults below are
    # therefore the honest answer of an ERP that does not report back, and each
    # is fail-closed if anything ever reached it anyway: a write-back is
    # refused (never "posted"), a bill reads ``unknown`` (never ``open``, so
    # the dispatch-time check would refuse, not pay), and no changes or linked
    # bills are claimed. Not NotImplementedError: these adapters are not
    # skeletons — status sync is an optional capability, like the payment
    # adapters' (``tests/test_adapter_contract_integrity.py``).

    def _no_status_sync(self) -> ErpPostResult:
        return ErpPostResult(
            success=False,
            message=f"{self.erp_type} does not support payment write-back",
            retryable=False,
        )

    async def post_payment(self, payload: PaymentPayload) -> ErpPostResult:
        """Apply a FeohLedger payment to its bill in the ERP, idempotently on
        ``payload.payment_id``: a retry never creates a second ERP payment.
        Default: refused — this ERP takes no write-back."""
        return self._no_status_sync()

    async def void_payment(self, payload: PaymentPayload) -> ErpPostResult:
        """Reverse what :meth:`post_payment` wrote for ``payload.payment_id``.
        Success when nothing of ours is left on the bill. Default: refused."""
        return self._no_status_sync()

    async def bill_report(self, bill_id: str) -> ErpBillReport:
        """Where ``bill_id`` stands now. ``deleted`` only on the ERP's positive
        answer; an unreadable bill raises, never reads as gone. Default:
        ``unknown`` — never ``open``, so nothing is paid on its say-so."""
        return ErpBillReport(bill_id=str(bill_id), state=ErpBillState.unknown)

    async def bills_for_payment(self, payment_id: str) -> list[str]:
        """The bills an ERP payment record is applied to (for a webhook that
        names the payment, not the bill). Default: none known."""
        return []

    async def changed_bills(self, since: datetime) -> list[str]:
        """Every bill changed since ``since`` — directly, or through a payment
        applied to it — the change-data-capture backstop for lost webhooks.
        Raises rather than return a partial list. Default: none reported."""
        return []
