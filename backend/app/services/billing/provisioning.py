"""Per-org Stripe customer + per-plan price provisioning.

The live ``stripe_billing`` adapter's ``create_subscription`` needs the resolved
provider-side ``customer`` id (one per org) and ``price`` id (one per plan).
This module owns *resolving* those: read what's already persisted on
``Organization.settings.billing``, ask the adapter to create anything missing
(idempotent at the provider), persist the new ids, and hand the caller the
``{stripe_customer_id, stripe_price_id}`` config the adapter consumes.

Where the linkage lives (NO migration — reuses existing JSONB)
--------------------------------------------------------------
``Organization.settings.billing`` (control-plane JSONB, the same block that holds
``provider``):

    {
      "billing": {
        "provider": "stripe_billing",
        "stripe_customer_id": "cus_...",          # one per org
        "plan_price_ids": {"growth": "price_..."}, # keyed by Plan.code
        "ai_overage_price_ids": {"growth": "price_..."} # metered overage (paid tiers)
      }
    }

``Subscription.external_subscription_id`` (an existing column) holds the live
provider subscription id once ``create_subscription`` returns.

Money / PII / secrets
---------------------
The price unit amount is derived from ``Plan.monthly_price`` (Decimal) with exact
math inside the adapter — never float. Only the org's business name + an admin
email are sent to the provider (never bank/tax/PAN). The adapter fails closed
without ``FEOH_BILLING_STRIPE_API_KEY`` (the dispatcher injects the key); the
``mock`` adapter returns deterministic synthetic ids with no network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.billing import Plan
from app.models.organization import Organization
from app.models.user import User
from app.services.billing.ai_invoice_meter import allowance_for_plan
from app.services.billing_adapters import get_billing_adapter
from app.services.billing_adapters.base import BillingAdapter
from app.tenant import lock_organization

logger = logging.getLogger(__name__)

#: ``settings.billing`` key holding each plan's metered overage price id.
OVERAGE_PRICE_IDS_KEY = "ai_overage_price_ids"


@dataclass(frozen=True)
class ProvisionedIds:
    """Resolved provider-side ids for an (org, plan) pair."""

    customer_id: str
    price_id: str
    #: The plan's metered AI-read OVERAGE price (decisions §253), or ``None`` for
    #: a plan with no overage (Free pauses instead of billing).
    overage_price_id: str | None = None


def resolve_provider(org: Organization) -> str:
    """Per-org override (`settings.billing.provider`) → platform default."""
    from app.config import settings

    billing = (org.settings or {}).get("billing") or {}
    return billing.get("provider") or settings.billing_provider


def _adapter_for(org: Organization, *, extra_config: dict | None = None) -> BillingAdapter:
    """Build the org's billing adapter with the process key/base config injected
    (via the dispatcher) plus any per-call extra (customer/price ids)."""
    adapter = get_billing_adapter(resolve_provider(org))
    if extra_config:
        # The dispatcher already populated config with the Stripe key/base; merge
        # the resolved customer/price ids so create_subscription can read them.
        adapter.config = {**adapter.config, **extra_config}
    return adapter


async def provision_org_billing(
    control_db: AsyncSession, *, org: Organization, plan: Plan
) -> ProvisionedIds:
    """Resolve-or-create the org's customer id + the plan's price id; persist them.

    Reads `settings.billing.stripe_customer_id` + `.plan_price_ids[plan.code]`;
    creates anything missing via the adapter (idempotent at the provider) and
    writes the new ids back onto `Organization.settings.billing`. Commits the
    settings mutation so a later retry reuses the persisted ids. Returns the
    resolved pair.

    Fail-closed: with the live `stripe_billing` adapter and no API key, the
    adapter raises `BillingNotConfigured` from `ensure_customer` / `ensure_price`
    and nothing is persisted.
    """
    adapter = _adapter_for(org)

    settings_dict = dict(org.settings or {})
    billing = dict(settings_dict.get("billing") or {})
    price_ids = dict(billing.get("plan_price_ids") or {})

    mutated = False

    customer_id = billing.get("stripe_customer_id")
    if not customer_id:
        admin_email = await _first_admin_email(control_db, org.id)
        customer_id = await adapter.ensure_customer(
            organization_id=str(org.id), name=org.name, email=admin_email
        )
        billing["stripe_customer_id"] = customer_id
        mutated = True

    price_id = price_ids.get(plan.code)
    if not price_id:
        price_id = await adapter.ensure_price(
            plan_code=plan.code, monthly_price=plan.monthly_price, currency=plan.currency
        )
        price_ids[plan.code] = price_id
        billing["plan_price_ids"] = price_ids
        mutated = True

    # The metered overage price, for a plan that bills AI-read invoices past its
    # allowance. Without it on the subscription the provider records the
    # overage meter events and invoices none of them.
    allowance = allowance_for_plan(plan)
    overage_ids = dict(billing.get(OVERAGE_PRICE_IDS_KEY) or {})
    overage_price_id = overage_ids.get(plan.code)
    if allowance.bills_overage and not overage_price_id:
        assert allowance.overage_unit_price is not None
        overage_price_id = await adapter.ensure_overage_price(
            plan_code=plan.code, unit_price=allowance.overage_unit_price, currency=plan.currency
        )
        overage_ids[plan.code] = overage_price_id
        billing[OVERAGE_PRICE_IDS_KEY] = overage_ids
        mutated = True

    if mutated:
        # The adapter calls above are network round trips, so the row is locked
        # only now, and the ids are merged into what is stored THEN rather than
        # written back over the snapshot read before them — a settings writer
        # that committed meanwhile (`lock_organization`) is kept. An id a
        # concurrent provisioning stored first wins; the provider calls are
        # idempotent, so ours names the same object.
        org = await lock_organization(control_db, org)
        settings_dict = dict(org.settings or {})
        stored = dict(settings_dict.get("billing") or {})
        stored.setdefault("stripe_customer_id", customer_id)
        stored_prices = dict(stored.get("plan_price_ids") or {})
        stored_prices.setdefault(plan.code, price_id)
        stored["plan_price_ids"] = stored_prices
        if overage_price_id:
            stored_overage = dict(stored.get(OVERAGE_PRICE_IDS_KEY) or {})
            stored_overage.setdefault(plan.code, overage_price_id)
            stored[OVERAGE_PRICE_IDS_KEY] = stored_overage
            overage_price_id = stored_overage[plan.code]
        customer_id = stored["stripe_customer_id"]
        price_id = stored_prices[plan.code]
        settings_dict["billing"] = stored
        org.settings = settings_dict
        flag_modified(org, "settings")
        await control_db.commit()
        await control_db.refresh(org)

    return ProvisionedIds(
        customer_id=str(customer_id),
        price_id=str(price_id),
        overage_price_id=str(overage_price_id) if overage_price_id else None,
    )


async def _first_admin_email(control_db: AsyncSession, organization_id) -> str | None:
    """An admin contact email for the org (a business contact, not regulated PII).

    Best-effort — returns ``None`` if the org has no users; the provider customer
    is still created (email is optional). The user's email is the only field
    sent, never tax/bank data.
    """
    email = (
        await control_db.execute(
            select(User.email)
            .where(User.organization_id == organization_id)
            .order_by(User.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return email
