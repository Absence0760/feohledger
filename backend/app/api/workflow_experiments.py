"""A/B testing for workflow rules — experiment CRUD + lifecycle + results.

Run a controlled experiment comparing two workflow-rule configurations (an **A**
control vs a **B** variant) on the same workflow definition and measure which
performs better on objective, deterministic metrics (time-to-approval, touchless
rate, exception rate, rejection rate).

This file does the SQL, the response shaping, the lifecycle transitions, and the
results-row assembly; the deterministic assignment + metrics math live in the
pure ``services/workflow_experiments``. Assignment at invoice-creation time lives
in ``services/workflow_experiments_runtime`` (called by the workflow engine).

Boundaries:
  * **Never moves money** — an experiment only routes (which config an invoice
    runs under) and measures.
  * **RBAC** — read for managers/CFO (matching the adaptive surface); mutate
    admin-only (editing workflow rules is an admin act, like editing a workflow
    definition). Every mutation is audited.
  * Metrics are deterministic (no LLM, no cloud key) and computed over the
    *recorded* assignments, so the readout is reproducible.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    require_roles,
)
from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.models.user import User
from app.models.workflow import AuditLog, WorkflowDefinition
from app.models.workflow_experiment import WorkflowExperiment
from app.schemas.workflow_experiments import (
    ExperimentCreate,
    ExperimentListResponse,
    ExperimentResponse,
    ExperimentResultsResponse,
    ExperimentUpdate,
)
from app.services.adaptive_workflows import _decimal_days
from app.services.audit_dispatch import dispatch_audit
from app.services.workflow_experiments import (
    PRIMARY_METRICS,
    VARIANT_A,
    VARIANT_B,
    compute_experiment_results,
)
from app.tenant import (
    apply_entity_scope,
    ensure_in_entity_scope,
    get_entity_id,
    get_tenant_db,
)

router = APIRouter(prefix="/experiments", tags=["workflow-experiments"])

_READ_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)
_WRITE_ROLES = (ROLE_ADMIN,)

#: Fields that DEFINE what a recorded assignment means. Once any invoice has
#: been assigned, changing one of these re-labels measurements that were taken
#: under the old value. `name` / `description` / `primary_metric` /
#: `min_sample_per_variant` are readout knobs and stay editable — they change
#: how the same data is presented, not what the data is.
_MEASUREMENT_FIELDS = frozenset({"config_a", "config_b", "split_a_pct"})


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _experiment_response(
    exp: WorkflowExperiment, *, definition_name: str | None
) -> ExperimentResponse:
    return ExperimentResponse(
        id=str(exp.id),
        name=exp.name,
        description=exp.description,
        workflow_definition_id=str(exp.workflow_definition_id),
        workflow_definition_name=definition_name,
        config_a=exp.config_a,
        config_b=exp.config_b,
        split_a_pct=exp.split_a_pct,
        primary_metric=exp.primary_metric,
        min_sample_per_variant=exp.min_sample_per_variant,
        status=exp.status,
        started_at=_iso(exp.started_at),
        ended_at=_iso(exp.ended_at),
        assigned_count=len(exp.assignments or {}),
        entity_id=str(exp.entity_id) if exp.entity_id else None,
        created_at=_iso(exp.created_at),
        updated_at=_iso(exp.updated_at),
    )


async def _definition_names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(WorkflowDefinition.id, WorkflowDefinition.name).where(
                WorkflowDefinition.id.in_(ids)
            )
        )
    ).all()
    return {did: name for did, name in rows}


async def _get_experiment(
    db: AsyncSession, experiment_id: uuid.UUID, *, organization_id: uuid.UUID
) -> WorkflowExperiment:
    exp = (
        await db.execute(
            select(WorkflowExperiment).where(
                WorkflowExperiment.id == experiment_id,
                WorkflowExperiment.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if exp is None:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    return exp


# ---------------------------------------------------------------------------
# GET /api/experiments — list
# ---------------------------------------------------------------------------


@router.get("", response_model=ExperimentListResponse)
async def list_experiments(
    status: str | None = Query(None),
    db: AsyncSession = Depends(get_tenant_db),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    q = select(WorkflowExperiment).where(WorkflowExperiment.organization_id == user.organization_id)
    q = apply_entity_scope(q, WorkflowExperiment, entity_id)
    if status:
        q = q.where(WorkflowExperiment.status == status)
    q = q.order_by(WorkflowExperiment.created_at.desc())
    experiments = list((await db.execute(q)).scalars().all())
    names = await _definition_names(db, {e.workflow_definition_id for e in experiments})
    return ExperimentListResponse(
        experiments=[
            _experiment_response(e, definition_name=names.get(e.workflow_definition_id))
            for e in experiments
        ]
    )


# ---------------------------------------------------------------------------
# POST /api/experiments — create (draft)
# ---------------------------------------------------------------------------


@router.post("", response_model=ExperimentResponse, status_code=201)
async def create_experiment(
    body: ExperimentCreate,
    db: AsyncSession = Depends(get_tenant_db),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
):
    if body.primary_metric not in PRIMARY_METRICS:
        raise HTTPException(
            status_code=422,
            detail=f"primary_metric must be one of {sorted(PRIMARY_METRICS)}.",
        )
    defn = (
        await db.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.id == body.workflow_definition_id,
                WorkflowDefinition.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if defn is None:
        raise HTTPException(status_code=404, detail="Workflow definition not found.")

    exp = WorkflowExperiment(
        organization_id=user.organization_id,
        name=body.name,
        description=body.description,
        workflow_definition_id=body.workflow_definition_id,
        config_a=body.config_a,
        config_b=body.config_b,
        split_a_pct=body.split_a_pct,
        primary_metric=body.primary_metric,
        min_sample_per_variant=body.min_sample_per_variant,
        status="draft",
        assignments={},
        entity_id=entity_id,
    )
    db.add(exp)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=user.organization_id,
        actor_id=user.id,
        action="workflow_experiment.created",
        entity_type="workflow_experiment",
        entity_id=exp.id,
        details={
            "name": exp.name,
            "workflow_definition_id": str(exp.workflow_definition_id),
            "split_a_pct": exp.split_a_pct,
            "primary_metric": exp.primary_metric,
        },
    )
    await db.commit()
    await db.refresh(exp)
    return _experiment_response(exp, definition_name=defn.name)


# ---------------------------------------------------------------------------
# PATCH /api/experiments/{id} — edit (draft only)
# ---------------------------------------------------------------------------


@router.patch("/{experiment_id}", response_model=ExperimentResponse)
async def update_experiment(
    experiment_id: uuid.UUID,
    body: ExperimentUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(
        db, WorkflowExperiment, experiment_id, entity_id, detail="Experiment not found."
    )
    exp = await _get_experiment(db, experiment_id, organization_id=user.organization_id)
    if exp.status != "draft":
        raise HTTPException(
            status_code=409,
            detail="Only draft experiments can be edited.",
        )
    data = body.model_dump(exclude_unset=True)
    # `draft` is not the same thing as "never ran": `stop` returns a running
    # experiment to `draft` while KEEPING its recorded `assignments`, and those
    # invoices are already carrying a variant config frozen onto their workflow
    # snapshot. Rewriting the arms now would pool invoices that ran under the
    # old config with invoices that ran under the new one and label the result
    # "variant B" — a winner called on a config half the sample never saw. The
    # per-invoice frozen snapshot (decisions §13) is exactly the invariant this
    # protects, one level up.
    if exp.assignments and (frozen := sorted(_MEASUREMENT_FIELDS & set(data))):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot change {', '.join(frozen)} — this experiment has already "
                f"assigned {len(exp.assignments)} invoice(s), which are running under "
                "the current arms. Conclude it and start a new experiment instead."
            ),
        )
    if "primary_metric" in data and data["primary_metric"] not in PRIMARY_METRICS:
        raise HTTPException(
            status_code=422,
            detail=f"primary_metric must be one of {sorted(PRIMARY_METRICS)}.",
        )
    for field, value in data.items():
        setattr(exp, field, value)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=user.organization_id,
        actor_id=user.id,
        action="workflow_experiment.updated",
        entity_type="workflow_experiment",
        entity_id=exp.id,
        details={"fields": sorted(data.keys())},
    )
    await db.commit()
    await db.refresh(exp)
    names = await _definition_names(db, {exp.workflow_definition_id})
    return _experiment_response(exp, definition_name=names.get(exp.workflow_definition_id))


# ---------------------------------------------------------------------------
# Lifecycle: start / stop / conclude
# ---------------------------------------------------------------------------


@router.post("/{experiment_id}/start", response_model=ExperimentResponse)
async def start_experiment(
    experiment_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(
        db, WorkflowExperiment, experiment_id, entity_id, detail="Experiment not found."
    )
    exp = await _get_experiment(db, experiment_id, organization_id=user.organization_id)
    if exp.status == "running":
        # Idempotent — already running.
        names = await _definition_names(db, {exp.workflow_definition_id})
        return _experiment_response(exp, definition_name=names.get(exp.workflow_definition_id))
    if exp.status != "draft":
        raise HTTPException(
            status_code=409,
            detail="Only a draft experiment can be started.",
        )
    exp.status = "running"
    exp.started_at = datetime.now(UTC)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=user.organization_id,
        actor_id=user.id,
        action="workflow_experiment.started",
        entity_type="workflow_experiment",
        entity_id=exp.id,
        details={"workflow_definition_id": str(exp.workflow_definition_id)},
    )
    await db.commit()
    await db.refresh(exp)
    names = await _definition_names(db, {exp.workflow_definition_id})
    return _experiment_response(exp, definition_name=names.get(exp.workflow_definition_id))


@router.post("/{experiment_id}/stop", response_model=ExperimentResponse)
async def stop_experiment(
    experiment_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Stop a running experiment without concluding — new invoices stop being
    assigned, but already-assigned in-flight invoices keep their frozen variant
    snapshot. Returns the experiment to ``draft`` so it can be re-tuned/restarted.
    """
    await ensure_in_entity_scope(
        db, WorkflowExperiment, experiment_id, entity_id, detail="Experiment not found."
    )
    exp = await _get_experiment(db, experiment_id, organization_id=user.organization_id)
    if exp.status != "running":
        raise HTTPException(status_code=409, detail="Only a running experiment can be stopped.")
    exp.status = "draft"
    exp.started_at = None
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=user.organization_id,
        actor_id=user.id,
        action="workflow_experiment.stopped",
        entity_type="workflow_experiment",
        entity_id=exp.id,
        details={},
    )
    await db.commit()
    await db.refresh(exp)
    names = await _definition_names(db, {exp.workflow_definition_id})
    return _experiment_response(exp, definition_name=names.get(exp.workflow_definition_id))


