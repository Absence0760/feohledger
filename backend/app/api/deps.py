"""Shared FastAPI dependencies for auth, tenant context, and DB sessions."""

import logging
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Header, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_control_db, get_tenant_engine
from app.models.api_key import ApiKey
from app.models.organization import Organization
from app.models.user import User
from app.redis import is_token_blocked

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"

# JWT `typ` values that are NOT full employee access tokens and therefore must
# never resolve through `get_current_user`. These are same-secret JWTs minted by
# other flows (the supplier-portal access token, and the post-password MFA
# challenge tokens for both employees and the portal) — accepting any of them as
# an access token would bypass the second factor or cross the AP/vendor boundary.
# Mirror of the `typ` strings in `app.services.mfa` + `create_vendor_access_token`;
# duplicated as bare strings here to avoid a circular import (mfa imports nothing
# from deps, but keeping deps import-light matters for the Lambda paths). A real
# access token is `typ="user"`; `typ` absent is legacy-access and still allowed.
_NON_ACCESS_TOKEN_TYPES = frozenset({"vendor", "mfa_challenge", "vendor_mfa_challenge"})

# Role constants — mirror the four roles seeded into `roles` table by
# `scripts/seed.py`. Centralised here so a typo doesn't silently lock
# everyone out (`require_roles("admni")` would be a 403 for everyone).
ROLE_ADMIN = "admin"
ROLE_AP_MANAGER = "ap_manager"
ROLE_AP_CLERK = "ap_clerk"
ROLE_CFO = "cfo"

ALL_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)


def create_access_token(user_id: uuid.UUID, org_id: uuid.UUID) -> str:
    token, _jti = create_access_token_with_jti(user_id, org_id)
    return token


def create_access_token_with_jti(user_id: uuid.UUID, org_id: uuid.UUID) -> tuple[str, str]:
    """Mint an access token and return both the encoded JWT and the JTI.

    Callers that need to track the session in Redis (login, MFA verify, SSO
    callback) use this variant so they can register the JTI without having
    to re-decode the token.
    """
    jti = str(uuid.uuid4())
    expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "org": str(org_id),
        "typ": "user",
        "jti": jti,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM), jti


