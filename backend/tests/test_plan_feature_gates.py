"""Per-tier feature gates (docs/decisions.md §253 the tiers, §258 the gates).

Each gate is driven three ways where it applies: the plan lacks the feature →
a coded 402 (``plan_feature_required`` naming the feature), the plan has it →
allowed, and — for SSO, SCIM, entities, webhooks — what a DOWNGRADED tenant
keeps. The harness's orgs hold no subscription unless a test arranges one
(``realdb.subscribe``), which reads exactly like ``free`` — Free's grants, which
since decisions §281 are the direct ERP adapters and nothing else.

The downgrade rules this file pins, because each is a deliberate call (§258):

* SSO: a plan without ``sso`` reads the stored block as switched off — no IdP
  button, no SSO handshake — and a plan without ``sso_enforcement`` reads
  ``sso_only`` as off. Password sign-in REOPENS rather than nobody being able
  to sign in. The stored block is never rewritten.
* SCIM: reads and deprovisioning stay open, so an IdP can still shut out a
  leaver; provisioning and group grants are refused.
* Entities: only CREATING one is gated; existing extra entities keep working.
* Webhooks: switching a subscription off stays open; nothing new is queued.
* ERP: only a LIVE adapter is gated; ``mock`` stays open on every plan. Every
  catalog plan, Free included, grants the direct adapters (§281); a config that
  routes through Merge.dev needs ``erp_merge`` too, which only Scale grants
  (§256). The refusal side of ``erp_integrations`` is a negotiated plan that
  leaves it out (``_negotiated_plan``), since no catalog plan does.
"""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import PLAN_FEATURE_REQUIRED, plan_feature_refusal
from app.models.organization import Organization
from app.models.user import User
from app.services.billing.entitlements import NO_PLAN_ENTITLEMENTS
from app.services.billing.plan_catalog import (
    ALL_FEATURES,
    FEATURE_ERP_INTEGRATIONS,
    FEATURE_ERP_MERGE,
    FEATURE_MULTI_ENTITY,
    FEATURE_PUBLIC_API,
    FEATURE_SCIM,
    FEATURE_SSO,
    FEATURE_SSO_ENFORCEMENT,
)
from app.services.erp_adapters.dispatcher import erp_config_is_live
from app.services.sso_plan import plan_scoped_settings

OIDC_READY = {
    "enabled": True,
    "sso_only": True,
    "protocol": "oidc",
    "provider": "okta",
    "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
    "client_id": "feoh",
    "client_secret": "not-a-real-secret",
}
SAML_READY = {
    "enabled": True,
    "sso_only": True,
    "protocol": "saml",
    "provider": "saml",
    "idp_entity_id": "https://idp.example.com/saml",
    "idp_sso_url": "https://idp.example.com/saml/sso",
    "idp_x509_cert": base64.b64encode(b"fake-but-valid-base64-der-bytes").decode(),
}
LIVE_ERP = {"type": "netsuite", "integration_method": "direct", "account_id": "1234567"}
MOCK_ERP = {"type": "mock", "integration_method": "direct"}
MERGE_ERP = {"type": "sage_intacct", "integration_method": "merge_dev"}
# No `integration_method`: the dispatcher defaults it to Merge.dev, so the gate
# must read this as the Merge connection it is, not as a direct Xero.
TYPE_ONLY_ERP = {"type": "xero"}
NEGOTIATED_PREFIX = "gate_test_"


def _assert_plan_refusal(resp, feature: str) -> None:
    assert resp.status_code == 402, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == PLAN_FEATURE_REQUIRED
    assert detail["params"] == {"feature": feature}


async def _write_settings(realdb, key: str = "a", **values) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        merged = dict(org.settings or {})
        for name, value in values.items():
            if value is None:
                merged.pop(name, None)
            else:
                merged[name] = value
        org.settings = merged
        flag_modified(org, "settings")
        await s.commit()


async def _negotiated_plan(realdb, entitlements: dict, key: str = "a") -> None:
    """Bind ``key``'s org to a throwaway non-catalog plan granting ``entitlements``
    — the shape of an operator-configured Enterprise contract, and the only way
    left to reach a refusal for a feature every catalog plan grants. Remove it
    with ``realdb.purge_plans(NEGOTIATED_PREFIX, org_ids=[...])``."""
    from decimal import Decimal

    from app.models.billing import Plan, Subscription

    org_id = realdb.info(key).org_id
    async with realdb.control_sessionmaker()() as s:
        await s.execute(delete(Subscription).where(Subscription.organization_id == org_id))
        plan = Plan(
            id=uuid.uuid4(),
            code=f"{NEGOTIATED_PREFIX}{uuid.uuid4().hex[:8]}",
            name="Negotiated",
            monthly_price=Decimal("0.00"),
            currency="USD",
            entitlements=entitlements,
            trial_days=0,
        )
        s.add(plan)
        await s.flush()
        s.add(
            Subscription(id=uuid.uuid4(), organization_id=org_id, plan_id=plan.id, status="active")
        )
        await s.commit()


async def _purge_negotiated(realdb, key: str = "a") -> None:
    await realdb.purge_plans(NEGOTIATED_PREFIX, org_ids=[realdb.info(key).org_id])


async def _approved_invoice(realdb, prefix: str, key: str = "a") -> uuid.UUID:
    from decimal import Decimal

    from app.models.invoice import Invoice, InvoiceStatus

    invoice_id = uuid.uuid4()
    async with realdb.sessionmaker(key)() as s:
        s.add(
            Invoice(
                id=invoice_id,
                correlation_id=uuid.uuid4(),
                organization_id=realdb.info(key).org_id,
                vendor_name="Plan Gate Supplies",
                invoice_number=f"{prefix}-{invoice_id.hex[:6]}",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.approved,
            )
        )
        await s.commit()
    return invoice_id


async def _invoice_status(realdb, invoice_id, key: str = "a"):
    from app.models.invoice import Invoice

    async with realdb.sessionmaker(key)() as s:
        return (await s.get(Invoice, invoice_id)).status


