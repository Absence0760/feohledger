"""Unit tests for the admin user-management privilege-escalation guards
(`app/api/admin.py::_authorize_role_grant` / `_authorize_target_mutation`) and
the admin password reset (`POST /api/admin/users/{id}/reset-password`).

Regression coverage for issue #158: a `user.manage`-only actor must not be able
to (a) grant a role carrying more authority than they hold — most importantly
the system `admin` role — or (b) set an account's password at all: since #566
the reset only ever issues a generated temp the owner must change. The pure
guard tests take lightweight stand-ins for the caller (`.roles` /
`.effective_permissions`) and Role rows; the endpoint tests run on `realdb`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.admin import (
    _authorize_role_grant,
    _authorize_target_mutation,
)
from app.api.deps import ROLE_ADMIN, ROLE_AP_CLERK
from app.api.permissions import (
    ALL_PERMISSIONS,
    PERM_PAYMENT_EXECUTE,
    PERM_USER_MANAGE,
    ROLE_DEFAULT_PERMISSIONS,
    effective_permissions,
)
from app.models.workflow import AuditLog


def _sys_role(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, organization_id=None, permissions=None)


def _custom_role(perms: list[str], name: str = "Custom") -> SimpleNamespace:
    return SimpleNamespace(name=name, organization_id=uuid.uuid4(), permissions=perms)


def _caller(roles: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(roles=roles, effective_permissions=effective_permissions(roles))


# --------------------------------------------------------------------------
# _authorize_role_grant
# --------------------------------------------------------------------------


def test_admin_caller_may_grant_admin():
    admin = _caller([_sys_role(ROLE_ADMIN)])
    _authorize_role_grant(admin, [_sys_role(ROLE_ADMIN)])  # no raise


def test_user_manage_only_caller_cannot_grant_admin():
    """The core exploit: a custom 'User Admin' role carrying ONLY user.manage
    tries to grant the system admin role."""
    caller = _caller([_custom_role([PERM_USER_MANAGE], name="UserAdmin")])
    with pytest.raises(HTTPException) as exc:
        _authorize_role_grant(caller, [_sys_role(ROLE_ADMIN)])
    assert exc.value.status_code == 403


def test_full_catalog_non_admin_still_cannot_grant_admin():
    """Even a caller holding every *catalog* permission can't grant the admin
    system role — admin carries non-catalog superuser authority the subset
    check can't see."""
    caller = _caller([_custom_role(list(ALL_PERMISSIONS), name="Superuser")])
    assert caller.effective_permissions == frozenset(ALL_PERMISSIONS)
    with pytest.raises(HTTPException) as exc:
        _authorize_role_grant(caller, [_sys_role(ROLE_ADMIN)])
    assert exc.value.status_code == 403


def test_cannot_grant_permission_not_held():
    caller = _caller([_custom_role([PERM_USER_MANAGE], name="UserAdmin")])
    target = _custom_role([PERM_PAYMENT_EXECUTE], name="Payer")
    with pytest.raises(HTTPException) as exc:
        _authorize_role_grant(caller, [target])
    assert exc.value.status_code == 403


def test_can_grant_subset_role():
    """A caller may hand out a role whose catalog permissions are a subset of
    their own (and which isn't the admin system role)."""
    caller = _caller([_custom_role([PERM_USER_MANAGE], name="UserAdmin")])
    # ap_clerk confers no catalog permission — the empty set is a subset of any.
    _authorize_role_grant(caller, [_sys_role(ROLE_AP_CLERK)])
    # A custom role granting the same permission the caller holds is fine too.
    _authorize_role_grant(caller, [_custom_role([PERM_USER_MANAGE])])


def test_admin_caller_may_grant_any_role():
    admin = _caller([_sys_role(ROLE_ADMIN)])
    assert admin.effective_permissions == ROLE_DEFAULT_PERMISSIONS[ROLE_ADMIN]
    _authorize_role_grant(admin, [_custom_role([PERM_PAYMENT_EXECUTE])])  # no raise


def test_grant_guard_falls_back_when_effective_permissions_missing():
    """The guard recomputes from .roles if the caller has no cached
    effective_permissions attribute (defensive)."""
    caller = SimpleNamespace(roles=[_custom_role([PERM_USER_MANAGE], name="UserAdmin")])
    with pytest.raises(HTTPException):
        _authorize_role_grant(caller, [_sys_role(ROLE_ADMIN)])


# --------------------------------------------------------------------------
# Issue #566 — the admin password reset is a dedicated action that issues a
# GENERATED temp the user must change, never an admin-typed value via PATCH.
# The typed path let the admin permanently know the password, never forced a
# change, and — because the Edit form carried the field — let a browser
# autofill the admin's own saved password into it, so saving an unrelated edit
# silently reset the target's credential. Issue #160's forced logout rides
# along on the new endpoint.
# --------------------------------------------------------------------------


