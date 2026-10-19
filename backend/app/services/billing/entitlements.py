"""Entitlement resolution — what features an org's active plan grants.

An org's effective entitlements are the ``entitlements`` JSON of the ``Plan``
behind its **live** ``Subscription`` (status != ``canceled``). An org with no
live subscription — one provisioned before ``ensure_subscription`` existed, or
one the dunning sweep canceled — reads as the catalog's ``free`` plan, the plan
every new tenant lands on, exactly as the AI-read meter already reads it
(``ai_invoice_meter.allowance_for_plan``). Still fail-closed: a feature is
granted only when a plan explicitly includes it, and Free grants only what any
new workspace gets (decisions §281). Before Free carried a grant, "no plan" and
"Free" were both ``{}``; reading them alike keeps a lapsed paid tenant from
landing *below* Free.

Pure read; never mutates. Used by the ``require_entitlement`` dependency in
``api/deps.py`` and by the customer billing endpoint.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import Plan, Subscription
from app.services.billing.plan_catalog import DEFAULT_PLAN_CATALOG

#: What an org with no live plan is granted: the Free tier's catalog grants.
#: From the code catalog, not the ``free`` row, for the same reason as the
#: meter's ``_FREE_COMPONENT`` — it must not depend on a row being present.
NO_PLAN_ENTITLEMENTS: dict = dict(
    next(p for p in DEFAULT_PLAN_CATALOG if p["code"] == "free")["entitlements"]
)


async def get_active_subscription(
    db: AsyncSession, organization_id
) -> tuple[Subscription, Plan] | None:
    """Return the org's (live subscription, plan) pair, or ``None``.

    "Live" = status not ``canceled``. Newest-created wins if (defensively) more
    than one non-canceled row exists — the partial unique index normally makes
    that impossible.
    """
    row = (
        await db.execute(
            select(Subscription, Plan)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.organization_id == organization_id,
                Subscription.status != "canceled",
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1]


async def get_entitlements(db: AsyncSession, organization_id) -> dict:
    """Effective entitlement map for the org; the Free grants when no live plan."""
    active = await get_active_subscription(db, organization_id)
    if active is None:
        return dict(NO_PLAN_ENTITLEMENTS)
    _subscription, plan = active
    return dict(plan.entitlements or {})


def has_entitlement(entitlements: dict, feature: str) -> bool:
    """Truthiness check for a single feature flag. Unknown feature → False."""
    return bool(entitlements.get(feature))
