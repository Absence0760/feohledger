"""Invoice workflow state machine — validates transitions and orchestrates steps."""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import WorkflowDefinition, WorkflowInstance, WorkflowStep
from app.services.audit_dispatch import dispatch_audit

# Re-exported for callers that already import the vocabulary from the engine.
# The definitions live in workflow_step_types — see the note below STEP_TYPES.
from app.services.workflow_step_types import (
    BUILDER_STEP_TYPES,  # noqa: F401
    CANONICAL_STEP_TYPES,
    KNOWN_STEP_TYPES,  # noqa: F401
    canonical_step_index,
    is_known_step_type,  # noqa: F401
    resolve_step_type,
)

_log = logging.getLogger(__name__)

# ---------- valid status transitions ----------

VALID_TRANSITIONS: dict[InvoiceStatus, set[InvoiceStatus]] = {
    InvoiceStatus.new: {
        InvoiceStatus.pending,
        InvoiceStatus.ready_for_review,
        InvoiceStatus.approved,
        InvoiceStatus.done,
    },
    InvoiceStatus.pending: {
        InvoiceStatus.ready_for_review,
        InvoiceStatus.approved,
        InvoiceStatus.failed,
        # AI reading paused (decisions §253): the plan's allowance or the
        # customer's spending cap is used, so the extraction never called the
        # model and the invoice goes back to draft for manual entry — the state
        # an extraction-disabled upload is left in. Not `failed`: nothing broke,
        # and `failed` only leads back to re-extraction. `POST /bulk/status`
        # refuses this edge for a human (an invoice in `pending` is mid-read).
        InvoiceStatus.new,
    },
    InvoiceStatus.ready_for_review: {InvoiceStatus.approved, InvoiceStatus.rejected},
    InvoiceStatus.approved: {
        InvoiceStatus.sending_to_erp,
        InvoiceStatus.payment_scheduled,
        InvoiceStatus.done,
    },
    InvoiceStatus.rejected: {InvoiceStatus.ready_for_review, InvoiceStatus.new},
    InvoiceStatus.sending_to_erp: {InvoiceStatus.sent_to_erp, InvoiceStatus.failed},
    InvoiceStatus.sent_to_erp: {InvoiceStatus.posted_in_erp, InvoiceStatus.done},
    InvoiceStatus.posted_in_erp: {
        InvoiceStatus.payment_scheduled,
        InvoiceStatus.done,
    },
    InvoiceStatus.payment_scheduled: {InvoiceStatus.paid, InvoiceStatus.approved},
    InvoiceStatus.paid: {InvoiceStatus.done, InvoiceStatus.approved},
    InvoiceStatus.done: set(),  # terminal
    InvoiceStatus.failed: {InvoiceStatus.pending, InvoiceStatus.sending_to_erp},
}

# The step-type vocabulary lives in ONE module (services/workflow_step_types.py)
# and is re-exported here for the many callers that already import it from the
# engine. Do NOT redeclare either tuple: they were hand-copied into this module
# and into workflow_builder with no cross-check, which is how the engine ended up
# validating a definition against nothing at all.
STEP_TYPES = CANONICAL_STEP_TYPES


DEFAULT_STEPS_CONFIG = {
    "steps": [
        {
            "number": 1,
            "type": "extraction",
            "name": "Data Extraction",
            "enabled": False,
            "config": {
                "auto_approve_enabled": False,
                "auto_approve_threshold": 0.95,
            },
        },
        {
            "number": 2,
            "type": "approval",
            "name": "Manager Approval",
            # Approval is ON in the fallback, deliberately. This config is what
            # `get_or_create_workflow_definition` mints when a tenant has NO
            # active definition, and with approval disabled `complete_invoice`
            # falls through every branch to the default `→ done` transition:
            # the invoice reaches a terminal, immutable state with no approval,
            # no approval signature, no `invoice.approved` audit row, no
            # segregation check and no CFO gate. A fallback must fail CLOSED.
            # `provision_tenant` seeds a real definition so this is a backstop,
            # not the operative config for any tenant.
            "enabled": True,
            "config": {
                "required": True,
                "approver_id": None,
                "approver_strategy": "manual",
                "require_segregation": True,
            },
        },
        {
            "number": 3,
            "type": "erp_export",
            "name": "ERP Export",
            "enabled": False,
            "config": {
                "erp_system": "default",
                "export_format": "json",
                "endpoint_url": "",
            },
        },
    ],
}


