import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, TimestampMixin

#: Every status a memo can hold. `applied` is all-or-nothing — one memo credits
#: its whole amount to exactly one invoice in one transaction — so there is no
#: "partially applied" state to represent.
CREDIT_MEMO_STATUSES: tuple[str, ...] = ("open", "applied", "void")


class CreditMemo(Base, EntityMixin, TimestampMixin):
    """Vendor-issued credit reducing what's owed.

    Lifecycle: ``open`` (issued, unapplied) → ``applied`` (linked to an
    invoice; reduces the payable) → ``void`` (rescinded). Applied
    credits are immutable for audit purposes.
    """

    __tablename__ = "credit_memos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memo_number: Mapped[str] = mapped_column(String(100), nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False, index=True
    )
    # Set when applied to an invoice. Null while status='open'.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    issued_date: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, applied, void
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_by: Mapped[str | None] = mapped_column(String(255))

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
