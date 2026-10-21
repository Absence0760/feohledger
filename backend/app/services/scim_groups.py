"""SCIM 2.0 group storage + group→role reconciliation.

Groups are low-cardinality per tenant (a handful of IdP groups), and the only
thing we *do* with a group is map it to an RBAC `Role`. So group state lives as
JSONB on `Organization.settings.sso` alongside the rest of the SSO config —
no dedicated table (the upgrade path if group volume ever grows is a
control-plane `scim_groups` table; the endpoint/service boundary here stays the
same). Roles remain the real RBAC primitive (`roles` / `user_roles`).

Storage shape (`settings.sso`):
    "scim_groups": {
        "<group-uuid>": {
            "displayName": "AP Managers",
            "externalId": "<idp-group-id>",
            "members": ["<userId>", ...],
            "created": "<iso>",
            "lastModified": "<iso>",
        }
    }
    "scim_group_role_map": { "AP Managers": "ap_manager", ... }

Mapping contract: a role named in `scim_group_role_map` becomes **IdP-managed**
for any user the IdP places in (or removes from) a mapped group — reconciliation
grants/revokes exactly those roles based on group membership, and never touches
roles outside the map (manual / JIT assignments are preserved).
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN
from app.models.organization import Organization
from app.models.user import Role, User, UserRole
from app.services.admin_lockout import guard_admin_removal
from app.services.audit_dispatch import dispatch_auth_audit

logger = logging.getLogger(__name__)

SCIM_GROUPS_KEY = "scim_groups"
SCIM_GROUP_ROLE_MAP_KEY = "scim_group_role_map"

# SCIM member-removal filter path: members[value eq "<id>"]
_MEMBER_FILTER_RE = re.compile(r'members\[value eq "(?P<id>[^"]+)"\]', re.IGNORECASE)


class GroupPatchError(ValueError):
    """An unsupported SCIM PATCH op/path — surfaced as a 400 invalidPath."""


# --- JSONB accessors --------------------------------------------------------


def _sso_block(org_settings: dict | None) -> dict:
    return (org_settings or {}).get("sso") or {}


def get_groups(org_settings: dict | None) -> dict:
    """Return a copy of the tenant's SCIM groups (id → group dict)."""
    return dict(_sso_block(org_settings).get(SCIM_GROUPS_KEY) or {})


def get_role_map(org_settings: dict | None) -> dict:
    """Return a copy of the displayName → role-name map."""
    return dict(_sso_block(org_settings).get(SCIM_GROUP_ROLE_MAP_KEY) or {})


def write_groups(org: Organization, groups: dict) -> None:
    """Persist the groups dict back onto org.settings.sso, reassigning fresh
    dicts so SQLAlchemy marks the JSONB column dirty (no MutableDict wrapper)."""
    new_settings = dict(org.settings or {})
    sso = dict(new_settings.get("sso") or {})
    sso[SCIM_GROUPS_KEY] = groups
    new_settings["sso"] = sso
    org.settings = new_settings


# --- Pure mapping logic (unit-tested directly) ------------------------------


def desired_role_names_for_user(groups: dict, role_map: dict, user_id: str) -> set[str]:
    """The roles a user should hold from SCIM group membership: the mapped role
    of every group they belong to (union across groups)."""
    desired: set[str] = set()
    for group in groups.values():
        if user_id in (group.get("members") or []):
            role_name = role_map.get(group.get("displayName"))
            if role_name:
                desired.add(role_name)
    return desired


def managed_role_names(role_map: dict) -> set[str]:
    """The roles SCIM controls — only these are ever added/removed by
    reconciliation, so manual/JIT assignments to other roles are untouched."""
    return set(role_map.values())


def affected_member_ids(*member_lists: list[str] | None) -> set[str]:
    """Union of member ids across snapshots — the users whose roles need
    re-reconciling after a group create/replace/patch/delete."""
    out: set[str] = set()
    for members in member_lists:
        out.update(members or [])
    return out


def _member_ids_from_value(value) -> list[str]:
    """Pull member ids out of a SCIM PATCH/PUT `value` (list of {value: id})."""
    out: list[str] = []
    for item in value or []:
        if isinstance(item, dict) and item.get("value"):
            out.append(str(item["value"]))
        elif isinstance(item, str):
            out.append(item)
    return out


def apply_patch_ops(data: dict, operations) -> dict:
    """Apply SCIM PATCH ops to a group, returning a new group-data dict with
    `displayName` / `members` updated. Members are NOT validated here — the
    caller filters them to real org users. Handles the shapes Okta / Entra /
    Authentik actually send; anything else raises GroupPatchError.

    Supported: replace displayName (`path="displayName"` or a pathless value
    dict); add/remove/replace `members` (value = [{value: id}]); single-member
    removal via `members[value eq "id"]`; clear members (`remove` on `members`
    with no value).
    """
    members = list(data.get("members") or [])
    display_name = data.get("displayName")

    for opn in operations:
        op = (opn.op or "").lower()
        path = opn.path or ""
        if path.lower() == "displayname":
            if isinstance(opn.value, str):
                display_name = opn.value
        elif _MEMBER_FILTER_RE.fullmatch(path):
            rid = _MEMBER_FILTER_RE.fullmatch(path).group("id")
            members = [m for m in members if m != rid]
        elif path.lower() == "members":
            ids = _member_ids_from_value(opn.value)
            if op == "add":
                members = members + [i for i in ids if i not in members]
            elif op == "remove":
                members = [m for m in members if m not in ids] if ids else []
            elif op == "replace":
                members = ids
            else:
                raise GroupPatchError(f"Unsupported op {op!r} on members")
        elif not path and isinstance(opn.value, dict):
            if "displayName" in opn.value:
                display_name = opn.value["displayName"]
            if "members" in opn.value:
                members = _member_ids_from_value(opn.value["members"])
        else:
            raise GroupPatchError(f"Unsupported PATCH op/path: {op} {path}")

    return {**data, "displayName": display_name, "members": members}


