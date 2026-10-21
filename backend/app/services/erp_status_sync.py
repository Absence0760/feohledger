"""What the ERP says about a bill, brought back into FeohLedger (decisions §295).

Two directions of the same fact meet here:

* **The ERP reports a payment status.** The generic ERP webhook
  (``api/erp_webhook``) has always handled a ``Paid`` report;
  :func:`record_erp_reported_payment` is that path — the ERP paying an invoice
  FeohLedger never paid is booked as a payment made outside FeohLedger, with
  the ERP's own payment date and rail when it states them, and a refusal is
  never silent.
* **QuickBooks Online status sync-back.** QuickBooks sends webhooks that name
  only an entity id, so the bill is read back
  (``ErpAdapter.bill_report``) and :func:`apply_bill_report` decides what it
  means for the invoice it was posted from. The same function runs for a
  webhook (``api/erp_webhook.quickbooks_webhook`` → :func:`process_qbo_events`)
  and for the change-data-capture sweep (:func:`run_status_sync_once`), which
  is the backstop: Intuit does not guarantee delivery.

**What closes the double-payment risk.** With status sync on
(``FEOH_ERP_STATUS_SYNC_ENABLED``, :func:`status_sync_active`) a confirmed
QuickBooks push leaves the invoice payable at ``posted_in_erp`` instead of
closing it at ``done`` (``services/erp.send_to_erp_internal``). A bill then
paid in QuickBooks reaches the ERP-paid path and the invoice moves to ``paid``,
so it leaves our payment queue; a bill paid there while FeohLedger also holds
a payment on it, partly paid there, or deleted there, opens the
payment-blocking ``payment_reconciliation`` exception, which the run builder
and every dispatch refuse on. Without status sync the push still closes the
invoice at ``done`` — nothing would guarantee a QuickBooks payment ever reached
us, so leaving the invoice payable would invite the double payment.

**A bill paid only by our own write-back is not news.** ``post_payment``
marks each BillPayment it writes, and ``bill_report`` separates those from
payments a person made in QuickBooks (``paid_only_by_us``).

Everything this module writes is PII-free: ERP ids, statuses, codes. Never a
vendor, a memo, or the ERP's error text.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database import control_session_factory, get_tenant_engine
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.workflow import WorkflowInstance
from app.services.erp_adapters.base import ErpAdapter, ErpBillReport, ErpBillState
from app.services.exception_service import create_exception
from app.services.workflow_engine import get_invoice_for_update, transition_invoice
from app.utils.dates import utc_today

logger = logging.getLogger(__name__)

#: The roster type for "the ERP and our ledger disagree; a person reconciles"
#: (not payment-blocking).
ERP_RECONCILIATION_EXCEPTION_TYPE = "erp_reconciliation"
#: The payment-blocking type, for a disagreement that would let FeohLedger pay
#: a bill the ERP has already paid, part-paid or removed.
PAYMENT_RECONCILIATION_EXCEPTION_TYPE = "payment_reconciliation"

#: Invoice statuses from which an ERP `Paid` report is RECORDED as a payment
#: made outside FeohLedger rather than dropped. `payment_scheduled` is absent on
#: purpose: it already has a FeohLedger payment behind it, and the legal
#: `payment_scheduled → paid` edge (with its settlement-coverage hold) handles it.
ERP_PAID_RECORDABLE = frozenset(
    {InvoiceStatus.approved, InvoiceStatus.sent_to_erp, InvoiceStatus.posted_in_erp}
)

#: Statuses at which FeohLedger may still pay the invoice — where a bill paid,
#: part-paid or removed in the ERP must stop our payment runs.
PAYABLE_HERE = frozenset(
    {InvoiceStatus.approved, InvoiceStatus.posted_in_erp, InvoiceStatus.payment_scheduled}
)

#: Refusals meaning FeohLedger still holds its own claim on paying the invoice
#: (`services/external_payment`). Raised as the BLOCKING `payment_reconciliation`
#: type, because that claim proceeding after the ERP paid is a double payment.
DOUBLE_PAY_REFUSALS = frozenset(
    {
        "external_payment_in_run",
        "external_payment_payment_live",
        "external_payment_card_live",
    }
)

#: The adapter whose bills sync back today (the only ``supports_status_sync``).
QBO_PROVIDER = "quickbooks_online"


# ---------------------------------------------------------------------------
# The ERP-paid path (shared with the generic ERP webhook)
# ---------------------------------------------------------------------------


def reported_paid_on(value: object) -> date | None:
    """An ERP-stated payment date, validated: an ISO date that is not in the
    future (nothing is paid tomorrow). One day of grace past the UTC date,
    because the ERP dates a payment in the COMPANY's time zone (Auckland is
    already "tomorrow" for much of the UTC day). Anything else is None, and the
    caller falls back to the day the report arrived."""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed <= utc_today() + timedelta(days=1) else None


def reported_payment_method(value: object) -> str | None:
    """An ERP-stated rail, validated against the rails a payment made outside
    FeohLedger can be recorded on (``schemas/payment.PaymentMethod`` minus
    ``virtual_card``, which is FeohLedger's own card programme). Anything else
    is None — NULL, which counts as 1099-reportable."""
    from app.schemas.payment import PaymentMethod

    if not isinstance(value, str):
        return None
    try:
        method = PaymentMethod(value.strip().lower())
    except ValueError:
        return None
    return None if method == PaymentMethod.virtual_card else method.value


async def open_erp_reconciliation_exception(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id,
    description: str,
    exception_type: str = ERP_RECONCILIATION_EXCEPTION_TYPE,
) -> bool:
    """Open ONE reconciliation exception for human review, PII-free.

    **Idempotent**: skipped when an open or escalated exception of the same type
    already exists for the invoice, so distinct ERP events (or a webhook and the
    sweep reporting the same change) never pile up duplicates. Returns whether
    one was opened.

    ``description`` carries only safe routing identifiers and statuses — never
    the ERP's payload, which may include vendor bank / tax / address fields.
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
        # Raised from what the tenant's ERP reported, not by a control-plane user.
        raised_by_user_id=None,
    )
    return True