def _check_step_enabled(steps_config: dict, step_type: str) -> bool:
    """Check if a step type is enabled in a steps_config dict."""
    for step in steps_config.get("steps", []):
        if step.get("type") == step_type:
            return step.get("enabled", True)
    return True  # enabled by default if not configured


def get_step_config(steps_config: dict, step_type: str) -> dict:
    """Return the config dict for a specific step type, or empty dict."""
    for step in steps_config.get("steps", []):
        if step.get("type") == step_type:
            return step.get("config", {})
    return {}


async def is_step_enabled(
    db: AsyncSession,
    organization_id: uuid.UUID,
    step_type: str,
    *,
    invoice_id: uuid.UUID | None = None,
) -> bool:
    """Check if a step type is enabled.

    If invoice_id is provided, reads from the instance's frozen snapshot.
    Otherwise reads from the org's active workflow definition.
    """
    if invoice_id:
        instance = await get_workflow_instance(db, invoice_id)
        if instance and instance.steps_config_snapshot:
            return _check_step_enabled(instance.steps_config_snapshot, step_type)

    defn = await get_or_create_workflow_definition(db, organization_id)
    return _check_step_enabled(defn.steps_config, step_type)


def validate_transition(current: InvoiceStatus, target: InvoiceStatus) -> None:
    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot transition from '{current.value}' to '{target.value}'",
        )


async def get_invoice_for_update(db: AsyncSession, invoice_id: uuid.UUID) -> Invoice:
    """Fetch an invoice with a row-level lock to prevent concurrent transitions.

    Eager-loads `extraction_results` so callers that build an
    InvoiceResponse from the returned row don't trigger an
    async-illegal lazy load inside `_priors_summary`. Every
    /api/invoices/<id>/* endpoint that returns InvoiceResponse goes
    through here.
    """
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.extraction_results))
        .where(Invoice.id == invoice_id)
        .with_for_update()
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


async def transition_invoice(
    db: AsyncSession,
    invoice: Invoice,
    target_status: InvoiceStatus,
    *,
    actor_id: uuid.UUID | None = None,
    action_name: str,
    details: dict | None = None,
) -> Invoice:
    """Validate and apply a status transition, writing an audit log entry."""
    validate_transition(invoice.status, target_status)
    old_status = invoice.status.value
    invoice.status = target_status

    # One id per transition OCCURRENCE. An invoice legitimately reaches
    # `approved` (and `paid`) more than once — `POST /api/payments/{id}/void`
    # takes it back to `approved` and a later run settles it again — so the
    # outbound webhook event id has to be minted per transition, not per
    # invoice. Keyed on the invoice id, the second genuine occurrence was
    # swallowed by the `(subscription_id, event_id)` dedupe index and the
    # customer's ERP never heard about the void-and-re-pay.
    occurrence_id = uuid.uuid4()

    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=actor_id,
        action=action_name,
        entity_type="invoice",
        entity_id=invoice.id,
        details={**(details or {}), "old_status": old_status, "new_status": target_status.value},
    )

    # Best-effort notification fan-out. Keyed off the *resulting* status so all
    # the paths that converge on a given status (e.g. payment webhook + ERP
    # webhook + ERP-sync all reaching `paid`) notify once, here, rather than at
    # every call site. Never allowed to break the transition — notify_event
    # swallows its own failures, and this outer guard is a final backstop so a
    # bug in recipient resolution can't abort a committed status change.
    try:
        await _maybe_notify_transition(
            db,
            invoice,
            target_status,
            actor_id=actor_id,
            details=details,
        )
    except Exception:  # noqa: BLE001
        _log.exception("notification hook failed for invoice transition to %s", target_status.value)

    # Best-effort outbound-webhook emit (the push counterpart of the /api/v1
    # pull surface). Same chokepoint as the notification hook — keyed off the
    # resulting status so every path that converges on `approved`/`paid` emits
    # exactly once. `emit_event` opens its own control-plane session, never
    # raises into here, and is a silent no-op when FEOH_WEBHOOKS_ENABLED is off.
    try:
        await _maybe_emit_webhook(invoice, target_status, occurrence_id=occurrence_id)
    except Exception:  # noqa: BLE001 — a webhook emit must never break the transition
        _log.exception("webhook emit hook failed for invoice transition to %s", target_status.value)
    return invoice