@router.post("/{experiment_id}/conclude", response_model=ExperimentResponse)
async def conclude_experiment(
    experiment_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Conclude an experiment — terminal. No new assignments; the results readout
    remains available over the recorded assignments. Idempotent."""
    await ensure_in_entity_scope(
        db, WorkflowExperiment, experiment_id, entity_id, detail="Experiment not found."
    )
    exp = await _get_experiment(db, experiment_id, organization_id=user.organization_id)
    if exp.status == "concluded":
        names = await _definition_names(db, {exp.workflow_definition_id})
        return _experiment_response(exp, definition_name=names.get(exp.workflow_definition_id))
    exp.status = "concluded"
    exp.ended_at = datetime.now(UTC)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=user.organization_id,
        actor_id=user.id,
        action="workflow_experiment.concluded",
        entity_type="workflow_experiment",
        entity_id=exp.id,
        details={"assigned_count": len(exp.assignments or {})},
    )
    await db.commit()
    await db.refresh(exp)
    names = await _definition_names(db, {exp.workflow_definition_id})
    return _experiment_response(exp, definition_name=names.get(exp.workflow_definition_id))


# ---------------------------------------------------------------------------
# DELETE /api/experiments/{id} — draft only
# ---------------------------------------------------------------------------


@router.delete("/{experiment_id}", status_code=204)
async def delete_experiment(
    experiment_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(
        db, WorkflowExperiment, experiment_id, entity_id, detail="Experiment not found."
    )
    exp = await _get_experiment(db, experiment_id, organization_id=user.organization_id)
    if exp.status != "draft":
        raise HTTPException(
            status_code=409,
            detail="Only a draft experiment can be deleted (stop/conclude a "
            "running one to preserve its measurement history).",
        )
    # ...and `stop` is the documented way to leave `running`, which puts the row
    # back in `draft` WITH its assignments intact. So the status check above was
    # bypassed by the very action whose message it cites: stop-then-delete
    # destroyed the measurement history it exists to preserve. `assignments` is
    # the only record of which invoice ran which arm — `GET /results` derives
    # everything from it, the invoices' frozen snapshots outlive the row, and the
    # `invoice.experiment_assigned` audit rows would be left naming an
    # experiment id that no longer resolves.
    if exp.assignments:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete an experiment that has assigned {len(exp.assignments)} "
                "invoice(s) — its assignment record is the only evidence of which "
                "invoice ran which arm. Conclude it instead."
            ),
        )
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=user.organization_id,
        actor_id=user.id,
        action="workflow_experiment.deleted",
        entity_type="workflow_experiment",
        entity_id=exp.id,
        details={"name": exp.name},
    )
    await db.delete(exp)
    await db.commit()


# ---------------------------------------------------------------------------
# GET /api/experiments/{id}/results — per-variant metrics + winner
# ---------------------------------------------------------------------------


def _details_obj(details) -> dict:
    """An ``audit_log.details`` value as a dict, or ``{}`` when it isn't one.

    The column is JSONB with no shape constraint. Every writer in this codebase
    stores an object, so a list / string / number can only arrive by a direct-DB
    write — which is precisely the tampering an experiment readout should
    survive rather than 500 on.
    """
    return details if isinstance(details, dict) else {}


async def _experiment_metric_rows(
    db: AsyncSession, exp: WorkflowExperiment
) -> tuple[list[dict], list[dict]]:
    """Build the per-variant duck-typed metric rows from the recorded
    assignments. Each row carries the invoice's terminal decision, the touchless
    signals, time-to-approval, and whether it raised any exception — the shape
    ``services/workflow_experiments`` consumes."""
    assignments: dict = exp.assignments or {}
    if not assignments:
        return [], []

    invoice_ids: list[uuid.UUID] = []
    for raw_id in assignments:
        try:
            invoice_ids.append(uuid.UUID(raw_id))
        except (ValueError, TypeError):
            continue
    if not invoice_ids:
        return [], []

    # Terminal decision + touchless signals from the audit log: the approval row
    # (human ``invoice.approved`` or system ``invoice.auto_approved``) or the
    # ``invoice.rejected`` row. Pull all decision rows for these invoices.
    decision_rows = (
        await db.execute(
            select(
                AuditLog.entity_id,
                AuditLog.action,
                AuditLog.created_at,
                AuditLog.details,
            ).where(
                AuditLog.entity_type == "invoice",
                AuditLog.entity_id.in_(invoice_ids),
                AuditLog.action.in_(
                    ("invoice.approved", "invoice.auto_approved", "invoice.rejected")
                ),
            )
        )
    ).all()

    # ready_for_review clock-starts for the time-to-approval leg.
    start_rows = (
        await db.execute(
            select(AuditLog.entity_id, AuditLog.created_at, AuditLog.details).where(
                AuditLog.entity_type == "invoice",
                AuditLog.entity_id.in_(invoice_ids),
            )
        )
    ).all()
    rfr_starts: dict[uuid.UUID, datetime] = {}
    for inv_id, created_at, details in start_rows:
        # `audit_log.details` is JSONB with no object-shape constraint, so a
        # non-object value (array / scalar) is reachable — by exactly the
        # direct-DB write this readout would otherwise be blind to. `.get` on it
        # raised `AttributeError` and took the WHOLE experiment readout down with
        # a 500, losing every other invoice's evidence over one bad row. Same
        # posture as `approval_signature.check_approval_row`: treat it as
        # carrying nothing, keep the readout.
        if _details_obj(details).get("new_status") == "ready_for_review":
            cur = rfr_starts.get(inv_id)
            if cur is None or (created_at is not None and created_at < cur):
                rfr_starts[inv_id] = created_at

    # Latest decision per invoice (an invoice may have multiple decision rows
    # over re-review; the most recent is the terminal one).
    decisions: dict[uuid.UUID, dict] = {}
    for inv_id, action, created_at, details in decision_rows:
        prev = decisions.get(inv_id)
        if prev is None or (created_at is not None and created_at >= prev["created_at"]):
            decisions[inv_id] = {
                "action": action,
                "created_at": created_at,
                # A non-object `details` records no corrections — see above. It
                # is NOT read as "corrections present": the touchless leg asks
                # whether a human changed a field, and a malformed blob is not
                # evidence that one did.
                "details": _details_obj(details),
            }

    # Invoice base rows (created_at fallback for the clock-start) + exception
    # presence.
    inv_rows = (
        await db.execute(select(Invoice.id, Invoice.created_at).where(Invoice.id.in_(invoice_ids)))
    ).all()
    inv_created: dict[uuid.UUID, datetime] = {iid: c for iid, c in inv_rows}

    exc_rows = (
        await db.execute(
            select(APException.invoice_id).where(APException.invoice_id.in_(invoice_ids)).distinct()
        )
    ).all()
    has_exception = {iid for (iid,) in exc_rows if iid is not None}

    rows_a: list[dict] = []
    rows_b: list[dict] = []
    for raw_id, variant in assignments.items():
        try:
            inv_id = uuid.UUID(raw_id)
        except (ValueError, TypeError):
            continue
        dec = decisions.get(inv_id)
        decision: str | None = None
        auto_approved = False
        unmodified = False
        ttd: Decimal | None = None
        if dec is not None:
            action = dec["action"]
            if action in ("invoice.approved", "invoice.auto_approved"):
                decision = "approved"
                auto_approved = action == "invoice.auto_approved"
                unmodified = not dec["details"].get("changes")
                clock_start = rfr_starts.get(inv_id) or inv_created.get(inv_id)
                if clock_start is not None and dec["created_at"] is not None:
                    ttd = max(Decimal("0"), _decimal_days(dec["created_at"] - clock_start))
            elif action == "invoice.rejected":
                decision = "rejected"
        row = {
            "decision": decision,
            "auto_approved": auto_approved,
            "unmodified": unmodified,
            "time_to_approval_days": ttd,
            "had_exception": inv_id in has_exception,
        }
        if variant == VARIANT_A:
            rows_a.append(row)
        elif variant == VARIANT_B:
            rows_b.append(row)
    return rows_a, rows_b


def _variant_dict(m) -> dict:
    return {
        "variant": m.variant,
        "assigned_count": m.assigned_count,
        "completed_count": m.completed_count,
        "approved_count": m.approved_count,
        "rejected_count": m.rejected_count,
        "touchless_count": m.touchless_count,
        "exception_count": m.exception_count,
        "median_time_to_approval_days": str(m.median_time_to_approval_days),
        "avg_time_to_approval_days": str(m.avg_time_to_approval_days),
        "touchless_rate_pct": str(m.touchless_rate_pct),
        "exception_rate_pct": str(m.exception_rate_pct),
        "rejection_rate_pct": str(m.rejection_rate_pct),
    }


@router.get("/{experiment_id}/results", response_model=ExperimentResultsResponse)
async def experiment_results(
    experiment_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(
        db, WorkflowExperiment, experiment_id, entity_id, detail="Experiment not found."
    )
    exp = await _get_experiment(db, experiment_id, organization_id=user.organization_id)
    rows_a, rows_b = await _experiment_metric_rows(db, exp)
    results = compute_experiment_results(
        rows_a,
        rows_b,
        primary_metric=exp.primary_metric,
        min_sample_per_variant=exp.min_sample_per_variant,
    )
    return ExperimentResultsResponse(
        experiment_id=str(exp.id),
        experiment_name=exp.name,
        status=exp.status,
        primary_metric=results.primary_metric,
        min_sample_per_variant=results.min_sample_per_variant,
        enough_data=results.enough_data,
        winner=results.winner,
        rationale=results.rationale,
        notes=results.notes,
        variant_a=_variant_dict(results.variant_a),
        variant_b=_variant_dict(results.variant_b),
        generated_at=datetime.now(UTC).isoformat(),
    )
