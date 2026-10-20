"""An email address names one identity regardless of case.

`users.email` is the login identifier and the key SSO JIT and SCIM link an IdP
identity to. SCIM and SSO lower-cased what they wrote and looked up; the admin
create/update path, tenant provisioning and the supplier-portal invite stored
whatever was typed; and every lookup compared exactly. So an admin-created
`Jane.Doe@Acme.com` was a different person from the `jane.doe@acme.com` her IdP
asserts:

- SSO JIT missed her on the email-link branch and minted a SECOND account, as
  `ap_clerk`, with none of her roles;
- SCIM's uniqueness guard missed and provisioned a duplicate — and a later SCIM
  deprovision deactivated only the duplicate, leaving the original (roles and a
  password) active;
- `userName eq` probes, which Okta and Entra run before every POST, answered
  "no such user";
- she could not sign in typing her address in lower case.

These run against real Postgres: the stored column and its UNIQUE constraint are
the bug, which a mocked session cannot show. The mixed-case rows are inserted
directly, the way a row written before normalization looks.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.api.scim import _apply_filter, create_user
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.schemas.scim import SCIMUserCreate
from app.services.identity_provisioning import EmailInOtherWorkspace, jit_provision
from app.utils.emails import normalize_email
from app.utils.passwords import pwd_context

pytestmark = pytest.mark.asyncio

_PASSWORD = "Passw0rd!CaseTest"
_REQUEST = SimpleNamespace(base_url="http://testserver/")


async def _org_a(realdb) -> Organization:
    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()


@pytest_asyncio.fixture
async def mixed_case_user(realdb):
    """A tenant-A user stored with a mixed-case address, as a pre-normalization
    admin-created row is. Everything in tenant A whose lower-cased address
    matches is removed afterwards, so a duplicate the bug created is reaped too.
    """
    mk = realdb.control_sessionmaker()
    made: list[str] = []

    async def _make() -> tuple[uuid.UUID, str]:
        user_id = uuid.uuid4()
        email = f"Case.{user_id.hex[:10]}@Example.Test"
        async with mk() as s:
            s.add(
                User(
                    id=user_id,
                    email=email,
                    full_name="Case Probe",
                    hashed_password=pwd_context.hash(_PASSWORD),
                    organization_id=realdb.info("a").org_id,
                    is_active=True,
                    must_change_password=False,
                )
            )
            await s.commit()
        made.append(email.lower())
        return user_id, email

    yield _make

    async with mk() as s:
        for lowered in made:
            ids = (
                (await s.execute(select(User.id).where(func.lower(User.email) == lowered)))
                .scalars()
                .all()
            )
            if ids:
                await s.execute(delete(UserRole).where(UserRole.user_id.in_(ids)))
                await s.execute(delete(User).where(User.id.in_(ids)))
        await s.commit()


@pytest.fixture
def session_redis(monkeypatch):
    """A successful login registers a session, which needs the zset+hash
    stand-in rather than the key/value-only autouse stub."""
    from tests.test_session_management import FakeRedis

    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


async def _accounts_for(realdb, email: str) -> list[uuid.UUID]:
    async with realdb.control_sessionmaker()() as s:
        return list(
            (await s.execute(select(User.id).where(func.lower(User.email) == email.lower())))
            .scalars()
            .all()
        )


async def test_normalize_email_trims_and_lowercases():
    assert normalize_email("  Jane.Doe@Acme.COM \n") == "jane.doe@acme.com"


async def test_sso_jit_links_the_existing_account_instead_of_minting_a_second(
    realdb, mixed_case_user
):
    user_id, email = await mixed_case_user()
    org = await _org_a(realdb)

    async with realdb.control_sessionmaker()() as s:
        linked = await jit_provision(s, org, email.lower(), f"sub-{user_id}", "oidc", {})
        await s.commit()

    assert linked.id == user_id
    assert await _accounts_for(realdb, email) == [user_id]


# SCIM provisioning is plan-gated (decisions §258); the harness org reads as free.
@pytest.mark.plan("scale")
async def test_scim_create_of_a_case_variant_is_a_409_not_a_duplicate(realdb, mixed_case_user):
    _user_id, email = await mixed_case_user()
    org = await _org_a(realdb)

    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(HTTPException) as exc:
            await create_user(SCIMUserCreate(userName=email.lower()), _REQUEST, org, s)

    assert exc.value.status_code == 409
    assert exc.value.detail["scimType"] == "uniqueness"
    assert len(await _accounts_for(realdb, email)) == 1


async def test_scim_username_filter_finds_a_mixed_case_row(realdb, mixed_case_user):
    """The probe Okta and Entra run before a POST: a miss here is what made
    them create the duplicate."""
    user_id, email = await mixed_case_user()

    query = _apply_filter(
        select(User.id).where(User.organization_id == realdb.info("a").org_id),
        f'userName eq "{email.lower()}"',
    )
    async with realdb.control_sessionmaker()() as s:
        found = (await s.execute(query)).scalars().all()

    assert found == [user_id]


async def test_password_login_is_case_insensitive(realdb, mixed_case_user, session_redis):
    _user_id, email = await mixed_case_user()

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            "/api/auth/login", json={"email": f"  {email.lower()} ", "password": _PASSWORD}
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]


async def test_admin_create_stores_normalized_and_refuses_a_case_variant(realdb):
    email = f"New.Hire.{uuid.uuid4().hex[:8]}@Acme.Test"
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/admin/users",
            json={"email": email, "full_name": "New Hire", "role_names": ["ap_clerk"]},
        )
        assert resp.status_code == 201, resp.text
        new_id = resp.json()["id"]
        try:
            assert resp.json()["email"] == email.lower()
            dup = await c.post(
                "/api/admin/users",
                json={"email": email.upper(), "full_name": "Dup", "role_names": ["ap_clerk"]},
            )
            assert dup.status_code == 409
        finally:
            await c.delete(f"/api/admin/users/{new_id}")


async def test_signing_in_on_another_workspaces_page_is_refused(
    realdb, mixed_case_user, session_redis
):
    """Production, 2026-10-20: an account of one workspace signed in on
    another's page, got its own org's token, and then every call on that page
    was refused (403 ``token_tenant_mismatch``) behind a
    signed-in screen. The sign-in itself is refused instead, after the password
    verifies, with the account's own sign-in link; on its own page it works."""
    _user_id, email = await mixed_case_user()

    async with realdb.client(key="b", role=None) as c:
        resp = await c.post("/api/auth/login", json={"email": email, "password": _PASSWORD})
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "wrong_workspace"
    assert "access_token" not in resp.json()
    url = detail["params"]["sign_in_url"]
    assert url is None or f"{realdb.info('a').slug}" in url

    # A wrong password on the other page is the ordinary 401: the workspace is
    # named only to someone who proved the password.
    async with realdb.client(key="b", role=None) as c:
        wrong = await c.post(
            "/api/auth/login", json={"email": email, "password": f"{_PASSWORD}-wrong"}
        )
    assert wrong.status_code == 401

    async with realdb.client(key="a", role=None) as c:
        ok = await c.post("/api/auth/login", json={"email": email, "password": _PASSWORD})
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# One address, one account, one workspace — the cross-workspace collisions
# (decisions §290). `users.email` is globally unique, so every path that would
# create or rename an account onto an address held in ANOTHER workspace must
# refuse in words, never trip the UNIQUE constraint into a 500.
# ---------------------------------------------------------------------------


