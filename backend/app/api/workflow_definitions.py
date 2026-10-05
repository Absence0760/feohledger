"""CRUD endpoints for workflow definitions."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, get_current_user, get_org_id, require_roles
from app.api.pagination import PaginationParams, pagination_params
from app.models.user import User
from app.models.workflow import WorkflowDefinition, WorkflowInstance, WorkflowVersion
from app.schemas.workflow import (
    CreateFromTemplateRequest,
    CreateVersionRequest,
    ImportWorkflowRequest,
    SimulateRequest,
    SimulationResult,
    WorkflowDefinitionCreate,
    WorkflowDefinitionListResponse,
    WorkflowDefinitionResponse,
    WorkflowDefinitionUpdate,
    WorkflowDiff,
    WorkflowExport,
    WorkflowTemplate,
    WorkflowTemplateListResponse,
    WorkflowVersionListResponse,
)
from app.schemas.workflow import (
    WorkflowVersion as WorkflowVersionSchema,
)
from app.services.audit_dispatch import dispatch_audit
from app.services.workflow_engine import DEFAULT_STEPS_CONFIG
from app.tenant import (
    apply_entity_scope,
    ensure_in_entity_scope,
    get_entity_id,
    get_tenant_db,
    get_write_entity_id,
    resolve_default_entity_id,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _get_workflow_or_404(
    db: AsyncSession, workflow_id: uuid.UUID, org_id: uuid.UUID
) -> WorkflowDefinition:
    result = await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.id == workflow_id,
            WorkflowDefinition.organization_id == org_id,
        )
    )
    defn = result.scalar_one_or_none()
    if not defn:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return defn


def _entity_scope_predicate(entity_id: uuid.UUID | None):
    """NULL-safe equality on ``WorkflowDefinition.entity_id``.

    A definition either belongs to one subsidiary (``entity_id`` set) or is the
    shared / org-wide fallback (``entity_id IS NULL``) — the two buckets
    ``get_or_create_workflow_definition`` resolves in precedence order. SQL
    treats ``NULL = NULL`` as unknown, so the shared bucket needs ``IS NULL``
    rather than an equality test.
    """
    if entity_id is None:
        return WorkflowDefinition.entity_id.is_(None)
    return WorkflowDefinition.entity_id == entity_id


async def _active_definition_count(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    exclude_id: uuid.UUID | None = None,
) -> int:
    """Count the ACTIVE definitions in one entity scope, excluding ``exclude_id``."""
    query = (
        select(func.count())
        .select_from(WorkflowDefinition)
        .where(
            WorkflowDefinition.organization_id == org_id,
            WorkflowDefinition.is_active.is_(True),
            _entity_scope_predicate(entity_id),
        )
    )
    if exclude_id is not None:
        query = query.where(WorkflowDefinition.id != exclude_id)
    return int((await db.execute(query)).scalar() or 0)


async def _next_version_number(db: AsyncSession, definition_id: uuid.UUID) -> int:
    current = (
        await db.execute(
            select(func.max(WorkflowVersion.version_number)).where(
                WorkflowVersion.definition_id == definition_id
            )
        )
    ).scalar()
    return int(current or 0) + 1


async def _snapshot_version(
    db: AsyncSession,
    *,
    defn: WorkflowDefinition,
    org_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    note: str | None,
) -> WorkflowVersion:
    """Persist the definition's CURRENT steps_config as a new version row."""
    version = WorkflowVersion(
        organization_id=org_id,
        definition_id=defn.id,
        version_number=await _next_version_number(db, defn.id),
        note=note,
        steps_config=defn.steps_config,
        created_by=actor_id,
    )
    db.add(version)
    await db.flush()
    await db.refresh(version)
    return version


