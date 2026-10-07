"""``scripts/seed.py`` — which plan each seeded tenant lands on.

The seed used to put every org on ``free``, whose entitlements are ``{}``. With
the public API, and since decisions §254 SSO, SCIM, multiple entities, live
ERPs and outbound webhooks too, behind a plan gate, that made whole product
surfaces unreachable on a fresh clone without hand-editing the control plane —
the local-first promise (guard rail 7) broken.

Two things have to be true for the seed to actually take, and both are guarded
here:

1. The plan the demo tenant (and every e2e worker) lands on must really grant
   every feature. That is a cross-module claim — ``seed.ACME_PLAN_CODE`` naming
   a code, and ``plan_catalog.DEFAULT_PLAN_CATALOG`` deciding what that code
   grants — so stripping a grant (or renaming the plan) has to fail loudly here
   rather than silently re-close a surface.
2. Re-running the seed has to REPAIR a control plane seeded before the change.
   ``ensure_subscription`` no-ops once an org holds any live subscription, so
   without the in-place repoint every existing dev box would have stayed 402'd
   forever no matter how many times ``pnpm seed`` was run.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import delete, select

from app.models.billing import Plan, Subscription
from app.services.billing.entitlements import get_entitlements, has_entitlement
from app.services.billing.plan_catalog import (
    ALL_FEATURES,
    DEFAULT_PLAN_CATALOG,
    ensure_plan_catalog,
    ensure_subscription,
)
from scripts import seed as seed_script

_CATALOG_BY_CODE = {spec["code"]: spec for spec in DEFAULT_PLAN_CATALOG}


def _grants_every_feature(entitlements: dict) -> bool:
    return all(has_entitlement(entitlements, f) for f in ALL_FEATURES)


def _grants_no_feature(entitlements: dict) -> bool:
    return not any(has_entitlement(entitlements, f) for f in ALL_FEATURES)


def test_acme_lands_on_a_plan_that_actually_grants_every_feature():
    """``seed.ACME_PLAN_CODE`` is only a *code*; what it grants lives in the
    plan catalog, so assert the two agree. Acme is the tenant the local IdPs
    are wired to, so SSO enforcement and SCIM have to be in it too."""
    spec = _CATALOG_BY_CODE.get(seed_script.ACME_PLAN_CODE)
    assert spec is not None, (
        f"seed.ACME_PLAN_CODE={seed_script.ACME_PLAN_CODE!r} is not in "
        "DEFAULT_PLAN_CATALOG — ensure_subscription would silently no-op and "
        "the demo tenant would end up with no subscription at all"
    )
    assert _grants_every_feature(spec["entitlements"]), (
        f"the {seed_script.ACME_PLAN_CODE!r} plan no longer grants every plan "
        "feature, so part of the product 402s on a fresh clone"
    )


def test_e2e_workers_land_on_a_plan_that_grants_every_feature():
    """The Playwright suite drives every gated surface on its worker's tenant
    (SSO settings, SCIM token, entities, ERP, API keys, webhooks), and the
    workers must stay interchangeable — so all of them get the full plan."""
    spec = _CATALOG_BY_CODE.get(seed_script.E2E_PLAN_CODE)
    assert spec is not None, f"{seed_script.E2E_PLAN_CODE!r} is not in DEFAULT_PLAN_CATALOG"
    assert _grants_every_feature(spec["entitlements"])


def test_techflow_stays_unentitled():
    """``techflow`` keeps the refusal side of every gate — and the upgrade
    prompt — exercisable on a local stack."""
    spec = _CATALOG_BY_CODE.get(seed_script.TECHFLOW_PLAN_CODE)
    assert spec is not None, f"{seed_script.TECHFLOW_PLAN_CODE!r} is not in DEFAULT_PLAN_CATALOG"
    assert _grants_no_feature(spec["entitlements"]), (
        f"{seed_script.TECHFLOW_PLAN_CODE!r} now grants a plan feature — the seed "
        "no longer demonstrates every gate refusing someone"
    )


async def _clear_billing(realdb, org_ids) -> None:
    """Drop every subscription for ``org_ids`` and every default-catalog plan.

    Mirrors ``tests/test_plan_catalog.py::_clear_catalog`` — subscriptions FK
    to plans, so they go first.
    """
    codes = list(_CATALOG_BY_CODE)
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        await s.execute(delete(Subscription).where(Subscription.organization_id.in_(org_ids)))
        plan_ids = (await s.execute(select(Plan.id).where(Plan.code.in_(codes)))).scalars().all()
        if plan_ids:
            await s.execute(delete(Subscription).where(Subscription.plan_id.in_(plan_ids)))
        await s.execute(delete(Plan).where(Plan.code.in_(codes)))
        await s.commit()


def _point_seed_at(monkeypatch, acme_org_id, tech_org_id) -> None:
    """Run the seed's demo-tenant policy against this slot's own orgs.

    ``ensure_demo_billing_baseline`` reads the fixed demo org ids off the
    module, and ``subscriptions.organization_id`` is a real FK — so redirect
    the two ids at orgs that exist in this slot's control plane rather than
    inserting (and having to clean up) fake ``00000000-…-0001`` organizations.
    """
    monkeypatch.setattr(seed_script, "ACME_ORG_ID", acme_org_id)
    monkeypatch.setattr(seed_script, "TECH_ORG_ID", tech_org_id)


async def _live_subscriptions(realdb, org_id):
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        return (
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


async def test_baseline_entitles_the_demo_tenant_and_leaves_the_other_free(realdb, monkeypatch):
    """A fresh control plane: after the seed's billing baseline, the demo
    tenant's live plan grants every feature and the second one grants none."""
    acme_org, tech_org = realdb.info("a").org_id, realdb.info("b").org_id
    _point_seed_at(monkeypatch, acme_org, tech_org)
    await _clear_billing(realdb, [acme_org, tech_org])
    ctrl_mk = realdb.control_sessionmaker()
    try:
        async with ctrl_mk() as s:
            await seed_script.ensure_demo_billing_baseline(s)
            await s.commit()

        async with ctrl_mk() as s:
            acme_ents = await get_entitlements(s, acme_org)
            tech_ents = await get_entitlements(s, tech_org)
        assert _grants_every_feature(acme_ents)
        assert _grants_no_feature(tech_ents)
    finally:
        await _clear_billing(realdb, [acme_org, tech_org])