async def _maybe_emit_webhook(
    invoice: Invoice,
    target_status: InvoiceStatus,
    *,
    occurrence_id: uuid.UUID | None = None,
) -> None:
    """Map an invoice status transition to an outbound webhook event + emit.

    Only `approved` → `invoice.approved` and `paid` → `payment.settled` are
    emitted here (`exception.raised` is emitted from
    `exception_service.create_exception`).

    `occurrence_id` is the caller's per-transition id and becomes the event's
    dedupe key. Re-emitting the SAME transition (same id) still dedupes; a NEW
    transition mints a new id and therefore a new, deliverable event.
    """
    if target_status is InvoiceStatus.approved:
        from app.services.webhooks import emit_invoice_approved

        await emit_invoice_approved(invoice, occurrence_id=occurrence_id)
    elif target_status is InvoiceStatus.paid:
        from app.services.webhooks import emit_payment_settled

        await emit_payment_settled(invoice, occurrence_id=occurrence_id)


async def _maybe_notify_transition(
    db: AsyncSession,
    invoice: Invoice,
    target_status: InvoiceStatus,
    *,
    actor_id: uuid.UUID | None,
    details: dict | None,
) -> None:
    """Map a status transition to a notification event + recipients and dispatch.

    The "assigned" event is NOT handled here (assignment doesn't always change
    status) — it's fired explicitly from `review.assign_reviewer`.
    """
    from app.models.notification import (
        EVENT_INVOICE_APPROVED,
        EVENT_INVOICE_PAID,
        EVENT_INVOICE_REJECTED,
    )
    from app.services.notification_dispatch import (
        notify_event,
        resolve_role_user_ids,
    )
    from app.services.notification_templates import InvoiceContext

    event_type: str | None = None
    recipients: list[uuid.UUID] = []

    # Read every invoice field defensively — the hook must never assume more
    # about `invoice` than `dispatch_audit` does (which only needs id /
    # correlation_id / organization_id). A missing optional field degrades to
    # "no notification," never an exception that aborts the transition.
    uploaded_by_id = getattr(invoice, "uploaded_by_id", None)

    if target_status is InvoiceStatus.approved:
        event_type = EVENT_INVOICE_APPROVED
        if uploaded_by_id:
            recipients.append(uploaded_by_id)
    elif target_status is InvoiceStatus.rejected:
        event_type = EVENT_INVOICE_REJECTED
        if uploaded_by_id:
            recipients.append(uploaded_by_id)
    elif target_status is InvoiceStatus.paid:
        event_type = EVENT_INVOICE_PAID
        if uploaded_by_id:
            recipients.append(uploaded_by_id)
        try:
            recipients.extend(await resolve_role_user_ids(invoice.organization_id, "ap_manager"))
        except Exception:  # noqa: BLE001 — role lookup must not break the transition
            pass

    if not event_type:
        return

    # Vendor-portal fan-out (paid / rejected) — supplier portal users who own
    # this invoice get an email IF their per-user preference allows it. This is
    # independent of the control-plane recipient resolution above: a
    # portal-submitted invoice often has no `uploaded_by_id` (the actor is a
    # VendorUser, not a User), so it has zero control-plane recipients yet still
    # must reach the supplier. Best-effort + self-contained (never raises).
    try:
        from app.services.vendor_notifications import notify_vendor_of_invoice_event

        await notify_vendor_of_invoice_event(
            db,
            event_type=event_type,
            invoice=invoice,
            reason=(details or {}).get("reason"),
        )
    except Exception:  # noqa: BLE001 — vendor email must not break the transition
        _log.exception("vendor notification hook failed for invoice event %s", event_type)

    if not recipients:
        return

    ctx = InvoiceContext(
        invoice_number=getattr(invoice, "invoice_number", ""),
        vendor_name=getattr(invoice, "vendor_name", ""),
        amount=getattr(invoice, "amount", None),
        currency=getattr(invoice, "currency", None) or "USD",
        reason=(details or {}).get("reason"),
    )
    await notify_event(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        event_type=event_type,
        entity_id=invoice.id,
        recipient_user_ids=recipients,
        invoice_ctx=ctx,
        actor_id=actor_id,
    )


# ---------- workflow instance / step helpers ----------


