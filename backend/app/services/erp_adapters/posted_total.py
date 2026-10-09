"""Confirming the total an ERP booked for a bill whose total it computes itself.

QuickBooks Online (``TotalAmt``), Xero (``Total``), Sage Business Cloud
Accounting v3.1 (``total_amount``) and Sage Accounting ZA (``Total``) derive a
bill's total from its lines, and a default tax code on an account can make that
differ from what we sent. ``payload.amount`` is the approved figure a payment
run pays, so a created bill counts as posted only when the ERP's own total is
exactly that amount:

* equal → ``None`` (the caller reports success);
* different → non-retryable ``posted_total_mismatch``, after voiding the bill
  just created where the ERP allows it. The message says which of three things
  happened (:class:`VoidOutcome`): the bill was voided now, it could not be
  voided, or the ERP already had it deleted / voided;
* not reported → non-retryable ``posted_total_unconfirmed``. Never success: we
  cannot tell what was booked. A manual retry re-finds the bill through the
  adapter's idempotency lookup, which applies this same check to it.

Messages carry the provider literal and a stable reason only (they land on the
append-only ``invoice.erp_failed`` audit row).

**Replayed creates.** QuickBooks (``requestid``) and Xero (``Idempotency-Key``)
replay the original create response when a key repeats. Once a
``posted_total_mismatch`` has removed the bill, an operator's retry under the
same key gets "created" back for a bill that no longer exists — and if that
replayed total happened to match, the push would report success against a
deleted bill. So those adapters read the bill back after every create, and only
when the ERP *positively* reports it deleted / voided do they create again under
the next key of a deterministic sequence (:func:`create_attempt_key`).

Moving to a fresh key is duplicate-safe for two reasons: the bill the old key
named is confirmed gone, so the new bill is the only live one; and every
concurrent or later retry walks the same sequence, so the ERP's own key dedupe
still collapses them onto one bill. Anything short of a positive "gone" — an
error, a 404, a bill we cannot read — fails the push (retryably) instead of
advancing. Refusing outright (``previous_bill_removed`` on the first removed
bill) was rejected: the correlation id is the invoice's for life, so that
refusal would make the invoice unpostable for good, even after the operator
fixed the tax code that caused the mismatch. :data:`MAX_CREATE_ATTEMPTS` bounds
the walk; past it the push fails non-retryably with
:data:`PREVIOUS_BILL_REMOVED`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from enum import StrEnum

from app.services.erp_adapters.base import (
    POSTED_TOTAL_MISMATCH,
    POSTED_TOTAL_UNCONFIRMED,
    ErpAdapter,
    ErpPostResult,
    InvoicePayload,
)

#: Stable reason code: every key of the create sequence named a bill the ERP
#: reports deleted / voided (each an earlier attempt that was cleaned up), and
#: the walk is exhausted. Non-retryable: it needs a person to look at the ERP.
PREVIOUS_BILL_REMOVED = "previous_bill_removed"

#: How many idempotency keys one push may walk. Each removed bill is one
#: earlier push that failed ``posted_total_mismatch``; ten failed attempts on
#: one invoice is past the point of retrying blind.
MAX_CREATE_ATTEMPTS = 10


class VoidOutcome(StrEnum):
    """What happened to a bill whose booked total did not match."""

    #: Voided / deleted by this call.
    VOIDED = "voided"
    #: Still live in the ERP (money applied, the call failed, unreadable, …).
    NOT_VOIDED = "not_voided"
    #: The ERP already reports it deleted or voided; nothing was done.
    ALREADY_GONE = "already_gone"


def create_attempt_key(correlation_id: str, attempt: int, max_len: int) -> str:
    """The idempotency key for create attempt ``attempt`` (1-based).

    Attempt 1 is the correlation id itself (cut to ``max_len``), so a key sent
    before this sequence existed still dedupes against its replay. Later
    attempts append ``#r<n>`` and cut the id, never the suffix, so no two
    attempts share a key.
    """
    if attempt <= 1:
        return correlation_id[:max_len]
    suffix = f"#r{attempt}"
    return correlation_id[: max_len - len(suffix)] + suffix


def previous_bill_removed(provider: str) -> ErpPostResult:
    """The non-retryable failure for an exhausted create-key walk."""
    return ErpPostResult(
        success=False,
        message=f"{provider} post failed: {PREVIOUS_BILL_REMOVED} (every earlier bill "
        f"for this invoice was deleted or voided in {provider})",
        retryable=False,
    )


def posted_total_failure(
    provider: str,
    outcome: VoidOutcome,
    *,
    document_id: str | None,
    document_number: str | None,
    raw_response: dict | None = None,
) -> ErpPostResult:
    """The non-retryable ``posted_total_mismatch`` result for ``outcome``."""
    if outcome is VoidOutcome.VOIDED:
        detail = "the bill was voided"
    elif outcome is VoidOutcome.ALREADY_GONE:
        detail = f"the bill was already deleted or voided in {provider}"
    else:
        detail = f"the bill could not be voided in {provider}"
    return ErpPostResult(
        success=False,
        erp_document_id=document_id,
        erp_document_number=document_number,
        message=f"{provider} post failed: {POSTED_TOTAL_MISMATCH} ({detail})",
        raw_response=raw_response,
        retryable=False,
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
    void_bill: Callable[[str], Awaitable[VoidOutcome]] | None = None,
) -> ErpPostResult | None:
    """None when the ERP booked exactly ``payload.amount``; else the failure.

    ``void_bill`` reports the three-way :class:`VoidOutcome`; an adapter that
    can tell "already gone" from "could not void" passes it. Without it, the
    adapter's boolean ``void_invoice`` decides (True → voided, else not).
    """
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
    outcome = VoidOutcome.NOT_VOIDED
    if document_id:
        try:
            if void_bill is not None:
                outcome = await void_bill(document_id)
            elif await adapter.void_invoice(document_id):
                outcome = VoidOutcome.VOIDED
        except Exception:
            # Not swallowed: the message below says the bill was not voided.
            outcome = VoidOutcome.NOT_VOIDED
    return posted_total_failure(
        provider,
        outcome,
        document_id=document_id,
        document_number=document_number,
        raw_response=raw_response,
    )
