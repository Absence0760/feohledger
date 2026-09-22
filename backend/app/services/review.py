"""Review service — approve, reject, and resubmit invoices."""

import logging
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.invoice import Invoice, InvoiceStatus
from app.services.rag import store_embedding
from app.services.vendor_priors import record_corrections
from app.services.workflow_engine import (
    advance_workflow,
    complete_current_step,
    create_workflow_step,
    get_step_config,
    get_workflow_instance,
    transition_invoice,
)
from app.utils.dates import utc_today

_log = logging.getLogger(__name__)


async def _fetch_invoice_bytes(invoice: Invoice) -> bytes | None:
    """Best-effort S3 fetch of the invoice file for RAG embedding.

    Returns None on any failure — RAG storage is a learning-side-effect, not
    a correctness requirement, so we never block the approval on it.
    """
    file_key = invoice.file_key
    if not file_key:
        return None
    try:
        # boto3 is blocking; `_get_object` runs it in a worker thread so an
        # approval never parks the event loop on an S3 round trip.
        from app.services.storage import _get_object

        content, _content_type = await _get_object(file_key)
        return content
    except Exception as exc:
        _log.warning("Failed to fetch bytes for RAG embedding of %s: %s", invoice.id, exc)
        return None


async def resolve_approval_config(
    db: AsyncSession,
    invoice: Invoice,
    instance=None,
) -> dict:
    """The approval-step config governing THIS invoice's approval.

    The frozen per-invoice snapshot first (the invariant: an in-flight invoice
    is governed by the config it entered under, not the live definition), and —
    only when there is no snapshot to read — the org's currently-active
    definition as a **fail-closed** fallback.

    That fallback is the whole point. Not every invoice has a
    ``WorkflowInstance``: the email-intake and PEPPOL-inbound ingest paths
    create the row without one, and so does any legacy / directly-inserted
    invoice. Returning ``{}`` there did not mean "no rules apply" — it meant the
    max-amount cap, the CFO gate, the structuring guard and the named-approver
    check were ALL skipped, so a $50,000 invoice that arrived by email cleared a
    $1,000 ``require_cfo_above`` on a lone ap_manager's approval. A money
    control must not be contingent on a bookkeeping row existing.

    Resolution is read-only (``resolve_active_workflow_definition``, never the
    get-or-CREATE variant) — a definition must never appear as a side effect of
    an approval. ``{}`` now means only "this org has no active definition, or
    its definition has no approval step", which is genuinely nothing to enforce.

    Pass ``instance`` when the caller already loaded it, to save a query.
    """
    from app.services.workflow_engine import resolve_active_workflow_definition

    if instance is None:
        instance = await get_workflow_instance(db, invoice.id)
    snapshot = getattr(instance, "steps_config_snapshot", None) if instance else None
    if not snapshot:
        defn = await resolve_active_workflow_definition(
            db, invoice.organization_id, getattr(invoice, "entity_id", None)
        )
        snapshot = defn.steps_config if defn else None
    if not snapshot:
        return {}
    return get_step_config(snapshot, "approval") or {}


