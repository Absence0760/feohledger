import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, EntityMixin, TimestampMixin


class ContractStatus(enum.StrEnum):
    draft = "draft"
    active = "active"
    expired = "expired"
    terminated = "terminated"
    cancelled = "cancelled"


class ContractType(enum.StrEnum):
    purchase = "purchase"
    service = "service"
    subscription = "subscription"
    lease = "lease"
    sla = "sla"
    msa = "msa"
    sow = "sow"
    other = "other"


class Contract(Base, EntityMixin, TimestampMixin):
    """A vendor contract — the spine of contract lifecycle management.

    Drives four downstream features (see ``backend/docs/contracts.md``):
    spend-to-contract tracking (``Invoice.contract_id``), renewal alerts
    (``services.contract_renewal``), compliance monitoring
    (``services.contract_compliance`` → ``invoice_warnings``), and
    contract-based PO creation (``POST /api/contracts/{id}/create-po``).

    Money is exact: every currency column is ``Numeric(15, 2)`` (never
    float) — the project's money-is-exact invariant.
    """

    __tablename__ = "contracts"

    # `GET /api/contracts`'s default order + its status chips. Same shape and
    # same reasoning as `ix_invoices_created_at_id` — see migration 0092.
    __table_args__ = (
        Index("ix_contracts_created_at_id", text("created_at DESC"), text("id DESC")),
        Index(
            "ix_contracts_status_created_at_id",
            "status",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contract_number: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    contract_type: Mapped[ContractType] = mapped_column(
        Enum(ContractType, native_enum=False, length=30),
        default=ContractType.purchase,
    )
    status: Mapped[ContractStatus] = mapped_column(
        Enum(ContractStatus, native_enum=False, length=30),
        default=ContractStatus.draft,
    )

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False, index=True
    )

    # --- Money (all Numeric, never float) ---------------------------------
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # Total committed value over the life of the contract.
    total_value: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    # Cumulative-spend ceiling for compliance monitoring. NULL = no ceiling.
    spend_limit: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    # When true, spend above ``spend_limit`` is a hard violation (error-severity
    # exception); when false it is advisory (warning). See contract_compliance.
    not_to_exceed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Lifecycle dates ---------------------------------------------------
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, index=True)
    signed_date: Mapped[date | None] = mapped_column(Date)
    # When the contract first came into force — stamped on the first activation
    # and never cleared, so a contract that was active and later cancelled still
    # reads as one that bound the parties. `signed_date` is optional user input
    # and cannot be relied on for that. Read by the workspace-closure guard
    # (decisions §306); migration 0120 backfilled it from the audit trail.
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Renewal config (drives services.contract_renewal) -----------------
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    renewal_term_months: Mapped[int | None] = mapped_column(Integer)
    # Notify this many days before ``end_date``. Per-contract override of the
    # org/platform default window.
    renewal_notice_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    # Set once a renewal alert fires for the current ``end_date`` so the sweep
    # loop is idempotent (no daily re-notification). Cleared on renew/extend.
    renewal_alert_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    payment_terms: Mapped[str | None] = mapped_column(String(100))
    # Internal contract owner (control-plane User id). Notified on renewal.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # --- Stored contract document (S3/MinIO) -------------------------------
    file_url: Mapped[str | None] = mapped_column(String(1024))
    file_key: Mapped[str | None] = mapped_column(String(512))

    # Structured terms for compliance checks. Recognised keys:
    #   {"allowed_gl_accounts": [str], "allowed_cost_centers": [str],
    #    "categories": [str]}
    # Anything else is preserved untouched.
    terms: Mapped[dict | None] = mapped_column(JSONB)
    # Free-form per-contract metadata bag. No PII / banking data.
    meta: Mapped[dict | None] = mapped_column(JSONB)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )

    line_items: Mapped[list["ContractLineItem"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )


class ContractLineItem(Base, TimestampMixin):
    __tablename__ = "contract_line_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contracts.id"), nullable=False, index=True
    )
    line_number: Mapped[int | None] = mapped_column(Integer)
    item_code: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    gl_account: Mapped[str | None] = mapped_column(String(100))

    contract: Mapped[Contract] = relationship(back_populates="line_items")
