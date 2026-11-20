"""SCIM /Groups — group→role mapping, storage, PATCH parsing, reconciliation.

Mirrors the DB-free style of test_sso_scim.py: pure logic is unit-tested
directly, and the role-reconciliation DB writes are exercised with a recording
mock session (no Postgres needed).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.schemas.scim import SCIMGroup, SCIMGroupListResponse, SCIMGroupMember
from app.services import scim_groups as sg

# --- pure mapping logic -----------------------------------------------------


def test_desired_role_names_unions_across_groups():
    groups = {
        "g1": {"displayName": "AP Managers", "members": ["u1", "u2"]},
        "g2": {"displayName": "Finance Admins", "members": ["u1"]},
        "g3": {"displayName": "Unmapped", "members": ["u1"]},
    }
    role_map = {"AP Managers": "ap_manager", "Finance Admins": "admin"}
    # u1 is in both mapped groups + an unmapped one
    assert sg.desired_role_names_for_user(groups, role_map, "u1") == {"ap_manager", "admin"}
    # u2 only in AP Managers
    assert sg.desired_role_names_for_user(groups, role_map, "u2") == {"ap_manager"}
    # u3 in nothing
    assert sg.desired_role_names_for_user(groups, role_map, "u3") == set()


def test_managed_role_names():
    assert sg.managed_role_names({"A": "ap_manager", "B": "admin"}) == {"ap_manager", "admin"}
    assert sg.managed_role_names({}) == set()


# --- JSONB storage ----------------------------------------------------------


def test_get_groups_and_role_map():
    settings = {
        "sso": {
            "scim_groups": {"g1": {"displayName": "X", "members": ["u1"]}},
            "scim_group_role_map": {"X": "admin"},
        }
    }
    assert sg.get_groups(settings) == {"g1": {"displayName": "X", "members": ["u1"]}}
    assert sg.get_role_map(settings) == {"X": "admin"}
    assert sg.get_groups(None) == {}
    assert sg.get_role_map({}) == {}


def test_write_groups_reassigns_fresh_dict_and_preserves_siblings():
    org = SimpleNamespace(settings={"sso": {"enabled": True, "provider": "okta"}, "company": {}})
    sg.write_groups(org, {"g1": {"displayName": "X", "members": []}})
    # JSONB dirty-tracking needs a fresh object, not an in-place mutation
    assert org.settings["sso"]["scim_groups"] == {"g1": {"displayName": "X", "members": []}}
    # sibling sso + top-level keys preserved
    assert org.settings["sso"]["enabled"] is True
    assert "company" in org.settings


# --- PATCH op parsing -------------------------------------------------------


def _op(op, path=None, value=None):
    return SimpleNamespace(op=op, path=path, value=value)


def test_patch_add_members():
    data = {"displayName": "X", "members": ["u1"]}
    out = sg.apply_patch_ops(data, [_op("add", "members", [{"value": "u2"}, {"value": "u1"}])])
    assert out["members"] == ["u1", "u2"]  # u1 not duplicated


def test_patch_remove_member_via_filter_path():
    data = {"displayName": "X", "members": ["u1", "u2"]}
    out = sg.apply_patch_ops(data, [_op("remove", 'members[value eq "u1"]')])
    assert out["members"] == ["u2"]


def test_patch_remove_members_via_value():
    data = {"displayName": "X", "members": ["u1", "u2", "u3"]}
    out = sg.apply_patch_ops(data, [_op("remove", "members", [{"value": "u2"}])])
    assert out["members"] == ["u1", "u3"]


def test_patch_replace_members_and_rename():
    data = {"displayName": "Old", "members": ["u1"]}
    out = sg.apply_patch_ops(
        data,
        [
            _op("replace", "displayName", "New"),
            _op("replace", "members", [{"value": "u9"}]),
        ],
    )
    assert out["displayName"] == "New"
    assert out["members"] == ["u9"]


def test_patch_pathless_value_dict():
    data = {"displayName": "Old", "members": ["u1"]}
    out = sg.apply_patch_ops(data, [_op("replace", None, {"displayName": "New", "members": []})])
    assert out["displayName"] == "New"
    assert out["members"] == []


def test_patch_unsupported_raises():
    with pytest.raises(sg.GroupPatchError):
        sg.apply_patch_ops({"members": []}, [_op("add", "externalId", "x")])


# --- role reconciliation (recording mock session) ---------------------------


class _RecordingDB:
    """Records UserRole adds + delete() calls; serves the user's current roles
    from `current_roles` (list of (name, id))."""

    def __init__(self, current_roles, role_lookup):
        self._current = current_roles
        self._role_lookup = role_lookup  # name -> Role(id)
        self.added = []
        self.deleted = []
        self._calls = 0

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        # Call order per reconcile_user_roles: (1) current roles select,
        # then per managed role a _resolve_role select OR a delete().
        text = str(stmt).lower()
        result = MagicMock()
        if "delete" in text:
            self.deleted.append(stmt)
            return result
        if "join" in text:  # the current-roles query
            result.all = MagicMock(return_value=list(self._current))
            return result
        # _resolve_role select — return roles matching by lookup. We can't parse
        # the name out of the compiled SQL easily, so return ALL known roles and
        # let _resolve_role pick; tests use a single mapped role so this is exact.
        result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=list(self._role_lookup.values())))
        )
        return result


def _role(name, org_id=None):
    return SimpleNamespace(id=uuid.uuid4(), name=name, organization_id=org_id)


@pytest.fixture
def audit_calls(monkeypatch):
    """Capture SCIM role-change audit events (dispatch_auth_audit uses its own
    tenant session, which would otherwise reach for a real DB in these tests)."""
    calls = []

    async def _audit(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.services.scim_groups.dispatch_auth_audit", _audit)
    return calls


@pytest.mark.asyncio
async def test_reconcile_grants_missing_role(audit_calls):
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    groups = {"g1": {"displayName": "AP Managers", "members": [str(user_id)]}}
    role_map = {"AP Managers": "ap_manager"}
    mgr = _role("ap_manager")
    db = _RecordingDB(current_roles=[], role_lookup={"ap_manager": mgr})

    await sg.reconcile_user_roles(db, org_id, user_id, groups, role_map)

    assert len(db.added) == 1
    assert db.added[0].role_id == mgr.id
    assert db.added[0].user_id == user_id
    assert db.deleted == []
    # SOX: the grant is audited (PII-safe — role name + user id, no email)
    granted = [c for c in audit_calls if c["action"] == "auth.scim.role_granted"]
    assert len(granted) == 1
    assert granted[0]["details"] == {"role": "ap_manager", "source": "scim_group"}
    assert granted[0]["entity_id"] == user_id


@pytest.mark.asyncio
async def test_reconcile_revokes_role_when_no_longer_in_group(audit_calls):
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    groups = {"g1": {"displayName": "AP Managers", "members": []}}  # user removed
    role_map = {"AP Managers": "ap_manager"}
    mgr = _role("ap_manager")
    db = _RecordingDB(current_roles=[("ap_manager", mgr.id)], role_lookup={"ap_manager": mgr})

    await sg.reconcile_user_roles(db, org_id, user_id, groups, role_map)

    assert db.added == []
    assert len(db.deleted) == 1  # the managed role was revoked
    assert [c["action"] for c in audit_calls] == ["auth.scim.role_revoked"]


@pytest.mark.asyncio
async def test_reconcile_leaves_unmanaged_roles_untouched():
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    groups = {"g1": {"displayName": "AP Managers", "members": [str(user_id)]}}
    role_map = {"AP Managers": "ap_manager"}
    mgr = _role("ap_manager")
    # user already holds ap_manager (managed, satisfied) AND admin (NOT managed)
    db = _RecordingDB(
        current_roles=[("ap_manager", mgr.id), ("admin", uuid.uuid4())],
        role_lookup={"ap_manager": mgr},
    )

    await sg.reconcile_user_roles(db, org_id, user_id, groups, role_map)

    # Nothing added (already has ap_manager), nothing removed (admin is unmanaged)
    assert db.added == []
    assert db.deleted == []


@pytest.mark.asyncio
async def test_reconcile_noop_when_no_role_map():
    db = _RecordingDB(current_roles=[], role_lookup={})
    await sg.reconcile_user_roles(db, uuid.uuid4(), uuid.uuid4(), {}, {})
    assert db.added == [] and db.deleted == []


# --- schema shapes ----------------------------------------------------------


def test_scim_group_schema_shape():
    g = SCIMGroup(
        id="g1",
        displayName="AP Managers",
        members=[SCIMGroupMember(value="u1", display="u1@acme.com")],
    )
    dumped = g.model_dump(by_alias=True)
    assert dumped["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:Group"]
    assert dumped["displayName"] == "AP Managers"
    assert dumped["members"][0]["value"] == "u1"


def test_scim_group_list_response_envelope():
    resp = SCIMGroupListResponse(totalResults=0, itemsPerPage=0, Resources=[])
    dumped = resp.model_dump()
    assert dumped["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    assert dumped["totalResults"] == 0


# --- role resolution against a REAL control-plane DB -------------------------
#
# The recording mock above answers every SELECT with the same canned role list,
# so it cannot see WHICH rows the WHERE clause would actually match. That blind
# spot hid a live bug: `_resolve_role` filtered with
# `Role.organization_id.in_([org_id, None])`, which Postgres renders as
# `organization_id IN (:org, NULL)`. `IN` compares with `=`, and `NULL = NULL`
# is NULL — so the four SYSTEM roles (the only rows with a NULL
# `organization_id`) matched nothing and every group→role GRANT silently
# no-oped, while the revoke branch (which resolves the role off the user's
# existing `user_roles` rows, not through here) kept working. A tenant that
# mapped an IdP group to `admin` therefore had `admin` stripped from everyone
# outside the group and handed to nobody.
#
# These two run against real Postgres precisely because the predicate's SQL
# semantics ARE the bug.


@pytest.mark.asyncio
async def test_resolve_role_finds_the_system_role(realdb):
    """A system role (`organization_id IS NULL`) must resolve for any org."""
    org_id = realdb.info("a").org_id
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        for name in ("admin", "ap_manager", "ap_clerk", "cfo"):
            role = await sg._resolve_role(s, org_id, name)
            assert role is not None, f"system role {name!r} did not resolve"
            assert role.name == name
            assert role.organization_id is None


@pytest.mark.asyncio
async def test_reconcile_grants_system_role_against_real_db(realdb, audit_calls):
    """End-to-end: a mapped group must actually INSERT the `user_roles` row.

    Uses a throwaway control-plane user — the seeded role users persist across
    tests, so granting one of them a role would leak into every later run — and
    removes it again in `finally`.
    """
    from sqlalchemy import delete, select

    from app.models.user import Role, User, UserRole

    org_id = realdb.info("a").org_id
    mk = realdb.control_sessionmaker()
    user_id = uuid.uuid4()

    async with mk() as s:
        s.add(
            User(
                id=user_id,
                email=f"scim-grant-{user_id}@example.test",
                full_name="SCIM Grant Probe",
                hashed_password=None,
                organization_id=org_id,
                is_active=True,
                must_change_password=False,
            )
        )
        await s.commit()

    try:
        groups = {"g1": {"displayName": "AP Managers", "members": [str(user_id)]}}
        role_map = {"AP Managers": "ap_manager"}
        async with mk() as s:
            await sg.reconcile_user_roles(s, org_id, user_id, groups, role_map)
            await s.commit()

        async with mk() as s:
            granted = (
                (
                    await s.execute(
                        select(Role.name)
                        .join(UserRole, UserRole.role_id == Role.id)
                        .where(UserRole.user_id == user_id)
                    )
                )
                .scalars()
                .all()
            )
        assert granted == ["ap_manager"]
        assert [c["action"] for c in audit_calls] == ["auth.scim.role_granted"]
    finally:
        async with mk() as s:
            await s.execute(delete(UserRole).where(UserRole.user_id == user_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()
