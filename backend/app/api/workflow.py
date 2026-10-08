"""Workflow action endpoints — upload, review, ERP, and read endpoints."""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    ensure_live_erp_entitled,
    get_current_user,
    get_org_id,
    require_permission,
    require_roles,
)
from app.api.file_proxy import serve_owned_file
from app.api.invoice_entry import (
    INVOICE_ENTRY_ROLES,
    INVOICE_ENTRY_WINDOW_CLOSED,
    in_entry_window,
    is_entry_only,
    missing_required_fields,
    refuse_entry_only_outside_window,
    stamp_entry_editor,
    was_ever_approved,
)
from app.api.invoice_version import (
    INVOICE_STALE_APPROVAL,
    STALE_APPROVAL_MESSAGE,
    matches_loaded_version,
)
from app.api.permissions import PERM_INVOICE_APPROVE
from app.api.refusals import coded_refusal
from app.database import get_control_db
from app.models.invoice import Invoice, InvoiceExtractionResult, InvoiceStatus
from app.models.organization import Organization
from app.models.user import User
from app.models.workflow import AuditLog, WorkflowInstance, WorkflowStep
from app.schemas.invoice import InvoiceResponse
from app.schemas.workflow import (
    ApproveRequest,
    AssignReviewerRequest,
    AuditLogEntryResponse,
    RejectRequest,
    WorkflowInstanceResponse,
)
from app.services import erp as erp_svc
from app.services import review as review_svc
from app.services.erp_dispatch import dispatch_erp
from app.services.extraction_dispatch import dispatch_extraction
from app.services.invoice_warnings import refresh_warnings
from app.services.storage import upload_invoice_file
from app.services.workflow_engine import (
    advance_workflow,
    create_workflow_instance,
    create_workflow_step,
    get_invoice_for_update,
    get_step_config,
    get_workflow_instance,
    is_step_enabled,
    transition_invoice,
)
from app.tenant import (
    ensure_in_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.dates import utc_today

router = APIRouter(prefix="/invoices", tags=["workflow"])

#: `POST /invoices/{id}/complete` refused for blank required fields; `fields`
#: names them (`vendor` / `invoice_number` / `amount`).
INVOICE_REQUIRED_FIELDS_MISSING = "invoice_required_fields_missing"


# ---------- Stage 1: Upload ----------


@router.post("/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_invoice(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*INVOICE_ENTRY_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    """Upload an invoice file, create the invoice, and optionally trigger extraction."""
    try:
        # Create invoice with blank fields, under the selected (or default) entity.
        invoice = Invoice(
            invoice_number="",
            vendor_name="",
            description="",
            amount=Decimal("0"),
            currency="USD",
            status=InvoiceStatus.new,
            organization_id=org_id,
            entity_id=entity_id,
            uploaded_by_id=user.id,
        )
        db.add(invoice)
        await db.flush()

        # Upload file to S3
        file_key, file_url = await upload_invoice_file(org_id, invoice.id, file)
        invoice.file_key = file_key
        invoice.file_url = file_url

        # Create workflow instance
        instance = await create_workflow_instance(db, invoice)

        # Check if extraction is enabled — read the snapshot `create_workflow_instance`
        # just froze onto THIS invoice, never the live definition. Resolving the
        # definition a second time can disagree with the frozen one (breaking the
        # frozen-snapshot invariant, decisions §13) and, worse,
        # `get_or_create_workflow_definition` INSERTs a definition when it finds
        # none — inside the upload transaction. Every sibling call in this file
        # passes `invoice_id`.
        extraction_enabled = await is_step_enabled(db, org_id, "extraction", invoice_id=invoice.id)
        print(f"[upload] Invoice {invoice.id} created, extraction_enabled={extraction_enabled}")

        if extraction_enabled:
            # Transition new → pending and trigger extraction
            await transition_invoice(
                db,
                invoice,
                InvoiceStatus.pending,
                actor_id=user.id,
                action_name="invoice.uploaded",
                details={"filename": file.filename, "content_type": file.content_type},
            )
            await create_workflow_step(db, instance, "upload")
            await db.commit()
            await db.refresh(invoice)

            print(f"[upload] Dispatching extraction for invoice {invoice.id}")
            # An entry-only caller's upload always lands at review: the
            # unattended confidence / amount gates would otherwise approve a
            # document they chose with no second person involved
            # (`api/invoice_entry.py`).
            await dispatch_extraction(
                invoice.id, org_id, user.id, suppress_auto_approve=is_entry_only(user)
            )
            print(f"[upload] Extraction dispatched for invoice {invoice.id}")

            # Log that extraction was dispatched
            from app.services.audit_dispatch import dispatch_audit

            await dispatch_audit(
                db,
                correlation_id=invoice.correlation_id,
                organization_id=org_id,
                actor_id=user.id,
                action="invoice.extraction_dispatched",
                entity_type="invoice",
                entity_id=invoice.id,
                details={"trigger": "auto_on_upload", "filename": file.filename},
            )

            return {
                "id": str(invoice.id),
                "correlation_id": str(invoice.correlation_id),
                "status": invoice.status.value,
                "message": "Invoice uploaded. Extraction in progress.",
            }
        else:
            # No extraction — leave as new for manual entry
            await create_workflow_step(db, instance, "upload")
            await refresh_warnings(db, invoice, org_settings=org.settings)
            await db.commit()
            await db.refresh(invoice)

            return {
                "id": str(invoice.id),
                "correlation_id": str(invoice.correlation_id),
                "status": invoice.status.value,
                "message": "Invoice uploaded. Ready for manual entry.",
            }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{invoice_id}/extract")
async def trigger_extraction(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*INVOICE_ENTRY_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Manually trigger or re-trigger extraction on an invoice.

    Works on invoices in 'new' or 'failed' status that have a file attached.
    """
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)
    # An approved invoice whose ERP push failed is `failed` too; re-extracting
    # it would rewrite signed-off fields, so a clerk is held to the entry window.
    refuse_entry_only_outside_window(user, invoice)

    if invoice.status not in (InvoiceStatus.new, InvoiceStatus.failed):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot extract from '{invoice.status.value}' status. Must be 'new' or 'failed'."
            ),
        )
    # The same holds for everyone, not only clerks: an approved invoice whose
    # ERP push failed is `failed`, and re-reading its document would rewrite
    # the vendor, amount and lines an approver signed while `approved_by`
    # still names them. The remedy for a failed push is Retry ERP.
    if was_ever_approved(invoice):
        raise HTTPException(
            status_code=409,
            detail=(
                "This invoice has already been approved, so its document can't be read "
                "again. Retry the ERP push, or reject it for rework."
            ),
        )

    if not invoice.file_key:
        raise HTTPException(
            status_code=400, detail="No file attached to this invoice. Upload a file first."
        )

    # Re-extraction rewrites the vendor, amount, dates and lines: a content
    # change by whoever asked for it.
    stamp_entry_editor(user, invoice)
    # Transition to pending
    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.pending,
        actor_id=user.id,
        action_name="invoice.extraction_triggered",
        details={"manual": True},
    )
    await db.commit()
    await db.refresh(invoice)

    # Dispatch extraction
    from app.services.extraction_dispatch import dispatch_extraction

    # Same as upload: a clerk may have just attached or swapped the document,
    # so their extraction never auto-approves.
    await dispatch_extraction(
        invoice.id, org_id, user.id, suppress_auto_approve=is_entry_only(user)
    )

    return {
        "id": str(invoice.id),
        "status": invoice.status.value,
        "message": "Extraction triggered. Fields will be populated shortly.",
    }


