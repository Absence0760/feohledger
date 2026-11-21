"""The provider half of a plan change: the live Stripe subscription.

`plan_change.change_plan` owns OUR row (the lock, the proration, the audit
row). This module owns what the billing provider must be told so the plan the
org is on is also the plan it is charged for (decisions §480):

* **onto a paid plan, no provider subscription yet** — require a saved card,
  then create the subscription (base price + the metered AI-overage price),
  its billing cycle anchored on the 1st of next month, UTC;
* **paid → paid** — swap the subscription's items, prorated and invoiced now;
* **paid → free** — cancel immediately, crediting unused time and invoicing the
  period's metered overage on a final invoice;
* **free → free** — nothing.

Every call here is a network round trip, so `change_plan` runs this OUTSIDE its
row lock (`docs/billing.md` § Concurrency). What keeps two plan changes for one
org from interleaving their provider calls is a short **lease** on
``Organization.settings.billing`` (:func:`claim_plan_change_lease`) — taken only
when a live provider is involved, since only then is there an external effect
for the row lock not to cover.

The ``mock`` provider is never a live provider: locally a plan change only
repoints the row (guard rail 7). In a DEPLOYED environment a move onto a paid
plan with ``mock`` in effect is refused (:func:`refuse_unbilled_paid_plan`) —
otherwise every feature behind a paid tier would be one click away and free.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.billing import Plan
from app.models.organization import Organization
from app.services.billing.ai_invoice_meter import allowance_for_plan
from app.services.billing.provisioning import ProvisionedIds
from app.services.billing_adapters.base import (
    BillingAdapter,
    CreateSubscriptionRequest,
    ProviderSubscription,
)

logger = logging.getLogger(__name__)

#: ``settings.billing`` key holding an in-flight live plan change's lease.
LEASE_KEY = "plan_change_lease"
#: How long a lease holds before another change may take over. It must outlast
#: the slowest change that is still running — a change past its lease could
#: race the one that took it over into a SECOND provider subscription — so it
#: is sized to the worst case, not the typical one: five provider calls, each
#: with a 15 s timeout per connect / write / read phase (~4 min), plus the
#: locked write. A request that crashed blocks the org's plan changes for this
#: long, which is the cheaper failure.
LEASE_TTL = timedelta(minutes=10)

#: The adapter name that is never a live provider.
MOCK_PROVIDER = "mock"


class ProviderLegRefused(RuntimeError):
    """A plan change the provider leg refuses. ``code`` is the coded refusal
    the API answers with (`api/billing.py`)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def plan_is_paid(plan: Plan) -> bool:
    """Whether being on ``plan`` costs money: a monthly price, or AI-read
    overage it bills past the allowance."""
    return plan.monthly_price > Decimal("0") or allowance_for_plan(plan).bills_overage


def is_live_provider(adapter: BillingAdapter) -> bool:
    """A provider that charges real money. Read off the RESOLVED adapter, not
    the configured name: an unregistered name falls back to ``mock``
    (`billing_adapters.dispatcher`), and that fallback must not count as live."""
    return adapter.provider_name != MOCK_PROVIDER


def refuse_unbilled_paid_plan(adapter: BillingAdapter, new_plan: Plan, *, deployed: bool) -> None:
    """Fail closed: in a deployed environment, a paid plan needs a live provider.

    `settings.is_deployed` is the same discriminator the payment rails use to
    refuse the ``mock`` processor (`payment_execution_mode`). Local dev and CI
    keep ``mock`` working end to end (guard rail 7), and a move DOWN to a free
    plan is never refused — leaving a paid tier must always be possible.
    """
    if deployed and not is_live_provider(adapter) and plan_is_paid(new_plan):
        raise ProviderLegRefused("billing_not_configured")


def next_month_start(now: datetime) -> datetime:
    """00:00 UTC on the 1st of the month after ``now`` — the billing anchor."""
    now = now.astimezone(UTC)
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, tzinfo=UTC)
    return datetime(now.year, now.month + 1, 1, tzinfo=UTC)


def calendar_month(now: datetime) -> tuple[datetime, datetime]:
    """The UTC calendar month containing ``now`` — a live subscription's
    billing period, since every one is anchored on the 1st."""
    now = now.astimezone(UTC)
    return datetime(now.year, now.month, 1, tzinfo=UTC), next_month_start(now)


# --- Lease --------------------------------------------------------------------


async def claim_plan_change_lease(
    control_db: AsyncSession, org: Organization, *, now: datetime
) -> str:
    """Take the org's plan-change lease, or refuse ``plan_change_in_progress``.

    Written under `lock_organization` (every settings writer serialises there)
    and committed at once, so a second change sees it before it makes any
    provider call. An expired lease — a request that died mid-change — is
    simply taken over.
    """
    locked = await _lock_org(control_db, org.id)
    settings_dict = dict(locked.settings or {})
    billing = dict(settings_dict.get("billing") or {})
    lease = billing.get(LEASE_KEY)
    if isinstance(lease, dict):
        try:
            expires_at = datetime.fromisoformat(str(lease.get("expires_at")))
        except ValueError:
            expires_at = None
        if expires_at is not None and expires_at > now:
            await control_db.rollback()
            raise ProviderLegRefused("plan_change_in_progress")
    token = uuid.uuid4().hex
    billing[LEASE_KEY] = {"token": token, "expires_at": (now + LEASE_TTL).isoformat()}
    settings_dict["billing"] = billing
    locked.settings = settings_dict
    flag_modified(locked, "settings")
    await control_db.commit()
    return token