async def _throwaway_clerk(client, headers: dict | None = None) -> str:
    """A disposable ap_clerk — a password-mutating test must never touch one of
    the seeded role users, whose control-plane rows persist across the slot."""
    created = await client.post(
        "/api/admin/users",
        json={
            "email": f"reset-{uuid.uuid4().hex[:8]}@acme.test",
            "full_name": "Reset Target",
            "role_names": ["ap_clerk"],
        },
        headers=headers or {},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _stored_user(realdb, user_id):
    from app.models.user import User

    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(select(User).where(User.id == uuid.UUID(str(user_id))))
        ).scalar_one()


@pytest.mark.asyncio
async def test_patch_with_a_password_is_refused_and_changes_nothing(realdb, monkeypatch):
    """Removed, not ignored: a client still sending `password` must learn the
    reset did not happen (coded 422), and nothing on the row may move — not the
    hash, not the name riding in the same body, and no session is revoked."""
    calls: list = []

    async def _fake_revoke(user_id):
        calls.append(user_id)
        return []

    monkeypatch.setattr("app.api.admin.revoke_user_sessions", _fake_revoke)

    async with realdb.client(key="a", role="admin") as c:
        target_id = await _throwaway_clerk(c)
        try:
            before = await _stored_user(realdb, target_id)
            resp = await c.patch(
                f"/api/admin/users/{target_id}",
                json={"full_name": "Should Not Apply", "password": "AdminTyped123"},
            )
            assert resp.status_code == 422, resp.text
            assert resp.json()["detail"]["code"] == "admin_password_field_removed"
            assert "AdminTyped123" not in resp.text

            after = await _stored_user(realdb, target_id)
            assert after.hashed_password == before.hashed_password
            assert after.full_name == "Reset Target"
            assert calls == []
        finally:
            await c.delete(f"/api/admin/users/{target_id}")


def test_update_user_request_schema_no_longer_publishes_password():
    """The field survives only so the handler can refuse it — it must not be
    advertised as part of the request contract."""
    from app.schemas.admin import UpdateUserRequest

    assert "password" not in UpdateUserRequest.model_json_schema()["properties"]


@pytest.mark.asyncio
async def test_admin_reset_issues_forced_change_temp_and_revokes_sessions(realdb, monkeypatch):
    """The whole contract: a generated temp that satisfies the platform policy,
    `must_change_password` set, every session of the target revoked, the temp
    actually authenticating, and a PII-free audit row."""
    from app.utils.passwords import validate_password_complexity, verify_password

    calls: list = []

    async def _fake_revoke(user_id):
        calls.append(user_id)
        return ["jti-1"]

    monkeypatch.setattr("app.api.admin.revoke_user_sessions", _fake_revoke)

    async with realdb.client(key="a", role="admin") as c:
        target_id = await _throwaway_clerk(c)
        try:
            # Simulate the user having already replaced their invite temp.
            from app.models.user import User

            async with realdb.control_sessionmaker()() as s:
                row = (
                    await s.execute(select(User).where(User.id == uuid.UUID(target_id)))
                ).scalar_one()
                row.must_change_password = False
                await s.commit()

            resp = await c.post(f"/api/admin/users/{target_id}/reset-password")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            temp = body["temporary_password"]
            assert body["id"] == target_id
            validate_password_complexity(temp)  # same policy as every password

            stored = await _stored_user(realdb, target_id)
            assert stored.must_change_password is True
            assert await verify_password(temp, stored.hashed_password)
            assert calls == [uuid.UUID(target_id)]

            async with realdb.sessionmaker("a")() as s:
                row = (
                    await s.execute(
                        select(AuditLog).where(
                            AuditLog.action == "user.password_reset",
                            AuditLog.entity_id == uuid.UUID(target_id),
                        )
                    )
                ).scalar_one()
            assert row.actor_id == realdb.info("a").users["admin"]
            assert row.details == {
                "changed_fields": ["password"],
                "must_change_password": True,
                "sessions_revoked": 1,
            }
            assert temp not in str(row.details)
        finally:
            await c.delete(f"/api/admin/users/{target_id}")


