"""Recurring / subscription invoice template endpoints.

CRUD + lifecycle (pause / resume / end) for
:class:`~app.models.recurring_invoice.RecurringInvoiceTemplate`, plus the
manual ``generate-now`` (idempotent), a projected upcoming schedule, and the
generated-invoice history.

All money is ``Decimal`` end-to-end; every mutation is RBAC-gated and writes an
audit row; reads are entity-scoped (multi-entity). The scheduling /
generation / variance math is shared verbatim with the background sweep via
``app.services.recurring_invoices``. See ``backend/docs/recurring-invoices.md``.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_roles,
)
from app.models.invoice import Invoice
from app.models.recurring_invoice import (
    STATUS_ACTIVE,
    STATUS_ENDED,
    STATUS_PAUSED,
    RecurringInvoiceTemplate,
)
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.recurring_invoice import (
    GeneratedHistoryResponse,
    GeneratedInvoiceItem,
    RecurringCurrencyTotal,
    RecurringTemplateCreate,
    RecurringTemplateListResponse,
    RecurringTemplateResponse,
    RecurringTemplateSummaryResponse,
    RecurringTemplateUpdate,
    ScheduleOccurrence,
    UpcomingScheduleResponse,
)
from app.services import recurring_invoices as svc
from app.services.audit_dispatch import dispatch_audit
from app.services.gl_chart import refuse_foreign_gl_codes
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_tenant_db,
    get_write_entity_id,
)
from app.utils.dates import utc_today
from app.utils.search import ilike_contains

router = APIRouter(prefix="/recurring", tags=["recurring"])

_READ_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)
_WRITE_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER)

# Fields whose change re-anchors `next_run_on` for an active template.
_SCHEDULE_FIELDS = {"cadence", "day_of_period", "start_date"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _resolve_vendor_name(db: AsyncSession, vendor_id: uuid.UUID | None) -> str | None:
    if vendor_id is None:
        return None
    return (
        await db.execute(select(Vendor.name).where(Vendor.id == vendor_id))
    ).scalar_one_or_none()


async def _get_scoped(
    db: AsyncSession, template_id: uuid.UUID, entity_id: uuid.UUID | None
) -> RecurringInvoiceTemplate:
    q = apply_entity_scope(
        select(RecurringInvoiceTemplate).where(RecurringInvoiceTemplate.id == template_id),
        RecurringInvoiceTemplate,
        entity_id,
    )
    template = (await db.execute(q)).scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="Recurring template not found")
    return template


def _seed_next_run_on(template: RecurringInvoiceTemplate, *, after: date) -> None:
    """(Re)compute and stamp ``next_run_on`` from the template's own fields."""
    template.next_run_on = svc.compute_next_run_on(
        template.cadence,
        template.day_of_period,
        after=after,
        start_date=template.start_date,
        end_date=template.end_date,
    )


# --------------------------------------------------------------------------- #
# List / create / detail
# --------------------------------------------------------------------------- #


def _recurring_list_filters(
    query,
    *,
    status_filter: str | None,
    vendor_id: uuid.UUID | None,
    search: str | None,
):
    """Apply the recurring-template ``status`` / ``vendor_id`` / free-text
    filters to ``query``.

    Shared by ``GET /api/recurring`` and ``GET /api/recurring/summary`` so the
    KPI rollup can never describe a different set than the rows it sits above.
    Entity scope is applied by the caller.
    """
    if status_filter:
        query = query.where(RecurringInvoiceTemplate.status == status_filter)
    if vendor_id:
        query = query.where(RecurringInvoiceTemplate.vendor_id == vendor_id)
    if search and search.strip():
        term = search.strip()
        query = query.where(
            or_(
                ilike_contains(RecurringInvoiceTemplate.name, term),
                ilike_contains(RecurringInvoiceTemplate.vendor_name, term),
            )
        )
    return query


