"""The ledger of sub-processor change notices sent to customer organisations.

One row per (change-log entry, organisation): the record that the advance notice
`/legal/dpa` § 8 promises was given, and when. ``services/subprocessor_notices``
claims the row (and COMMITS the claim) before it sends, so the UNIQUE pair is
what makes the notice idempotent — an organisation is told once per entry,
however many replicas run the sweep and however often it ticks
(`docs/decisions.md` §321).

The row moves through three timestamps, each its own short transaction so no
lock is held across an email send or a tenant-database round trip:

- ``claimed_at`` — a run took the pair. A claim with no ``sent_at`` older than
  ``subprocessor_notices.CLAIM_TIMEOUT`` belongs to a run that died mid-send
  and may be re-taken (the only path that can email an organisation twice).
- ``sent_at`` — at least one administrator's email reached the provider; the
  counts and ``late`` are recorded with it. From here the emails are never
  re-sent.
- ``audited_at`` — the tenant's ``privacy.subprocessor_notice_sent`` audit row
  is written. A sent row with no ``audited_at`` is retried for the audit alone.

Control plane: an organisation is a control-plane row, and so are the admins
the notice goes to. No foreign key, deliberately, like ``workspace_closures``:
a closed workspace's organisation row is deleted, and the evidence that it was
given notice should outlive it rather than cascade away. No email address or
name — the recipients are user ids, which the audit retry needs.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SubprocessorNotice(Base):
    __tablename__ = "subprocessor_notices"
    __table_args__ = (
        UniqueConstraint("entry_id", "organization_id", name="uq_subprocessor_notices_entry_org"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: ``subprocessor_changelog.ChangelogEntry.id`` — which change was notified.
    entry_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    audited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The entry's effective date as it stood when this notice went out, so the
    #: ledger answers "how much notice did this organisation get" on its own.
    effective_on: Mapped[date] = mapped_column(Date, nullable=False)
    #: Administrators the email reached / could not be handed to the provider.
    recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: User ids the email reached — what the tenant audit row records.
    recipient_user_ids: Mapped[list | None] = mapped_column(JSONB)
    #: True when it went out with fewer than the promised days before
    #: ``effective_on`` — an urgent entry, or a sweep that was not running.
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