async def record_erp_reported_payment(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org: Organization,
    erp_type: str,
    erp_document_id: str | None,
    event_id: str | None,
    paid_on: date | None = None,
    method: str | None = None,
) -> None:
    """Book the ERP's `Paid` as a payment made outside FeohLedger.

    Through `services/external_payment.record_invoice_paid_outside` — the same
    gates a user's record goes through, minus segregation of duties (no human
    actor). ``paid_on`` / ``method`` are the ERP's own payment date and rail
    when it states them (already validated: :func:`reported_paid_on`,
    :func:`reported_payment_method`); absent, the payment is dated the day the
    report arrived and its rail is NULL (1099-reportable). A refusal (an open
    payment-blocking exception, a live FeohLedger payment or card that would
    make this a double payment, a credit we can't net) is never silent: it
    opens a reconciliation exception naming the refusal code. Never commits.
    """
    from app.services.external_payment import (
        ExternalPaymentRefused,
        record_invoice_paid_outside,
    )

    if invoice.status == InvoiceStatus.sent_to_erp:
        # The ERP can only have paid a bill it holds: the `posted_in_erp` report
        # was lost or arrived out of order. Apply it first so the trail reads in
        # the order things happened.
        await transition_invoice(
            db,
            invoice,
            InvoiceStatus.posted_in_erp,
            action_name="invoice.erp_status_posted_in_erp",
            details={
                "erp_type": erp_type,
                "erp_status": "implied_by_paid",
                "erp_document_id": erp_document_id,
                "event_id": event_id,
            },
        )

    reference = (erp_document_id or (f"erp-event:{event_id}" if event_id else None) or erp_type)[
        :255
    ]
    try:
        await record_invoice_paid_outside(
            db,
            org=org,
            invoice=invoice,
            method=method,
            reference=reference,
            paid_on=paid_on or utc_today(),
            actor_id=None,
            source="erp",
        )
    except ExternalPaymentRefused as exc:
        # When FeohLedger itself still has a claim on paying this invoice — a
        # live payment, a draft/exported run, a live card — the ERP has just
        # paid it, so letting that claim proceed pays the supplier TWICE.
        # `erp_reconciliation` is not payment-blocking, so for those refusals
        # the flag is the blocking `payment_reconciliation`, which the run
        # builder and every dispatch (`dispatch_preflight`) refuse on. Any other
        # refusal is a question for a human, not a double-pay risk.
        double_pay = exc.refusal.code in DOUBLE_PAY_REFUSALS
        await open_erp_reconciliation_exception(
            db,
            invoice,
            org_id=org.id,
            exception_type=(
                PAYMENT_RECONCILIATION_EXCEPTION_TYPE
                if double_pay
                else ERP_RECONCILIATION_EXCEPTION_TYPE
            ),
            description=(
                f"ERP reported 'Paid' via {erp_type} for an invoice FeohLedger has not "
                f"paid, but it could not be recorded ({exc.refusal.code}). Resolve the "
                f"cause, then record it via POST /api/payments/record-outside "
                f"(erp_document_id={erp_document_id or '-'}, event={event_id or '-'})."
            ),
        )