async def test_re_seed_repairs_a_demo_tenant_stranded_on_free(realdb, monkeypatch):
    """The regression that made this unfixable by re-seeding.

    An older seed parked the demo tenant on ``free`` (and a later one on
    ``growth``). ``ensure_subscription`` no-ops once ANY live subscription
    exists, so a plain re-seed left it there and every gated surface stayed
    402'd. The baseline must repoint the existing row —
    and must still leave exactly one live subscription, because
    ``uq_subscription_one_live_per_org`` forbids a second.
    """
    acme_org, tech_org = realdb.info("a").org_id, realdb.info("b").org_id
    _point_seed_at(monkeypatch, acme_org, tech_org)
    await _clear_billing(realdb, [acme_org, tech_org])
    ctrl_mk = realdb.control_sessionmaker()
    try:
        # Recreate the pre-fix state exactly: catalog present, org live on free.
        async with ctrl_mk() as s:
            await ensure_plan_catalog(s)
            stranded = await ensure_subscription(s, organization_id=acme_org, plan_code="free")
            await s.commit()
        assert stranded is not None
        stranded_id = stranded.id

        async with ctrl_mk() as s:
            ents = await get_entitlements(s, acme_org)
        assert _grants_no_feature(ents), "precondition: the 402 state"

        async with ctrl_mk() as s:
            await seed_script.ensure_demo_billing_baseline(s)
            await s.commit()

        async with ctrl_mk() as s:
            ents = await get_entitlements(s, acme_org)
        assert _grants_every_feature(ents)

        live = await _live_subscriptions(realdb, acme_org)
        assert len(live) == 1, "a repair must repoint the live row, never add a second"
        # Repointed in place — same row id, new plan.
        assert live[0].id == stranded_id
    finally:
        await _clear_billing(realdb, [acme_org, tech_org])


