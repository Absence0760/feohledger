"""Commerce inventory sync — tenant tables (migration 0111, decisions §268).

* :class:`CommerceInventorySync` — one row per received line per direction:
  the record that a quantity was (or was not) pushed to the business's
  commerce platform. The unique ``(gr_line_item_id, direction)`` key is what
  makes the push idempotent at our end; the row's ``idempotency_key`` is what
  makes it idempotent at the provider's.
* :class:`CommerceSkuMapping` — an admin's override of the provider's own SKU
  lookup, for a SKU the platform spells differently or does not index.

Neither table holds PII: SKUs, quantities, provider ids, and coded reasons.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, TimestampMixin

#: ``CommerceInventorySync.direction``.
DIRECTION_RECEIVE = "receive"
DIRECTION_REVERSE = "reverse"

#: ``CommerceInventorySync.status``.
SYNC_PENDING = "pending"  # planned, not yet pushed (or reset by a retry)
SYNC_IN_FLIGHT = "in_flight"  # claimed by a push; the provider call is running
SYNC_SYNCED = "synced"  # the provider recorded it (`provider_reference`)
SYNC_SKIPPED = "skipped"  # nothing to push, with `reason` (not an error)
SYNC_FAILED = "failed"  # the provider refused or was unreachable, with `reason`
SYNC_CANCELLED = "cancelled"  # a receive that never landed, voided by a cancel
SYNC_STATUSES = (
    SYNC_PENDING,
    SYNC_IN_FLIGHT,
    SYNC_SYNCED,
    SYNC_SKIPPED,
    SYNC_FAILED,
    SYNC_CANCELLED,
)


class CommerceInventorySync(Base, EntityMixin, TimestampMixin):
    __tablename__ = "commerce_inventory_syncs"
    __table_args__ = (
        UniqueConstraint(
            "gr_line_item_id", "direction", name="uq_commerce_inventory_syncs_line_direction"
        ),
        # The retry sweep's scan: open rows by status, oldest first.
        Index("ix_commerce_inventory_syncs_status_id", "status", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goods_receipts.id"), nullable=False, index=True
    )
    gr_line_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gr_line_items.id"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    #: The provider the row was planned for. A push under a different
    #: configured provider is refused (`provider_changed`) rather than sent to
    #: a platform whose item and location ids it never resolved.
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(100))
    #: Signed: + for a receipt, − for its reversal. Matches
    #: `gr_line_items.quantity_received` Numeric(12, 4).
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=SYNC_PENDING)
    #: Stable machine reason for `skipped` / `failed` (`no_sku`, `unmapped_sku`,
    #: `no_location`, `auth_failed`, `transient`, …). The web client localizes it.
    reason: Mapped[str | None] = mapped_column(String(40))
    #: The adapter's paraphrase of a failure — never a provider response body.
    error_message: Mapped[str | None] = mapped_column(String(300))
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider_item_id: Mapped[str | None] = mapped_column(String(255))
    provider_location_id: Mapped[str | None] = mapped_column(String(255))
    provider_reference: Mapped[str | None] = mapped_column(String(255))
    #: Sent to the provider with the adjustment; one per (line, direction).
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommerceSkuMapping(Base, TimestampMixin):
    """An admin's SKU → provider item id override, consulted before the lookup."""

    __tablename__ = "commerce_sku_mappings"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "provider", "sku", name="uq_commerce_sku_mappings_org_provider_sku"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_item_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Control-plane user id, so no FK.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
