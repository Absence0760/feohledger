"""User management endpoints for organization admins."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.api.deps import (
    ALL_ROLES,
    ROLE_ADMIN,
    get_org_id,
    require_permission,
    require_roles,
)
from app.api.pagination import PaginationParams, pagination_params
from app.api.permissions import (
    ALL_PERMISSIONS,
    PERM_USER_MANAGE,
    PERMISSION_LABELS,
    effective_permissions,
    permissions_for_role,
    sanitize_permissions,
)
from app.database import get_control_db, get_tenant_engine
from app.models.organization import Organization
from app.models.user import Role, User, UserRole
from app.schemas.admin import (
    AdminUserListResponse,
    AdminUserResponse,
    CreateRoleRequest,
    CreateUserRequest,
    CreateUserResponse,
    PermissionCatalogEntry,
    RoleResponse,
    UpdateRoleRequest,
    UpdateUserRequest,
)
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.email_adapters import EmailMessage, get_email_adapter
from app.services.session_management import revoke_user_sessions
from app.utils.emails import email_matches, normalize_email
from app.utils.passwords import (
    PasswordError,
    generate_temp_password,
    hash_password,
    validate_password_complexity,
)
from app.utils.search import ilike_contains
from app.utils.tenant_urls import tenant_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _role_to_response(role: Role) -> RoleResponse:
    # System role → its static default permission set; custom role → its stored
    # (sanitized) list. `effective_permissions` over a single role yields the
    # same union, so it's the one place this resolution lives.
    perms = effective_permissions([role])
    return RoleResponse(
        id=str(role.id),
        name=role.name,
        description=role.description,
        is_system=role.organization_id is None,
        # Sort by catalog order for a stable, predictable UI.
        permissions=[p for p in ALL_PERMISSIONS if p in perms],
    )


def _user_to_response(user: User) -> AdminUserResponse:
    return AdminUserResponse(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        roles=[_role_to_response(r) for r in user.roles],
        created_at=user.created_at.isoformat() if user.created_at else "",
    )


@router.get("/users", response_model=AdminUserListResponse)
async def list_users(
    db: AsyncSession = Depends(get_control_db),
    # user.manage defaults to admin-only (unchanged) — matches the mutating
    # /users routes below. A custom role holding only user.manage still needs
    # to SEE the roster it's allowed to manage.
    user: User = Depends(require_permission(PERM_USER_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
    search: str | None = Query(None, description="Filter by full_name or email (case-insensitive)"),
    pagination: PaginationParams = Depends(pagination_params),
):
    base = select(User).where(User.organization_id == org_id)
    if search and search.strip():
        term = search.strip()
        base = base.where(ilike_contains(User.full_name, term) | ilike_contains(User.email, term))

    total_q = await db.execute(select(func.count()).select_from(base.subquery()))
    total = int(total_q.scalar() or 0)

    paged = (
        base.options(selectinload(User.roles))
        .order_by(User.created_at.desc(), User.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    result = await db.execute(paged)
    users = result.scalars().all()
    return AdminUserListResponse(
        items=[_user_to_response(u) for u in users],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/roles", response_model=list[RoleResponse])
async def list_roles(
    db: AsyncSession = Depends(get_control_db),
    # Read-only and needed by a user.manage-only holder to populate the role
    # picker on user create/update (`POST/PATCH /admin/users` already accept
    # `role_names`, gated by the grant/mutation guards below — not by this
    # read). Role CRUD (`POST/PATCH/DELETE /admin/roles` below) stays
    # require_roles(ROLE_ADMIN): minting/editing a role definition can bundle
    # ANY catalog permission (including ones the definer doesn't hold), which
    # is a materially stronger capability than assigning an EXISTING role to a
    # user — see `_authorize_role_grant`'s docstring. Listing existing roles
    # carries no such risk.
    user: User = Depends(require_permission(PERM_USER_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """System roles + this org's custom roles. The four built-ins
    (organization_id IS NULL) gate hardcoded routes; custom rows are
    org-scoped."""
    result = await db.execute(
        select(Role)
        .where((Role.organization_id.is_(None)) | (Role.organization_id == org_id))
        .order_by(Role.organization_id.is_not(None), Role.name)
    )
    return [_role_to_response(r) for r in result.scalars().all()]


@router.get("/permissions", response_model=list[PermissionCatalogEntry])
async def list_permission_catalog(
    # Static data, no DB read, PII-free — the same low-risk profile as
    # `GET /admin/roles` above. Primarily the /admin/roles editor's companion
    # (which stays admin-only), but a user.manage-only holder legitimately
    # reaches this too since `GET /admin/roles` already echoes each role's
    # permission KEYS on `RoleResponse.permissions` — this is only the
    # key→label lookup for rendering them, not a new grant of any kind.
    user: User = Depends(require_permission(PERM_USER_MANAGE)),
):
    """The granular-permission catalog (key + human label), in display order.

    Drives the permission checkboxes in the /admin/roles create/edit modal so
    the frontend never hardcodes the catalog; also usable anywhere a role's
    permission keys (already visible via `GET /admin/roles`) need a human
    label. Static data, no DB read."""
    return [
        PermissionCatalogEntry(key=key, label=PERMISSION_LABELS[key]) for key in ALL_PERMISSIONS
    ]


@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: CreateRoleRequest,
    db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Mint a custom role for this org. The name must not collide with a
    system role, since the route-level RBAC gates would silently treat the
    custom role as the built-in one.

    A custom role grants exactly the granular permissions in `body.permissions`
    (sanitized to the catalog). With an empty list it's an inert organizational
    label, exactly as before this layer; with permissions it lets the org SPLIT
    fraud-sensitive duties (e.g. approve invoices but not execute payments). See
    roadmap "Granular permissions / segregation of duties" + docs/authentication.md
    § RBAC."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Role name cannot be empty")
    if name in ALL_ROLES:
        raise HTTPException(status_code=400, detail=f"'{name}' is a reserved system role name")
    # Per-org uniqueness on (name, organization_id) — DB enforces it via
    # uq_roles_name_org, but we 409 explicitly so the UI can show a clean
    # message instead of an IntegrityError surfaced as 500.
    existing = await db.execute(
        select(Role).where(Role.name == name, Role.organization_id == org_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Role name already exists for this org")

    role = Role(
        name=name,
        description=body.description,
        organization_id=org_id,
        permissions=sanitize_permissions(body.permissions),
    )
    db.add(role)
    await db.commit()
    await db.refresh(role)

    await dispatch_auth_audit(
        organization_id=org_id,
        actor_id=user.id,
        action="role.created",
        entity_id=role.id,
        details={"name": role.name, "permissions": role.permissions},
    )

    return _role_to_response(role)


@router.patch("/roles/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: uuid.UUID,
    body: UpdateRoleRequest,
    db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Edit a custom role — its description and/or granular permissions. The
    name is immutable (it's referenced by approval-chain configs) and system
    roles are read-only entirely (renaming `admin` would silently break every
    `require_roles(ROLE_ADMIN)` gate; their permissions come from the static
    default map, not this column)."""
    role = (await db.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.organization_id is None:
        raise HTTPException(status_code=403, detail="System roles cannot be edited")
    if role.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Role not found")

    changed_fields = []
    if body.description is not None:
        role.description = body.description
        changed_fields.append("description")
    # A provided list (even empty) replaces the grants; omitting it (None)
    # leaves them untouched. Sanitized to the known catalog.
    if body.permissions is not None:
        role.permissions = sanitize_permissions(body.permissions)
        changed_fields.append("permissions")
    await db.commit()
    await db.refresh(role)

    if changed_fields:
        await dispatch_auth_audit(
            organization_id=org_id,
            actor_id=user.id,
            action="role.updated",
            entity_id=role.id,
            details={
                "changed_fields": changed_fields,
                "permissions": role.permissions if "permissions" in changed_fields else None,
            },
        )

    return _role_to_response(role)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Delete a custom role. Refuses if any user still holds it (409 with
    `users_count`) so the operator can detach it first instead of leaving
    orphaned UserRole rows. System roles are protected."""
    role = (await db.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.organization_id is None:
        raise HTTPException(status_code=403, detail="System roles cannot be deleted")
    if role.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Role not found")

    in_use = (
        await db.execute(
            select(func.count()).select_from(UserRole).where(UserRole.role_id == role_id)
        )
    ).scalar() or 0
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"Role is assigned to {in_use} user(s); detach it first",
        )

    role_name = role.name
    await db.delete(role)
    await db.commit()

    await dispatch_auth_audit(
        organization_id=org_id,
        actor_id=user.id,
        action="role.deleted",
        entity_id=role_id,
        details={"name": role_name},
    )

    return None


def _authorize_role_grant(caller: User, roles_to_grant: list[Role]) -> None:
    """Refuse a privilege-escalating role assignment (issue #158).

    A ``user.manage`` holder must not be able to grant a role that carries more
    authority than the caller themselves holds — otherwise a custom "User Admin"
    role scoped to *only* ``user.manage`` could grant itself (or anyone) the
    system ``admin`` role and take over the org.

    Two guards, both must pass:

    * The system ``admin`` role may only be granted by a caller who is themselves
      an admin — ``admin`` carries non-catalog superuser authority (org settings,
      role CRUD) that the permission subset check below can't see.
    * The union of the *catalog* permissions the granted roles would confer must
      be a subset of the caller's own effective permissions — you can never hand
      out a sensitive permission you don't hold.
    """
    caller_holds_admin = any(
        r.name == ROLE_ADMIN and r.organization_id is None for r in (caller.roles or ())
    )
    granting_admin = any(r.name == ROLE_ADMIN and r.organization_id is None for r in roles_to_grant)
    if granting_admin and not caller_holds_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an admin may grant the admin role.",
        )

    granted_perms: set[str] = set()
    for role in roles_to_grant:
        granted_perms |= permissions_for_role(
            name=role.name,
            organization_id=role.organization_id,
            permissions=role.permissions,
        )
    caller_perms = getattr(caller, "effective_permissions", None)
    if caller_perms is None:
        caller_perms = effective_permissions(caller.roles)
    if not granted_perms <= set(caller_perms):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot grant permissions you do not hold.",
        )


def _authorize_target_mutation(caller: User, target: User) -> None:
    """Refuse a mutation of an account that outranks the caller.

    ``_authorize_role_grant`` stops a ``user.manage`` holder from handing OUT
    authority they don't hold. This is the other half: it stops them from
    seizing authority that already sits on someone else's account. Resetting an
    org admin's password (the actor chooses the value, so the complexity check
    cannot help), deactivating them, moving their email to an address the actor
    controls, stripping their roles, deleting them, or force-logging them out
    are each a takeover or denial-of-service primitive that a permission scoped
    to "manage users" must not confer.

    Same rule as the grant guard, turned around onto the TARGET — one rule, two
    directions, so neither can drift:

    * The system ``admin`` role may only be acted on by a caller who is
      themselves an admin. ``admin`` carries non-catalog superuser authority
      (org settings, role CRUD) the permission subset check below can't see.
    * The target's effective *catalog* permissions must be a subset of the
      caller's — you can never mutate an account that can do more than you.

    Self-mutation always passes: a user's own permissions are trivially a subset
    of themselves, and an admin acting on an admin passes the first check too.
    """
    caller_holds_admin = any(
        r.name == ROLE_ADMIN and r.organization_id is None for r in (caller.roles or ())
    )
    target_holds_admin = any(
        r.name == ROLE_ADMIN and r.organization_id is None for r in (target.roles or ())
    )
    if target_holds_admin and not caller_holds_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an admin may modify an admin account.",
        )

    target_perms = effective_permissions(target.roles)
    caller_perms = getattr(caller, "effective_permissions", None)
    if caller_perms is None:
        caller_perms = effective_permissions(caller.roles)
    if not target_perms <= set(caller_perms):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot modify a user who holds permissions you do not hold.",
        )


async def _org_has_other_active_admin(
    db: AsyncSession, org_id: uuid.UUID, *, exclude_user_id: uuid.UUID
) -> bool:
    """Whether the org has an active system-``admin`` holder other than the
    excluded user. Backs the last-admin lockout guard below."""
    result = await db.execute(
        select(func.count(func.distinct(User.id)))
        .select_from(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            User.organization_id == org_id,
            User.id != exclude_user_id,
            User.is_active.is_(True),
            Role.name == ROLE_ADMIN,
            Role.organization_id.is_(None),
        )
    )
    return (result.scalar() or 0) > 0


async def _guard_against_last_admin_lockout(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    target: User,
    was_active: bool,
    previous_role_names: list[str],
    new_is_active: bool,
    new_role_names: list[str],
) -> None:
    """Refuse a mutation that would leave the org with zero active admins.

    `_authorize_target_mutation` only stops a caller from seizing authority
    on SOMEONE ELSE's account — self-mutation always passes it (a user's own
    permissions are trivially a subset of themselves), so it does nothing to
    stop the org's sole admin from removing their own `admin` role or
    deactivating their own account. Either is a one-way door: once the last
    admin is gone, nothing short of a DB fix can create a new one (role
    grants and reactivation both require `user.manage`, which only an admin
    holds by default).

    Fires whenever the target is CURRENTLY an active admin and the mutation
    would make them not one (demoted, deactivated, or both) — self or not —
    and no OTHER active admin exists to undo it. For a caller acting on
    someone else, `_authorize_target_mutation` already required the caller
    to be an admin themselves, so the caller remains one and this is a
    no-op; it only ever actually blocks the self-service case.
    """
    is_admin_now = was_active and ROLE_ADMIN in previous_role_names
    stays_admin = new_is_active and ROLE_ADMIN in new_role_names
    if is_admin_now and not stays_admin:
        if not await _org_has_other_active_admin(db, org_id, exclude_user_id=target.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Cannot remove the organization's last active admin. "
                    "Promote another user to admin first."
                ),
            )


def _validate_admin_set_password(password: str) -> None:
    """Run an admin-set password through the same complexity policy as
    self-service change-password (issue #158) — a ``user.manage`` actor must not
    be able to reset an account to a trivial value and log in as it."""
    try:
        validate_password_complexity(password)
    except PasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/users", response_model=CreateUserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    db: AsyncSession = Depends(get_control_db),
    # user.manage defaults to admin-only (unchanged) — a custom role can be
    # granted user management without inheriting the rest of `admin`.
    user: User = Depends(require_permission(PERM_USER_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    # Check email uniqueness
    # Case-insensitive, and stored normalized: `users.email` is a login
    # identifier, so `Jane@Acme.com` and `jane@acme.com` are one person
    # (`utils/emails`).
    email = normalize_email(body.email)
    existing = await db.execute(select(User.id).where(email_matches(User.email, email)).limit(1))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already in use")

    # The SHARED generator (`utils/passwords`), not a local one. The local copy
    # this replaces drew 12 characters from `ascii_letters + digits` with no
    # guarantee of case mix or a digit, so an admin-created account could be
    # handed a temp credential that `validate_password_complexity` — the policy
    # every other password on the platform must satisfy — would itself refuse.
    # Signup and partner provisioning already use this one.
    temp_password = generate_temp_password()
    new_user = User(
        email=email,
        full_name=body.full_name,
        hashed_password=await hash_password(temp_password),
        organization_id=org_id,
        must_change_password=True,
    )
    db.add(new_user)
    await db.flush()

    # Assign roles. Restrict the lookup to system roles + this org's
    # custom roles so an admin from acme can't accidentally (or
    # otherwise) grant a role minted by techflow.
    if body.role_names:
        result = await db.execute(
            select(Role).where(
                Role.name.in_(body.role_names),
                (Role.organization_id.is_(None)) | (Role.organization_id == org_id),
            )
        )
        roles = result.scalars().all()
        _authorize_role_grant(user, list(roles))
        for role in roles:
            db.add(UserRole(user_id=new_user.id, role_id=role.id))
        await db.flush()

    # Reload with roles
    result = await db.execute(
        select(User).where(User.id == new_user.id).options(selectinload(User.roles))
    )
    new_user = result.scalar_one()
    await db.commit()

    await dispatch_auth_audit(
        organization_id=org_id,
        actor_id=user.id,
        action="user.created",
        entity_id=new_user.id,
        details={"role_names": sorted(r.name for r in new_user.roles)},
    )

    # Best-effort welcome email — same posture as the supplier-portal invite
    # (`vendors.py::invite_vendor_portal_user`) and the tenant-signup welcome
    # email: a failed send never breaks user creation, and the temp password
    # keeps riding the API response below so local dev (where email may be
    # unconfigured — `console` adapter just logs) and an admin who'd rather
    # relay it out-of-band both still work. This makes "Invite User" an actual
    # email invite instead of only a shown-once password the admin must copy.
    org_row = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org_row is not None:
        tenant_url = tenant_base_url(org_row.slug, org_row.settings) or None
        try:
            await get_email_adapter().send(
                EmailMessage(
                    to=new_user.email,
                    subject=f"You've been invited to {org_row.name} on FeohLedger",
                    body_text=(
                        f"Hi {new_user.full_name},\n\n"
                        f"An administrator has created an account for you on "
                        f"{org_row.name}'s FeohLedger workspace.\n\n"
                        + (f"  URL:      {tenant_url}\n" if tenant_url else "")
                        + f"  Email:    {new_user.email}\n"
                        f"  Password: {temp_password}\n\n"
                        "You'll be asked to change your password on first sign-in.\n"
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("Welcome email failed for new user %s (org=%s)", new_user.id, org_id)

    resp = _user_to_response(new_user)
    return CreateUserResponse(**resp.model_dump(), temporary_password=temp_password)


@router.patch("/users/{user_id}", response_model=AdminUserResponse)
async def update_user(
    user_id: uuid.UUID,
    body: UpdateUserRequest,
    db: AsyncSession = Depends(get_control_db),
    current_user: User = Depends(require_permission(PERM_USER_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    result = await db.execute(
        select(User)
        .where(User.id == user_id, User.organization_id == org_id)
        .options(selectinload(User.roles))
    )
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Every branch below except `full_name` either seizes control of the target
    # account (password / email), disables it (`is_active`), or changes what it
    # can do (`role_names` — including *removing* a role, which the grant guard
    # never sees because it only inspects the roles being granted). Gate them
    # all on the caller out-ranking the target. A rename carries no authority,
    # so it stays open to any `user.manage` holder.
    if (
        body.password is not None
        or body.email is not None
        or body.is_active is not None
        or body.role_names is not None
    ):
        _authorize_target_mutation(current_user, target)

    # Snapshot pre-state so we can decide whether to force-logout the target.
    previous_role_names = sorted(r.name for r in target.roles)
    was_active = target.is_active

    # Refuse — before touching anything — a mutation that would leave the org
    # with zero active admins. Must run on the pre-mutation snapshot, not
    # `target` in-place, since role/active changes below are applied directly
    # onto the ORM object this same `db` session will flush.
    new_role_names = (
        sorted(set(body.role_names)) if body.role_names is not None else previous_role_names
    )
    new_is_active = body.is_active if body.is_active is not None else was_active
    await _guard_against_last_admin_lockout(
        db,
        org_id=org_id,
        target=target,
        was_active=was_active,
        previous_role_names=previous_role_names,
        new_is_active=new_is_active,
        new_role_names=new_role_names,
    )

    changed_fields = []
    if body.full_name is not None:
        target.full_name = body.full_name
        changed_fields.append("full_name")
    if body.email is not None:
        new_email = normalize_email(body.email)
        existing = await db.execute(
            select(User.id).where(email_matches(User.email, new_email), User.id != user_id).limit(1)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already in use")
        target.email = new_email
        changed_fields.append("email")
    if body.is_active is not None:
        target.is_active = body.is_active
        changed_fields.append("is_active")
    password_changed = body.password is not None
    if password_changed:
        _validate_admin_set_password(body.password)
        target.hashed_password = await hash_password(body.password)
        changed_fields.append("password")

    roles_changed = False
    if body.role_names is not None:
        roles_changed = new_role_names != previous_role_names
        await db.execute(UserRole.__table__.delete().where(UserRole.user_id == user_id))
        if body.role_names:
            result = await db.execute(
                select(Role).where(
                    Role.name.in_(body.role_names),
                    (Role.organization_id.is_(None)) | (Role.organization_id == org_id),
                )
            )
            roles = result.scalars().all()
            _authorize_role_grant(current_user, list(roles))
            for role in roles:
                db.add(UserRole(user_id=user_id, role_id=role.id))
    if roles_changed:
        changed_fields.append("role_names")

    await db.flush()

    # `populate_existing()`: the role change above deletes `UserRole` rows via
    # a raw Core-level `.delete()`, which bypasses the ORM unit-of-work and
    # never touches `target.roles` in memory. On a SELF-mutating request
    # `target` is the same identity-mapped object `get_current_user` already
    # loaded (with `roles` already populated) earlier in this same request —
    # so without this, a plain re-`select` sees the object is already in the
    # session and skips re-loading the (already-populated) relationship,
    # silently returning the pre-delete role list in the response even though
    # the DB write itself is correct.
    result = await db.execute(
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.roles))
        .execution_options(populate_existing=True)
    )
    target = result.scalar_one()
    await db.commit()

    # SOC 2: a role change (elevation or demotion), account deactivation, and
    # an admin-initiated password reset must all drop the user's existing
    # sessions. The previous JWT was signed before the change and would
    # otherwise keep the old (or compromised) session alive until it expired
    # (up to 30 min) — for a password reset that's the exact window the reset
    # is meant to close (issue #160).
    deactivated = was_active and body.is_active is False
    if roles_changed or deactivated or password_changed:
        await revoke_user_sessions(user_id)

    if changed_fields:
        await dispatch_auth_audit(
            organization_id=org_id,
            actor_id=current_user.id,
            action="user.updated",
            entity_id=user_id,
            details={
                "changed_fields": changed_fields,
                "role_names": sorted(r.name for r in target.roles) if roles_changed else None,
                "is_active": target.is_active if "is_active" in changed_fields else None,
            },
        )

    return _user_to_response(target)


class UserDeleteConflict(BaseModel):
    """Why a user can't be deleted — surfaced in the 409 body so the UI can list it."""

    open_invoice_assignments: int  # invoices.assigned_to_id, status not in (done, rejected, paid)
    pending_approval_steps: int  # workflow_steps.assigned_to (incomplete)
    active_workflow_approver_in: int  # workflow_definitions.steps_config approver_ids contains user


async def _user_reference_counts(db_name: str, user_id: uuid.UUID) -> UserDeleteConflict:
    """Count tenant-DB rows that reference this user and would be orphaned by delete.

    All three fields must be zero for a delete to proceed. Audit-log /
    payment-initiator / invoice-uploader references are intentionally
    excluded — those are historical and must survive the user.
    """
    engine = get_tenant_engine(db_name)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        # Open invoices currently assigned to this user.
        invoice_q = await session.execute(
            text(
                "SELECT COUNT(*) FROM invoices "
                "WHERE assigned_to_id = :uid "
                "AND status NOT IN ('done', 'rejected', 'paid')"
            ),
            {"uid": user_id},
        )
        open_invoices = int(invoice_q.scalar() or 0)

        # Workflow steps awaiting their approval.
        step_q = await session.execute(
            text(
                "SELECT COUNT(*) FROM workflow_steps "
                "WHERE (assigned_to = :uid OR original_assigned_to = :uid) "
                "AND completed_at IS NULL"
            ),
            {"uid": user_id},
        )
        pending_steps = int(step_q.scalar() or 0)

        # Active workflow definitions whose approver_ids list contains this user.
        # @> requires a JSONB containment check; the user_id can appear at the
        # step.config.approver_ids level OR inside step.config.approval_chain[].approver_ids.
        # JSON path '$ ?? @ == "uid"' isn't portable across pg versions; the
        # straightforward route is a text-match, which is good enough for a
        # blocklist (false positives only happen if a user's UUID literally
        # appears as a substring elsewhere — UUIDs are uncorrelated, so the
        # collision rate is effectively zero).
        defn_q = await session.execute(
            text(
                "SELECT COUNT(*) FROM workflow_definitions "
                "WHERE is_active = true "
                "AND steps_config::text LIKE :pattern"
            ),
            {"pattern": f"%{user_id}%"},
        )
        active_defs = int(defn_q.scalar() or 0)

        return UserDeleteConflict(
            open_invoice_assignments=open_invoices,
            pending_approval_steps=pending_steps,
            active_workflow_approver_in=active_defs,
        )


class RevokeSessionsResponse(BaseModel):
    """How many of the target's sessions the force-logout ended."""

    revoked: int


@router.post("/users/{user_id}/revoke-sessions", response_model=RevokeSessionsResponse)
async def revoke_user_sessions_endpoint(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_control_db),
    current_user: User = Depends(require_permission(PERM_USER_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
) -> RevokeSessionsResponse:
    """Force-log-out a user without touching their account.

    Forced logout already happens as a *side effect* of a role change, a
    password reset and deactivation — but incident response often needs it on
    its own ("this laptop was stolen; keep the account, kill the sessions").
    Making an admin deactivate-and-reactivate to get there means the account
    spends the window locked out and the audit trail records a suspension that
    never happened.

    Org-scoped: a user outside the caller's org is the same 404 as a missing
    one. Idempotent — with nothing signed in it reports ``revoked: 0``.
    """
    target = (
        await db.execute(
            select(User)
            .where(User.id == user_id, User.organization_id == org_id)
            .options(selectinload(User.roles))
        )
    ).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    # Force-logout is a denial primitive: repeated against an org admin it locks
    # the only account that can undo the attacker's other changes out of the app.
    _authorize_target_mutation(current_user, target)

    revoked = await revoke_user_sessions(user_id)
    await dispatch_auth_audit(
        organization_id=org_id,
        actor_id=current_user.id,
        action="user.sessions_revoked",
        entity_id=user_id,
        details={"revoked": len(revoked)},
    )
    return RevokeSessionsResponse(revoked=len(revoked))


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_control_db),
    current_user: User = Depends(require_permission(PERM_USER_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    result = await db.execute(
        select(User)
        .where(User.id == user_id, User.organization_id == org_id)
        .options(selectinload(User.roles))
    )
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == current_user.id:
        raise HTTPException(status_code=409, detail="Cannot delete yourself")
    # Deleting an account you couldn't otherwise touch is the bluntest form of
    # the same takeover — same rule as the PATCH branches above.
    _authorize_target_mutation(current_user, target)

    # Pre-flight safety: refuse if the user is referenced by anything that
    # would be silently orphaned. The admin must reassign references first.
    org_q = await db.execute(select(Organization).where(Organization.id == org_id))
    org = org_q.scalar_one()
    conflict = await _user_reference_counts(org.db_name, user_id)
    if (
        conflict.open_invoice_assignments
        or conflict.pending_approval_steps
        or conflict.active_workflow_approver_in
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "Cannot delete user — they are still referenced by "
                    "in-flight work. Reassign these first, then retry."
                ),
                "references": conflict.model_dump(),
            },
        )

    # Remove role assignments first
    await db.execute(UserRole.__table__.delete().where(UserRole.user_id == user_id))
    await db.delete(target)
    await db.commit()

    # Deletion → revoke any active JWT so the tombstoned user can't keep
    # hitting the API until their token expires.
    await revoke_user_sessions(user_id)

    await dispatch_auth_audit(
        organization_id=org_id,
        actor_id=current_user.id,
        action="user.deleted",
        entity_id=user_id,
    )


class BulkDeleteRequest(BaseModel):
    user_ids: list[str]


class BulkDeleteFailure(BaseModel):
    user_id: str
    reason: str  # "not_found" | "self" | "forbidden" | "blocked"
    references: UserDeleteConflict | None = None


class BulkDeleteResponse(BaseModel):
    deleted: list[str]
    failed: list[BulkDeleteFailure]


@router.post("/users/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_users(
    body: BulkDeleteRequest,
    db: AsyncSession = Depends(get_control_db),
    current_user: User = Depends(require_permission(PERM_USER_MANAGE)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Best-effort delete of multiple users.

    Each id is processed independently; a single failure does not
    short-circuit the others. The response splits successes from failures
    so the UI can refresh the table and show a per-row reason for the
    ones that didn't go through.
    """
    org_q = await db.execute(select(Organization).where(Organization.id == org_id))
    org = org_q.scalar_one()

    deleted: list[str] = []
    failed: list[BulkDeleteFailure] = []

    for raw_id in body.user_ids:
        try:
            user_uuid = uuid.UUID(raw_id)
        except ValueError:
            failed.append(BulkDeleteFailure(user_id=raw_id, reason="not_found"))
            continue

        if user_uuid == current_user.id:
            failed.append(BulkDeleteFailure(user_id=raw_id, reason="self"))
            continue

        result = await db.execute(
            select(User)
            .where(User.id == user_uuid, User.organization_id == org_id)
            .options(selectinload(User.roles))
        )
        target = result.scalar_one_or_none()
        if not target:
            failed.append(BulkDeleteFailure(user_id=raw_id, reason="not_found"))
            continue

        # Same privilege guard the single delete applies — reported per row so
        # one out-ranking target doesn't abort the rest of the batch.
        try:
            _authorize_target_mutation(current_user, target)
        except HTTPException:
            failed.append(BulkDeleteFailure(user_id=raw_id, reason="forbidden"))
            continue

        conflict = await _user_reference_counts(org.db_name, user_uuid)
        if (
            conflict.open_invoice_assignments
            or conflict.pending_approval_steps
            or conflict.active_workflow_approver_in
        ):
            failed.append(BulkDeleteFailure(user_id=raw_id, reason="blocked", references=conflict))
            continue

        await db.execute(UserRole.__table__.delete().where(UserRole.user_id == user_uuid))
        await db.delete(target)
        deleted.append(raw_id)

    await db.commit()

    for raw_id in deleted:
        await revoke_user_sessions(uuid.UUID(raw_id))
        await dispatch_auth_audit(
            organization_id=org_id,
            actor_id=current_user.id,
            action="user.deleted",
            entity_id=uuid.UUID(raw_id),
        )

    return BulkDeleteResponse(deleted=deleted, failed=failed)