async def test_re_seed_lifts_a_demo_tenant_parked_on_growth(realdb, monkeypatch):
    """The previous seed put acme on ``growth``. That plan grants SSO but not
    SCIM, ``sso_only`` or multiple entities, so a box seeded then must be
    lifted to the full plan by the next ``pnpm seed`` — not left half-entitled
    because the old upgrade-only check asked about ``public_api`` alone."""
    acme_org, tech_org = realdb.info("a").org_id, realdb.info("b").org_id
    _point_seed_at(monkeypatch, acme_org, tech_org)
    await _clear_billing(realdb, [acme_org, tech_org])
    ctrl_mk = realdb.control_sessionmaker()
    try:
        async with ctrl_mk() as s:
            await ensure_plan_catalog(s)
            await ensure_subscription(s, organization_id=acme_org, plan_code="growth")
            await s.commit()

        async with ctrl_mk() as s:
            await seed_script.ensure_demo_billing_baseline(s)
            await s.commit()

        async with ctrl_mk() as s:
            ents = await get_entitlements(s, acme_org)
        assert _grants_every_feature(ents)
        assert len(await _live_subscriptions(realdb, acme_org)) == 1
    finally:
        await _clear_billing(realdb, [acme_org, tech_org])


async def test_baseline_never_downgrades_an_already_entitled_tenant(realdb, monkeypatch):
    """Upgrade-only. An operator who moved the demo tenant onto a plan at least
    as rich as the seed's (a negotiated Enterprise plan, or a billing fixture)
    must not be quietly walked back onto the catalog plan by the next
    ``pnpm seed``."""
    acme_org, tech_org = realdb.info("a").org_id, realdb.info("b").org_id
    _point_seed_at(monkeypatch, acme_org, tech_org)
    await _clear_billing(realdb, [acme_org, tech_org])
    await realdb.purge_plans("seedtest_", org_ids=[acme_org])
    ctrl_mk = realdb.control_sessionmaker()
    try:
        async with ctrl_mk() as s:
            await ensure_plan_catalog(s)
            enterprise = Plan(
                code="seedtest_enterprise",
                name="Enterprise (test)",
                monthly_price=Decimal("999.00"),
                currency="USD",
                entitlements={feature: True for feature in ALL_FEATURES},
                usage_components={},
                trial_days=0,
            )
            s.add(enterprise)
            await s.flush()
            s.add(
                Subscription(
                    organization_id=acme_org,
                    plan_id=enterprise.id,
                    status="active",
                )
            )
            await s.commit()
            enterprise_id = enterprise.id

        async with ctrl_mk() as s:
            await seed_script.ensure_demo_billing_baseline(s)
            await s.commit()

        live = await _live_subscriptions(realdb, acme_org)
        assert len(live) == 1
        assert live[0].plan_id == enterprise_id, "the richer plan was replaced by a re-seed"
    finally:
        await _clear_billing(realdb, [acme_org, tech_org])
        await realdb.purge_plans("seedtest_", org_ids=[acme_org])


async def test_repair_survives_a_leftover_canceled_row_on_the_target_plan(realdb, monkeypatch):
    """``uq_subscription_org_plan`` is ``UNIQUE (organization_id, plan_id)`` and
    does NOT exclude canceled rows, so repointing a live row onto a plan the org
    already holds a canceled row for is an IntegrityError.

    ``services/billing/plan_change.py::change_plan`` — the only other in-place
    repointer — deletes that stale canceled row first for exactly this reason.
    The seed's repair has to do the same, or a demo tenant whose paid
    subscription was once canceled (a dunning sweep, a parked e2e fixture)
    would crash the next ``pnpm seed`` instead of being repaired by it.
    """
    acme_org, tech_org = realdb.info("a").org_id, realdb.info("b").org_id
    _point_seed_at(monkeypatch, acme_org, tech_org)
    await _clear_billing(realdb, [acme_org, tech_org])
    ctrl_mk = realdb.control_sessionmaker()
    try:
        async with ctrl_mk() as s:
            await ensure_plan_catalog(s)
            # A canceled row on the TARGET plan — the collision — plus a live
            # row on `free`, which is the state the repair has to move.
            canceled = await ensure_subscription(
                s, organization_id=acme_org, plan_code=seed_script.ACME_PLAN_CODE
            )
            canceled.status = "canceled"
            await s.flush()
            await ensure_subscription(s, organization_id=acme_org, plan_code="free")
            await s.commit()

        async with ctrl_mk() as s:
            await seed_script.ensure_demo_billing_baseline(s)
            await s.commit()

        async with ctrl_mk() as s:
            ents = await get_entitlements(s, acme_org)
        assert _grants_every_feature(ents)

        live = await _live_subscriptions(realdb, acme_org)
        assert len(live) == 1
    finally:
        await _clear_billing(realdb, [acme_org, tech_org])