def create_vendor_access_token(vendor_user_id: uuid.UUID, vendor_id: uuid.UUID) -> str:
    """Mint a supplier-portal JWT. `typ=vendor` is the gate that stops a vendor
    JWT from resolving through `get_current_user` (and vice versa)."""
    jti = str(uuid.uuid4())
    expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(vendor_user_id),
        "ven": str(vendor_id),
        "typ": "vendor",
        "jti": jti,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode a JWT and return the payload. Raises on invalid tokens."""
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


async def get_current_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_control_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    token = authorization.removeprefix("Bearer ")
    payload = decode_token(token)

    # Only a full access token may resolve to an employee session. Rejecting
    # just `typ="vendor"` is NOT enough: the MFA *challenge* tokens
    # (`typ="mfa_challenge"` for employees, `typ="vendor_mfa_challenge"` for the
    # portal) are same-secret JWTs that also carry `sub`, so a bare not-vendor
    # check let a password-verified-but-MFA-pending user wield their challenge
    # token as a fully-authenticated access token — a complete second-factor
    # bypass. Reject every NON-access token type here. `typ` absent is still
    # accepted (legacy access tokens predate the claim); a real access token is
    # `typ="user"`. The portal side uses the symmetric allowlist
    # (`get_current_vendor_user` requires `typ == "vendor"`).
    if payload.get("typ") in _NON_ACCESS_TOKEN_TYPES:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # Check blocklist
    jti = payload.get("jti")
    if jti and await is_token_blocked(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked"
        )

    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.roles))
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # Compute the user's effective granular permissions once, here, off the
    # eager-loaded roles (system roles via the static default map, custom roles
    # via their stored list). Stash the frozenset on a transient attribute so
    # `require_permission` and the `/auth/me` serializer read it without a
    # re-query. Local import avoids a module-load cycle (permissions.py imports
    # the ROLE_* constants from this module).
    from app.api.permissions import effective_permissions

    user.effective_permissions = effective_permissions(user.roles)
    # The JTI of the token that authenticated THIS request, stashed the same
    # transient way. The session endpoints need it to mark the caller's own
    # entry as current and to exclude it from "sign out everywhere else" —
    # re-decoding the header inside those handlers would duplicate the
    # blocklist/type checks already done above. `None` for a legacy token
    # minted before the claim existed.
    user.session_jti = jti
    return user


def get_org_id(user: User = Depends(get_current_user)) -> uuid.UUID:
    return user.organization_id


def require_roles(*allowed: str):
    """Dependency factory — restricts an endpoint to users holding ANY of the
    given roles. 403 on miss.

    Usage::

        @router.post("/users")
        async def create_user(
            body: CreateUserRequest,
            user: User = Depends(require_roles(ROLE_ADMIN)),
        ):
            ...

    The check is "any-of," not "all-of." A user with multiple roles passes if
    they hold at least one of the listed ones — matches the way the frontend
    role gates work (`hasAnyRole`). For tighter checks, compose two
    dependencies or call `user.roles` directly inside the handler.

    Misses are logged at WARNING with the request method/path, the actor, and
    the role mismatch. They're attack-shaped events — flooding the log on 403
    is intentional so monitoring picks up brute-force probing.
    """
    if not allowed:
        raise ValueError("require_roles() needs at least one role")
    unknown = set(allowed) - set(ALL_ROLES)
    if unknown:
        # Catch typos at import time, not at request time.
        raise ValueError(f"Unknown role(s) in require_roles: {sorted(unknown)}")

    allowed_set = frozenset(allowed)

    async def checker(
        request: Request,
        user: User = Depends(get_current_user),
    ) -> User:
        held = {r.name for r in user.roles}
        if held & allowed_set:
            return user
        logger.warning(
            "RBAC denied: user=%s org=%s roles=%s required_any=%s %s %s",
            user.id,
            user.organization_id,
            sorted(held),
            sorted(allowed_set),
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role does not permit this action.",
        )

    return checker


def require_permission(*needed: str):
    """Dependency factory — restrict an endpoint to users holding ANY of the
    given granular permissions. 403 on miss.

    This is the segregation-of-duties counterpart to ``require_roles``: it gates
    on the user's *effective permissions* (the union over their roles — system
    roles via the static default map, custom roles via their stored list,
    computed in ``get_current_user``) rather than on a role NAME. It lets an org
    SPLIT fraud-sensitive duties that share one system role today (e.g. approving
    a vendor bank-detail change vs. executing a payment run, both ``ap_manager``).

    Backward-compatible: ``ROLE_DEFAULT_PERMISSIONS`` reproduces the current RBAC
    matrix exactly, so on a route migrated from ``require_roles(...)`` to
    ``require_permission(...)`` the four system roles behave identically — only a
    deliberately-configured custom role changes the outcome. The single
    deliberate exception is ``POST /api/cards/{id}/cancel``, where the prior role
    gate was WIDER than the ``payment.void`` gate on the void it duplicates; that
    narrowing is the fix, and it is documented on the route and in
    ``app/api/permissions.py``.

    "Any-of," like ``require_roles``. Permission names are validated against the
    catalog at import time so a typo is a startup ``ValueError``, not a silent
    always-deny. Misses log at WARNING (attack-shaped events) — PII-free.
    """
    from app.api.permissions import ALL_PERMISSIONS

    if not needed:
        raise ValueError("require_permission() needs at least one permission")
    unknown = set(needed) - set(ALL_PERMISSIONS)
    if unknown:
        raise ValueError(f"Unknown permission(s) in require_permission: {sorted(unknown)}")

    needed_set = frozenset(needed)

    async def checker(
        request: Request,
        user: User = Depends(get_current_user),
    ) -> User:
        held = getattr(user, "effective_permissions", frozenset())
        if held & needed_set:
            return user
        logger.warning(
            "RBAC denied (permission): user=%s org=%s perms=%s required_any=%s %s %s",
            user.id,
            user.organization_id,
            sorted(held),
            sorted(needed_set),
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your role does not permit this action.",
        )

    return checker


# ---------------------------------------------------------------------------
# Programmatic API-key authentication (public /api/v1 surface).
#
# Separate from the JWT Bearer path above: a key authenticates an *org's*
# machine-to-machine access via the `X-API-Key` header, resolves to its
# organization, and the org resolves to its tenant DB via the existing tenant
# chokepoint (`get_tenant_engine(org.db_name)`). There is no X-Tenant-Slug on
# this surface — the key IS the tenant boundary. See `backend/docs/public-api.md`.
# ---------------------------------------------------------------------------

# Single non-enumerating 401 for every api-key failure (missing header,
# unknown prefix, bad digest, revoked key). Distinct messages would let a
# caller probe which keys/prefixes exist.
_API_KEY_401 = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid API key",
    headers={"WWW-Authenticate": "ApiKey"},
)


@dataclass(frozen=True)
class ApiKeyPrincipal:
    """The resolved identity behind a validated `X-API-Key` request."""

    api_key_id: uuid.UUID
    organization_id: uuid.UUID
    db_name: str
    scopes: tuple[str, ...]


async def _record_api_key_usage(
    db: AsyncSession,
    *,
    api_key_id: uuid.UUID,
    org_id: uuid.UUID,
    at: datetime,
) -> None:
    """Increment the per-key, per-day request counter (best-effort meter).

    Single ``INSERT … ON CONFLICT (api_key_id, usage_date) DO UPDATE`` against
    the ``api_key_usage`` aggregate so a busy key is one cheap upsert per request
    (no per-request log rows, no read-modify-write race). The caller runs this
    inside the same best-effort try/except as the ``last_used_at`` stamp and
    owns the commit/rollback — a metering failure never breaks the auth path.
    """
    # Local import: the upsert dialect helper + model stay out of deps.py's
    # module import graph for every consumer (mirrors the api_keys import above).
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.api_key import ApiKeyUsage

    usage_date = at.astimezone(UTC).date()
    stmt = pg_insert(ApiKeyUsage).values(
        id=uuid.uuid4(),
        api_key_id=api_key_id,
        organization_id=org_id,
        usage_date=usage_date,
        request_count=1,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ApiKeyUsage.api_key_id, ApiKeyUsage.usage_date],
        set_={
            "request_count": ApiKeyUsage.request_count + 1,
            "updated_at": at,
        },
    )
    await db.execute(stmt)


async def get_api_key_principal(
    x_api_key: str | None = Header(default=None),
    db: AsyncSession = Depends(get_control_db),
) -> ApiKeyPrincipal:
    """Authenticate a programmatic request from the `X-API-Key` header.

    Resolves prefix → row by indexed prefix → constant-time sha256 compare →
    reject if missing / revoked. On success records `last_used_at` best-effort.
    Every failure raises the same opaque 401 so the response can't enumerate
    keys or tenants. The org's tenant DB is resolved (and a session opened) by
    `get_api_key_db` below, which depends on this.
    """
    # Local import keeps the token primitives out of the module import graph
    # of every consumer of deps.py.
    from app.services.api_keys import (
        constant_time_equals,
        hash_api_key,
        key_prefix_of,
    )

    # Platform kill switch — when off, the surface fails closed with the same
    # opaque 401 (never a distinct "feature disabled" that would confirm the
    # endpoint exists).
    if not settings.public_api_enabled:
        raise _API_KEY_401
    if not x_api_key:
        raise _API_KEY_401
    presented = x_api_key.strip()
    if not presented:
        raise _API_KEY_401

    prefix = key_prefix_of(presented)
    result = await db.execute(select(ApiKey).where(ApiKey.key_prefix == prefix))
    candidates = result.scalars().all()

    presented_hash = hash_api_key(presented)
    matched: ApiKey | None = None
    for candidate in candidates:
        # Constant-time digest compare — never short-circuit on the first
        # differing byte. Revocation is checked after a hash match so a revoked
        # key and a wrong key are indistinguishable from the outside.
        if constant_time_equals(candidate.key_hash, presented_hash):
            matched = candidate
            break
    if matched is None or matched.revoked_at is not None:
        raise _API_KEY_401

    org = await db.get(Organization, matched.organization_id)
    if org is None:
        raise _API_KEY_401

    # Best-effort last-used stamp + per-day usage meter on the SAME
    # request-scoped control session. A separate session (own connection) would
    # run a 2nd concurrent operation on the control engine's pooled asyncpg
    # connection — asyncpg forbids that ("another operation is in progress") and
    # it poisons the connection for the rest of the request. Sequential reuse of
    # `db` is safe. METERING IS BEST-EFFORT: a failure here must never break an
    # otherwise valid authenticated request, so it's swallowed (PII-free log).
    now = datetime.now(UTC)
    try:
        await db.execute(update(ApiKey).where(ApiKey.id == matched.id).values(last_used_at=now))
        await _record_api_key_usage(db, api_key_id=matched.id, org_id=org.id, at=now)
        await db.commit()
    except Exception as exc:  # pragma: no cover - observability, not auth
        # PII-free: only the key id (a UUID, never the plaintext/hash) + error.
        logger.warning(
            "api-key usage/last_used write failed: id=%s err=%s",
            matched.id,
            exc.__class__.__name__,
        )
        await db.rollback()

    # Per-key request cap on the whole /api/v1 surface. Enforced AFTER the key
    # authenticates (above), so an unauthenticated/garbage key already returned
    # the opaque 401 and never reaches here — a 429 only ever confirms a *valid*
    # key over its limit, never that a key exists. Keyed on the API key id, so
    # one key flooding the API can't 429 another org's key (per-key, not per-IP,
    # not per-org). A `RateLimitExceeded` (429 + Retry-After) is allowed to
    # propagate; any OTHER failure (e.g. Redis unreachable) FAILS OPEN — a Redis
    # blip must not deny otherwise-valid authenticated API access. The window is
    # one minute; the limit is `FEOH_PUBLIC_API_RATE_LIMIT_PER_MINUTE`.
    from app.services.rate_limit import RateLimitExceeded, check_rate_limit

    try:
        await check_rate_limit(
            "public_api",
            limit=settings.public_api_rate_limit_per_minute,
            window_seconds=60,
            subject=str(matched.id),
        )
    except RateLimitExceeded:
        raise
    except Exception as exc:  # pragma: no cover - fail-open on a limiter outage
        logger.warning(
            "api-key rate-limit check failed open: id=%s err=%s",
            matched.id,
            exc.__class__.__name__,
        )

    return ApiKeyPrincipal(
        api_key_id=matched.id,
        organization_id=org.id,
        db_name=org.db_name,
        scopes=tuple(matched.scopes or ()),
    )


async def get_api_key_db(
    principal: ApiKeyPrincipal = Depends(get_api_key_principal),
) -> AsyncGenerator[AsyncSession]:
    """Yield a session bound to the API key's org tenant DB.

    Reuses the same `get_tenant_engine` chokepoint as the JWT path's
    `get_tenant_db`, so programmatic reads are tenant-isolated at the data
    layer — never a hand-rolled engine, never a hardcoded `feoh_<slug>` name.
    """
    engine = get_tenant_engine(principal.db_name)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def require_api_scope(scope: str):
    """Dependency factory — require a scope on the authenticating API key.

    This slice only mints `read` keys, but gating each route on the scope it
    needs means a future write surface inherits enforcement for free.
    """

    async def checker(
        principal: ApiKeyPrincipal = Depends(get_api_key_principal),
    ) -> ApiKeyPrincipal:
        if scope not in principal.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key lacks the required scope.",
            )
        return principal

    return checker


# ---------------------------------------------------------------------------
# Plan entitlement gating (platform billing).
#
# Composes WITH auth/RBAC, never replaces it — the entitlement dependencies
# below each depend on an already-authenticated identity (the JWT `User` or the
# API-key principal), so a route still needs `require_roles(...)` /
# `require_api_scope(...)` for who-can-call; entitlement answers does-your-plan-
# include-this. A plan that lacks the feature yields HTTP 402 Payment Required
# (the plan must be upgraded) — distinct from a 403 role denial.
# ---------------------------------------------------------------------------


#: The coded refusal every plan-feature gate answers with (decisions §253, §258).
#: ``params.feature`` is the ``plan_catalog.FEATURE_*`` key the plan lacks, so the
#: SPA can name the tier that grants it instead of showing a bare 402.
PLAN_FEATURE_REQUIRED = "plan_feature_required"


def plan_feature_refusal(feature: str) -> HTTPException:
    """The 402 a plan-feature gate raises. One builder, so no gate drifts."""
    from app.api.refusals import coded_refusal

    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=coded_refusal(
            PLAN_FEATURE_REQUIRED,
            "Your plan does not include this feature.",
            feature=feature,
        ),
    )


async def org_has_feature(db: AsyncSession, organization_id, feature: str) -> bool:
    """Does the org's live plan grant ``feature``? Fail-closed (no plan → no)."""
    # Local import keeps the billing service out of deps.py's import graph
    # for every consumer (mirrors the api_keys local import above).
    from app.services.billing.entitlements import get_entitlements, has_entitlement

    return has_entitlement(await get_entitlements(db, organization_id), feature)