# ---------------------------------------------------------------------------
# QuickBooks status sync-back
# ---------------------------------------------------------------------------


def status_sync_active(erp_config: dict | None) -> bool:
    """Does a confirmed push to ``erp_config``'s ERP leave the invoice payable
    at ``posted_in_erp``? Only when the operator has status sync on AND the
    adapter reports its bills' payments back (``supports_status_sync``)."""
    if not settings.erp_status_sync_enabled or not isinstance(erp_config, dict):
        return False
    adapter = _adapter_or_none(erp_config)
    return bool(adapter is not None and adapter.supports_status_sync)


def _adapter_or_none(erp_config: dict) -> ErpAdapter | None:
    from app.services.erp_adapters import get_erp_adapter

    try:
        return get_erp_adapter(erp_config)
    except Exception:  # noqa: BLE001 — an unknown adapter has no status sync
        return None


@dataclass(frozen=True)
class SyncOutcome:
    """What :func:`apply_bill_report` did with one report (for counting/tests)."""

    action: str
    invoice_id: uuid.UUID | None = None


async def _matching_invoice_id(
    db: AsyncSession, report: ErpBillReport, realm: str | None
) -> uuid.UUID | None:
    """The invoice this bill was posted from, or None.

    A bill that still carries our marker names its invoice by correlation id,
    and the invoice must record the same bill as its ERP reference — a bill
    keyed in the ERP by hand, or copied, matches nothing. A deleted bill can no
    longer be read, so it is matched by the reference alone, and only within
    the connection realm the push recorded (QuickBooks numbers bills per
    company, so a sandbox bill id names something else in production).
    """
    bill_id = str(report.bill_id)
    if report.correlation_id:
        try:
            correlation = uuid.UUID(report.correlation_id)
        except ValueError:
            return None
        row = (
            await db.execute(
                select(Invoice.id, WorkflowInstance.state_data)
                .join(WorkflowInstance, WorkflowInstance.invoice_id == Invoice.id, isouter=True)
                .where(Invoice.correlation_id == correlation)
            )
        ).first()
        if row is None:
            return None
        invoice_id, state = row
        if str((state or {}).get("erp_reference") or "") != bill_id:
            return None
        recorded = (state or {}).get("erp_realm")
        if recorded and realm and recorded != realm:
            return None
        return invoice_id
    if report.state is not ErpBillState.deleted or not realm:
        return None
    rows = (
        await db.execute(
            select(WorkflowInstance.invoice_id).where(
                WorkflowInstance.state_data["erp_reference"].astext == bill_id,
                WorkflowInstance.state_data["erp_realm"].astext == realm,
            )
        )
    ).all()
    # More than one invoice naming the same bill in one company is not a match
    # we can act on; nothing is guessed.
    return rows[0][0] if len(rows) == 1 else None


def _describe(report: ErpBillReport, provider: str, event_id: str | None) -> str:
    source = event_id or "reconciliation-sweep"
    return f"(provider={provider}, bill={report.bill_id}, event={source})"