async def _enforce_approval_thresholds(
    db: AsyncSession,
    invoice: Invoice,
    actor_roles: set[str],
    *,
    org_settings: dict | None = None,
    approval_config: dict | None = None,
) -> None:
    """Check approval thresholds from the workflow snapshot. Raises on violation."""
    from fastapi import HTTPException, status

    config = (
        approval_config
        if approval_config is not None
        else await resolve_approval_config(db, invoice)
    )
    if not config:
        return

    # Compare on the money path with Decimal, never float — a float cast of the
    # invoice amount can misjudge a boundary amount against the CFO/max gate.
    amount = Decimal(str(invoice.amount or 0))

    # Structuring guard: a same-vendor rolling-window aggregate that can push
    # the effective amount over the max/CFO gate even though THIS invoice
    # alone doesn't cross it — closes the "split one big payable into several
    # small ones, each under threshold, with distinct invoice numbers so the
    # exact-match duplicate check never fires" bypass. See services/structuring.py.
    aggregate_amount = amount
    recent_spend = Decimal(0)
    invoice_currency = (getattr(invoice, "currency", None) or "").strip().upper() or "USD"
    vendor_id = getattr(invoice, "vendor_id", None)
    structuring_window_days = 0
    if vendor_id is not None:
        from app.services.structuring import get_structuring_config, vendor_recent_spend

        s_cfg = get_structuring_config(org_settings)
        if s_cfg["enabled"]:
            structuring_window_days = s_cfg["window_days"]
            recent_spend = await vendor_recent_spend(
                db,
                vendor_id=vendor_id,
                exclude_invoice_id=invoice.id,
                window_days=structuring_window_days,
                currency=getattr(invoice, "currency", None) or "USD",
                entity_id=getattr(invoice, "entity_id", None),
            )
            aggregate_amount = amount + recent_spend

    # Both money gates below are BARE NUMBERS denominated in the org's REPORTING
    # currency — the same convention `payments.cfo_approval_above` follows (see
    # `services/payment_controls.cfo_approval_decision`) and
    # `settings.expense_approval.cfo_threshold` follows. So the figure compared
    # is the aggregate expressed in that currency, at the rate already LOCKED on
    # this invoice row, not the amount as billed. Without it a GBP 9,000 invoice
    # (USD 11,400) sailed under a USD 10,000 `require_cfo_above` and was approved
    # with no CFO signature at all. `vendor_recent_spend` is scoped to the
    # invoice's own currency, so one rate prices the whole aggregate.
    #
    # An invoice we cannot express in that currency reports `expressible=False`,
    # which `approval_chain`'s shared gate body reads as fail-CLOSED: the gate
    # fires and a human (a CFO, for the CFO gate) has to decide.
    from app.services.approval_chain import reporting_gate_amount

    gate_amount = reporting_gate_amount(invoice, amount=aggregate_amount, org_settings=org_settings)
    gate_currency = gate_amount.currency

    def _structuring_note(threshold_dec: Decimal) -> str:
        if recent_spend <= 0 or amount > threshold_dec:
            return ""
        return (
            f" This invoice alone is under the threshold, but combined with "
            f"{recent_spend:,.2f} {invoice_currency} in other recent invoices from this "
            f"vendor (last {structuring_window_days} days) it totals "
            f"{aggregate_amount:,.2f} {invoice_currency}."
        )

    def _compared_note() -> str:
        """What was actually measured, when it isn't the billed figure."""
        if not gate_amount.expressible:
            return (
                f" This invoice could not be expressed in {gate_currency}, the currency the "
                f"limit is set in, so it cannot be cleared against it."
            )
        if gate_amount.amount == aggregate_amount and invoice_currency == gate_currency:
            return ""
        return (
            f" Measured as {gate_amount.amount:,.2f} {gate_currency} — the limit is set in "
            f"{gate_currency}."
        )

    # Both money gates below read the config through the shared fail-CLOSED
    # parsers in `approval_chain`. `steps_config` is a JSONB blob and
    # `POST /api/workflows/import` accepts it free-form (no Pydantic `Decimal`
    # field constrains it there), so a threshold can genuinely arrive as a
    # non-numeric string — and a bare `Decimal(str(...))` on it raises
    # InvalidOperation, which is a 500 on EVERY approval under that workflow,
    # legitimate ones included. A misconfigured money control must refuse
    # loudly, never crash and never silently skip itself.
    from app.services.approval_chain import _to_decimal, cfo_gate_applies, max_amount_gate_applies

    # Hard reject if over max. The parsed Decimal is used for BOTH the
    # comparison and the message; when it is unusable the gate still fires
    # (fail-closed) and the message names the misconfiguration instead of
    # formatting a value that would raise.
    max_amount = config.get("max_invoice_amount")
    if max_amount_gate_applies(max_amount, gate_amount):
        max_amount_dec = _to_decimal(max_amount)
        if max_amount_dec is None or not max_amount_dec.is_finite():
            detail = (
                "This workflow's approval step has an unusable 'max_invoice_amount'. "
                "Approval is blocked until an admin corrects the workflow definition."
            )
        else:
            detail = (
                f"Invoice amount {amount:,.2f} {invoice_currency} exceeds maximum allowed "
                f"{max_amount_dec:,.2f} {gate_currency}."
                + _structuring_note(max_amount_dec)
                + _compared_note()
            )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)

    # CFO role gate for high-value invoices. `cfo_gate_applies` is the shared
    # fail-CLOSED parse: a configured-but-malformed `require_cfo_above` demands
    # CFO sign-off (never silently skips the gate) instead of raising an
    # InvalidOperation that would 500 every approval — even a legitimate CFO's —
    # and brick the queue on a single settings typo.
    cfo_threshold = config.get("require_cfo_above")
    # The gate reads `aggregate_amount`, not `amount`, so the structuring bypass
    # (split one payable into several under-threshold invoices from the same
    # vendor) can't walk under it either.
    if cfo_gate_applies(cfo_threshold, gate_amount) and "cfo" not in actor_roles:
        threshold_dec = _to_decimal(cfo_threshold)
        limit = (
            f"{threshold_dec:,.2f} {gate_currency}"
            if threshold_dec is not None
            else "the configured limit"
        )
        # A malformed threshold has no Decimal to measure the aggregate against,
        # so the note is omitted — the gate itself still fires (fail-closed).
        note = _structuring_note(threshold_dec) if threshold_dec is not None else ""
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Invoice amount {amount:,.2f} {invoice_currency} exceeds {limit}. "
                f"CFO approval required." + note + _compared_note()
            ),
        )


