"""The ERP push refuses an invoice whose liability is itself in question.

Approval gates on no payment-blocking exception, so an approved invoice can
still carry an open ``duplicate`` or ``fraud_flag``. Our payment runs refuse
it, but the ERP push used to post it anyway: the bill then lived in the ERP's
books, where a bookkeeper can pay it directly and our run gate never sees it.
The push now refuses while one of ``api/payments.ERP_EXPORT_BLOCKING_EXCEPTION_TYPES``
is open or escalated (decisions §286).

Two places ask, with one predicate (``payment_runs.blocking_exception_types``):

* ``services/erp._call_erp``, directly before the adapter: every push passes
  through it (manual send, retry, the workflow's own advance, the local
  dispatcher and the Lambda worker), and it re-reads on every attempt, so a
  flag raised between retries stops the next one. It raises
  ``ErpPostRefusedError`` with :func:`blocked_export_refusal`, which lands the
  invoice at ``failed`` without spending the retry budget. When an earlier
  attempt's outcome is unknown (``erp.possibly_posted``: a timeout or 5xx the
  ERP may have applied), the refusal and the audit row say
  ``possibly_posted: true``, since that bill would already be payable there.
* the three HTTP doors in ``api/workflow.py``, before they move the invoice to
  ``sending_to_erp``, so the user gets a 409 saying what to do instead of a
  queued push that fails a moment later, and the invoice stays where it was.

Clearing the flag goes through ``exception_lifecycle.record_decision``, whose
segregation check refuses anyone implicated in the invoice. Nothing here
bypasses that: the gate only reads the exceptions.

**FeohLedger already paying the invoice refuses the push too** (decisions
§309). Staging a payment run books a live ``Payment`` (``pending``; an
``exported`` NACHA file is the same) and minting a virtual card books a live
``VirtualCard``, and neither moves the invoice off ``approved``. A bill pushed
then is payable in the ERP as well, so it could be paid twice: once by our
rail and once from the ERP. The two rails already refuse each other through
``payment_runs.live_payment_invoices`` and ``card_issuance.live_card_invoice_ids``;
:func:`erp_export_live_claim` asks the same two predicates, so "live" has one
definition (``api/payments.LIVE_PAYMENT_TERMINAL_STATUSES`` and the card slot
index). It runs at the same two places, after the exception check, and
:func:`erp_export_refusal` is the one answer both take.

**A vendor the payment path will not pay refuses the push too** when paying
it from the ERP would get round our screening (§286's reasoning applied to the
payee rather than the debt). :func:`erp_export_vendor_hold` reads the payment
path's own vendor read, ``payment_runs.vendor_payability``, and decides per
status what may still be booked:

* the sticky ``payments_blocked`` flag (a sanctions match or a manual block)
  and a last stored screen of ``match`` / ``review`` refuse: those are the
  verdicts dispatch's live pre-payment screen refuses or holds on, and a bill
  in the ERP is paid without any screen. The gate reads the STORED screen and
  never calls the adapter, so a stale ``review`` is cleared by re-screening
  the vendor (``POST /api/vendors/{id}/screen``), which the 409 says;
* ``rejected`` (an invalid or duplicate vendor record) refuses, and so does a
  status this build does not know: the bill would book a liability to a payee
  AP has declared invalid;
* ``unverified`` and ``inactive`` still book. The approver signed a real debt;
  verification is a pre-PAYMENT control (the run gate still holds the payment
  here), and the ERP keeps its own vendor record and its own inactive rule.

It runs at the same two places, after the live-claim check, with the coded
refusal ``vendor_blocks_erp_export`` and ``params = {hold}``.

Every string here is PII-free: an exception type, a claim and a hold are fixed
literals, never a vendor field.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

#: The refusal code, registered in ``erp_adapters/base.ERP_REFUSAL_CODES`` so
#: the invoice modal states it (``frontend/src/lib/api/erpRefusals.ts``) and in
#: the coded-refusal registry the 409 is localized by (``codedRefusals.ts``).
EXCEPTION_BLOCKS_ERP_EXPORT = "exception_blocks_erp_export"

#: The refusal code when FeohLedger is already paying the invoice, registered
#: in the same two places. Its ``claim`` param names the rail that holds it.
PAYMENT_BLOCKS_ERP_EXPORT = "payment_blocks_erp_export"

#: The ``claim`` values: a live ``Payment`` (a staged or exported run, an
#: executing or settled payment) or a live ``VirtualCard``.
CLAIM_PAYMENT = "payment"
CLAIM_VIRTUAL_CARD = "virtual_card"

#: The refusal code when the invoice's vendor may not be paid in a way the ERP
#: would get round, registered in the same two places. Its ``hold`` param says
#: which.
VENDOR_BLOCKS_ERP_EXPORT = "vendor_blocks_erp_export"

#: The vendor lifecycle statuses whose approved bill may still be booked. Named
#: rather than the refused ones, so a status added later refuses until someone
#: decides otherwise (the run gate's ``PAYABLE_VENDOR_STATUS`` rule).
ERP_BOOKABLE_VENDOR_STATUSES: frozenset[str] = frozenset({"active", "unverified", "inactive"})

#: The ``hold`` values beyond the screening gate's own
#: (``payment_runs.HOLD_PAYMENTS_BLOCKED`` / ``HOLD_SANCTIONS_MATCH`` /
#: ``HOLD_SANCTIONS_REVIEW``).
HOLD_VENDOR_REJECTED = "vendor_rejected"
HOLD_VENDOR_STATUS_UNKNOWN = "vendor_status_unknown"


async def erp_export_blocking_exception(db: AsyncSession, invoice_id: uuid.UUID) -> str | None:
    """The type of the open or escalated exception that refuses this push, or None.

    When an invoice carries both, the one earliest in
    ``ERP_EXPORT_BLOCKING_EXCEPTION_TYPES`` is named.
    """
    from app.api.payments import ERP_EXPORT_BLOCKING_EXCEPTION_TYPES
    from app.services.payment_runs import blocking_exception_types

    blocked = await blocking_exception_types(
        db, [invoice_id], types=ERP_EXPORT_BLOCKING_EXCEPTION_TYPES
    )
    return blocked.get(invoice_id)


#: Appended to the English when an earlier attempt may have created the bill.
_POSSIBLY_POSTED_NOTE = "a bill from an earlier attempt may already be in the ERP"


def blocked_export_message(exception_type: str, *, possibly_posted: bool = False) -> str:
    """The English ``error`` on the ``invoice.erp_failed`` row."""
    message = f"ERP post refused: {EXCEPTION_BLOCKS_ERP_EXPORT} ({exception_type})"
    return f"{message}; {_POSSIBLY_POSTED_NOTE}" if possibly_posted else message


def blocked_export_params(exception_type: str, *, possibly_posted: bool = False) -> dict:
    """The refusal's params. ``possibly_posted`` appears only when true (an
    earlier attempt's outcome is unknown, ``erp.possibly_posted``), so a
    first-attempt refusal keeps the shape it always had."""
    params: dict = {"exception_type": exception_type}
    if possibly_posted:
        params["possibly_posted"] = True
    return params


def blocked_export_refusal(exception_type: str, *, possibly_posted: bool = False) -> dict:
    """The coded ``refusal`` beside it, in ``erp.erp_refusal_record``'s shape."""
    return {
        "code": EXCEPTION_BLOCKS_ERP_EXPORT,
        "params": blocked_export_params(exception_type, possibly_posted=possibly_posted),
    }