async def apply_bill_report(
    db: AsyncSession,
    *,
    org: Organization,
    report: ErpBillReport,
    realm: str | None,
    erp_type: str = QBO_PROVIDER,
    event_id: str | None = None,
) -> SyncOutcome:
    """Bring one bill's ERP state into the invoice it was posted from. Commits.

    =================== ============================ ================================
    Bill in the ERP     Invoice here                 What happens
    =================== ============================ ================================
    paid outside us     payable here, partly or      blocking payment_reconciliation:
    (vendor credit)     wholly by a credit           settled without money leaving
    paid outside us     approved / sent / posted     recorded as paid outside, with
    (cash)                                           the ERP's date and rail
    paid outside us     payment_scheduled            blocking payment_reconciliation:
                                                     FeohLedger is paying it too
    paid outside us     paid by a FeohLedger rail    erp_reconciliation: the
                                                     supplier was paid twice
    paid (only by us)   any                          nothing — our own write-back
    partially paid      payable here                 blocking payment_reconciliation:
    (outside us)                                     our run would pay it in full
    open                paid by an ERP report        erp_reconciliation: the ERP no
                                                     longer shows it paid
    deleted             payable here                 blocking payment_reconciliation
    deleted             sent / paid / done / failed  erp_reconciliation
    =================== ============================ ================================

    "Outside us" is judged from the BALANCE (``ErpBillReport.reduced_outside``):
    anything that took the bill down beyond our own write-backs. Every other
    combination is already consistent and changes nothing. The invoice is
    row-locked before it is judged, like every status change.
    """
    invoice_id = await _matching_invoice_id(db, report, realm)
    if invoice_id is None:
        await db.rollback()
        return SyncOutcome("unmatched")
    invoice = await get_invoice_for_update(db, invoice_id)
    status = invoice.status
    where = _describe(report, erp_type, event_id)
    action = "unchanged"

    if report.state is ErpBillState.paid and report.reduced_outside:
        if report.settled_by_credit and status in PAYABLE_HERE:
            # Settled in the ERP by a vendor credit (in whole or part), not by
            # money leaving: never booked as a cash payment, but never payable
            # here either until a person says what is owed.
            opened = await open_erp_reconciliation_exception(
                db,
                invoice,
                org_id=org.id,
                exception_type=PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
                description=(
                    "The ERP shows this bill settled, at least in part by a vendor credit "
                    "applied there. FeohLedger will not pay it until someone confirms what "
                    f"is still owed {where}."
                ),
            )
            action = "credit_settlement_flagged" if opened else action
        elif status in ERP_PAID_RECORDABLE:
            await record_erp_reported_payment(
                db,
                invoice,
                org=org,
                erp_type=erp_type,
                erp_document_id=report.bill_id,
                event_id=event_id,
                paid_on=reported_paid_on(report.paid_on),
                method=reported_payment_method(report.payment_method),
            )
            action = "paid_recorded"
        elif status == InvoiceStatus.payment_scheduled:
            opened = await open_erp_reconciliation_exception(
                db,
                invoice,
                org_id=org.id,
                exception_type=PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
                description=(
                    "The ERP shows this bill paid by a payment FeohLedger did not make, "
                    "while a FeohLedger payment is scheduled for it. Stop or void one of "
                    f"them before both reach the supplier {where}."
                ),
            )
            action = "double_payment_flagged" if opened else action
        elif status == InvoiceStatus.paid and await _paid_through_feohledger(db, invoice.id):
            # FeohLedger's own rail paid it (its write-back missing or
            # refused) and a person paid it in QuickBooks too: the money has
            # left twice. A payment recorded as made OUTSIDE FeohLedger is
            # most likely that same QuickBooks payment, so it is not flagged.
            opened = await open_erp_reconciliation_exception(
                db,
                invoice,
                org_id=org.id,
                description=(
                    "FeohLedger paid this invoice, and the ERP shows the bill also paid by a "
                    "payment FeohLedger did not make — the supplier may have been paid twice. "
                    f"Recover one of the payments {where}."
                ),
            )
            action = "double_payment_flagged" if opened else action
    elif report.state is ErpBillState.partially_paid and report.reduced_outside:
        if status in PAYABLE_HERE:
            opened = await open_erp_reconciliation_exception(
                db,
                invoice,
                org_id=org.id,
                exception_type=PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
                description=(
                    "The ERP shows part of this bill paid or credited outside FeohLedger; "
                    "a FeohLedger payment for the full amount would overpay it. "
                    f"Reconcile what is still owed before paying {where}."
                ),
            )
            action = "partial_payment_flagged" if opened else action
    elif report.state is ErpBillState.open and status == InvoiceStatus.paid:
        if await _paid_by_erp_report(db, invoice.id, report.bill_id):
            opened = await open_erp_reconciliation_exception(
                db,
                invoice,
                org_id=org.id,
                description=(
                    "The ERP reported this bill paid, and FeohLedger recorded that payment, "
                    "but the ERP no longer shows it paid (the payment was deleted or "
                    f"voided there). Confirm whether it is still owed {where}."
                ),
            )
            action = "unpaid_flagged" if opened else action
    elif report.state is ErpBillState.deleted:
        blocking = status in PAYABLE_HERE
        opened = await open_erp_reconciliation_exception(
            db,
            invoice,
            org_id=org.id,
            exception_type=(
                PAYMENT_RECONCILIATION_EXCEPTION_TYPE
                if blocking
                else ERP_RECONCILIATION_EXCEPTION_TYPE
            ),
            description=(
                "The bill this invoice was posted as has been deleted or voided in the ERP"
                + (
                    "; FeohLedger will not pay it until someone confirms it is still owed "
                    if blocking
                    else ". Confirm the books still agree "
                )
                + where
                + "."
            ),
        )
        action = "deleted_flagged" if opened else action

    await db.commit()
    return SyncOutcome(action, invoice_id)