async def ensure_entitlement(db: AsyncSession, organization_id, feature: str) -> None:
    """Raise the 402 unless the org's live plan grants ``feature``.

    The inline form of ``require_entitlement``, for a gate that depends on the
    request body or the stored config rather than on the route alone — turning
    SSO *on*, saving a *live* ERP adapter. ``db`` is the control-plane session.
    """
    if not await org_has_feature(db, organization_id, feature):
        raise plan_feature_refusal(feature)


async def ensure_live_erp_entitled(db: AsyncSession, organization_id, erp_config) -> None:
    """``FEATURE_ERP_INTEGRATIONS`` gate for an action that would reach an ERP.

    Only a LIVE adapter is gated. The ``mock`` ERP is the local-first default
    (guard rail 7) and stays usable on every plan, and an org with no ERP
    configured at all has nothing to push to — the route's own "not configured"
    answer is the right one there, not a 402.
    """
    from app.services.billing.plan_catalog import FEATURE_ERP_INTEGRATIONS
    from app.services.erp_adapters.dispatcher import erp_config_is_live

    if erp_config_is_live(erp_config):
        await ensure_entitlement(db, organization_id, FEATURE_ERP_INTEGRATIONS)


def require_entitlement(feature: str):
    """Dependency factory — require the org's active plan to grant ``feature``.

    For the JWT (SPA) surface. Reads the org's live subscription → plan
    entitlements from the control plane; 402 when the plan doesn't include the
    feature. Fail-closed: an org with no live subscription has no entitlements.
    Compose alongside ``require_roles(...)`` on the same route. Pass a
    ``plan_catalog.FEATURE_*`` constant, never a string literal.
    """

    async def checker(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_control_db),
    ) -> User:
        await ensure_entitlement(db, user.organization_id, feature)
        return user

    return checker


def require_api_entitlement(feature: str):
    """Dependency factory — entitlement gate for the API-key (``/api/v1``) surface.

    Same semantics as ``require_entitlement`` but keyed off the API-key
    principal's org. Compose alongside ``require_api_scope(...)``.
    """

    async def checker(
        principal: ApiKeyPrincipal = Depends(get_api_key_principal),
        db: AsyncSession = Depends(get_control_db),
    ) -> ApiKeyPrincipal:
        await ensure_entitlement(db, principal.organization_id, feature)
        return principal

    return checker
