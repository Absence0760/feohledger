"""Scheduled-report CRUD — ``/api/analytics/scheduled-reports``.

The input surface for ``services/scheduled_reports``' runner. The runner has
been complete and tested for a while; nothing under ``app/api/`` referenced the
``ScheduledReport`` model, so a row could only be created by direct SQL,
``list_due_schedules`` returned ``[]`` on every tick forever, and the documented
5-strike auto-disable was a one-way door — an operator had no way to re-enable a
schedule the runner had switched off.

Design notes worth keeping:

- **Admin-only to mutate.** A schedule is a standing instruction to email a CSV
  of the tenant's AP spend to an arbitrary address on a recurring basis, with no
  further review of any individual send. That is a data-egress control, not a
  reporting preference, so it sits above the `_CFO_ROLES` gate the rest of
  ``/analytics`` uses. Reads are admin + CFO — the CFO owns the reports and
  needs to see (and audit) what is going out.
- **Tenant-scoped, deliberately NOT entity-scoped.** ``ScheduledReport`` has no
  ``entity_id`` and the runner's ``_materialise_rows`` applies no entity filter
  to any of its six report types — the emailed CSV is whole-tenant by
  construction. Stamping an entity on the schedule row would advertise a scope
  the delivered file does not honour, which is worse than not offering it.
  Making it real means entity-filtering the materializer AND a migration; that
  is its own slice. Tenant isolation itself is enforced the normal way, through
  the ``get_tenant_db`` chokepoint.
- **Validation is against the runner's own registries**, not restated copies —
  see ``app/schemas/scheduled_report.py``. A ``report_type`` outside
  ``report_export.EXPORTERS`` raises on every tick and burns the auto-disable
  without ever sending; an unknown ``cadence`` silently reschedules as daily.

Every mutation writes a PII-free audit row: recipient addresses are third-party
PII and never enter the trail — only their **count** does.

See ``backend/docs/analytics.md`` § Scheduled report delivery.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, ROLE_CFO, get_org_id, require_roles
from app.models.scheduled_report import ScheduledReport
from app.models.user import User
from app.schemas.scheduled_report import (
    ScheduledReportCreate,
    ScheduledReportListResponse,
    ScheduledReportResponse,
    ScheduledReportUpdate,
)
from app.services.audit_dispatch import dispatch_audit
from app.tenant import get_tenant_db

router = APIRouter(prefix="/analytics/scheduled-reports", tags=["analytics"])

_READ_ROLES = (ROLE_ADMIN, ROLE_CFO)
_WRITE_ROLES = (ROLE_ADMIN,)


def _audit_details(row: ScheduledReport) -> dict:
    """PII-free audit payload.

    The recipient ADDRESSES are third-party PII and the audit trail is
    append-only + WORM-shipped, so they must never enter it. The count is the
    part an auditor actually needs — "this schedule went to 4 people" answers
    the control question; who they were is on the row, which is mutable and
    correctable.
    """
    return {
        "name": row.name,
        "report_type": row.report_type,
        "cadence": row.cadence,
        "recipient_count": len(row.recipients or []),
        "period_days": row.period_days,
        "enabled": row.enabled,
    }


async def _get_or_404(db: AsyncSession, schedule_id: uuid.UUID) -> ScheduledReport:
    row = (
        await db.execute(select(ScheduledReport).where(ScheduledReport.id == schedule_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Scheduled report not found")
    return row


@router.get("", response_model=ScheduledReportListResponse)
async def list_scheduled_reports(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    """Every schedule in the tenant, newest first, plus the valid
    `report_types` / `cadences` so a client never hardcodes them."""
    rows = list(
        (await db.execute(select(ScheduledReport).order_by(ScheduledReport.created_at.desc())))
        .scalars()
        .all()
    )
    return ScheduledReportListResponse(schedules=[ScheduledReportResponse.from_db(r) for r in rows])


@router.post("", response_model=ScheduledReportResponse, status_code=status.HTTP_201_CREATED)
async def create_scheduled_report(
    body: ScheduledReportCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    row = ScheduledReport(
        organization_id=org_id,
        name=body.name,
        report_type=body.report_type,
        cadence=body.cadence,
        recipients=body.recipients,
        period_days=body.period_days,
        enabled=body.enabled,
        # Omitted → due on the next tick, so the operator can see it work.
        # `advance_next_run` then HOLDS this time-of-day for every later run.
        next_run_at=body.next_run_at or datetime.now(UTC),
        created_by=user.id,
    )
    db.add(row)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="scheduled_report.created",
        entity_type="scheduled_report",
        entity_id=row.id,
        details=_audit_details(row),
    )
    await db.commit()
    await db.refresh(row)
    return ScheduledReportResponse.from_db(row)


@router.get("/{schedule_id}", response_model=ScheduledReportResponse)
async def get_scheduled_report(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    return ScheduledReportResponse.from_db(await _get_or_404(db, schedule_id))


@router.patch("/{schedule_id}", response_model=ScheduledReportResponse)
async def update_scheduled_report(
    schedule_id: uuid.UUID,
    body: ScheduledReportUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Partial update. Re-enabling a schedule the 5-strike rule disabled is the
    main reason this endpoint exists, so it also clears the failure marker —
    otherwise `_mark_failure` reads the stale `[retry 5]` prefix and the very
    next failure disables it again immediately, which is indistinguishable from
    the re-enable not having worked."""
    row = await _get_or_404(db, schedule_id)

    # Only a value that differs from the stored one is applied and named in the
    # audit row — the edit form resends every field, and recording presence
    # made the trail claim a recipient change on a save that only renamed the
    # schedule (#575).
    submitted = body.model_dump(exclude_unset=True)
    changes = {field: value for field, value in submitted.items() if getattr(row, field) != value}
    for field, value in changes.items():
        setattr(row, field, value)

    # Keyed on what was SUBMITTED, not on `changes`: "enable" on a schedule that
    # is still enabled but failing (under the 5-strike limit) is the operator
    # acknowledging the failure, and it must still reset the marker.
    if submitted.get("enabled") is True and row.last_run_status == "failure":
        row.last_run_status = None
        row.last_run_error = None
        changes["last_run_status"] = None

    if changes:
        row.updated_at = datetime.now(UTC)
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="scheduled_report.updated",
            entity_type="scheduled_report",
            entity_id=row.id,
            # Field NAMES only for what changed — a recipient list edit must not
            # write the addresses into the append-only trail.
            details={**_audit_details(row), "fields_changed": sorted(changes)},
        )
    await db.commit()
    await db.refresh(row)
    return ScheduledReportResponse.from_db(row)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scheduled_report(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    row = await _get_or_404(db, schedule_id)
    details = _audit_details(row)
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="scheduled_report.deleted",
        entity_type="scheduled_report",
        entity_id=row.id,
        details=details,
    )
    await db.delete(row)
    await db.commit()
    return None