async def approve_invoice(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID,
    actor_name: str,
    actor_roles: set[str] | None = None,
    corrections: dict | None = None,
    org_settings: dict | None = None,
) -> Invoice:
    from app.services.approval_chain import (
        advance_approval_chain,
        check_level_approver,
        check_segregation,
        get_chain_progress,
    )

    # Read the approval config: the invoice's frozen snapshot, falling back —
    # fail-CLOSED — to the org's active definition when it has none. See
    # `resolve_approval_config`. Resolved ONCE and threaded into
    # `_enforce_approval_thresholds` below, so the segregation / named-approver
    # gates and the money gates can never read different configs.
    instance = await get_workflow_instance(db, invoice.id)
    approval_config: dict = await resolve_approval_config(db, invoice, instance)

    # Segregation of duties: uploader cannot approve
    check_segregation(invoice, actor_id, approval_config)

    # Single-level named-approver gate. "specific" restricts this step to the
    # listed approver_ids (or their active delegate) — the coarse
    # require_permission(PERM_INVOICE_APPROVE) RBAC gate on the endpoint only
    # confirms the actor holds an approving role, not that they are one of the
    # named approvers.
    if approval_config.get("approver_strategy") == "specific":
        specific_ids = approval_config.get("approver_ids") or (
            [approval_config["approver_id"]] if approval_config.get("approver_id") else []
        )
        await check_level_approver(specific_ids, actor_id)

    # Apply any field corrections FIRST, capturing a per-field before/after diff
    # for the audit trail (SOX change-history requirement). Money fields
    # serialise as string-Decimal inside the diff (build_field_diff handles the
    # typing).
    #
    # Order matters: corrections (which can include `amount`) must be applied
    # BEFORE threshold enforcement so the max-amount cap and the CFO gate are
    # evaluated against the POST-correction amount. Enforcing first would let a
    # reviewer approve a $100 invoice with corrections={"amount": 5000} and slip
    # past a $1000 cap / $500 CFO gate — the gate would read the stale $100.
    field_diff: dict = {}
    if corrections:
        from app.services.audit_access import build_field_diff
        from app.services.gl_chart import refuse_gl_codes_outside_chart

        # A GL correction is a coding decision like any other write, so it must
        # resolve in the invoice's own chart (shared ∪ its entity) — never
        # another entity's, and an active account of it whenever it has any
        # (decisions §194/§199) — refused BEFORE any correction is applied. This
        # is also the door the GL-coding exception agent approves through; its
        # coordinator turns the 422 into an escalation with this text as the
        # rationale, so a vendor's dominant historical code whose account has
        # since been retired escalates instead of being approved in.
        new_gl = corrections.get("gl_account")
        if new_gl and new_gl != invoice.gl_account:
            await refuse_gl_codes_outside_chart(
                db,
                organization_id=invoice.organization_id,
                entity_id=invoice.entity_id,
                codes=[new_gl],
            )

        field_map = {"vendor": "vendor_name"}
        before: dict = {}
        after: dict = {}
        for field, value in corrections.items():
            if value is not None:
                attr = field_map.get(field, field)
                before[attr] = getattr(invoice, attr, None)
                setattr(invoice, attr, value)
                after[attr] = getattr(invoice, attr, None)
        diff_fields = list(after.keys())

        # Re-resolve the vendor LINK whenever the correction (re)saved the
        # vendor name and the link is stale or absent — the same rule
        # `PATCH /api/invoices/{id}` applies (`api/invoices.py`). `vendor_id`,
        # not `vendor_name`, is what the payment run reads the payee's bank
        # details from and what the credit-memo vendor guard compares against,
        # so approving a corrected vendor NAME without re-linking left
        # `Invoice.vendor_id` pointing at the PRE-correction vendor — a
        # display/payee mismatch of the same shape the PATCH-path fix already
        # closed, just missing on this one entry point into `approved`.
        # Blank name → no provable vendor → NULL (un-creditable / unpayable
        # until someone names the vendor again), mirroring the PATCH path.
        if "vendor_name" in after and (
            before.get("vendor_name") != invoice.vendor_name or invoice.vendor_id is None
        ):
            from app.services.vendor_matching import match_and_link_vendor

            before["vendor_id"] = invoice.vendor_id
            if (invoice.vendor_name or "").strip():
                await match_and_link_vendor(db, invoice, invoice.organization_id, source="manual")
            else:
                invoice.vendor_id = None
            after["vendor_id"] = invoice.vendor_id
            diff_fields.append("vendor_id")

        field_diff = build_field_diff(before, after, diff_fields)

        # Store vendor-consistent corrections in the correction cache so
        # future extractions from the same vendor pick up the right values.
        await record_corrections(db, invoice, corrections)

        # Recompute warnings + po_match against the CORRECTED fields. Without
        # this the approved invoice keeps the pre-correction artefacts: a
        # po_number correction leaves a stale `po_mismatch` warning and a failed
        # match against the old PO on the row, and a vendor_name correction skips
        # the duplicate check on the new value. The PATCH path already refreshes
        # after setattr; the approve path must too. Best-effort — a warnings
        # recompute must never break an otherwise-valid approval.
        from app.services.invoice_warnings import refresh_warnings

        try:
            await refresh_warnings(db, invoice, org_settings=org_settings)
        except Exception as exc:  # noqa: BLE001
            _log.warning("refresh_warnings after corrections failed for %s: %s", invoice.id, exc)

    # Threshold enforcement — runs against the now-corrected invoice amount, and
    # against the SAME approval config the segregation / named-approver gates
    # above read.
    await _enforce_approval_thresholds(
        db,
        invoice,
        actor_roles or set(),
        org_settings=org_settings,
        approval_config=approval_config,
    )

    # Upsert the RAG embedding using the invoice's NOW-correct fields.
    # Best-effort: failures (S3 unavailable, no text layer, embedding API
    # down) log and move on — the approval itself still commits.
    try:
        file_bytes = await _fetch_invoice_bytes(invoice)
        if file_bytes:
            await store_embedding(db, invoice, file_bytes=file_bytes)
    except Exception as exc:  # noqa: BLE001
        _log.warning("RAG embedding storage failed for %s: %s", invoice.id, exc)

    # Multi-level chain: check if this approval satisfies the current level
    # or if more levels remain.
    if approval_config.get("approver_strategy") == "chain" and instance:
        # Lock the workflow instance row to prevent concurrent approval races
        from app.models.workflow import WorkflowInstance
        from app.services.approval_chain import init_chain_state, resolve_applicable_levels

        locked_result = await db.execute(
            select(WorkflowInstance).where(WorkflowInstance.id == instance.id).with_for_update()
        )
        instance = locked_result.scalar_one()

        # Initialize chain state on first approval if not yet initialized
        # `get_chain_progress` owns the read (and the "a stored `null` means
        # no chain" coercion) — see `approval_chain.chain_state_of`.
        if not get_chain_progress(instance):
            from app.services.approval_chain import invoice_routing_attrs, reporting_gate_amount

            applicable = resolve_applicable_levels(
                approval_config.get("approval_chain", []),
                # A `GateAmount` in the org's REPORTING currency — the per-level
                # `min_amount` / `max_amount` bands are bare numbers denominated
                # there, exactly like `require_cfo_above`, so a foreign invoice
                # must not be routed on its billed figure. Exact Decimal
                # throughout, so a boundary amount still lands on the right
                # approver tier; an invoice with no locked rate reports
                # `expressible=False` and every routing-matched level applies.
                reporting_gate_amount(invoice, org_settings=org_settings),
                invoice_attrs=invoice_routing_attrs(invoice),
            )
            if applicable:
                init_chain_state(instance, applicable)
            else:
                # No levels apply — treat as single-level, fall through
                pass

        # The level index this approval is being recorded against — read BEFORE
        # advancing (advance_approval_chain bumps current_level once the level
        # is satisfied). Used for the partial-approval audit row below.
        chain_state = get_chain_progress(instance)
        chain_levels = chain_state.get("levels", [])
        approved_level = chain_state.get("current_level", 0)

        # Named-approver gate for this level. Without it, any actor holding
        # the coarse role-based RBAC permission can clear a level meant for a
        # specific person (e.g. the named CFO), collapsing the SoD control the
        # chain exists to enforce.
        if approved_level < len(chain_levels):
            await check_level_approver(
                chain_levels[approved_level].get("approver_ids", []), actor_id
            )

        chain_complete = advance_approval_chain(instance, actor_id)
        if not chain_complete:
            # More levels needed — stay in ready_for_review, record partial.
            #
            # Each intermediate approver's decision must land in the immutable
            # audit trail, not only in the mutable state_data JSONB. Without
            # this, a 3-level chain produced exactly ONE audit row (the final
            # approval), so an auditor could never reconstruct who approved at
            # levels 1..N-1. Write an append-only `invoice.approval_step` row
            # capturing the actor + the level they approved at.
            from app.services.audit_dispatch import dispatch_audit

            await dispatch_audit(
                db,
                correlation_id=invoice.correlation_id,
                organization_id=invoice.organization_id,
                actor_id=actor_id,
                action="invoice.approval_step",
                entity_type="invoice",
                entity_id=invoice.id,
                details={
                    "decision": "approved",
                    "level": approved_level,
                    **({"changes": field_diff} if field_diff else {}),
                },
            )
            await db.flush()
            return invoice

    # All approvals satisfied (or single-level) — finalize
    invoice.approval_date = utc_today()
    invoice.approved_by = actor_name

    # Digital signature on the approval (SOX non-repudiation): an HMAC-SHA256
    # "timestamp + user hash" over the canonical approval facts (invoice id +
    # exact Decimal amount + actor + decision + timestamp), stamped into THIS
    # approval audit row's immutable `details`. Re-verifiable later via
    # GET /api/audit/invoice/{id}/verify-signatures. No-op (None) when no signing
    # key is configured. The same `signed_at` timestamp feeds both the digest and
    # the stored block so a re-derivation reproduces identical canonical bytes.
    from datetime import UTC, datetime

    from app.services.approval_signature import build_signature_detail

    signed_at = datetime.now(UTC)
    audit_details: dict = {}
    if field_diff:
        audit_details["changes"] = field_diff
    signature = build_signature_detail(
        invoice_id=invoice.id,
        amount=Decimal(str(invoice.amount or 0)),
        actor_id=actor_id,
        decision="approved",
        timestamp=signed_at,
        signing_key=settings.approval_signing_key,
    )
    if signature:
        audit_details["signature"] = signature

    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.approved,
        actor_id=actor_id,
        action_name="invoice.approved",
        details=audit_details or None,
    )

    if instance:
        await advance_workflow(db, instance, "erp_push", action="approved")

    return invoice


