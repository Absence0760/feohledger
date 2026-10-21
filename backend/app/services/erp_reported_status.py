"""Applying what the ERP reports about a bill it holds: paid, or cancelled.

Two callers, one implementation, so they cannot disagree about what a report
means (docs/decisions.md §303):

* ``api/erp_webhook`` — the ERP (or Merge.dev) pushes a status to us;
* ``services/erp_status_sync`` — the status sweep polls the ERP for bills we
  pushed and nobody has reported on.

**Paid** is recorded as a payment made outside FeohLedger, through
``services/external_payment`` — the same gates a user's record goes through,
minus segregation of duties (no human actor). **Cancelled / voided** after the
invoice has moved past the point where ``→ failed`` is legal is never applied:
money may be in flight, so a person reconciles it.

Nothing here commits; the caller owns the transaction and holds the invoice
``FOR UPDATE`` (``workflow_engine.get_invoice_for_update``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.services.exception_service import create_exception

#: Opened when the ERP and our ledger disagree and a person must reconcile —
#: a void/cancel we can no longer apply, a bill the ERP shows settled with no
#: payment. Not payment-blocking. See backend/CLAUDE.md § Exception types.
ERP_RECONCILIATION_EXCEPTION_TYPE = "erp_reconciliation"

#: Opened when the ERP says it PAID an invoice and we could not record that.
#: Payment-blocking, so no payment run, standalone payment or card can pay the
#: supplier a second time while it is open (decisions §251, §303).
ERP_PAID_UNRECORDED_EXCEPTION_TYPE = "payment_reconciliation"

#: Invoice statuses from which an ERP `Paid` report is RECORDED as a payment
#: made outside FeohLedger rather than dropped. `payment_scheduled` is absent on
#: purpose: it already has a FeohLedger payment behind it, and the legal
#: `payment_scheduled → paid` edge (with its settlement-coverage hold) handles
#: it. `sent_to_erp` is where a pushed invoice rests (decisions §303).
ERP_PAID_RECORDABLE = frozenset(
    {InvoiceStatus.approved, InvoiceStatus.sent_to_erp, InvoiceStatus.posted_in_erp}
)


async def open_erp_reconciliation_exception(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id,
    description: str,
    exception_type: str = ERP_RECONCILIATION_EXCEPTION_TYPE,
) -> bool:
    """Open ONE exception of ``exception_type`` for human review, PII-free.

    **Idempotent.** Skips when an OPEN or ESCALATED exception of the same type
    already exists for this invoice, so two distinct ERP events — or every tick
    of the status sweep — never pile up duplicates. Returns whether one was
    opened.

    ``description`` must carry only safe routing identifiers and status —
    never a raw ERP payload (which may include vendor bank / tax / address
    fields).
    """
    existing = await db.execute(
        select(func.count()).where(
            APException.invoice_id == invoice.id,
            APException.exception_type == exception_type,
            APException.status.in_(("open", "escalated")),
        )
    )
    if (existing.scalar() or 0) > 0:
        return False

    await create_exception(
        db,
        exception_type=exception_type,
        severity="error",
        description=description,
        status="open",
        organization_id=org_id,
        invoice=invoice,
        # Raised on the ERP's word — an HMAC-authenticated webhook or the
        # status sweep's own read — never a control-plane user's act.
        raised_by_user_id=None,
    )
    return True


async def raise_erp_cancelled_exception(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id,
    erp_type: str,
    erp_status: str,
    erp_document_id: str | None,
    event_id,
    exception_type: str = ERP_RECONCILIATION_EXCEPTION_TYPE,
) -> bool:
    """The ERP reports the bill VOIDED/CANCELLED after the invoice moved past
    the point where ``→ failed`` is legal (``sent_to_erp`` / ``posted_in_erp`` /
    ``payment_scheduled`` / ``paid``). Money may be in flight, so this opens an
    ``erp_reconciliation`` exception rather than transitioning the invoice."""
    return await open_erp_reconciliation_exception(
        db,
        invoice,
        org_id=org_id,
        exception_type=exception_type,
        description=(
            f"ERP reported '{erp_status}' (void/cancel) via {erp_type} for an "
            f"invoice already at '{invoice.status.value}' — money may be in flight. "
            f"Needs human reconciliation "
            f"(erp_document_id={erp_document_id or '-'}, event={event_id or '-'})."
        ),
    )


async def record_erp_reported_payment(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org: Organization,
    erp_type: str,
    erp_document_id: str | None,
    event_id: str | None,
    amount: Decimal | None = None,
    paid_on: date | None = None,
) -> bool:
    """Book the ERP's `Paid` as a payment made outside FeohLedger.

    ``amount`` / ``paid_on`` are what the ERP says was paid and when, when the
    caller can read them (the status sweep, through `ErpBillStatus`). A stated
    amount must equal the net owed, or the record is refused like any other
    figure mismatch — which is what stops a bill part-settled by an ERP-side
    credit being booked as paid in full. Without a date, or with one in the
    future, the payment is dated today.

    Through `services/external_payment.record_invoice_paid_outside`, which
    walks a `sent_to_erp` invoice through `posted_in_erp` once every gate has
    passed. Returns True when the payment was recorded.

    A refusal (an open payment-blocking exception, a live FeohLedger payment,
    run or card, a credit we can't net) is never silent: it opens the BLOCKING
    `payment_reconciliation` exception naming the refusal code, and returns
    False. Blocking for every refusal, not only the double-pay ones: once the
    ERP says it paid, any FeohLedger path that later pays this invoice pays
    the supplier twice — including one the refusal's own cause would reopen
    when a person clears it (a duplicate flag dismissed, a credit corrected).
    Never commits.
    """
    from app.services.external_payment import (
        ExternalPaymentRefused,
        record_invoice_paid_outside,
    )
    from app.utils.dates import utc_today

    today = utc_today()
    reference = (erp_document_id or (f"erp-event:{event_id}" if event_id else None) or erp_type)[
        :255
    ]
    try:
        await record_invoice_paid_outside(
            db,
            org=org,
            invoice=invoice,
            method=None,
            reference=reference,
            paid_on=paid_on if paid_on is not None and paid_on <= today else today,
            actor_id=None,
            source="erp",
            amount=amount,
        )
    except ExternalPaymentRefused as exc:
        await open_erp_reconciliation_exception(
            db,
            invoice,
            org_id=org.id,
            exception_type=ERP_PAID_UNRECORDED_EXCEPTION_TYPE,
            description=(
                f"ERP reported 'Paid' via {erp_type} for an invoice FeohLedger has not "
                f"paid, but it could not be recorded ({exc.refusal.code}). FeohLedger "
                f"will not pay it while this is open. Resolve the cause, then record "
                f"it via POST /api/payments/record-outside "
                f"(erp_document_id={erp_document_id or '-'}, event={event_id or '-'})."
            ),
        )
        return False
    return True


async def flag_void_of_erp_held_bill(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id,
    payment_id,
) -> bool:
    """A payment on an invoice the ERP holds as a bill was voided: block re-payment.

    The void sends the invoice `paid → approved`, which is payable, and the
    status sweep no longer looks at it. But the bill is still in the ERP — and
    if the voided payment was the ERP's own (recorded from its `Paid`), the ERP
    has paid it. A FeohLedger run would then pay the supplier a second time. So
    a void on a pushed invoice (its workflow carries an `erp_reference`) opens
    the blocking `payment_reconciliation` in the void's own transaction, and a
    person decides whether FeohLedger may pay it (decisions §303). Returns
    whether a flag was opened. Never commits.
    """
    from app.services.workflow_engine import get_workflow_instance

    instance = await get_workflow_instance(db, invoice.id)
    state = (instance.state_data if instance is not None else None) or {}
    reference = state.get("erp_reference")
    if not reference:
        return False
    return await open_erp_reconciliation_exception(
        db,
        invoice,
        org_id=org_id,
        exception_type=ERP_PAID_UNRECORDED_EXCEPTION_TYPE,
        description=(
            f"A payment ({payment_id}) was voided on an invoice whose bill is held in the "
            f"ERP (erp_document_id={reference}). If the ERP paid it, paying it here pays "
            f"the supplier twice, so FeohLedger will not pay it while this is open. "
            f"Check the bill in the ERP before resolving."
        ),
    )