@pytest.mark.asyncio
async def test_admin_reset_temp_signs_in_and_is_forced_to_change(realdb, monkeypatch):
    """End to end through the real login: the temp authenticates, the session
    says the password must change, `/change-password` replaces it, and the temp
    is dead afterwards."""
    from tests.test_session_management import FakeRedis

    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)

    async with realdb.client(key="a", role="admin") as c:
        target_id = await _throwaway_clerk(c)
        email = (await _stored_user(realdb, target_id)).email
        reset = await c.post(f"/api/admin/users/{target_id}/reset-password")
        assert reset.status_code == 200, reset.text
        temp = reset.json()["temporary_password"]

    try:
        async with realdb.client(key="a", role=None) as anon:
            login = await anon.post("/api/auth/login", json={"email": email, "password": temp})
            assert login.status_code == 200, login.text
            token = login.json()["access_token"]
            auth = {"Authorization": f"Bearer {token}"}
            me = await anon.get("/api/auth/me", headers=auth)
            assert me.json()["must_change_password"] is True

            changed = await anon.post(
                "/api/auth/change-password",
                json={"current_password": temp, "new_password": "OwnerChosen-Pass9"},
                headers=auth,
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["must_change_password"] is False

            stale = await anon.post("/api/auth/login", json={"email": email, "password": temp})
            assert stale.status_code == 401
            fresh = await anon.post(
                "/api/auth/login", json={"email": email, "password": "OwnerChosen-Pass9"}
            )
            assert fresh.status_code == 200, fresh.text
    finally:
        async with realdb.client(key="a", role="admin") as c:
            await c.delete(f"/api/admin/users/{target_id}")


@pytest.mark.asyncio
async def test_admin_reset_survives_a_failed_session_revoke(realdb, monkeypatch):
    """The temp is shown only on this response, and the password is already
    replaced when the revoke runs — so a Redis failure there must not become a
    500 that locks the user out with no audit row. It is reported instead."""

    async def _broken_revoke(user_id):
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr("app.api.admin.revoke_user_sessions", _broken_revoke)

    async with realdb.client(key="a", role="admin") as c:
        target_id = await _throwaway_clerk(c)
        try:
            resp = await c.post(f"/api/admin/users/{target_id}/reset-password")
            assert resp.status_code == 200, resp.text
            assert resp.json()["temporary_password"]
            assert resp.json()["sessions_revoked"] is False

            async with realdb.sessionmaker("a")() as s:
                row = (
                    await s.execute(
                        select(AuditLog).where(
                            AuditLog.action == "user.password_reset",
                            AuditLog.entity_id == uuid.UUID(target_id),
                        )
                    )
                ).scalar_one()
            assert row.details["sessions_revoked"] is None
        finally:
            # DELETE revokes sessions too — give cleanup a working one. (Not
            # `monkeypatch.undo()`: that would also undo the autouse Redis stub.)
            async def _ok_revoke(user_id):
                return []

            monkeypatch.setattr("app.api.admin.revoke_user_sessions", _ok_revoke)
            await c.delete(f"/api/admin/users/{target_id}")


@pytest.mark.asyncio
async def test_admin_reset_refuses_own_account(realdb, monkeypatch):
    calls: list = []

    async def _fake_revoke(user_id):
        calls.append(user_id)
        return []

    monkeypatch.setattr("app.api.admin.revoke_user_sessions", _fake_revoke)
    admin_id = realdb.info("a").users["admin"]
    before = (await _stored_user(realdb, admin_id)).hashed_password
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/admin/users/{admin_id}/reset-password")
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "admin_password_reset_self"
    assert (await _stored_user(realdb, admin_id)).hashed_password == before
    assert calls == []


@pytest.mark.asyncio
async def test_admin_reset_is_org_scoped_and_needs_user_manage(realdb, monkeypatch):
    calls: list = []

    async def _fake_revoke(user_id):
        calls.append(user_id)
        return []

    monkeypatch.setattr("app.api.admin.revoke_user_sessions", _fake_revoke)
    foreign_id = realdb.info("b").users["ap_clerk"]
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/admin/users/{foreign_id}/reset-password")
        assert resp.status_code == 404, resp.text

        clerk = {"Authorization": f"Bearer {realdb.token('a', 'ap_clerk')}"}
        target = realdb.info("a").users["ap_manager"]
        resp = await c.post(f"/api/admin/users/{target}/reset-password", headers=clerk)
        assert resp.status_code == 403, resp.text
    assert calls == []


# --------------------------------------------------------------------------
# #566 — the SOX trail must name only fields whose value actually changed.
# The Edit form sends every field on every save; recording a field because the
# body carried it logged changes that never happened.
# --------------------------------------------------------------------------


async def _user_updated_rows(realdb, target_id):
    async with realdb.sessionmaker("a")() as s:
        return (
            (
                await s.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.action == "user.updated",
                        AuditLog.entity_id == uuid.UUID(str(target_id)),
                    )
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_resubmitting_unchanged_fields_records_no_change(realdb):
    """The Edit modal's save with nothing edited: every field resent at its
    current value is not a change and writes no audit row."""
    async with realdb.client(key="a", role="admin") as c:
        target_id = await _throwaway_clerk(c)
        try:
            current = await _stored_user(realdb, target_id)
            resp = await c.patch(
                f"/api/admin/users/{target_id}",
                json={
                    "full_name": current.full_name,
                    "email": current.email.upper(),  # same identity, different case
                    "is_active": True,
                    "role_names": ["ap_clerk"],
                },
            )
            assert resp.status_code == 200, resp.text
            assert await _user_updated_rows(realdb, target_id) == []
        finally:
            await c.delete(f"/api/admin/users/{target_id}")


@pytest.mark.asyncio
async def test_full_form_save_records_only_the_field_that_differs(realdb):
    async with realdb.client(key="a", role="admin") as c:
        target_id = await _throwaway_clerk(c)
        try:
            current = await _stored_user(realdb, target_id)
            resp = await c.patch(
                f"/api/admin/users/{target_id}",
                json={
                    "full_name": "Actually Renamed",
                    "email": current.email,
                    "is_active": True,
                    "role_names": ["ap_clerk"],
                },
            )
            assert resp.status_code == 200, resp.text
            rows = await _user_updated_rows(realdb, target_id)
            assert len(rows) == 1
            assert rows[0].details["changed_fields"] == ["full_name"]
        finally:
            await c.delete(f"/api/admin/users/{target_id}")


@pytest.mark.asyncio
async def test_admin_update_user_other_field_change_does_not_revoke(realdb, monkeypatch):
    """A plain field edit (no role change, no deactivation, no password reset)
    must not force-logout the target — only the sensitive branches should."""
    calls: list = []

    async def _fake_revoke(user_id):
        calls.append(user_id)
        return []

    monkeypatch.setattr("app.api.admin.revoke_user_sessions", _fake_revoke)

    target_id = realdb.info("a").users["ap_clerk"]
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            f"/api/admin/users/{target_id}",
            json={"full_name": "Renamed Clerk"},
        )
    assert resp.status_code == 200
    assert calls == []


# --------------------------------------------------------------------------
# Issue #161 — no audit trail for admin user/role mutations. Role grants,
# permission edits, password resets, and user deletions were invisible in the
# SOX trail, compounding the privilege-escalation exposure #158 already
# guards against: a takeover left zero trace in AuditLog.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_update_user_noop_writes_no_audit_row(realdb):
    """An empty PATCH (nothing actually changed) must not write a spurious
    audit row — only a real change should."""
    target_id = realdb.info("a").users["ap_clerk"]
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(f"/api/admin/users/{target_id}", json={})
    assert resp.status_code == 200

    tmk = realdb.sessionmaker("a")
    async with tmk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "user.updated", AuditLog.entity_id == target_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.asyncio
async def test_admin_create_user_audits_role_names(realdb):
    # Control-plane Users aren't truncated between tests (only tenant tables
    # are) — a unique email avoids colliding with a leftover row from a prior
    # run, and the test deletes what it creates.
    email = f"new-hire-{uuid.uuid4().hex[:8]}@acme.test"
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/admin/users",
            json={"email": email, "full_name": "New Hire", "role_names": ["ap_clerk"]},
        )
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        try:
            tmk = realdb.sessionmaker("a")
            async with tmk() as s:
                row = (
                    await s.execute(
                        select(AuditLog).where(
                            AuditLog.action == "user.created",
                            AuditLog.entity_id == uuid.UUID(new_id),
                        )
                    )
                ).scalar_one()
            assert row.details["role_names"] == ["ap_clerk"]
        finally:
            await c.delete(f"/api/admin/users/{new_id}")


