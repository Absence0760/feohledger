"""services/billing/plan_catalog.py — default catalog + baseline Subscription.

Before this module, nothing in the app ever created a Plan or Subscription
row outside of tests (issue #180): every org's `require_entitlement` /
`require_api_entitlement` check silently failed closed, and
`services/billing/plan_change.py::change_plan` 404'd with "no live
subscription" for every org — an admin could never even upgrade. See
`test_tenant_provisioning.py::test_provision_tenant_org_can_upgrade_out_of_free`
for the end-to-end proof; this file covers `plan_catalog.py`'s own idempotency
in isolation.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.models.billing import Plan, Subscription
from app.services.billing import plan_catalog as plan_catalog_module
from app.services.billing.plan_catalog import (
    DEFAULT_PLAN_CATALOG,
    clear_stale_canceled_subscription,
    ensure_plan_catalog,
    ensure_subscription,
)


async def _clear_catalog(realdb) -> None:
    codes = [spec["code"] for spec in DEFAULT_PLAN_CATALOG]
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        # Subscriptions FK to plans — clear any test-created ones first.
        plan_ids = (await s.execute(select(Plan.id).where(Plan.code.in_(codes)))).scalars().all()
        if plan_ids:
            await s.execute(delete(Subscription).where(Subscription.plan_id.in_(plan_ids)))
        await s.execute(delete(Plan).where(Plan.code.in_(codes)))
        await s.commit()


async def test_ensure_plan_catalog_creates_all_default_plans(realdb):
    ctrl_mk = realdb.control_sessionmaker()
    await _clear_catalog(realdb)
    try:
        async with ctrl_mk() as s:
            by_code = await ensure_plan_catalog(s)
            await s.commit()

        assert set(by_code) == {"free", "growth", "scale"}
        assert by_code["free"].entitlements == {"erp_integrations": True}
        assert "erp_merge" not in by_code["growth"].entitlements
        assert by_code["scale"].entitlements.get("erp_merge") is True
        assert by_code["growth"].entitlements.get("public_api") is True
        assert by_code["scale"].entitlements.get("public_api") is True

        async with ctrl_mk() as s:
            rows = (await s.execute(select(Plan).where(Plan.code == "free"))).scalars().all()
        assert len(rows) == 1
    finally:
        await _clear_catalog(realdb)


async def test_ensure_plan_catalog_is_idempotent_and_preserves_edits(realdb):
    """A second call must not duplicate rows, and must not clobber a plan an
    operator has since edited (price/entitlements) — it only fills gaps."""
    ctrl_mk = realdb.control_sessionmaker()
    await _clear_catalog(realdb)
    try:
        async with ctrl_mk() as s:
            await ensure_plan_catalog(s)
            await s.commit()

        # Operator edits the seeded "free" plan's price.
        async with ctrl_mk() as s:
            free_plan = (await s.execute(select(Plan).where(Plan.code == "free"))).scalar_one()
            free_plan.monthly_price = Decimal("9.99")
            await s.commit()

        async with ctrl_mk() as s:
            by_code = await ensure_plan_catalog(s)
            await s.commit()

        assert by_code["free"].monthly_price == Decimal("9.99")

        async with ctrl_mk() as s:
            count = len((await s.execute(select(Plan).where(Plan.code == "free"))).scalars().all())
        assert count == 1
    finally:
        await _clear_catalog(realdb)


async def test_ensure_subscription_creates_then_is_a_noop(realdb):
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    await _clear_catalog(realdb)
    try:
        async with ctrl_mk() as s:
            await ensure_plan_catalog(s)
            await s.commit()

        async with ctrl_mk() as s:
            sub1 = await ensure_subscription(s, organization_id=org_id, plan_code="free")
            await s.commit()
        assert sub1 is not None
        assert sub1.status == "active"

        # Calling again (e.g. a re-seed) must not create a second live row.
        async with ctrl_mk() as s:
            sub2 = await ensure_subscription(s, organization_id=org_id, plan_code="free")
            await s.commit()
        assert sub2.id == sub1.id

        async with ctrl_mk() as s:
            live_count = (
                (
                    await s.execute(
                        select(Subscription).where(
                            Subscription.organization_id == org_id,
                            Subscription.status != "canceled",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(live_count) == 1
    finally:
        async with ctrl_mk() as s:
            await s.execute(delete(Subscription).where(Subscription.organization_id == org_id))
            await s.commit()
        await _clear_catalog(realdb)


async def test_ensure_subscription_returns_none_for_unknown_plan_code(realdb):
    """A fresh control DB before ensure_plan_catalog has run, or a caller
    passing a code that was never seeded, must not crash provisioning —
    mirrors the admin-role skip-silently pattern already used elsewhere in
    tenant_provisioning."""
    ctrl_mk = realdb.control_sessionmaker()
    org_id = uuid.uuid4()  # doesn't need to be a real org for this pure check
    async with ctrl_mk() as s:
        result = await ensure_subscription(
            s, organization_id=org_id, plan_code=f"nonexistent_{uuid.uuid4().hex[:8]}"
        )
    assert result is None


# --- `uq_subscription_org_plan` is (org, plan) with NO status filter ---------
#
# `uq_subscription_one_live_per_org` bounds the LIVE count; this second
# constraint bounds the TOTAL, so a canceled row keeps occupying its (org, plan)
# slot forever. Every writer that tries to put an org back onto a plan it once
# held has to free that slot first — `change_plan` always did it inline, and
# `ensure_subscription` did not, so an org whose subscription was canceled (the
# dunning sweep is the path that does that) could never resubscribe to the same
# plan: the INSERT raised IntegrityError. `clear_stale_canceled_subscription` is
# now the one owner of that rule.


async def _seed_canceled(realdb, org_id, plan_code: str):
    """Leave `org_id` with a CANCELED subscription on `plan_code` and no live
    one — the state the dunning sweep produces."""
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        await ensure_plan_catalog(s)
        sub = await ensure_subscription(s, organization_id=org_id, plan_code=plan_code)
        sub.status = "canceled"
        await s.commit()
        return sub.id


async def test_ensure_subscription_resubscribes_after_a_cancellation(realdb):
    """The fix. Re-binding an org to the plan it was canceled on must succeed."""
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    await _clear_catalog(realdb)
    try:
        canceled_id = await _seed_canceled(realdb, org_id, "growth")

        async with ctrl_mk() as s:
            revived = await ensure_subscription(s, organization_id=org_id, plan_code="growth")
            await s.commit()
        assert revived is not None
        assert revived.status == "active"
        assert revived.id != canceled_id, "a fresh row, not the canceled one reused"

        async with ctrl_mk() as s:
            rows = (
                (
                    await s.execute(
                        select(Subscription).where(Subscription.organization_id == org_id)
                    )
                )
                .scalars()
                .all()
            )
        # The stale canceled row was consumed to free the slot; exactly one
        # row for this (org, plan), and it is the live one.
        assert len(rows) == 1
        assert rows[0].id == revived.id
    finally:
        async with ctrl_mk() as s:
            await s.execute(delete(Subscription).where(Subscription.organization_id == org_id))
            await s.commit()
        await _clear_catalog(realdb)


async def test_without_the_guard_the_same_call_raises_integrityerror(realdb, monkeypatch):
    """The repro: neutralise the guard and the INSERT collides, proving the
    constraint is real and that the guard — not luck — is what avoids it."""
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    await _clear_catalog(realdb)
    try:
        await _seed_canceled(realdb, org_id, "growth")

        async def _noop(session, *, organization_id, plan_id):
            return None

        monkeypatch.setattr(plan_catalog_module, "clear_stale_canceled_subscription", _noop)

        async with ctrl_mk() as s:
            with pytest.raises(IntegrityError) as excinfo:
                await ensure_subscription(s, organization_id=org_id, plan_code="growth")
            await s.rollback()
        assert "uq_subscription_org_plan" in str(excinfo.value)
    finally:
        async with ctrl_mk() as s:
            await s.execute(delete(Subscription).where(Subscription.organization_id == org_id))
            await s.commit()
        await _clear_catalog(realdb)


async def test_clear_stale_canceled_subscription_deletes_only_its_own_target(realdb):
    """Narrow by construction: it frees ONE (org, plan) slot. It must not touch
    a canceled row on a different plan (that org's history) nor a LIVE row on
    the target plan (deleting that would drop a paying subscription)."""
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    await _clear_catalog(realdb)
    try:
        async with ctrl_mk() as s:
            await ensure_plan_catalog(s)
            growth_id = (await s.execute(select(Plan.id).where(Plan.code == "growth"))).scalar_one()
            scale_id = (await s.execute(select(Plan.id).where(Plan.code == "scale"))).scalar_one()
            free_id = (await s.execute(select(Plan.id).where(Plan.code == "free"))).scalar_one()
            # canceled on growth (the target), canceled on scale (history),
            # live on free (untouchable).
            for plan_id, status in (
                (growth_id, "canceled"),
                (scale_id, "canceled"),
                (free_id, "active"),
            ):
                s.add(
                    Subscription(
                        id=uuid.uuid4(),
                        organization_id=org_id,
                        plan_id=plan_id,
                        status=status,
                    )
                )
            await s.commit()

        async with ctrl_mk() as s:
            await clear_stale_canceled_subscription(s, organization_id=org_id, plan_id=growth_id)
            await s.commit()

        async with ctrl_mk() as s:
            remaining = {
                (r.plan_id, r.status)
                for r in (
                    await s.execute(
                        select(Subscription).where(Subscription.organization_id == org_id)
                    )
                )
                .scalars()
                .all()
            }
        assert remaining == {(scale_id, "canceled"), (free_id, "active")}

        # And deleting a LIVE row on the target plan is out of scope too.
        async with ctrl_mk() as s:
            await clear_stale_canceled_subscription(s, organization_id=org_id, plan_id=free_id)
            await s.commit()
        async with ctrl_mk() as s:
            live = (
                (
                    await s.execute(
                        select(Subscription).where(
                            Subscription.organization_id == org_id,
                            Subscription.plan_id == free_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(live) == 1, "a live subscription must never be cleared"
    finally:
        async with ctrl_mk() as s:
            await s.execute(delete(Subscription).where(Subscription.organization_id == org_id))
            await s.commit()
        await _clear_catalog(realdb)