async def _paid_through_feohledger(db: AsyncSession, invoice_id: uuid.UUID) -> bool:
    """Did a FeohLedger rail (not a payment recorded as made outside) pay it?"""
    from app.services.external_payment import EXTERNAL_PAYMENT_PROVIDER

    found = await db.execute(
        select(func.count()).where(
            Payment.invoice_id == invoice_id,
            Payment.status == "completed",
            Payment.provider.is_distinct_from(EXTERNAL_PAYMENT_PROVIDER),
        )
    )
    return (found.scalar() or 0) > 0


async def flag_void_of_erp_reported_payment(
    db: AsyncSession, *, invoice: Invoice, payment: Payment, org_id: uuid.UUID
) -> bool:
    """Called by ``POST /api/payments/{id}/void`` in its own transaction.

    Voiding a payment the ERP REPORTED (``provider = external``, its reference
    the bill the invoice was posted as) sends the invoice back to ``approved``
    and payable — while the ERP still shows the bill paid, and nothing about
    the bill changes, so no webhook or sweep would ever say so again. The next
    run would pay it a second time. So the void opens the payment-blocking
    ``payment_reconciliation`` with it: a person confirms the ERP's payment is
    really undone before FeohLedger pays. Returns whether one was opened.
    """
    from app.services.external_payment import EXTERNAL_PAYMENT_PROVIDER

    if payment.provider != EXTERNAL_PAYMENT_PROVIDER:
        return False
    state = (
        await db.execute(
            select(WorkflowInstance.state_data).where(WorkflowInstance.invoice_id == invoice.id)
        )
    ).scalar_one_or_none()
    bill_id = str((state or {}).get("erp_reference") or "")
    if not bill_id or (payment.reference or "") != bill_id[:255]:
        return False
    return await open_erp_reconciliation_exception(
        db,
        invoice,
        org_id=org_id,
        exception_type=PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
        description=(
            f"A payment the ERP reported (bill={bill_id}) was voided in FeohLedger, but the "
            "ERP may still show the bill paid. FeohLedger will not pay it again until "
            "someone confirms the ERP's payment is undone."
        ),
    )


async def _paid_by_erp_report(db: AsyncSession, invoice_id: uuid.UUID, bill_id: str) -> bool:
    """Did an ERP report (not a FeohLedger payment) pay this invoice?"""
    from app.services.external_payment import EXTERNAL_PAYMENT_PROVIDER

    found = await db.execute(
        select(func.count()).where(
            Payment.invoice_id == invoice_id,
            Payment.status == "completed",
            Payment.provider == EXTERNAL_PAYMENT_PROVIDER,
            Payment.reference == str(bill_id)[:255],
        )
    )
    return (found.scalar() or 0) > 0


