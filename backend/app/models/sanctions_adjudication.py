"""Sanctions false-positive adjudications — tenant-scoped (migration 0139).

A screen that keeps returning ``match`` or ``review_required`` for a vendor
that is NOT the listed party stops the vendor being paid (dispatch re-screens
live) and booked (the ERP push reads the stored screen, decisions §333). This
row is the recorded human decision that one specific screen result is a false
positive: who decided, when, why, and WHICH result — the provider, the verdict,
the list name and a fingerprint of the matched entry
(``services/sanctions_adjudication.screen_fingerprint``).

A later screen is cleared by an active row only when it returns the SAME
fingerprint from the SAME provider; a different or new hit is not covered and
holds again. Rows are never deleted or rewritten except to stamp a revocation
(``revoked_*``), which ends the clearance; the audit log carries both events.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SanctionsAdjudication(Base):
    __tablename__ = "sanctions_adjudications"

    # The coverage lookup a screen runs: is there an ACTIVE adjudication of
    # this vendor's result from this provider with this fingerprint? Partial
    # and unique, so one result is adjudicated at most once at a time.
    __table_args__ = (
        Index(
            "uq_sanctions_adjudications_active",
            "vendor_id",
            "provider",
            "fingerprint",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False
    )
    # The screen that was adjudicated (the trail row a reviewer looked at).
    sanctions_check_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sanctions_checks.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # 'match' | 'review_required' — part of the fingerprint too, so clearing a
    # review never clears a later match on the same entry.
    result: Mapped[str] = mapped_column(String(30), nullable=False)
    matched_list: Mapped[str | None] = mapped_column(String(80))
    # sha256 hex of the screen result's identity; never the raw match details.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Control-plane user ids, no FK (as `invoices.uploaded_by_id`).
    adjudicated_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    adjudicated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
