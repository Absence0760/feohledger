"""Tests for the custom-role admin CRUD.

The endpoints live in app/api/admin.py — list_roles / create_role /
update_role / delete_role. These pin the pure-Python edges (validation,
shape) without the FastAPI dispatch layer; the org-scope round-trip is
covered in the e2e suite.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

# Mock-DB tests: role changes write an audit row through their own control-plane/tenant sessions
# (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("no_auth_audit_write")


def _ctx():
    """Build the (db, user, org_id) triple every endpoint takes. The
    AsyncMock is reconfigured per-test for the specific query shape it
    needs to answer."""
    org_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), organization_id=org_id)
    db = AsyncMock()
    return db, user, org_id


def _execute_returning(scalar=None, scalars_all=None):
    """Helper that builds the result object .execute() should return."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar)
    result.scalar = MagicMock(return_value=scalar if scalar is not None else 0)
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=scalars_all or [])
    result.scalars = MagicMock(return_value=scalars)
    return result


# ---------- create_role --------------------------------------------------


@pytest.mark.asyncio
async def test_create_role_rejects_system_role_name():
    """Reserved system role names (admin / ap_manager / ...) cannot be
    minted as custom roles — the route-level RBAC gates would treat the
    custom row as the built-in one and silently break access checks."""
    from app.api.admin import create_role
    from app.schemas.admin import CreateRoleRequest

    db, user, org_id = _ctx()
    body = CreateRoleRequest(name="admin")
    with pytest.raises(HTTPException) as exc:
        await create_role(body, db=db, user=user, org_id=org_id)
    assert exc.value.status_code == 400
    assert "system role" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_create_role_rejects_blank_name():
    from app.api.admin import create_role
    from app.schemas.admin import CreateRoleRequest

    db, user, org_id = _ctx()
    body = CreateRoleRequest(name="   ")
    with pytest.raises(HTTPException) as exc:
        await create_role(body, db=db, user=user, org_id=org_id)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_create_role_returns_409_on_duplicate():
    from app.api.admin import create_role
    from app.models.user import Role
    from app.schemas.admin import CreateRoleRequest

    db, user, org_id = _ctx()
    existing = Role(id=uuid.uuid4(), name="Approver", organization_id=org_id)
    db.execute = AsyncMock(return_value=_execute_returning(scalar=existing))

    body = CreateRoleRequest(name="Approver")
    with pytest.raises(HTTPException) as exc:
        await create_role(body, db=db, user=user, org_id=org_id)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_role_persists_with_org_id():
    """Happy path: name is unique, the role is added with this org's id
    and committed."""
    from app.api.admin import create_role
    from app.schemas.admin import CreateRoleRequest

    db, user, org_id = _ctx()
    db.execute = AsyncMock(return_value=_execute_returning(scalar=None))
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    body = CreateRoleRequest(name="Approver", description="Mid-level approver")
    resp = await create_role(body, db=db, user=user, org_id=org_id)

    persisted = db.add.call_args.args[0]
    assert persisted.name == "Approver"
    assert persisted.organization_id == org_id
    assert resp.is_system is False


@pytest.mark.asyncio
async def test_create_role_stores_sanitized_permissions():
    """A custom role can now be minted WITH granular permissions — the teeth.
    Unknown entries are dropped; known ones are stored on the row."""
    from app.api.admin import create_role
    from app.api.permissions import PERM_INVOICE_APPROVE
    from app.schemas.admin import CreateRoleRequest

    db, user, org_id = _ctx()
    db.execute = AsyncMock(return_value=_execute_returning(scalar=None))
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    body = CreateRoleRequest(name="Approver", permissions=[PERM_INVOICE_APPROVE, "bogus.perm"])
    resp = await create_role(body, db=db, user=user, org_id=org_id)

    persisted = db.add.call_args.args[0]
    assert persisted.permissions == [PERM_INVOICE_APPROVE]  # bogus dropped
    assert resp.permissions == [PERM_INVOICE_APPROVE]
    assert resp.is_system is False


@pytest.mark.asyncio
async def test_create_role_defaults_to_no_permissions():
    """Omitting permissions keeps the inert pre-feature default (empty list)."""
    from app.api.admin import create_role
    from app.schemas.admin import CreateRoleRequest

    db, user, org_id = _ctx()
    db.execute = AsyncMock(return_value=_execute_returning(scalar=None))
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    resp = await create_role(CreateRoleRequest(name="Label"), db=db, user=user, org_id=org_id)
    assert db.add.call_args.args[0].permissions == []
    assert resp.permissions == []


# ---------- update_role --------------------------------------------------