# --- Role reconciliation (DB) -----------------------------------------------


async def _resolve_role(db: AsyncSession, org_id, role_name: str) -> Role | None:
    """Find the Role by name, preferring an org-scoped custom role over the
    system role of the same name.

    The predicate is `== org_id OR IS NULL`, never `IN (org_id, None)`. SQL's
    `IN` compares with `=`, and `NULL = NULL` is NULL — so an `organization_id
    IN (:org, NULL)` list silently matches no system role at all (system roles
    are exactly the rows with a NULL `organization_id`). That made every
    group→role GRANT a no-op for the four built-in roles while the revoke
    branch — which looks the role up off the user's existing `user_roles` rows,
    not through here — kept working, turning a mapped group into a one-way
    strip of `admin`/`ap_manager`/`cfo`/`ap_clerk`.
    """
    result = await db.execute(
        select(Role).where(
            Role.name == role_name,
            or_(Role.organization_id == org_id, Role.organization_id.is_(None)),
        )
    )
    roles = result.scalars().all()
    # Prefer the org-scoped row (organization_id == org_id) if both exist.
    return next((r for r in roles if r.organization_id == org_id), roles[0] if roles else None)


async def reconcile_user_roles(
    db: AsyncSession, org_id, user_id, groups: dict, role_map: dict
) -> None:
    """Bring one user's SCIM-managed roles in line with their group membership.

    Only roles named in the map are added/removed; every other role the user
    holds (manual, JIT default) is left alone. Idempotent.
    """
    managed = managed_role_names(role_map)
    if not managed:
        return
    # Members are stored as strings in JSONB; coerce to UUID for the UUID column
    # (and for the audit entity_id). A malformed id is skipped rather than risking
    # a bad insert.
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, AttributeError):
        return
    desired = desired_role_names_for_user(groups, role_map, str(uid))

    current = await db.execute(
        select(Role.name, Role.id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == uid)
    )
    current_by_name = {name: rid for name, rid in current.all()}

    for role_name in managed:
        has = role_name in current_by_name
        want = role_name in desired
        if want and not has:
            role = await _resolve_role(db, org_id, role_name)
            if role is not None:
                db.add(UserRole(user_id=uid, role_id=role.id))
                logger.info("SCIM groups: granted role %s to user %s", role_name, uid)
                await dispatch_auth_audit(
                    organization_id=org_id,
                    actor_id=None,
                    action="auth.scim.role_granted",
                    entity_id=uid,
                    details={"role": role_name, "source": "scim_group"},
                )
        elif has and not want:
            await db.execute(
                delete(UserRole).where(
                    UserRole.user_id == uid,
                    UserRole.role_id == current_by_name[role_name],
                )
            )
            logger.info("SCIM groups: revoked role %s from user %s", role_name, uid)
            await dispatch_auth_audit(
                organization_id=org_id,
                actor_id=None,
                action="auth.scim.role_revoked",
                entity_id=uid,
                details={"role": role_name, "source": "scim_group"},
            )


async def admins_losing_admin(
    db: AsyncSession, org_id, member_ids: set[str], groups: dict, role_map: dict
) -> list[uuid.UUID]:
    """The active system-``admin`` holders this reconciliation would strip of it.

    Empty unless ``admin`` is itself an IdP-managed role in the map. Read before
    any role is touched, so the last-admin guard can refuse the whole group
    write up front rather than half-way through its members.
    """
    if ROLE_ADMIN not in managed_role_names(role_map):
        return []
    candidates: list[uuid.UUID] = []
    for raw in member_ids:
        try:
            uid = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
        except (ValueError, AttributeError):
            continue
        if ROLE_ADMIN not in desired_role_names_for_user(groups, role_map, str(uid)):
            candidates.append(uid)
    if not candidates:
        return []
    rows = await db.execute(
        select(User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            User.id.in_(candidates),
            User.organization_id == org_id,
            User.is_active.is_(True),
            Role.name == ROLE_ADMIN,
            Role.organization_id.is_(None),
        )
    )
    return list(rows.scalars().all())


async def reconcile_members(db: AsyncSession, org: Organization, member_ids: set[str]) -> None:
    """Reconcile roles for a set of affected users after a group change.
    Reads the (already-persisted) groups + map off org.settings.

    Raises :class:`~app.services.admin_lockout.LastAdminLockout`, before any
    role is changed, when the change would strip ``admin`` from every active
    admin in the org — the same guard the admin API and SCIM deprovisioning
    apply. The caller's transaction must then roll back, group state included.
    """
    groups = get_groups(org.settings)
    role_map = get_role_map(org.settings)
    if not managed_role_names(role_map):
        return
    await guard_admin_removal(
        db,
        org.id,
        removed_user_ids=await admins_losing_admin(db, org.id, member_ids, groups, role_map),
    )
    for user_id in member_ids:
        await reconcile_user_roles(db, org.id, user_id, groups, role_map)
