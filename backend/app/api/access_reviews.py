"""Periodic access-review endpoints (`/api/access-reviews`) — SOX access control.

Two routes, both admin/CFO only (the reviewer privilege):

- ``GET /api/access-reviews`` — the computed review list: every user holding an
  elevated role, with their last *mutating* privileged action and a dormancy
  verdict. This is a sensitive read (it enumerates who holds privileged access),
  so it writes an ``access_review.viewed`` audit row via ``log_access``.
- ``POST /api/access-reviews/acknowledge`` — records that a reviewer completed
  the review for the period. Writes an ``access_review.completed`` audit row and
  stamps ``Organization.settings.access_review.last_completed_at`` (+ reviewer
  id) on the control-plane org row. Idempotent-friendly: re-acknowledging just
  re-stamps with the new timestamp + reviewer.

Both routes are GET/POST reads-or-settings only — no money moves — so the
idempotency-on-money invariant does not bind. The computed list resolves users
from the control DB and last-action timestamps from the tenant DB; the
acknowledge stamp is a control-plane settings write. Tenant isolation is enforced
by the injected ``get_tenant_db`` (which cross-checks the JWT ``org`` claim) plus
filtering every query by ``user.organization_id``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, ROLE_CFO, require_roles
from app.config import settings
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.schemas.access_review import (
    AccessReviewAcknowledgeResponse,
    AccessReviewResponse,
    AccessReviewUser,
)
from app.services.access_review import compute_access_review
from app.services.audit_access import log_access
from app.services.audit_dispatch import dispatch_audit
from app.tenant import get_tenant_db, lock_organization

router = APIRouter(prefix="/access-reviews", tags=["access-reviews"])


@router.get("", response_model=AccessReviewResponse)
async def list_access_review(
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
):
    """Compute the periodic access review for the current org.

    Lists every active user holding an elevated role (``admin`` / ``ap_manager``
    / ``cfo``) with their last mutating privileged action and a dormancy flag.
    The read itself is audited (``access_review.viewed``) — viewing the privileged
    roster is a SOX-relevant access event.
    """
    now = datetime.now(UTC)
    rows = await compute_access_review(
        control_db,
        db,
        organization_id=user.organization_id,
        dormant_after_days=settings.access_review_dormant_days,
        now=now,
    )

    # Sensitive read: record WHO ran the review and HOW MANY users it covered.
    # No regulated value enters the details — only counts + the dormancy window.
    dormant_count = sum(1 for r in rows if r.dormant)
    await log_access(
        db,
        user=user,
        organization_id=user.organization_id,
        entity_type="access_review",
        entity_id=user.organization_id,
        extra={
            "reviewed": len(rows),
            "dormant": dormant_count,
            "dormant_after_days": settings.access_review_dormant_days,
        },
    )
    await db.commit()

    return AccessReviewResponse(
        dormant_after_days=settings.access_review_dormant_days,
        generated_at=now.isoformat(),
        total=len(rows),
        dormant_count=dormant_count,
        users=[AccessReviewUser.from_row(r) for r in rows],
    )


@router.post("/acknowledge", response_model=AccessReviewAcknowledgeResponse)
async def acknowledge_access_review(
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
):
    """Record that a reviewer completed the access review for this period.

    The review-workflow closure: writes an ``access_review.completed`` audit row
    (tenant DB, append-only) and stamps ``settings.access_review.last_completed_at``
    + ``last_completed_by`` on the control-plane Organization. Re-acknowledging is
    safe — it simply re-stamps with the latest timestamp + reviewer.
    """
    now = datetime.now(UTC)

    org = await control_db.get(Organization, user.organization_id)
    # Serialise with every other settings writer, and refresh whatever snapshot
    # the identity map handed back (`lock_organization`).
    org = await lock_organization(control_db, org)
    # ``settings`` is JSONB; mutate a fresh dict so SQLAlchemy detects the change
    # (in-place JSONB mutation isn't tracked without a Mutable type).
    org_settings = dict(org.settings or {})
    org_settings["access_review"] = {
        "last_completed_at": now.isoformat(),
        "last_completed_by": str(user.id),
    }
    org.settings = org_settings
    # control_db commit is handled by the request-session dependency wrapper.

    # Append-only audit row in the tenant trail. Non-PII: just the reviewer +
    # completion timestamp metadata.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=user.organization_id,
        actor_id=user.id,
        action="access_review.completed",
        entity_type="access_review",
        entity_id=user.organization_id,
        details={"completed_at": now.isoformat()},
    )
    await db.commit()

    return AccessReviewAcknowledgeResponse(
        acknowledged=True,
        last_completed_at=now.isoformat(),
        reviewer_id=str(user.id),
    )