@pytest.mark.asyncio
async def test_admin_update_user_audits_changed_fields(realdb):
    """A rename is an ordinary field change — no forced logout, but it must
    still land as a PII-free audit row. `is_active: true` on an already-active
    account rides along unchanged and must NOT be named (#566)."""
    target_id = realdb.info("a").users["ap_clerk"]
    # Unique per run: the seeded clerk's control-plane row persists across the
    # slot, so a fixed name would be a no-op rename on the second run.
    new_name = f"Renamed Clerk {uuid.uuid4().hex[:6]}"
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            f"/api/admin/users/{target_id}",
            json={"full_name": new_name, "is_active": True},
        )
    assert resp.status_code == 200

    tmk = realdb.sessionmaker("a")
    async with tmk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "user.updated", AuditLog.entity_id == target_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows
    details = rows[-1].details
    assert details["changed_fields"] == ["full_name"]
    # PII-free: no full_name value leaks into the audit trail.
    assert new_name not in str(details)
    assert details["is_active"] is None
    assert details["role_names"] is None


@pytest.mark.asyncio
async def test_admin_delete_user_audits(realdb):
    email = f"to-delete-{uuid.uuid4().hex[:8]}@acme.test"
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(
            "/api/admin/users",
            json={"email": email, "full_name": "To Delete", "role_names": []},
        )
        target_id = created.json()["id"]
        resp = await c.delete(f"/api/admin/users/{target_id}")
    assert resp.status_code == 204

    tmk = realdb.sessionmaker("a")
    async with tmk() as s:
        row = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "user.deleted",
                    AuditLog.entity_id == uuid.UUID(target_id),
                )
            )
        ).scalar_one()
    assert row.actor_id == realdb.info("a").users["admin"]


@pytest.mark.asyncio
async def test_admin_role_crud_audits(realdb):
    role_name = f"Custom Auditor {uuid.uuid4().hex[:8]}"
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(
            "/api/admin/roles",
            json={"name": role_name, "description": "d1", "permissions": []},
        )
        assert created.status_code == 201
        role_id = created.json()["id"]

        updated = await c.patch(
            f"/api/admin/roles/{role_id}",
            json={"description": "d2"},
        )
        assert updated.status_code == 200

        deleted = await c.delete(f"/api/admin/roles/{role_id}")
        assert deleted.status_code == 204

    tmk = realdb.sessionmaker("a")
    async with tmk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_id == uuid.UUID(role_id))
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    actions = [r.action for r in rows]
    assert actions == ["role.created", "role.updated", "role.deleted"]
    assert rows[1].details["changed_fields"] == ["description"]
    assert rows[2].details["name"] == role_name