async def reject_invoice(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID,
    actor_name: str,
    reason: str,
) -> Invoice:
    invoice.rejected_by = actor_name
    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.rejected,
        actor_id=actor_id,
        action_name="invoice.rejected",
        details={"reason": reason},
    )

    # Create an exception record (shared chokepoint → emits `exception.raised`)
    from app.services.exception_service import create_exception

    await create_exception(
        db,
        exception_type="review_rejected",
        description=reason,
        status="open",
        organization_id=invoice.organization_id,
        invoice=invoice,  # exception follows its invoice (P2)
        # `actor_id` (the rejecter) IS in scope here and is deliberately not
        # stamped. This row notifies AP of a decision the reviewer already made
        # and already audited (`invoice.rejected`); it is not a second look at
        # their act, so there is nothing for a different person to check.
        # Recording them would make finishing your own rework queue item a
        # segregation breach.
        raised_by_user_id=None,
    )

    instance = await get_workflow_instance(db, invoice.id)
    if instance:
        await complete_current_step(db, instance, "rejected")
        # Track rejection count + reset any multi-level approval chain state.
        #
        # `approve_invoice` only initialises chain state when
        # `approval_levels` is absent. If a rejected-and-reworked invoice kept
        # its old chain state, the next approval would RESUME at whatever level
        # it was rejected at — silently skipping every already-satisfied level
        # (a manager→CFO chain rejected at L0 would then need only the CFO). A
        # reworked invoice must re-run the whole chain, so clear it here (the
        # single reject chokepoint) and let the next approval re-initialise.
        from app.services.approval_chain import clear_chain_state

        state_data = dict(instance.state_data or {})
        state_data["rejection_count"] = state_data.get("rejection_count", 0) + 1
        clear_chain_state(state_data)
        instance.state_data = state_data

    return invoice


