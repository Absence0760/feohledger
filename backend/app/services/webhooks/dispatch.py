"""Emit a domain event to every matching active subscription.

``emit_event`` is the single chokepoint the three event sources call. It is
**best-effort and never raises into its caller** — a webhook failure must never
break the invoice/payment transition that produced the event (same contract as
``notification_dispatch.notify_event``). It:

  1. opens its OWN short-lived control-plane session (the caller's session is
     tenant-scoped; subscriptions live in the control plane — mirrors
     ``audit_dispatch.dispatch_auth_audit``),
  2. finds every active subscription for the org subscribed to the event type,
  3. inserts one ``WebhookDelivery`` (status=pending) per subscription in a
     single ``INSERT ... ON CONFLICT DO NOTHING``, deduped on
     ``(subscription_id, event_id)`` so a re-fired/replayed event can't queue
     the same delivery twice (webhook discipline — dedupe by event id). The
     dedupe is the DB constraint, never a caught ``IntegrityError``: rolling
     one back expired the session's loaded subscriptions, and the next
     iteration's attribute read then raised out of the loop, so a duplicate for
     ONE subscription dropped the event for every remaining one, and
  4. kicks off an in-process, fire-and-forget delivery attempt for each new row
     (local-first: deliveries run in-process, no cloud queue). The background
     retry sweep (``delivery.run_webhook_delivery_loop``) is the durable backstop
     for anything the immediate attempt doesn't land.

The payload is built here, ONCE, and frozen on the delivery row so a retry
re-sends byte-identical bytes (and thus the same signature).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.models.webhook import (
    DELIVERY_PENDING,
    EVENT_EXCEPTION_RAISED,
    EVENT_INVOICE_APPROVED,
    EVENT_PAYMENT_SETTLED,
    WebhookDelivery,
    WebhookSubscription,
)
from app.services.billing.entitlements import get_entitlements, has_entitlement
from app.services.billing.plan_catalog import FEATURE_PUBLIC_API

logger = logging.getLogger(__name__)

# Strong references to the in-flight immediate-delivery tasks.
#
# `asyncio` keeps only a WEAK reference to a running task, so a task whose sole
# reference is the local variable that created it can be garbage-collected
# mid-await — the hazard the stdlib docs warn about. Here that would abandon a
# delivery *after* its row was written and quite possibly mid-POST to the
# customer's endpoint, leaving it `pending` until the retry sweep happened
# along. `erp_dispatch` and `payment_erp_sync` hold their fire-and-forget tasks
# in exactly this shape; the discard callback keeps the set bounded by the
# number actually in flight.
_delivery_tasks: set[asyncio.Task] = set()


def _money_str(value) -> str | None:
    """Money serialises as an exact string (never float) — money-is-exact."""
    if value is None:
        return None
    return str(value)


def _occurrence_key(occurrence_id) -> str:
    """Normalise a caller-supplied occurrence id into an ``event_key``.

    The key must identify the OCCURRENCE, not the entity. An entity-keyed id
    (the invoice id) made every event after the first for that invoice
    permanently undeliverable: the ``(subscription_id, event_id)`` unique index
    swallows the insert, and a conforming receiver deduping on
    ``X-Webhook-Event-Id`` would drop it even without the index. An invoice
    genuinely reaches ``approved`` more than once (void → re-approve) and
    ``paid`` more than once (void → re-pay), and the customer's ERP has to hear
    about each one.

    ``None`` mints a fresh id, so a bare call is one occurrence. Passing the
    SAME id twice still dedupes — that is what makes a replay of one transition
    safe.
    """
    return str(occurrence_id) if occurrence_id is not None else str(uuid.uuid4())


async def emit_event(
    *,
    organization_id: uuid.UUID,
    event_type: str,
    event_key: str,
    data: dict,
) -> None:
    """Enqueue + best-effort deliver a webhook event. Never raises.

    ``event_key`` is a stable, caller-supplied identity for the event
    OCCURRENCE — combined with ``event_type`` it forms the per-event id used for
    dedupe, so re-emitting the same occurrence produces ONE delivery per
    subscription. It must NOT be a bare entity id for an event an entity can
    raise more than once (an invoice can be approved, voided and re-approved);
    ``_occurrence_key`` is the helper the typed emitters use.
    """
    if not settings.webhooks_enabled:
        # Master kill switch OFF (local-dev default): emit is a silent no-op so
        # a fresh clone never makes outbound HTTP calls.
        return
    try:
        await _emit(
            organization_id=organization_id,
            event_type=event_type,
            event_key=event_key,
            data=data,
        )
    except Exception:  # noqa: BLE001 — emit must never break the caller's transition
        logger.exception("webhook emit failed for org=%s event=%s", organization_id, event_type)


async def _emit(
    *,
    organization_id: uuid.UUID,
    event_type: str,
    event_key: str,
    data: dict,
) -> None:
    from app.database import control_session_factory

    # event_id is deterministic per (type, occurrence) so a re-fire dedupes.
    event_id = f"{event_type}:{event_key}"
    payload = {
        "id": event_id,
        "type": event_type,
        "created_at": datetime.now(UTC).isoformat(),
        "organization_id": str(organization_id),
        "data": data,
    }

    new_delivery_ids: list[uuid.UUID] = []
    async with control_session_factory() as db:
        subs = (
            (
                await db.execute(
                    select(WebhookSubscription).where(
                        WebhookSubscription.organization_id == organization_id,
                        WebhookSubscription.active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        # Only the subscription ID is carried forward — never the ORM instance.
        # The previous shape looped over `subs`, committing per subscription and
        # calling `db.rollback()` on a duplicate. A rollback EXPIRES every
        # instance the session loaded, so the next iteration's `sub.event_types`
        # read became an implicit lazy refresh — synchronous IO from an async
        # context, i.e. `MissingGreenlet`. `emit_event`'s blanket handler
        # swallowed it, so ONE already-queued duplicate silently dropped the
        # event for every remaining subscription of that org, with no delivery
        # row to show for it. That fired on exactly the replay the dedupe exists
        # to make safe (a re-emitted occurrence).
        target_ids = [sub.id for sub in subs if event_type in (sub.event_types or [])]

        # Outbound webhooks are part of the public-API feature (decisions §253).
        # A tenant that downgraded keeps its subscriptions — re-upgrading
        # resumes them — but nothing new is queued while the plan lacks the
        # feature. Checked only when there is something to send, so the
        # common no-subscription emit costs no extra query.
        if target_ids and not has_entitlement(
            await get_entitlements(db, organization_id), FEATURE_PUBLIC_API
        ):
            target_ids = []

        if target_ids:
            # One statement, and the dedupe is the DB's `uq_webhook_delivery_sub_event`
            # constraint rather than a caught exception — so there is no rollback
            # to expire anything, and a duplicate for one subscription cannot
            # affect any other. `RETURNING` yields only the rows actually
            # inserted, which is exactly the set to attempt immediately.
            stmt = (
                pg_insert(WebhookDelivery)
                .values(
                    [
                        {
                            "id": uuid.uuid4(),
                            "subscription_id": sub_id,
                            "organization_id": organization_id,
                            "event_id": event_id,
                            "event_type": event_type,
                            "payload": payload,
                            "status": DELIVERY_PENDING,
                            "attempt_count": 0,
                            "next_attempt_at": datetime.now(UTC),
                        }
                        for sub_id in target_ids
                    ]
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        WebhookDelivery.subscription_id,
                        WebhookDelivery.event_id,
                    ]
                )
                .returning(WebhookDelivery.id)
            )
            new_delivery_ids = list((await db.execute(stmt)).scalars().all())
            await db.commit()

    # Fire-and-forget an immediate attempt for each freshly-queued delivery so a
    # local dev / single-process deploy delivers without waiting for the sweep.
    for did in new_delivery_ids:
        _spawn_immediate_attempt(did)


def _finish_delivery_task(task: asyncio.Task) -> None:
    """Release the strong reference and retrieve any exception."""
    _delivery_tasks.discard(task)
    if not task.cancelled():
        task.exception()


def _spawn_immediate_attempt(delivery_id: uuid.UUID) -> None:
    """Best-effort immediate delivery on the running event loop, if any."""
    from app.database import in_dispatch_scope
    from app.services.webhooks.delivery import process_delivery_by_id

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (e.g. a sync worker thread) — the sweep will pick it up.
        return

    if in_dispatch_scope():
        # On a dispatcher's own event loop (`extraction_dispatch`'s worker).
        # That loop closes as soon as the job returns and its engines are
        # disposed with it, so a task started here would be abandoned
        # mid-flight — quite possibly mid-HTTP-POST to the customer's endpoint,
        # having already written the delivery row. The sweep owns it instead:
        # the same durable fallback this function already takes when there is
        # no running loop at all. See `database.dispatch_engine_scope`.
        return
    task = loop.create_task(
        process_delivery_by_id(delivery_id), name=f"webhook-delivery-{delivery_id}"
    )
    # Hold a strong reference until the task finishes — without it the only
    # reference is this local, and the loop's own registry is weak, so the task
    # can be collected mid-POST (see `_delivery_tasks`).
    _delivery_tasks.add(task)
    # Discard the reference AND swallow any task exception, so an unawaited
    # failure never surfaces as an "exception was never retrieved" warning —
    # delivery errors are persisted on the row, not raised.
    task.add_done_callback(_finish_delivery_task)


# ---------------------------------------------------------------------------
# Typed emit helpers for the three event sources — keep the call sites at the
# emit sites a single, obvious line.
# ---------------------------------------------------------------------------


async def emit_invoice_approved(invoice, *, occurrence_id=None) -> None:
    """Emit ``invoice.approved`` for a freshly-approved invoice.

    ``occurrence_id`` identifies THIS approval, not the invoice — see
    ``_occurrence_key``. ``workflow_engine.transition_invoice`` passes the id it
    minted for the transition it just committed.
    """
    await emit_event(
        organization_id=invoice.organization_id,
        event_type=EVENT_INVOICE_APPROVED,
        event_key=_occurrence_key(occurrence_id),
        data={
            "invoice_id": str(invoice.id),
            "invoice_number": getattr(invoice, "invoice_number", None),
            "vendor_name": getattr(invoice, "vendor_name", None),
            "amount": _money_str(getattr(invoice, "amount", None)),
            "currency": getattr(invoice, "currency", None) or "USD",
            "status": getattr(getattr(invoice, "status", None), "value", None),
        },
    )


async def emit_payment_settled(invoice, *, occurrence_id=None) -> None:
    """Emit ``payment.settled`` when an invoice reaches the ``paid`` state.

    ``occurrence_id`` identifies THIS settlement, not the invoice — a voided and
    re-paid invoice settles twice and must deliver twice. See
    ``_occurrence_key``.
    """
    await emit_event(
        organization_id=invoice.organization_id,
        event_type=EVENT_PAYMENT_SETTLED,
        event_key=_occurrence_key(occurrence_id),
        data={
            "invoice_id": str(invoice.id),
            "invoice_number": getattr(invoice, "invoice_number", None),
            "vendor_name": getattr(invoice, "vendor_name", None),
            "amount": _money_str(getattr(invoice, "amount", None)),
            "currency": getattr(invoice, "currency", None) or "USD",
            "status": getattr(getattr(invoice, "status", None), "value", None),
        },
    )


async def emit_exception_raised(
    *,
    organization_id,
    exception_id,
    exception_type,
    severity=None,
    status=None,
    invoice=None,
    invoice_id=None,
) -> None:
    """Emit ``exception.raised`` when an AP exception is opened.

    Wired from the shared exception-create chokepoint
    (``services/exception_service.create_exception``) so every freshly-opened
    ``Exception`` row — whatever code path raised it — fires exactly one
    ``exception.raised`` event per subscription. ``event_key`` is the exception
    id, so a best-effort re-run that re-creates the same row (or a double-fire)
    dedupes on ``(subscription, exception.raised:<exception_id>)``.

    The payload is PII-free: identifiers, the exception classification, and the
    invoice's public-facing fields (number, vendor *name*, amount as an exact
    string, currency). No tax id / bank detail / address. Some exceptions have no
    invoice (e.g. a Positive Pay never-issued cheque) — then ``invoice`` is
    ``None`` and only ``invoice_id`` (also ``None``) is carried.
    """
    inv_id = invoice_id if invoice_id is not None else getattr(invoice, "id", None)
    await emit_event(
        organization_id=organization_id,
        event_type=EVENT_EXCEPTION_RAISED,
        event_key=str(exception_id),
        data={
            "exception_id": str(exception_id),
            "exception_type": exception_type,
            "severity": severity,
            "status": status,
            "invoice_id": str(inv_id) if inv_id else None,
            "invoice_number": getattr(invoice, "invoice_number", None),
            "vendor_name": getattr(invoice, "vendor_name", None),
            "amount": _money_str(getattr(invoice, "amount", None)),
            "currency": getattr(invoice, "currency", None) or "USD",
            "link": f"/invoices/{inv_id}" if inv_id else "/exceptions",
        },
    )
