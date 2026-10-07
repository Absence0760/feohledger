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
from app.services.identity_provisioning import jit_provision
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
