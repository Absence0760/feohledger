"""Platform billing & metering — control-plane.

A ``Plan`` is a sellable tier (Free / Growth / Scale …) and a ``Subscription``
binds one ``Organization`` to one ``Plan`` with a lifecycle status. Both live in
the **control-plane** DB keyed by org (like ``Organization`` / ``User`` /
``ExtractionUsage`` / ``ApiKey``), NOT in any tenant DB: billing is a property of
the customer account, and usage is metered off control-plane tables
(``extraction_usage`` etc.). They must NOT fan out to per-tenant DBs.

Money invariant: monthly price + every per-seat / usage component is
``Numeric`` (exact ``Decimal``), never float. Feature entitlements + usage
component pricing live in JSONB so adding a tier feature or a metered component
needs no migration. See ``backend/docs/billing.md``.

``Subscription.external_subscription_id`` is the live provider's subscription
id: set when a plan change creates or adopts it, cleared when a move to a free
plan cancels it (decisions §480). Always NULL with the ``mock`` provider.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Subscription lifecycle. Mirrors the common provider shape (Stripe) so the
# live adapter can map 1:1 without an enum translation table.
SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due", "canceled")


class Plan(Base, TimestampMixin):
    """A sellable subscription tier.

    ``code`` is the stable machine identifier (``free`` / ``growth`` / ``scale``)
    used by the seed + the entitlement lookups; ``name`` is the human label.
    """

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Stable machine code (unique). Entitlement + provisioning code keys off this.
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Flat monthly recurring price. Exact money — Numeric, never Float.
    monthly_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    # Per-seat pricing component, e.g. {"price": "12.00", "included": 5} — price
    # per seat beyond the included count. JSONB so the shape can grow without a
    # migration. Money values are stored as decimal-STRINGS, parsed back to
    # Decimal by the rollup (never float).
    seat_component: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Usage-metered components keyed by meter name, e.g.
    #   {"extractions": {"price": "0.25", "included": 100}}
    # Same decimal-string convention as seat_component.
    usage_components: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Feature entitlements, e.g. {"public_api": true, "max_seats": 25}.
    # The entitlement helper in deps.py reads truthiness here.
    entitlements: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    trial_days: Mapped[int] = mapped_column(nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(default=True)


class Subscription(Base, TimestampMixin):
    """Binds an organization to a plan with a lifecycle status.

    One live subscription per org is the invariant (a partial unique index on
    ``organization_id WHERE status <> 'canceled'`` — mirrors the PEPPOL
    one-live-per-invoice pattern). A canceled row is kept for history; a new
    subscription can be created once the old one is canceled.
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        # One subscription row per (org, plan) keeps re-subscribe idempotent at
        # the seed/test layer. It does NOT bound the live count: two rows for
        # two DIFFERENT plans satisfy it, which is exactly the double-billing
        # shape the partial unique index below refuses.
        UniqueConstraint("organization_id", "plan_id", name="uq_subscription_org_plan"),
        # "At most one *live* subscription per org" — the invariant itself.
        # Migration 0056 created it and nothing declared it, so on every
        # `create_all`-provisioned control plane the rule was enforced by
        # application code alone.
        Index(
            "uq_subscription_one_live_per_org",
            "organization_id",
            unique=True,
            postgresql_where=text("status <> 'canceled'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="trialing")
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The live provider's (Stripe) subscription id, set by `change_plan` when
    # it creates/adopts one and cleared when it cancels one. Nullable: the mock
    # provider + local dev never populate it.
    external_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
