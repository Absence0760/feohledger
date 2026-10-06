import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, TimestampMixin

#: ``QualityInspection.source`` — where the verdict came from (migration 0105).
INSPECTION_SOURCE_MANUAL = "manual"
INSPECTION_SOURCE_QMS = "qms"


class QualityInspection(Base, EntityMixin, TimestampMixin):
    """Quality-inspection record — the 4th leg of 4-way matching.

    Pairs an invoice/PO/GR with an inspection outcome (``pass`` / ``fail`` /
    ``partial``). ``po_matching.match_invoice_to_po`` looks one up to gate the
    match: the receipt's own inspection first (``gr_id``), then a PO-level one
    (``po_id`` with ``gr_id`` NULL) — the shape ``qms_sync`` writes whenever the
    QMS knows the PO number but not the GR number. ``entity_id`` comes from
    ``EntityMixin``; see ``docs/po-matching.md`` § 4-way.
    """

    __tablename__ = "quality_inspections"

    # The 4-way match looks an inspection up by receipt first, then by PO —
    # both on every matched invoice. Migration 0033's; declared here so
    # `create_all` builds them too.
    __table_args__ = (
        Index("ix_quality_inspections_po_id", "po_id"),
        Index("ix_quality_inspections_gr_id", "gr_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    inspection_number: Mapped[str] = mapped_column(String(100), nullable=False)
    po_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id")
    )
    gr_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goods_receipts.id")
    )
    result: Mapped[str] = mapped_column(String(20), nullable=False, default="pass")
    inspected_date: Mapped[date | None] = mapped_column(Date)
    inspector: Mapped[str | None] = mapped_column(String(255))
    accepted_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    rejected_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    deviation_notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="completed")
    # Where the verdict came from, and — for one typed in by hand — who typed it
    # (migration 0105). A ``quality_hold`` is a payment block, and a refresh
    # that no longer finds it closes it (`invoice_warnings.
    # _close_cleared_po_exceptions`); a ``pass`` recorded by someone implicated
    # in the invoice must not be what releases it. ``qms`` = synced from the
    # QMS (`services/qms_sync`, recorder NULL); ``manual`` = `POST
    # /api/inspections`, recorder stamped. NULL on rows predating the columns,
    # which the close treats as unknown — held for a human. Control-plane user
    # id, so no FK.
    source: Mapped[str | None] = mapped_column(String(20))
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