@pytest.mark.asyncio
async def test_update_role_blocks_system_role_edit():
    from app.api.admin import update_role
    from app.models.user import Role
    from app.schemas.admin import UpdateRoleRequest

    db, user, org_id = _ctx()
    system_role = Role(id=uuid.uuid4(), name="admin", organization_id=None)
    db.execute = AsyncMock(return_value=_execute_returning(scalar=system_role))

    with pytest.raises(HTTPException) as exc:
        await update_role(
            system_role.id, UpdateRoleRequest(description="x"), db=db, user=user, org_id=org_id
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_role_404_for_other_orgs_role():
    """An admin must not be able to peek at or edit another org's
    custom role — the cross-tenant boundary surfaces as 404 (not 403),
    so a probe can't infer that the id exists."""
    from app.api.admin import update_role
    from app.models.user import Role
    from app.schemas.admin import UpdateRoleRequest

    db, user, org_id = _ctx()
    other_org_role = Role(id=uuid.uuid4(), name="X", organization_id=uuid.uuid4())
    db.execute = AsyncMock(return_value=_execute_returning(scalar=other_org_role))

    with pytest.raises(HTTPException) as exc:
        await update_role(
            other_org_role.id,
            UpdateRoleRequest(description="x"),
            db=db,
            user=user,
            org_id=org_id,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_update_role_replaces_permissions():
    """A provided permission list (even empty) replaces the role's grants."""
    from app.api.admin import update_role
    from app.api.permissions import PERM_VENDOR_MANAGE
    from app.models.user import Role
    from app.schemas.admin import UpdateRoleRequest

    db, user, org_id = _ctx()
    role = Role(
        id=uuid.uuid4(), name="Approver", organization_id=org_id, permissions=["invoice.approve"]
    )
    db.execute = AsyncMock(return_value=_execute_returning(scalar=role))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    resp = await update_role(
        role.id,
        UpdateRoleRequest(permissions=[PERM_VENDOR_MANAGE]),
        db=db,
        user=user,
        org_id=org_id,
    )
    assert role.permissions == [PERM_VENDOR_MANAGE]
    assert resp.permissions == [PERM_VENDOR_MANAGE]


@pytest.mark.asyncio
async def test_update_role_omitting_permissions_leaves_them_untouched():
    from app.api.admin import update_role
    from app.models.user import Role
    from app.schemas.admin import UpdateRoleRequest

    db, user, org_id = _ctx()
    role = Role(
        id=uuid.uuid4(), name="Approver", organization_id=org_id, permissions=["invoice.approve"]
    )
    db.execute = AsyncMock(return_value=_execute_returning(scalar=role))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    await update_role(
        role.id, UpdateRoleRequest(description="new desc"), db=db, user=user, org_id=org_id
    )
    assert role.permissions == ["invoice.approve"]  # untouched


# ---------- delete_role --------------------------------------------------


@pytest.mark.asyncio
async def test_delete_role_blocks_system_role():
    from app.api.admin import delete_role
    from app.models.user import Role

    db, user, org_id = _ctx()
    system_role = Role(id=uuid.uuid4(), name="admin", organization_id=None)
    db.execute = AsyncMock(return_value=_execute_returning(scalar=system_role))

    with pytest.raises(HTTPException) as exc:
        await delete_role(system_role.id, db=db, user=user, org_id=org_id)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_role_409_when_assigned_to_users():
    """Refuses with a count so the operator knows how many users to
    detach. Leaving orphaned UserRole rows would 500 the next role
    listing."""
    from app.api.admin import delete_role
    from app.models.user import Role

    db, user, org_id = _ctx()
    custom_role = Role(id=uuid.uuid4(), name="Approver", organization_id=org_id)

    # First execute() returns the role, second returns the user-count.
    role_result = _execute_returning(scalar=custom_role)
    count_result = _execute_returning(scalar=3)
    db.execute = AsyncMock(side_effect=[role_result, count_result])

    with pytest.raises(HTTPException) as exc:
        await delete_role(custom_role.id, db=db, user=user, org_id=org_id)
    assert exc.value.status_code == 409
    assert "3" in exc.value.detail


@pytest.mark.asyncio
async def test_delete_role_succeeds_when_unassigned():
    from app.api.admin import delete_role
    from app.models.user import Role

    db, user, org_id = _ctx()
    custom_role = Role(id=uuid.uuid4(), name="Approver", organization_id=org_id)
    db.execute = AsyncMock(
        side_effect=[
            _execute_returning(scalar=custom_role),  # the role lookup
            _execute_returning(scalar=0),  # zero users hold it
        ]
    )
    db.delete = AsyncMock()
    db.commit = AsyncMock()

    result = await delete_role(custom_role.id, db=db, user=user, org_id=org_id)
    assert result is None
    db.delete.assert_awaited_once_with(custom_role)
