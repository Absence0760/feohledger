"""The record of a workspace an administrator closed themselves.

Control plane, and deliberately OUTSIDE the tenant deletion it records: a
self-service closure (`services/workspace_closure.py`, `docs/decisions.md`
§294) destroys the tenant database, so an audit row written only there would
go with it, and the organisation and user rows it would point at are deleted
too. This row is what is left — the evidence that the closure was asked for,
by whom (a user id that no longer resolves, never an address), which proof
authorized it, and what the deletion removed.

It also carries the one step the application cannot do itself: the per-tenant
backup dumps live in the operator's backup bucket, which the API has no access
to, and `/legal/dpa` § 13 promises they go within 60 days. `backups_cleared_at`
is NULL until an operator runs `deploy/remove-tenant.sh <slug>` (whose backup
leg runs on a tenant that is already gone) and marks the row with
`scripts/workspace_closures.py` — that listing is the operator's work queue.

No foreign keys, on purpose: both the organisation and the requesting user are
deleted by the closure this row describes. No email, name or company name:
the slug and the ids are enough to correlate with an operator's records and
with the shipped audit trail, and nothing here is personal data on its own.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: The closure has passed its guards and the deletion has begun (or failed
#: part-way and may be retried by the same administrator).
CLOSURE_STARTED = "started"
#: The tenant database, documents and control-plane rows are gone.
CLOSURE_COMPLETED = "completed"


class WorkspaceClosure(Base):
    __tablename__ = "workspace_closures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # One closure per organisation: a retry after a partial failure updates
    # this row rather than writing a second. UNIQUE is also what makes the
    # upsert in `services/workspace_closure` a single statement.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True
    )
    slug: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The requester's role NAMES at the moment of the closure (e.g. ["admin"]),
    # because the user row they would be read from is deleted by it. Names
    # only: no address, no display name, no source IP — the IP is on the
    # shipped `organization.closing` audit event, like every other audit row.
    requested_by_roles: Mapped[list | None] = mapped_column(JSONB)
    # `api/auth.require_sensitive_step_up`'s answer: totp | passkey |
    # mfa_off_local (the local-dev skip, named so it is never mistaken for a
    # verified proof).
    step_up_proof: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    documents_removed: Mapped[int | None] = mapped_column(Integer)
    database_dropped: Mapped[bool | None] = mapped_column(Boolean)
    control_rows_removed: Mapped[int | None] = mapped_column(Integer)
    # The last time a run of this closure failed part-way, and the exception
    # CLASS (never its message). The tenant's own log may already be gone when
    # that happens, so this is where the failure is recorded.
    last_failure: Mapped[str | None] = mapped_column(String(64))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set by an operator once the tenant's backup objects are deleted (see the
    # module docstring). NULL on a completed closure = still owed.
    backups_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Who did: the operator identifier `scripts/workspace_closures.py
    # mark-cleared --operator` requires (a handle or ticket, not an address).
    backups_cleared_by: Mapped[str | None] = mapped_column(String(100))