def _diff_steps_config(before: dict, after: dict) -> WorkflowDiff:
    """Compare two steps_config envelopes by step ``number`` + field.

    Produces an added / removed / changed change list. ``from_version`` /
    ``to_version`` are filled in by the caller.
    """
    before_steps = {s.get("number"): s for s in (before or {}).get("steps", [])}
    after_steps = {s.get("number"): s for s in (after or {}).get("steps", [])}
    changes: list[dict] = []

    for num in sorted(set(before_steps) | set(after_steps), key=lambda n: (n is None, n)):
        b = before_steps.get(num)
        a = after_steps.get(num)
        if b is None and a is not None:
            changes.append(
                {
                    "kind": "added",
                    "step_number": num,
                    "field": None,
                    "before": None,
                    "after": a,
                    "summary": f"Added step {num}: {a.get('name', a.get('type', '?'))}",
                }
            )
            continue
        if a is None and b is not None:
            changes.append(
                {
                    "kind": "removed",
                    "step_number": num,
                    "field": None,
                    "before": b,
                    "after": None,
                    "summary": f"Removed step {num}: {b.get('name', b.get('type', '?'))}",
                }
            )
            continue
        # both present — compare field by field
        for field in sorted(set(b) | set(a)):
            bv = b.get(field)
            av = a.get(field)
            if bv != av:
                changes.append(
                    {
                        "kind": "changed",
                        "step_number": num,
                        "field": field,
                        "before": bv,
                        "after": av,
                        "summary": f"Step {num} {field}: {bv!r} → {av!r}",
                    }
                )

    return WorkflowDiff(from_version="", to_version="", changes=changes)


