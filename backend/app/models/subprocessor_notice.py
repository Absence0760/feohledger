"""The ledger of sub-processor change notices sent to customer organisations.

One row per (change-log entry, organisation): the record that the advance notice
`/legal/dpa` § 8 promises was given, and when. ``services/subprocessor_notices``
claims the row before it sends, so the UNIQUE pair is what makes the notice
idempotent — an organisation is told once per entry, however many replicas run
the sweep and however often it ticks (`docs/decisions.md` §320).

Control plane: an organisation is a control-plane row, and so are the admins
the notice goes to. No foreign key, deliberately, like ``workspace_closures``:
a closed workspace's organisation row is deleted, and the evidence that it was
given notice should outlive it rather than cascade away. No email address, name
or user id — the per-recipient detail is on the tenant's own audit row
(``privacy.subprocessor_notice_sent``); this row holds ids and counts only.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
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
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: The entry's effective date as it stood when this notice went out, so the
    #: ledger answers "how much notice did this organisation get" on its own.
    effective_on: Mapped[date] = mapped_column(Date, nullable=False)
    #: Administrators the email reached / could not be handed to the provider.
    recipients: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: True when it went out with fewer than the promised days before
    #: ``effective_on`` — an urgent entry, or a sweep that was not running.
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