async def org_for_company(provider: str, company_id: str) -> Organization | None:
    """The tenant connected to ``provider``'s company ``company_id``.

    Served by the control-plane unique index on the OAuth block's
    ``(provider, external_tenant_id)`` (migration 0117), which is also what
    guarantees at most one tenant per company. The org must still be on that
    ERP: the OAuth block survives a switch of ERP type, and a tenant that has
    moved to another ERP is not synced.
    """
    from app.services.erp_adapters.dispatcher import resolve_adapter_key

    oauth = Organization.settings["erp"]["oauth"]
    async with control_session_factory() as ctrl:
        org = (
            await ctrl.execute(
                select(Organization).where(
                    oauth["provider"].astext == provider,
                    oauth["external_tenant_id"].astext == str(company_id),
                )
            )
        ).scalar_one_or_none()
    if org is None:
        return None
    erp = (org.settings or {}).get("erp")
    if not isinstance(erp, dict) or resolve_adapter_key(erp) != provider:
        return None
    return org


async def _resolved_erp(org: Organization) -> dict | None:
    from app.services.provider_credentials import provider_config

    async with control_session_factory() as ctrl:
        return await provider_config(org, "erp", db=ctrl) or None


async def sync_bills(org: Organization, bill_ids: list[str], *, event_id: str | None) -> int:
    """Read each bill back and apply it to the tenant. Returns how many changed
    something. Raises on a failed read (the caller decides what that means)."""
    from app.services.erp_link_realm import connection_realm

    erp_config = await _resolved_erp(org)
    adapter = _adapter_or_none(erp_config or {})
    if adapter is None or not adapter.supports_status_sync:
        return 0
    realm = connection_realm(erp_config)
    factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
    changed = 0
    for bill_id in dict.fromkeys(bill_ids):
        report = await adapter.bill_report(bill_id)
        async with factory() as db:
            outcome = await apply_bill_report(
                db,
                org=org,
                report=report,
                realm=realm,
                erp_type=adapter.erp_type,
                event_id=event_id,
            )
        if outcome.action not in ("unmatched", "unchanged"):
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# QuickBooks CloudEvents webhooks
# ---------------------------------------------------------------------------

#: ``qbo.<entity>.<operation>.v<n>`` — the CloudEvents ``type`` Intuit sends.
_QBO_EVENT_TYPE = re.compile(r"^qbo\.([a-z]+)\.([a-z]+)\.v\d+$")
_QBO_DEDUP_PROVIDER = "erp:quickbooks_online"


@dataclass(frozen=True)
class QboEvent:
    event_id: str
    company_id: str
    entity: str
    entity_id: str


def parse_qbo_events(body: object) -> list[QboEvent]:
    """The bill and bill-payment events in an Intuit CloudEvents delivery.

    The body is a JSON array of CloudEvents (one object is accepted too). An
    event with no id, company (``intuitaccountid``) or entity id
    (``intuitentityid``), or about another entity, is dropped. Nothing else is
    read from the event: its ``data`` is never trusted, the bill is read back.
    """
    events = body if isinstance(body, list) else [body]
    out: list[QboEvent] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        match = _QBO_EVENT_TYPE.match(str(event.get("type") or "").lower())
        if not match or match.group(1) not in ("bill", "billpayment"):
            continue
        event_id = str(event.get("id") or "").strip()
        company = str(event.get("intuitaccountid") or "").strip()
        entity_id = str(event.get("intuitentityid") or "").strip()
        if not (event_id and company and entity_id) or max(map(len, (event_id, company))) > 128:
            continue
        out.append(QboEvent(event_id, company, match.group(1), entity_id[:64]))
    return out