async def resubmit_invoice(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID,
) -> Invoice:
    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.ready_for_review,
        actor_id=actor_id,
        action_name="invoice.resubmitted",
    )

    instance = await get_workflow_instance(db, invoice.id)
    if instance:
        # Create a new review step
        await create_workflow_step(db, instance, "review")
        instance.current_step = 1  # review step index

    return invoice


async def assign_reviewer(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    reviewer_name: str,
    control_db: AsyncSession | None = None,
) -> None:
    from app.services.audit_dispatch import dispatch_audit

    # Check delegation — if reviewer is OOO, reassign to their delegate
    original_id = None
    if control_db:
        from app.services.approval_chain import resolve_assignee

        effective_id, original_id = await resolve_assignee(reviewer_id, control_db)
        if original_id:
            # Delegation active — look up delegate's name
            from app.models.user import User as UserModel

            delegate_result = await control_db.execute(
                select(UserModel).where(UserModel.id == effective_id)
            )
            delegate = delegate_result.scalar_one_or_none()
            if delegate:
                reviewer_id = effective_id
                reviewer_name = delegate.full_name

    invoice.assigned_to_id = reviewer_id
    invoice.assigned_to = reviewer_name

    # Mirror the assignment onto the open approval step, when there is one.
    #
    # A missing WorkflowInstance must NOT short-circuit the rest of this
    # function. It used to `return` here, so an invoice with no instance — the
    # email-intake and PEPPOL-inbound ingest paths create one without, and so
    # does any legacy row — had its assignee written with **no
    # `invoice.assigned_for_review` audit row and no notification**. The
    # reviewer was never told, and no email / Slack / Teams approval token was
    # ever minted (that happens inside `notify_event`), so the invoice sat
    # assigned-but-silent. The step row is a nice-to-have; the audit trail and
    # telling the human are not.
    instance = await get_workflow_instance(db, invoice.id)
    if instance is not None:
        from app.models.workflow import WorkflowStep

        # Steps are now persisted under the canonical "approval" type, but rows
        # written before that normalisation may still carry the legacy "review"
        # alias — match both so reassignment finds either.
        result = await db.execute(
            select(WorkflowStep)
            .where(
                WorkflowStep.instance_id == instance.id,
                WorkflowStep.step_type.in_(("approval", "review")),
                WorkflowStep.completed_at.is_(None),
            )
            .order_by(WorkflowStep.created_at.desc())
            .limit(1)
        )
        step = result.scalar_one_or_none()
        if step:
            step.assigned_to = reviewer_id
            if original_id:
                step.original_assigned_to = original_id

    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=actor_id,
        action="invoice.assigned_for_review",
        entity_type="invoice",
        entity_id=invoice.id,
        details={
            "reviewer_id": str(reviewer_id),
            **({"delegated_from": str(original_id)} if original_id else {}),
        },
    )

    # Best-effort notification to the (possibly delegated) reviewer. Assignment
    # is the one notifiable event that does not flow through transition_invoice,
    # so it's dispatched explicitly here. Never breaks the assignment.
    from app.models.notification import EVENT_INVOICE_ASSIGNED
    from app.services.notification_dispatch import notify_event
    from app.services.notification_templates import InvoiceContext

    # `notify_event` swallows its own template/recipient/email failures, but its
    # per-recipient `db.add(...)` is unguarded; this outer guard is the final
    # backstop so a session error there can't abort an otherwise-valid
    # assignment — mirrors the guard in workflow_engine.transition_invoice.
    try:
        await notify_event(
            db,
            correlation_id=invoice.correlation_id,
            organization_id=invoice.organization_id,
            event_type=EVENT_INVOICE_ASSIGNED,
            entity_id=invoice.id,
            recipient_user_ids=[reviewer_id],
            invoice_ctx=InvoiceContext(
                invoice_number=invoice.invoice_number,
                vendor_name=invoice.vendor_name,
                amount=invoice.amount,
                currency=invoice.currency or "USD",
            ),
            actor_id=actor_id,
        )
    except Exception:  # noqa: BLE001 — never let a notification bug break assignment
        _log.exception("assign_reviewer: notification dispatch failed for invoice=%s", invoice.id)
