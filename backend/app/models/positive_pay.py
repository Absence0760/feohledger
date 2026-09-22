"""Positive Pay / payment-fraud file — ``positive_pay_files``.

Positive Pay is a treasury fraud control. We hand the bank a file of the
cheques we *issued* (check number + payee + exact amount + issue date) — or,
for ACH debit-block / authorization, the list of accounts authorized to pull
from us. When an item is later presented for payment, the bank matches it
against our issued file; anything that doesn't match (an altered amount, a
cheque we never wrote) is flagged for our review instead of clearing silently.

A :class:`PositivePayFile` is one generated export — a ``check_issue`` file for
a single :class:`~app.models.payment.PaymentRun`, or a standalone
``ach_authorization`` file. The rendered file itself (which legitimately
contains full account numbers — that's its purpose) lives in MinIO under
``file_key``; this DB row stores only PII-free metadata, masking the
originating account to ``account_last4``. Return processing (the bank tells us
which items it saw) classifies presented items and records the fraud summary in
:attr:`meta`.

Idempotency: the partial unique index ``uq_positive_pay_run_format`` guarantees
one check-issue file per ``(payment_run_id, bank_format)`` — re-generating
returns the existing row rather than emitting a second file.

Money is ``Numeric`` (never float). PII (full account / routing numbers) never
enters this row, the audit trail, logs, or error bodies. See
``backend/docs/positive-pay.md``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, TimestampMixin

# File type — what the export represents.
FILE_TYPE_CHECK_ISSUE = "check_issue"
FILE_TYPE_ACH_AUTHORIZATION = "ach_authorization"
FILE_TYPES = (FILE_TYPE_CHECK_ISSUE, FILE_TYPE_ACH_AUTHORIZATION)

# Lifecycle status.
STATUS_GENERATED = "generated"
STATUS_RETURNED_PROCESSED = "returned_processed"
STATUSES = (STATUS_GENERATED, STATUS_RETURNED_PROCESSED)


class PositivePayFile(Base, EntityMixin, TimestampMixin):
    __tablename__ = "positive_pay_files"

    # THE idempotency backstop for `POST /payment-runs/{id}/check-issue`, which
    # is a read-then-insert: two concurrent requests both find no existing row
    # and both insert, and only this index turns the loser into the
    # `IntegrityError` the handler catches. Without it the run ends up with two
    # files (two MinIO objects carrying full account numbers) AND the
    # `scalar_one_or_none()` idempotency lookup then raises
    # `MultipleResultsFound` on every later call — a permanent 500 on that run.
    # Migration 0048 created it; nothing declared it, so every
    # `create_all`-provisioned tenant ran without it. Partial because an
    # `ach_authorization` file is org-wide and run-less (NULL never collides).
    __table_args__ = (
        Index(
            "uq_positive_pay_run_format",
            "payment_run_id",
            "bank_format",
            unique=True,
            postgresql_where=text("payment_run_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )

    # The payment run this check-issue file was generated for. NULL for an
    # ``ach_authorization`` file (which is org-wide, not run-scoped).
    #
    # Deliberately NOT `index=True`. `uq_positive_pay_run_format` above leads on
    # this column, and a B-tree's leading column is independently searchable, so
    # the composite serves every read a `(payment_run_id)` index could — the two
    # real call sites (the check-issue idempotency lookup and its
    # post-`IntegrityError` re-read) qualify on `bank_format` too, so it serves
    # them BETTER, matching both columns instead of filtering the second. The
    # separate narrow index migration 0048 built was therefore pure write
    # overhead; migration 0094 drops it, and `EXEMPT` in
    # `tests/test_migration_model_index_parity.py` records why 0048's CREATE has
    # no model declaration. The one read the partial composite cannot serve is
    # `payment_run_id IS NULL` (its predicate excludes those rows) — no such
    # query exists; the run-less `ach_authorization` files are reached through
    # `file_type`. Adding one is the trigger to revisit this.
    payment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_runs.id")
    )

    # 'check_issue' | 'ach_authorization'.
    file_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # Formatter key, e.g. 'csv' | 'fixed_width'.
    bank_format: Mapped[str] = mapped_column(String(30), nullable=False)

    # 'generated' until the bank's return is processed, then 'returned_processed'.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=STATUS_GENERATED, index=True
    )

    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)

    # Currency ``total_amount`` is denominated in (ISO 4217). For a check-issue
    # file, the one currency its cheques' invoices agree on — ``Payment.amount``
    # is in the INVOICE's currency, not the org's — so a stored label, not an
    # FX conversion. NULL for legacy rows and for cheques that disagree; the UI
    # renders those totals bare. See ``backend/docs/positive-pay.md``.
    currency: Mapped[str | None] = mapped_column(String(3))

    # sha256 hex of the rendered file content (tamper-evidence / dedupe aid).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # MinIO storage key of the rendered file (the file holding full account numbers).
    file_key: Mapped[str | None] = mapped_column(String(512))

    # Masked originating / check account — NEVER the full number.
    account_last4: Mapped[str | None] = mapped_column(String(4))

    generated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Return-processing summary (presented counts, fraud classifications,
    # unmatched returns). PII-free. Mutable so in-place updates flush.
    meta: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSONB))