async def erp_export_live_claim(db: AsyncSession, invoice_id: uuid.UUID) -> str | None:
    """Which of our rails is already paying this invoice, or None.

    The run builder and the card mint refuse each other with these same two
    predicates, so the ERP push cannot disagree with them about what is live:
    a payment not ``voided`` / ``failed`` / ``cancelled``, a card not
    ``cancelled``. A payment is named first when both exist.
    """
    from app.services.card_issuance import live_card_invoice_ids
    from app.services.payment_runs import live_payment_invoices

    if invoice_id in await live_payment_invoices(db, [invoice_id]):
        return CLAIM_PAYMENT
    if invoice_id in await live_card_invoice_ids(db, [invoice_id]):
        return CLAIM_VIRTUAL_CARD
    return None


def claimed_export_params(claim: str, *, possibly_posted: bool = False) -> dict:
    """The live-claim refusal's params; ``possibly_posted`` only when true."""
    params: dict = {"claim": claim}
    if possibly_posted:
        params["possibly_posted"] = True
    return params


def claimed_export_message(claim: str, *, possibly_posted: bool = False) -> str:
    """The English ``error`` on the ``invoice.erp_failed`` row."""
    message = f"ERP post refused: {PAYMENT_BLOCKS_ERP_EXPORT} ({claim})"
    return f"{message}; {_POSSIBLY_POSTED_NOTE}" if possibly_posted else message


