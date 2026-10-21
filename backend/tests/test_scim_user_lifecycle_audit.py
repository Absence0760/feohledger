"""SCIM user writes are audited, end sessions, and cannot strand the org adminless.

Persona-admin finding: create, deactivate (PATCH / PUT / DELETE) and rename over
SCIM wrote no audit row, never revoked the deprovisioned user's sessions, and
skipped the last-admin guard the admin API applies — so "who offboarded Jane,
and when?" had no answer for IdP-driven leavers, and an IdP misconfiguration
could deactivate the org's only admin with nothing to undo it.

Driven against real Postgres through the handlers, like
`test_scim_active_patch.py`, so the assertions are on stored rows.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api.scim import (
    create_user,
    delete_user,
    patch_group,
    patch_user,
    replace_user,
)
from app.models.organization import Organization
from app.models.user import Role, User, UserRole
from app.models.workflow import AuditLog
from app.schemas.scim import SCIMEmail, SCIMPatchOp, SCIMPatchRequest, SCIMUserCreate

pytestmark = [pytest.mark.asyncio, pytest.mark.plan("scale")]

_REQUEST = SimpleNamespace(base_url="http://testserver/")


async def _org_a(realdb) -> Organization:
    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()


async def _rows(realdb, action, entity_id):
    async with realdb.sessionmaker("a")() as s:
        return (
            (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.action == action, AuditLog.entity_id == entity_id)
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _stored(realdb, user_id) -> User:
    async with realdb.control_sessionmaker()() as s:
        return await s.get(User, user_id)


@pytest.fixture
def revoked(monkeypatch):
    """Record which users had their sessions revoked."""
    calls: list[uuid.UUID] = []

    async def _revoke(user_id):
        calls.append(user_id)
        return ["jti-1", "jti-2"]

    monkeypatch.setattr("app.api.scim.revoke_user_sessions", _revoke)
    return calls


@pytest_asyncio.fixture
async def scim_user(realdb):
    mk = realdb.control_sessionmaker()
    created: list[uuid.UUID] = []

    async def _make(*, active: bool = True) -> tuple[uuid.UUID, str]:
        user_id = uuid.uuid4()
        email = f"scim-life-{user_id}@example.test"
        async with mk() as s:
            s.add(
                User(
                    id=user_id,
                    email=email,
                    full_name="SCIM Lifecycle Probe",
                    hashed_password=None,
                    organization_id=realdb.info("a").org_id,
                    is_active=active,
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


async def _call(realdb, handler, *args):
    org = await _org_a(realdb)
    async with realdb.control_sessionmaker()() as s:
        result = await handler(*args, org, s)
        await s.commit()
    return result


# --- create ---------------------------------------------------------------------


async def test_create_writes_a_user_created_row(realdb):
    email = f"scim-new-{uuid.uuid4().hex[:8]}@example.test"
    body = SCIMUserCreate(userName=email, emails=[SCIMEmail(value=email, primary=True)])
    org = await _org_a(realdb)
    async with realdb.control_sessionmaker()() as s:
        created = await create_user(body, _REQUEST, org, s)
    user_id = uuid.UUID(created.id)
    try:
        (row,) = await _rows(realdb, "user.created", user_id)
        assert row.actor_id is None
        assert row.details == {"role_names": [], "via": "scim", "is_active": True}
    finally:
        async with realdb.control_sessionmaker()() as s:
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()


# --- deactivation: PATCH, PUT, DELETE --------------------------------------------


async def test_patch_deprovision_is_audited_and_ends_sessions(realdb, scim_user, revoked):
    user_id, _ = await scim_user()
    await _call(
        realdb,
        patch_user,
        user_id,
        SCIMPatchRequest(Operations=[SCIMPatchOp(op="replace", path="active", value=False)]),
        _REQUEST,
    )

    assert (await _stored(realdb, user_id)).is_active is False
    assert revoked == [user_id]
    (row,) = await _rows(realdb, "user.updated", user_id)
    assert row.actor_id is None
    assert row.details["via"] == "scim"
    assert row.details["operation"] == "patch"
    assert row.details["changed_fields"] == ["is_active"]
    assert row.details["previous_is_active"] is True
    assert row.details["is_active"] is False
    assert row.details["sessions_revoked"] == 2


async def test_delete_is_the_same_audited_deactivation(realdb, scim_user, revoked):
    user_id, _ = await scim_user()
    org = await _org_a(realdb)
    async with realdb.control_sessionmaker()() as s:
        await delete_user(user_id, org, s)

    assert (await _stored(realdb, user_id)).is_active is False
    assert revoked == [user_id]
    (row,) = await _rows(realdb, "user.updated", user_id)
    assert row.details["operation"] == "delete"
    assert row.details["changed_fields"] == ["is_active"]


async def test_put_rename_records_the_address_before_and_after(realdb, scim_user, revoked):
    user_id, email = await scim_user()
    new_email = f"scim-renamed-{uuid.uuid4().hex[:8]}@example.test"
    body = SCIMUserCreate(
        userName=new_email,
        emails=[SCIMEmail(value=new_email, primary=True)],
        name={"formatted": "SCIM Lifecycle Probe"},
    )
    await _call(realdb, replace_user, user_id, body, _REQUEST)

    assert revoked == []  # a rename is not a deactivation
    (row,) = await _rows(realdb, "user.updated", user_id)
    assert row.details["changed_fields"] == ["email"]
    assert row.details["previous_email"] == email
    assert row.details["email"] == new_email
    assert row.details["operation"] == "put"


async def test_an_unchanged_write_records_nothing(realdb, scim_user, revoked):
    user_id, _ = await scim_user(active=False)
    await _call(
        realdb,
        patch_user,
        user_id,
        SCIMPatchRequest(Operations=[SCIMPatchOp(op="replace", path="active", value=False)]),
        _REQUEST,
    )
    assert revoked == []
    assert await _rows(realdb, "user.updated", user_id) == []


async def test_a_failed_revoke_does_not_fail_the_deprovision(realdb, scim_user, monkeypatch):
    """The deactivation has committed; a 5xx would only make the IdP retry it."""

    async def _redis_down(_user_id):
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.api.scim.revoke_user_sessions", _redis_down)
    user_id, _ = await scim_user()
    await _call(
        realdb,
        patch_user,
        user_id,
        SCIMPatchRequest(Operations=[SCIMPatchOp(op="replace", path="active", value="False")]),
        _REQUEST,
    )
    assert (await _stored(realdb, user_id)).is_active is False
    (row,) = await _rows(realdb, "user.updated", user_id)
    assert row.details["sessions_revoked"] is None


# --- the last admin ---------------------------------------------------------------


@pytest.mark.parametrize("via", ["patch", "put", "delete"])
async def test_deprovisioning_the_last_admin_is_refused(realdb, revoked, via):
    """The seeded admin is org A's only active admin: every SCIM spelling of a
    deprovision is a 400 `mutability`, changes nothing, and is audited."""
    admin_id = realdb.info("a").users["admin"]
    org = await _org_a(realdb)
    try:
        async with realdb.control_sessionmaker()() as s:
            with pytest.raises(HTTPException) as exc:
                if via == "patch":
                    await patch_user(
                        admin_id,
                        SCIMPatchRequest(
                            Operations=[SCIMPatchOp(op="replace", path="active", value=False)]
                        ),
                        _REQUEST,
                        org,
                        s,
                    )
                elif via == "put":
                    stored = await _stored(realdb, admin_id)
                    body = SCIMUserCreate(userName=stored.email, active=False)
                    await replace_user(admin_id, body, _REQUEST, org, s)
                else:
                    await delete_user(admin_id, org, s)
        assert exc.value.status_code == 400
        assert exc.value.detail["scimType"] == "mutability"
        assert "last active admin" in exc.value.detail["detail"]
        assert (await _stored(realdb, admin_id)).is_active is True
        assert revoked == []
        rows = await _rows(realdb, "user.deactivation_refused", admin_id)
        assert rows[-1].details == {
            "via": "scim",
            "operation": via,
            "reason": "last_active_admin",
        }
    finally:
        async with realdb.control_sessionmaker()() as s:
            user = await s.get(User, admin_id)
            user.is_active = True
            await s.commit()


async def test_a_group_change_cannot_strip_the_last_admin(realdb):
    """With `admin` IdP-managed, removing the only admin from the mapped group
    would revoke the role — refused before any role or group state changes."""
    admin_id = realdb.info("a").users["admin"]
    org_id = realdb.info("a").org_id
    group_id = str(uuid.uuid4())
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, org_id)
        org.settings = {
            **(org.settings or {}),
            "sso": {
                **((org.settings or {}).get("sso") or {}),
                "scim_group_role_map": {"Admins": "admin"},
                "scim_groups": {group_id: {"displayName": "Admins", "members": [str(admin_id)]}},
            },
        }
        await s.commit()

    org = await _org_a(realdb)
    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(HTTPException) as exc:
            await patch_group(
                group_id,
                SCIMPatchRequest(
                    Operations=[SCIMPatchOp(op="remove", path=f'members[value eq "{admin_id}"]')]
                ),
                _REQUEST,
                org,
                s,
            )
    assert exc.value.status_code == 400
    assert exc.value.detail["scimType"] == "mutability"

    async with realdb.control_sessionmaker()() as s:
        roles = (
            (
                await s.execute(
                    select(Role.name)
                    .join(UserRole, UserRole.role_id == Role.id)
                    .where(UserRole.user_id == admin_id)
                )
            )
            .scalars()
            .all()
        )
        stored_org = await s.get(Organization, org_id)
    assert "admin" in roles
    # The group write rolled back with it: the admin is still a member.
    assert stored_org.settings["sso"]["scim_groups"][group_id]["members"] == [str(admin_id)]
    rows = await _rows(realdb, "auth.scim.role_revoke_refused", org_id)
    assert rows[-1].details["reason"] == "last_active_admin"