@router.get("", response_model=RecurringTemplateListResponse)
async def list_templates(
    status_filter: str | None = Query(None, alias="status"),
    vendor_id: uuid.UUID | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    query = _recurring_list_filters(
        apply_entity_scope(select(RecurringInvoiceTemplate), RecurringInvoiceTemplate, entity_id),
        status_filter=status_filter,
        vendor_id=vendor_id,
        search=search,
    )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    query = (
        query.order_by(
            RecurringInvoiceTemplate.created_at.desc(), RecurringInvoiceTemplate.id.desc()
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list((await db.execute(query)).scalars().all())
    return RecurringTemplateListResponse(
        items=[RecurringTemplateResponse.from_db(t) for t in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# Literal `/summary` declared BEFORE `/{template_id}` so it isn't captured as a
# {template_id} UUID.
@router.get("/summary", response_model=RecurringTemplateSummaryResponse)
async def template_summary(
    status_filter: str | None = Query(None, alias="status"),
    vendor_id: uuid.UUID | None = None,
    search: str | None = None,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Whole-set status counts + per-currency monthly-equivalent recurring spend
    + the soonest upcoming run.

    Takes the SAME filters as ``GET /api/recurring`` through the shared
    ``_recurring_list_filters``. The page derived ``activeCount`` /
    ``soonestNextRun`` / ``monthlyRecurringTotal`` from the LOADED page and did
    the cadence-normalisation division in float. Here each template's monthly
    figure is an exact Postgres numeric (``amount`` / {1, 3, 12}); those exact
    quotients are summed per currency and the SUM is quantised once to 2dp
    (``ROUND_HALF_UP``) — never across currencies, and never rounded per
    template. Rounding first would drop up to half a cent per row: three
    100.00 annual templates are 25.00 a month, not 3 x 8.33 = 24.99.
    """
    tmpl = RecurringInvoiceTemplate

    status_rows = (
        await db.execute(
            _recurring_list_filters(
                apply_entity_scope(
                    select(tmpl.status, func.count()).select_from(tmpl), tmpl, entity_id
                ),
                status_filter=status_filter,
                vendor_id=vendor_id,
                search=search,
            ).group_by(tmpl.status)
        )
    ).all()
    by_status = {str(s): int(n) for s, n in status_rows}

    # Monthly-equivalent spend, ACTIVE templates with a set amount only. Divisor
    # per cadence; the division stays in Postgres numeric (exact), quantised in
    # Python.
    divisor = case(
        (tmpl.cadence == "quarterly", 3),
        (tmpl.cadence == "annual", 12),
        else_=1,
    )
    currency_key = func.upper(tmpl.currency)
    monthly_rows = (
        await db.execute(
            _recurring_list_filters(
                apply_entity_scope(
                    select(
                        currency_key,
                        func.coalesce(func.sum(tmpl.amount / divisor), 0),
                        func.count(),
                    )
                    .select_from(tmpl)
                    .where(tmpl.status == STATUS_ACTIVE, tmpl.amount.is_not(None)),
                    tmpl,
                    entity_id,
                ),
                status_filter=status_filter,
                vendor_id=vendor_id,
                search=search,
            )
            .group_by(currency_key)
            .order_by(currency_key)
        )
    ).all()

    soonest = (
        await db.execute(
            _recurring_list_filters(
                apply_entity_scope(
                    select(func.min(tmpl.next_run_on))
                    .select_from(tmpl)
                    .where(tmpl.status == STATUS_ACTIVE, tmpl.next_run_on.is_not(None)),
                    tmpl,
                    entity_id,
                ),
                status_filter=status_filter,
                vendor_id=vendor_id,
                search=search,
            )
        )
    ).scalar()

    return RecurringTemplateSummaryResponse(
        total=sum(by_status.values()),
        by_status=by_status,
        monthly_equivalent=[
            RecurringCurrencyTotal(
                currency=str(currency or "").upper() or "USD",
                total=str(Decimal(monthly).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                count=int(n),
            )
            for currency, monthly, n in monthly_rows
        ],
        soonest_next_run=soonest.isoformat() if soonest else None,
    )


@router.post("", response_model=RecurringTemplateResponse, status_code=status.HTTP_201_CREATED)
async def create_template(
    body: RecurringTemplateCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID = Depends(get_write_entity_id),
):
    vendor_uuid = uuid.UUID(body.vendor_id) if body.vendor_id else None
    vendor_name = await _resolve_vendor_name(db, vendor_uuid)
    # Every invoice this template raises is coded with this string and lands
    # under the template's entity, so the code has to resolve in THAT entity's
    # chart — refused here, once, rather than on each generated invoice.
    await refuse_foreign_gl_codes(
        db, organization_id=org_id, entity_id=entity_id, codes=[body.gl_account]
    )

    template = RecurringInvoiceTemplate(
        organization_id=org_id,
        entity_id=entity_id,
        name=body.name,
        vendor_id=vendor_uuid,
        vendor_name=vendor_name,
        description=body.description,
        amount=body.amount,
        currency=(body.currency or "USD").upper(),
        gl_account=body.gl_account,
        cost_center=body.cost_center,
        department=body.department,
        project=body.project,
        po_number=body.po_number,
        payment_terms=body.payment_terms,
        cadence=body.cadence.value,
        day_of_period=body.day_of_period,
        start_date=body.start_date,
        end_date=body.end_date,
        variance_tolerance_pct=body.variance_tolerance_pct,
        notes=body.notes,
        status=STATUS_ACTIVE,
        # The employee whose standing instruction this is. Segregation of
        # duties keys on it: the background sweep has no live actor, so
        # `generate_one` falls back to this column when stamping the generated
        # invoice's `uploaded_by_id` — without it the author could approve the
        # invoice their own template raised, exempt exactly as a legacy row is.
        created_by_user_id=user.id,
    )
    # First occurrence on/after start_date matching day_of_period.
    _seed_next_run_on(template, after=body.start_date)
    db.add(template)
    await db.flush()
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="recurring_template.created",
        entity_type="recurring_invoice_template",
        entity_id=template.id,
        details={"name": template.name, "cadence": template.cadence},
    )
    await db.commit()
    await db.refresh(template)
    return RecurringTemplateResponse.from_db(template)


@router.get("/{template_id}", response_model=RecurringTemplateResponse)
async def get_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    template = await _get_scoped(db, template_id, entity_id)
    return RecurringTemplateResponse.from_db(template)


@router.patch("/{template_id}", response_model=RecurringTemplateResponse)
async def update_template(
    template_id: uuid.UUID,
    body: RecurringTemplateUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    template = await _get_scoped(db, template_id, entity_id)
    data = body.model_dump(exclude_unset=True)

    new_gl = data.get("gl_account")
    if new_gl and new_gl != template.gl_account:
        await refuse_foreign_gl_codes(
            db,
            organization_id=template.organization_id,
            entity_id=template.entity_id,
            codes=[new_gl],
        )

    changed: list[str] = []
    for field, value in data.items():
        if field == "vendor_id":
            new_vendor = uuid.UUID(value) if value else None
            if new_vendor != template.vendor_id:
                template.vendor_id = new_vendor
                template.vendor_name = await _resolve_vendor_name(db, new_vendor)
                changed.append("vendor_id")
            continue
        if field == "cadence":
            value = value.value if hasattr(value, "value") else value
        if field == "currency" and value:
            value = value.upper()
        if getattr(template, field) != value:
            setattr(template, field, value)
            changed.append(field)

    # Recompute next_run_on if a schedule-shaping field changed AND the template
    # is still active (paused/ended templates don't carry a live cursor).
    if template.status == STATUS_ACTIVE and (_SCHEDULE_FIELDS & set(changed)):
        _seed_next_run_on(template, after=max(utc_today(), template.start_date))

    # Segregation of duties: whoever changed a TERM of the payable (vendor,
    # amount, currency, GL coding, schedule — `MATERIAL_EDIT_FIELDS`) has shaped
    # every invoice this template goes on to raise, as completely as its author
    # did, and must not also approve one. `generate_one` carries the set onto
    # `Invoice.segregation_actor_ids`.
    #
    # Keyed off `changed`, so a PATCH that re-sends a field with its existing
    # value implicates nobody — an idempotent save is not an edit. Cosmetic
    # fields (`name`, `notes`, …) are excluded on purpose: implicating a
    # typo-fixer costs the control its credibility and closes no hole.
    material = svc.material_edit_fields(changed)
    if material:
        svc.record_material_editor(template, user.id)

    # This edit is how an operator fixes a template the sweep couldn't generate
    # from; once the reason no longer holds, the skip marker is stale and its
    # count must not carry into a future miss. Still-missing fields keep it.
    if changed:
        svc.clear_generation_skip_if_resolved(template)

    if changed:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="recurring_template.updated",
            entity_type="recurring_invoice_template",
            entity_id=template.id,
            # `material` says WHY the editor joined the template's implicated-actor
            # set — without it the trail shows a wider segregation block on the
            # generated invoices with nothing explaining which edit earned it.
            details={"changed": sorted(changed), "material": sorted(material)},
        )
    await db.commit()
    await db.refresh(template)
    return RecurringTemplateResponse.from_db(template)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    template = await _get_scoped(db, template_id, entity_id)
    generated = (
        await db.execute(select(func.count()).where(Invoice.recurring_template_id == template.id))
    ).scalar() or 0
    if generated > 0:
        raise HTTPException(
            status_code=409,
            detail="Template has generated invoices; end it instead of deleting.",
        )
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="recurring_template.deleted",
        entity_type="recurring_invoice_template",
        entity_id=template.id,
        details={"name": template.name},
    )
    await db.delete(template)
    await db.commit()


# --------------------------------------------------------------------------- #
# Lifecycle — pause / resume / end
# --------------------------------------------------------------------------- #


async def _audit_lifecycle(
    db: AsyncSession,
    template: RecurringInvoiceTemplate,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
) -> None:
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=actor_id,
        action=action,
        entity_type="recurring_invoice_template",
        entity_id=template.id,
        details={"status": template.status},
    )


@router.post("/{template_id}/pause", response_model=RecurringTemplateResponse)
async def pause_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    template = await _get_scoped(db, template_id, entity_id)
    if template.status != STATUS_ACTIVE:
        raise HTTPException(status_code=409, detail="Only an active template can be paused")
    template.status = STATUS_PAUSED
    await _audit_lifecycle(db, template, org_id, user.id, "recurring_template.paused")
    await db.commit()
    await db.refresh(template)
    return RecurringTemplateResponse.from_db(template)


@router.post("/{template_id}/resume", response_model=RecurringTemplateResponse)
async def resume_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    template = await _get_scoped(db, template_id, entity_id)
    if template.status != STATUS_PAUSED:
        raise HTTPException(status_code=409, detail="Only a paused template can be resumed")
    template.status = STATUS_ACTIVE
    # Re-anchor from today so resuming never back-fires every historic period
    # the template slept through.
    _seed_next_run_on(template, after=max(utc_today(), template.start_date))
    # Resuming a template the SWEEP auto-paused is the operator saying it's
    # fixed — but only clear the marker if it actually is. A template resumed
    # still missing its vendor keeps it (and will re-trip the pause), because
    # the reason it names is still true.
    svc.clear_generation_skip_if_resolved(template)
    await _audit_lifecycle(db, template, org_id, user.id, "recurring_template.resumed")
    await db.commit()
    await db.refresh(template)
    return RecurringTemplateResponse.from_db(template)


@router.post("/{template_id}/end", response_model=RecurringTemplateResponse)
async def end_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    template = await _get_scoped(db, template_id, entity_id)
    if template.status == STATUS_ENDED:
        raise HTTPException(status_code=409, detail="Template is already ended")
    template.status = STATUS_ENDED
    template.next_run_on = None  # terminal — nothing pending
    await _audit_lifecycle(db, template, org_id, user.id, "recurring_template.ended")
    await db.commit()
    await db.refresh(template)
    return RecurringTemplateResponse.from_db(template)


# --------------------------------------------------------------------------- #
# Generate-now (idempotent) / upcoming schedule / history
# --------------------------------------------------------------------------- #


@router.post("/{template_id}/generate-now", status_code=status.HTTP_201_CREATED)
async def generate_now(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_WRITE_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    template = await _get_scoped(db, template_id, entity_id)
    if template.status == STATUS_ENDED:
        raise HTTPException(status_code=409, detail="Cannot generate from an ended template")
    # Same verdict the sweep reaches — one condition, so a manual generate-now
    # can never disagree with what the background sweep decided about this
    # template (and skipped a period over).
    if svc.not_generatable_reason(template) is not None:
        raise HTTPException(
            status_code=422,
            detail="Template needs a vendor and amount before it can generate an invoice",
        )

    # The current due period — a pure function of (schedule, today), NOT the
    # mutable next_run_on cursor, so a re-call on the same day always resolves
    # to the SAME period and short-circuits to the existing invoice (idempotent
    # regardless of how far the sweep has since advanced the cursor).
    run_on = svc.current_due_run_on(
        template.cadence,
        template.day_of_period,
        today=utc_today(),
        start_date=template.start_date,
    )
    period_key = svc.period_key_for(template.cadence, run_on)

    # Pre-check for an already-generated period → idempotent 200, no duplicate.
    existing = (
        await db.execute(
            select(Invoice).where(
                Invoice.recurring_template_id == template.id,
                Invoice.recurring_period_key == period_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # This period is already satisfied and the guard above proved the
        # template is generatable, so a leftover skip marker is stale. The
        # idempotent branch never reaches `generate_one`'s own clear, and a
        # stale `consecutive` would make the next single sweep miss trip the
        # auto-pause meant for three.
        svc.clear_generation_skip(template)
        await db.commit()
        return Response(
            content=_generate_payload(existing, period_key),
            media_type="application/json",
            status_code=200,
        )

    invoice = await svc.generate_one(db, template, run_on=run_on, actor_id=user.id)
    if invoice is None:
        raise HTTPException(status_code=422, detail="Template could not generate an invoice")
    await db.commit()
    await db.refresh(invoice)
    return Response(
        content=_generate_payload(invoice, invoice.recurring_period_key or period_key),
        media_type="application/json",
        status_code=201,
    )


def _generate_payload(invoice: Invoice, period_key: str) -> str:
    return json.dumps(
        {
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "period_key": period_key,
            "status": invoice.status.value
            if hasattr(invoice.status, "value")
            else str(invoice.status),
        }
    )


@router.get("/{template_id}/upcoming-schedule", response_model=UpcomingScheduleResponse)
async def upcoming_schedule(
    template_id: uuid.UUID,
    count: int = Query(6, ge=1, le=60),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    template = await _get_scoped(db, template_id, entity_id)
    occurrences = svc.project_schedule(template, count=count)
    return UpcomingScheduleResponse(
        template_id=str(template.id),
        occurrences=[
            ScheduleOccurrence(
                period_key=pk,
                run_on=run_on.isoformat(),
                amount=template.amount,
                currency=template.currency,
            )
            for pk, run_on in occurrences
        ],
    )


@router.get("/{template_id}/history", response_model=GeneratedHistoryResponse)
async def template_history(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_READ_ROLES)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    template = await _get_scoped(db, template_id, entity_id)
    rows = list(
        (
            await db.execute(
                select(Invoice)
                .where(Invoice.recurring_template_id == template.id)
                .order_by(Invoice.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    items = [
        GeneratedInvoiceItem(
            invoice_id=str(inv.id),
            invoice_number=inv.invoice_number,
            period_key=inv.recurring_period_key,
            amount=inv.amount,
            currency=inv.currency,
            status=inv.status.value if hasattr(inv.status, "value") else str(inv.status),
            created_at=inv.created_at.isoformat() if inv.created_at else "",
        )
        for inv in rows
    ]
    return GeneratedHistoryResponse(template_id=str(template.id), items=items, total=len(items))