async def erp_export_vendor_hold(db: AsyncSession, invoice) -> str | None:
    """Why the invoice's vendor stops its push, or None.

    Reads ``payment_runs.vendor_payability``, the one vendor read the run gate
    (``inactive_vendor_statuses``) shares, so the two cannot read the vendor
    differently. The screening hold is named first. An invoice with no linked
    vendor is not refused here: there is no vendor record to judge, and the
    adapter's own vendor resolution answers that.
    """
    from app.services.payment_runs import vendor_payability

    payability = (await vendor_payability(db, [invoice])).get(invoice.id)
    if payability is None:
        return None
    hold = payability.sanctions_hold
    if hold is not None:
        return hold
    if payability.status in ERP_BOOKABLE_VENDOR_STATUSES:
        return None
    if payability.status == "rejected":
        return HOLD_VENDOR_REJECTED
    return HOLD_VENDOR_STATUS_UNKNOWN


def vendor_hold_params(hold: str, *, possibly_posted: bool = False) -> dict:
    """The vendor refusal's params; ``possibly_posted`` only when true."""
    params: dict = {"hold": hold}
    if possibly_posted:
        params["possibly_posted"] = True
    return params


def vendor_hold_message(hold: str, *, possibly_posted: bool = False) -> str:
    """The English ``error`` on the ``invoice.erp_failed`` row."""
    message = f"ERP post refused: {VENDOR_BLOCKS_ERP_EXPORT} ({hold})"
    return f"{message}; {_POSSIBLY_POSTED_NOTE}" if possibly_posted else message


@dataclass(frozen=True)
class ExportRefusal:
    """Why the push is refused: the coded refusal, and its English for the
    ``invoice.erp_failed`` row."""

    code: str
    params: dict
    error: str

    @property
    def refusal(self) -> dict:
        """In ``erp.erp_refusal_record``'s shape."""
        return {"code": self.code, "params": self.params}


async def erp_export_refusal(
    db: AsyncSession, invoice, *, possibly_posted: bool
) -> ExportRefusal | None:
    """Every reason this invoice may not be pushed now, or None. The first wins:
    an open duplicate / fraud exception (§286), then a live payment or card
    (§309), then a vendor the payment path will not pay. Both the HTTP doors and
    ``erp._call_erp`` ask this one function."""
    invoice_id = invoice.id
    exception_type = await erp_export_blocking_exception(db, invoice_id)
    if exception_type is not None:
        return ExportRefusal(
            EXCEPTION_BLOCKS_ERP_EXPORT,
            blocked_export_params(exception_type, possibly_posted=possibly_posted),
            blocked_export_message(exception_type, possibly_posted=possibly_posted),
        )
    claim = await erp_export_live_claim(db, invoice_id)
    if claim is not None:
        return ExportRefusal(
            PAYMENT_BLOCKS_ERP_EXPORT,
            claimed_export_params(claim, possibly_posted=possibly_posted),
            claimed_export_message(claim, possibly_posted=possibly_posted),
        )
    hold = await erp_export_vendor_hold(db, invoice)
    if hold is not None:
        return ExportRefusal(
            VENDOR_BLOCKS_ERP_EXPORT,
            vendor_hold_params(hold, possibly_posted=possibly_posted),
            vendor_hold_message(hold, possibly_posted=possibly_posted),
        )
    return None


