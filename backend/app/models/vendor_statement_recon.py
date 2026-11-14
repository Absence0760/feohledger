"""Vendor statement reconciliation — `vendor_statement_reconciliations` + lines.

A supplier periodically sends a **statement of open items** — every invoice it
believes we still owe. Reconciling that statement against our own AP ledger is a
core month-end-close task that's entirely manual today: the clerk eyeballs the
supplier's list against our open invoices and chases the differences.

A :class:`VendorStatementReconciliation` is one such reconciliation run for one
vendor, as of a `statement_date`. Its :class:`VendorStatementReconLine` children
are the per-line results the (pure) reconciliation engine produced — each
classified as one of:

  * ``matched``               — statement line ↔ our invoice, amounts agree
  * ``amount_mismatch``       — same invoice, amounts differ beyond tolerance
  * ``missing_on_our_side``   — supplier billed it, we have no invoice (the
                                actionable rows that feed invoice intake)
  * ``missing_on_their_side`` — we have an open invoice the statement omitted

Distinct from *bank* reconciliation (cleared payments ↔ bank lines —
``app.models.bank_reconciliation``): that ties out cash that already moved; this
ties out *open balances* before they do.

Money is ``Numeric`` (never float). See
``backend/docs/vendor-statement-reconciliation.md``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, EntityMixin, TimestampMixin

# Line classification — the four reconciliation outcomes.
CLASS_MATCHED = "matched"
CLASS_AMOUNT_MISMATCH = "amount_mismatch"
CLASS_MISSING_OUR_SIDE = "missing_on_our_side"
CLASS_MISSING_THEIR_SIDE = "missing_on_their_side"
CLASSIFICATIONS = (
    CLASS_MATCHED,
    CLASS_AMOUNT_MISMATCH,
    CLASS_MISSING_OUR_SIDE,
    CLASS_MISSING_THEIR_SIDE,
)

# Run-level review status.
STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
RUN_STATUSES = (STATUS_OPEN, STATUS_RESOLVED)

# Per-line resolution status.
RESOLUTION_UNRESOLVED = "unresolved"
RESOLUTION_RESOLVED = "resolved"
RESOLUTION_IGNORED = "ignored"
RESOLUTION_STATUSES = (RESOLUTION_UNRESOLVED, RESOLUTION_RESOLVED, RESOLUTION_IGNORED)

# Where the statement came from.
SOURCE_CSV = "csv"
SOURCE_MANUAL = "manual"
SOURCE_PDF = "pdf"
SOURCE_FORMATS = (SOURCE_CSV, SOURCE_MANUAL, SOURCE_PDF)


class VendorStatementReconciliation(Base, EntityMixin, TimestampMixin):
    __tablename__ = "vendor_statement_reconciliations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )

    # The vendor whose statement this is. `vendor_name` is denormalised for
    # display (mirrors Invoice). Nullable FK so the row survives a vendor
    # delete, but a run is always created against a real vendor.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), index=True
    )
    vendor_name: Mapped[str | None] = mapped_column(String(255))

    # "As of" date the supplier's statement covers; the supplier's own
    # statement number/reference, when present.
    statement_date: Mapped[date] = mapped_column(Date, nullable=False)
    statement_reference: Mapped[str | None] = mapped_column(String(120))
    # No default: both intake doors require the statement's currency
    # (decisions §377, §402), and a run matches only the ledger in it.
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # 'csv' | 'manual' | 'pdf'. The S3 key of the uploaded statement, kept for
    # audit replay (NULL for the manual / pasted-lines path).
    source_format: Mapped[str] = mapped_column(String(20), nullable=False, default=SOURCE_MANUAL)
    file_key: Mapped[str | None] = mapped_column(String(512))

    # Run review status: 'open' until the clerk has cleared every actionable
    # difference, then 'resolved'.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_OPEN, index=True)

    # Totals: the statement's claimed open balance vs. our matched ledger total.
    statement_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    ledger_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))

    # Outcome rollup — denormalised counts so the list view needs no line scan.
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amount_mismatch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_our_side_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_their_side_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    notes: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    meta: Mapped[dict | None] = mapped_column(JSONB)

    lines: Mapped[list[VendorStatementReconLine]] = relationship(
        back_populates="reconciliation",
        cascade="all, delete-orphan",
        order_by="VendorStatementReconLine.created_at",
    )


class VendorStatementReconLine(Base, EntityMixin):
    __tablename__ = "vendor_statement_recon_lines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendor_statement_reconciliations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # The supplier's view of this line. All NULL for a `missing_on_their_side`
    # row (no statement line — it's one of OUR open invoices the statement
    # omitted).
    statement_invoice_number: Mapped[str | None] = mapped_column(String(100))
    statement_date: Mapped[date | None] = mapped_column(Date)
    statement_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    statement_status: Mapped[str | None] = mapped_column(String(40))

    classification: Mapped[str] = mapped_column(String(30), nullable=False, index=True)

    # Our view — the invoice we matched (or the orphan invoice, for
    # `missing_on_their_side`). NULL for `missing_on_our_side`.
    matched_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), index=True
    )
    ledger_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    # statement_amount − ledger_amount (signed), for the amount_mismatch view.
    amount_difference: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    match_method: Mapped[str | None] = mapped_column(String(40))  # 'invoice_number' | 'amount_date'

    resolution_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RESOLUTION_UNRESOLVED
    )
    resolution_note: Mapped[str | None] = mapped_column(String(500))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    reconciliation: Mapped[VendorStatementReconciliation] = relationship(back_populates="lines")
