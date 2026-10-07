"""Default plan catalog + baseline Subscription provisioning (control plane).

Before this module, nothing in the app ever created a `Plan` or `Subscription`
row outside of tests (every test exercising `/api/v1` or `/api/billing` had to
hand-seed both). Two consequences: `require_entitlement` /
`require_api_entitlement` fail closed to `{}` for every org (issue #180 — the
public Developer API 402s for every org, forever, with no way out), and
`services/billing/plan_change.py::change_plan` 404s with "no live subscription"
for every org, so an admin could never even upgrade out of it.

`ensure_plan_catalog` + `ensure_subscription` fix this at the two places a
tenant comes into being: `services/tenant_provisioning.py` (CLI + self-service
signup) and `scripts/seed.py` (the demo tenants). Both are idempotent — safe to
call on every provision, never duplicates a plan by `code` or creates a second
live subscription for an org.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import Plan, Subscription
from app.services.billing.period import add_months

# ---------------------------------------------------------------------------
# Feature entitlements (decisions §253). One name per gated capability; a plan
# grants a feature by carrying its key with a truthy value. These constants are
# the single spelling — gates call ``require_entitlement(FEATURE_SSO)``, never a
# string literal, so a typo cannot silently fail a gate closed.
# ---------------------------------------------------------------------------
FEATURE_PUBLIC_API = "public_api"  # /api/v1 + API keys + outbound webhooks
FEATURE_ERP_INTEGRATIONS = "erp_integrations"  # live ERP adapters (mock stays open)
FEATURE_SSO = "sso"  # OIDC + SAML sign-in
FEATURE_SCIM = "scim"  # SCIM 2.0 user provisioning
FEATURE_SSO_ENFORCEMENT = "sso_enforcement"  # "require SSO" for every user
FEATURE_MULTI_ENTITY = "multi_entity"  # more than the one default entity
FEATURE_AUDIT_SIEM_EXPORT = "audit_siem_export"  # audit-log shipping to a SIEM

ALL_FEATURES: tuple[str, ...] = (
    FEATURE_PUBLIC_API,
    FEATURE_ERP_INTEGRATIONS,
    FEATURE_SSO,
    FEATURE_SCIM,
    FEATURE_SSO_ENFORCEMENT,
    FEATURE_MULTI_ENTITY,
    FEATURE_AUDIT_SIEM_EXPORT,
)

# ---------------------------------------------------------------------------
# Usage components (decisions §253). The one metered unit is an **AI-read
# invoice**: a distinct invoice whose extraction ran on the PLATFORM's model
# key and succeeded, in a calendar month (UTC). BYOK extractions are the
# customer's own model bill and never count; structured e-invoices (PEPPOL,
# UBL, CSV import) use no model and never count; re-reading the same invoice in
# the same month counts once.
#
#   included            — AI-read invoices covered by the monthly price.
#   overage_unit_price  — decimal string charged per AI-read invoice past
#                         ``included``, or ``None`` for "no overage: AI reading
#                         pauses at the limit" (the Free tier). Pausing never
#                         blocks anything else — manual entry, approval and
#                         payment recording keep working.
# ---------------------------------------------------------------------------
METER_AI_INVOICES = "ai_invoices"

# Every catalog plan is priced in this currency. Named once so the public
# pricing page (generated from this module by
# ``scripts/gen_pricing_catalog.py``) states the same code the rows carry.
CATALOG_CURRENCY = "USD"

# Stable machine codes referenced throughout backend/docs/billing.md. `free` is
# the default every new tenant lands on. Enterprise is not a catalog plan: it is
# a negotiated contract an operator sets up per customer, so the pricing page
# shows it as "contact sales" rather than rendering it from here.
DEFAULT_PLAN_CATALOG: tuple[dict, ...] = (
    {
        "code": "free",
        "name": "Free",
        "monthly_price": Decimal("0.00"),
        "entitlements": {},
        "usage_components": {
            METER_AI_INVOICES: {"included": 100, "overage_unit_price": None},
        },
        "trial_days": 0,
    },
    {
        "code": "growth",
        "name": "Growth",
        "monthly_price": Decimal("49.00"),
        "entitlements": {
            FEATURE_PUBLIC_API: True,
            FEATURE_ERP_INTEGRATIONS: True,
            FEATURE_SSO: True,
        },
        "usage_components": {
            METER_AI_INVOICES: {"included": 500, "overage_unit_price": "0.10"},
        },
        "trial_days": 14,
    },
    {
        "code": "scale",
        "name": "Scale",
        "monthly_price": Decimal("199.00"),
        "entitlements": {feature: True for feature in ALL_FEATURES},
        "usage_components": {
            METER_AI_INVOICES: {"included": 3000, "overage_unit_price": "0.07"},
        },
        "trial_days": 14,
    },
)


async def ensure_plan_catalog(session: AsyncSession) -> dict[str, Plan]:
    """Idempotently create any :data:`DEFAULT_PLAN_CATALOG` entry missing by
    `code`. Never touches a plan that already exists — an operator who has
    since edited a plan's price/entitlements keeps their edits; this only
    fills in gaps on a fresh control DB. Returns every catalog plan (existing
    + newly created), keyed by code, so the caller can bind a Subscription.
    """
    codes = [spec["code"] for spec in DEFAULT_PLAN_CATALOG]
    existing = (await session.execute(select(Plan).where(Plan.code.in_(codes)))).scalars().all()
    by_code = {p.code: p for p in existing}

    for spec in DEFAULT_PLAN_CATALOG:
        if spec["code"] in by_code:
            continue
        plan = Plan(
            id=uuid.uuid4(),
            code=spec["code"],
            name=spec["name"],
            monthly_price=spec["monthly_price"],
            currency=CATALOG_CURRENCY,
            entitlements=spec["entitlements"],
            usage_components=spec["usage_components"],
            trial_days=spec["trial_days"],
        )
        session.add(plan)
        by_code[spec["code"]] = plan

    await session.flush()
    return by_code


async def clear_stale_canceled_subscription(
    session: AsyncSession, *, organization_id: uuid.UUID, plan_id: uuid.UUID
) -> None:
    """Free the ``(organization_id, plan_id)`` slot by deleting a leftover
    CANCELED subscription row occupying it.

    ``uq_subscription_org_plan`` is ``UNIQUE (organization_id, plan_id)`` with
    **no status filter** — deliberately, so an org can never hold two rows for
    one plan — while ``uq_subscription_one_live_per_org`` is the partial index
    that bounds the LIVE count. A canceled row therefore keeps occupying its
    slot forever, and any write that tries to put the org back on that plan
    raises ``IntegrityError`` rather than succeeding: the INSERT in
    :func:`ensure_subscription` below, and the in-place ``plan_id`` repoint in
    ``services/billing/plan_change.py::change_plan`` and
    ``scripts/seed.py::ensure_public_api_entitled``.

    ``change_plan`` has always cleared the row inline for exactly this reason.
    This is that same rule, named once, so a fourth writer inherits it instead
    of rediscovering it as a production ``IntegrityError``.

    Deleting is the established resolution, not a new one: the canceled row is
    convenience history of a plan the org is re-adopting, the live row is the
    source of truth, and the durable record of a plan change is the append-only
    ``billing.plan_changed`` audit row — not this table. Caller commits.
    """
    await session.execute(
        Subscription.__table__.delete().where(
            Subscription.organization_id == organization_id,
            Subscription.plan_id == plan_id,
            Subscription.status == "canceled",
        )
    )


async def ensure_subscription(
    session: AsyncSession, *, organization_id: uuid.UUID, plan_code: str
) -> Subscription | None:
    """Bind `organization_id` to `plan_code` if it has no live subscription
    yet (mirrors `uq_subscription_one_live_per_org`: at most one row with
    `status != "canceled"`). No-ops and returns the existing row if the org
    is already subscribed to anything — never creates a second live
    subscription. Returns `None` when `plan_code` isn't in the catalog yet
    (a fresh control DB before `ensure_plan_catalog` has run) — mirrors the
    "skip silently, don't crash provisioning" pattern already used for the
    admin role lookup in `tenant_provisioning._provision_into`.

    Having NO live subscription is not the same as the `(org, plan)` slot
    being free: `uq_subscription_org_plan` ignores status, so an org whose
    subscription to this very plan was CANCELED (the dunning sweep is the
    path that does that) still occupies it, and the INSERT below raised
    `IntegrityError` instead of resubscribing. Hence
    :func:`clear_stale_canceled_subscription` first.
    """
    existing = (
        await session.execute(
            select(Subscription).where(
                Subscription.organization_id == organization_id,
                Subscription.status != "canceled",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    plan = (await session.execute(select(Plan).where(Plan.code == plan_code))).scalar_one_or_none()
    if plan is None:
        return None

    await clear_stale_canceled_subscription(
        session, organization_id=organization_id, plan_id=plan.id
    )

    # Stamp the first billing window. Plans are flat monthly, and every reader
    # of these columns (proration, the dunning grace clock, the subscription
    # summary) is useless without them — leaving them NULL is what made every
    # mid-period plan change prorate 0.00. See `services/billing/period.py`.
    started = datetime.now(UTC)
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=organization_id,
        plan_id=plan.id,
        status="active",
        current_period_start=started,
        current_period_end=add_months(started, 1),
    )
    session.add(sub)
    await session.flush()
    return sub
