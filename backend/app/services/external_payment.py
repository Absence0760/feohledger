"""Recording a payment the customer made OUTSIDE FeohLedger.

The pilot's no-rail model (issue #517, `docs/decisions.md` §251): the customer
pays its suppliers from its own bank or ERP, and FeohLedger records that the
payment happened. Before this module a tenant with no rail had no honest path
to ``paid`` — the payment adapter was the only automatic writer of
``payment_scheduled → paid``, and on a tenant with no processor configured that
adapter is ``mock``, which reports success without moving money.

Three callers, one implementation, so they cannot disagree about what
"recorded as paid" means:

* ``POST /api/payments/record-outside`` — a user records one invoice as paid
  (cheque number, bank-portal reference).
* ``POST /api/payments/runs/{id}/record-outside`` — a whole draft run the
  customer paid through their bank (e.g. after uploading the NACHA file
  ``GET /api/payments/runs/{id}/nacha`` generated).
* ``api/erp_webhook`` — the customer's ERP reports an invoice ``Paid`` that
  FeohLedger never paid (``posted_in_erp → paid`` is not an edge of the state
  machine, so that report used to be silently dropped).

What a recorded payment IS: a ``Payment`` row in ``completed`` with
``provider = "external"``, its settled figure equal to what was recorded, and
``submitted_at = completed_at`` on the paid-on date. That is the shape every
downstream reader already understands, so bank reconciliation matches it, the
1099 YTD aggregate counts it (by ``completed_at`` year and ``method``) and the
invoice's ``paid`` status takes it out of aging — exactly as a rail-settled
payment does, with no reader special-casing it.

What it is NOT: verified. FeohLedger never saw the money move. The audit row
names the source (``user`` / ``erp``) and the Terms (§7.2) say a recorded
paid status is the customer's own record.

Controls, the same as the money path where the reason carries over:

* the invoice must be payable (approved / posted_in_erp / payment_scheduled);
* **segregation of duties** — the recorder may not be anyone implicated in
  creating the payable (``approval_chain.violates_segregation``, the approval
  rule), unless the org opted out with
  ``settings.payments.require_run_segregation: false`` (the single-operator
  switch every payment-side SoD check already honours);
* an unresolved payment-blocking exception refuses — marking an invoice paid
  would bury a duplicate / fraud flag under a closed item;
* a live virtual card, an applied credit that no longer pairs, or another live
  payment (one FeohLedger dispatched or holds in a run) refuses — each is a
  double-pay risk or a figure we cannot state;
* the amount is the invoice's net payable (credit memos, accepted early-pay
  discount at the paid-on date). A caller-stated amount must equal it; partial
  payments are not supported.

The vendor's verification status and the CFO threshold are deliberately NOT
gated: both exist to stop FeohLedger from SENDING money, and here the money
has already left the customer's bank. Refusing to record it would not un-send
it — it would only leave the ledger wrong. (Invoice approval, which the
payable-status gate requires, already applied the CFO approval threshold.)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.services.applied_credit_integrity import applied_credit_conflicts
from app.services.approval_chain import violates_segregation
from app.services.payment_controls import run_segregation_enabled
from app.services.payment_runs import (
    blocking_exception_types,
    card_claimed_invoice_ids,
    net_payable_amount,
    payable_amounts,
)
from app.services.workflow_engine import VALID_TRANSITIONS, transition_invoice

#: ``Payment.provider`` of every payment recorded through this module. Not a
#: registered payment adapter, so nothing can ever dispatch, void upstream or
#: accept a webhook under this name (`get_payment_adapter` fails closed on it).
EXTERNAL_PAYMENT_PROVIDER = "external"

#: Where the record came from — rides the audit row.
Source = Literal["user", "erp"]

# ── Refusal codes ─────────────────────────────────────────────────────────
# Stable, PII-free. The API turns them into coded refusals; the ERP webhook
# into an `erp_reconciliation` exception's description.
REFUSAL_NOT_PAYABLE = "external_payment_not_payable"
REFUSAL_SEGREGATION = "external_payment_segregation"
REFUSAL_BLOCKING_EXCEPTION = "external_payment_blocking_exception"
REFUSAL_CARD_LIVE = "external_payment_card_live"
REFUSAL_CREDIT_CONFLICT = "external_payment_credit_conflict"
REFUSAL_NOTHING_TO_PAY = "external_payment_nothing_to_pay"
REFUSAL_PAYMENT_LIVE = "external_payment_payment_live"
REFUSAL_PAYMENT_IN_RUN = "external_payment_in_run"
REFUSAL_AMOUNT_MISMATCH = "external_payment_amount_mismatch"
REFUSAL_AMOUNT_CHANGED = "external_payment_amount_changed"


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str
    #: 403 for an identity refusal (SoD), 422 for a bad figure, 409 otherwise.
    http_status: int = 409
    params: dict | None = None


class ExternalPaymentRefused(Exception):  # noqa: N818 — reads as a verdict
    def __init__(self, refusal: Refusal):
        self.refusal = refusal
        super().__init__(refusal.message)


@dataclass(frozen=True)
class RecordedPayment:
    payment: Payment
    #: False when an identical record already existed (idempotent replay).
    created: bool


def paid_on_timestamp(paid_on: date) -> datetime:
    """The instant a recorded payment is booked at: midnight UTC on ``paid_on``.

    Midnight UTC keeps the calendar date exact for every reader that extracts a
    UTC date or year from ``completed_at`` (the 1099 aggregate does), so a
    payment recorded for 31 December is never pushed into January.
    """
    return datetime.combine(paid_on, time.min, tzinfo=UTC)


async def _live_payments(db: AsyncSession, invoice_id: uuid.UUID) -> list[Payment]:
    from app.api.payments import LIVE_PAYMENT_TERMINAL_STATUSES

    return list(
        (
            await db.execute(
                select(Payment)
                .where(
                    Payment.invoice_id == invoice_id,
                    Payment.status.notin_(LIVE_PAYMENT_TERMINAL_STATUSES),
                )
                .order_by(Payment.created_at.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )


def _is_undispatched(payment: Payment) -> bool:
    """A payment FeohLedger booked but never handed to any rail."""
    return (
        payment.status == "pending"
        and payment.provider_payment_id is None
        and payment.submitted_at is None
    )


def _same_record(payment: Payment, *, reference: str, paid_on: date) -> bool:
    return (
        payment.provider == EXTERNAL_PAYMENT_PROVIDER
        and payment.status == "completed"
        and (payment.reference or "") == reference
        and payment.completed_at is not None
        and payment.completed_at.astimezone(UTC).date() == paid_on
    )


def segregation_refusal(
    invoice: Invoice, actor_id: uuid.UUID | None, org_settings: dict | None
) -> Refusal | None:
    """The approval rule, applied to recording the payment. ``None`` = allowed.

    A system source (the ERP) has no actor and is never refused here.
    """
    if actor_id is None:
        return None
    if not run_segregation_enabled((org_settings or {}).get("payments")):
        return None
    if violates_segregation(invoice, actor_id, {}):
        return Refusal(
            REFUSAL_SEGREGATION,
            "Segregation of duties: you created or shaped this invoice, so a different "
            "user must record its payment.",
            http_status=403,
        )
    return None


async def check_recordable(
    db: AsyncSession,
    *,
    org: Organization,
    invoice: Invoice,
    actor_id: uuid.UUID | None,
) -> Refusal | None:
    """Every invoice-level gate except the live-payment one. ``None`` = recordable.

    The caller holds ``invoice`` FOR UPDATE.
    """
    from app.api.payments import PAYABLE_INVOICE_STATUSES

    if invoice.status.value not in PAYABLE_INVOICE_STATUSES:
        return Refusal(
            REFUSAL_NOT_PAYABLE,
            "Only an approved invoice that has not been paid can be recorded as paid.",
            params={"status": invoice.status.value},
        )
    refusal = segregation_refusal(invoice, actor_id, org.settings)
    if refusal is not None:
        return refusal
    blocking = await blocking_exception_types(db, [invoice.id])
    if blocking:
        return Refusal(
            REFUSAL_BLOCKING_EXCEPTION,
            f"This invoice has an unresolved {blocking[invoice.id]} exception. Resolve it "
            "before recording the invoice as paid.",
            params={"exception_type": blocking[invoice.id]},
        )
    if await card_claimed_invoice_ids(db, [(invoice.id, None)]):
        return Refusal(
            REFUSAL_CARD_LIVE,
            "A live virtual card is issued against this invoice. Cancel the card before "
            "recording a payment made another way.",
        )
    if invoice.id in await applied_credit_conflicts(db, [invoice]):
        return Refusal(
            REFUSAL_CREDIT_CONFLICT,
            "An applied credit memo no longer matches this invoice's vendor or currency, "
            "so the amount owed can't be stated. Correct the invoice first.",
        )
    return None


async def _amount_owed(
    db: AsyncSession, invoice: Invoice, *, paid_on: date
) -> tuple[Decimal, uuid.UUID | None, Decimal | None]:
    net = await net_payable_amount(db, invoice)
    if net <= 0:
        raise ExternalPaymentRefused(
            Refusal(
                REFUSAL_NOTHING_TO_PAY,
                "This invoice is fully covered by applied credit memos — there is nothing "
                "to record as paid.",
            )
        )
    payable = (
        await payable_amounts(db, [invoice], pay_date=paid_on, net_amounts={invoice.id: net})
    )[invoice.id]
    if payable.discount_offer_id is not None and await _discount_predates_acceptance(
        db, payable.discount_offer_id, paid_on
    ):
        # A discount is the supplier's price for being paid early AFTER the
        # buyer accepted the offer. `payable_amounts` only checks the paid-on
        # date against the offer's deadline — right for a run, which always
        # pays today — but a recorded `paid_on` is the caller's word, and
        # backdating it into the window would book a saving the supplier never
        # granted. A payment dated before the acceptance owes the full net.
        return net, None, None
    return payable.amount, payable.discount_offer_id, payable.discount_amount


async def _discount_predates_acceptance(
    db: AsyncSession, offer_id: uuid.UUID, paid_on: date
) -> bool:
    from app.models.discount import DiscountOffer

    accepted_at = (
        await db.execute(select(DiscountOffer.accepted_at).where(DiscountOffer.id == offer_id))
    ).scalar_one_or_none()
    return accepted_at is None or paid_on < accepted_at.astimezone(UTC).date()


def _check_amount(stated: Decimal | None, owed: Decimal, currency: str | None) -> None:
    if stated is not None and stated != owed:
        raise ExternalPaymentRefused(
            Refusal(
                REFUSAL_AMOUNT_MISMATCH,
                "The amount must equal what the invoice owes, net of applied credit memos "
                "and any accepted early-payment discount. Partial payments can't be "
                "recorded.",
                http_status=422,
                params={"amount": str(owed), "currency": currency},
            )
        )


async def _complete(
    db: AsyncSession,
    *,
    org: Organization,
    invoice: Invoice,
    payment: Payment,
    method: str | None,
    reference: str,
    paid_on: date,
    actor_id: uuid.UUID | None,
    source: Source,
    converted: bool,
) -> None:
    """Book ``payment`` as completed outside FeohLedger and walk the invoice to ``paid``."""
    from app.api.payments import _capture_discount_offers
    from app.services.audit_dispatch import dispatch_audit

    at = paid_on_timestamp(paid_on)
    previous_status = payment.status if converted else None
    payment.status = "completed"
    payment.provider = EXTERNAL_PAYMENT_PROVIDER
    payment.method = method or payment.method
    payment.reference = reference
    payment.submitted_at = at
    payment.completed_at = at
    # What the customer says left their account: the amount owed, in the
    # invoice's own currency. No FX leg is recorded — FeohLedger never saw the
    # home-currency outflow, so realized FX is not booked (the helper returns
    # None for that case anyway, and inventing a rate would be worse).
    payment.settled_amount = payment.amount
    payment.settled_currency = (invoice.currency or "").upper() or None
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=payment.correlation_id or uuid.uuid4(),
        organization_id=org.id,
        actor_id=actor_id,
        action="payment.recorded_outside",
        entity_type="payment",
        entity_id=payment.id,
        details={
            # PII-free: ids, the exact amount as a decimal string, the rail and
            # the customer's own reference — never a bank or account number.
            "invoice_id": str(invoice.id),
            "amount": str(payment.amount),
            "currency": payment.settled_currency,
            "method": payment.method,
            "reference": payment.reference,
            "paid_on": paid_on.isoformat(),
            "source": source,
            "previous_status": previous_status,
            "payment_run_id": str(payment.payment_run_id) if payment.payment_run_id else None,
            "discount_amount": (
                str(payment.discount_amount) if payment.discount_amount is not None else None
            ),
        },
    )

    await _capture_discount_offers(
        db, org=org, payment=payment, actor_id=actor_id, now=at, invoice=invoice
    )

    details = {"payment_id": str(payment.id), "source": source}
    if InvoiceStatus.payment_scheduled in VALID_TRANSITIONS.get(invoice.status, set()):
        await transition_invoice(
            db,
            invoice,
            InvoiceStatus.payment_scheduled,
            actor_id=actor_id,
            action_name="invoice.payment_scheduled",
            details={**details, "result": "recorded_outside"},
        )
    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.paid,
        actor_id=actor_id,
        action_name=("invoice.paid_outside" if source == "user" else "invoice.paid_via_erp_report"),
        details=details,
    )


async def record_invoice_paid_outside(
    db: AsyncSession,
    *,
    org: Organization,
    invoice: Invoice,
    method: str | None,
    reference: str,
    paid_on: date,
    actor_id: uuid.UUID | None,
    source: Source,
    amount: Decimal | None = None,
) -> RecordedPayment:
    """Record one invoice as paid outside FeohLedger. Never commits.

    The caller holds ``invoice`` FOR UPDATE (`get_invoice_for_update`).

    **Idempotent**: replaying the same record (same reference and paid-on date)
    for an invoice it already paid returns the existing payment with
    ``created=False`` and writes nothing. Raises :class:`ExternalPaymentRefused`.
    """
    # Replay first: a second identical POST arrives with the invoice already
    # `paid`, which the payable-status gate would otherwise refuse.
    if invoice.status == InvoiceStatus.paid:
        for existing in await _live_payments(db, invoice.id):
            if _same_record(existing, reference=reference, paid_on=paid_on):
                return RecordedPayment(existing, created=False)

    refusal = await check_recordable(db, org=org, invoice=invoice, actor_id=actor_id)
    if refusal is not None:
        raise ExternalPaymentRefused(refusal)

    live = await _live_payments(db, invoice.id)
    target: Payment | None = None
    for existing in live:
        if existing.payment_run_id is not None:
            raise ExternalPaymentRefused(
                Refusal(
                    REFUSAL_PAYMENT_IN_RUN,
                    "This invoice is in a payment run. Record the whole run as paid "
                    "outside FeohLedger, or cancel the run first.",
                    params={"payment_run_id": str(existing.payment_run_id)},
                )
            )
        if not _is_undispatched(existing) or target is not None:
            raise ExternalPaymentRefused(
                Refusal(
                    REFUSAL_PAYMENT_LIVE,
                    "A payment for this invoice is already in progress or settled. Void "
                    "it before recording a payment made another way.",
                    params={"payment_status": existing.status},
                )
            )
        target = existing

    if target is not None:
        # A standalone booking FeohLedger never dispatched: the customer paid it
        # themselves. Complete it in place rather than leave it holding the
        # live-payment slot. Its booked amount is what is owed.
        _check_amount(amount, target.amount, invoice.currency)
        await _complete(
            db,
            org=org,
            invoice=invoice,
            payment=target,
            method=method,
            reference=reference,
            paid_on=paid_on,
            actor_id=actor_id,
            source=source,
            converted=True,
        )
        return RecordedPayment(target, created=True)

    owed, discount_offer_id, discount_amount = await _amount_owed(db, invoice, paid_on=paid_on)
    _check_amount(amount, owed, invoice.currency)
    payment = Payment(
        invoice_id=invoice.id,
        entity_id=invoice.entity_id,
        amount=owed,
        discount_offer_id=discount_offer_id,
        discount_amount=discount_amount,
        method=method,
        payment_run_id=None,
        correlation_id=uuid.uuid4(),
    )
    # Insert inside a savepoint. The invoice lock serializes two records of the
    # same invoice, but run creation does not take that lock, so a run booking
    # a live payment for this invoice between our check and our insert would
    # surface as `uq_payments_one_live_per_invoice` — a refusal, not a 500.
    try:
        async with db.begin_nested():
            db.add(payment)
            await db.flush()
    except IntegrityError as exc:
        raise ExternalPaymentRefused(
            Refusal(
                REFUSAL_PAYMENT_LIVE,
                "Another payment for this invoice was booked at the same moment. Reload "
                "and check it before recording a payment made another way.",
                params={"payment_status": "pending"},
            )
        ) from exc
    await _complete(
        db,
        org=org,
        invoice=invoice,
        payment=payment,
        method=method,
        reference=reference,
        paid_on=paid_on,
        actor_id=actor_id,
        source=source,
        converted=False,
    )
    return RecordedPayment(payment, created=True)


async def run_payment_refusal(
    db: AsyncSession,
    *,
    org: Organization,
    invoice: Invoice,
    payment: Payment,
    actor_id: uuid.UUID | None,
    paid_on: date,
) -> Refusal | None:
    """Would recording this draft-run payment be refused? ``None`` = no.

    The run-level caller asks this for EVERY payment before recording ANY of
    them. Checking inside the write loop would let an audit row for the first
    payment leave the process before the second is refused — in `lambda` audit
    mode `dispatch_audit` ships to SQS immediately, so a transaction rollback
    cannot take it back, and the trail would record a payment that never was.
    """
    refusal = await check_recordable(db, org=org, invoice=invoice, actor_id=actor_id)
    if refusal is not None:
        return refusal
    if not _is_undispatched(payment):
        return Refusal(
            REFUSAL_PAYMENT_LIVE,
            "A payment in this run has already been dispatched.",
            params={"payment_status": payment.status},
        )
    # The run was staged at one figure; credits applied, or a discount that
    # lapsed or was accepted, since then change what is owed on `paid_on`.
    # Booking the staged figure regardless would record the credit as applied
    # AND the full amount as paid. Never silently re-priced — the same rule
    # `dispatch_preflight` applies before money moves.
    try:
        owed, discount_offer_id, _ = await _amount_owed(db, invoice, paid_on=paid_on)
    except ExternalPaymentRefused as exc:
        return exc.refusal
    if owed != payment.amount or discount_offer_id != payment.discount_offer_id:
        return Refusal(
            REFUSAL_AMOUNT_CHANGED,
            "What this invoice owes has changed since the run was staged (a credit "
            "memo or early-payment discount). Resolve the difference with the "
            "supplier, then record the invoice on its own.",
            params={
                "staged_amount": str(payment.amount),
                "amount": str(owed),
                "currency": invoice.currency,
            },
        )
    return None


async def record_run_payment_paid_outside(
    db: AsyncSession,
    *,
    org: Organization,
    invoice: Invoice,
    payment: Payment,
    reference: str,
    paid_on: date,
    actor_id: uuid.UUID | None,
    method: str | None = None,
) -> None:
    """Record one undispatched payment of a draft run as paid outside. Never commits.

    The run-level caller has already applied the run's own controls (maker-
    checker, CFO sign-off), holds the run and ``invoice`` FOR UPDATE, and has
    cleared :func:`run_payment_refusal` for every payment in the run. The check
    is repeated here so this function is never the one that skips it.
    """
    refusal = await run_payment_refusal(
        db, org=org, invoice=invoice, payment=payment, actor_id=actor_id, paid_on=paid_on
    )
    if refusal is not None:
        raise ExternalPaymentRefused(refusal)
    await _complete(
        db,
        org=org,
        invoice=invoice,
        payment=payment,
        method=method,
        reference=reference,
        paid_on=paid_on,
        actor_id=actor_id,
        source="user",
        converted=True,
    )