async def process_qbo_event(event: QboEvent) -> None:
    """Apply one event. Raises on our own failure (the caller releases its
    dedup claim; Intuit already has its 204, so the CDC sweep is what
    reprocesses the change)."""
    org = await org_for_company(QBO_PROVIDER, event.company_id)
    if org is None:
        return  # a company no tenant is connected to: a decision, not a failure
    erp_config = await _resolved_erp(org)
    adapter = _adapter_or_none(erp_config or {})
    if adapter is None or not adapter.supports_status_sync:
        return
    if event.entity == "billpayment":
        bill_ids = await adapter.bills_for_payment(event.entity_id)
    else:
        bill_ids = [event.entity_id]
    if bill_ids:
        await sync_bills(org, bill_ids, event_id=event.event_id)


async def claim_qbo_events(events: list[QboEvent]) -> list[QboEvent]:
    """The events not seen before, each claimed by its event id
    (``is_event_already_processed``, dedupe per the webhook invariant). Called
    by the route BEFORE it answers, so a Redis failure raises there and becomes
    a 503 that makes Intuit redeliver, instead of a 2xx for unclaimed work."""
    from app.services.webhook_security import is_event_already_processed

    fresh: list[QboEvent] = []
    for event in events:
        if not await is_event_already_processed(_QBO_DEDUP_PROVIDER, event.event_id):
            fresh.append(event)
    return fresh


async def process_qbo_events(events: list[QboEvent]) -> int:
    """Apply each claimed event. Returns how many failed; a failed event's
    claim is released (Intuit already has its 204: the CDC sweep is what picks
    the change up again). Never raises."""
    from app.services.webhook_security import release_event_claim

    failures = 0
    for event in events:
        try:
            await process_qbo_event(event)
        except Exception as exc:  # noqa: BLE001 — one event never stops the rest
            failures += 1
            await release_event_claim(_QBO_DEDUP_PROVIDER, event.event_id)
            logger.warning(
                "[qbo-webhook] event for company %s failed: %s",
                event.company_id,
                exc.__class__.__name__,
            )
    return failures


#: Strong references to in-flight webhook tasks (asyncio holds only a weak one).
_webhook_tasks: set[asyncio.Task] = set()


def dispatch_qbo_events(events: list[QboEvent]) -> None:
    """Process a verified delivery after the response has gone back.

    Intuit expects its webhook answered within seconds, and each event costs
    one to three reads of the company, so the work runs as a task on the
    request's own loop (the ``payment_erp_sync`` shape — pure-await I/O). A
    failure releases its dedup claim; the change-data-capture sweep is what
    guarantees the change still arrives.
    """
    if not events:
        return
    task = asyncio.create_task(process_qbo_events(events), name="qbo-webhook")
    _webhook_tasks.add(task)
    task.add_done_callback(_webhook_tasks.discard)


# ---------------------------------------------------------------------------
# The change-data-capture sweep (the backstop)
# ---------------------------------------------------------------------------


@dataclass
class StatusSyncResult:
    orgs_scanned: int = 0
    bills_read: int = 0
    invoices_changed: int = 0
    #: Passes that read back every payable bill (no mark, a gap past CDC's
    #: reach, or a CDC answer too full to trust).
    full_reconciliations: int = 0
    #: Payment voids still waiting to be voided in the ERP after this pass.
    voids_pending: int = 0
    failures: int = 0


#: ``settings.erp.oauth`` key: the start of this connection's last COMPLETE
#: sweep pass. The next pass reads changes from there (or the lookback window,
#: whichever is earlier), so a sweep that was off, failing or skipped for days
#: still sees every change it missed — CDC's 30 days permitting.
SYNCED_THROUGH_KEY = "status_sync_through"
#: Intuit's change-data-capture answers changes up to 30 days old; a day of
#: margin keeps a window from starting past its reach.
_CDC_REACH = timedelta(days=29)


async def _open_bill_ids(org: Organization, realm: str | None) -> list[str]:
    """Every bill still payable here that was posted to ``realm``: what a full
    reconciliation reads back when CDC cannot prove it saw every change."""
    if not realm:
        return []
    factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
    async with factory() as db:
        rows = await db.execute(
            select(WorkflowInstance.state_data["erp_reference"].astext)
            .join(Invoice, Invoice.id == WorkflowInstance.invoice_id)
            .where(
                Invoice.status.in_(tuple(PAYABLE_HERE)),
                WorkflowInstance.state_data["erp_realm"].astext == realm,
                WorkflowInstance.state_data["erp_reference"].astext.isnot(None),
            )
        )
        return [str(r) for r in rows.scalars() if r]


