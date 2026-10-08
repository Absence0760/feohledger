"""SCIM `userName` uniqueness is platform-wide, and rejected as a SCIM 409.

`users.email` carries a global UNIQUE constraint — it is the login identifier,
and `/auth/login` resolves an account by address alone with no tenant hint. The
SCIM write paths checked uniqueness only within the calling tenant (create, PUT)
or not at all (PATCH), so an IdP pushing an address already held in a DIFFERENT
tenant sailed past the guard and tripped the constraint on `flush()`: an
unhandled `IntegrityError`, i.e. a 500, where RFC 7644 §3.3 / §3.5.2 require a
409 `uniqueness`. Providers treat a 5xx as retryable, so the same doomed write
came back on every reconcile cycle.

These run against real Postgres because the constraint IS the bug: a mocked
session can't tell a scoped predicate from a global one. Tenant B's seeded admin
supplies a real address that exists in another org.

The handlers are driven directly rather than over HTTP so the tests don't have
to stand up a per-tenant SCIM bearer token; `get_scim_tenant` (the auth
dependency) is covered by `test_rbac.py`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api.scim import create_user, patch_user, replace_user
from app.models.organization import Organization
from app.models.user import User
from app.schemas.scim import SCIMPatchOp, SCIMPatchRequest, SCIMUserCreate

# SCIM provisioning is plan-gated (decisions §258) and the realdb harness orgs hold no
# subscription (= free), so bind both to Scale.
pytestmark = [pytest.mark.asyncio, pytest.mark.plan("scale")]

_REQUEST = SimpleNamespace(base_url="http://testserver/")


async def _org_a(realdb) -> Organization:
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        return (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()


@pytest_asyncio.fixture
async def scim_user(realdb):
    """Factory for a throwaway control-plane user in tenant A, torn down after.

    Control-plane rows persist across tests, so anything created here has to be
    cleaned up explicitly.
    """
    mk = realdb.control_sessionmaker()
    created: list[uuid.UUID] = []

    async def _make() -> tuple[uuid.UUID, str]:
        user_id = uuid.uuid4()
        email = f"scim-uniq-{user_id}@example.test"
        async with mk() as s:
            s.add(
                User(
                    id=user_id,
                    email=email,
                    full_name="SCIM Uniqueness Probe",
                    hashed_password=None,
                    organization_id=realdb.info("a").org_id,
                    is_active=True,
                    must_change_password=False,
                )
            )
            await s.commit()
        created.append(user_id)
        return user_id, email

    yield _make

    async with mk() as s:
        for user_id in created:
            await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


def _assert_scim_conflict(exc: HTTPException) -> None:
    assert exc.status_code == 409
    assert exc.detail["scimType"] == "uniqueness"


async def test_create_rejects_an_email_held_by_another_tenant(realdb):
    """The address exists in tenant B; provisioning it into tenant A must be a
    SCIM 409, not an IntegrityError."""
    org = await _org_a(realdb)
    foreign_email = realdb.email("b", "admin")
    mk = realdb.control_sessionmaker()

    async with mk() as s:
        with pytest.raises(HTTPException) as exc:
            await create_user(
                SCIMUserCreate(userName=foreign_email),
                _REQUEST,
                org,
                s,
            )
    _assert_scim_conflict(exc.value)


async def test_patch_rejects_a_rename_onto_an_email_held_by_another_tenant(realdb, scim_user):
    """PATCH had no uniqueness check at all."""
    org = await _org_a(realdb)
    user_id, original_email = await scim_user()
    foreign_email = realdb.email("b", "admin")
    mk = realdb.control_sessionmaker()

    async with mk() as s:
        with pytest.raises(HTTPException) as exc:
            await patch_user(
                user_id,
                SCIMPatchRequest(
                    Operations=[SCIMPatchOp(op="replace", path="userName", value=foreign_email)]
                ),
                _REQUEST,
                org,
                s,
            )
        _assert_scim_conflict(exc.value)

    # The rename must not have been staged onto the row either.
    async with mk() as s:
        assert (await s.get(User, user_id)).email == original_email


async def test_patch_root_replace_rejects_a_conflicting_rename(realdb, scim_user):
    """Okta's pathless root `replace` carries userName too — same guard."""
    org = await _org_a(realdb)
    user_id, original_email = await scim_user()
    foreign_email = realdb.email("b", "admin")
    mk = realdb.control_sessionmaker()

    async with mk() as s:
        with pytest.raises(HTTPException) as exc:
            await patch_user(
                user_id,
                SCIMPatchRequest(
                    Operations=[
                        SCIMPatchOp(op="replace", value={"userName": foreign_email, "active": True})
                    ]
                ),
                _REQUEST,
                org,
                s,
            )
        _assert_scim_conflict(exc.value)

    async with mk() as s:
        assert (await s.get(User, user_id)).email == original_email


async def test_patch_still_renames_to_a_free_address(realdb, scim_user):
    """Guard the other direction — an unused address still goes through."""
    org = await _org_a(realdb)
    user_id, _ = await scim_user()
    new_email = f"scim-uniq-free-{uuid.uuid4()}@example.test"
    mk = realdb.control_sessionmaker()

    async with mk() as s:
        result = await patch_user(
            user_id,
            SCIMPatchRequest(
                Operations=[SCIMPatchOp(op="replace", path="userName", value=new_email)]
            ),
            _REQUEST,
            org,
            s,
        )
        await s.commit()
    assert result.userName == new_email

    async with mk() as s:
        assert (await s.get(User, user_id)).email == new_email


async def test_put_rejects_a_rename_onto_an_email_held_by_another_tenant(realdb, scim_user):
    """PUT checked uniqueness, but only within the calling tenant."""
    org = await _org_a(realdb)
    user_id, original_email = await scim_user()
    foreign_email = realdb.email("b", "admin")
    mk = realdb.control_sessionmaker()

    async with mk() as s:
        with pytest.raises(HTTPException) as exc:
            await replace_user(
                user_id,
                SCIMUserCreate(userName=foreign_email),
                _REQUEST,
                org,
                s,
            )
        _assert_scim_conflict(exc.value)

    async with mk() as s:
        assert (await s.get(User, user_id)).email == original_email
