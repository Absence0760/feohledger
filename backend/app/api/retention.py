"""Retention-policy configuration endpoints (`/api/retention-policy`).

Per-record-class retention windows live on ``Organization.settings.retention``
(configurable, not hardcoded). Admin-only read + update; every mutation writes a
``retention_policy.updated`` audit row into the tenant trail. The enforcement
sweep that acts on these windows is ``services/retention_sweep.py`` (disabled by
default behind ``FEOH_RETENTION_ENABLED``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import ROLE_ADMIN, require_roles
from app.config import settings
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.retention_sweep import resolve_retention_months
from app.tenant import get_tenant

router = APIRouter(prefix="/retention-policy", tags=["retention"])

# Record classes the retention engine understands. Each maps to a
# ``<class>_months`` key under ``Organization.settings.retention``.
#
# ``positive_pay`` expires the generated check-issue / ACH-authorization FILE
# from object storage — the one artefact that legitimately carries full account
# and routing numbers — while keeping its PII-free row. It defaults to a much
# shorter window than the rest (``retention_sweep.RECORD_CLASS_DEFAULT_MONTHS``)
# because the bank consumes the file within days.
RECORD_CLASSES = ["invoices", "audit_log", "positive_pay"]


class RetentionPolicyResponse(BaseModel):
    # Effective windows (per-org override → platform default) per record class.
    policy: dict[str, int]
    # The platform default, surfaced so the UI can show "(default)" affordances.
    default_months: int
    enabled: bool


class UpdateRetentionPolicyRequest(BaseModel):
    # Each value is a retention window in months (> 0). Only the classes present
    # are updated; omitted classes keep their current value. Unknown keys 422.
    policy: dict[str, int] = Field(..., min_length=1)


def _effective_policy(settings_dict: dict | None) -> dict[str, int]:
    return {cls: resolve_retention_months(settings_dict, cls) for cls in RECORD_CLASSES}


@router.get("", response_model=RetentionPolicyResponse)
async def get_retention_policy(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """Return the effective retention policy (per-org override → default)."""
    return RetentionPolicyResponse(
        policy=_effective_policy(org.settings),
        default_months=settings.retention_default_months,
        enabled=settings.retention_enabled,
    )


@router.put("", response_model=RetentionPolicyResponse)
async def update_retention_policy(
    body: UpdateRetentionPolicyRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Update one or more per-class retention windows. Admin only; audited.

    Unknown record classes or non-positive windows are rejected (422) before any
    write, so a typo can't silently create a dead config key.
    """
    for cls, months in body.policy.items():
        if cls not in RECORD_CLASSES:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown record class '{cls}'; valid: {RECORD_CLASSES}",
            )
        if not isinstance(months, int) or months <= 0:
            raise HTTPException(
                status_code=422,
                detail=f"Retention months for '{cls}' must be a positive integer",
            )

    existing = dict(org.settings or {})
    retention = dict(existing.get("retention") or {})
    before = {cls: retention.get(f"{cls}_months") for cls in body.policy}
    for cls, months in body.policy.items():
        retention[f"{cls}_months"] = months
    existing["retention"] = retention
    org.settings = existing
    flag_modified(org, "settings")

    await db.commit()

    # Audit the config change into the TENANT trail (where every other mutation
    # for this tenant lands). The settings live on the control plane, so use the
    # self-committing tenant-audit helper — it resolves the tenant DB from the
    # org id and opens its own short-lived session, rather than holding a
    # request-scoped tenant session open across validation. PII-free: only class
    # names + month windows.
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="retention_policy.updated",
        entity_id=org.id,
        details={
            "changes": {
                cls: {"old": before[cls], "new": months} for cls, months in body.policy.items()
            }
        },
    )

    return RetentionPolicyResponse(
        policy=_effective_policy(org.settings),
        default_months=settings.retention_default_months,
        enabled=settings.retention_enabled,
    )