async def resolve_active_workflow_definition(
    db: AsyncSession,
    organization_id: uuid.UUID,
    entity_id: uuid.UUID | None = None,
) -> WorkflowDefinition | None:
    """Read-only resolve of the active WorkflowDefinition governing an invoice.

    The selection half of ``get_or_create_workflow_definition`` — split out so a
    caller that must NOT write (a control re-deriving an invoice's approval
    config on the fly, e.g. ``review._enforce_approval_thresholds``) can ask the
    same question without a definition appearing as a side effect of an
    approval. Returns ``None`` when the org has no active definition at all.

    Selection precedence (multi-entity Phase 3 — see docs/multi-entity.md):

    1. The invoice's own entity has an active definition (``entity_id`` matches)
       — prefer its ``is_default`` one, then any active (stable ``created_at``
       tiebreak).
    2. Otherwise a shared / org-wide active definition (``entity_id IS NULL``) —
       same default-then-oldest ordering.
    3. Otherwise ANY active definition in the org, whatever its scope. An entity
       with no definition of its own and no shared fallback inherits the org's
       existing default rather than getting a fresh stub.

    Step 3 is also what a no-entity (consolidated) call resolves directly, and
    it must NOT prefer a NULL-scoped row blindly: a fully-disabled "Invoice
    Processing" stub (auto-created by an earlier no-entity call, or left over
    from migration 0029) is also ``is_default``, so a NULL-only lookup would
    return the stub and shadow the seeded entity-scoped default — breaking
    active-steps (every step reads disabled) and routing no-entity invoices
    through an empty workflow. Oldest-default-wins returns the genuine seeded
    definition instead.
    """
    # Deterministic ordering: prefer the explicit default, then the oldest
    # active definition as a stable tiebreak.
    order = (
        WorkflowDefinition.is_default.desc(),
        WorkflowDefinition.created_at.asc(),
    )

    async def _first(*extra_conditions) -> WorkflowDefinition | None:
        result = await db.execute(
            select(WorkflowDefinition)
            .where(
                WorkflowDefinition.organization_id == organization_id,
                WorkflowDefinition.is_active == True,  # noqa: E712
                *extra_conditions,
            )
            .order_by(*order)
        )
        return result.scalars().first()

    if entity_id is not None:
        defn = await _first(WorkflowDefinition.entity_id == entity_id)
        if defn:
            return defn
        # An entity was requested but has no definition of its own — fall back
        # to a shared (entity_id IS NULL) org-wide definition.
        defn = await _first(WorkflowDefinition.entity_id.is_(None))
        if defn:
            return defn

    return await _first()


async def get_or_create_workflow_definition(
    db: AsyncSession,
    organization_id: uuid.UUID,
    entity_id: uuid.UUID | None = None,
) -> WorkflowDefinition:
    """Resolve the active WorkflowDefinition that governs a new invoice, minting
    the org-wide default when the org has none at all.

    Selection is ``resolve_active_workflow_definition`` (see its docstring for
    the precedence). Creating is reserved for an org that genuinely has NO
    active definition: minting one otherwise is the accumulation bug
    `tests-e2e/fixtures/globalSetup.ts` fails the whole e2e run over and
    `docs/known-issues.md` records — a second ``is_default=True`` row appears in
    a different entity scope, and because the auto-created stub has every step
    disabled it can then shadow the real seeded default for no-entity reads.
    """
    defn = await resolve_active_workflow_definition(db, organization_id, entity_id)
    if defn:
        return defn

    # NULL — the SHARED org-wide bucket — deliberately, and unlike the other
    # three creation paths (`POST /api/workflows`, the `GET /api/workflows`
    # auto-seed, and `tenant_provisioning`), which all stamp the caller's
    # entity to match migration 0029's backfill.
    #
    # This one is different because it is the LAST-RESORT fallback: nothing
    # resolved for the requested entity and nothing shared exists either. A
    # shared row serves every entity at once, which is what a fallback should
    # do; stamping the requesting entity would mint one stub per entity and
    # couple each to that entity's lifetime (deleting the entity then fails on
    # this row's FK). The one-active-per-scope invariant is unaffected — the
    # paths a user actually creates definitions through all agree, and this
    # fires only when there is nothing to conflict with.
    defn = WorkflowDefinition(
        name="Invoice Processing",
        description="Upload → Review → ERP → Done",
        steps_config=DEFAULT_STEPS_CONFIG,
        is_active=True,
        is_default=True,
        organization_id=organization_id,
        entity_id=None,
    )
    db.add(defn)
    await db.flush()
    return defn


