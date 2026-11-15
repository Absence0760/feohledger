"""Outbound webhook subscriptions + delivery log — control-plane.

A ``WebhookSubscription`` lets an org's external integrator subscribe to
platform events (invoice approved, approval recalled, payment settled,
exception raised) at a target URL. Each emitted event becomes a
``WebhookDelivery`` row per matching active subscription; the dispatch service
signs the payload (HMAC-SHA256) and POSTs it with bounded retries,
dead-lettering after exhaustion. Each event's payload is defined once, in
``app/schemas/webhook_events.py``.

Both tables live in the CONTROL plane keyed by ``organization_id`` — the SAME
placement as the programmatic ``ApiKey`` (see ``app/models/api_key.py``): an
outbound webhook is the push counterpart of the pull ``/api/v1`` surface, and a
subscription belongs to an org, not a tenant DB. Added to
``tenant_provisioning.CONTROL_TABLES`` so it never fans out to per-tenant DBs.

Signing-secret hashing rationale (read before "fixing" this):
    The signing secret is the customer's verification key — they must be able to
    re-derive the same HMAC we send, so unlike a password it is a SHARED secret
    that the dispatch service needs in plaintext at send time. It is generated
    server-side (``secrets.token_urlsafe``), returned to the admin EXACTLY ONCE
    at create time (like an API-key mint), and stored as a plaintext column so
    the dispatcher can sign with it. It is NEVER logged or echoed back after
    creation (list/get responses carry only ``secret_prefix``). This is the same
    trade-off the per-tenant inbound webhook secrets make
    (``Organization.settings.payments.webhook_secret`` is stored verbatim too) —
    an HMAC verification key is symmetric by definition.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Delivery lifecycle states.
DELIVERY_PENDING = "pending"  # queued, not yet attempted (or awaiting retry)
DELIVERY_DELIVERED = "delivered"  # target returned 2xx
DELIVERY_FAILED = "failed"  # attempt failed, retries remain
DELIVERY_DEAD = "dead"  # retries exhausted — dead-letter

# Event types an org may subscribe to. Kept as plain strings (mirrors the
# notification EVENT_* constants) so adding one needs no migration.
EVENT_INVOICE_APPROVED = "invoice.approved"
# An approval was taken back (`review.recall_approval`, decisions §374, §414):
# an integrator that acted on `invoice.approved` has to hear it was withdrawn.
EVENT_INVOICE_APPROVAL_RECALLED = "invoice.approval_recalled"
EVENT_PAYMENT_SETTLED = "payment.settled"
EVENT_EXCEPTION_RAISED = "exception.raised"

WEBHOOK_EVENT_TYPES = (
    EVENT_INVOICE_APPROVED,
    EVENT_INVOICE_APPROVAL_RECALLED,
    EVENT_PAYMENT_SETTLED,
    EVENT_EXCEPTION_RAISED,
)


class WebhookSubscription(Base, TimestampMixin):
    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    # Human label so an admin can tell two subscriptions apart in the UI.
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Destination URL we POST signed event payloads to. http(s) only — validated
    # at the API boundary.
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Subscribed event types (subset of WEBHOOK_EVENT_TYPES). JSONB so the set
    # grows without a migration.
    event_types: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # HMAC-SHA256 signing secret — the customer's verification key. Stored
    # plaintext because the dispatcher must sign with it (it is a SHARED secret,
    # not a password); see the module docstring. Returned to the admin ONCE at
    # create time, never echoed afterwards.
    signing_secret: Mapped[str] = mapped_column(String(128), nullable=False)
    # Non-secret first segment shown in list/get responses so an admin can match
    # a subscription to the secret they copied at create time.
    secret_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # Rotation overlap (migration 0084). While `previous_secret_expires_at` is
    # in the future, the dispatcher ALSO signs each payload with
    # `previous_signing_secret` and sends it as `X-Webhook-Signature-Previous`,
    # so a receiver that accepts either header rotates with zero dropped
    # deliveries. Both NULL is the ordinary state: no rotation in flight.
    #
    # The pair is only meaningful together — `webhooks/rotation.py` treats an
    # elapsed expiry exactly like a NULL secret, so a stale window can never
    # keep a retired key signing.
    previous_signing_secret: Mapped[str | None] = mapped_column(String(128))
    previous_secret_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WebhookDelivery(Base, TimestampMixin):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # One delivery per (subscription, event) — the dedupe that keeps a
        # re-fired or replayed event from queueing the same delivery twice
        # (webhook discipline: dedupe by event id). The emit path inserts every
        # subscription's delivery in ONE `INSERT ... ON CONFLICT DO NOTHING`
        # against this constraint; it deliberately does NOT catch an
        # `IntegrityError`, because rolling one back expires the session's
        # loaded subscriptions and the fan-out then dies mid-loop (see
        # `services/webhooks/dispatch.py`).
        UniqueConstraint("subscription_id", "event_id", name="uq_webhook_delivery_sub_event"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    # Stable, unique-per-event id (also sent in the X-Webhook-Event-Id header so
    # the receiver can dedupe). Mirrors inbound-webhook discipline.
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # The exact JSON body that gets signed + POSTed. Frozen at emit time so a
    # retry re-sends byte-identical bytes (and the same signature).
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DELIVERY_PENDING, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # When the next attempt is due (exponential backoff). NULL once terminal
    # (delivered / dead).
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last HTTP status code seen (NULL if the request never reached the server,
    # e.g. timeout / connection refused).
    response_code: Mapped[int | None] = mapped_column(Integer)
