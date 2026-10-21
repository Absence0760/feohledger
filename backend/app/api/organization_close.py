"""Closing a workspace — an administrator deletes their own unused workspace.

Mounted at `/api/organization/close`. Two routes, both admin-only:

* `GET` — the preflight: which blockers (if any) stop a self-service closure,
  so the page can say "contact support" before it asks for a second factor.
* `POST` — the closure: a typed confirmation of the workspace's slug, then a
  second-factor step-up (`api/auth.require_sensitive_step_up`, operation
  `workspace_close` — a code or a passkey, never the password), then
  `services/workspace_closure.close_workspace`, which re-checks the blockers
  under locks, writes the audit record that outlives the tenant, and deletes it.

Why it exists, and why it refuses so much: `docs/decisions.md` §294. A
refusal names its blockers by stable code (`params.blockers`); every refusal of
a guard is audited into the tenant's own log (which still exists, since
nothing was deleted). The success path's record is the closure service's
(`organization.closing` + the control-plane `workspace_closures` row), because
the tenant log does not survive it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_sensitive_step_up
from app.api.deps import ROLE_ADMIN, require_roles
from app.api.refusals import (
    TOKEN_TENANT_MISMATCH_DETAIL,
    WORKSPACE_CLOSE_ACTOR_NOT_ADMIN_DETAIL,
    WORKSPACE_CLOSE_AUDIT_UNAVAILABLE_DETAIL,
    WORKSPACE_CLOSE_CONFIRMATION_MISMATCH_DETAIL,
    WORKSPACE_CLOSE_IN_PROGRESS_DETAIL,
    WORKSPACE_CLOSE_INCOMPLETE_DETAIL,
    workspace_close_refused_detail,
)
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.schemas.auth import STEP_UP_OPERATION_WORKSPACE_CLOSE, MFAStepUpRequest
from app.services import workspace_closure
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.rate_limit import resolve_client_ip
from app.tenant import get_tenant, get_tenant_db

router = APIRouter(prefix="/organization/close", tags=["organization"])


def _require_own_workspace(user: User, org: Organization) -> None:
    """The caller's account must belong to the workspace being closed.

    `tenant.get_tenant` already refuses a token whose `org` claim names another
    workspace, but it skips a token with no `org` claim at all; for a route that
    drops a database, the account's own `organization_id` is checked here too.
    A 403 coded like `get_tenant`'s, so the SPA ends that session the same way.
    """
    if user.organization_id != org.id:
        raise HTTPException(status_code=403, detail=TOKEN_TENANT_MISMATCH_DETAIL)


class ClosurePreflightResponse(BaseModel):
    #: True when nothing blocks a self-service closure.
    closable: bool
    #: Stable codes (`services/workspace_closure.BLOCKERS`), empty when closable.
    blockers: list[str]
    #: What the confirmation must match, so the page shows the same string the
    #: server compares against.
    slug: str
    #: What the closure would delete that no guard stopped, as non-zero counts
    #: by class (`services/workspace_closure.INVENTORY_COUNTS` codes plus
    #: `inactive_users`), so the confirmation is an informed one.
    will_delete: dict[str, int]


class CloseWorkspaceRequest(BaseModel):
    #: The workspace's slug, typed by the administrator.
    confirm_slug: str = Field(..., min_length=1, max_length=100)
    #: A second-factor proof (`code` or `assertion`; a password is ignored).
    step_up: MFAStepUpRequest | None = None


class CloseWorkspaceResponse(BaseModel):
    closed: bool


@router.get("", response_model=ClosurePreflightResponse)
async def closure_preflight(
    org: Organization = Depends(get_tenant),
    control_db: AsyncSession = Depends(get_control_db),
    tenant_db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """Which blockers stop this workspace being closed here, and what a closure
    would delete. Reads only."""
    _require_own_workspace(user, org)
    result = await workspace_closure.preflight(control_db, tenant_db, org, user.id)
    return ClosurePreflightResponse(
        closable=not result.blockers,
        blockers=result.blockers,
        slug=org.slug,
        will_delete=result.will_delete,
    )


@router.post("", response_model=CloseWorkspaceResponse)
async def close_workspace(
    body: CloseWorkspaceRequest,
    request: Request,
    org: Organization = Depends(get_tenant),
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    host: Annotated[str | None, Header()] = None,
):
    """Delete this workspace, its users and its data, if nothing must be kept.

    Order: the confirmation (free), then the step-up (throttled and audited by
    `require_sensitive_step_up` itself), then the closure. A refusal at any
    point leaves everything as it was.
    """
    _require_own_workspace(user, org)
    if body.confirm_slug.strip() != org.slug:
        raise HTTPException(status_code=400, detail=WORKSPACE_CLOSE_CONFIRMATION_MISMATCH_DETAIL)

    proof = await require_sensitive_step_up(
        user,
        body.step_up,
        db=control_db,
        operation=STEP_UP_OPERATION_WORKSPACE_CLOSE,
        host=host,
    )
    # End this request's control-plane transaction before the closure deletes
    # the rows it read (the org and this very user): nothing is pending, and
    # nothing should be left holding a snapshot across the deletion.
    await control_db.commit()

    ip = resolve_client_ip(request)

    async def _audit(action: str, details: dict) -> None:
        # Into the tenant's own log, which still exists on every path that
        # calls this (a refusal deletes nothing). Best-effort, like every
        # refusal row: an audit outage must not turn a refusal into a 500.
        await dispatch_auth_audit(
            organization_id=org.id,
            actor_id=user.id,
            action=action,
            entity_type="organization",
            entity_id=org.id,
            details={**details, "ip": ip},
        )

    try:
        await workspace_closure.close_workspace(
            org_id=org.id, actor_id=user.id, step_up_proof=proof, ip=ip
        )
    except workspace_closure.ClosureRefused as exc:
        await _audit("organization.close.refused", {"blockers": exc.blockers})
        raise HTTPException(
            status_code=409, detail=workspace_close_refused_detail(exc.blockers)
        ) from exc
    except workspace_closure.ClosureActorInvalid as exc:
        await _audit("organization.close.refused", {"reason": "actor_not_admin"})
        raise HTTPException(status_code=403, detail=WORKSPACE_CLOSE_ACTOR_NOT_ADMIN_DETAIL) from exc
    except workspace_closure.ClosureInProgress as exc:
        await _audit("organization.close.in_progress", {})
        raise HTTPException(status_code=409, detail=WORKSPACE_CLOSE_IN_PROGRESS_DETAIL) from exc
    except workspace_closure.ClosureAuditUnavailable as exc:
        # Deliberately NOT audited into the tenant: this refusal means the
        # audit path itself just failed (the closing row, the flush or the
        # seal), so a second write there is the likeliest thing to fail too.
        # The service has logged it with ids only, and nothing was deleted.
        raise HTTPException(
            status_code=503, detail=WORKSPACE_CLOSE_AUDIT_UNAVAILABLE_DETAIL
        ) from exc
    except workspace_closure.ClosureIncomplete as exc:
        # The tenant may already be gone, so the record of this is the closure
        # row's `last_failure` plus the service's id-only ERROR line.
        raise HTTPException(status_code=503, detail=WORKSPACE_CLOSE_INCOMPLETE_DETAIL) from exc
    return CloseWorkspaceResponse(closed=True)