async def _lock_org(control_db: AsyncSession, organization_id) -> Organization:
    """`lock_organization` by id. The callers here run after a rollback, which
    expires every loaded instance — reading ``org.id`` off one from async code
    would be a lazy load (``MissingGreenlet``), so they hold the plain id."""
    return (
        await control_db.execute(
            select(Organization)
            .where(Organization.id == organization_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def lease_is_ours(control_db: AsyncSession, organization_id, token: str) -> bool:
    """Whether the org's lease is still ``token`` — read under the org lock.

    `change_plan` checks it inside its locked write: a change whose lease ran
    out and was taken over must not record what it did at the provider over
    the change that now owns the org."""
    locked = await _lock_org(control_db, organization_id)
    lease = ((locked.settings or {}).get("billing") or {}).get(LEASE_KEY)
    return isinstance(lease, dict) and lease.get("token") == token


async def release_plan_change_lease(control_db: AsyncSession, organization_id, token: str) -> None:
    """Drop the lease if it is still ours. Best-effort: a failure here only
    means the lease runs out on its own after `LEASE_TTL`."""
    try:
        locked = await _lock_org(control_db, organization_id)
        settings_dict = dict(locked.settings or {})
        billing = dict(settings_dict.get("billing") or {})
        lease = billing.get(LEASE_KEY)
        if isinstance(lease, dict) and lease.get("token") == token:
            billing.pop(LEASE_KEY, None)
            settings_dict["billing"] = billing
            locked.settings = settings_dict
            flag_modified(locked, "settings")
            await control_db.commit()
        else:
            await control_db.rollback()
    except Exception as exc:  # noqa: BLE001 — the lease expires by itself
        logger.warning("billing plan-change lease release failed: %s", type(exc).__name__)
        await control_db.rollback()


# --- The provider leg -----------------------------------------------------------


@dataclass(frozen=True)
class ProviderChange:
    """What the provider leg did, for `change_plan` to record on our row."""

    action: str  # created | adopted | updated | canceled | none
    #: The subscription id our row should carry afterwards (None after a
    #: cancel: the provider subscription is gone, and keeping its id would let
    #: its `customer.subscription.deleted` webhook cancel the FREE row).
    external_subscription_id: str | None
    subscription: ProviderSubscription | None = None


async def apply_plan_at_provider(
    adapter: BillingAdapter,
    *,
    organization_id,
    external_subscription_id: str | None,
    new_plan: Plan,
    ids: ProvisionedIds,
    change_key: str,
    now: datetime,
) -> ProviderChange:
    """Make the live provider's subscription match ``new_plan``.

    ``change_key`` identifies this change of our row (the row id, its last
    update and the target plan); the default card is appended to it. A retry
    of the same change with the same card reuses the same ``Idempotency-Key``s,
    while a later change of the row, or a retry with a new card after a
    decline, does not.
    Raises :class:`ProviderLegRefused` (``payment_method_required``) and lets
    the adapter's `BillingPaymentDeclined` / `BillingProviderError` propagate.
    """
    if not plan_is_paid(new_plan):
        if external_subscription_id is None:
            # Our row names none — but one may still be billing: created by an
            # attempt that died before recording it, or detached from the row
            # before a cancel that then failed (`change_plan`). Leaving a paid
            # tier must stop the charge whichever way the id got lost.
            live = await adapter.find_live_subscription(ids.customer_id)
            if live is None:
                return ProviderChange(action="none", external_subscription_id=None)
            external_subscription_id = live.external_subscription_id
        canceled = await adapter.cancel_subscription(external_subscription_id)
        return ProviderChange(
            action="canceled", external_subscription_id=None, subscription=canceled
        )

    # Onto (or between) paid plans: the subscription charges a card off
    # session, so one must be on file first. Checked before any write at the
    # provider, so the refusal leaves nothing behind.
    payment_method = await adapter.ensure_default_payment_method(ids.customer_id)
    if not payment_method:
        raise ProviderLegRefused("payment_method_required")
    # The card is part of the key. Stripe stores a declined (402) answer
    # under its key and replays it for 24 h, and a decline changes nothing on
    # our row — so without this, the retry after the customer replaced the
    # declined card would replay the decline instead of charging the new one.
    change_key = f"{change_key}-{payment_method}"

    if external_subscription_id is None:
        # A previous attempt may have created the subscription and died before
        # our row recorded it. Adopt that one rather than create a second, which
        # would bill the org twice.
        existing = await adapter.find_live_subscription(ids.customer_id)
        if existing is not None:
            external_subscription_id = existing.external_subscription_id
            action = "adopted"
        else:
            adapter.config = {
                **adapter.config,
                "stripe_customer_id": ids.customer_id,
                "stripe_price_id": ids.price_id,
                "stripe_overage_price_id": ids.overage_price_id,
            }
            created = await adapter.create_subscription(
                CreateSubscriptionRequest(
                    organization_id=str(organization_id),
                    plan_code=new_plan.code,
                    monthly_price=new_plan.monthly_price,
                    currency=new_plan.currency,
                    # A self-serve upgrade from Free is not a trial: the
                    # customer chose to pay, and the features unlock now.
                    trial_days=0,
                    idempotency_key=f"feohledger-sub-create-{change_key}",
                    billing_cycle_anchor=next_month_start(now),
                )
            )
            return ProviderChange(
                action="created",
                external_subscription_id=created.external_subscription_id,
                subscription=created,
            )
    else:
        action = "updated"

    updated = await adapter.change_subscription_plan(
        external_subscription_id,
        price_id=ids.price_id,
        overage_price_id=ids.overage_price_id,
        plan_code=new_plan.code,
        idempotency_key=f"feohledger-sub-update-{change_key}",
    )
    return ProviderChange(
        action=action,
        external_subscription_id=updated.external_subscription_id,
        subscription=updated,
    )
