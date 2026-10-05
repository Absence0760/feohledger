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
    get_current_user,
    get_org_id,
    require_permission,
    require_roles,
)
from app.api.file_proxy import serve_owned_file
from app.api.permissions import PERM_INVOICE_APPROVE
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


# ---------- Stage 1: Upload ----------


@router.post("/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_invoice(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
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
            await dispatch_extraction(invoice.id, org_id, user.id)
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
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Manually trigger or re-trigger extraction on an invoice.

    Works on invoices in 'new' or 'failed' status that have a file attached.
    """
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)

    if invoice.status not in (InvoiceStatus.new, InvoiceStatus.failed):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot extract from '{invoice.status.value}' status. Must be 'new' or 'failed'."
            ),
        )

    if not invoice.file_key:
        raise HTTPException(
            status_code=400, detail="No file attached to this invoice. Upload a file first."
        )

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

    await dispatch_extraction(invoice.id, org_id, user.id)

    return {
        "id": str(invoice.id),
        "status": invoice.status.value,
        "message": "Extraction triggered. Fields will be populated shortly.",
    }


@router.post("/{invoice_id}/reset-extraction")
async def reset_extraction(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Reset a stuck extraction — moves invoice from 'pending' back to 'new'."""
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)

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
    corrections = body.model_dump(exclude_unset=True) if body else None

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
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)

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
):
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
):
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
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Advance an invoice to the next logical step based on the workflow.

    - new + approval enabled → ready_for_review
    - new + no approval → done
    - approved + ERP enabled → triggers ERP dispatch
    - approved + no ERP → done
    """
    await ensure_in_entity_scope(db, Invoice, invoice_id, entity_id, detail="Invoice not found")
    invoice = await get_invoice_for_update(db, invoice_id)

    # Validate required fields
    missing = []
    if not invoice.vendor_name or not invoice.vendor_name.strip():
        missing.append("vendor")
    if not invoice.invoice_number or not invoice.invoice_number.strip():
        missing.append("invoice_number")
    if invoice.amount is None or invoice.amount <= 0:
        missing.append("amount")
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Required fields missing: {', '.join(missing)}",
        )

    # Check workflow config for this invoice
    approval_enabled = await is_step_enabled(db, org_id, "approval", invoice_id=invoice.id)
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
            reporting_gate_amount,
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
        # submission — a second pair of eyes still signs off.
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
        if decide_auto_approve(
            {},  # no extraction result here — see the note above
            approval_config,
            overall_confidence=0.0,
            amount=gate_amount,
            aggregate_amount=reporting_gate_amount(
                invoice, amount=gate_aggregate, org_settings=org.settings
            ),
        ) and not violates_segregation(invoice, user.id, approval_config):
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
