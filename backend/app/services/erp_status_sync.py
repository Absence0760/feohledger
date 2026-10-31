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
from app.services.invoice_warning_catalog import (
    ERP_UNNAMED_SUFFIX,
    ERP_UNRECORDED_REASONS,
    exception_finding,
)
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


#: ``Exception.erp_report["kind"]`` values — what the ERP reported that a
#: person now has to reconcile (decisions §319).
#:
#: * ``paid_not_recorded`` — the ERP reported the bill paid and FeohLedger could
#:   not record that payment. The row keeps the payment's facts, which is what
#:   :func:`retry_erp_reported_payments` and the queue's one-click record read.
#: * ``settled_short`` — the ERP reported paid while FeohLedger's own payment
#:   settled short of what AP authorized.
#: * ``voided_after_post`` — the ERP voided a bill FeohLedger had already
#:   moved past the point a void can be applied.
ERP_REPORT_PAID_NOT_RECORDED = "paid_not_recorded"
ERP_REPORT_SETTLED_SHORT = "settled_short"
ERP_REPORT_VOIDED_AFTER_POST = "voided_after_post"

#: How each ERP adapter is named to an AP user. An adapter missing here reads
#: as "your ERP" — never its raw registry key.
_ERP_DISPLAY_NAMES = {
    "quickbooks_online": "QuickBooks Online",
    "netsuite": "NetSuite",
    "xero": "Xero",
    "dynamics_365_bc": "Dynamics 365 Business Central",
    "sage_intacct": "Sage Intacct",
}


def erp_product_name(erp_type: str | None) -> str | None:
    """The ERP's product name (a proper noun, never translated), or None for an
    adapter with none — a client then says "your ERP" in its own language."""
    return _ERP_DISPLAY_NAMES.get((erp_type or "").strip().lower())


def erp_finding(name: str, erp_type: str | None, /, **params: object) -> dict:
    """A catalogue finding for an ERP sentence that opens with the ERP's name.

    ``name`` is the bare catalogue name (``"erp_settled_short"``). With a product
    name the ``{erp}`` code is used; without one its ``_unnamed`` twin, which
    says "Your ERP" — a word, so a code of its own, never a spliced param
    (``invoice_warning_catalog.ERP_UNNAMED_SUFFIX``).
    """
    product = erp_product_name(erp_type)
    if product is None:
        return exception_finding(name + ERP_UNNAMED_SUFFIX, **params)
    return exception_finding(name, erp=product, **params)


_REFUSAL_PREFIX = "external_payment_"


def unrecorded_payment_finding(erp_type: str | None, refusal_code: str) -> dict:
    """The sentence an AP user reads on a ``paid_not_recorded`` exception.

    What happened and what to do — no API path, no ids, no refusal code: those
    are machine detail and live in ``Exception.erp_report``. One catalogue code
    per refusal (``ERP_UNRECORDED_REASONS``); an unlisted refusal reads the
    generic ``other`` clause.
    """
    reason = refusal_code.removeprefix(_REFUSAL_PREFIX)
    if not refusal_code.startswith(_REFUSAL_PREFIX) or reason not in ERP_UNRECORDED_REASONS:
        reason = "other"
    return erp_finding(f"erp_paid_unrecorded_{reason}", erp_type)


def _set_finding(row: APException, finding: dict) -> None:
    """Restate a live row in ``finding``'s words: the English fallback and the
    code + params a client localizes on move together, never one without the
    other."""
    row.description = finding["message"]
    row.description_code = finding.get("code")
    row.description_params = finding.get("params")


