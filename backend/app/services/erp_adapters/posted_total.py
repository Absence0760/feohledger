"""Confirming the total an ERP booked for a bill whose total it computes itself.

QuickBooks Online (``TotalAmt``), Xero (``Total``), Sage Business Cloud
Accounting v3.1 (``total_amount``) and Sage Accounting ZA (``Total``) derive a
bill's total from its lines, and a default tax code on an account can make that
differ from what we sent. ``payload.amount`` is the approved figure a payment
run pays, so a created bill counts as posted only when the ERP's own total is
exactly that amount:

* equal → ``None`` (the caller reports success);
* different → non-retryable ``posted_total_mismatch``, after voiding the bill
  just created where the ERP allows it (the message says whether it did);
* not reported → non-retryable ``posted_total_unconfirmed``. Never success: we
  cannot tell what was booked. A manual retry re-finds the bill through the
  adapter's idempotency lookup, which applies this same check to it.

Messages carry the provider literal and a stable reason only (they land on the
append-only ``invoice.erp_failed`` audit row).
"""

from __future__ import annotations

from decimal import Decimal

from app.services.erp_adapters.base import (
    POSTED_TOTAL_MISMATCH,
    POSTED_TOTAL_UNCONFIRMED,
    ErpAdapter,
    ErpPostResult,
    InvoicePayload,
)


async def check_posted_total(
    adapter: ErpAdapter,
    provider: str,
    payload: InvoicePayload,
    *,
    posted_total: Decimal | None,
    document_id: str | None,
    document_number: str | None = None,
    raw_response: dict | None = None,
) -> ErpPostResult | None:
    """None when the ERP booked exactly ``payload.amount``; else the failure."""
    if posted_total is not None and posted_total == payload.amount:
        return None
    if posted_total is None:
        return ErpPostResult(
            success=False,
            erp_document_id=document_id,
            erp_document_number=document_number,
            message=f"{provider} post unconfirmed: {POSTED_TOTAL_UNCONFIRMED} "
            f"(the bill was created but {provider} reported no total)",
            raw_response=raw_response,
            retryable=False,
        )
    voided = False
    if document_id:
        try:
            voided = await adapter.void_invoice(document_id)
        except Exception:
            # Not swallowed: the message below says the bill was not voided.
            voided = False
    outcome = "the bill was voided" if voided else f"the bill could not be voided in {provider}"
    return ErpPostResult(
        success=False,
        erp_document_id=document_id,
        erp_document_number=document_number,
        message=f"{provider} post failed: {POSTED_TOTAL_MISMATCH} ({outcome})",
        raw_response=raw_response,
        retryable=False,
    )