async def _org(realdb, key: str = "a") -> Organization:
    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


def test_the_refusal_is_coded_and_names_the_feature():
    exc = plan_feature_refusal(FEATURE_SSO)
    assert exc.status_code == 402
    assert exc.detail["code"] == PLAN_FEATURE_REQUIRED
    assert exc.detail["params"] == {"feature": FEATURE_SSO}
    assert exc.detail["message"]


@pytest.mark.parametrize(
    "config,live",
    [
        (None, False),
        ({}, False),
        ("netsuite", False),
        (MOCK_ERP, False),
        ({"integration_method": "direct"}, False),  # blank type resolves to mock
        (LIVE_ERP, True),
        ({"type": "dynamics_365_bc", "integration_method": "direct"}, True),
        # `integration_method` defaults to merge_dev, which wins over `type`.
        ({"type": "mock"}, True),
        ({"type": "netsuite", "integration_method": "merge_dev"}, True),
    ],
)
def test_erp_config_is_live_matches_the_dispatcher(config, live):
    assert erp_config_is_live(config) is live


@pytest.mark.parametrize(
    "grants,enabled,sso_only",
    [
        ({}, False, False),
        ({FEATURE_SSO: True}, True, False),
        ({FEATURE_SSO: True, FEATURE_SSO_ENFORCEMENT: True}, True, True),
        # Enforcement without SSO itself still cannot close the password.
        ({FEATURE_SSO_ENFORCEMENT: True}, False, True),
    ],
)
def test_plan_scoped_settings_switches_off_what_the_plan_lacks(grants, enabled, sso_only):
    stored = {"brand": {"name": "x"}, "sso": dict(OIDC_READY)}
    scoped = plan_scoped_settings(stored, grants)
    assert scoped["sso"]["enabled"] is enabled
    assert scoped["sso"]["sso_only"] is sso_only
    # Everything else passes through, and the stored dict is never mutated.
    assert scoped["brand"] == {"name": "x"}
    assert scoped["sso"]["client_id"] == "feoh"
    assert stored["sso"]["enabled"] is True and stored["sso"]["sso_only"] is True


def test_plan_scoped_settings_passes_through_a_settings_dict_without_sso():
    assert plan_scoped_settings(None, {}) is None
    assert plan_scoped_settings({"brand": {}}, {}) == {"brand": {}}


# ---------------------------------------------------------------------------
# /auth/me exposes the granted feature keys
# ---------------------------------------------------------------------------


def test_no_plan_reads_as_free():
    """An org with no live plan (provisioned before baseline subscriptions, or
    canceled by dunning) is granted what Free grants — no more, and, since Free
    carries the direct ERP adapters (§281), no less either."""
    from app.services.billing.plan_catalog import DEFAULT_PLAN_CATALOG

    free = next(p for p in DEFAULT_PLAN_CATALOG if p["code"] == "free")
    assert NO_PLAN_ENTITLEMENTS == free["entitlements"] == {FEATURE_ERP_INTEGRATIONS: True}


@pytest.mark.asyncio
async def test_a_lapsed_paid_plan_reads_as_free_not_below_it(realdb):
    """A Scale subscription the dunning sweep canceled leaves no live plan; the
    org keeps Free's direct ERP rather than dropping to nothing (§281)."""
    from app.models.billing import Subscription

    await realdb.subscribe("a", "scale")
    async with realdb.control_sessionmaker()() as s:
        sub = (
            await s.execute(
                select(Subscription).where(Subscription.organization_id == realdb.info("a").org_id)
            )
        ).scalar_one()
        sub.status = "canceled"
        await s.commit()

    async with realdb.client(key="a") as c:
        assert (await c.get("/api/auth/me")).json()["entitlements"] == [FEATURE_ERP_INTEGRATIONS]
        saved = await c.patch("/api/organization", json={"settings": {"erp": LIVE_ERP}})
        assert saved.status_code == 200, saved.text
        _assert_plan_refusal(
            await c.patch("/api/organization", json={"settings": {"erp": MERGE_ERP}}),
            FEATURE_ERP_MERGE,
        )


@pytest.mark.asyncio
async def test_auth_me_lists_the_granted_features(realdb):
    async with realdb.client(key="a") as c:
        assert (await c.get("/api/auth/me")).json()["entitlements"] == [FEATURE_ERP_INTEGRATIONS]

    await realdb.subscribe("a", "free")
    async with realdb.client(key="a") as c:
        assert (await c.get("/api/auth/me")).json()["entitlements"] == [FEATURE_ERP_INTEGRATIONS]

    await realdb.subscribe("a", "growth")
    async with realdb.client(key="a") as c:
        assert (await c.get("/api/auth/me")).json()["entitlements"] == sorted(
            [FEATURE_ERP_INTEGRATIONS, FEATURE_PUBLIC_API, FEATURE_SSO]
        )

    await realdb.subscribe("a", "scale")
    async with realdb.client(key="a") as c:
        assert (await c.get("/api/auth/me")).json()["entitlements"] == sorted(ALL_FEATURES)


# ---------------------------------------------------------------------------
# SSO configuration (PUT /api/organization/sso)
# ---------------------------------------------------------------------------

SSO_URL = "/api/organization/sso"