@pytest.mark.asyncio
async def test_role_update_names_only_the_fields_that_differ(realdb):
    """The role editor resends description + permissions on every save; the
    trail names a field only when its value changed (#575), and a save that
    changes nothing writes no row at all. Permission ORDER is not a change."""
    role_name = f"Custom Diff {uuid.uuid4().hex[:8]}"
    perms = ["invoice.approve", "vendor.manage"]
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(
            "/api/admin/roles",
            json={"name": role_name, "description": "same", "permissions": perms},
        )
        assert created.status_code == 201, created.text
        role_id = created.json()["id"]
        try:
            # Full-form save, nothing changed (permissions re-sent reordered).
            noop = await c.patch(
                f"/api/admin/roles/{role_id}",
                json={"description": "same", "permissions": list(reversed(perms))},
            )
            assert noop.status_code == 200, noop.text
            # Full-form save, only the grants changed.
            grants = await c.patch(
                f"/api/admin/roles/{role_id}",
                json={"description": "same", "permissions": ["invoice.approve"]},
            )
            assert grants.status_code == 200, grants.text
        finally:
            await c.delete(f"/api/admin/roles/{role_id}")

    async with realdb.sessionmaker("a")() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_id == uuid.UUID(role_id),
                        AuditLog.action == "role.updated",
                    )
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [r.details["changed_fields"] for r in rows] == [["permissions"]]
    assert rows[0].details["permissions"] == ["invoice.approve"]


# --------------------------------------------------------------------------
# Standalone force-logout — `POST /api/admin/users/{id}/revoke-sessions`.
# Forced logout already rides along with a role change / password reset /
# deactivation, but incident response needs it on its own: keep the account,
# kill the sessions. Without this an admin has to deactivate-and-reactivate,
# locking the user out and recording a suspension that never happened.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_revoke_sessions_endpoint_forces_logout(realdb, monkeypatch):
    calls: list = []

    async def _fake_revoke(user_id):
        calls.append(user_id)
        return ["jti-1", "jti-2"]

    monkeypatch.setattr("app.api.admin.revoke_user_sessions", _fake_revoke)

    target_id = realdb.info("a").users["ap_clerk"]
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/admin/users/{target_id}/revoke-sessions")

    assert resp.status_code == 200
    assert resp.json() == {"revoked": 2}
    assert calls == [target_id]

    tmk = realdb.sessionmaker("a")
    async with tmk() as s:
        row = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "user.sessions_revoked",
                    AuditLog.entity_id == target_id,
                )
            )
        ).scalar_one()
    assert row.actor_id == realdb.info("a").users["admin"]
    assert row.details == {"revoked": 2}


@pytest.mark.asyncio
async def test_admin_revoke_sessions_refuses_a_user_from_another_org(realdb, monkeypatch):
    """Org-scoped: tenant B's user is a 404 to tenant A's admin, and no
    revocation runs — otherwise the endpoint would be a cross-tenant kill switch."""
    calls: list = []

    async def _fake_revoke(user_id):
        calls.append(user_id)
        return []

    monkeypatch.setattr("app.api.admin.revoke_user_sessions", _fake_revoke)

    foreign_id = realdb.info("b").users["admin"]
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/admin/users/{foreign_id}/revoke-sessions")

    assert resp.status_code == 404
    assert calls == []


@pytest.mark.asyncio
async def test_admin_revoke_sessions_requires_user_manage(realdb, monkeypatch):
    """Ending someone else's session is a user-administration action, not
    something any authenticated employee may do to a colleague."""
    calls: list = []

    async def _fake_revoke(user_id):
        calls.append(user_id)
        return []

    monkeypatch.setattr("app.api.admin.revoke_user_sessions", _fake_revoke)

    target_id = realdb.info("a").users["admin"]
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/admin/users/{target_id}/revoke-sessions")

    assert resp.status_code == 403
    assert calls == []


# --------------------------------------------------------------------------
# _authorize_target_mutation — the other half of the escalation guard.
#
# `_authorize_role_grant` stops a `user.manage` holder from handing OUT
# authority they don't hold. It never inspected the TARGET, so the same actor
# could reset an org admin's password (they choose the value, so the complexity
# check above is no obstacle), deactivate them, move their email, strip their
# roles, delete them, or force-log them out — every one of which is a takeover
# or lock-out of an account that outranks them.
# --------------------------------------------------------------------------


