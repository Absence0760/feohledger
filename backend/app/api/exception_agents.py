"""Autonomous exception-agent endpoints — run an agent, read the decision log."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, ROLE_AP_MANAGER, require_roles
from app.api.pagination import PaginationParams, paginated, pagination_params
from app.models.agent_decision import AgentDecision
from app.models.exception import Exception as APException
from app.models.organization import Organization
from app.models.user import User
from app.schemas.exception_agent import (
    AgentDecisionListResponse,
    AgentResolveResponse,
    AgentStatsResponse,
)
from app.services.exception_agents import ExceptionNotActionable, run_agent
from app.tenant import (
    apply_entity_scope,
    ensure_in_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
)

router = APIRouter(prefix="/exceptions", tags=["exception-agents"])


def _decision_dict(d: AgentDecision) -> dict:
    return {
        "id": str(d.id),
        "exception_id": str(d.exception_id),
        "invoice_id": str(d.invoice_id),
        "exception_type": d.exception_type,
        "action_taken": d.action_taken,
        "confidence": float(d.confidence),  # display only — stored exact
        "rationale": d.rationale,
        "changes": d.changes,
        "autonomy_level": d.autonomy_level,
        "agent_type": d.agent_type,
        "created_at": d.created_at.isoformat() if d.created_at else "",
    }


@router.get("/agent-decisions", response_model=AgentDecisionListResponse)
async def list_agent_decisions(
    exception_type: str | None = None,
    action_taken: str | None = None,
    pagination: PaginationParams = Depends(pagination_params),
    db: AsyncSession = Depends(get_tenant_db),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
):
    q = select(AgentDecision)
    q = apply_entity_scope(q, AgentDecision, entity_id)
    if exception_type:
        q = q.where(AgentDecision.exception_type == exception_type)
    if action_taken:
        q = q.where(AgentDecision.action_taken == action_taken)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    q = (
        q.order_by(AgentDecision.created_at.desc(), AgentDecision.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(q)).scalars().all()
    return paginated([_decision_dict(d) for d in rows], int(total), pagination)


@router.get("/agent-stats", response_model=AgentStatsResponse)
async def agent_stats(
    db: AsyncSession = Depends(get_tenant_db),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
):
    q = select(AgentDecision.action_taken, func.count(AgentDecision.id)).group_by(
        AgentDecision.action_taken
    )
    q = apply_entity_scope(q, AgentDecision, entity_id)
    rows = (await db.execute(q)).all()
    counts = {a: c for a, c in rows}
    total = sum(counts.values())
    auto = counts.get("auto_resolved", 0)
    esc = counts.get("escalated", 0)
    return {
        "total_decisions": total,
        "auto_resolved": auto,
        "escalated": esc,
        "no_action": counts.get("no_action", 0),
        "resolution_rate": round(auto / total, 4) if total else 0.0,
        "escalation_rate": round(esc / total, 4) if total else 0.0,
        # Accuracy needs a human-overturn signal (was an auto-resolution later
        # reversed?). Not tracked in this slice — placeholder, deferred.
        "accuracy": None,
    }


@router.post("/{exception_id}/agent-resolve", response_model=AgentResolveResponse)
async def agent_resolve(
    exception_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),  # for .settings (autonomy_level)
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(
        db, APException, exception_id, entity_id, detail="Exception not found"
    )
    exc = (
        await db.execute(select(APException).where(APException.id == exception_id))
    ).scalar_one_or_none()
    if not exc:
        raise HTTPException(status_code=404, detail="Exception not found")
    if exc.status not in ("open", "escalated"):
        raise HTTPException(status_code=409, detail=f"Cannot run agent from '{exc.status}' status")
    if exc.invoice_id is None:
        # Invoice-less exceptions (e.g. a Positive Pay not_on_file fraud return)
        # have no invoice for an agent to act on — human triage only.
        raise HTTPException(
            status_code=422,
            detail="This exception has no associated invoice and can't be auto-resolved by an agent.",  # noqa: E501
        )

    try:
        result = await run_agent(
            db,
            exception=exc,
            actor_id=user.id,
            org_settings=org.settings or {},
            # The triggering user's REAL roles — so a CFO-gated invoice resolved
            # by a CFO isn't blocked by a hardcoded ap_manager set, and the audit
            # trail's authoriser role matches the actor_id it records.
            actor_roles={r.name for r in user.roles},
        )
    except ExceptionNotActionable as e:
        # Lost a race with a concurrent agent-resolve — the row lock found the
        # exception already moved on. Same 409 the pre-lock check returns.
        raise HTTPException(
            status_code=409, detail=f"Cannot run agent from '{e.status}' status"
        ) from e
    return {
        "exception": {"id": str(exc.id), "status": exc.status},
        "decision": _decision_dict(result.decision),
    }