@router.post("/{invoice_id}/reset-extraction")
async def reset_extraction(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*INVOICE_ENTRY_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Reset a stuck extraction — moves invoice from 'pending' back to 'new'."""
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)
    refuse_entry_only_outside_window(user, invoice)

    if invoice.status != InvoiceStatus.pending:
        raise HTTPException(
            status_code=409,
            detail=f"Can only reset from 'pending' status, not '{invoice.status.value}'.",
        )

    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.failed,
        actor_id=user.id,
        action_name="invoice.extraction_reset",
        details={"reason": "Manual reset — extraction stuck or failed silently"},
    )
    await db.commit()
    await db.refresh(invoice)

    return {
        "id": str(invoice.id),
        "status": invoice.status.value,
        "message": "Extraction reset. You can re-extract or edit manually.",
    }


# ---------- Stage 2: Review ----------


@router.post("/{invoice_id}/assign", response_model=InvoiceResponse)
async def assign_reviewer(
    invoice_id: uuid.UUID,
    body: AssignReviewerRequest,
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)
    if invoice.status != InvoiceStatus.ready_for_review:
        raise HTTPException(
            status_code=409, detail="Invoice must be in 'ready_for_review' to assign a reviewer"
        )

    try:
        reviewer_id = uuid.UUID(body.user_id)
    except (TypeError, ValueError) as exc:
        # Attacker-controlled input; a validation error, never a bare 500.
        raise HTTPException(status_code=422, detail="`user_id` must be a UUID.") from exc

    # Scope the reviewer to the CALLER'S OWN org, at the data layer.
    #
    # `users` is control-plane, so an unscoped `WHERE id = :id` reaches every
    # tenant's accounts: an admin/ap_manager of tenant A could stamp tenant B's
    # user onto `Invoice.assigned_to_id`, and `review.assign_reviewer` then
    # dispatched the `invoice_assigned` notification to them — an email carrying
    # tenant A's invoice number, vendor and amount to somebody in another
    # tenant. The invoice was also left owned by an account that can never open
    # it (`get_tenant`'s org-claim cross-check refuses them, and the email
    # approval link's `_load_reviewer` rejects a wrong-org actor), so it sat in
    # the queue owned by nobody.
    #
    # `is_active` is part of the same guard for the second reason: a
    # deactivated reviewer is the identical quiet failure — this is exactly what
    # `POST /api/auth/delegation` and `POST /api/exceptions/{id}/assign` already
    # check for, and `GET /api/invoices/assignable-reviewers` (this endpoint's
    # own picker) only ever offers active same-org users. Same opaque 404 for
    # "another tenant's user", "deactivated" and "no such user", so the response
    # can't enumerate accounts across tenants.
    result = await control_db.execute(
        select(User).where(
            User.id == reviewer_id,
            User.organization_id == user.organization_id,
            User.is_active.is_(True),
        )
    )
    reviewer = result.scalar_one_or_none()
    if not reviewer:
        raise HTTPException(status_code=404, detail="Reviewer not found")

    await review_svc.assign_reviewer(
        db,
        invoice,
        actor_id=user.id,
        reviewer_id=reviewer_id,
        reviewer_name=reviewer.full_name,
    )
    return InvoiceResponse.from_db(invoice)


@router.post("/{invoice_id}/approve", response_model=InvoiceResponse)
async def approve_invoice(
    invoice_id: uuid.UUID,
    body: ApproveRequest | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_permission(PERM_INVOICE_APPROVE)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)
    corrections = body.model_dump(exclude_unset=True) if body else {}
    # The version the approver loaded, compared under the row lock above: an
    # edit that landed after they read the invoice is refused, not signed
    # (`api/invoice_version.py`). A request token, not a correction — popped
    # before `corrections` reaches the review service.
    expected_updated_at = corrections.pop("expected_updated_at", None)
    if expected_updated_at is not None and not matches_loaded_version(invoice, expected_updated_at):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=coded_refusal(INVOICE_STALE_APPROVAL, STALE_APPROVAL_MESSAGE),
        )

    actor_roles = {r.name for r in user.roles} if user.roles else set()
    # `org_settings` is not optional detail on this path — it carries the org's
    # own `fraud_rules` (which rules raise a payment-BLOCKING exception), its
    # `matching` per-vendor/commodity PO tolerances, its `exceptions` routing/SLA
    # map, and the structuring-guard window the threshold gate measures against.
    # Omitting it silently reverted every one of them to the platform default
    # mid-approval: a rule an org had turned off still opened a `fraud_flag` that
    # then refused the payment run, and an approve-with-corrections recomputed
    # `invoice.po_match` at the default 5% tolerance, erasing a stricter rule's
    # `po_mismatch` from the row. `POST /api/invoices/bulk/status` always passed
    # it; the single-invoice endpoint didn't.
    await review_svc.approve_invoice(
        db,
        invoice,
        actor_id=user.id,
        actor_name=user.full_name,
        actor_roles=actor_roles,
        corrections=corrections or None,
        org_settings=org.settings,
    )
    return InvoiceResponse.from_db(invoice)


@router.post("/{invoice_id}/reject", response_model=InvoiceResponse)
async def reject_invoice(
    invoice_id: uuid.UUID,
    body: RejectRequest,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(PERM_INVOICE_APPROVE)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)

    await review_svc.reject_invoice(
        db,
        invoice,
        actor_id=user.id,
        actor_name=user.full_name,
        reason=body.reason,
    )
    return InvoiceResponse.from_db(invoice)


@router.post("/{invoice_id}/resubmit", response_model=InvoiceResponse)
async def resubmit_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*INVOICE_ENTRY_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)
    refuse_entry_only_outside_window(user, invoice)

    await review_svc.resubmit_invoice(
        db,
        invoice,
        actor_id=user.id,
    )
    return InvoiceResponse.from_db(invoice)


# ---------- Stage 3: ERP ----------


@router.post("/{invoice_id}/send-to-erp", status_code=status.HTTP_202_ACCEPTED)
async def send_to_erp(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    org: Organization = Depends(get_tenant),
    control_db: AsyncSession = Depends(get_control_db),
):
    # Pushing to a LIVE ERP is a Growth feature (decisions §253/§258); the
    # local-first `mock` ERP and an org with no ERP configured are never gated.
    await ensure_live_erp_entitled(control_db, org.id, (org.settings or {}).get("erp"))
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)

    # Transition to sending_to_erp before dispatching
    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.sending_to_erp,
        actor_id=user.id,
        action_name="invoice.erp_submitted",
    )
    await db.commit()

    # Dispatch ERP call — local background task or SQS depending on config
    await dispatch_erp(invoice.id, org_id, user.id)

    return {
        "id": str(invoice.id),
        "correlation_id": str(invoice.correlation_id),
        "status": invoice.status.value,
    }


@router.post("/{invoice_id}/retry-erp", status_code=status.HTTP_202_ACCEPTED)
async def retry_erp(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    org: Organization = Depends(get_tenant),
    control_db: AsyncSession = Depends(get_control_db),
):
    # Pushing to a LIVE ERP is a Growth feature (decisions §253/§258); the
    # local-first `mock` ERP and an org with no ERP configured are never gated.
    await ensure_live_erp_entitled(control_db, org.id, (org.settings or {}).get("erp"))
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)

    await erp_svc.retry_erp(db, invoice, actor_id=user.id)
    await db.commit()

    # Dispatch the retried ERP call
    await dispatch_erp(invoice.id, org_id, user.id)

    return {
        "id": str(invoice.id),
        "correlation_id": str(invoice.correlation_id),
        "status": invoice.status.value,
    }


# ---------- Stage 4: Complete ----------


@router.post("/{invoice_id}/complete", status_code=status.HTTP_200_OK)
async def complete_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*INVOICE_ENTRY_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    control_db: AsyncSession = Depends(get_control_db),
):
    """Advance an invoice to the next logical step based on the workflow.

    - new + approval enabled → ready_for_review
    - new + no approval → done
    - approved + ERP enabled → triggers ERP dispatch
    - approved + no ERP → done

    An entry-only caller (an AP clerk) may take only the first: submitting a
    `new` invoice for review is the end of entry, while closing an invoice with
    no approval step, or pushing an approved one to the ERP, is past it. Their
    submit ALWAYS lands at review — the amount-floor auto-approve below is
    skipped for them, whatever the org's `require_segregation`. The floor's own
    segregation degrade would usually catch a clerk (they are the uploader, or
    `stamp_entry_editor` put them in the set), but an org that opted out of
    segregation would then have the floor approve the clerk's own figures with
    no second person involved; entry never ends in an approval.
    """
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)

    approval_enabled = await is_step_enabled(db, org_id, "approval", invoice_id=invoice.id)
    entry_only = is_entry_only(user)
    if entry_only and not (
        invoice.status == InvoiceStatus.new and approval_enabled and in_entry_window(invoice)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=coded_refusal(
                INVOICE_ENTRY_WINDOW_CLOSED,
                "AP clerks can submit a new invoice for review; completing it is "
                "an AP manager's step.",
                status=invoice.status.value,
            ),
        )

    missing = missing_required_fields(invoice)
    if missing:
        # Coded (`api/refusals.coded_refusal`): the invoice modal suppresses this
        # toast because its form already highlights the fields, and it decides
        # that on the CODE — a substring match on the English broke the moment
        # the sentence was localized.
        raise HTTPException(
            status_code=422,
            detail=coded_refusal(
                INVOICE_REQUIRED_FIELDS_MISSING,
                f"Required fields missing: {', '.join(missing)}",
                fields=missing,
            ),
        )

    # Check workflow config for this invoice
    erp_enabled = await is_step_enabled(db, org_id, "erp_export", invoice_id=invoice.id)

    await refresh_warnings(db, invoice, org_settings=org.settings)

    if invoice.status == InvoiceStatus.new and approval_enabled:
        # Check auto_approve_below — skip review for small invoices
        instance = await get_workflow_instance(db, invoice.id)
        approval_config: dict = {}
        if instance and instance.steps_config_snapshot:
            approval_config = get_step_config(instance.steps_config_snapshot, "approval")

        # Use the shared, gated decision so the amount-floor auto-approve still
        # honours the max_invoice_amount / require_cfo_above money-control gates
        # (a misconfigured high `auto_approve_below` must not slip a CFO-gated
        # amount past review).
        #
        # The extraction step's config is deliberately NOT passed. There is no
        # extraction result on this path — the `0.0` below is a sentinel meaning
        # "no confidence evidence", not a measurement — and `auto_approve_threshold`
        # is a schema-valid `0.0..1.0` float, so an org that set the bar to `0`
        # ("auto-approve at any confidence", meant for the extraction path) made
        # `0.0 >= 0.0` fire HERE: every manually-completed invoice auto-approved
        # with no amount floor configured at all, stamped `reason:
        # "below_threshold"` with a null threshold, and then 500-ing on the
        # response message AFTER the commit — so the caller saw an error while
        # the invoice was silently approved. Handing in `{}` makes the code match
        # the rule this branch documents: on the manual-complete path the amount
        # floor is the only trigger that means anything.
        from app.services.approval_chain import (
            finite_money_threshold,
            receipt_recorders,
            reporting_gate_amount,
            violates_receiving_segregation,
            violates_segregation,
        )
        from app.services.extraction import decide_auto_approve, resolve_gate_aggregate

        # The parsed floor: also what the success message formats, so the figure
        # shown can never diverge from the one compared against (and an unusable
        # value simply isn't a floor — `decide_auto_approve` reads it the same way).
        auto_below = finite_money_threshold(approval_config.get("auto_approve_below"))

        # Segregation of duties is another control gate the amount floor must
        # honour: if the caller uploaded this invoice and the org requires
        # segregation, the amount-floor auto-approve would make them the
        # effective approver of their own invoice. Degrade to human review (as
        # the CFO/max-amount gates already do) rather than 403 a legitimate
        # submission — a second pair of eyes still signs off. The same holds for
        # whoever hand-recorded a goods receipt this invoice is billed against
        # (decisions §267): the floor would make the receiver its approver.
        # The max-amount / CFO gates inside decide_auto_approve are measured
        # against the same same-vendor rolling aggregate `review`'s human path
        # uses (the structuring guard), so splitting a payable can't slip each
        # piece past the controls unattended.
        gate_aggregate = await resolve_gate_aggregate(db, invoice, org_settings=org.settings)
        # `auto_approve_below` / `require_cfo_above` / `max_invoice_amount` are
        # bare numbers denominated in the org's REPORTING currency (like
        # `payments.cfo_approval_above`), so both operands are expressed there —
        # at the rate `refresh_warnings` above just locked onto the row, never
        # one fetched now. An invoice we can't express there fails closed: the
        # floor doesn't fire and the CFO / max gates do. Either way, a human.
        gate_amount = reporting_gate_amount(invoice, org_settings=org.settings)
        if (
            decide_auto_approve(
                {},  # no extraction result here — see the note above
                approval_config,
                overall_confidence=0.0,
                amount=gate_amount,
                aggregate_amount=reporting_gate_amount(
                    invoice, amount=gate_aggregate, org_settings=org.settings
                ),
            )
            and not entry_only
            and not violates_segregation(invoice, user.id, approval_config)
            and not violates_receiving_segregation(
                user.id, await receipt_recorders(db, invoice), approval_config
            )
        ):
            invoice.approval_date = utc_today()
            invoice.approved_by = "system (below threshold)"
            await transition_invoice(
                db,
                invoice,
                InvoiceStatus.approved,
                actor_id=user.id,
                action_name="invoice.auto_approved",
                details={
                    "reason": "below_threshold",
                    # Exact decimal string — money never rides an audit row as a
                    # float, and this is now guaranteed non-null: the branch is
                    # only reachable through the floor.
                    "threshold": str(auto_below),
                    "amount": str(invoice.amount),
                },
            )
            if instance:
                await advance_workflow(db, instance, "erp_push", action="auto_approved")
            await db.commit()
            await db.refresh(invoice)
            return {
                "id": str(invoice.id),
                "correlation_id": str(invoice.correlation_id),
                "status": invoice.status.value,
                "message": (
                    # `auto_below` is the SAME parsed Decimal the comparison
                    # used, so the figure shown can't diverge from the figure
                    # enforced — and formatting can't raise on a raw JSONB value
                    # the way `Decimal(str(auto_below))` did when the branch was
                    # reachable without a floor at all. The currency is named
                    # rather than assumed to be dollars: the threshold is
                    # denominated in the org's reporting currency.
                    f"Auto-approved (amount below the {auto_below:,.2f} "
                    f"{gate_amount.currency} threshold)."
                ),
            }

        # Submit for review
        await transition_invoice(
            db,
            invoice,
            InvoiceStatus.ready_for_review,
            actor_id=user.id,
            action_name="invoice.submitted_for_review",
        )
        await db.commit()
        await db.refresh(invoice)
        return {
            "id": str(invoice.id),
            "correlation_id": str(invoice.correlation_id),
            "status": invoice.status.value,
            "message": "Submitted for review.",
        }

    if invoice.status == InvoiceStatus.approved and erp_enabled:
        # A live ERP push needs the plan (§258). Refused BEFORE the transition,
        # so the invoice stays `approved` — still payable directly — rather
        # than parking in `sending_to_erp` for a push that will never run.
        await ensure_live_erp_entitled(control_db, org.id, (org.settings or {}).get("erp"))
        # Trigger ERP dispatch
        await transition_invoice(
            db,
            invoice,
            InvoiceStatus.sending_to_erp,
            actor_id=user.id,
            action_name="invoice.erp_submitted",
        )
        await db.commit()
        await dispatch_erp(invoice.id, org_id, user.id)
        return {
            "id": str(invoice.id),
            "correlation_id": str(invoice.correlation_id),
            "status": invoice.status.value,
            "message": "Sending to ERP.",
        }

    # Default: mark as done
    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.done,
        actor_id=user.id,
        action_name="invoice.completed",
    )
    await db.commit()
    await db.refresh(invoice)
    return {
        "id": str(invoice.id),
        "correlation_id": str(invoice.correlation_id),
        "status": invoice.status.value,
        "message": "Invoice complete.",
    }


@router.get("/{invoice_id}/export")
async def export_invoice(
    invoice_id: uuid.UUID,
    format: str = "json",
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Export invoice data in the requested format for ERP upload."""
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    data = {
        "invoice_number": invoice.invoice_number,
        "vendor": invoice.vendor_name,
        "amount": str(invoice.amount),
        "currency": invoice.currency,
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "po_number": invoice.po_number,
        "description": invoice.description,
        "subtotal": str(invoice.subtotal) if invoice.subtotal else None,
        "tax_amount": str(invoice.tax_amount) if invoice.tax_amount else None,
        "gl_account": invoice.gl_account,
        "cost_center": invoice.cost_center,
        "correlation_id": str(invoice.correlation_id),
    }

    if format == "xml":
        import xml.etree.ElementTree as ET

        root = ET.Element("Invoice")
        for key, value in data.items():
            child = ET.SubElement(root, key)
            child.text = value if value is not None else ""
        content = ET.tostring(root, encoding="unicode", xml_declaration=True)

        from fastapi.responses import Response

        return Response(
            content=content,
            media_type="application/xml",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="invoice-{invoice.invoice_number or invoice_id}.xml"'
                )
            },
        )

    elif format == "csv":
        import csv
        import io

        from app.services.report_export import csv_safe_cell

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=data.keys())
        writer.writeheader()
        # Neutralize CSV formula injection (CWE-1236).
        writer.writerow({k: csv_safe_cell(v) for k, v in data.items()})

        from fastapi.responses import Response

        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="invoice-{invoice.invoice_number or invoice_id}.csv"'
                )
            },
        )

    else:
        # JSON (default)
        return data