async def open_erp_reconciliation_exception(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id,
    finding: dict | None = None,
    description: str | None = None,
    exception_type: str = ERP_RECONCILIATION_EXCEPTION_TYPE,
    erp_report: dict | None = None,
) -> bool:
    """Open ONE reconciliation exception for human review, PII-free.

    **Idempotent**: skipped when an open or escalated exception of the same type
    already exists for the invoice, so distinct ERP events (or a webhook and the
    sweep reporting the same change) never pile up duplicates. Returns whether
    one was opened.

    A ``paid_not_recorded`` report goes through :func:`_keep_erp_paid_report`
    instead, which keeps at most ONE live such row per invoice and refreshes it.

    ``finding`` is the sentence for a person, as an ``invoice_warning_catalog``
    finding (``erp_finding`` / ``exception_finding``): its English is stored as
    ``description`` and its code + params let each client render it in the
    reader's language. It carries nothing from the ERP's payload, which may
    include vendor bank / tax / address fields. ``erp_report`` is the PII-free
    machine detail, as data (decisions §319).

    ``description`` (plain English, no code) remains only for the QuickBooks
    write-back's sync-failure sentences (``erp_payment_writeback``), which carry
    machine codes like the other sync-back failures and are not catalogued yet
    (``backend/docs/invoice-warnings.md`` § What is NOT localized). Pass exactly
    one of the two.
    """
    if (finding is None) == (description is None):
        raise TypeError("pass exactly one of `finding` or `description`")
    if finding is None:
        finding = {"message": description, "code": None, "params": None}
    if (erp_report or {}).get("kind") == ERP_REPORT_PAID_NOT_RECORDED:
        return await _keep_erp_paid_report(
            db,
            invoice,
            org_id=org_id,
            finding=finding,
            exception_type=exception_type,
            erp_report=erp_report,
        )
    if await _live_of_type(db, invoice.id, exception_type):
        return False
    await _create_reconciliation(
        db,
        invoice,
        org_id=org_id,
        finding=finding,
        exception_type=exception_type,
        erp_report=erp_report,
    )
    return True


async def _live_of_type(db: AsyncSession, invoice_id: uuid.UUID, exception_type: str) -> bool:
    existing = await db.execute(
        select(func.count()).where(
            APException.invoice_id == invoice_id,
            APException.exception_type == exception_type,
            APException.status.in_(("open", "escalated")),
        )
    )
    return (existing.scalar() or 0) > 0


def _merged_report(old: dict | None, new: dict, *, keep_refusal: bool) -> dict:
    """A later report folded into the facts a live row already holds.

    The payment is the one the ERP first reported, unless the ERP now says more:
    a stated date replaces a stand-in one but an undated report never replaces
    a date; a document id replaces an event-derived reference but an id-less
    report never replaces a reference (the event id changes per delivery, and
    the reference is the replay key `_same_record` reads). ``first_reported_at``
    keeps the age of the claim.
    """
    merged = dict(new)
    old = old or {}
    if not new.get("paid_on_reported") and old.get("paid_on"):
        merged["paid_on"] = old["paid_on"]
        merged["paid_on_reported"] = bool(old.get("paid_on_reported"))
    if not new.get("erp_document_id") and old.get("reference"):
        merged["reference"] = old["reference"]
        merged["erp_document_id"] = old.get("erp_document_id")
    merged["first_reported_at"] = old.get("first_reported_at") or old.get("reported_at")
    if keep_refusal and old.get("refusal_code"):
        merged["refusal_code"] = old["refusal_code"]
    return merged


async def _keep_erp_paid_report(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id,
    finding: dict,
    exception_type: str,
    erp_report: dict,
) -> bool:
    """Keep ONE live ``paid_not_recorded`` row per invoice, whatever its type.

    * A live BLOCKING row (a double-pay refusal) stays the row. A later report
      is refused BY it (its own type blocks the record), which says nothing new
      about why, so its refusal and sentence stand unless the new refusal is
      itself a double-pay one; its payment facts are refreshed.
    * A new double-pay refusal over a live non-blocking row opens the blocking
      row — the run must stop — and closes the non-blocking one as superseded.
      If an older blocker of that type with no ERP facts is already open, the
      run is stopped anyway: the non-blocking row is refreshed instead.
    * Otherwise the live non-blocking row is refreshed, or a row is opened.

    Two live rows would each offer to record the same payment, and a stale one
    would be re-attempted against a decision made on the other.
    """
    from app.services.exception_lifecycle import is_payment_blocking

    live = await live_erp_paid_reports(db, [invoice.id])
    blocking = [r for r in live if is_payment_blocking(r.exception_type)]
    others = [r for r in live if not is_payment_blocking(r.exception_type)]
    new_blocking = is_payment_blocking(exception_type)

    if blocking:
        row = blocking[0]
        row.erp_report = _merged_report(row.erp_report, erp_report, keep_refusal=not new_blocking)
        if new_blocking:
            _set_finding(row, finding)
        await _supersede(db, invoice, others)
        return False
    if new_blocking and not await _live_of_type(db, invoice.id, exception_type):
        await _supersede(db, invoice, others)
        await _create_reconciliation(
            db,
            invoice,
            org_id=org_id,
            finding=finding,
            exception_type=exception_type,
            erp_report=erp_report,
        )
        return True
    if others:
        row = others[0]
        row.erp_report = _merged_report(row.erp_report, erp_report, keep_refusal=False)
        _set_finding(row, finding)
        await _supersede(db, invoice, others[1:])
        return False
    # The facts ride a row of their own, never swallowed by the per-type dedupe
    # other ERP rows use. Non-blocking even for a double-pay refusal here: we
    # only reach this with one when a blocker of that type is already open,
    # which stops the run on its own.
    await _create_reconciliation(
        db,
        invoice,
        org_id=org_id,
        finding=finding,
        exception_type=ERP_RECONCILIATION_EXCEPTION_TYPE,
        erp_report=erp_report,
    )
    return True