@pytest.mark.asyncio
async def test_enabling_sso_needs_the_sso_feature(realdb):
    body = {**OIDC_READY, "sso_only": False}
    async with realdb.client(key="a") as c:
        _assert_plan_refusal(await c.put(SSO_URL, json=body), FEATURE_SSO)

    await realdb.subscribe("a", "growth")
    async with realdb.client(key="a") as c:
        resp = await c.put(SSO_URL, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True


@pytest.mark.asyncio
async def test_require_sso_needs_the_enforcement_feature(realdb):
    await realdb.subscribe("a", "growth")
    async with realdb.client(key="a") as c:
        _assert_plan_refusal(await c.put(SSO_URL, json=OIDC_READY), FEATURE_SSO_ENFORCEMENT)

    await realdb.subscribe("a", "scale")
    async with realdb.client(key="a") as c:
        resp = await c.put(SSO_URL, json=OIDC_READY)
    assert resp.status_code == 200, resp.text
    assert resp.json()["sso_only"] is True
    assert resp.json()["password_sign_in_closed"] is True


@pytest.mark.asyncio
async def test_a_downgraded_tenant_can_always_switch_sso_off(realdb):
    """Turning a feature OFF is never refused — whatever the stored block says."""
    await _write_settings(realdb, sso=dict(OIDC_READY))
    async with realdb.client(key="a") as c:
        resp = await c.put(SSO_URL, json={**OIDC_READY, "enabled": False, "sso_only": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False


@pytest.mark.asyncio
async def test_a_changed_scim_role_map_needs_the_scim_feature(realdb):
    await realdb.subscribe("a", "growth")
    body = {
        **OIDC_READY,
        "sso_only": False,
        "scim_group_role_map": {"AP Managers": "ap_manager"},
    }
    async with realdb.client(key="a") as c:
        _assert_plan_refusal(await c.put(SSO_URL, json=body), FEATURE_SCIM)


@pytest.mark.asyncio
async def test_the_panel_reports_the_plan_scoped_posture(realdb):
    """A stored `sso_only` the plan does not honour closes nothing — and the
    admin panel says so instead of echoing the stored flag as the verdict."""
    await _write_settings(realdb, sso=dict(OIDC_READY))
    await realdb.subscribe("a", "growth")
    async with realdb.client(key="a") as c:
        status = (await c.get(SSO_URL)).json()
    assert status["sso_only"] is True  # what was configured
    assert status["password_sign_in_closed"] is False  # what sign-in does


# ---------------------------------------------------------------------------
# SSO sign-in after a downgrade — the password reopens, nobody is locked out
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis(monkeypatch):
    """A successful login registers a session, which needs the zset+hash
    Redis stand-in rather than the key/value-only autouse stub."""
    from tests.test_session_management import FakeRedis

    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


async def _throwaway_user(realdb, password: str) -> str:
    from app.utils.passwords import pwd_context

    email = f"plan-gate-{uuid.uuid4().hex[:10]}@{realdb.info('a').slug}.test"
    async with realdb.control_sessionmaker()() as s:
        s.add(
            User(
                id=uuid.uuid4(),
                email=email,
                full_name="Plan gate probe",
                hashed_password=pwd_context.hash(password),
                is_active=True,
                organization_id=realdb.info("a").org_id,
                must_change_password=False,
            )
        )
        await s.commit()
    return email


@pytest.mark.asyncio
@pytest.mark.parametrize("block", [OIDC_READY, SAML_READY], ids=["oidc", "saml"])
async def test_sign_in_follows_the_plan_through_a_downgrade(realdb, fake_redis, block):
    pw = "Correct-Horse-9"
    email = await _throwaway_user(realdb, pw)
    slug = realdb.info("a").slug
    proto = "saml" if block is SAML_READY else "sso"
    await _write_settings(realdb, sso=dict(block))

    async def _probe():
        async with realdb.client(key="a", role=None) as c:
            config = (await c.get(f"/api/auth/{proto}/config?slug={slug}")).json()
            login = await c.post("/api/auth/login", json={"email": email, "password": pw})
            me = None
            if login.status_code == 200:
                token = login.json()["access_token"]
                me = (
                    await c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
                ).json()
        return config, login, me

    # Scale: SSO on, password closed.
    await realdb.subscribe("a", "scale")
    config, login, _ = await _probe()
    assert config["enabled"] is True and config["sso_only"] is True
    assert login.status_code == 403, login.text

    # Growth: SSO still offered, but `sso_only` is a Scale feature — the
    # password reopens and the page shows its form again.
    await realdb.subscribe("a", "growth")
    config, login, me = await _probe()
    assert config["enabled"] is True and config["sso_only"] is False
    assert login.status_code == 200, login.text
    assert me["password_sign_in_closed"] is False

    # Free: no SSO at all. No button, and the password is the way in.
    await realdb.subscribe("a", "free")
    config, login, me = await _probe()
    assert config == {"enabled": False, "provider": None, "sso_only": False}
    assert login.status_code == 200, login.text
    assert me["password_sign_in_closed"] is False

    # The stored block was never rewritten — an upgrade resumes it as it was.
    stored = (await _org(realdb)).settings["sso"]
    assert stored["enabled"] is True and stored["sso_only"] is True


@pytest.mark.asyncio
async def test_the_sso_handshake_refuses_without_the_sso_feature(realdb, fake_redis):
    """Free: /authorize answers exactly as for an unconfigured tenant."""
    await _write_settings(realdb, sso=dict(OIDC_READY))
    slug = realdb.info("a").slug
    async with realdb.client(key="a", role=None) as c:
        oidc = await c.get(f"/api/auth/sso/authorize?slug={slug}", follow_redirects=False)
    assert oidc.status_code == 400
    assert oidc.json()["detail"] == "SSO is not configured for this tenant."

    await _write_settings(realdb, sso=dict(SAML_READY))
    async with realdb.client(key="a", role=None) as c:
        saml = await c.get(f"/api/auth/saml/login?slug={slug}", follow_redirects=False)
        metadata = await c.get(f"/api/auth/saml/metadata?slug={slug}")
    assert saml.status_code == 400
    assert metadata.status_code == 404


# ---------------------------------------------------------------------------
# SCIM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minting_a_scim_token_needs_the_scim_feature(realdb):
    await realdb.subscribe("a", "growth")
    async with realdb.client(key="a") as c:
        _assert_plan_refusal(await c.post("/api/organization/sso/scim-token"), FEATURE_SCIM)

    await realdb.subscribe("a", "scale")
    try:
        async with realdb.client(key="a") as c:
            resp = await c.post("/api/organization/sso/scim-token")
        assert resp.status_code == 200, resp.text
    finally:
        async with realdb.control_sessionmaker()() as s:
            org = await s.get(Organization, realdb.info("a").org_id)
            org.scim_bearer_hash = None
            await s.commit()


_REQUEST = SimpleNamespace(base_url="http://testserver/")


async def _scim_user(realdb, *, active: bool = True) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with realdb.control_sessionmaker()() as s:
        s.add(
            User(
                id=user_id,
                email=f"scim-plan-{user_id}@example.test",
                full_name="SCIM plan probe",
                hashed_password=None,
                organization_id=realdb.info("a").org_id,
                is_active=active,
                must_change_password=False,
            )
        )
        await s.commit()
    return user_id


async def _drop_user(realdb, user_id) -> None:
    async with realdb.control_sessionmaker()() as s:
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def _is_active(realdb, user_id) -> bool:
    async with realdb.control_sessionmaker()() as s:
        return (await s.get(User, user_id)).is_active


@pytest.mark.asyncio
async def test_scim_provisioning_without_the_feature_is_a_scim_shaped_402(realdb):
    from app.api.scim import create_group, create_user
    from app.schemas.scim import SCIMGroupCreate, SCIMUserCreate

    org = await _org(realdb)
    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(HTTPException) as exc:
            await create_user(
                SCIMUserCreate(userName=f"new-{uuid.uuid4().hex[:6]}@example.test"),
                _REQUEST,
                org,
                s,
            )
    assert exc.value.status_code == 402
    assert exc.value.detail["status"] == "402"
    assert "urn:ietf:params:scim:api:messages:2.0:Error" in exc.value.detail["schemas"]

    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(HTTPException) as exc:
            await create_group(SCIMGroupCreate(displayName="AP"), _REQUEST, org, s)
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_scim_deprovisioning_survives_a_downgrade(realdb):
    """A leaver must still be shut out by the IdP after the tenant drops below
    Scale — otherwise their account stays active, and without SSO its password
    sign-in is open."""
    from app.api.scim import delete_user, get_user, patch_user, replace_user
    from app.schemas.scim import SCIMPatchOp, SCIMPatchRequest, SCIMUserCreate

    org = await _org(realdb)
    ids = [await _scim_user(realdb) for _ in range(3)]
    try:
        # Reads stay open (Okta/Entra look the user up first).
        async with realdb.control_sessionmaker()() as s:
            assert (await get_user(ids[0], _REQUEST, org, s)).active is True

        # PATCH active=false — Entra's string spelling included.
        async with realdb.control_sessionmaker()() as s:
            await patch_user(
                ids[0],
                SCIMPatchRequest(
                    Operations=[SCIMPatchOp(op="Replace", path="active", value="False")]
                ),
                _REQUEST,
                org,
                s,
            )
            await s.commit()
        assert await _is_active(realdb, ids[0]) is False

        # PUT active=false applies ONLY the deactivation.
        async with realdb.control_sessionmaker()() as s:
            before = (await s.get(User, ids[1])).email
            await replace_user(
                ids[1],
                SCIMUserCreate(userName="renamed@example.test", active=False),
                _REQUEST,
                org,
                s,
            )
            await s.commit()
        async with realdb.control_sessionmaker()() as s:
            row = await s.get(User, ids[1])
        assert row.is_active is False
        assert row.email == before, "a downgraded PUT must not write anything but the deactivation"

        # DELETE (soft) stays open.
        async with realdb.control_sessionmaker()() as s:
            await delete_user(ids[2], org, s)
            await s.commit()
        assert await _is_active(realdb, ids[2]) is False
    finally:
        for user_id in ids:
            await _drop_user(realdb, user_id)


@pytest.mark.asyncio
async def test_scim_changes_other_than_deprovisioning_need_the_feature(realdb):
    from app.api.scim import patch_user, replace_user
    from app.schemas.scim import SCIMPatchOp, SCIMPatchRequest, SCIMUserCreate

    org = await _org(realdb)
    user_id = await _scim_user(realdb, active=False)
    try:
        for body in (
            # Re-activating a user is a grant, not a deprovision.
            SCIMPatchRequest(Operations=[SCIMPatchOp(op="Replace", path="active", value=True)]),
            SCIMPatchRequest(
                Operations=[
                    SCIMPatchOp(op="Replace", path="active", value=False),
                    SCIMPatchOp(op="Replace", path="userName", value="x@example.test"),
                ]
            ),
        ):
            async with realdb.control_sessionmaker()() as s:
                with pytest.raises(HTTPException) as exc:
                    await patch_user(user_id, body, _REQUEST, org, s)
            assert exc.value.status_code == 402

        async with realdb.control_sessionmaker()() as s:
            with pytest.raises(HTTPException) as exc:
                await replace_user(
                    user_id, SCIMUserCreate(userName="y@example.test"), _REQUEST, org, s
                )
        assert exc.value.status_code == 402
        assert await _is_active(realdb, user_id) is False

        # With the feature, the same re-activation goes through.
        await realdb.subscribe("a", "scale")
        async with realdb.control_sessionmaker()() as s:
            await patch_user(
                user_id,
                SCIMPatchRequest(Operations=[SCIMPatchOp(op="Replace", path="active", value=True)]),
                _REQUEST,
                org,
                s,
            )
            await s.commit()
        assert await _is_active(realdb, user_id) is True
    finally:
        await _drop_user(realdb, user_id)


# ---------------------------------------------------------------------------
# Multiple entities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creating_a_second_entity_needs_the_multi_entity_feature(realdb):
    body = {"name": "US Inc", "slug": "us-inc", "currency": "USD"}
    await realdb.subscribe("a", "growth")
    async with realdb.client(key="a") as c:
        _assert_plan_refusal(await c.post("/api/entities", json=body), FEATURE_MULTI_ENTITY)

    await realdb.subscribe("a", "scale")
    async with realdb.client(key="a") as c:
        resp = await c.post("/api/entities", json=body)
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_existing_entities_keep_working_after_a_downgrade(realdb):
    await realdb.subscribe("a", "scale")
    async with realdb.client(key="a") as c:
        created = (await c.post("/api/entities", json={"name": "UK Ltd", "slug": "uk-ltd"})).json()

    await realdb.subscribe("a", "free")
    async with realdb.client(key="a") as c:
        listed = await c.get("/api/entities")
        renamed = await c.patch(f"/api/entities/{created['id']}", json={"name": "UK Limited"})
        deactivated = await c.patch(f"/api/entities/{created['id']}", json={"is_active": False})
        reactivated = await c.patch(f"/api/entities/{created['id']}", json={"is_active": True})
        defaulted = await c.post(f"/api/entities/{created['id']}/set-default")
        scoped = await c.get("/api/invoices", headers={"X-Entity-ID": created["id"]})
    assert created["id"] in {e["id"] for e in listed.json()}
    for resp in (renamed, deactivated, reactivated, defaulted, scoped):
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# ERP integrations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saving_a_live_erp_needs_the_erp_feature(realdb):
    await _negotiated_plan(realdb, {})
    try:
        async with realdb.client(key="a") as c:
            _assert_plan_refusal(
                await c.patch("/api/organization", json={"settings": {"erp": LIVE_ERP}}),
                FEATURE_ERP_INTEGRATIONS,
            )
            # The local-first mock ERP is open on every plan (guard rail 7).
            mock = await c.patch("/api/organization", json={"settings": {"erp": MOCK_ERP}})
            assert mock.status_code == 200, mock.text
    finally:
        await _purge_negotiated(realdb)

    await realdb.subscribe("a", "growth")
    async with realdb.client(key="a") as c:
        resp = await c.patch("/api/organization", json={"settings": {"erp": LIVE_ERP}})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_free_connects_a_direct_erp_and_pushes_to_it(realdb, monkeypatch):
    """§281: Free is one direct ERP that imports and pushes. The save and the
    push both pass the gate on the Free plan itself."""
    from app.models.invoice import InvoiceStatus
    from app.services import erp_send

    dispatched: list[uuid.UUID] = []

    async def _record(invoice_id, *_args, **_kwargs):
        dispatched.append(invoice_id)

    # The adapter call behind the push is not this test's subject; the gate is.
    monkeypatch.setattr(erp_send, "dispatch_erp", _record)

    await realdb.subscribe("a", "free")
    async with realdb.client(key="a") as c:
        saved = await c.patch("/api/organization", json={"settings": {"erp": LIVE_ERP}})
    assert saved.status_code == 200, saved.text

    invoice_id = await _approved_invoice(realdb, "FP")
    async with realdb.client(key="a") as c:
        pushed = await c.post(f"/api/invoices/{invoice_id}/send-to-erp")
    assert pushed.status_code == 202, pushed.text
    assert dispatched == [invoice_id]
    assert await _invoice_status(realdb, invoice_id) == InvoiceStatus.sending_to_erp


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["free", "growth"])
async def test_merge_dev_is_scale_only(realdb, plan):
    """§256: every Merge connection past the first three is billed to us, so
    neither Free nor Growth may save, test or push one — including a config
    that names only a `type`, which the dispatcher routes through Merge."""
    from app.models.invoice import InvoiceStatus

    await realdb.subscribe("a", plan)
    async with realdb.client(key="a") as c:
        for config in (MERGE_ERP, TYPE_ONLY_ERP):
            _assert_plan_refusal(
                await c.patch("/api/organization", json={"settings": {"erp": config}}),
                FEATURE_ERP_MERGE,
            )
        _assert_plan_refusal(
            await c.post("/api/organization/test-erp", json=MERGE_ERP), FEATURE_ERP_MERGE
        )

    # A Merge config already stored (a Scale tenant that downgraded) is refused
    # before the transition, like any other push the plan lacks (§258).
    invoice_id = await _approved_invoice(realdb, "MG")
    await _write_settings(realdb, erp=dict(MERGE_ERP))
    async with realdb.client(key="a") as c:
        _assert_plan_refusal(
            await c.post(f"/api/invoices/{invoice_id}/send-to-erp"), FEATURE_ERP_MERGE
        )
        _assert_plan_refusal(await c.post("/api/vendors/sync-erp"), FEATURE_ERP_MERGE)
    assert await _invoice_status(realdb, invoice_id) == InvoiceStatus.approved

    await realdb.subscribe("a", "scale")
    async with realdb.client(key="a") as c:
        resp = await c.patch("/api/organization", json={"settings": {"erp": MERGE_ERP}})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_a_downgraded_tenant_can_still_save_unrelated_settings(realdb):
    """Only the `erp` key a PATCH carries is checked — a stored live ERP must not
    stop a downgraded tenant from editing its company profile."""
    await _write_settings(realdb, erp=dict(LIVE_ERP))
    async with realdb.client(key="a") as c:
        resp = await c.patch(
            "/api/organization", json={"settings": {"company": {"name": "Acme Holdings"}}}
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_reaching_a_live_erp_needs_the_erp_feature(realdb):
    await _negotiated_plan(realdb, {})
    try:
        async with realdb.client(key="a") as c:
            _assert_plan_refusal(
                await c.post("/api/organization/test-erp", json=LIVE_ERP),
                FEATURE_ERP_INTEGRATIONS,
            )
            mock_test = await c.post("/api/organization/test-erp", json=MOCK_ERP)
        assert mock_test.status_code == 200
        assert mock_test.json()["success"] is True

        await _write_settings(realdb, erp=dict(LIVE_ERP))
        async with realdb.client(key="a") as c:
            for path in (
                "/api/vendors/sync-erp",
                "/api/gl-accounts/sync-erp",
                "/api/purchase-orders/sync-erp",
            ):
                _assert_plan_refusal(await c.post(path), FEATURE_ERP_INTEGRATIONS)
    finally:
        await _purge_negotiated(realdb)


@pytest.mark.asyncio
async def test_sending_an_invoice_to_a_live_erp_needs_the_erp_feature(realdb):
    """Refused BEFORE the transition: the invoice stays `approved` (still
    payable directly) instead of parking in `sending_to_erp`."""
    from app.models.invoice import InvoiceStatus

    invoice_id = await _approved_invoice(realdb, "PG")
    await _write_settings(realdb, erp=dict(LIVE_ERP))
    await _negotiated_plan(realdb, {})
    try:
        async with realdb.client(key="a") as c:
            _assert_plan_refusal(
                await c.post(f"/api/invoices/{invoice_id}/send-to-erp"), FEATURE_ERP_INTEGRATIONS
            )
    finally:
        await _purge_negotiated(realdb)
    assert await _invoice_status(realdb, invoice_id) == InvoiceStatus.approved


# ---------------------------------------------------------------------------
# Public API: API keys + outbound webhooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minting_an_api_key_needs_the_public_api_feature(realdb):
    async with realdb.client(key="a") as c:
        _assert_plan_refusal(await c.post("/api/api-keys", json={"name": "k"}), FEATURE_PUBLIC_API)
        # Listing stays open, so a downgraded tenant can still see and revoke.
        assert (await c.get("/api/api-keys")).status_code == 200

    await realdb.subscribe("a", "growth")
    async with realdb.client(key="a") as c:
        resp = await c.post("/api/api-keys", json={"name": "k"})
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_webhook_subscriptions_follow_the_public_api_feature(realdb, monkeypatch):
    async def _public(url):  # the SSRF guard resolves DNS; not this test's subject
        return None

    monkeypatch.setattr("app.api.webhooks.ensure_public_webhook_target", _public)

    body = {
        "name": "erp-bridge",
        "target_url": "https://hooks.example.test/in",
        "event_types": ["invoice.approved"],
    }
    async with realdb.client(key="a") as c:
        _assert_plan_refusal(await c.post("/api/webhooks", json=body), FEATURE_PUBLIC_API)

    await realdb.subscribe("a", "growth")
    async with realdb.client(key="a") as c:
        created = await c.post("/api/webhooks", json=body)
    assert created.status_code == 201, created.text
    sub_id = created.json()["subscription"]["id"]

    await realdb.subscribe("a", "free")
    async with realdb.client(key="a") as c:
        _assert_plan_refusal(
            await c.patch(f"/api/webhooks/{sub_id}", json={"name": "renamed"}),
            FEATURE_PUBLIC_API,
        )
        off = await c.patch(f"/api/webhooks/{sub_id}", json={"active": False})
        # Rotating a (possibly leaked) signing secret is remediation, not use.
        rotated = await c.post(f"/api/webhooks/{sub_id}/rotate-secret", json={})
    assert off.status_code == 200, off.text
    assert off.json()["active"] is False
    assert rotated.status_code == 200, rotated.text


@pytest.mark.asyncio
async def test_no_webhook_is_queued_for_a_plan_without_the_feature(realdb, monkeypatch):
    from app.config import settings
    from app.models.webhook import EVENT_INVOICE_APPROVED, WebhookDelivery, WebhookSubscription
    from app.services.webhooks import dispatch as dispatch_mod
    from app.services.webhooks.signing import generate_signing_secret

    org_id = realdb.info("a").org_id
    secret, prefix = generate_signing_secret()
    sub_id = uuid.uuid4()
    async with realdb.control_sessionmaker()() as s:
        s.add(
            WebhookSubscription(
                id=sub_id,
                organization_id=org_id,
                name="kept",
                target_url="https://hooks.example.test/in",
                event_types=[EVENT_INVOICE_APPROVED],
                signing_secret=secret,
                secret_prefix=prefix,
                active=True,
            )
        )
        await s.commit()
    monkeypatch.setattr(settings, "webhooks_enabled", True)
    monkeypatch.setattr(dispatch_mod, "_spawn_immediate_attempt", lambda did: None)

    async def _deliveries() -> int:
        async with realdb.control_sessionmaker()() as s:
            rows = await s.execute(
                select(WebhookDelivery).where(WebhookDelivery.subscription_id == sub_id)
            )
            return len(rows.scalars().all())

    try:
        await dispatch_mod.emit_event(
            organization_id=org_id,
            event_type=EVENT_INVOICE_APPROVED,
            event_key="free-1",
            data={},
        )
        assert await _deliveries() == 0

        await realdb.subscribe("a", "growth")
        await dispatch_mod.emit_event(
            organization_id=org_id,
            event_type=EVENT_INVOICE_APPROVED,
            event_key="growth-1",
            data={},
        )
        assert await _deliveries() == 1
    finally:
        async with realdb.control_sessionmaker()() as s:
            await s.execute(
                delete(WebhookDelivery).where(WebhookDelivery.subscription_id == sub_id)
            )
            await s.execute(delete(WebhookSubscription).where(WebhookSubscription.id == sub_id))
            await s.commit()


# ---------------------------------------------------------------------------
# §281 breadth: the routing rule, every ERP route, the in-flight exemptions,
# and a lapsed plan across the other gates
# ---------------------------------------------------------------------------

SYNC_PATHS = (
    "/api/vendors/sync-erp",
    "/api/gl-accounts/sync-erp",
    "/api/purchase-orders/sync-erp",
)


@pytest.mark.parametrize(
    "config,adapter_key,features",
    [
        # No `integration_method`: the dispatcher's default sends it to Merge.
        ({"type": "xero"}, "merge_dev", [FEATURE_ERP_INTEGRATIONS, FEATURE_ERP_MERGE]),
        (
            {"integration_method": "merge_dev"},
            "merge_dev",
            [FEATURE_ERP_INTEGRATIONS, FEATURE_ERP_MERGE],
        ),
        (LIVE_ERP, "netsuite", [FEATURE_ERP_INTEGRATIONS]),
        (
            {"type": "netsuite", "integration_method": "direct"},
            "netsuite",
            [FEATURE_ERP_INTEGRATIONS],
        ),
        (MOCK_ERP, "mock", []),
        ({}, None, []),
        (None, None, []),
    ],
)
def test_the_gate_reads_the_route_the_dispatcher_takes(monkeypatch, config, adapter_key, features):
    """The gate and `get_erp_adapter` share `resolve_adapter_key`; this pins
    both ends of that agreement for the configs the plan rule turns on."""
    import asyncio

    from app.api import deps
    from app.services.erp_adapters.dispatcher import (
        _ADAPTER_REGISTRY,
        load_builtin_adapters,
        resolve_adapter_key,
    )

    checked: list[str] = []

    async def _record(_db, _org_id, feature):
        checked.append(feature)

    monkeypatch.setattr(deps, "ensure_entitlement", _record)
    asyncio.run(deps.ensure_live_erp_entitled(None, None, config))
    assert checked == features

    if adapter_key is not None:
        load_builtin_adapters()
        assert resolve_adapter_key(config) == adapter_key
        assert adapter_key in _ADAPTER_REGISTRY


@pytest.mark.asyncio
async def test_get_entitlements_hands_back_a_copy(realdb):
    """Mutating the no-plan answer must not rewrite what every later
    plan-less org is granted."""
    from app.services.billing.entitlements import get_entitlements

    async with realdb.control_sessionmaker()() as s:
        first = await get_entitlements(s, realdb.info("a").org_id)
        first["public_api"] = True
        first.pop(FEATURE_ERP_INTEGRATIONS)
        second = await get_entitlements(s, realdb.info("a").org_id)
    assert NO_PLAN_ENTITLEMENTS == {FEATURE_ERP_INTEGRATIONS: True}
    assert second == {FEATURE_ERP_INTEGRATIONS: True}


@pytest.mark.asyncio
async def test_growth_does_not_carry_merge(realdb):
    from app.services.billing.entitlements import get_entitlements

    await realdb.subscribe("a", "growth")
    async with realdb.control_sessionmaker()() as s:
        ents = await get_entitlements(s, realdb.info("a").org_id)
    assert ents.get(FEATURE_ERP_INTEGRATIONS) is True
    assert FEATURE_ERP_MERGE not in ents


async def _lapse(realdb, key: str = "a") -> None:
    """Cancel the org's live subscription in place — the dunning sweep's write,
    which `get_active_subscription`'s status filter reads, a different path
    from an org that never had one."""
    from app.models.billing import Subscription

    async with realdb.control_sessionmaker()() as s:
        sub = (
            await s.execute(
                select(Subscription).where(Subscription.organization_id == realdb.info(key).org_id)
            )
        ).scalar_one()
        sub.status = "canceled"
        await s.commit()


async def _arrange(realdb, plan: str) -> None:
    if plan == "canceled_scale":
        await realdb.subscribe("a", "scale")
        await _lapse(realdb)
    else:
        await realdb.subscribe("a", plan)


def _mock_adapter_for_any_config(monkeypatch) -> None:
    """Let a route that passed the gate reach an adapter without a network:
    the gate is this file's subject, not NetSuite."""
    from app.services import erp_adapters
    from app.services.erp_adapters.mock_adapter import MockAdapter

    monkeypatch.setattr(erp_adapters, "get_erp_adapter", lambda config: MockAdapter(config))


async def _failed_invoice(realdb, prefix: str) -> uuid.UUID:
    """An invoice whose ERP push failed after approval — what retry-erp takes."""
    from decimal import Decimal

    from app.models.invoice import Invoice, InvoiceStatus

    invoice_id = uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        s.add(
            Invoice(
                id=invoice_id,
                correlation_id=uuid.uuid4(),
                organization_id=realdb.info("a").org_id,
                vendor_name="Retry Supplies",
                invoice_number=f"{prefix}-{invoice_id.hex[:6]}",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.failed,
                approved_by="an approver",
            )
        )
        await s.commit()
    return invoice_id


def _record_dispatch(monkeypatch) -> list[uuid.UUID]:
    from app.services import erp_send

    dispatched: list[uuid.UUID] = []

    async def _record(invoice_id, *_args, **_kwargs):
        dispatched.append(invoice_id)

    monkeypatch.setattr(erp_send, "dispatch_erp", _record)
    return dispatched


@pytest.mark.asyncio
async def test_free_retries_a_failed_push_to_a_direct_erp(realdb, monkeypatch):
    from app.models.invoice import InvoiceStatus

    dispatched = _record_dispatch(monkeypatch)
    await realdb.subscribe("a", "free")
    await _write_settings(realdb, erp=dict(LIVE_ERP))
    invoice_id = await _failed_invoice(realdb, "RT")
    async with realdb.client(key="a") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/retry-erp")
    assert resp.status_code == 202, resp.text
    assert dispatched == [invoice_id]
    assert await _invoice_status(realdb, invoice_id) == InvoiceStatus.sending_to_erp


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["free", "growth", "canceled_scale"])
async def test_retrying_a_merge_push_needs_scale(realdb, monkeypatch, plan):
    from app.models.invoice import InvoiceStatus

    dispatched = _record_dispatch(monkeypatch)
    await _arrange(realdb, plan)
    await _write_settings(realdb, erp=dict(MERGE_ERP))
    invoice_id = await _failed_invoice(realdb, "RM")
    async with realdb.client(key="a") as c:
        _assert_plan_refusal(
            await c.post(f"/api/invoices/{invoice_id}/retry-erp"), FEATURE_ERP_MERGE
        )
    assert dispatched == []
    assert await _invoice_status(realdb, invoice_id) == InvoiceStatus.failed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "erp,expected",
    [(LIVE_ERP, "sending_to_erp"), (MERGE_ERP, "approved")],
    ids=["direct-dispatches", "merge-stays-approved"],
)
async def test_completing_an_approved_invoice_on_free(realdb, monkeypatch, erp, expected):
    """The completion leg (`POST /complete` on an approved invoice with the ERP
    step on): a direct ERP is pushed on Free; a Merge one is refused BEFORE the
    transition, so the invoice stays `approved` and payable."""
    from app.api import workflow as workflow_api
    from app.models.invoice import InvoiceStatus

    async def _erp_step_on(*_args, **_kwargs):
        return True

    # The harness tenant's workflow keeps the ERP step off; this leg is the
    # one that runs when a tenant has it on.
    monkeypatch.setattr(workflow_api, "is_step_enabled", _erp_step_on)
    dispatched = _record_dispatch(monkeypatch)
    await realdb.subscribe("a", "free")
    await _write_settings(realdb, erp=dict(erp))
    invoice_id = await _approved_invoice(realdb, "CP")
    async with realdb.client(key="a") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/complete")
    if expected == "approved":
        _assert_plan_refusal(resp, FEATURE_ERP_MERGE)
        assert dispatched == []
    else:
        assert resp.status_code == 200, resp.text
        assert dispatched == [invoice_id]
    assert await _invoice_status(realdb, invoice_id) == InvoiceStatus(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", SYNC_PATHS)
async def test_free_syncs_from_a_direct_erp(realdb, monkeypatch, path):
    _mock_adapter_for_any_config(monkeypatch)
    await realdb.subscribe("a", "free")
    await _write_settings(realdb, erp=dict(LIVE_ERP))
    async with realdb.client(key="a") as c:
        resp = await c.post(path)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", SYNC_PATHS)
@pytest.mark.parametrize("plan", ["free", "growth"])
async def test_syncing_from_merge_needs_scale(realdb, monkeypatch, path, plan):
    _mock_adapter_for_any_config(monkeypatch)
    await realdb.subscribe("a", plan)
    await _write_settings(realdb, erp=dict(MERGE_ERP))
    async with realdb.client(key="a") as c:
        _assert_plan_refusal(await c.post(path), FEATURE_ERP_MERGE)


@pytest.mark.asyncio
async def test_free_tests_a_direct_erp_connection(realdb, monkeypatch):
    _mock_adapter_for_any_config(monkeypatch)
    await realdb.subscribe("a", "free")
    async with realdb.client(key="a") as c:
        resp = await c.post("/api/organization/test-erp", json=LIVE_ERP)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True


class _DedupRedis:
    """SET NX / get / delete — all the ERP webhook's dedup claim needs."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def get(self, key):
        return self._store.get(key)

    async def delete(self, key):
        self._store.pop(key, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["free", "canceled_scale"])
async def test_the_erp_webhook_is_accepted_for_a_stored_merge_config(realdb, monkeypatch, plan):
    """The in-flight exemption (§258, §281): a push already sent before the
    plan stopped covering Merge still has its status reported back. The
    webhook is not plan-gated, so `sent_to_erp → posted_in_erp` applies."""
    import hashlib
    import hmac
    import json

    from app.api import erp_webhook
    from app.models.invoice import Invoice, InvoiceStatus

    secret = f"whsec-{uuid.uuid4().hex}"
    fake = _DedupRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webhook_security.get_redis", _get_redis)
    monkeypatch.setattr(erp_webhook, "control_session_factory", realdb.control_sessionmaker())

    await _arrange(realdb, plan)
    await _write_settings(realdb, erp=dict(MERGE_ERP))
    await realdb.store_provider_secrets("a", "erp", {"webhook_signing_secret": secret})

    invoice_id = await _approved_invoice(realdb, "WH")
    async with realdb.sessionmaker("a")() as s:
        invoice = await s.get(Invoice, invoice_id)
        invoice.status = InvoiceStatus.sent_to_erp
        correlation_id = str(invoice.correlation_id)
        await s.commit()

    body = json.dumps(
        {
            "tenant_slug": realdb.info("a").slug,
            "correlation_id": correlation_id,
            "event_id": f"evt-{uuid.uuid4().hex}",
            "status": "posted",
        }
    ).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            "/api/erp/webhook/merge_dev",
            content=body,
            headers={"Content-Type": "application/json", "X-Webhook-Signature": signature},
        )
    assert resp.status_code == 204, resp.text
    assert await _invoice_status(realdb, invoice_id) == InvoiceStatus.posted_in_erp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gate",
    ["api_key", "webhook", "sso", "scim_token", "scim_provisioning", "second_entity"],
)
async def test_a_lapsed_scale_plan_is_refused_every_paid_gate(realdb, monkeypatch, gate):
    """A canceled subscription reaches `get_active_subscription`'s status
    filter — not the no-row path — and must read as Free there too: every
    paid feature refused. `audit_siem_export` gates nothing yet (§258)."""
    await realdb.subscribe("a", "scale")
    await _lapse(realdb)

    if gate == "scim_provisioning":
        from app.api.scim import create_user
        from app.schemas.scim import SCIMUserCreate

        org = await _org(realdb)
        async with realdb.control_sessionmaker()() as s:
            with pytest.raises(HTTPException) as exc:
                await create_user(
                    SCIMUserCreate(userName=f"lapsed-{uuid.uuid4().hex[:6]}@example.test"),
                    _REQUEST,
                    org,
                    s,
                )
        assert exc.value.status_code == 402
        return

    if gate == "webhook":

        async def _public(*_a, **_k):
            return None

        monkeypatch.setattr("app.api.webhooks.ensure_public_webhook_target", _public)

    requests = {
        "api_key": ("post", "/api/api-keys", {"name": "k"}, FEATURE_PUBLIC_API),
        "webhook": (
            "post",
            "/api/webhooks",
            {
                "name": "lapsed",
                "target_url": "https://hooks.example.test/in",
                "event_types": ["invoice.approved"],
            },
            FEATURE_PUBLIC_API,
        ),
        "sso": ("put", SSO_URL, {**OIDC_READY, "sso_only": False}, FEATURE_SSO),
        "scim_token": ("post", "/api/organization/sso/scim-token", None, FEATURE_SCIM),
        "second_entity": (
            "post",
            "/api/entities",
            {"name": "Lapsed Inc", "slug": "lapsed-inc", "currency": "USD"},
            FEATURE_MULTI_ENTITY,
        ),
    }
    method, path, body, feature = requests[gate]
    async with realdb.client(key="a") as c:
        resp = await c.request(method.upper(), path, json=body)
    _assert_plan_refusal(resp, feature)