# ---------- File access ----------


@router.get("/file/{file_key:path}")
async def get_invoice_file(
    file_key: str,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Proxy an invoice's source document from S3 to the browser.

    The key is not the authorisation: ``api/file_proxy`` parses the owning
    invoice out of ``<org_id>/<invoice_id>/<filename>``, resolves it within the
    caller's selected entity, and requires the key to be that invoice's CURRENT
    ``file_key``. Wrong org, wrong layout, another subsidiary's invoice, a
    superseded object and a missing one are all the same 404, so the response
    can't enumerate keys (``docs/decisions.md`` §226).
    """
    return await serve_owned_file(
        db, file_key, org_id=user.organization_id, entity_id=entity_id, kind="invoice"
    )


# ---------- Read endpoints ----------


@router.get("/{invoice_id}/workflow")
async def get_workflow(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(
        db, Invoice, invoice_id, entity_id, detail="No workflow found for this invoice"
    )
    result = await db.execute(
        select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice_id)
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="No workflow found for this invoice")

    steps_result = await db.execute(
        select(WorkflowStep)
        .where(WorkflowStep.instance_id == instance.id)
        .order_by(WorkflowStep.step_number, WorkflowStep.created_at)
    )
    steps = steps_result.scalars().all()

    return WorkflowInstanceResponse.from_db(instance, steps)


@router.get("/{invoice_id}/audit-log")
async def get_audit_log(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    # Get the invoice's correlation_id
    result = await db.execute(select(Invoice.correlation_id).where(Invoice.id == invoice_id))
    correlation_id = result.scalar_one_or_none()
    if not correlation_id:
        raise HTTPException(status_code=404, detail="Invoice not found")

    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.correlation_id == correlation_id)
        .order_by(AuditLog.created_at)
    )
    entries = result.scalars().all()

    # Resolve actor names from control DB
    actor_ids = {e.actor_id for e in entries if e.actor_id}
    actor_names: dict[str, str] = {}
    if actor_ids:
        result = await control_db.execute(select(User).where(User.id.in_(actor_ids)))
        for u in result.scalars().all():
            actor_names[str(u.id)] = u.full_name

    # SOX access-control auditing: viewing the audit trail is itself an
    # auditable event. Write the view-event on its own (a GET has no business
    # transaction to ride) before returning.
    from app.services.audit_access import log_access

    await log_access(
        db,
        user=user,
        organization_id=user.organization_id,
        entity_type="audit",
        entity_id=invoice_id,
        correlation_id=correlation_id,
    )
    await db.commit()

    return [AuditLogEntryResponse.from_db(e, actor_names) for e in entries]


@router.get("/{invoice_id}/extraction")
async def get_extraction_results(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    result = await db.execute(
        select(InvoiceExtractionResult)
        .where(InvoiceExtractionResult.invoice_id == invoice_id)
        .order_by(InvoiceExtractionResult.created_at.desc())
    )
    results = result.scalars().all()

    return [
        {
            "id": str(r.id),
            "method": r.method,
            "confidence": float(r.confidence) if r.confidence else None,
            "raw_result": r.raw_result,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in results
    ]
