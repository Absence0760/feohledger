"""Customer-facing billing — current plan, status, usage-to-date (`/api/billing`).

FIRST SLICE: a single read endpoint, ``GET /api/billing/subscription``. It
returns the requesting tenant's live plan + subscription status + a usage rollup
for the current period. Plan-change, payment-method, and invoice-list endpoints
are later slices.

Auth-before-everything: behind JWT + ``require_roles(admin, cfo)`` (billing is a
finance/admin concern). The org is resolved from the tenant chokepoint
(``get_tenant``). ``Plan`` / ``Subscription`` are control-plane, so those reads
use ``get_control_db``; the usage METERS (``extraction_usage`` / ``card_rebates``)
are **tenant** tables — neither is in ``tenant_provisioning.CONTROL_TABLES``, so
neither exists in the control DB at all — and the rollup reads ``get_tenant_db``.
Money is serialised as exact decimal strings (never float) — this is a billing
surface where exactness is the point. See ``backend/docs/billing.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import ROLE_ADMIN, ROLE_CFO, require_roles
from app.config import settings
from app.database import get_control_db
from app.models.billing import Plan
from app.models.organization import Organization
from app.models.user import User
from app.schemas.money import OptionalExactMoneyInput
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.billing import (
    PlanChangeError,
    change_plan,
    current_period,
    get_active_subscription,
    rollup_usage,
)
from app.services.billing.ai_invoice_meter import (
    BILLING_SETTINGS_KEY,
    SPEND_CAP_KEY,
    AiUsageSummary,
    allowance_for_plan,
    parse_spend_cap,
    summarize,
)
from app.services.billing_adapters import get_billing_adapter
from app.tenant import get_tenant, get_tenant_db, lock_organization

router = APIRouter(prefix="/billing", tags=["billing"])


class PlanView(BaseModel):
    code: str
    name: str
    # Exact money as a decimal string — never float on a billing surface.
    monthly_price: str
    currency: str
    entitlements: dict
    trial_days: int


class SubscriptionView(BaseModel):
    status: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    trial_end: datetime | None
    # Whether a live provider subscription backs this (always False on mock).
    externally_managed: bool


class AiUsageView(BaseModel):
    """This month's AI-read invoices against the plan allowance (decisions §253).

    Money is an exact decimal STRING in ``currency``; counts are ints. Null
    ``included`` = the plan does not meter AI reads. Null
    ``overage_unit_price`` = the plan pauses at the limit instead of billing.
    """

    period: str
    used: int
    included: int | None
    overage_unit_price: str | None
    currency: str
    # Billable overage so far (clamped to the spending cap) and its amount.
    overage_units: int
    overage_amount: str
    # Overage at this month's daily pace, clamped to the cap.
    projected_overage_amount: str
    # The customer's monthly overage cap, or None for no cap.
    spend_cap: str | None
    # Whether the NEXT new AI read would be refused, and why.
    paused: bool
    pause_reason: str | None  # allowance_reached | spend_cap_reached


class BillingSummaryResponse(BaseModel):
    # Provider in effect for this org (per-org override → FEOH_BILLING_PROVIDER).
    provider: str
    # None when the org has no live subscription (e.g. never subscribed).
    plan: PlanView | None
    subscription: SubscriptionView | None
    period: str
    # Billable meters for the current period, as exact decimal strings.
    usage: dict[str, str]
    ai_usage: AiUsageView


def _ai_usage_view(summary: AiUsageSummary, *, currency: str) -> AiUsageView:
    allowance = summary.allowance
    return AiUsageView(
        period=summary.period,
        used=summary.used,
        included=allowance.included,
        overage_unit_price=(
            str(allowance.overage_unit_price) if allowance.overage_unit_price is not None else None
        ),
        currency=currency,
        overage_units=summary.overage_units,
        overage_amount=str(summary.overage_amount),
        projected_overage_amount=str(summary.projected_overage_amount),
        spend_cap=str(summary.cap) if summary.cap is not None else None,
        paused=summary.paused,
        pause_reason=summary.pause_reason,
    )


def _current_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _resolve_provider(org: Organization) -> str:
    """Per-org override (`settings.billing.provider`) → platform default."""
    billing = (org.settings or {}).get("billing") or {}
    return billing.get("provider") or settings.billing_provider


@router.get("/subscription", response_model=BillingSummaryResponse)
async def get_subscription(
    org: Organization = Depends(get_tenant),
    _user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
    control_db: AsyncSession = Depends(get_control_db),
    tenant_db: AsyncSession = Depends(get_tenant_db),
) -> BillingSummaryResponse:
    """Current plan + subscription status + usage-to-date for the tenant.

    Plans/subscriptions are control-plane (`control_db`); the usage meters
    (`extraction_usage` / `card_rebates`) are tenant-scoped, so the rollup reads
    `tenant_db`.
    """
    period = _current_period()
    active = await get_active_subscription(control_db, org.id)

    plan_view: PlanView | None = None
    sub_view: SubscriptionView | None = None
    if active is not None:
        subscription, plan = active
        plan_view = PlanView(
            code=plan.code,
            name=plan.name,
            monthly_price=str(plan.monthly_price),
            currency=plan.currency,
            entitlements=dict(plan.entitlements or {}),
            trial_days=plan.trial_days,
        )
        # Report the window the subscription is actually in, resolved by the
        # same rule `change_plan` persists and the dunning grace clock reads
        # (`services/billing/period.py`). Compute-on-read, no write: a row
        # created before that rule existed carries NULL bounds, and echoing
        # those told the customer their billing period was unknown while the
        # plan-change screen quietly prorated `0.00` off the same absence.
        window = current_period(subscription, now=datetime.now(UTC))
        sub_view = SubscriptionView(
            status=subscription.status,
            current_period_start=window.start,
            current_period_end=window.end,
            trial_end=subscription.trial_end,
            externally_managed=subscription.external_subscription_id is not None,
        )

    usage = await rollup_usage(tenant_db, organization_id=org.id, period=period)
    ai_summary = summarize(
        used=usage.ai_invoices,
        allowance=allowance_for_plan(active[1] if active else None),
        cap=parse_spend_cap(org.settings),
        now=datetime.now(UTC),
    )

    return BillingSummaryResponse(
        provider=_resolve_provider(org),
        plan=plan_view,
        subscription=sub_view,
        period=period,
        usage=usage.as_meters(),
        ai_usage=_ai_usage_view(ai_summary, currency=plan_view.currency if plan_view else "USD"),
    )


#: The largest cap the endpoint accepts — a guard against a typo'd extra digits,
#: not a commercial limit (a negotiated plan does not use the self-serve cap).
MAX_SPEND_CAP = Decimal("1000000.00")


class SpendCapRequest(BaseModel):
    # Exact decimal STRING in the plan's currency; null removes the cap. A JSON
    # number is refused (it is already a float by the time it arrives). Whole
    # cents between 0.00 and MAX_SPEND_CAP — an out-of-range value is a 422.
    monthly_spend_cap: OptionalExactMoneyInput = Field(
        default=None, ge=0, le=MAX_SPEND_CAP, max_digits=9, decimal_places=2
    )


class SpendCapResponse(BaseModel):
    monthly_spend_cap: str | None
    ai_usage: AiUsageView


@router.put("/spending-cap", response_model=SpendCapResponse)
async def set_spending_cap(
    body: SpendCapRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    control_db: AsyncSession = Depends(get_control_db),
    tenant_db: AsyncSession = Depends(get_tenant_db),
) -> SpendCapResponse:
    """Set or clear the org's monthly AI-read overage spending cap.

    Admin only — the cap decides when AI reading pauses for the whole org.
    ``0.00`` is a valid cap ("never bill overage": reading pauses at the plan
    allowance, like Free); ``null`` removes it. Stored on
    ``Organization.settings.billing.monthly_spend_cap`` as an exact decimal
    string. Every change writes a ``billing.spending_cap_updated`` audit row
    carrying only the old and new amounts — no person, no customer data.
    """
    cap = body.monthly_spend_cap
    if cap is not None:
        cap = cap.quantize(Decimal("0.01"))

    # Serialise with every other settings writer (`lock_organization`).
    org = await lock_organization(control_db, org)
    settings_dict = dict(org.settings or {})
    billing = dict(settings_dict.get(BILLING_SETTINGS_KEY) or {})
    previous = parse_spend_cap(settings_dict)
    if cap is None:
        billing.pop(SPEND_CAP_KEY, None)
    else:
        billing[SPEND_CAP_KEY] = str(cap)
    settings_dict[BILLING_SETTINGS_KEY] = billing
    org.settings = settings_dict
    flag_modified(org, "settings")

    if previous != cap:
        # Audit BEFORE the commit, as `change_plan` does, so the change is
        # never durable without an audit attempt.
        await dispatch_auth_audit(
            organization_id=org.id,
            actor_id=user.id,
            action="billing.spending_cap_updated",
            entity_id=org.id,
            entity_type="organization",
            details={
                "previous_cap": str(previous) if previous is not None else None,
                "new_cap": str(cap) if cap is not None else None,
            },
        )
    await control_db.commit()

    active = await get_active_subscription(control_db, org.id)
    plan = active[1] if active else None
    usage = await rollup_usage(tenant_db, organization_id=org.id, period=_current_period())
    summary = summarize(
        used=usage.ai_invoices,
        allowance=allowance_for_plan(plan),
        cap=cap,
        now=datetime.now(UTC),
    )
    return SpendCapResponse(
        monthly_spend_cap=str(cap) if cap is not None else None,
        ai_usage=_ai_usage_view(summary, currency=plan.currency if plan else "USD"),
    )


class PlansCatalogResponse(BaseModel):
    plans: list[PlanView]


@router.get("/plans", response_model=PlansCatalogResponse)
async def list_plans(
    _user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
    control_db: AsyncSession = Depends(get_control_db),
) -> PlansCatalogResponse:
    """The sellable plan catalog (active plans only), for the plan-change picker.

    admin/cfo only (matches the other billing routes). The catalog is global —
    not org-scoped — so this doesn't resolve a tenant, only the caller's
    control-plane identity/role. Ordered by price so the picker reads as a
    ladder. Money is an exact decimal string, same as every other billing view.
    """
    plans = (
        (
            await control_db.execute(
                select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.monthly_price)
            )
        )
        .scalars()
        .all()
    )
    return PlansCatalogResponse(
        plans=[
            PlanView(
                code=p.code,
                name=p.name,
                monthly_price=str(p.monthly_price),
                currency=p.currency,
                entitlements=dict(p.entitlements or {}),
                trial_days=p.trial_days,
            )
            for p in plans
        ]
    )


class PlanChangeRequest(BaseModel):
    # Target plan's stable machine code (Plan.code).
    plan_code: str = Field(min_length=1, max_length=50)


class ProrationView(BaseModel):
    # Net mid-period adjustment as an exact decimal STRING (never float):
    # positive = extra charge (upgrade), negative = credit (downgrade),
    # "0.00" = no change / same plan.
    amount: str
    unused_days: int
    period_days: int


class PlanChangeResponse(BaseModel):
    changed: bool
    old_plan_code: str
    new_plan_code: str
    proration: ProrationView


@router.post("/change-plan", response_model=PlanChangeResponse)
async def change_subscription_plan(
    body: PlanChangeRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
    control_db: AsyncSession = Depends(get_control_db),
) -> PlanChangeResponse:
    """Move the org's live subscription to another plan, prorated mid-period.

    admin/cfo only (matches the read endpoint). Idempotent: changing to the plan
    the org is already on is a successful no-op (``changed=false``, zero
    proration) — a retry can't double-charge. 404 when the org has no live
    subscription or the target plan is unknown/inactive. Every applied change
    writes an append-only ``billing.plan_changed`` audit row. Money in the
    response is an exact decimal string.
    """
    try:
        result = await change_plan(
            control_db, org=org, new_plan_code=body.plan_code, actor_id=user.id
        )
    except PlanChangeError as exc:
        # 404 (not 400): don't enumerate which plan codes / subscriptions exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plan change not available."
        ) from exc

    return PlanChangeResponse(
        changed=result.changed,
        old_plan_code=result.old_plan_code,
        new_plan_code=result.new_plan_code,
        proration=ProrationView(
            amount=str(result.proration.amount),
            unused_days=result.proration.unused_days,
            period_days=result.proration.period_days,
        ),
    )


class BillingInvoiceView(BaseModel):
    id: str
    number: str | None
    period: str | None
    # Exact money as a decimal string — never float on a billing surface.
    amount: str
    currency: str
    status: str  # paid | open | void
    hosted_url: str | None
    created_at: str | None


class BillingInvoicesResponse(BaseModel):
    provider: str
    invoices: list[BillingInvoiceView]


def _resolve_customer_id(org: Organization) -> str | None:
    """The provider-side customer id persisted on `settings.billing`, if any.

    `None` when the org was never provisioned with the billing provider — the
    adapter then has nothing to list and returns an empty list (not a 500).
    """
    billing = (org.settings or {}).get("billing") or {}
    return billing.get("stripe_customer_id")


@router.get("/invoices", response_model=BillingInvoicesResponse)
async def list_billing_invoices(
    org: Organization = Depends(get_tenant),
    _user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
) -> BillingInvoicesResponse:
    """The org's past platform-billing invoices / receipts (newest first).

    admin/cfo only (matches `GET /subscription`). Sourced through the org's
    billing adapter (`mock` locally → deterministic synthetic receipts;
    `stripe_billing` → the org's Stripe invoices). Graceful degradation: an org
    never provisioned with the provider (no customer id) — or an unconfigured /
    unavailable provider — yields an empty list, never a 500. Money is an exact
    decimal string (this is a billing surface — exactness is the point).
    """
    provider = _resolve_provider(org)
    adapter = get_billing_adapter(provider)
    customer_id = _resolve_customer_id(org)
    try:
        provider_invoices = await adapter.list_invoices(customer_id=customer_id)
    except Exception:  # noqa: BLE001
        # The live provider fails closed (no key) or is unreachable — surface an
        # empty list rather than a 500. PII-free: no provider detail is echoed.
        provider_invoices = []

    return BillingInvoicesResponse(
        provider=provider,
        invoices=[
            BillingInvoiceView(
                id=inv.external_invoice_id,
                number=inv.number,
                period=inv.period,
                amount=inv.amount,
                currency=inv.currency,
                status=inv.status,
                hosted_url=inv.hosted_url,
                created_at=inv.created_at,
            )
            for inv in provider_invoices
        ],
    )


class SetupIntentResponse(BaseModel):
    provider: str
    # True once a SetupIntent could be started (org provisioned + provider
    # configured). False → `client_secret` is None and the UI shows a clear
    # "billing not configured" state rather than an error.
    configured: bool
    # Single-use secret the frontend confirms the card with (via the provider's
    # JS SDK). None when not configured. NEVER a long-lived secret or a PAN.
    client_secret: str | None
    setup_intent_id: str | None


@router.post("/payment-method/setup-intent", response_model=SetupIntentResponse)
async def create_payment_method_setup_intent(
    org: Organization = Depends(get_tenant),
    _user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
) -> SetupIntentResponse:
    """Start a SetupIntent so the org can add or replace a saved card.

    admin/cfo only (matches the other billing routes). Returns the provider's
    single-use `client_secret`; the frontend confirms the card against it with
    the provider's JS SDK — no charge, no PAN ever touches our backend. Graceful
    degradation: an org never provisioned with the provider (no customer id) — or
    an unconfigured / unavailable provider — yields `configured=false` with a
    null `client_secret`, never a 500.
    """
    provider = _resolve_provider(org)
    adapter = get_billing_adapter(provider)
    customer_id = _resolve_customer_id(org)
    try:
        intent = await adapter.create_setup_intent(customer_id)
    except Exception:  # noqa: BLE001
        # The live provider fails closed (no key) or is unreachable — surface a
        # not-configured shape rather than a 500. PII-free: no provider detail.
        intent = None

    return SetupIntentResponse(
        provider=provider,
        configured=intent is not None,
        client_secret=intent.client_secret if intent else None,
        setup_intent_id=intent.external_setup_intent_id if intent else None,
    )


class PaymentMethodView(BaseModel):
    id: str
    # PII-safe card metadata ONLY — brand / last4 / expiry. NEVER a full PAN.
    brand: str | None
    last4: str | None
    exp_month: int | None
    exp_year: int | None
    is_default: bool


class PaymentMethodsResponse(BaseModel):
    provider: str
    payment_methods: list[PaymentMethodView]


@router.get("/payment-methods", response_model=PaymentMethodsResponse)
async def list_payment_methods(
    org: Organization = Depends(get_tenant),
    _user: User = Depends(require_roles(ROLE_ADMIN, ROLE_CFO)),
) -> PaymentMethodsResponse:
    """The org's saved cards — PII-safe metadata only (brand / last4 / expiry).

    admin/cfo only. NEVER returns or logs a full card number. Graceful
    degradation: an org never provisioned with the provider (no customer id) — or
    an unconfigured / unavailable provider — yields an empty list, never a 500.
    """
    provider = _resolve_provider(org)
    adapter = get_billing_adapter(provider)
    customer_id = _resolve_customer_id(org)
    try:
        methods = await adapter.list_payment_methods(customer_id)
    except Exception:  # noqa: BLE001
        methods = []

    return PaymentMethodsResponse(
        provider=provider,
        payment_methods=[
            PaymentMethodView(
                id=pm.external_payment_method_id,
                brand=pm.brand,
                last4=pm.last4,
                exp_month=pm.exp_month,
                exp_year=pm.exp_year,
                is_default=pm.is_default,
            )
            for pm in methods
        ],
    )