@router.get("/active/steps")
async def get_active_steps(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Return which steps are enabled in the active workflow.

    Passes the entity context from ``X-Entity-ID`` so that multi-entity
    tenants get the entity-scoped (or shared org-wide) definition rather
    than always triggering the org-wide auto-create fallback.
    """
    from app.services.workflow_engine import get_or_create_workflow_definition

    defn = await get_or_create_workflow_definition(db, org_id, entity_id)
    steps = defn.steps_config.get("steps", [])
    result: dict = {}
    for step in steps:
        step_type = step["type"]
        result[step_type] = step.get("enabled", True)
        if step_type == "approval" and step.get("enabled", True):
            cfg = step.get("config", {})
            result["approval_config"] = {
                "approver_strategy": cfg.get("approver_strategy", "manual"),
                "approver_ids": cfg.get("approver_ids", []),
            }
    return result


@router.get("", response_model=WorkflowDefinitionListResponse)
async def list_workflows(
    pagination: PaginationParams = Depends(pagination_params),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    org_id: uuid.UUID = Depends(get_org_id),
    # Definitions are a genuine per-entity concept here (uq_workflow_definitions_one_default
    # enforces one default per (org, entity)), so the list follows the same per-entity
    # resolution as get_active_steps above rather than staying org-wide like GLAccount.
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    base = select(WorkflowDefinition).where(WorkflowDefinition.organization_id == org_id)
    # include_shared=True: an org-wide definition (entity_id IS NULL) is the
    # documented fallback `get_or_create_workflow_definition` resolves for an
    # entity with no definition of its own (see that function's docstring —
    # WorkflowDefinition uses NULL-as-shared the same way GLAccount does, not
    # the "backfilled, never meaningfully NULL" case apply_entity_scope's
    # general docstring describes for most other tables). Scoping this list
    # with a strict equality check instead would both (a) hide the effective
    # org-wide default from an entity that's actually governed by it, and (b)
    # make the zero-row auto-create below fire again for every entity's first
    # visit, minting a second entity_id=NULL row and violating
    # uq_workflow_definitions_one_default.
    base = apply_entity_scope(base, WorkflowDefinition, entity_id, include_shared=True)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0

    # Auto-create the default workflow if the org has none at all (own-entity
    # or shared) — independent of which page was requested, so a stray
    # `?page=2` can't trigger a second default. After creation the first page
    # holds exactly that row.
    if total == 0:
        # Same entity stamp as every other creation path (create_workflow,
        # workflow_engine's lazy fallback, tenant_provisioning). A NULL here put
        # this row in the SHARED bucket while `POST /api/workflows` put its rows
        # in the caller's entity bucket, so activating a new workflow could not
        # deactivate this one and the tenant ended up with two active
        # definitions. Migration 0029 backfilled every existing tenant's
        # definitions onto the default entity; NULL is a bucket no tenant's real
        # rows occupy.
        default = WorkflowDefinition(
            name="Default Workflow",
            description="Standard invoice processing: extract, review, and send to ERP.",
            steps_config=DEFAULT_STEPS_CONFIG,
            is_active=True,
            is_default=True,
            organization_id=org_id,
            entity_id=entity_id or await resolve_default_entity_id(db),
        )
        db.add(default)
        await db.flush()
        await db.refresh(default)
        total = 1

    result = await db.execute(
        base.order_by(
            WorkflowDefinition.is_default.desc(),
            WorkflowDefinition.created_at,
            WorkflowDefinition.id.desc(),
        )
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    definitions = result.scalars().all()

    return WorkflowDefinitionListResponse(
        items=[WorkflowDefinitionResponse.from_db(d) for d in definitions],
        total=int(total),
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("", response_model=WorkflowDefinitionResponse, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    body: WorkflowDefinitionCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
    write_entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    # mode="json" — a step's config can carry Decimal money fields
    # (ApprovalStepConfig.auto_approve_below, ApprovalLevelConfig.max_amount,
    # …) since the workflow-schema discriminator fix; the default python-mode
    # model_dump() leaves those as Decimal objects, which the JSONB column's
    # json.dumps chokes on. json mode serializes them as exact strings,
    # matching what `_to_decimal` (workflow_engine.py) already coerces back.
    steps_config = {"steps": [s.model_dump(mode="json") for s in body.steps]}
    # New workflows start inactive — user must explicitly activate
    # Stamp the caller's entity, like every other entity-scoped create. This
    # left `entity_id` NULL, which put every UI-created definition in the
    # SHARED bucket — a bucket no migrated tenant's rows actually occupy,
    # because migration 0029 backfilled `workflow_definitions.entity_id` to the
    # default entity. Harmless while peer-deactivation was org-wide; once that
    # was correctly scoped to the definition's own entity, a newly activated
    # workflow stopped deactivating the seeded default and the tenant had two
    # active definitions.
    defn = WorkflowDefinition(
        name=body.name,
        description=body.description,
        steps_config=steps_config,
        is_active=False,
        is_default=False,
        organization_id=org_id,
        entity_id=write_entity_id,
    )
    db.add(defn)
    await db.flush()
    await db.refresh(defn)

    # A workflow definition IS the approval routing rule set — who signs off on
    # what, at which thresholds. Creating one was the single mutator in this
    # module that left no trace, while its siblings (`from-template`, `import`,
    # every version op, `delete`) all audit; an auditor reconstructing why an
    # invoice took the route it did could see the definition being edited,
    # restored and deleted, but never brought into existence. PII-free: the
    # definition's own name and shape, no step contents.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="workflow.created",
        entity_type="workflow_definition",
        entity_id=defn.id,
        details={
            "name": defn.name,
            "step_count": len(body.steps),
            # `get_write_entity_id` never returns NULL — it resolves the
            # tenant default, or 500s if the tenant has none — so there is no
            # unscoped case to encode here.
            "entity_id": str(write_entity_id),
        },
    )
    await db.commit()
    await db.refresh(defn)
    return WorkflowDefinitionResponse.from_db(defn)


# ---------------------------------------------------------------------------
# templates (static path — registered before /{workflow_id})
# ---------------------------------------------------------------------------


@router.get("/templates", response_model=WorkflowTemplateListResponse)
async def list_workflow_templates(
    user: User = Depends(get_current_user),
):
    """Return the pre-built no-code workflow templates."""
    from app.services.workflow_templates import list_templates

    return WorkflowTemplateListResponse(items=[WorkflowTemplate(**t) for t in list_templates()])


@router.post(
    "/from-template",
    response_model=WorkflowDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow_from_template(
    body: CreateFromTemplateRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Clone a template into a fresh, inactive workflow definition."""
    from app.services.workflow_templates import get_template

    template = get_template(body.template_key)
    if not template:
        raise HTTPException(status_code=404, detail="Unknown template")

    defn = WorkflowDefinition(
        name=body.name,
        description=template.get("description"),
        steps_config=template["steps_config"],
        is_active=False,
        is_default=False,
        organization_id=org_id,
    )
    db.add(defn)
    await db.flush()
    await db.refresh(defn)

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="workflow.created_from_template",
        entity_type="workflow_definition",
        entity_id=defn.id,
        details={"template_key": body.template_key, "name": body.name},
    )
    await db.commit()
    await db.refresh(defn)
    return WorkflowDefinitionResponse.from_db(defn)


