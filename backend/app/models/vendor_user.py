"""Supplier-portal user — tenant-scoped, belongs to exactly one Vendor.

Kept separate from `User` (the AP-team model in the control plane) so a bug
in `require_roles` or the JWT decoder can't leak cross-surface: a vendor
JWT rejected outside the portal, an employee JWT rejected inside it.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class VendorUser(Base, TimestampMixin):
    __tablename__ = "vendor_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The tenant Organization this portal user belongs to. A `vendor_users` row
    # already lives in exactly one tenant DB, so isolation currently rests on the
    # per-tenant DB boundary + a UUID-collision assumption (the same VendorUser.id
    # can't exist in two tenant DBs). This column makes the tenant binding
    # explicit and positively verifiable: `get_current_vendor_user` cross-checks
    # it against the resolved tenant, so even a (astronomically unlikely) id
    # collision — or a manually mis-provisioned row — can't authenticate against
    # the wrong tenant. Nullable so the additive migration can backfill legacy
    # rows; every creation site populates it (from `vendor.organization_id`).
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # The control-plane `User.id` who last steered this credential — stamped by
    # `POST /api/vendors/{id}/portal-users` and re-stamped by
    # `.../reset-password`, the ONLY two routes that put a supplier credential
    # in motion on an AP actor's say-so. Neither hands out a password any more
    # (each mails a set-password link, decisions §283), but the invite's link
    # goes to an address the AP actor chose, so the actor may hold it.
    #
    # This is a segregation-of-duties record, not a bookkeeping nicety. Without
    # it one `ap_manager` could complete a BEC bank redirect alone: provision a
    # portal identity, sign in as it, stage a bank change (which fills
    # `requested_by_vendor_user_id` and leaves `requested_by_user_id` NULL), and
    # then approve their own request — because `approve_change_request`'s
    # requester-is-not-approver check only ever compared the AP column.
    # `_stage_change` copies this value onto the change request at staging time
    # so the evidence survives the portal user being deleted afterwards.
    #
    # NULL means "no AP actor in this system has ever held this credential"
    # (a legacy row, or a supplier who has always managed their own password) —
    # correctly permissive, because an AP actor cannot sign in as that identity
    # without going through invite or reset, both of which stamp the column.
    #
    # Deliberately NOT cleared when the supplier changes or sets their own
    # password (`POST /api/portal/auth/change-password`, `.../reset-password`):
    # an attacker who chose the invited address holds the link and then the
    # password, so clearing there would be a one-request bypass of the very
    # control this column exists to enforce. The stamp is durable
    # provenance, not a "who knows the password right now" flag.
    provisioned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Per-portal-user notification preferences. Shape: a map of event_type ->
    # {"email": bool}. Empty `{}` means "use defaults" (all channels on) —
    # see services/notification_dispatch.resolve_prefs. Mirrors
    # `User.notification_prefs` but vendor-scoped: the supplier controls whether
    # they get emailed when their invoices are paid / rejected. Vendors have no
    # in-app notification center, so only the `email` channel is consulted.
    notification_prefs: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # MFA — TOTP shared secret + enrollment metadata. Mirrors the columns on
    # `User` (control plane) exactly: `mfa_secret` is the base32 TOTP seed,
    # populated during enrollment and only treated as "active" once
    # `mfa_enabled` flips true (after the vendor verifies a code). The pending
    # secret stays around so the user can re-scan without restarting enrollment.
    # MFA stays opt-in per vendor user and is gated by the `FEOH_MFA_ENABLED`
    # master switch, exactly like employee MFA. See docs/supplier-portal.md.
    mfa_secret: Mapped[str | None] = mapped_column(String(64))
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Account-level email language preference — "what language to email this
    # supplier user in" (invoice paid/rejected notifications). NULL = English
    # fallback. Mirrors `User.locale` (control plane) but vendor-scoped: set via
    # PATCH /api/portal/auth/me, validated against the supported set, read ONLY
    # by the email-rendering path — never to drive portal UI. See
    # docs/notifications.md.
    locale: Mapped[str | None] = mapped_column(String(16))

    vendor: Mapped["Vendor"] = relationship(back_populates="portal_users")  # noqa: F821