async def sync_org(org: Organization, *, now: datetime, result: StatusSyncResult) -> None:
    """One connected company's pass. Raises on our own failure; the high-water
    mark moves only when the whole pass went through."""
    from app.services.erp_adapters.base import ErpListingIncomplete
    from app.services.erp_link_realm import connection_realm
    from app.services.erp_oauth import parse_timestamp, update_connection_metadata
    from app.services.erp_payment_writeback import resume_pending_voids

    oauth_meta = ((org.settings or {}).get("erp") or {}).get("oauth") or {}
    erp_config = await _resolved_erp(org)
    adapter = _adapter_or_none(erp_config or {})
    if adapter is None or not adapter.supports_status_sync:
        return
    hours = max(1, min(int(settings.erp_status_sync_lookback_hours), 24 * 30))
    since = now - timedelta(hours=hours)
    mark = parse_timestamp(oauth_meta.get(SYNCED_THROUGH_KEY))
    # No mark (a first pass, or a new connection after a reconnect), or one
    # older than CDC can reach: CDC alone cannot prove nothing was missed.
    full = mark is None or mark < now - _CDC_REACH
    if mark is not None:
        since = min(since, mark)
    since = max(since, now - _CDC_REACH)
    try:
        bill_ids = await adapter.changed_bills(since)
    except ErpListingIncomplete:
        bill_ids, full = [], True  # more changes than one CDC answer holds
    if full:
        result.full_reconciliations += 1
        bill_ids = [*bill_ids, *await _open_bill_ids(org, connection_realm(erp_config))]
    result.bills_read += len(set(bill_ids))
    if bill_ids:
        result.invoices_changed += await sync_bills(org, bill_ids, event_id=None)
    result.voids_pending += await resume_pending_voids(org)
    await update_connection_metadata(
        QBO_PROVIDER,
        org.id,
        str(oauth_meta.get("connection_id") or ""),
        {SYNCED_THROUGH_KEY: now.isoformat()},
    )


async def run_status_sync_once(*, now: datetime | None = None) -> StatusSyncResult:
    """One pass over every tenant connected to QuickBooks: ask its company for
    the bills and bill payments changed since its last complete pass (at least
    the lookback window) and apply each bill; read back every still-payable
    bill when CDC cannot vouch for the gap. Re-applying a report changes
    nothing. One tenant's failure never stops the others."""
    result = StatusSyncResult()
    if not settings.erp_status_sync_enabled:
        return result
    moment = now or datetime.now(UTC)
    oauth = Organization.settings["erp"]["oauth"]
    async with control_session_factory() as ctrl:
        orgs = (
            (
                await ctrl.execute(
                    select(Organization).where(oauth["provider"].astext == QBO_PROVIDER)
                )
            )
            .scalars()
            .all()
        )
    for org in orgs:
        oauth_meta = ((org.settings or {}).get("erp") or {}).get("oauth") or {}
        if oauth_meta.get("needs_reconnect"):
            # Nothing to read with; the connection-health notice tells the
            # admins, and the reconnect starts a new connection with no mark,
            # so its first pass reads back every payable bill.
            continue
        result.orgs_scanned += 1
        try:
            await sync_org(org, now=moment, result=result)
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            result.failures += 1
            logger.warning("[erp-status-sync] org %s failed: %s", org.id, exc.__class__.__name__)
    if result.invoices_changed or result.failures:
        logger.info(
            "[erp-status-sync] %d org(s), %d bill(s) read, %d invoice(s) changed, %d failed",
            result.orgs_scanned,
            result.bills_read,
            result.invoices_changed,
            result.failures,
        )
    return result


async def run_status_sync_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; body is the shared runner."""
    from app.services.sweep_health import SWEEP_ERP_STATUS_SYNC, run_sweep_loop

    await run_sweep_loop(
        SWEEP_ERP_STATUS_SYNC,
        lambda: run_status_sync_once(),
        interval_seconds=settings.erp_status_sync_interval_seconds,
        log=logger,
        log_prefix="[erp-status-sync]",
    )