async def _org_b(realdb) -> Organization:
    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(select(Organization).where(Organization.id == realdb.info("b").org_id))
        ).scalar_one()


async def test_sso_jit_refuses_an_address_held_in_another_workspace(realdb, mixed_case_user):
    """Workspace B's IdP asserts an address workspace A's account holds. Before,
    the lookup was B-only and the insert hit the global UNIQUE at flush: a 500.
    Now a stated refusal, and nothing is written in either workspace."""
    user_id, email = await mixed_case_user()
    org_b = await _org_b(realdb)

    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(EmailInOtherWorkspace):
            await jit_provision(s, org_b, email.lower(), "sub-elsewhere", "oidc", {})
        await s.rollback()

    assert await _accounts_for(realdb, email) == [user_id]


async def test_sso_jit_concurrent_insert_is_still_a_refusal(realdb, mixed_case_user, monkeypatch):
    """The pre-check lost a race: another workspace took the address between
    it and the flush. The savepoint turns the IntegrityError into the same
    refusal instead of a 500."""
    from app.services import identity_provisioning

    user_id, email = await mixed_case_user()
    org_b = await _org_b(realdb)

    real_lookup = identity_provisioning.email_holder_org_id
    calls = {"n": 0}

    async def _first_says_free(db, address, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the pre-check, before the other writer committed
        return await real_lookup(db, address, **kw)

    monkeypatch.setattr(identity_provisioning, "email_holder_org_id", _first_says_free)

    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(EmailInOtherWorkspace):
            # Exact case, so the stored row's case-sensitive UNIQUE trips.
            await jit_provision(s, org_b, email, "sub-race", "oidc", {})
        await s.rollback()

    assert calls["n"] == 2
    assert await _accounts_for(realdb, email) == [user_id]


async def test_oidc_callback_refuses_and_audits_in_the_idp_workspace(
    realdb, mixed_case_user, monkeypatch
):
    """End to end through `sso_callback` with the IdP legs stubbed and the real
    `jit_provision` against Postgres: a coded 403 (no link — §290), one
    `auth.sso.login.failure` row in workspace B, none in A, no account."""
    from app.api import auth_sso

    _user_id, email = await mixed_case_user()
    org_b = await _org_b(realdb)
    slug_b = realdb.info("b").slug

    async def _consume_state(state):
        return {"tenant": slug_b, "nonce": "n"}

    async def _fetch_org(slug, db):
        return org_b

    async def _sign_in_settings(db, org):
        return {
            "sso": {
                "enabled": True,
                "provider": "okta",
                "discovery_url": "https://idp.example.test/.well-known/openid-configuration",
                "client_id": "cid",
                "client_secret": "not-a-real-secret",
            }
        }

    async def _discovery(url):
        return {}

    async def _exchange(*a, **kw):
        return {"id_token": "stub"}

    async def _validate(*a, **kw):
        return {"email": email.lower(), "sub": "sub-b"}

    audits: list[dict] = []

    async def _audit(**kw):
        audits.append(kw)

    monkeypatch.setattr(auth_sso, "consume_state", _consume_state)
    monkeypatch.setattr(auth_sso, "_fetch_org_by_slug", _fetch_org)
    monkeypatch.setattr(auth_sso, "sign_in_settings", _sign_in_settings)
    monkeypatch.setattr(auth_sso, "fetch_discovery", _discovery)
    monkeypatch.setattr(auth_sso, "exchange_code_for_tokens", _exchange)
    monkeypatch.setattr(auth_sso, "validate_id_token", _validate)
    monkeypatch.setattr(auth_sso, "dispatch_auth_audit", _audit)

    request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.7"), headers={})
    body = auth_sso.SSOCallbackRequest(code="c", state="s")
    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(HTTPException) as exc:
            await auth_sso.sso_callback(body, request, db=s)

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "sso_email_in_other_workspace"
    assert exc.value.detail["params"] == {}
    assert realdb.info("a").slug not in exc.value.detail["message"]

    assert len(audits) == 1
    row = audits[0]
    assert row["organization_id"] == org_b.id
    assert row["action"] == "auth.sso.login.failure"
    assert row["details"]["reason"] == "sso_email_in_other_workspace"
    assert row["details"]["tenant"] == slug_b
    assert len(await _accounts_for(realdb, email)) == 1


async def test_admin_create_says_which_workspace_holds_the_address(realdb, mixed_case_user):
    """Same workspace and other workspace are different refusals, each coded
    and stated — never the bare "Email already in use" — and neither names
    the other workspace."""
    _user_id, email = await mixed_case_user()  # held in A

    async with realdb.client(key="a", role="admin") as c:
        same = await c.post(
            "/api/admin/users",
            json={"email": email.lower(), "full_name": "Again", "role_names": ["ap_clerk"]},
        )
    assert same.status_code == 409
    assert same.json()["detail"]["code"] == "email_in_this_workspace"

    async with realdb.client(key="b", role="admin") as c:
        other = await c.post(
            "/api/admin/users",
            json={"email": email.lower(), "full_name": "Elsewhere", "role_names": ["ap_clerk"]},
        )
    assert other.status_code == 409
    detail = other.json()["detail"]
    assert detail["code"] == "email_in_other_workspace"
    assert detail["params"] == {}
    assert realdb.info("a").slug not in detail["message"]
    assert len(await _accounts_for(realdb, email)) == 1


async def test_admin_rename_onto_another_workspaces_address_is_coded(realdb, mixed_case_user):
    _user_id, email = await mixed_case_user()  # held in A
    fresh = f"rename.{uuid.uuid4().hex[:8]}@example.test"

    async with realdb.client(key="b", role="admin") as c:
        made = await c.post(
            "/api/admin/users",
            json={"email": fresh, "full_name": "Renamer", "role_names": ["ap_clerk"]},
        )
        assert made.status_code == 201, made.text
        target = made.json()["id"]
        try:
            resp = await c.patch(f"/api/admin/users/{target}", json={"email": email.lower()})
            assert resp.status_code == 409
            assert resp.json()["detail"]["code"] == "email_in_other_workspace"
        finally:
            await c.delete(f"/api/admin/users/{target}")


async def test_other_workspace_refusals_are_budgeted_per_workspace(
    realdb, mixed_case_user, session_redis, monkeypatch
):
    """The other-workspace refusal is an unavoidable existence signal while an
    address is global; a per-workspace budget bounds sweeping it. Once spent,
    even a free address is throttled — the 429 comes before the lookup, so a
    throttled caller learns nothing."""
    from app.api import admin as admin_api
    from app.config import settings as app_settings

    _user_id, email = await mixed_case_user()  # held in A
    monkeypatch.setattr(app_settings, "rate_limit_enabled", True)
    monkeypatch.setattr(admin_api, "EMAIL_COLLISION_LIMIT", 2)

    body = {"email": email.lower(), "full_name": "Sweep", "role_names": ["ap_clerk"]}
    async with realdb.client(key="b", role="admin") as c:
        first = await c.post("/api/admin/users", json=body)
        second = await c.post("/api/admin/users", json=body)
        third = await c.post("/api/admin/users", json=body)
        fourth = await c.post(
            "/api/admin/users",
            json={**body, "email": f"free.{uuid.uuid4().hex[:8]}@example.test"},
        )
    assert [first.status_code, second.status_code] == [409, 409]
    assert third.status_code == 429
    assert fourth.status_code == 429


@pytest.mark.plan("scale")
async def test_scim_create_says_the_address_is_in_another_workspace(realdb, mixed_case_user):
    _user_id, email = await mixed_case_user()  # held in A
    org_b = await _org_b(realdb)

    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(HTTPException) as exc:
            await create_user(SCIMUserCreate(userName=email.lower()), _REQUEST, org_b, s)

    assert exc.value.status_code == 409
    assert exc.value.detail["scimType"] == "uniqueness"
    assert "another workspace" in exc.value.detail["detail"]
    assert realdb.info("a").slug not in exc.value.detail["detail"]


async def test_wrong_workspace_audit_names_the_page_and_the_verified_password(
    realdb, mixed_case_user, session_redis, monkeypatch
):
    """The account's workspace gets the row, with the workspace whose page it
    was and the fact that the password verified; the attempted workspace gets
    nothing (§290)."""
    from app.api import auth as auth_api

    _user_id, email = await mixed_case_user()  # held in A
    audits: list[dict] = []

    async def _audit(**kw):
        audits.append(kw)

    monkeypatch.setattr(auth_api, "dispatch_auth_audit", _audit)

    async with realdb.client(key="b", role=None) as c:
        resp = await c.post("/api/auth/login", json={"email": email, "password": _PASSWORD})
    assert resp.status_code == 403, resp.text

    rows = [a for a in audits if a["details"].get("reason") == "wrong_workspace"]
    assert len(rows) == 1
    row = rows[0]
    assert row["organization_id"] == realdb.info("a").org_id
    assert row["details"]["attempted_workspace"] == realdb.info("b").slug
    assert row["details"]["password_verified"] is True
    assert row["details"]["email"] == email.lower()
    assert not [a for a in audits if a["organization_id"] == realdb.info("b").org_id]