# ---------------------------------------------------------------------------
# import (static path — registered before /{workflow_id})
# ---------------------------------------------------------------------------


@router.post(
    "/import",
    response_model=WorkflowDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_workflow(
    body: ImportWorkflowRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Create an inactive workflow from an exported definition.

    Validates the builder step config (Worker A's validator) before persisting
    so a malformed import is rejected with a clear 422.
    """
    from app.services.workflow_builder import validate_builder_steps

    export = body.definition
    steps = (export.steps_config or {}).get("steps")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(status_code=422, detail="Definition has no steps")

    errors = validate_builder_steps(steps)
    if errors:
        raise HTTPException(
            status_code=422, detail={"message": "Invalid workflow", "errors": errors}
        )

    defn = WorkflowDefinition(
        name=body.name or export.name,
        description=export.description,
        steps_config={"steps": steps},
        is_active=False,
        is_default=False,
        organization_id=org_id,
    )
    db.add(defn)
    await db.flush()
    await db.refresh(defn)

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="workflow.imported",
        entity_type="workflow_definition",
        entity_id=defn.id,
        details={"name": defn.name, "schema_version": export.schema_version},
    )
    await db.commit()
    await db.refresh(defn)
    return WorkflowDefinitionResponse.from_db(defn)


@router.get("/{workflow_id}", response_model=WorkflowDefinitionResponse)
async def get_workflow(
    workflow_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(
        db,
        WorkflowDefinition,
        workflow_id,
        entity_id,
        detail="Workflow not found",
        include_shared=True,
    )
    result = await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.id == workflow_id,
            WorkflowDefinition.organization_id == org_id,
        )
    )
    defn = result.scalar_one_or_none()
    if not defn:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowDefinitionResponse.from_db(defn)


@router.patch("/{workflow_id}", response_model=WorkflowDefinitionResponse)
async def update_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowDefinitionUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(
        db,
        WorkflowDefinition,
        workflow_id,
        entity_id,
        detail="Workflow not found",
        include_shared=True,
    )
    result = await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.id == workflow_id,
            WorkflowDefinition.organization_id == org_id,
        )
    )
    defn = result.scalar_one_or_none()
    if not defn:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if body.name is not None:
        defn.name = body.name
    if body.description is not None:
        defn.description = body.description
    if body.is_active is not None:
        if body.is_active and not defn.is_active:
            # Enforce the one-active-workflow invariant PER ENTITY SCOPE: when
            # this workflow flips inactive → active, deactivate any peer that's
            # currently active in the SAME scope (its own subsidiary, or the
            # shared entity_id IS NULL bucket). Org-wide deactivation would take
            # a sibling subsidiary's definition — and the shared fallback — down
            # with it, defeating the per-entity resolution
            # `get_or_create_workflow_definition` performs (multi-entity Phase 3,
            # docs/multi-entity.md).
            await db.execute(
                sql_update(WorkflowDefinition)
                .where(
                    WorkflowDefinition.organization_id == org_id,
                    WorkflowDefinition.id != workflow_id,
                    _entity_scope_predicate(defn.entity_id),
                )
                .values(is_active=False)
            )
        elif not body.is_active and defn.is_active:
            # Refuse a deactivation that would leave this scope with NO active
            # definition. `get_or_create_workflow_definition` would then lazily
            # mint an "Invoice Processing" stub with is_default=True — which
            # collides with any existing shared default under
            # `uq_workflow_definitions_one_default` (migration 0050) and 500s
            # invoice create/upload. Mirrors the default/active/in-flight guards
            # `delete_workflow` already applies.
            own_scope = await _active_definition_count(
                db, org_id=org_id, entity_id=defn.entity_id, exclude_id=workflow_id
            )
            shared_fallback = 0
            if defn.entity_id is not None:
                # An entity with no definition of its own legitimately falls back
                # to a shared (entity_id IS NULL) one, so that still resolves.
                shared_fallback = await _active_definition_count(
                    db, org_id=org_id, entity_id=None, exclude_id=workflow_id
                )
            if own_scope + shared_fallback == 0:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Cannot deactivate the last active workflow — new invoices "
                        "would have no definition to snapshot. Activate another "
                        "workflow first."
                    ),
                )
        defn.is_active = body.is_active
    if body.steps is not None:
        # mode="json" — see the identical comment on the create-workflow path
        # above; a step's config can carry Decimal money fields that must be
        # written to the JSONB column as exact strings, not Decimal objects.
        new_steps_config = {"steps": [s.model_dump(mode="json") for s in body.steps]}
        # Auto-versioning: snapshot the PRIOR steps_config into history before
        # overwriting it, but only when the steps actually change (so a no-op
        # PATCH or a name-only edit doesn't pile up empty versions).
        if new_steps_config != defn.steps_config:
            await _snapshot_version(
                db,
                defn=defn,
                org_id=org_id,
                actor_id=user.id,
                note="Auto-saved before edit",
            )
            await dispatch_audit(
                db,
                correlation_id=uuid.uuid4(),
                organization_id=org_id,
                actor_id=user.id,
                action="workflow.version_snapshot",
                entity_type="workflow_definition",
                entity_id=defn.id,
                details={"reason": "auto_save_before_edit"},
            )
        defn.steps_config = new_steps_config

    await db.flush()
    await db.refresh(defn)
    return WorkflowDefinitionResponse.from_db(defn)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


@router.get("/{workflow_id}/versions", response_model=WorkflowVersionListResponse)
async def list_versions(
    workflow_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(
        db,
        WorkflowDefinition,
        workflow_id,
        entity_id,
        detail="Workflow not found",
        include_shared=True,
    )
    await _get_workflow_or_404(db, workflow_id, org_id)
    result = await db.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.definition_id == workflow_id)
        .order_by(WorkflowVersion.version_number.desc())
    )
    versions = result.scalars().all()
    return WorkflowVersionListResponse(items=[WorkflowVersionSchema.from_db(v) for v in versions])


@router.post(
    "/{workflow_id}/versions",
    response_model=WorkflowVersionSchema,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    workflow_id: uuid.UUID,
    body: CreateVersionRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Manually snapshot the current steps_config as a named version."""
    await ensure_in_entity_scope(
        db,
        WorkflowDefinition,
        workflow_id,
        entity_id,
        detail="Workflow not found",
        include_shared=True,
    )
    defn = await _get_workflow_or_404(db, workflow_id, org_id)
    version = await _snapshot_version(
        db, defn=defn, org_id=org_id, actor_id=user.id, note=body.note
    )
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="workflow.version_created",
        entity_type="workflow_definition",
        entity_id=defn.id,
        details={"version_number": version.version_number},
    )
    await db.commit()
    await db.refresh(version)
    return WorkflowVersionSchema.from_db(version)


@router.post("/{workflow_id}/restore/{version_id}", response_model=WorkflowDefinitionResponse)
async def restore_version(
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Restore a definition's steps from a prior version.

    The current state is first snapshotted as a new version (so a restore is
    itself undoable), then the chosen version's steps are applied.
    """
    await ensure_in_entity_scope(
        db,
        WorkflowDefinition,
        workflow_id,
        entity_id,
        detail="Workflow not found",
        include_shared=True,
    )
    defn = await _get_workflow_or_404(db, workflow_id, org_id)
    version = (
        await db.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.id == version_id,
                WorkflowVersion.definition_id == workflow_id,
            )
        )
    ).scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")

    # Snapshot current first so the restore is reversible.
    await _snapshot_version(
        db,
        defn=defn,
        org_id=org_id,
        actor_id=user.id,
        note=f"Auto-saved before restoring version {version.version_number}",
    )
    defn.steps_config = version.steps_config

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="workflow.version_restored",
        entity_type="workflow_definition",
        entity_id=defn.id,
        details={"restored_version_number": version.version_number},
    )
    await db.commit()
    await db.refresh(defn)
    return WorkflowDefinitionResponse.from_db(defn)


@router.get("/{workflow_id}/versions/diff", response_model=WorkflowDiff)
async def diff_versions(
    workflow_id: uuid.UUID,
    from_: str = Query(..., alias="from"),
    to: str = Query("current"),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Diff two versions (or a version against the current live steps_config).

    ``from`` / ``to`` are version row ids; ``to=current`` (default) compares
    against the definition's live steps_config.
    """
    await ensure_in_entity_scope(
        db,
        WorkflowDefinition,
        workflow_id,
        entity_id,
        detail="Workflow not found",
        include_shared=True,
    )
    defn = await _get_workflow_or_404(db, workflow_id, org_id)

    async def _resolve(token: str) -> tuple[dict, int | str]:
        if token == "current":
            return defn.steps_config, "current"
        try:
            vid = uuid.UUID(token)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid version id") from exc
        ver = (
            await db.execute(
                select(WorkflowVersion).where(
                    WorkflowVersion.id == vid,
                    WorkflowVersion.definition_id == workflow_id,
                )
            )
        ).scalar_one_or_none()
        if not ver:
            raise HTTPException(status_code=404, detail="Version not found")
        return ver.steps_config, ver.version_number

    before, from_label = await _resolve(from_)
    after, to_label = await _resolve(to)

    diff = _diff_steps_config(before, after)
    diff.from_version = from_label
    diff.to_version = to_label
    return diff


# ---------------------------------------------------------------------------
# simulate
# ---------------------------------------------------------------------------


@router.post("/{workflow_id}/simulate", response_model=SimulationResult)
async def simulate_workflow(
    workflow_id: uuid.UUID,
    body: SimulateRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Dry-run the workflow against a sample or real invoice (no side effects)."""
    await ensure_in_entity_scope(
        db,
        WorkflowDefinition,
        workflow_id,
        entity_id,
        detail="Workflow not found",
        include_shared=True,
    )
    from app.models.invoice import Invoice
    from app.services.workflow_builder import build_invoice_context
    from app.services.workflow_simulation import simulate

    defn = await _get_workflow_or_404(db, workflow_id, org_id)

    if body.invoice_id:
        try:
            inv_uuid = uuid.UUID(body.invoice_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid invoice id") from exc
        invoice = (
            await db.execute(select(Invoice).where(Invoice.id == inv_uuid))
        ).scalar_one_or_none()
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        ctx = build_invoice_context(invoice)
    elif body.invoice is not None:
        ctx = build_invoice_context(body.invoice.model_dump())
    else:
        raise HTTPException(status_code=422, detail="Provide invoice or invoice_id")

    result = await simulate(defn.steps_config, ctx)
    return SimulationResult(**result)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@router.get("/{workflow_id}/export", response_model=WorkflowExport)
async def export_workflow(
    workflow_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Export a definition as a portable JSON document (safe to download)."""
    await ensure_in_entity_scope(
        db,
        WorkflowDefinition,
        workflow_id,
        entity_id,
        detail="Workflow not found",
        include_shared=True,
    )
    defn = await _get_workflow_or_404(db, workflow_id, org_id)
    return WorkflowExport(
        schema_version=1,
        name=defn.name,
        description=defn.description,
        steps_config=defn.steps_config,
    )


async def _workflow_instance_count(db: AsyncSession, workflow_id: uuid.UUID) -> int:
    """Count workflow_instances bound to this definition.

    Each instance freezes a steps_config snapshot at invoice creation,
    so deleting the definition out from under live instances would
    leave them with a dangling FK and confuse the editor history.
    """
    result = await db.execute(
        select(func.count())
        .select_from(WorkflowInstance)
        .where(WorkflowInstance.definition_id == workflow_id)
    )
    return int(result.scalar() or 0)


async def _audit_workflow_deleted(
    db: AsyncSession,
    defn: WorkflowDefinition,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    version_count: int,
    bulk: bool,
) -> None:
    """PII-free terminal record for a workflow-definition delete.

    Deleting a definition also removes every `WorkflowVersion` under it — the
    change history of how invoices were routed for approval. That history is
    exactly what a SOX auditor reaches for, so its removal cannot be the one
    tenant mutation with no record of who did it. The version count is recorded
    because it is the size of what went with the definition.
    """
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=actor_id,
        action="workflow.deleted",
        entity_type="workflow_definition",
        entity_id=defn.id,
        details={"name": defn.name, "versions_deleted": version_count, "bulk": bulk},
    )


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    workflow_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(
        db,
        WorkflowDefinition,
        workflow_id,
        entity_id,
        detail="Workflow not found",
        include_shared=True,
    )
    result = await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.id == workflow_id,
            WorkflowDefinition.organization_id == org_id,
        )
    )
    defn = result.scalar_one_or_none()
    if not defn:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if defn.is_default:
        raise HTTPException(status_code=409, detail="Cannot delete the default workflow")
    if defn.is_active:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete an active workflow — deactivate it first",
        )

    instance_count = await _workflow_instance_count(db, workflow_id)
    if instance_count:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "Cannot delete workflow — it is the snapshot source for "
                    f"{instance_count} in-flight invoice"
                    f"{'s' if instance_count != 1 else ''}."
                ),
                "instance_count": instance_count,
            },
        )

    # workflow_versions FK-reference the definition with no DB cascade, so they
    # must be removed first or the DELETE raises a ForeignKeyViolation. (Every
    # PATCH snapshots a version, so any edited workflow has rows here.)
    version_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(WorkflowVersion)
                .where(WorkflowVersion.definition_id == defn.id)
            )
        ).scalar()
        or 0
    )
    await _audit_workflow_deleted(
        db, defn, org_id=org_id, actor_id=user.id, version_count=version_count, bulk=False
    )
    await db.execute(sql_delete(WorkflowVersion).where(WorkflowVersion.definition_id == defn.id))
    await db.delete(defn)