async def create_workflow_instance(db: AsyncSession, invoice: Invoice) -> WorkflowInstance:
    defn = await get_or_create_workflow_definition(db, invoice.organization_id, invoice.entity_id)
    # A/B testing: if a running experiment targets this definition, the invoice
    # is deterministically assigned to a variant and that variant's config is
    # frozen onto the snapshot (respecting the per-invoice snapshot invariant).
    # Best-effort: a failure here never blocks invoice creation — the invoice
    # falls back to the live definition's config.
    snapshot = defn.steps_config
    try:
        from app.services.workflow_experiments_runtime import maybe_assign_experiment_variant

        variant_config = await maybe_assign_experiment_variant(db, invoice, defn)
        if variant_config is not None:
            snapshot = variant_config
    except Exception:  # noqa: BLE001 — experiment routing must never break creation
        _log.exception("workflow-experiment assignment failed; using live config")
    instance = WorkflowInstance(
        correlation_id=invoice.correlation_id,
        definition_id=defn.id,
        invoice_id=invoice.id,
        current_step=0,
        state="active",
        steps_config_snapshot=snapshot,
    )
    db.add(instance)
    await db.flush()
    return instance


async def get_workflow_instance(db: AsyncSession, invoice_id: uuid.UUID) -> WorkflowInstance | None:
    result = await db.execute(
        select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice_id)
    )
    return result.scalar_one_or_none()


async def create_workflow_step(
    db: AsyncSession,
    instance: WorkflowInstance,
    step_type: str,
    *,
    assigned_to: uuid.UUID | None = None,
) -> WorkflowStep:
    # Normalise to the canonical step type BEFORE persisting. Callers pass a mix
    # of canonical names and legacy aliases ("upload"/"review"/"erp_push"); if we
    # stored the raw alias, queries that filter on the canonical name (e.g. the
    # dashboard pending-approvals / analytics / assistant lookups for
    # step_type == "approval") would silently miss alias-named rows. Storing the
    # resolved name keeps the persisted data consistent across all call sites
    # (and matches what scripts/seed.py writes).
    #
    # `canonical_step_index` is the guard: it refuses a no-code builder type
    # (orchestration config, no place in the pipeline) and an unrecognised one
    # BY NAME, before anything is added to the session. The bare
    # `STEP_TYPES.index(resolved)` this replaces raised
    # `ValueError: list.index(x): x not in list` — a 500 naming neither the
    # value nor the reason.
    resolved = resolve_step_type(step_type)
    step_number = canonical_step_index(resolved)
    step = WorkflowStep(
        correlation_id=instance.correlation_id,
        instance_id=instance.id,
        step_number=step_number,
        step_type=resolved,
        assigned_to=assigned_to,
    )
    db.add(step)
    await db.flush()
    return step


async def complete_current_step(
    db: AsyncSession,
    instance: WorkflowInstance,
    action: str,
) -> WorkflowStep | None:
    """Mark the most recent incomplete step as completed."""
    result = await db.execute(
        select(WorkflowStep)
        .where(
            WorkflowStep.instance_id == instance.id,
            WorkflowStep.completed_at.is_(None),
        )
        .order_by(WorkflowStep.step_number.desc())
        .limit(1)
    )
    step = result.scalar_one_or_none()
    if step:
        step.action = action
        step.completed_at = datetime.now(UTC)
    return step


async def advance_workflow(
    db: AsyncSession,
    instance: WorkflowInstance,
    next_step_type: str,
    *,
    action: str,
    assigned_to: uuid.UUID | None = None,
) -> WorkflowStep:
    """Complete the current step and create the next one."""
    # Resolve BEFORE closing the current step: this used to run after
    # `complete_current_step`, so a step type the pipeline can't drive left the
    # current step closed with no successor opened — a permanently stranded
    # instance — on its way to raising.
    resolved_next = resolve_step_type(next_step_type)
    next_index = canonical_step_index(resolved_next) - 1
    await complete_current_step(db, instance, action)
    instance.current_step = next_index
    new_step = await create_workflow_step(db, instance, next_step_type, assigned_to=assigned_to)
    return new_step


async def complete_workflow(
    db: AsyncSession,
    instance: WorkflowInstance,
    action: str = "completed",
) -> None:
    """Mark the workflow as completed."""
    await complete_current_step(db, instance, action)
    # Create the final "done" step
    done_step = await create_workflow_step(db, instance, "done")
    done_step.action = "completed"
    done_step.completed_at = datetime.now(UTC)
    instance.current_step = len(STEP_TYPES) - 1
    instance.state = "completed"
