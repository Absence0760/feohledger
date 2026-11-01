"""A payment FeohLedger made, written back to the ERP against its bill (§295).

Phase 4 of the QuickBooks Online adapter: once a FeohLedger payment has
settled and its invoice is ``paid``, ``payment_erp_sync`` hands the payment
here, and an ERP that supports status sync (``ErpAdapter.supports_status_sync``
— QuickBooks Online) records it as a payment of the bill the invoice was posted
as. "The bill shows paid in QuickBooks" is what keeps a bookkeeper there from
paying it a second time.

Rules this module owns:

* **Only a bill we posted, in the company we are connected to.** The bill id
  is the invoice's ``erp_reference``, and it is used only when the push
  recorded the same connection realm (``state_data.erp_realm``): ERP ids are
  per company (§284), so a bill id from the sandbox names something else in
  production. An invoice never pushed (paid on the direct-schedule path) has
  no bill and nothing is written.
* **Idempotent on the payment id.** The adapter looks for its own earlier write
  before creating one, and sends the payment id as the ERP's idempotency key —
  a retry (``POST /api/payments/runs/{id}/sync-erp``) never creates a second
  ERP payment.
* **The pay-from account is the org's choice, never guessed.**
  ``settings.erp.bill_payment_bank_account`` / ``bill_payment_card_account``
  name GL codes in our chart; each resolves to its ERP id under the current
  realm (``gl_chart.resolve_erp_account_ids``). Unmapped or unlinked → the
  adapter refuses ``payment_account_not_mapped``.
* **A refusal or failure is never silent.** It writes ``payment.erp_post_failed``
  (the coded refusal, ``intuit_code`` and all, never the ERP's text) and opens a
  de-duped ``erp_reconciliation`` exception; the money already moved, so it
  never blocks or undoes anything here.
* **A bill left partly open in the ERP is flagged** (a discount or credit
  taken in FeohLedger that the ERP does not know about) — the ERP's books
  would otherwise show the difference as still owed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice
from app.models.payment import Payment
from app.services.audit_dispatch import dispatch_audit
from app.services.erp_adapters.base import ErpAdapter, ErpPostResult, PaymentPayload
from app.services.erp_link_realm import connection_realm, legacy_links_trusted
from app.services.erp_status_sync import erp_finding, open_erp_reconciliation_exception
from app.services.payment_methods import CARD_PAYMENT_METHODS
from app.utils.dates import utc_today

logger = logging.getLogger(__name__)

#: The ``settings.erp`` keys naming the GL accounts a written-back payment is
#: booked from: one for bank rails, one for card rails.
BANK_ACCOUNT_KEY = "bill_payment_bank_account"
CARD_ACCOUNT_KEY = "bill_payment_card_account"

#: ``erp_report["kind"]`` of the rows this module opens. The sentence is for an
#: AP user (``invoice_warning_catalog``); the payment, its run, the bill and the
#: refusal ride the report as data, never the sentence (decisions §319, §331).
ERP_REPORT_WRITEBACK_FAILED = "payment_writeback_failed"
ERP_REPORT_WRITEBACK_BALANCE_OPEN = "payment_writeback_balance_open"
ERP_REPORT_VOID_WRITEBACK_FAILED = "payment_void_writeback_failed"


def _writeback_report(
    kind: str,
    *,
    erp_type: str,
    payment_id: uuid.UUID,
    payment_run_id: uuid.UUID | None,
    bill_id: str,
    **extra: object,
) -> dict:
    """The PII-free machine detail of a write-back row: ids, the provider and,
    on a failure, the coded refusal (``erp_refusal_record``: a code and its
    params, never the ERP's text)."""
    return {
        "kind": kind,
        "erp_type": erp_type,
        "erp_document_id": bill_id,
        "payment_id": str(payment_id),
        "payment_run_id": str(payment_run_id) if payment_run_id else None,
        **extra,
    }


#: Outcomes of :func:`write_back_payment`.
WROTE = "wrote"
SKIPPED = "skipped"
FAILED = "failed"


def bill_for_writeback(state_data: dict | None, erp_config: dict | None) -> str | None:
    """The ERP bill this invoice was posted as, when it lives in the company
    ``erp_config`` connects to now — else None (nothing to write back to)."""
    state = state_data or {}
    bill_id = state.get("erp_reference")
    realm = connection_realm(erp_config)
    if not bill_id or bill_id == "UNKNOWN" or realm is None:
        return None
    if state.get("erp_realm") != realm:
        return None
    return str(bill_id)


async def build_payment_payload(
    db: AsyncSession,
    *,
    payment: Payment,
    invoice: Invoice,
    bill_id: str,
    erp_config: dict,
) -> PaymentPayload:
    from app.services.gl_chart import resolve_erp_account_ids

    card = (payment.method or "") in CARD_PAYMENT_METHODS
    code = str((erp_config or {}).get(CARD_ACCOUNT_KEY if card else BANK_ACCOUNT_KEY) or "")
    code = code.strip() or None
    account_erp_id = None
    if code:
        realm = connection_realm(erp_config)
        ids = await resolve_erp_account_ids(
            db,
            invoice.organization_id,
            invoice.entity_id,
            [code],
            realm=realm,
            trust_legacy=legacy_links_trusted(erp_config, realm),
        )
        account_erp_id = ids.get(code)
    paid_at = payment.completed_at
    currency = (invoice.currency or "").upper()
    # What reached the supplier: the rail's settled figure when it is in the
    # bill's currency (a short settlement accepted as final pays less than was
    # booked), else the booked amount.
    amount = Decimal(payment.amount)
    if payment.settled_amount is not None and (payment.settled_currency or "").upper() == currency:
        amount = Decimal(payment.settled_amount)
    return PaymentPayload(
        payment_id=str(payment.id),
        bill_id=bill_id,
        amount=amount,
        currency=currency,
        paid_on=paid_at.astimezone(UTC).date() if paid_at else utc_today(),
        method=payment.method,
        card=card,
        pay_from_account_erp_id=account_erp_id,
        reference=payment.reference,
    )


def _refusal(result: ErpPostResult) -> dict | None:
    if not result.refusal_code:
        return None
    from app.services.erp import erp_refusal_record

    return erp_refusal_record(
        result.refusal_code, provider=result.refusal_provider, extra=result.refusal_params
    )


async def write_back_payment(
    db: AsyncSession,
    *,
    adapter: ErpAdapter,
    erp_config: dict,
    payment: Payment,
    invoice: Invoice,
    state_data: dict | None,
) -> str:
    """Write one settled payment back to the ERP. Commits. Never raises.

    The caller has committed the invoice's ``paid`` transition first, so no
    row lock is held across the ERP call.
    """
    if not adapter.supports_status_sync:
        return SKIPPED
    bill_id = bill_for_writeback(state_data, erp_config)
    if bill_id is None:
        return SKIPPED
    from sqlalchemy import select

    # Re-read: a void can commit between the caller's transition and here, and
    # a BillPayment for a voided payment would show the bill paid in the ERP.
    current = (
        await db.execute(select(Payment.status).where(Payment.id == payment.id))
    ).scalar_one_or_none()
    if current != "completed":
        await db.commit()
        return SKIPPED
    correlation = invoice.correlation_id or payment.correlation_id or invoice.id
    try:
        payload = await build_payment_payload(
            db, payment=payment, invoice=invoice, bill_id=bill_id, erp_config=erp_config
        )
        # End the read transaction: no connection is held open across the ERP
        # call (the caller's session does not expire rows on commit).
        await db.commit()
        result = await adapter.post_payment(payload)
    except Exception as exc:  # noqa: BLE001 — an adapter bug is a failed write-back
        result = ErpPostResult(success=False, message=f"write-back error: {exc.__class__.__name__}")

    try:
        if result.success:
            await dispatch_audit(
                db,
                correlation_id=correlation,
                organization_id=invoice.organization_id,
                actor_id=None,
                action="payment.erp_posted",
                entity_type="payment",
                entity_id=payment.id,
                details={
                    "erp_type": adapter.erp_type,
                    "bill_id": bill_id,
                    "erp_document_id": result.erp_document_id,
                    "bill_balance_after": (
                        str(result.bill_balance_after)
                        if result.bill_balance_after is not None
                        else None
                    ),
                },
            )
            if result.bill_balance_after is not None and result.bill_balance_after > 0:
                await open_erp_reconciliation_exception(
                    db,
                    invoice,
                    org_id=invoice.organization_id,
                    finding=erp_finding(
                        "erp_payment_writeback_balance_open",
                        adapter.erp_type,
                        amount=result.bill_balance_after,
                        currency=invoice.currency,
                    ),
                    erp_report=_writeback_report(
                        ERP_REPORT_WRITEBACK_BALANCE_OPEN,
                        erp_type=adapter.erp_type,
                        payment_id=payment.id,
                        payment_run_id=payment.payment_run_id,
                        bill_id=bill_id,
                        erp_payment_id=result.erp_document_id,
                        bill_balance_after=str(result.bill_balance_after),
                    ),
                )
            await db.commit()
            return WROTE

        refusal = _refusal(result)
        await dispatch_audit(
            db,
            correlation_id=correlation,
            organization_id=invoice.organization_id,
            actor_id=None,
            action="payment.erp_post_failed",
            entity_type="payment",
            entity_id=payment.id,
            details={
                "erp_type": adapter.erp_type,
                "bill_id": bill_id,
                # PII-free by construction: a fixed literal, an HTTP code and
                # a reason code (`base.erp_failure_message` / `erp_refusal`).
                "error": result.message,
                "retryable": result.retryable,
                **({"refusal": refusal} if refusal else {}),
            },
        )
        await open_erp_reconciliation_exception(
            db,
            invoice,
            org_id=invoice.organization_id,
            finding=erp_finding("erp_payment_writeback_failed", adapter.erp_type),
            erp_report=_writeback_report(
                ERP_REPORT_WRITEBACK_FAILED,
                erp_type=adapter.erp_type,
                payment_id=payment.id,
                payment_run_id=payment.payment_run_id,
                bill_id=bill_id,
                failure=refusal["code"] if refusal else "erp_post_failed",
                refusal=refusal,
                retryable=result.retryable,
            ),
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — recording must never fail the leg
        await db.rollback()
        logger.warning(
            "[payment-writeback] could not record the outcome for payment %s: %s",
            payment.id,
            exc.__class__.__name__,
        )
    return FAILED


# ---------------------------------------------------------------------------
# Writing back from a path other than the sync pass
# ---------------------------------------------------------------------------

_tasks: set[asyncio.Task] = set()


def _spawn(coro, name: str) -> None:
    """Run on the caller's loop, off the response (the ``payment_erp_sync``
    shape); asyncio keeps only a weak reference to a task, hence the set."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _org_adapter(org_id: uuid.UUID):
    """``(org, resolved erp config, adapter)`` for an org whose ERP reports
    back, else None."""
    from app.database import control_session_factory
    from app.models.organization import Organization
    from app.services.erp_adapters import get_erp_adapter
    from app.services.provider_credentials import provider_config

    async with control_session_factory() as ctrl:
        org = await ctrl.get(Organization, org_id)
        if org is None:
            return None
        erp_config = await provider_config(org, "erp", db=ctrl) or None
    if not erp_config:
        return None
    adapter = get_erp_adapter(erp_config)
    if not getattr(adapter, "supports_status_sync", False):
        return None
    return org, erp_config, adapter


async def _state_data(db: AsyncSession, invoice_id: uuid.UUID) -> dict | None:
    from sqlalchemy import select

    from app.models.workflow import WorkflowInstance

    return (
        await db.execute(
            select(WorkflowInstance.state_data).where(WorkflowInstance.invoice_id == invoice_id)
        )
    ).scalar_one_or_none()


def dispatch_settled_writeback(org_id: uuid.UUID, payment_id: uuid.UUID) -> None:
    """A FeohLedger payment reached ``paid`` outside the sync pass (a short
    settlement accepted as final): write it back the same way, after the
    caller's commit."""
    _spawn(write_back_settled_payment(org_id, payment_id), f"writeback-{payment_id}")


async def write_back_settled_payment(org_id: uuid.UUID, payment_id: uuid.UUID) -> str:
    """:func:`write_back_payment` for one payment, loading what it needs.
    Never raises."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import get_tenant_engine

    try:
        found = await _org_adapter(org_id)
        if found is None:
            return SKIPPED
        org, erp_config, adapter = found
        factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
        async with factory() as db:
            payment = await db.get(Payment, payment_id)
            invoice = await db.get(Invoice, payment.invoice_id) if payment else None
            if invoice is None or invoice.status.value != "paid":
                return SKIPPED
            state = await _state_data(db, invoice.id)
            return await write_back_payment(
                db,
                adapter=adapter,
                erp_config=erp_config,
                payment=payment,
                invoice=invoice,
                state_data=state,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort, after the caller committed
        logger.warning(
            "[payment-writeback] write-back failed for payment %s: %s",
            payment_id,
            exc.__class__.__name__,
        )
        return FAILED


# ---------------------------------------------------------------------------
# A voided payment: void what we wrote
# ---------------------------------------------------------------------------

#: ``WorkflowInstance.state_data`` key: ids of voided payments whose ERP
#: payment still has to be voided. Written in the void's own transaction, so a
#: process that dies before the background task runs leaves the intent behind,
#: and the status-sync sweep (:func:`resume_pending_voids`) finishes it.
PENDING_VOIDS_KEY = "erp_payment_voids_pending"


async def record_pending_erp_void(db: AsyncSession, *, invoice: Invoice, payment: Payment) -> bool:
    """In ``POST /api/payments/{id}/void``'s transaction: note that this
    payment's write-back (if any) must be voided in the ERP. Only a FeohLedger
    payment of an invoice that was posted to the ERP qualifies; returns whether
    one was recorded. Never commits."""
    from sqlalchemy import select

    from app.models.workflow import WorkflowInstance
    from app.services.external_payment import EXTERNAL_PAYMENT_PROVIDER

    if payment.provider == EXTERNAL_PAYMENT_PROVIDER:
        return False  # never written back (payment_erp_sync skips them)
    instance = (
        await db.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice.id))
    ).scalar_one_or_none()
    state = dict((instance.state_data if instance else None) or {})
    if instance is None or not state.get("erp_reference") or not state.get("erp_realm"):
        return False
    pending = [str(p) for p in state.get(PENDING_VOIDS_KEY) or []]
    if str(payment.id) not in pending:
        pending.append(str(payment.id))
    instance.state_data = {**state, PENDING_VOIDS_KEY: pending}
    return True


async def _clear_pending(db: AsyncSession, invoice_id: uuid.UUID, payment_id: uuid.UUID) -> None:
    from sqlalchemy import select

    from app.models.workflow import WorkflowInstance

    instance = (
        await db.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.invoice_id == invoice_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if instance is None:
        return
    state = dict(instance.state_data or {})
    pending = [p for p in state.get(PENDING_VOIDS_KEY) or [] if p != str(payment_id)]
    if pending:
        state[PENDING_VOIDS_KEY] = pending
    else:
        state.pop(PENDING_VOIDS_KEY, None)
    instance.state_data = state


def dispatch_void_writeback(org_id: uuid.UUID, payment_id: uuid.UUID) -> None:
    """After ``POST /api/payments/{id}/void`` commits, void the ERP payment we
    wrote for it, on the request's loop and off the response."""
    _spawn(void_written_payment(org_id, payment_id), f"void-writeback-{payment_id}")


async def void_written_payment(org_id: uuid.UUID, payment_id: uuid.UUID) -> str:
    """Void the ERP payment written back for ``payment_id``. Never raises.

    A void here that leaves our BillPayment standing in the ERP would show the
    bill paid there while FeohLedger owes it again: the bookkeeper would not
    pay it, and the books would disagree. So a failure opens an
    ``erp_reconciliation`` exception naming the cause and leaves the pending
    marker for the sweep to retry; a success writes ``payment.erp_voided`` —
    or ``payment.erp_void_not_needed`` when nothing of ours was there — and
    clears the marker.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import get_tenant_engine

    try:
        found = await _org_adapter(org_id)
        if found is None:
            return SKIPPED
        org, erp_config, adapter = found
        factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
        async with factory() as db:
            payment = await db.get(Payment, payment_id)
            if payment is None:
                return SKIPPED
            invoice = await db.get(Invoice, payment.invoice_id)
            state = await _state_data(db, payment.invoice_id)
            bill_id = bill_for_writeback(state, erp_config)
            if invoice is None or bill_id is None:
                return SKIPPED
            payload = PaymentPayload(
                payment_id=str(payment.id),
                bill_id=bill_id,
                amount=Decimal(payment.amount),
                currency=(invoice.currency or "").upper(),
                paid_on=utc_today(),
            )
            correlation = invoice.correlation_id or payment.correlation_id or invoice.id
            invoice_id = invoice.id
            run_id = payment.payment_run_id
            # Nothing held across the ERP call. A rollback expires the loaded
            # rows, so what is needed after it was read above.
            await db.rollback()
            result = await adapter.void_payment(payload)
            invoice = await db.get(Invoice, invoice_id)
            refusal = _refusal(result)
            if result.success:
                voided = bool(result.erp_document_id)
                action = "payment.erp_voided" if voided else "payment.erp_void_not_needed"
            else:
                action = "payment.erp_void_failed"
            await dispatch_audit(
                db,
                correlation_id=correlation,
                organization_id=invoice.organization_id,
                actor_id=None,
                action=action,
                entity_type="payment",
                entity_id=payment_id,
                details={
                    "erp_type": adapter.erp_type,
                    "bill_id": bill_id,
                    "erp_document_id": result.erp_document_id,
                    **({} if result.success else {"error": result.message}),
                    **({"refusal": refusal} if refusal else {}),
                },
            )
            if result.success:
                await _clear_pending(db, invoice_id, payment_id)
            else:
                await open_erp_reconciliation_exception(
                    db,
                    invoice,
                    org_id=invoice.organization_id,
                    finding=erp_finding("erp_payment_void_writeback_failed", adapter.erp_type),
                    erp_report=_writeback_report(
                        ERP_REPORT_VOID_WRITEBACK_FAILED,
                        erp_type=adapter.erp_type,
                        payment_id=payment_id,
                        payment_run_id=run_id,
                        bill_id=bill_id,
                        failure=refusal["code"] if refusal else "erp_void_failed",
                        refusal=refusal,
                        retryable=result.retryable,
                    ),
                )
            await db.commit()
            return WROTE if result.success else FAILED
    except Exception as exc:  # noqa: BLE001 — best-effort, after the void committed
        logger.warning(
            "[payment-writeback] void write-back failed for payment %s: %s",
            payment_id,
            exc.__class__.__name__,
        )
        return FAILED


async def resume_pending_voids(org) -> int:
    """Finish every ERP void a payment void recorded and nothing completed
    (the process died, or the ERP refused). Called per org by the status-sync
    sweep. Returns how many are still pending afterwards."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import get_tenant_engine
    from app.models.workflow import WorkflowInstance

    factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
    async with factory() as db:
        rows = (
            await db.execute(
                select(WorkflowInstance.state_data).where(
                    WorkflowInstance.state_data.has_key(PENDING_VOIDS_KEY)
                )
            )
        ).scalars()
        pending = [p for state in rows for p in (state or {}).get(PENDING_VOIDS_KEY) or []]
    left = 0
    for payment_id in pending:
        if await void_written_payment(org.id, uuid.UUID(payment_id)) == FAILED:
            left += 1
    return left
