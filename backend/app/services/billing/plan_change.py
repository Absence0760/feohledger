"""Mid-period plan change — orchestrates proration + provisioning + audit.

Changing the plan behind an org's live ``Subscription`` part-way through a
billing period:

  1. **Pre-flight, unlocked.** Resolve the target plan, confirm the org has a
     live subscription at all, and short-circuit a same-plan retry (so it never
     reaches the provider). Then resolve-or-create the provider customer + the
     NEW plan's price (`provision_org_billing`).
  2. Resolve the org's live subscription + current plan **row-locked**
     (`_get_active_subscription_for_update`, ``SELECT ... FOR UPDATE``). A
     second concurrent ``change_plan`` call for the same org blocks behind
     this one's commit and then re-reads the already-updated subscription as
     its own baseline, instead of racing off the same stale "current" plan
     (see `docs/billing.md` § Concurrency).
  3. **Idempotent no-op** when the target plan equals the current plan — returns
     a zero proration, mutates nothing, writes no audit row (mirrors
     `transition_invoice` / `apply_billing_event`'s no-op rule). A retry of the
     same change therefore can't double-charge. Re-checked here under the lock,
     because a racer may have moved the org onto this plan since the peek.
  4. Resolve (and persist) the subscription's current billing window via
     `period.current_period`, then compute the prorated adjustment
     (`compute_proration`, pure Decimal) off the locked baseline. Nothing used
     to write `current_period_start`/`_end`, so this step used to divide by a
     zero-length window and prorate `0.00` on every change.
  5. Repoint the subscription at the new plan, persist, and write an append-only
     ``billing.plan_changed`` audit row (PII-free; old/new plan + proration as an
     exact decimal string).

**With a live provider** (decisions §480) a provider leg runs between 1 and 2,
also outside the lock, under a per-org lease: it creates, amends or cancels the
org's Stripe subscription (`provider_sync.apply_plan_at_provider`), and step 5
records what it did. A paid plan needs a card on file; a declined charge makes
no change anywhere. In a deployed environment a paid plan with the ``mock``
provider is refused before anything is written.

**Step 1 must stay ahead of step 2.** `provision_org_billing` commits, and a
commit inside the locked section RELEASES the row lock mid-transaction: the
waiting racer's ``FOR UPDATE`` then returned the still-unrepointed row, both
changes prorated off the same stale plan, and both wrote an audit row claiming
the same ``from_plan``. That happened whenever provisioning had anything to
persist — i.e. on the first change to any plan the org had no stored price id
for. Keeping it ahead of the lock also keeps the (live, third-party) Stripe
round-trip out of the locked window.

Money-path boundary
-------------------
This never moves money itself. With a live provider the charge (or credit) is
the provider's: its subscription is created / amended / canceled and the
provider invoices the proration, under ``Idempotency-Key``s derived from this
change of the row. The proration computed here is recorded as our estimate; the
``mock`` provider moves nothing, so locally it is informational. Idempotency is
the "already on this plan" guard, the provider's idempotency keys, and adopting
a provider subscription a dead attempt left behind (`provider_sync`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.billing import Plan, Subscription
from app.models.organization import Organization
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.billing.entitlements import get_active_subscription
from app.services.billing.period import current_period
from app.services.billing.plan_catalog import clear_stale_canceled_subscription
from app.services.billing.proration import ProrationResult, compute_proration
from app.services.billing.provider_sync import (
    ProviderChange,
    ProviderLegRefused,
    apply_plan_at_provider,
    calendar_month,
    claim_plan_change_lease,
    is_live_provider,
    refuse_unbilled_paid_plan,
    release_plan_change_lease,
)
from app.services.billing.provisioning import billing_adapter_for, provision_org_billing

logger = logging.getLogger(__name__)


async def _get_active_subscription_for_update(
    db: AsyncSession, organization_id
) -> tuple[Subscription, Plan] | None:
    """Row-locked variant of ``entitlements.get_active_subscription``, for
    ``change_plan``'s read-modify-write ONLY.

    Plain read-only callers (the entitlement-check dependencies, the
    subscription-summary endpoint) keep using the unlocked
    ``get_active_subscription`` — they never mutate, so taking a row lock
    there would only add contention with no correctness benefit. This variant
    exists so a second concurrent plan change for the same org blocks on the
    ``FOR UPDATE`` until the first commits, then observes the first change's
    result as its own "current" baseline instead of a stale pre-change read.

    Deliberately **two separate queries**, not one ``SELECT ... FOR UPDATE``
    join across ``Subscription`` and ``Plan``: Postgres's lock-wait recheck
    (EvalPlanQual) re-fetches the latest version of the LOCKED row but reuses
    the join partner from the ORIGINAL scan. Since a plan change rewrites
    ``Subscription.plan_id`` — the very column the join keys on — a second
    waiter's joined query would recheck the new ``plan_id`` against the OLD
    (pre-change) ``Plan`` row, the join predicate would fail, and the query
    would come back with no row at all, even though the subscription plainly
    exists (confirmed with a two-connection repro against a real Postgres:
    the joined-``FOR UPDATE`` form drops the row after the first racer
    commits; splitting the lock from the ``Plan`` lookup does not). Locking
    only ``Subscription`` and then issuing a fresh, unlocked ``Plan`` lookup
    keyed off the just-locked (and therefore current) ``plan_id`` sidesteps
    the pitfall entirely.

    ``populate_existing=True`` is load-bearing, not defensive tidiness. If this
    session already holds the ``Subscription`` in its identity map — which it
    does, because ``change_plan``'s unlocked pre-flight peek loaded it — the
    default behaviour hands that instance BACK with its previously-loaded
    column values and discards the ones the locked SELECT just read. A second
    racer would then unblock, re-read the row Postgres has since updated, and
    still see the stale ``plan_id``, defeating the lock entirely. Same reason
    ``api/webhooks._get_owned_subscription`` sets it before a rotation.
    """
    subscription = (
        await db.execute(
            select(Subscription)
            .where(
                Subscription.organization_id == organization_id,
                Subscription.status != "canceled",
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if subscription is None:
        return None
    plan = (
        await db.execute(select(Plan).where(Plan.id == subscription.plan_id))
    ).scalar_one_or_none()
    if plan is None:
        return None
    return subscription, plan


class PlanChangeError(RuntimeError):
    """Raised for a caller-correctable plan-change failure (no live sub, bad plan)."""


@dataclass(frozen=True)
class PlanChangeResult:
    changed: bool
    old_plan_code: str
    new_plan_code: str
    proration: ProrationResult
    reason: str | None = None


async def change_plan(
    control_db: AsyncSession,
    *,
    org: Organization,
    new_plan_code: str,
    actor_id,
    change_at: datetime | None = None,
) -> PlanChangeResult:
    """Move the org's live subscription to ``new_plan_code``, prorated.

    Raises :class:`PlanChangeError` (the API maps to 4xx) when the org has no
    live subscription or the target plan code is unknown/inactive, and
    :class:`ProviderLegRefused` for a coded refusal the provider leg makes (no
    card on file, billing not configured in a deployed environment, another
    change in flight). A change to the plan the org is already on is a
    successful **no-op** (``changed=False``).
    """
    now = change_at or datetime.now(UTC)

    new_plan = (
        await control_db.execute(
            select(Plan).where(Plan.code == new_plan_code, Plan.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if new_plan is None:
        raise PlanChangeError("unknown_plan")

    # --- Pre-flight, deliberately BEFORE the row lock ------------------------
    # `provision_org_billing` COMMITS (it persists the resolved provider ids onto
    # `Organization.settings.billing`). Calling it between the `FOR UPDATE` read
    # and the repoint released that lock half-way through the read-modify-write:
    # the second racer's blocked SELECT then returned the still-unrepointed row,
    # so BOTH changes prorated off the same stale baseline and both wrote an
    # audit row claiming the same `from_plan` — the exact lost update
    # `_get_active_subscription_for_update` exists to prevent. It fired whenever
    # provisioning had anything to persist, i.e. on the FIRST change to any plan
    # the org has no stored price id for — the common case, not an edge one.
    #
    # Provisioning here also keeps the provider round-trips OUT of the locked
    # window, so an inbound billing webhook or the dunning sweep can't be
    # parked behind them on the same subscription row. The same holds for the
    # provider leg below (create / amend / cancel the live subscription).
    #
    # The unlocked peek is what preserves "a same-plan retry makes no provider
    # call"; the AUTHORITATIVE no-live-subscription / already-on-plan checks are
    # still the locked ones below.
    peek = await get_active_subscription(control_db, org.id)
    if peek is None:
        raise PlanChangeError("no_live_subscription")
    peek_subscription, peek_plan = peek
    if peek_plan.id == new_plan.id:
        return PlanChangeResult(
            changed=False,
            old_plan_code=peek_plan.code,
            new_plan_code=new_plan.code,
            proration=_zero_proration(peek_plan.monthly_price, new_plan.monthly_price),
            reason="already_on_plan",
        )
    # Plain values: the provisioning commit below expires every loaded instance.
    peek_plan_id = peek_plan.id
    peek_external_id = peek_subscription.external_subscription_id
    updated_at = peek_subscription.updated_at or peek_subscription.created_at
    change_key = (
        f"{peek_subscription.id}-{int(updated_at.timestamp()) if updated_at else 0}-{new_plan.code}"
    )

    adapter = billing_adapter_for(org)
    live = is_live_provider(adapter)
    # A deployed environment never hands out a paid plan the provider cannot
    # charge for (decisions §480). Before provisioning, so nothing is written.
    refuse_unbilled_paid_plan(adapter, new_plan, deployed=settings.is_deployed)

    # Resolve-or-create the provider customer + the NEW plan's price so the live
    # adapter has what create/update needs. Fails closed (BillingNotConfigured)
    # with the live adapter and no key — before anything is locked or mutated.
    ids = await provision_org_billing(control_db, org=org, plan=new_plan)

    lease: str | None = None
    provider_change: ProviderChange | None = None
    try:
        if live:
            # One live change per org at a time: its provider calls happen
            # outside the row lock, so the lock alone cannot serialise them.
            lease = await claim_plan_change_lease(control_db, org, now=now)
            provider_change = await apply_plan_at_provider(
                adapter,
                organization_id=org.id,
                external_subscription_id=peek_external_id,
                new_plan=new_plan,
                ids=ids,
                change_key=change_key,
                now=now,
            )

        result = await _record_change(
            control_db,
            org=org,
            new_plan=new_plan,
            actor_id=actor_id,
            now=now,
            provider=adapter.provider_name,
            provider_change=provider_change,
            expected_plan_id=peek_plan_id if live else None,
        )
    except BaseException:
        await control_db.rollback()
        if provider_change is not None and provider_change.action != "none":
            # The provider already changed and our row did not: the next
            # attempt adopts the provider's subscription (or re-applies the same
            # items), but an operator must know the two disagree until then.
            logger.error(
                "billing plan change reached the provider (%s) but was not recorded",
                provider_change.action,
            )
        raise
    finally:
        if lease is not None:
            await release_plan_change_lease(control_db, org, lease)
    return result


async def _record_change(
    control_db: AsyncSession,
    *,
    org: Organization,
    new_plan: Plan,
    actor_id,
    now: datetime,
    provider: str,
    provider_change: ProviderChange | None,
    expected_plan_id,
) -> PlanChangeResult:
    """The locked read-modify-write: ONE transaction, ONE commit."""
    active = await _get_active_subscription_for_update(control_db, org.id)
    if active is None:
        raise PlanChangeError("no_live_subscription")
    subscription, current_plan = active

    # Idempotent no-op: already on the target plan. No mutation, no audit row —
    # a retry of the same change can't double-charge. Re-checked under the lock
    # because a racer may have moved the org onto this plan since the peek.
    if current_plan.id == new_plan.id:
        return PlanChangeResult(
            changed=False,
            old_plan_code=current_plan.code,
            new_plan_code=new_plan.code,
            proration=_zero_proration(current_plan.monthly_price, new_plan.monthly_price),
            reason="already_on_plan",
        )
    if expected_plan_id is not None and current_plan.id != expected_plan_id:
        # The provider was changed from the peeked plan, and the row has since
        # moved off it (a writer that took no lease — the seed's repair, an
        # operator). Recording ours would claim a `from_plan` the provider
        # change was never made from.
        raise ProviderLegRefused("plan_change_in_progress")

    # Resolve the window the subscription is actually in, and PERSIST it. The
    # old `subscription.current_period_start or now` / `... or now` fallback
    # handed `compute_proration` a zero-length window on every real
    # subscription — nothing ever wrote those columns — so its
    # degenerate-window guard short-circuited and every plan change prorated
    # `0.00` while reporting that figure as correct. `current_period` is the
    # one rule the summary endpoint and the dunning grace clock read too, so
    # writing the resolved window back here also un-sticks a row those two
    # were reading as NULL. See `services/billing/period.py`.
    #
    # With a live provider the window is the UTC calendar month: every live
    # subscription is anchored on the 1st, so that is the period the provider
    # prorates over (decisions §480). The figure is still our day-granular
    # estimate; the provider's invoice is the charge of record.
    if provider_change is not None:
        start, end = calendar_month(now)
    else:
        period = current_period(subscription, now=now)
        start, end = period.start, period.end
    subscription.current_period_start = start
    subscription.current_period_end = end

    proration = compute_proration(
        old_monthly=current_plan.monthly_price,
        new_monthly=new_plan.monthly_price,
        period_start=start,
        period_end=end,
        change_at=now,
    )

    previous_status = subscription.status
    if provider_change is not None and provider_change.action != "none":
        subscription.external_subscription_id = provider_change.external_subscription_id
        synced = provider_change.subscription
        if provider_change.external_subscription_id is None:
            # Canceled at the provider: the org is on a free plan we do not
            # bill, which is simply live.
            subscription.status = "active"
        elif synced is not None:
            subscription.status = synced.status
            # The provider's window is authoritative once it bills the row.
            if synced.current_period_start and synced.current_period_end:
                subscription.current_period_start = synced.current_period_start
                subscription.current_period_end = synced.current_period_end

    # Guard the (org, plan) unique constraint: a leftover *canceled* row for the
    # target plan would collide when we repoint plan_id. Freeing that slot is
    # shared with `plan_catalog.ensure_subscription` and the seed's repair —
    # one owner, so a fourth writer inherits the rule instead of rediscovering
    # it as a production IntegrityError.
    await clear_stale_canceled_subscription(control_db, organization_id=org.id, plan_id=new_plan.id)

    previous_plan_code = current_plan.code
    subscription.plan_id = new_plan.id

    # Append-only audit (SOX): plan change is a regulated control-plane mutation.
    # PII-free — org, old/new plan code, proration as an exact decimal STRING,
    # and what the provider was told (its subscription id is an opaque
    # reference, not customer data). Dispatched BEFORE the commit (mirrors
    # apply_billing_event) so the change is never durably persisted without an
    # audit attempt; dispatch_auth_audit is fail-soft and opens its own tenant
    # session.
    details = {
        "from_plan": previous_plan_code,
        "to_plan": new_plan.code,
        "proration_amount": str(proration.amount),
        "unused_days": proration.unused_days,
        "period_days": proration.period_days,
        "provider": provider,
    }
    if provider_change is not None:
        details["provider_action"] = provider_change.action
        details["external_subscription_id"] = provider_change.external_subscription_id
    if subscription.status != previous_status:
        # The provider's answer moved the lifecycle status too (a past_due row
        # back to active, say): a status change on a regulated record is on
        # the trail, in the same row as the change that caused it.
        details["from_status"] = previous_status
        details["to_status"] = subscription.status
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=actor_id,
        action="billing.plan_changed",
        entity_id=subscription.id,
        details=details,
    )
    await control_db.commit()

    return PlanChangeResult(
        changed=True,
        old_plan_code=previous_plan_code,
        new_plan_code=new_plan.code,
        proration=proration,
    )


def _zero_proration(old_monthly: Decimal, new_monthly: Decimal) -> ProrationResult:
    return ProrationResult(
        amount=Decimal("0.00"),
        unused_days=0,
        period_days=0,
        old_monthly=old_monthly,
        new_monthly=new_monthly,
    )
