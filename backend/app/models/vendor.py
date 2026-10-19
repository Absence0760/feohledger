import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Index, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, EntityMixin, TimestampMixin


class Vendor(Base, EntityMixin, TimestampMixin):
    __tablename__ = "vendors"

    # The screening review queue (`screening_status`), the periodic re-screen
    # sweep's "last screened before X" scan (`last_screened_at`), and the
    # blocked-vendor surface — partial, because a blocked vendor is rare and the
    # index then holds only them. All three are migration 0042's; declared here
    # so `create_all` builds them too.
    __table_args__ = (
        Index("ix_vendors_screening_status", "screening_status"),
        Index("ix_vendors_last_screened_at", "last_screened_at"),
        Index(
            "ix_vendors_payments_blocked",
            "payments_blocked",
            postgresql_where=text("payments_blocked"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    address: Mapped[str | None] = mapped_column(String(500))
    # Public company website. Nullable; may be populated by an AP steward
    # applying an external-enrichment suggestion (migration 0061). PII-free
    # display metadata — safe to log / show. See backend/docs/data-enrichment.md.
    website: Mapped[str | None] = mapped_column(String(500))
    tax_id: Mapped[str | None] = mapped_column(String(50))
    payment_terms: Mapped[str | None] = mapped_column(String(100))
    bank_details: Mapped[dict | None] = mapped_column(JSONB)
    accepts_virtual_cards: Mapped[bool] = mapped_column(default=False)

    # Vendor status and verification
    status: Mapped[str] = mapped_column(
        String(30), default="active"
    )  # active, unverified, inactive, rejected
    source: Mapped[str] = mapped_column(
        String(30), default="manual"
    )  # manual, erp_sync, ai_extracted
    verified_by: Mapped[str | None] = mapped_column(String(255))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ERP sync
    erp_vendor_id: Mapped[str | None] = mapped_column(String(255))  # vendor ID in the external ERP
    erp_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The ERP company ``erp_vendor_id`` was linked under
    #: (``services/erp_link_realm``) — QuickBooks numbers vendors per company,
    #: so a link from another realm is never posted to. NULL: linked before
    #: migration 0115.
    erp_link_realm: Mapped[str | None] = mapped_column(String(255))

    # 1099 / W-9 tax data. See backend/docs/tax-1099.md.
    # tax_classification: IRS entity type — individual, sole_proprietor,
    # llc_s_corp, llc_c_corp, llc_partnership, c_corp, s_corp, partnership,
    # trust, other. Populated from the W-9 form box 3.
    tax_classification: Mapped[str | None] = mapped_column(String(50))
    # Flipped by AP when a W-9 is collected for a 1099-eligible vendor.
    # Corporations (c_corp/s_corp) are generally NOT 1099-eligible; we
    # leave this to tenant judgement rather than auto-deriving.
    is_1099_eligible: Mapped[bool] = mapped_column(default=False)
    w9_received_date: Mapped[date | None] = mapped_column(Date)
    w9_file_key: Mapped[str | None] = mapped_column(String(512))
    tin_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # KYC / AML. Drives whether `services.compliance` lets a
    # high-risk-corridor payment through. See migration 0018 and
    # `docs/international-payments.md` § KYC/AML.
    kyc_status: Mapped[str] = mapped_column(
        String(20), default="not_required"
    )  # pending | verified | rejected | not_required
    kyc_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kyc_verified_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    beneficial_owner_data: Mapped[dict | None] = mapped_column(JSONB)

    # Sanctions & vendor-risk screening. Populated by
    # `services.vendor_screening.screen_vendor_record` on vendor
    # create / update, the periodic re-screen sweep
    # (`services.vendor_rescreen`), and manual re-screens. The full
    # screening trail lives in `sanctions_checks`; these columns are the
    # denormalised "current state" the UI and the payment-block gate
    # read. See migration 0042 and `docs/vendor-risk-screening.md`.
    #
    # screening_status: unscreened | clear | review | match — mirrors the
    # most recent SanctionsCheck.result ('review_required' → 'review').
    screening_status: Mapped[str] = mapped_column(String(20), default="unscreened", nullable=False)
    last_screened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Hard payment block. Set True on a sanctions `match` (or a manual
    # block); `services.compliance.check_payment_compliance` refuses any
    # payment to a blocked vendor before it ever reaches a payment
    # adapter. Cleared only by an explicit unblock.
    payments_blocked: Mapped[bool] = mapped_column(default=False, nullable=False)
    payments_blocked_reason: Mapped[str | None] = mapped_column(String(255))
    payments_blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Composite vendor-risk score (0–100) + bucket. Derived by
    # `services.vendor_risk_scoring` from sanctions signals + fraud
    # signals + payment history. `risk_factors` holds the per-signal
    # breakdown (no PII — counts / scores / list NAMES only).
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    risk_level: Mapped[str] = mapped_column(
        String(20), default="unknown", nullable=False
    )  # low | medium | high | critical | unknown
    risk_factors: Mapped[dict | None] = mapped_column(JSONB)
    risk_scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="vendor_rel")  # noqa: F821
    portal_users: Mapped[list["VendorUser"]] = relationship(  # noqa: F821
        back_populates="vendor", cascade="all, delete-orphan"
    )
