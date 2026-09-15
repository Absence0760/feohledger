import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, TimestampMixin


class Exception(Base, EntityMixin, TimestampMixin):
    __tablename__ = "exceptions"

    # `GET /api/exceptions`'s default order + its status chips. Same shape and
    # same reasoning as `ix_invoices_created_at_id` — see migration 0092.
    __table_args__ = (
        Index("ix_exceptions_created_at_id", text("created_at DESC"), text("id DESC")),
        Index(
            "ix_exceptions_status_created_at_id",
            "status",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # The SLA sweep asks "what is overdue and still live?" — partial, so the
        # index only holds the open tail rather than every exception ever
        # resolved. Migration 0013's; declared here so `create_all` builds it too.
        Index(
            "ix_exceptions_due_at",
            "due_at",
            postgresql_where=text("status IN ('open', 'escalated')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable: nearly every exception is invoice-scoped, but a few fraud signals
    # have no invoice — notably a Positive Pay `not_on_file` return (a cheque the
    # bank cleared that we never issued). Those surface as a standalone
    # `fraud_flag` with `invoice_id=None` rather than being hidden in a JSON
    # field. Agent auto-resolution requires an invoice, so the agent-resolve
    # path 422s on an invoice-less exception (human triage only).
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=True, index=True
    )
    exception_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # Types: duplicate, po_mismatch, fraud_flag, extraction_failed,
    #        unverified_vendor, review_rejected, amount_exceeded, missing_data
    severity: Mapped[str] = mapped_column(String(20), default="warning")  # error, warning, info
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(30), default="open"
    )  # open, resolved, escalated, dismissed
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(255))  # user name
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_to: Mapped[str | None] = mapped_column(String(255))  # user name for routing
    # UUID-keyed assignee. Set by the auto-routing rule at creation, or
    # by `PATCH /api/exceptions/{id}/assign`. Lives alongside the
    # `assigned_to` string for backward compat — the API exposes both.
    assigned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    # The control-plane user whose OWN act this exception exists to have a
    # second person look at — not whoever was signed in when a detector fired.
    # Segregation of duties on the queue refuses this actor the clearing verbs
    # (`exception_lifecycle.segregation_refusal`), so a fabricated value would
    # manufacture a refusal and an honest-looking absolution for whoever really
    # caused the flag. Almost every raise site is a detector or a sweep with no
    # such actor and correctly stores NULL — which is permissive, for the same
    # reason `Invoice.uploaded_by_id` is (decisions §131, §152). No FK: `users`
    # is control-plane and this table is tenant-scoped, same placement as
    # `assigned_to_user_id` above and as migrations 0095-0097.
    # `tests/test_exception_raiser_stamping.py` is the guard that keeps every
    # `create_exception` call site answering the question explicitly.
    raised_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # SLA: deadline derived from org settings at creation time. NULL =
    # no SLA configured for this exception type.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Populated when the exception flips to a terminal state. Stored
    # in seconds for precision; the API surfaces it in hours.
    time_to_resolution_seconds: Mapped[int | None] = mapped_column(Integer)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
