"""SCIM 2.0 /Users + /Groups endpoints for Okta + Entra + Authentik provisioning.

Each tenant has its own SCIM base URL:
    https://app.com/api/scim/v2/{Users,Groups}

and its own bearer token (hash stored in Organization.settings.sso.
scim_bearer_hash). The IdP is configured once per tenant with that URL +
token and pushes users here: list, get by id, create, PATCH (SCIM's
partial-update flavor), delete (soft — sets active=false).

/Groups maps IdP groups onto RBAC roles: group state is JSONB on
settings.sso, and membership changes drive role reconciliation against the
per-tenant `scim_group_role_map`. See app/services/scim_groups.py.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.schemas.scim import (
    USER_SCHEMA,
    SCIMEmail,
    SCIMError,
    SCIMGroup,
    SCIMGroupCreate,
    SCIMGroupListResponse,
    SCIMGroupMember,
    SCIMListResponse,
    SCIMMeta,
    SCIMName,
    SCIMPatchRequest,
    SCIMUser,
    SCIMUserCreate,
)
from app.services import scim_groups
from app.services.sso import hash_scim_token
from app.tenant import lock_organization
from app.utils.emails import email_matches, normalize_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scim/v2", tags=["scim"])


# ---------------------------------------------------------------------------
# Auth: tenant is resolved from the bearer token's hash.
# ---------------------------------------------------------------------------


async def get_scim_tenant(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_control_db),
) -> Organization:
    """Resolve the tenant for this SCIM request from the Authorization header."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _scim_http_error(401, "Missing or malformed Authorization header.")

    token = authorization.split(None, 1)[1].strip()
    # Via the shared owner, not an inline sha256 — mint and verify must move
    # together or SCIM breaks for every tenant at once. See hash_scim_token.
    digest = hash_scim_token(token)

    # Indexed lookup via the dedicated column (migration 0021). The
    # settings.sso.scim_bearer_hash key is still mirrored for backward
    # compatibility but is no longer the source of truth for auth.
    result = await db.execute(select(Organization).where(Organization.scim_bearer_hash == digest))
    org = result.scalar_one_or_none()
    if org is None:
        raise _scim_http_error(401, "Bearer token not recognised.")
    return org


async def _org_has_scim(db: AsyncSession, org: Organization) -> bool:
    from app.services.billing.entitlements import get_entitlements, has_entitlement
    from app.services.billing.plan_catalog import FEATURE_SCIM

    return has_entitlement(await get_entitlements(db, org.id), FEATURE_SCIM)


async def require_scim_entitlement(db: AsyncSession, org: Organization) -> None:
    """``FEATURE_SCIM`` gate (decisions §253, §258), answered in the SCIM error
    shape IdPs parse rather than the SPA's coded refusal.

    Applied to every write that PROVISIONS or GRANTS — create, rename,
    re-activate, group writes. Deliberately NOT applied to reads or to
    deactivation: a tenant that drops below Scale still holds the users SCIM
    provisioned, and if the IdP's deprovision of a leaver were refused, that
    account would stay active — and on a plan without SSO its password sign-in
    reopens (§258). Okta and Entra look a user up before they deactivate it, so
    the reads stay open for the same reason. Only an org that once held the
    feature can reach any of this: minting the bearer token is itself gated.
    """
    if not await _org_has_scim(db, org):
        raise _scim_http_error(
            402,
            "SCIM provisioning is not included in this organization's plan. "
            "Deprovisioning and reads remain available.",
        )


def _reads_as_false(value: object) -> bool:
    if value is False:
        return True
    return isinstance(value, str) and value.strip().lower() == "false"


def _is_deactivation_only(body: SCIMPatchRequest) -> bool:
    """Does this PATCH do nothing but deactivate the user?

    The two spellings Okta and Entra send for a deprovision: ``active`` set to
    false (or removed), and a root ``replace`` whose value is just
    ``{"active": false}``. A value that does not plainly read as false is NOT a
    deactivation, so it falls through to the gate rather than slipping past it.
    """
    if not body.Operations:
        return False
    for op in body.Operations:
        action = (op.op or "").lower()
        path = (op.path or "").strip()
        value = op.value
        if path.lower() == "active":
            if action == "remove" or _reads_as_false(value):
                continue
            return False
        if (
            path == ""
            and action == "replace"
            and isinstance(value, dict)
            and set(value) == {"active"}
            and _reads_as_false(value["active"])
        ):
            continue
        return False
    return True