class BulkDeleteWorkflowRequest(BaseModel):
    workflow_ids: list[str]


class BulkDeleteWorkflowFailure(BaseModel):
    workflow_id: str
    reason: str  # "not_found" | "default" | "active" | "instances"
    instance_count: int | None = None


class BulkDeleteWorkflowResponse(BaseModel):
    deleted: list[str]
    failed: list[BulkDeleteWorkflowFailure]


@router.post("/bulk-delete", response_model=BulkDeleteWorkflowResponse)
async def bulk_delete_workflows(
    body: BulkDeleteWorkflowRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Best-effort delete of multiple workflows.

    Each id is processed independently. The same three guards apply
    per-id as the single-delete endpoint: default / active / has
    instances.
    """
    deleted: list[str] = []
    failed: list[BulkDeleteWorkflowFailure] = []

    for raw_id in body.workflow_ids:
        try:
            wf_uuid = uuid.UUID(raw_id)
        except ValueError:
            failed.append(BulkDeleteWorkflowFailure(workflow_id=raw_id, reason="not_found"))
            continue

        result = await db.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.id == wf_uuid,
                WorkflowDefinition.organization_id == org_id,
            )
        )
        defn = result.scalar_one_or_none()
        if not defn:
            failed.append(BulkDeleteWorkflowFailure(workflow_id=raw_id, reason="not_found"))
            continue
        if defn.is_default:
            failed.append(BulkDeleteWorkflowFailure(workflow_id=raw_id, reason="default"))
            continue
        if defn.is_active:
            failed.append(BulkDeleteWorkflowFailure(workflow_id=raw_id, reason="active"))
            continue

        instance_count = await _workflow_instance_count(db, wf_uuid)
        if instance_count:
            failed.append(
                BulkDeleteWorkflowFailure(
                    workflow_id=raw_id,
                    reason="instances",
                    instance_count=instance_count,
                )
            )
            continue

        version_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(WorkflowVersion)
                    .where(WorkflowVersion.definition_id == defn.id)
                )
            ).scalar()
            or 0
        )
        await _audit_workflow_deleted(
            db, defn, org_id=org_id, actor_id=user.id, version_count=version_count, bulk=True
        )
        # Remove version-history rows first (no DB cascade on the FK).
        await db.execute(
            sql_delete(WorkflowVersion).where(WorkflowVersion.definition_id == defn.id)
        )
        await db.delete(defn)
        deleted.append(raw_id)

    return BulkDeleteWorkflowResponse(deleted=deleted, failed=failed)