#: The 409's English for a live claim: what holds the invoice, and what to do.
_CLAIM_SENTENCE = {
    CLAIM_PAYMENT: (
        "FeohLedger is already paying this invoice: it has a live payment. "
        "Cancel its payment run or void the payment before sending the invoice to the ERP."
    ),
    CLAIM_VIRTUAL_CARD: (
        "A virtual card has been issued to pay this invoice. "
        "Cancel the card before sending the invoice to the ERP."
    ),
}

#: The 409's English for a vendor hold: what stops it, and what to do.
_HOLD_SENTENCE = {
    "payments_blocked": (
        "This invoice's vendor is blocked from payment. "
        "Unblock the vendor before sending the invoice to the ERP."
    ),
    "sanctions_match": (
        "This invoice's vendor matched a sanctions list. "
        "Review the vendor's screening before sending the invoice to the ERP."
    ),
    "sanctions_review": (
        "This invoice's vendor is held for sanctions screening review. "
        "Re-screen the vendor once the review is done, then send the invoice to the ERP."
    ),
    HOLD_VENDOR_REJECTED: (
        "This invoice's vendor has been rejected. "
        "Link the invoice to the correct vendor before sending it to the ERP."
    ),
    HOLD_VENDOR_STATUS_UNKNOWN: (
        "This invoice's vendor has a status that cannot be sent to the ERP. "
        "Set the vendor to active before sending the invoice to the ERP."
    ),
}


async def refuse_blocked_erp_export(db: AsyncSession, invoice) -> None:
    """409 before an HTTP door queues a push the chokepoint would refuse.

    Commits before raising. A raised ``HTTPException`` rolls the request's
    session back (``tenant.get_tenant_db``), and ``POST /complete`` runs
    ``refresh_warnings`` first: the duplicate it may have opened in this very
    request is the exception the refusal tells the user to resolve, so rolling
    it back would send them to an Exceptions queue with nothing in it. The
    invoice itself is unchanged, since every door calls this before its
    transition.
    """
    from app.api.refusals import coded_refusal
    from app.services.erp import possibly_posted
    from app.services.workflow_engine import get_workflow_instance

    # `/retry-erp` after a timed-out attempt: the bill may already be there.
    instance = await get_workflow_instance(db, invoice.id)
    posted = possibly_posted(instance.state_data if instance is not None else None)
    refused = await erp_export_refusal(db, invoice, possibly_posted=posted)
    if refused is None:
        return
    await db.commit()
    if refused.code == EXCEPTION_BLOCKS_ERP_EXPORT:
        exception_type = refused.params["exception_type"]
        message = (
            f"This invoice has an open {exception_type.replace('_', ' ')} exception. "
            "Resolve or dismiss it in Exceptions, then send it to the ERP."
        )
    elif refused.code == PAYMENT_BLOCKS_ERP_EXPORT:
        message = _CLAIM_SENTENCE[refused.params["claim"]]
    else:
        message = _HOLD_SENTENCE[refused.params["hold"]]
    if posted:
        message += (
            " An earlier attempt may already have created the bill in the ERP; "
            "check there before anyone pays it."
        )
    raise HTTPException(
        status_code=409,
        detail=coded_refusal(refused.code, message, **refused.params),
    )