def _scim_http_error(status: int, detail: str, scim_type: str | None = None) -> HTTPException:
    """Build a SCIM-compliant error response. IdPs rely on this shape."""
    body = SCIMError(status=str(status), detail=detail, scimType=scim_type).model_dump()
    return HTTPException(status_code=status, detail=body)


def _scim_active(value: object) -> bool:
    """Read a PATCH op's `active` value — a JSON boolean, or its string spelling.

    `bool(value)` was the old reading, and every non-empty string is truthy, so
    the deprovision Microsoft Entra ID sends by default —
    `{"op": "Replace", "path": "active", "value": "False"}`, the string, a known
    SCIM-compliance gap Microsoft documents behind the `aadOptscim062020` flag —
    LEFT THE ACCOUNT ACTIVE (or re-activated it) while answering 200, so the IdP
    recorded a deprovision that never happened. "true"/"false" in any case are
    accepted; anything else is a 400 `invalidValue` (RFC 7644 §3.12), never a
    guess — an unreadable lifecycle signal must surface at the IdP, not resolve
    silently to either state.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise _scim_http_error(400, "'active' must be a boolean.", "invalidValue")


async def _email_taken(
    db: AsyncSession, email: str, *, exclude_user_id: uuid.UUID | None = None
) -> bool:
    """Is this `userName` already a `users.email` ANYWHERE on the platform?

    Scoped platform-wide, not to the calling tenant, because `users.email`
    carries a global UNIQUE constraint — it is the login identifier, and
    `/auth/login` resolves an account by address alone with no tenant hint.

    Checking only `organization_id == org.id` (which is what create and PUT used
    to do, and which PATCH did not do at all) meant an IdP pushing an address
    already held in a DIFFERENT tenant sailed past the guard and tripped the DB
    constraint on flush: an unhandled IntegrityError, i.e. a 500, where RFC 7644
    §3.3 requires a 409 `uniqueness`. Providers treat a 5xx as retryable, so the
    same doomed write came back on every reconcile cycle. `admin.create_user` /
    `admin.update_user` have always checked globally; this brings SCIM in line.
    """
    # Case-insensitive too: an IdP pushing `jane@acme.com` for an admin-created
    # `Jane@Acme.com` is the same person, and missing that provisioned a
    # duplicate whose later deprovision left the original active.
    stmt = select(User.id).where(email_matches(User.email, email))
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Domain mapping: User row <-> SCIM representation.
# ---------------------------------------------------------------------------


def _user_to_scim(user: User, request: Request) -> SCIMUser:
    base = f"{str(request.base_url).rstrip('/')}{settings.scim_url_path}/Users/{user.id}"
    emails = [SCIMEmail(value=user.email, primary=True, type="work")]

    # Best-effort split of full_name → given/family. Whatever the IdP
    # pushed in originally wins on PATCH; this is just for GET responses.
    given, family = "", ""
    if user.full_name:
        parts = user.full_name.split(None, 1)
        given = parts[0]
        family = parts[1] if len(parts) > 1 else ""

    return SCIMUser(
        id=str(user.id),
        userName=user.email,
        externalId=user.sso_provider_id,
        name=SCIMName(givenName=given, familyName=family, formatted=user.full_name),
        emails=emails,
        active=user.is_active,
        meta=SCIMMeta(
            resourceType="User",
            created=user.created_at.isoformat() if user.created_at else None,
            lastModified=user.updated_at.isoformat() if user.updated_at else None,
            location=base,
        ),
    )


def _extract_primary_email(emails: list[SCIMEmail], fallback: str) -> str:
    for e in emails:
        if e.primary and e.value:
            return e.value
    for e in emails:
        if e.value:
            return e.value
    return fallback


def _extract_full_name(name: SCIMName | None, email: str) -> str:
    if name is None:
        return email.split("@", 1)[0]
    if name.formatted:
        return name.formatted
    parts = [name.givenName or "", name.familyName or ""]
    joined = " ".join(p for p in parts if p).strip()
    return joined or email.split("@", 1)[0]


# ---------------------------------------------------------------------------
# Filter parser — SCIM filters are their own mini-language; we support the
# subset Okta + Entra actually use for user provisioning. Anything else
# returns a 400 so the IdP shows a clear error to the admin.
# ---------------------------------------------------------------------------


def _apply_filter(query, filter_expr: str):
    """Apply a SCIM filter to a SQLAlchemy query. Supports:
        userName eq "email"
        emails eq "email"        (same — we store one email per user)
        externalId eq "xyz"
        active eq true / false
    Unsupported operators raise a SCIM-flavoured 400.
    """
    expr = filter_expr.strip()
    for attr, column in [
        ("userName", User.email),
        ("emails", User.email),
        ("externalId", User.sso_provider_id),
    ]:
        prefix = f"{attr} eq "
        if expr.startswith(prefix):
            value = expr[len(prefix) :].strip().strip('"')
            if column is User.email:
                # `userName` is `caseExact: false` (RFC 7643 §4.1.1). Okta and
                # Entra probe with this filter before a POST; an exact compare
                # answered "no such user" for a mixed-case row and the IdP then
                # created a second one.
                return query.where(email_matches(User.email, value))
            return query.where(column == value)

    if expr in ("active eq true", "active eq True"):
        return query.where(User.is_active.is_(True))
    if expr in ("active eq false", "active eq False"):
        return query.where(User.is_active.is_(False))

    raise _scim_http_error(
        400,
        f"Unsupported filter: {filter_expr}. Supported: userName/emails/externalId eq, active eq.",
        scim_type="invalidFilter",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/Users", response_model=SCIMListResponse)
async def list_users(
    request: Request,
    startIndex: int = Query(1, ge=1),
    count: int = Query(100, ge=0, le=1000),
    filter: str | None = Query(None),
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    base_query = select(User).where(User.organization_id == org.id)
    if filter:
        base_query = _apply_filter(base_query, filter)

    total = (
        await db.execute(select(func.count()).select_from(base_query.subquery()))
    ).scalar() or 0

    # SCIM uses 1-based indexing
    page_query = (
        base_query.order_by(User.created_at, User.id.desc()).offset(startIndex - 1).limit(count)
    )
    result = await db.execute(page_query)
    users = result.scalars().all()

    return SCIMListResponse(
        totalResults=total,
        startIndex=startIndex,
        itemsPerPage=len(users),
        Resources=[_user_to_scim(u, request) for u in users],
    )


@router.get("/Users/{user_id}", response_model=SCIMUser)
async def get_user(
    user_id: uuid.UUID,
    request: Request,
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    result = await db.execute(
        select(User).where(User.id == user_id, User.organization_id == org.id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _scim_http_error(404, f"User {user_id} not found.")
    return _user_to_scim(user, request)


@router.post("/Users", response_model=SCIMUser, status_code=201)
async def create_user(
    body: SCIMUserCreate,
    request: Request,
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    await require_scim_entitlement(db, org)
    email = normalize_email(_extract_primary_email(body.emails, body.userName))

    # SCIM requires 409 on duplicate userName. Platform-wide — see `_email_taken`.
    if await _email_taken(db, email):
        raise _scim_http_error(409, f"User with userName {email} already exists.", "uniqueness")

    sso = (org.settings or {}).get("sso") or {}
    provider = sso.get("provider") or "oidc"

    user = User(
        id=uuid.uuid4(),
        email=email,
        full_name=_extract_full_name(body.name, email),
        sso_provider=provider,
        sso_provider_id=body.externalId,
        hashed_password=None,  # SCIM-managed users are SSO-only
        is_active=body.active,
        organization_id=org.id,
        must_change_password=False,
    )
    db.add(user)
    await db.flush()
    logger.info("SCIM provisioned user %s in org %s", email, org.slug)
    return _user_to_scim(user, request)


@router.put("/Users/{user_id}", response_model=SCIMUser)
async def replace_user(
    user_id: uuid.UUID,
    body: SCIMUserCreate,
    request: Request,
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    """SCIM PUT — full-resource replace. Okta + Entra drive updates with PATCH,
    but Authentik (and RFC 7644 §3.5.1) PUT the whole updated resource on every
    change. Without this, those IdPs get a 405 on every user update. We map the
    mutable fields of the SCIM resource onto the existing row."""
    result = await db.execute(
        select(User).where(User.id == user_id, User.organization_id == org.id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _scim_http_error(404, f"User {user_id} not found.")

    if not await _org_has_scim(db, org):
        if body.active:
            await require_scim_entitlement(db, org)  # raises the SCIM-shaped 402
        # A PUT with `active: false` is how Authentik (and Okta in PUT mode)
        # deprovisions. Without the feature ONLY that deactivation is applied —
        # the rest of the resource is not written — so a leaver is still shut
        # out after a downgrade (see `require_scim_entitlement`).
        user.is_active = False
        await db.flush()
        await db.refresh(user)
        return _user_to_scim(user, request)

    email = normalize_email(_extract_primary_email(body.emails, body.userName))
    # Uniqueness invariant: PUT must not rename this user onto another user's
    # userName. Platform-wide — see `_email_taken`.
    if await _email_taken(db, email, exclude_user_id=user.id):
        raise _scim_http_error(409, f"User with userName {email} already exists.", "uniqueness")

    user.email = email
    user.full_name = _extract_full_name(body.name, email)
    if body.externalId is not None:
        user.sso_provider_id = body.externalId
    user.is_active = body.active
    await db.flush()
    # Same `updated_at` onupdate-expiry guard as patch_user — reload before
    # serializing so we don't trip a sync lazy-load in the async handler.
    await db.refresh(user)
    return _user_to_scim(user, request)


@router.patch("/Users/{user_id}", response_model=SCIMUser)
async def patch_user(
    user_id: uuid.UUID,
    body: SCIMPatchRequest,
    request: Request,
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    """Apply a subset of PATCH operations. We support the ops Okta + Entra
    send for user lifecycle: setting active=false on deprovision, replacing
    name/email, updating externalId.

    A `userName` op runs through the same uniqueness guard create and PUT use.
    PATCH had none at all, so an IdP renaming a user onto an address already in
    use hit the DB's global UNIQUE on flush — an unhandled 500 instead of the
    409 `uniqueness` RFC 7644 §3.5.2 calls for. The rename is staged in a local
    and only applied once it is known to be free, so a rejected PATCH cannot
    leave a half-applied op on the session.
    """
    result = await db.execute(
        select(User).where(User.id == user_id, User.organization_id == org.id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _scim_http_error(404, f"User {user_id} not found.")

    # Deprovisioning stays open on any plan; everything else needs SCIM.
    if not _is_deactivation_only(body):
        await require_scim_entitlement(db, org)

    new_email: str | None = None

    for op in body.Operations:
        action = (op.op or "").lower()
        path = (op.path or "").strip()
        value = op.value

        if action == "remove" and path == "":
            # Ignore — empty remove is a no-op
            continue

        if path.lower() == "active":
            user.is_active = _scim_active(value) if action != "remove" else False
        elif path == "userName" and action in ("replace", "add"):
            if isinstance(value, str):
                new_email = normalize_email(value)
        elif path == "externalId" and action in ("replace", "add"):
            if isinstance(value, str):
                user.sso_provider_id = value
        elif path == "" and isinstance(value, dict) and action == "replace":
            # Bulk replace on the root — common from Okta for status changes
            if "active" in value:
                user.is_active = _scim_active(value["active"])
            if "userName" in value and isinstance(value["userName"], str):
                new_email = normalize_email(value["userName"])
            if "externalId" in value and isinstance(value["externalId"], str):
                user.sso_provider_id = value["externalId"]
            if "name" in value and isinstance(value["name"], dict):
                name = value["name"]
                user.full_name = _extract_full_name(SCIMName(**name), new_email or user.email)
        else:
            # Silently ignore unsupported ops — SCIM lets the server do this,
            # and IdPs sometimes push ops we don't model (phoneNumbers etc.)
            logger.debug("Ignoring unsupported SCIM PATCH op: %s %s", action, path)

    if new_email is not None and new_email != user.email:
        if await _email_taken(db, new_email, exclude_user_id=user.id):
            raise _scim_http_error(
                409, f"User with userName {new_email} already exists.", "uniqueness"
            )
        user.email = new_email

    await db.flush()
    # The UPDATE fires `updated_at`'s server-side `onupdate=func.now()`, which
    # SQLAlchemy then marks expired. Reload it here, inside the async context,
    # so building the SCIM response (which reads `updated_at` for meta.lastModified)
    # doesn't trip a sync lazy-load → MissingGreenlet → 500 on the deprovision path.
    await db.refresh(user)
    return _user_to_scim(user, request)


@router.delete("/Users/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID,
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    """Soft delete — SCIM DELETE deactivates rather than hard-deletes, so
    we preserve the audit trail."""
    result = await db.execute(
        select(User).where(User.id == user_id, User.organization_id == org.id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _scim_http_error(404, f"User {user_id} not found.")
    user.is_active = False
    await db.flush()


# ---------------------------------------------------------------------------
# Groups — IdP group → RBAC Role mapping (see services/scim_groups.py).
# Group state is JSONB on settings.sso; membership drives role reconciliation.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _emails_for(db: AsyncSession, org_id, ids) -> dict[str, str]:
    """Batch-resolve member id → email for SCIM `display`. Skips non-UUID ids."""
    uuids = []
    for i in ids:
        try:
            uuids.append(uuid.UUID(str(i)))
        except (ValueError, AttributeError):
            continue
    if not uuids:
        return {}
    rows = await db.execute(
        select(User.id, User.email).where(User.organization_id == org_id, User.id.in_(uuids))
    )
    return {str(uid): email for uid, email in rows.all()}


async def _valid_member_ids(db: AsyncSession, org_id, ids) -> list[str]:
    """Keep only ids that resolve to a real user in this org (order-preserving,
    deduped). Stops phantom ids from reaching role reconciliation / FK inserts."""
    emails = await _emails_for(db, org_id, ids)
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        sid = str(i)
        if sid in emails and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _group_to_scim(group_id: str, data: dict, request: Request, emails: dict) -> SCIMGroup:
    base = f"{str(request.base_url).rstrip('/')}{settings.scim_url_path}/Groups/{group_id}"
    members = [
        SCIMGroupMember(value=uid, display=emails.get(uid)) for uid in (data.get("members") or [])
    ]
    return SCIMGroup(
        id=group_id,
        displayName=data.get("displayName", ""),
        externalId=data.get("externalId"),
        members=members,
        meta=SCIMMeta(
            resourceType="Group",
            created=data.get("created"),
            lastModified=data.get("lastModified"),
            location=base,
        ),
    )


@router.get("/Groups", response_model=SCIMGroupListResponse)
async def list_groups(
    request: Request,
    startIndex: int = Query(1, ge=1),
    count: int = Query(100, ge=0, le=1000),
    filter: str | None = Query(None),
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    groups = scim_groups.get_groups(org.settings)
    items = list(groups.items())
    if filter:
        # Only `displayName eq "X"` is supported (what Okta/Entra send to
        # reconcile a group by name); anything else is a clear 400.
        m = re.fullmatch(r'displayName eq "(?P<name>[^"]*)"', filter.strip(), re.IGNORECASE)
        if not m:
            raise _scim_http_error(400, f"Unsupported filter: {filter}", "invalidFilter")
        wanted = m.group("name")
        items = [(gid, d) for gid, d in items if d.get("displayName") == wanted]

    total = len(items)
    page = items[startIndex - 1 : (startIndex - 1 + count) if count else None]
    all_member_ids = {uid for _, d in page for uid in (d.get("members") or [])}
    emails = await _emails_for(db, org.id, all_member_ids)
    return SCIMGroupListResponse(
        totalResults=total,
        startIndex=startIndex,
        itemsPerPage=len(page),
        Resources=[_group_to_scim(gid, d, request, emails) for gid, d in page],
    )


@router.get("/Groups/{group_id}", response_model=SCIMGroup)
async def get_group(
    group_id: str,
    request: Request,
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    data = scim_groups.get_groups(org.settings).get(group_id)
    if data is None:
        raise _scim_http_error(404, f"Group {group_id} not found.")
    emails = await _emails_for(db, org.id, data.get("members") or [])
    return _group_to_scim(group_id, data, request, emails)


@router.post("/Groups", response_model=SCIMGroup, status_code=201)
async def create_group(
    body: SCIMGroupCreate,
    request: Request,
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    await require_scim_entitlement(db, org)
    # Serialise with every other settings writer (`tenant.lock_organization`).
    org = await lock_organization(db, org)
    groups = scim_groups.get_groups(org.settings)
    # Idempotency: a group with the same displayName already exists → 409.
    if any(d.get("displayName") == body.displayName for d in groups.values()):
        raise _scim_http_error(
            409, f"Group with displayName {body.displayName} already exists.", "uniqueness"
        )
    members = await _valid_member_ids(db, org.id, [m.value for m in body.members])
    group_id = str(uuid.uuid4())
    now = _now_iso()
    groups[group_id] = {
        "displayName": body.displayName,
        "externalId": body.externalId,
        "members": members,
        "created": now,
        "lastModified": now,
    }
    scim_groups.write_groups(org, groups)
    await db.flush()
    await scim_groups.reconcile_members(db, org, set(members))
    logger.info("SCIM created group %s in org %s", body.displayName, org.slug)
    emails = await _emails_for(db, org.id, members)
    return _group_to_scim(group_id, groups[group_id], request, emails)


@router.put("/Groups/{group_id}", response_model=SCIMGroup)
async def replace_group(
    group_id: str,
    body: SCIMGroupCreate,
    request: Request,
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    await require_scim_entitlement(db, org)
    # Serialise with every other settings writer (`tenant.lock_organization`).
    org = await lock_organization(db, org)
    groups = scim_groups.get_groups(org.settings)
    data = groups.get(group_id)
    if data is None:
        raise _scim_http_error(404, f"Group {group_id} not found.")
    old_members = set(data.get("members") or [])
    new_members = await _valid_member_ids(db, org.id, [m.value for m in body.members])
    data = {
        **data,
        "displayName": body.displayName,
        "externalId": body.externalId,
        "members": new_members,
        "lastModified": _now_iso(),
    }
    groups[group_id] = data
    scim_groups.write_groups(org, groups)
    await db.flush()
    await scim_groups.reconcile_members(db, org, old_members | set(new_members))
    emails = await _emails_for(db, org.id, new_members)
    return _group_to_scim(group_id, data, request, emails)


@router.patch("/Groups/{group_id}", response_model=SCIMGroup)
async def patch_group(
    group_id: str,
    body: SCIMPatchRequest,
    request: Request,
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    await require_scim_entitlement(db, org)
    # Serialise with every other settings writer (`tenant.lock_organization`).
    org = await lock_organization(db, org)
    groups = scim_groups.get_groups(org.settings)
    data = groups.get(group_id)
    if data is None:
        raise _scim_http_error(404, f"Group {group_id} not found.")
    old_members = list(data.get("members") or [])
    try:
        updated = scim_groups.apply_patch_ops(data, body.Operations)
    except scim_groups.GroupPatchError as exc:
        raise _scim_http_error(400, str(exc), "invalidPath") from exc

    members = await _valid_member_ids(db, org.id, updated["members"])
    data = {**updated, "members": members, "lastModified": _now_iso()}
    groups[group_id] = data
    scim_groups.write_groups(org, groups)
    await db.flush()
    await scim_groups.reconcile_members(db, org, set(old_members) | set(members))
    emails = await _emails_for(db, org.id, members)
    return _group_to_scim(group_id, data, request, emails)


@router.delete("/Groups/{group_id}", status_code=204)
async def delete_group(
    group_id: str,
    org: Organization = Depends(get_scim_tenant),
    db: AsyncSession = Depends(get_control_db),
):
    # Not plan-gated: deleting a group only REVOKES the roles it mapped, which
    # a downgraded tenant's IdP must still be able to do (see
    # `require_scim_entitlement`).
    # Serialise with every other settings writer (`tenant.lock_organization`).
    org = await lock_organization(db, org)
    groups = scim_groups.get_groups(org.settings)
    data = groups.pop(group_id, None)
    if data is None:
        raise _scim_http_error(404, f"Group {group_id} not found.")
    former_members = set(data.get("members") or [])
    scim_groups.write_groups(org, groups)
    await db.flush()
    # The group is gone, so reconciliation revokes its mapped role from former
    # members (unless another group still grants it).
    await scim_groups.reconcile_members(db, org, former_members)
    logger.info("SCIM deleted group %s in org %s", data.get("displayName"), org.slug)


@router.get("/ServiceProviderConfig")
async def service_provider_config():
    """Minimal discovery doc that Okta/Entra probe to learn what we support."""
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "OAuth Bearer Token",
                "description": "Per-tenant bearer token configured in Organization settings.",
            }
        ],
    }


@router.get("/Schemas/{schema_id}")
async def schemas(schema_id: str):
    """Entra probes this. Return the core User schema — enough for both IdPs
    to stop complaining and proceed with user sync."""
    if schema_id != USER_SCHEMA:
        raise _scim_http_error(404, f"Schema {schema_id} not known.")
    return {
        "id": USER_SCHEMA,
        "name": "User",
        "description": "User Account",
        "attributes": [
            {"name": "userName", "type": "string", "required": True, "uniqueness": "server"},
            {"name": "externalId", "type": "string"},
            {"name": "active", "type": "boolean"},
            {"name": "name", "type": "complex"},
            {"name": "emails", "type": "complex", "multiValued": True},
        ],
    }
