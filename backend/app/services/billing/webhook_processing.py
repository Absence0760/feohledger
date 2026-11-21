"""Apply a verified, deduped billing webhook event to our control-plane state.

The webhook route (``api/billing_webhook.py``) verifies the provider HMAC,
dedupes by ``event_id``, and hands a normalized :class:`BillingWebhookEvent`
here. This module owns the *effect*: resolve the ``Subscription`` the event
refers to (by its live provider id), apply the lifecycle transition, and write a
PII-free audit row.

Design:
  * **Control-plane only** — ``Plan`` / ``Subscription`` live in the control DB
    keyed by org; this never touches a tenant DB for the mutation itself (the
    audit row is written into the tenant ``audit_log`` via the shared
    ``dispatch_auth_audit`` helper, which resolves the tenant DB from the org).
  * **Idempotent** — applying the same target status twice is a no-op (the
    route's dedupe is the first line of defense; this is the second). A status
    that doesn't change writes no audit row.
  * **Fail-soft + silent** — an event referencing an unknown subscription, or
    carrying no mapped status, is dropped with a PII-free reason code. The route
    204s on every outcome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import SUBSCRIPTION_STATUSES, Subscription
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.billing_adapters.base import BillingWebhookEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookApplyResult:
    """Outcome of applying one billing webhook event."""

    applied: bool
    reason: str | None = None
    # The status the subscription ended up in (for logging/tests), if resolved.
    new_status: str | None = None


async def apply_billing_event(
    control_db: AsyncSession, *, event: BillingWebhookEvent
) -> WebhookApplyResult:
    """Drive a subscription lifecycle transition from a verified webhook event.

    Resolves the ``Subscription`` by ``external_subscription_id`` (the live
    provider's id, persisted on the row when the subscription was created at the
    provider). A ``canceled`` row is terminal. Copies the provider's billing
    window when the event carries one, then applies the target status — the
    event's own for a ``customer.subscription.*`` event, an
    :data:`INVOICE_TRANSITIONS` edge for an invoice event — when it differs
    from the current one, writing an append-only
    ``billing.subscription_<status>`` audit row (decisions §480).
    """
    if not event.external_subscription_id:
        # Account-level events with no subscription reference (e.g. a customer
        # update) carry no lifecycle effect for us in this slice — drop quietly.
        return WebhookApplyResult(applied=False, reason="no_subscription_reference")

    subscription = (
        await control_db.execute(
            select(Subscription).where(
                Subscription.external_subscription_id == event.external_subscription_id
            )
        )
    ).scalar_one_or_none()
    if subscription is None:
        # Unknown subscription — never enumerate; the route 204s regardless.
        return WebhookApplyResult(applied=False, reason="unknown_subscription")

    if subscription.status == "canceled":
        # Terminal. A provider subscription never leaves `canceled`, and a row
        # our own dunning sweep canceled must not be revived by a late
        # `customer.subscription.updated` (or invoice.paid) — the org may hold
        # a new live row by now, and one-live-per-org is the invariant.
        return WebhookApplyResult(applied=False, reason="subscription_canceled")

    period_changed = _sync_period(subscription, event)
    target = _target_status(event, current=subscription.status)

    if target is None or target == subscription.status:
        if period_changed:
            # The provider's window is authoritative once it bills the row
            # (`services/billing/period.py`); a renewal moves it without any
            # status change. Not a lifecycle transition, so no audit row.
            await control_db.commit()
            return WebhookApplyResult(
                applied=True, reason="period_synced", new_status=subscription.status
            )
        if target is None:
            return WebhookApplyResult(applied=False, reason="no_mapped_status")
        # Idempotent no-op: the target state is already current. No audit row for
        # a non-change (mirrors transition_invoice's "no-op writes no row").
        return WebhookApplyResult(applied=False, reason="already_in_status", new_status=target)

    previous = subscription.status
    subscription.status = target

    # Append-only audit (SOX): subscription status is a regulated control-plane
    # mutation. PII-free — only the org, the lifecycle states, and the event id.
    # Dispatched BEFORE the control commit (mirrors the transition_invoice
    # chokepoint) so the status change is never durably persisted without an
    # audit attempt. dispatch_auth_audit opens its own short-lived tenant-DB
    # session and is fail-soft, so it can't break the transition.
    await dispatch_auth_audit(
        organization_id=subscription.organization_id,
        actor_id=None,  # provider-driven, no human actor
        action=f"billing.subscription_{target}",
        entity_id=subscription.id,
        details={
            "from_status": previous,
            "to_status": target,
            "event_id": event.event_id,
            "event_type": event.event_type,
        },
    )
    await control_db.commit()

    return WebhookApplyResult(applied=True, new_status=target)


#: Invoice events, by type: the statuses each may move a subscription FROM, and
#: the status it moves it to. Deliberately narrow — the
#: ``customer.subscription.updated`` Stripe sends alongside is the authoritative
#: status; these are the backstop when that one is lost or reordered:
#:
#: * a paid invoice clears ``past_due`` (the retry succeeded) but never turns a
#:   ``trialing`` subscription active early, nor revives a canceled one;
#: * a failed payment marks an ``active`` subscription ``past_due`` — the state
#:   the dunning sweep's grace clock reads.
INVOICE_TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "invoice.paid": (frozenset({"past_due"}), "active"),
    "invoice.payment_failed": (frozenset({"active"}), "past_due"),
}


def _target_status(event: BillingWebhookEvent, *, current: str) -> str | None:
    """The status ``event`` moves a subscription in ``current`` to, if any."""
    rule = INVOICE_TRANSITIONS.get(event.event_type)
    if rule is not None:
        sources, target = rule
        return target if current in sources else None
    if event.status in SUBSCRIPTION_STATUSES:
        return event.status
    # Unmapped, or out of range: the adapter maps statuses, but an unknown
    # value is never trusted into the DB column.
    return None


def _sync_period(subscription: Subscription, event: BillingWebhookEvent) -> bool:
    """Copy the provider's billing window onto the row; True if it moved."""
    start, end = event.current_period_start, event.current_period_end
    if start is None or end is None or end <= start:
        return False
    if subscription.current_period_start == start and subscription.current_period_end == end:
        return False
    subscription.current_period_start = start
    subscription.current_period_end = end
    return True