_SUPERSEDED = "Closed automatically: a later report from the ERP replaced this one."


async def _supersede(db: AsyncSession, invoice: Invoice, rows: list[APException]) -> None:
    """Close NON-blocking ``paid_not_recorded`` rows a newer row replaces —
    through ``record_decision``, so each close is audited. Never a blocking
    row: closing one would be a machine clearing a payment block."""
    from app.services.exception_lifecycle import record_decision

    for row in rows:
        await record_decision(
            db,
            exception=row,
            action="resolve",
            resolution=_SUPERSEDED,
            actor_id=None,
            actor_name=ERP_REPORT_CLOSER,
            invoice=invoice,
            via="erp_superseded",
        )


async def _create_reconciliation(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id,
    finding: dict,
    exception_type: str,
    erp_report: dict | None,
) -> None:
    await create_exception(
        db,
        exception_type=exception_type,
        severity="error",
        description=finding["message"],
        description_code=finding.get("code"),
        description_params=finding.get("params"),
        status="open",
        organization_id=org_id,
        invoice=invoice,
        # Raised from what the tenant's ERP reported, not by a control-plane user.
        raised_by_user_id=None,
        erp_report=erp_report,
    )


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
    paid_on_reported: bool | None = None,
) -> bool:
    """Book the ERP's `Paid` as a payment made outside FeohLedger.

    Through `services/external_payment.record_invoice_paid_outside` — the same
    gates a user's record goes through, minus segregation of duties (no human
    actor). ``paid_on`` / ``method`` are the ERP's own payment date and rail
    when it states them (already validated: :func:`reported_paid_on`,
    :func:`reported_payment_method`); absent, the payment is dated the day the
    report arrived and its rail is NULL (1099-reportable). A refusal (an open
    payment-blocking exception, a live FeohLedger payment or card that would
    make this a double payment, a credit we can't net) is never silent: it
    opens a reconciliation exception that says, in an AP user's words, what
    stopped it — and keeps the payment's facts in ``erp_report`` so it can be
    recorded later without anyone re-keying them (decisions §319). Returns
    whether the payment was recorded. Never commits.

    ``paid_on_reported`` says whether ``paid_on`` came from the ERP. A
    re-attempt (:func:`retry_erp_reported_payments`) passes the stored flag,
    since by then ``paid_on`` is always set.
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

    if paid_on_reported is None:
        paid_on_reported = paid_on is not None
    # Fixed at the first report, never at a later re-attempt: the payment is
    # dated the day the ERP said, or the day it reported, never the day a
    # blocker happened to clear.
    paid_on = paid_on or utc_today()
    reference = erp_paid_reference(erp_type, erp_document_id, event_id)
    try:
        await record_invoice_paid_outside(
            db,
            org=org,
            invoice=invoice,
            method=method,
            reference=reference,
            paid_on=paid_on,
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
            finding=unrecorded_payment_finding(erp_type, exc.refusal.code),
            erp_report={
                "kind": ERP_REPORT_PAID_NOT_RECORDED,
                "erp_type": erp_type,
                "erp_document_id": erp_document_id,
                "event_id": str(event_id) if event_id else None,
                "method": method,
                "reference": reference,
                "paid_on": paid_on.isoformat(),
                "paid_on_reported": paid_on_reported,
                "refusal_code": exc.refusal.code,
                "reported_at": datetime.now(UTC).isoformat(),
            },
        )
        return False
    return True


def erp_paid_reference(erp_type: str, erp_document_id: str | None, event_id: str | None) -> str:
    """The reference an ERP-reported payment is recorded under: the ERP's own
    document id, else the event, else the ERP's name. Stable for one report,
    which is what makes a re-attempt an idempotent replay."""
    return (erp_document_id or (f"erp-event:{event_id}" if event_id else None) or erp_type)[:255]


def is_erp_paid_report(exception: APException) -> bool:
    """Does this row carry an ERP-reported payment FeohLedger could not record?"""
    report = getattr(exception, "erp_report", None)
    return isinstance(report, dict) and report.get("kind") == ERP_REPORT_PAID_NOT_RECORDED


async def live_erp_paid_reports(
    db: AsyncSession, invoice_ids: list[uuid.UUID], *, fresh: bool = False
) -> list[APException]:
    """The open / escalated ``paid_not_recorded`` rows on these invoices, newest first.

    ``fresh`` re-reads loaded rows from the database instead of serving the
    identity map's copy — for a caller that acts on the facts under a lock.
    """
    if not invoice_ids:
        return []
    query = (
        select(APException)
        .where(
            APException.invoice_id.in_(invoice_ids),
            APException.status.in_(("open", "escalated")),
            APException.erp_report["kind"].astext == ERP_REPORT_PAID_NOT_RECORDED,
        )
        .order_by(APException.created_at.desc(), APException.id.desc())
    )
    if fresh:
        query = query.execution_options(populate_existing=True)
    return list((await db.execute(query)).scalars().all())


#: ``resolved_by`` on a row FeohLedger closed itself because the payment it was
#: about has been recorded. The audit row's ``actor_id`` still names the person
#: whose act recorded it, when a person's did.
ERP_REPORT_CLOSER = "FeohLedger"
_CLOSED_BY_RECORD = "Closed automatically: the payment the ERP reported has been recorded."


async def close_erp_paid_reports(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID | None,
    actor_name: str | None = None,
    org_settings: dict | None = None,
) -> int:
    """Resolve every live ``paid_not_recorded`` row on ``invoice``.

    Called once the invoice HAS been recorded as paid outside FeohLedger
    (``external_payment._complete``), by whichever door: the automatic
    re-attempt, the queue's one-click record, a user's own "Record as paid", a
    run recorded outside. The question the row asked is answered, and leaving
    it open would only invite a second record.

    Through ``exception_lifecycle.record_decision``, so each close writes its
    ``exception.resolved`` audit row, as ``actor_name`` when a person recorded
    it. A payment-blocking row is live here only on the one-click path, which
    has already applied its clearing rules to that caller under lock; the same
    rule runs again here with the org's own settings.
    Returns how many were closed.
    """
    from app.services.exception_lifecycle import record_decision

    rows = await live_erp_paid_reports(db, [invoice.id])
    for row in rows:
        await record_decision(
            db,
            exception=row,
            action="resolve",
            resolution=_CLOSED_BY_RECORD,
            actor_id=actor_id,
            actor_name=actor_name or ERP_REPORT_CLOSER,
            invoice=invoice,
            via="payment_recorded",
            org_settings=org_settings,
        )
    return len(rows)


#: ``erp_report["kind"]`` of a ``paid_not_recorded`` row whose payment the ERP
#: has since withdrawn — the bill reads open, part-paid, deleted or voided.
ERP_REPORT_PAID_WITHDRAWN = "paid_withdrawn"


async def withdraw_erp_paid_reports(
    db: AsyncSession, invoice: Invoice, *, erp_type: str, erp_state: str
) -> int:
    """The ERP no longer shows the bill paid: retire the payment it reported.

    Every live ``paid_not_recorded`` row on the invoice stops being one — its
    kind becomes ``paid_withdrawn``, so nothing re-attempts the record and no
    one-click offers it. The row itself stays OPEN for a person, with a
    sentence saying so: a blocking row in particular is never cleared by a
    machine. Without this, a payment the ERP had since voided would be booked
    the moment someone cleared an unrelated blocker, and an invoice still owed
    would read paid (decisions §319). Returns how many were withdrawn.
    """
    rows = await live_erp_paid_reports(db, [invoice.id])
    for row in rows:
        row.erp_report = {
            **(row.erp_report or {}),
            "kind": ERP_REPORT_PAID_WITHDRAWN,
            "withdrawn_as": erp_state,
            "withdrawn_at": datetime.now(UTC).isoformat(),
        }
        _set_finding(row, erp_finding("erp_paid_withdrawn", erp_type))
    return len(rows)


async def retry_erp_reported_payments(
    db: AsyncSession, *, invoice_id: uuid.UUID, organization_id: uuid.UUID
) -> bool:
    """Re-attempt recording the ERP's payment once a blocker on the invoice clears.

    Called by ``exception_lifecycle.record_decision`` when a payment-blocking
    exception on ``invoice_id`` is resolved or dismissed. If the invoice has a
    live, NON-blocking ``paid_not_recorded`` row, its stored facts go back
    through :func:`record_erp_reported_payment` — the very gates the first
    attempt ran, so nothing is bypassed: another open blocker, a FeohLedger
    payment or card that would pay twice, a credit we can't net all still
    refuse, and a refusal refreshes the row with the new reason. On success
    ``external_payment._complete`` closes the row.

    A BLOCKING ``paid_not_recorded`` row (the double-pay refusals) is never
    retried here: it refuses the record itself, by design, until a person who
    may clear it decides the ERP's payment is the one that stands — which is the
    queue's one-click record (``POST /api/exceptions/{id}/record-erp-payment``).

    Same transaction as the decision that triggered it; never commits.
    Idempotent: nothing is live once it succeeds. Returns whether a payment
    was recorded.
    """
    from app.services.exception_lifecycle import is_payment_blocking

    def _candidates(rows: list[APException]) -> list[APException]:
        return [row for row in rows if not is_payment_blocking(row.exception_type)]

    # A cheap look first, so the common case (nothing kept) costs one query and
    # takes no lock. NOT what is acted on: see below.
    if not _candidates(await live_erp_paid_reports(db, [invoice_id])):
        return False
    try:
        async with control_session_factory() as ctrl:
            org = await ctrl.get(Organization, organization_id)
    except Exception:  # noqa: BLE001 — isolated follow-on, like the attempt below
        logger.exception("ERP-reported payment re-attempt: org unreadable: org=%s", organization_id)
        return False
    if org is None:
        return False
    # In a SAVEPOINT: this is a follow-on to the decision that triggered it. A
    # failure of ours here (a lock timeout, a bug) must not undo a person's
    # clearing of a duplicate or fraud flag. It rolls back only this attempt —
    # and `post_commit` drops whatever the attempt queued. A REFUSAL is not a
    # failure: `record_erp_reported_payment` turns it into the refreshed row.
    try:
        async with db.begin_nested():
            invoice = (
                await db.execute(
                    select(Invoice)
                    .where(Invoice.id == invoice_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if invoice is None or invoice.status not in ERP_PAID_RECORDABLE:
                # Paid some other way, voided back, or not posted yet: no longer
                # the question the row asked, so a person reads it as it stands.
                return False
            # Which payment, chosen only NOW, under the invoice lock every ERP
            # writer takes before touching these rows, and re-read rather than
            # served from the identity map — so a withdrawal the sweep committed
            # while this transaction waited is seen, and never recorded.
            fresh = _candidates(await live_erp_paid_reports(db, [invoice_id], fresh=True))
            if not fresh:
                return False
            facts = fresh[0].erp_report or {}
            try:
                paid_on = date.fromisoformat(str(facts.get("paid_on")))
            except ValueError:
                return False
            return await record_erp_reported_payment(
                db,
                invoice,
                org=org,
                erp_type=str(facts.get("erp_type") or ""),
                erp_document_id=facts.get("erp_document_id"),
                event_id=facts.get("event_id"),
                paid_on=paid_on,
                method=reported_payment_method(facts.get("method")),
                paid_on_reported=bool(facts.get("paid_on_reported")),
            )
    except Exception:  # noqa: BLE001 — isolated follow-on; logged with its traceback
        # ids only — no vendor, no amount, no reference.
        logger.exception(
            "ERP-reported payment re-attempt failed: invoice=%s org=%s", invoice_id, organization_id
        )
        await _mark_retry_failed(db, invoice_id)
        return False


async def _mark_retry_failed(db: AsyncSession, invoice_id: uuid.UUID) -> None:
    """After a re-attempt failed on OUR side, stop the kept rows promising an
    automatic record that will not come: stamp the failure, and point the
    reader at the one-click record instead. Best-effort — the failure is
    already logged, and this must not fail the decision either."""
    try:
        for row in await live_erp_paid_reports(db, [invoice_id], fresh=True):
            row.erp_report = {
                **(row.erp_report or {}),
                "retry_failed_at": datetime.now(UTC).isoformat(),
            }
            _set_finding(
                row,
                erp_finding("erp_paid_retry_failed", (row.erp_report or {}).get("erp_type")),
            )
    except Exception:  # noqa: BLE001 — best-effort annotation of an already-logged failure
        logger.exception(
            "ERP-reported payment: could not mark retry failure: invoice=%s", invoice_id
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


#: ``erp_report["kind"]`` of a row status sync-back opened from a bill report
#: (``apply_bill_report``), of one the pre-dispatch check opened
#: (``check_erp_bill_before_dispatch``), of one for a bill this connection
#: cannot read, and of one a void of an ERP-reported payment opened. Machine
#: detail only — what the row asks is in its description.
ERP_REPORT_BILL_STATE = "bill_state"
ERP_REPORT_DISPATCH_STOPPED = "dispatch_stopped"
ERP_REPORT_BILL_UNLINKED = "bill_unlinked"
ERP_REPORT_PAYMENT_VOIDED = "erp_payment_voided"


def _bill_report_detail(report: ErpBillReport, provider: str, event_id: str | None) -> dict:
    """What used to ride the sentence as ``(provider=…, bill=…, event=…)``, as
    data. ``event_id`` None means the reconciliation sweep read it."""
    return {
        "kind": ERP_REPORT_BILL_STATE,
        "erp_type": provider,
        "erp_document_id": report.bill_id,
        "event_id": str(event_id) if event_id else None,
        "source": "webhook" if event_id else "reconciliation_sweep",
        "bill_state": report.state.value,
    }


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
    detail = _bill_report_detail(report, erp_type, event_id)
    action = "unchanged"

    if report.state in (ErpBillState.open, ErpBillState.partially_paid, ErpBillState.deleted):
        # Whatever else this report means, it is no longer a bill paid in full:
        # a payment the ERP reported earlier, which FeohLedger could not record,
        # must never be recorded now (decisions §319).
        if await withdraw_erp_paid_reports(
            db, invoice, erp_type=erp_type, erp_state=report.state.value
        ):
            action = "erp_payment_withdrawn"

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
                finding=exception_finding("erp_credit_settlement"),
                erp_report=detail,
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
                finding=exception_finding("erp_paid_while_scheduled"),
                erp_report=detail,
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
                finding=exception_finding("erp_paid_twice"),
                erp_report=detail,
            )
            action = "double_payment_flagged" if opened else action
    elif report.state is ErpBillState.partially_paid and report.reduced_outside:
        if status in PAYABLE_HERE:
            opened = await open_erp_reconciliation_exception(
                db,
                invoice,
                org_id=org.id,
                exception_type=PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
                finding=exception_finding("erp_partly_paid_outside"),
                erp_report=detail,
            )
            action = "partial_payment_flagged" if opened else action
    elif report.state is ErpBillState.open and status == InvoiceStatus.paid:
        if await _paid_by_erp_report(db, invoice.id, report.bill_id):
            opened = await open_erp_reconciliation_exception(
                db,
                invoice,
                org_id=org.id,
                finding=exception_finding("erp_recorded_payment_withdrawn"),
                erp_report=detail,
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
            finding=exception_finding(
                "erp_bill_deleted_blocking" if blocking else "erp_bill_deleted"
            ),
            erp_report=detail,
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


#: Dispatch refusals of :func:`erp_bill_dispatch_refusal` (decisions §296).
#: All are made before any rail is called, so all are retry-safe
#: (`payment_runs._RETRY_SAFE_FAILURE_PREFIXES`).
ERP_BILL_NOT_OPEN = "erp_bill_not_open"
ERP_BILL_UNVERIFIED = "erp_bill_unverified"
#: The bill reads paid only by OUR OWN write-back, whose QuickBooks void is
#: still pending after a FeohLedger void: nothing outside us paid it, so no
#: person needs to clear anything — hold until the void lands.
ERP_BILL_VOID_PENDING = "erp_bill_void_pending"
#: The bill can never be read under this connection — the push named no
#: document, recorded no company, or the company is not the one connected now
#: (a sandbox → production switch). Held like the others, but a retry cannot
#: clear it, so a non-blocking ``erp_reconciliation`` gives it an owner.
ERP_BILL_UNLINKED = "erp_bill_unlinked"

#: After a read fails for an org, its further dispatch checks hold without
#: reading for this long: a degraded QuickBooks would otherwise cost every
#: item of a long run the full timeout, with its invoice row locked each time.
_UNREACHABLE_HOLD_SECONDS = 60.0
_unreachable_until: dict[uuid.UUID, float] = {}


def _org_unreachable(org_id: uuid.UUID) -> bool:
    import time

    until = _unreachable_until.get(org_id)
    if until is None:
        return False
    if time.monotonic() >= until:
        _unreachable_until.pop(org_id, None)
        return False
    return True


def _mark_unreachable(org_id: uuid.UUID) -> None:
    import time

    _unreachable_until[org_id] = time.monotonic() + _UNREACHABLE_HOLD_SECONDS


async def _bounded_bill_report(adapter: ErpAdapter, bill_id: str) -> ErpBillReport:
    """The bill, read within ``FEOH_ERP_DISPATCH_CHECK_TIMEOUT_SECONDS`` as a
    whole (the bill and its payments together). A token refresh inside it is
    safe to cut short: ``erp_oauth.get_access_token`` runs the rotation and
    its persistence as one shielded task."""
    bound = max(1.0, float(settings.erp_dispatch_check_timeout_seconds))
    return await asyncio.wait_for(adapter.bill_report(bill_id), timeout=bound)


def _connection_level(exc: BaseException) -> bool:
    """Does this failure say QuickBooks (or our connection to it) is down, as
    opposed to something about this one bill? Only those trip the org-wide
    hold; a 400 on one bill must not hold every other item of the run."""
    import httpx

    from app.services import erp_oauth

    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TransportError)):
        return True
    if isinstance(exc, (erp_oauth.ErpNotConnectedError, erp_oauth.ErpTokenRefreshError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status >= 500 or status in (401, 408, 429))


async def erp_bill_dispatch_refusal(
    db: AsyncSession,
    *,
    invoice: Invoice,
    org: Organization,
    payment: Payment | None = None,
) -> str | None:
    """Why money must not move for ``invoice`` now because of its ERP bill —
    or None. Decisions §296.

    Called immediately before money moves, with the invoice row locked:
    ``api/payments._execute_single_payment`` after ``dispatch_preflight`` and
    before the rail adapter (every rail dispatch: execute, resume,
    retry-failed, a standalone payment, compliance release, the virtual-card
    leg), the NACHA export, and ``POST /api/cards/generate`` before it mints.
    ``dispatch_preflight`` already re-confirmed the LOCAL facts in that lock.
    This adds the one fact no local row can know: whether the bill is still
    unpaid IN QUICKBOOKS right now. A bill paid there seconds ago — after the
    run was approved, or between two items of one run — has not reached us yet
    by webhook or sweep, and paying it here pays it twice.

    Only for an ERP whose bills sync back (``status_sync_active``) and an
    invoice posted there. Then a live ``bill_report``, bounded overall by
    ``FEOH_ERP_DISPATCH_CHECK_TIMEOUT_SECONDS``:

    * **open** (nothing taken off it, by anyone) → None: pay.
    * **taken down outside FeohLedger** (paid, part-paid, credited) or
      **deleted**, or a state we cannot classify → ``erp_bill_not_open:<state>``,
      and the payment-BLOCKING ``payment_reconciliation`` is opened in the
      caller's transaction, so no later run or ``/retry-failed`` pays it until
      a person clears it.
    * **paid only by our own write-back** (a FeohLedger void whose QuickBooks
      void is still pending) → ``erp_bill_void_pending``: held, no exception —
      the pending-void sweep resolves it, and nothing outside us paid it.
    * **unverifiable right now** — the read failed or timed out (and, after a
      connection-level failure, every check of that org for a minute without
      reading) → ``erp_bill_unverified``: HELD, never paid blind. No
      exception: nothing is known to be wrong, and ``/retry-failed`` re-checks.
    * **unreadable for good** under this connection — the push named no
      document, recorded no company, or the bill is in a company other than
      the one connected now → ``erp_bill_unlinked:<why>``: held, with a
      non-blocking ``erp_reconciliation`` so the way out (record it as paid
      outside, or re-post) has an owner.

    Idempotent: a read, plus a de-duplicated exception. Never commits.
    """
    from app.services.erp_link_realm import connection_realm

    erp_config = (org.settings or {}).get("erp")
    if not status_sync_active(erp_config):
        return None
    state = (
        await db.execute(
            select(WorkflowInstance.state_data).where(WorkflowInstance.invoice_id == invoice.id)
        )
    ).scalar_one_or_none() or {}
    bill_id = str(state.get("erp_reference") or "")
    if not bill_id:
        return None  # never posted to the ERP: no bill there to pay twice
    recorded_realm = state.get("erp_realm")
    unlinked = None
    if bill_id == "UNKNOWN":
        unlinked = "no_document"  # the push confirmed without naming its bill
    elif not recorded_realm:
        unlinked = "no_company"  # the push recorded no company
    elif recorded_realm != connection_realm(erp_config):
        unlinked = "other_company"  # e.g. pushed to the sandbox, now on production
    if unlinked is not None:
        # A bill may exist that we cannot find or read under this connection,
        # and no retry changes that: hold, and give it an owner.
        await open_erp_reconciliation_exception(
            db,
            invoice,
            org_id=org.id,
            finding=exception_finding(f"erp_bill_unlinked_{unlinked}"),
            erp_report={
                "kind": ERP_REPORT_BILL_UNLINKED,
                "erp_type": erp_config.get("type"),
                "erp_document_id": bill_id,
                "unlinked": unlinked,
            },
        )
        return f"{ERP_BILL_UNLINKED}:{unlinked}"
    adapter = _adapter_or_none(erp_config)
    if adapter is None or _org_unreachable(org.id):
        return ERP_BILL_UNVERIFIED
    subject = payment.id if payment is not None else invoice.id
    try:
        report = await _bounded_bill_report(adapter, bill_id)
    except Exception as exc:  # noqa: BLE001 — any failure to read is "unknown", never "open"
        if _connection_level(exc):
            _mark_unreachable(org.id)
        logger.warning(
            "[erp-dispatch-check] bill for %s unverified: %s", subject, exc.__class__.__name__
        )
        return ERP_BILL_UNVERIFIED
    if report.state is ErpBillState.open and not report.reduced_outside:
        return None
    if (
        report.state in (ErpBillState.paid, ErpBillState.partially_paid)
        and not report.reduced_outside
        and state.get("erp_payment_voids_pending")
    ):
        # Paid only by our own write-back, whose void a FeohLedger void has
        # recorded and the sweep has not yet finished: nothing outside us paid
        # it. Without a pending void, "paid only by us" is unexplained and
        # falls through to the blocking flag below.
        return ERP_BILL_VOID_PENDING
    await open_erp_reconciliation_exception(
        db,
        invoice,
        org_id=org.id,
        exception_type=PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
        finding=exception_finding(
            "erp_dispatch_stopped_"
            + (
                report.state.value
                if report.state
                in (ErpBillState.paid, ErpBillState.partially_paid, ErpBillState.deleted)
                else "unconfirmed"
            )
        ),
        erp_report={
            "kind": ERP_REPORT_DISPATCH_STOPPED,
            "erp_type": erp_config.get("type"),
            "erp_document_id": bill_id,
            "bill_state": report.state.value,
            "payment_id": str(payment.id) if payment is not None else None,
        },
    )
    return f"{ERP_BILL_NOT_OPEN}:{report.state.value}"


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
        finding=exception_finding("erp_reported_payment_voided"),
        erp_report={
            "kind": ERP_REPORT_PAYMENT_VOIDED,
            "erp_document_id": bill_id,
            "payment_id": str(payment.id),
        },
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