def _target(roles: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(roles=roles)


def test_target_mutation_admin_caller_may_touch_an_admin():
    caller = _caller([_sys_role(ROLE_ADMIN)])
    _authorize_target_mutation(caller, _target([_sys_role(ROLE_ADMIN)]))  # no raise


def test_target_mutation_user_manage_only_caller_cannot_touch_an_admin():
    """The exploit: a custom 'User Admin' role carrying ONLY user.manage aims
    at the org admin's account."""
    caller = _caller([_custom_role([PERM_USER_MANAGE], name="UserAdmin")])
    with pytest.raises(HTTPException) as exc:
        _authorize_target_mutation(caller, _target([_sys_role(ROLE_ADMIN)]))
    assert exc.value.status_code == 403


def test_target_mutation_full_catalog_non_admin_still_cannot_touch_an_admin():
    """Holding every catalog permission is still not `admin` — the system role
    carries non-catalog authority the subset check can't see."""
    caller = _caller([_custom_role(list(ALL_PERMISSIONS), name="Everything")])
    with pytest.raises(HTTPException) as exc:
        _authorize_target_mutation(caller, _target([_sys_role(ROLE_ADMIN)]))
    assert exc.value.status_code == 403


def test_target_mutation_refuses_target_holding_a_permission_caller_lacks():
    """No admin role on either side — a user.manage-only caller still can't
    seize an account that can execute payments."""
    caller = _caller([_custom_role([PERM_USER_MANAGE], name="UserAdmin")])
    target = _target([_custom_role([PERM_USER_MANAGE, PERM_PAYMENT_EXECUTE], name="Payer")])
    with pytest.raises(HTTPException) as exc:
        _authorize_target_mutation(caller, target)
    assert exc.value.status_code == 403


def test_target_mutation_allows_a_strictly_lesser_target():
    caller = _caller([_custom_role([PERM_USER_MANAGE, PERM_PAYMENT_EXECUTE], name="Ops")])
    _authorize_target_mutation(caller, _target([_sys_role(ROLE_AP_CLERK)]))  # no raise


def test_target_mutation_allows_self():
    """A user is trivially a subset of themselves — editing your own account
    must not be blocked by the guard."""
    roles = [_custom_role([PERM_USER_MANAGE], name="UserAdmin")]
    caller = _caller(roles)
    _authorize_target_mutation(caller, _target(roles))


def test_target_mutation_falls_back_when_effective_permissions_missing():
    """Mirrors the grant guard: a caller object without the transient
    `effective_permissions` attribute is resolved from its roles instead."""
    caller = SimpleNamespace(roles=[_sys_role(ROLE_ADMIN)])
    _authorize_target_mutation(caller, _target([_sys_role(ROLE_ADMIN)]))


# --------------------------------------------------------------------------
# End-to-end: a `user.manage`-only principal against the real endpoints.
# --------------------------------------------------------------------------


async def _make_user_manage_principal(client, org_id):
    """Create a custom role granting ONLY user.manage + a user holding it.

    Returns (token, user_id, role_id) — the caller cleans both rows up.
    """
    from app.api.deps import create_access_token

    suffix = uuid.uuid4().hex[:8]
    role_resp = await client.post(
        "/api/admin/roles",
        json={"name": f"UserAdmin-{suffix}", "permissions": [PERM_USER_MANAGE]},
    )
    assert role_resp.status_code == 201, role_resp.text
    role_id = role_resp.json()["id"]

    user_resp = await client.post(
        "/api/admin/users",
        json={
            "email": f"useradmin-{suffix}@acme.test",
            "full_name": "User Admin",
            "role_names": [f"UserAdmin-{suffix}"],
        },
    )
    assert user_resp.status_code == 201, user_resp.text
    user_id = user_resp.json()["id"]
    return create_access_token(uuid.UUID(user_id), org_id), user_id, role_id


@pytest.mark.asyncio
async def test_user_manage_only_caller_cannot_reset_an_admins_password(realdb):
    """The headline takeover: reset the org admin's password and receive the
    temp, then sign in as them. Must be 403, and the admin's stored hash must
    be untouched."""
    from app.models.user import User

    info = realdb.info("a")
    admin_id = info.users["admin"]
    async with realdb.client(key="a", role="admin") as c:
        token, actor_id, role_id = await _make_user_manage_principal(c, info.org_id)
        try:
            ctrl = realdb.control_sessionmaker()
            async with ctrl() as s:
                before = (
                    (await s.execute(select(User).where(User.id == admin_id)))
                    .scalar_one()
                    .hashed_password
                )

            resp = await c.post(
                f"/api/admin/users/{admin_id}/reset-password",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 403, resp.text
            assert "temporary_password" not in resp.text

            async with ctrl() as s:
                after = (
                    (await s.execute(select(User).where(User.id == admin_id)))
                    .scalar_one()
                    .hashed_password
                )
            assert after == before
        finally:
            await c.delete(f"/api/admin/users/{actor_id}")
            await c.delete(f"/api/admin/roles/{role_id}")


@pytest.mark.asyncio
async def test_user_manage_only_caller_cannot_deactivate_or_delete_an_admin(realdb):
    info = realdb.info("a")
    admin_id = info.users["admin"]
    async with realdb.client(key="a", role="admin") as c:
        token, actor_id, role_id = await _make_user_manage_principal(c, info.org_id)
        auth = {"Authorization": f"Bearer {token}"}
        try:
            deactivate = await c.patch(
                f"/api/admin/users/{admin_id}", json={"is_active": False}, headers=auth
            )
            assert deactivate.status_code == 403, deactivate.text

            steal_email = await c.patch(
                f"/api/admin/users/{admin_id}",
                json={"email": f"attacker-{uuid.uuid4().hex[:6]}@evil.test"},
                headers=auth,
            )
            assert steal_email.status_code == 403, steal_email.text

            demote = await c.patch(
                f"/api/admin/users/{admin_id}", json={"role_names": []}, headers=auth
            )
            assert demote.status_code == 403, demote.text

            delete = await c.delete(f"/api/admin/users/{admin_id}", headers=auth)
            assert delete.status_code == 403, delete.text

            revoke = await c.post(
                f"/api/admin/users/{admin_id}/revoke-sessions", json={}, headers=auth
            )
            assert revoke.status_code == 403, revoke.text

            bulk = await c.post(
                "/api/admin/users/bulk-delete", json={"user_ids": [str(admin_id)]}, headers=auth
            )
            assert bulk.status_code == 200, bulk.text
            assert bulk.json()["deleted"] == []
            assert [f["reason"] for f in bulk.json()["failed"]] == ["forbidden"]
        finally:
            await c.delete(f"/api/admin/users/{actor_id}")
            await c.delete(f"/api/admin/roles/{role_id}")


@pytest.mark.asyncio
async def test_user_manage_only_caller_may_still_manage_a_lesser_user(realdb):
    """The guard must not break the legitimate use it was built for: a
    user.manage holder administering an ordinary clerk."""
    info = realdb.info("a")
    async with realdb.client(key="a", role="admin") as c:
        token, actor_id, role_id = await _make_user_manage_principal(c, info.org_id)
        auth = {"Authorization": f"Bearer {token}"}
        suffix = uuid.uuid4().hex[:8]
        created = await c.post(
            "/api/admin/users",
            json={
                "email": f"clerk-{suffix}@acme.test",
                "full_name": "A Clerk",
                "role_names": ["ap_clerk"],
            },
            headers=auth,
        )
        assert created.status_code == 201, created.text
        clerk_id = created.json()["id"]
        try:
            resp = await c.post(f"/api/admin/users/{clerk_id}/reset-password", headers=auth)
            assert resp.status_code == 200, resp.text
            assert resp.json()["temporary_password"]
        finally:
            await c.delete(f"/api/admin/users/{clerk_id}")
            await c.delete(f"/api/admin/users/{actor_id}")
            await c.delete(f"/api/admin/roles/{role_id}")


@pytest.mark.asyncio
async def test_user_manage_only_caller_can_read_the_supporting_endpoints(realdb):
    """Persona-panel finding — `GET /admin/users`, `/admin/roles` and
    `/admin/permissions` were still `require_roles(ROLE_ADMIN)` even after the
    mutating `/admin/users` routes moved to `require_permission(user.manage)`,
    so a custom role holding ONLY user.manage could grant/manage users but
    couldn't SEE the roster or the role picker it needs to do it. All three
    are now `require_permission(PERM_USER_MANAGE)` (role CRUD itself —
    `POST`/`PATCH`/`DELETE /admin/roles` — deliberately was not moved)."""
    info = realdb.info("a")
    async with realdb.client(key="a", role="admin") as c:
        token, actor_id, role_id = await _make_user_manage_principal(c, info.org_id)
        auth = {"Authorization": f"Bearer {token}"}
        try:
            users_resp = await c.get("/api/admin/users", headers=auth)
            assert users_resp.status_code == 200, users_resp.text

            roles_resp = await c.get("/api/admin/roles", headers=auth)
            assert roles_resp.status_code == 200, roles_resp.text
            # The picker POST/PATCH /admin/users reads role_names from.
            assert any(r["name"] == "ap_clerk" for r in roles_resp.json())

            perms_resp = await c.get("/api/admin/permissions", headers=auth)
            assert perms_resp.status_code == 200, perms_resp.text
            assert any(p["key"] == PERM_USER_MANAGE for p in perms_resp.json())
        finally:
            await c.delete(f"/api/admin/users/{actor_id}")
            await c.delete(f"/api/admin/roles/{role_id}")


@pytest.mark.asyncio
async def test_user_manage_only_caller_can_create_update_delete_a_user(realdb):
    """The full lifecycle the permission is FOR: a custom role holding ONLY
    user.manage can create a lesser user, update it, and delete it — end to
    end through the real endpoints, not just the pure guard functions."""
    info = realdb.info("a")
    async with realdb.client(key="a", role="admin") as c:
        token, actor_id, role_id = await _make_user_manage_principal(c, info.org_id)
        auth = {"Authorization": f"Bearer {token}"}
        suffix = uuid.uuid4().hex[:8]
        try:
            created = await c.post(
                "/api/admin/users",
                json={
                    "email": f"lifecycle-{suffix}@acme.test",
                    "full_name": "Lifecycle Clerk",
                    "role_names": ["ap_clerk"],
                },
                headers=auth,
            )
            assert created.status_code == 201, created.text
            lesser_id = created.json()["id"]

            updated = await c.patch(
                f"/api/admin/users/{lesser_id}",
                json={"full_name": "Lifecycle Clerk Renamed"},
                headers=auth,
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["full_name"] == "Lifecycle Clerk Renamed"

            deleted = await c.delete(f"/api/admin/users/{lesser_id}", headers=auth)
            assert deleted.status_code == 204, deleted.text

            # Confirmed gone — a follow-up read via the admin client 404s.
            gone = await c.get("/api/admin/users", params={"search": f"lifecycle-{suffix}"})
            assert gone.json()["total"] == 0
        finally:
            await c.delete(f"/api/admin/users/{actor_id}")
            await c.delete(f"/api/admin/roles/{role_id}")


@pytest.mark.asyncio
async def test_without_user_manage_every_admin_users_route_is_refused(realdb):
    """The other half: a role that does NOT hold user.manage (a plain
    ap_clerk — none of the four system roles except admin default-hold it,
    see `ROLE_DEFAULT_PERMISSIONS`) is refused on every user.manage-gated
    route, including the three read endpoints this finding wired up."""
    info = realdb.info("a")
    auth = {"Authorization": f"Bearer {realdb.token('a', 'ap_clerk')}"}
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.get("/api/admin/users", headers=auth)).status_code == 403
        assert (await c.get("/api/admin/roles", headers=auth)).status_code == 403
        assert (await c.get("/api/admin/permissions", headers=auth)).status_code == 403
        assert (
            await c.post(
                "/api/admin/users",
                json={"email": "nope@acme.test", "full_name": "Nope", "role_names": []},
                headers=auth,
            )
        ).status_code == 403
        admin_id = info.users["admin"]
        assert (
            await c.patch(f"/api/admin/users/{admin_id}", json={"full_name": "x"}, headers=auth)
        ).status_code == 403
        assert (await c.delete(f"/api/admin/users/{admin_id}", headers=auth)).status_code == 403
        # Role CRUD stays admin-only regardless — a user.manage grant (which
        # this clerk doesn't even hold) still wouldn't reach it.
        assert (
            await c.post(
                "/api/admin/roles", json={"name": f"nope-{uuid.uuid4().hex[:6]}"}, headers=auth
            )
        ).status_code == 403


# --------------------------------------------------------------------------
# Persona-panel finding #328 — the sole org admin could self-demote or
# self-deactivate with no recovery short of a DB fix. `_authorize_target_mutation`
# only stops a caller from touching someone ELSE's account with more authority
# than they hold; self-mutation always passed it. These pin the last-admin
# lockout guard end-to-end against the real endpoint.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sole_admin_cannot_self_deactivate(realdb):
    info = realdb.info("a")
    admin_id = info.users["admin"]
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(f"/api/admin/users/{admin_id}", json={"is_active": False})
        assert resp.status_code == 409, resp.text
        assert "last active admin" in resp.json()["detail"].lower()

        # Confirm nothing was mutated by the refused request.
        check = await c.get("/api/admin/users")
        me = next(u for u in check.json()["items"] if u["id"] == str(admin_id))
        assert me["is_active"] is True


@pytest.mark.asyncio
async def test_sole_admin_cannot_self_demote(realdb):
    info = realdb.info("a")
    admin_id = info.users["admin"]
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(f"/api/admin/users/{admin_id}", json={"role_names": []})
        assert resp.status_code == 409, resp.text
        assert "last active admin" in resp.json()["detail"].lower()

        check = await c.get("/api/admin/users")
        me = next(u for u in check.json()["items"] if u["id"] == str(admin_id))
        assert any(r["name"] == "admin" for r in me["roles"])


@pytest.mark.asyncio
async def test_sole_admin_cannot_self_demote_and_deactivate_together(realdb):
    """Both fields changed in one PATCH — still refused, and still nothing
    partially applied (the guard runs before any field is mutated)."""
    info = realdb.info("a")
    admin_id = info.users["admin"]
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            f"/api/admin/users/{admin_id}",
            json={"role_names": ["ap_clerk"], "is_active": False},
        )
        assert resp.status_code == 409, resp.text

        check = await c.get("/api/admin/users")
        me = next(u for u in check.json()["items"] if u["id"] == str(admin_id))
        assert me["is_active"] is True
        assert any(r["name"] == "admin" for r in me["roles"])


@pytest.mark.asyncio
async def test_admin_can_self_demote_when_another_active_admin_exists(realdb):
    """The guard only blocks the LAST admin — with a second active admin in
    place, self-demotion is a legitimate handoff and must succeed."""
    from app.api.deps import create_access_token

    info = realdb.info("a")
    admin_id = info.users["admin"]
    suffix = uuid.uuid4().hex[:8]
    async with realdb.client(key="a", role="admin") as c:
        second = await c.post(
            "/api/admin/users",
            json={
                "email": f"second-admin-{suffix}@acme.test",
                "full_name": "Second Admin",
                "role_names": ["admin"],
            },
        )
        assert second.status_code == 201, second.text
        second_id = second.json()["id"]
        second_auth = {
            "Authorization": f"Bearer {create_access_token(uuid.UUID(second_id), info.org_id)}"
        }
        try:
            demote = await c.patch(f"/api/admin/users/{admin_id}", json={"role_names": []})
            assert demote.status_code == 200, demote.text
            assert demote.json()["roles"] == []
        finally:
            # Restore the seeded admin's role (via the second admin — the
            # original's token no longer clears an admin-gated route) so
            # later tests in this session see the fixture unchanged, then
            # remove the second admin this test created.
            restore = await c.patch(
                f"/api/admin/users/{admin_id}",
                json={"role_names": ["admin"]},
                headers=second_auth,
            )
            assert restore.status_code == 200, restore.text
            await c.delete(f"/api/admin/users/{second_id}", headers=second_auth)
