"""Dashboard aggregation endpoints — rich KPIs for the main page."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, get_current_user, holds_any_role, require_roles
from app.database import get_control_db
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.user import User
from app.schemas.dashboard import DashboardResponse, OnboardingResponse
from app.services import dashboard_metrics as metrics
from app.services.currency_conversion import resolve_reporting_currency
from app.services.onboarding import compute_onboarding
from app.tenant import get_entity_id, get_tenant, get_tenant_db
from app.utils.dates import utc_today

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/onboarding", response_model=OnboardingResponse)
async def get_onboarding(
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    _user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """Setup progress for `/organization`'s Getting started panel.

    The same derivation the dashboard's checklist reads (`services/onboarding`),
    served alone so the settings page does not compute a whole dashboard for
    seven booleans. Admin-only: it describes the tenant's configuration.
    """
    progress = await compute_onboarding(db=db, control_db=control_db, org=org)
    return {
        **progress.as_dict(),
        "organization_has_invoices": (await db.execute(select(Invoice.id).limit(1))).first()
        is not None,
    }


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(get_current_user),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Every figure is computed in `services/dashboard_metrics`, one function
    per section, awaited here in a fixed order (that module's docstring says
    why the order matters). This handler resolves the scope, calls the
    sections and assembles the response."""
    s = metrics.DashboardScope(
        db=db,
        entity_id=entity_id,
        today=utc_today(),
        reporting_currency=resolve_reporting_currency(org.settings),
    )
    total_invoices, total_amount = await metrics.invoice_totals(s)
    reporting = await metrics.reporting_rollup(s)
    pipeline = await metrics.pipeline(s)
    vendor_spend = await metrics.vendor_spend(s)
    aging, aging_reporting = await metrics.aging(s)
    monthly_trend = await metrics.monthly_trend(s)
    upcoming = await metrics.upcoming_payments(s)
    due = await metrics.overdue_and_due_soon(s)
    touchless_rate = await metrics.touchless_rate(s)
    payments = await metrics.payment_totals(s)
    total_rebates, excluded_rebate_count = await metrics.rebates(s)
    stale_approvals = await metrics.stale_approvals(s)
    open_exceptions = await metrics.open_exceptions(s)
    processing_time = await metrics.processing_time(s)
    approval_bottleneck = await metrics.approval_bottleneck(s)
    discount_capture = await metrics.discount_capture(s)

    # First-run state. Emptiness is decided org-wide (see the helper), and the
    # setup checklist is computed only for the one reader it is for: an admin
    # on a workspace with no invoices yet. Everyone else gets `null` and pays
    # for none of its queries (`docs/decisions.md` §315).
    organization_has_invoices = await metrics.organization_has_invoices(
        db, entity_id, total_invoices
    )
    onboarding = None
    if not organization_has_invoices and holds_any_role(user, (ROLE_ADMIN,)):
        onboarding = (await compute_onboarding(db=db, control_db=control_db, org=org)).as_dict()

    return {
        "total_invoices": total_invoices,
        "organization_has_invoices": organization_has_invoices,
        "onboarding": onboarding,
        "total_amount": total_amount,
        # Currency-aware rollup of the whole invoice book into ONE reporting
        # currency, plus the per-currency split so the UI can show the mix.
        "reporting": reporting,
        # `total_paid` / `total_pending`, their reporting-currency counterparts
        # and the counts each counterpart left out (`payment_totals`).
        **payments,
        "total_rebates": total_rebates,
        # Rebates left out of `total_rebates` for being denominated in another
        # currency, so the KPI can say it describes part of the set rather than
        # looking complete — the same disclosure the two payment counts above
        # provide for their own figures.
        "excluded_rebate_count": excluded_rebate_count,
        "open_exceptions": open_exceptions,
        "touchless_rate": touchless_rate,
        "stale_approvals": stale_approvals,
        "pipeline": pipeline,
        "vendor_spend": vendor_spend,
        "aging": aging,
        # Reporting-currency counterpart of `aging` (see `metrics.aging`).
        "aging_reporting": aging_reporting,
        "monthly_trend": monthly_trend,
        # `upcoming_payments` and its three totals.
        **upcoming,
        # `overdue_count` / `overdue_total` / `due_soon_count` / `due_soon_total`.
        **due,
        "processing_time": processing_time,
        "approval_bottleneck": approval_bottleneck,
        "discount_capture": discount_capture,
    }
