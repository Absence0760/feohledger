"""Tenant resolution — maps X-Tenant-Slug header to a per-tenant DB session.

Also hosts the multi-entity (Phase 2) request scoping primitives:
``get_entity_id`` resolves the ``X-Entity-ID`` header to a validated
subsidiary (or ``None`` for the consolidated "all entities" view),
``get_write_entity_id`` picks the entity a newly-created row lands under, and
``apply_entity_scope`` filters a ``select()`` to one entity. Entities subdivide
data *within* a tenant DB — they are NOT the tenant boundary itself (that's
still the per-org DB resolved below). See ``docs/multi-entity.md``.
"""

import uuid
from collections.abc import AsyncGenerator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select

from app.database import commit_before_response, get_control_db, get_tenant_engine
from app.models.entity import Entity
from app.models.organization import Organization

# Sentinel the entity switcher sends to ask for the consolidated view across
# every entity. An absent header means the same thing (so API consumers that
# predate multi-entity keep seeing all rows). Both resolve to ``None``.
ALL_ENTITIES = "all"


def normalize_custom_domain(host: str | None) -> str | None:
    """Reduce a raw ``Host`` header to a bare, lowercase hostname.

    Strips any ``:port`` suffix and surrounding whitespace, lowercases
    (hostnames are case-insensitive), and rejects empty / obviously
    non-host values. Returns ``None`` when there's nothing usable to
    match — the caller then falls back to the ``X-Tenant-Slug`` path.
    """
    if not host:
        return None
    h = host.strip().lower()
    # Drop a :port suffix. IPv6 literals (``[::1]``) carry colons inside
    # brackets — never a configured custom domain, so bail on them.
    if h.startswith("["):
        return None
    h = h.split(":", 1)[0]
    if not h or "/" in h or " " in h:
        return None
    return h


async def resolve_tenant_slug_by_custom_domain(db: AsyncSession, host: str | None) -> str | None:
    """Map a request ``Host`` to a tenant slug via the per-org custom-domain list.

    A tenant may register vanity hostnames (e.g. ``ap.acmecorp.com``) under
    ``Organization.settings.brand.custom_domains`` (a JSON array of bare
    hostnames). When a request arrives on one of those hosts, this maps it back
    to the owning org's slug so the existing ``get_tenant`` flow can resolve it.

    This is purely a *candidate* resolution: the slug it returns still goes
    through ``get_tenant``'s JWT ``org``-claim cross-check, so a forged ``Host``
    header alone can never widen access. An unknown / unmatched host returns
    ``None`` (caller falls back to the header path); the lookup never raises.
    """
    normalized = normalize_custom_domain(host)
    if normalized is None:
        return None
    # JSONB containment: find the org whose brand.custom_domains array holds
    # this exact host. Domains are stored normalized (lowercase, no port) so an
    # exact-element match is correct and uses the GIN-indexable @> operator on
    # the whole `settings` column (``settings @> '{"brand":{"custom_domains":["x"]}}'``).
    # ORDER BY is load-bearing, not cosmetic: without it Postgres may return the
    # matching rows in any order, so a host registered to two orgs (a duplicate
    # that predates the uniqueness guard, or one written straight into the JSONB)
    # would resolve to a DIFFERENT tenant from one request to the next. The
    # oldest claimant wins, deterministically, with the id as the final tiebreak
    # for two rows created in the same instant.
    stmt = (
        select(Organization.slug)
        .where(Organization.settings.contains({"brand": {"custom_domains": [normalized]}}))
        .order_by(Organization.created_at.asc(), Organization.id.asc())
    )
    try:
        result = await db.execute(stmt)
    except Exception:
        # A malformed settings blob (e.g. custom_domains not an array) must not
        # 500 tenant resolution — fall back to the header path.
        return None
    slug = result.scalars().first()
    return slug


async def get_tenant_slug(
    x_tenant_slug: str | None = Header(default=None),
    host: str | None = Header(default=None),
    db: AsyncSession = Depends(get_control_db),
) -> str:
    """Resolve the tenant slug for the request.

    Primary path: the ``X-Tenant-Slug`` header set by the SPA from the
    subdomain. Fallback (white-label custom domains): when the header is
    absent, the request ``Host`` is matched against the per-org
    ``settings.brand.custom_domains`` list — so a tenant served on a vanity
    hostname (``ap.acmecorp.com``) resolves without the SPA needing to know its
    slug.

    Either way the returned slug is only a *candidate*: ``get_tenant`` still
    cross-checks it against the JWT ``org`` claim, so neither a swapped header
    nor a forged ``Host`` widens access on its own.
    """
    if x_tenant_slug:
        return x_tenant_slug
    domain_slug = await resolve_tenant_slug_by_custom_domain(db, host)
    if domain_slug:
        return domain_slug
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Missing X-Tenant-Slug header",
    )


async def get_tenant(
    slug: str = Depends(get_tenant_slug),
    db: AsyncSession = Depends(get_control_db),
    authorization: str | None = Header(default=None),
) -> Organization:
    """Resolve the Organization for the requested tenant.

    Cross-tenant guard: if the caller presents an employee JWT (typ
    other than ``vendor``), the token's ``org`` claim must match the
    resolved tenant. Otherwise the tenant selector alone (the
    X-Tenant-Slug header, or a custom-domain Host that maps to a slug)
    decides which tenant's row the endpoint sees — letting any
    authenticated user from tenant A read or mutate tenant B's data by
    swapping the header (or pointing a forged Host at it).

    This cross-check is what makes the custom-domain fallback safe: a
    Host header resolves only a *candidate* tenant; the JWT org claim
    still has to match, so a leaked/forged Host alone can't widen
    access.

    Vendor-portal tokens are exempt: VendorUser rows live in the
    per-tenant DB, so a cross-tenant attempt fails naturally on the
    user-lookup query in ``get_current_vendor_user``. Unauthenticated
    requests are also exempt — the downstream auth dependency will
    reject them with 401 before any data is read.

    The check lives here (not in ``get_tenant_db``) so it covers
    every endpoint that pulls the Organization object directly, not
    only the ones that also open a per-tenant DB session.
    """
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown tenant: {slug}",
        )

    if authorization and authorization.startswith("Bearer "):
        # Local import avoids a circular dependency with `app.api.deps`,
        # which itself imports from `app.tenant`.
        from app.api.deps import decode_token

        try:
            payload = decode_token(authorization.removeprefix("Bearer "))
        except HTTPException:
            payload = None

        if payload is not None and payload.get("typ") != "vendor":
            token_org = payload.get("org")
            if token_org and str(org.id) != token_org:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Token does not match the requested tenant",
                )

    return org


async def lock_organization(db: AsyncSession, org: Organization) -> Organization:
    """Re-read ``org`` under ``SELECT … FOR UPDATE`` and refresh it in place.

    Every writer of ``Organization.settings`` reads the JSONB, changes one block
    and writes the WHOLE dict back. Two such writers interleaving means the one
    that commits second silently reverts the other — a SCIM group write undoing
    an SSO secret rotation, or the reverse. Call this first, on the request's own
    control-plane session (the one ``get_tenant`` / ``get_scim_tenant`` loaded
    ``org`` on), before reading ``org.settings``; the lock is held to commit.
    """
    return (
        await db.execute(
            select(Organization)
            .where(Organization.id == org.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def get_tenant_db(
    request: Request,
    tenant: Organization = Depends(get_tenant),
) -> AsyncGenerator[AsyncSession]:
    """Yield a SQLAlchemy session bound to the tenant's DB.

    The cross-tenant guard lives in ``get_tenant``; by the time we
    reach here, the JWT's org claim has already been validated
    against the requested tenant.
    """
    engine = get_tenant_engine(tenant.db_name)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        # Durability: commit before the response is sent, not from this
        # generator's post-yield teardown (which FastAPI runs after the client
        # already has its 201). See `commit_before_response` + decisions §20.
        commit_before_response(session, request)
        try:
            yield session
            # Backstop — see the matching comment in ``get_control_db``.
            if session.in_transaction():
                await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Multi-entity (Phase 2) — request-scoped entity selection
# ---------------------------------------------------------------------------


async def get_entity_id(
    x_entity_id: str | None = Header(default=None),
    db: AsyncSession = Depends(get_tenant_db),
) -> uuid.UUID | None:
    """Resolve the ``X-Entity-ID`` header to a validated entity, or ``None``.

    ``None`` (the consolidated "all entities" view) is returned when the header
    is absent — so a client that never learned about multi-entity keeps seeing
    every entity's rows — or when it is the literal ``all``.

    A UUID is validated against *this tenant's* ``entities`` table. The table is
    tenant-local, so an id that doesn't exist here can't widen the scope or
    point at another tenant's subsidiary: an unknown id is a 400, never a
    silent fall-through to "all". The dependency reuses the same tenant session
    as the endpoint (FastAPI caches ``get_tenant_db`` within a request), so this
    costs one indexed lookup.
    """
    if x_entity_id is None:
        return None
    raw = x_entity_id.strip()
    if raw.lower() == ALL_ENTITIES:
        return None
    try:
        entity_uuid = uuid.UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid X-Entity-ID header"
        )
    exists = (
        await db.execute(select(Entity.id).where(Entity.id == entity_uuid))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unknown entity for this tenant",
        )
    return entity_uuid


async def resolve_default_entity_id(db: AsyncSession) -> uuid.UUID:
    """Return the tenant's single ``is_default`` entity id.

    Every tenant is provisioned with exactly one default entity (migration
    0029 / ``tenant_provisioning``), so this is the home for rows created while
    the consolidated view is selected. A missing default means the tenant was
    provisioned incorrectly — surface it loudly rather than writing a NULL.
    """
    default_id = (await db.execute(select(Entity.id).where(Entity.is_default))).scalar_one_or_none()
    if default_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Tenant has no default entity",
        )
    return default_id


async def get_write_entity_id(
    entity_id: uuid.UUID | None = Depends(get_entity_id),
    db: AsyncSession = Depends(get_tenant_db),
) -> uuid.UUID:
    """The entity a newly-created row should belong to.

    When the caller has an entity selected, new rows land under it. When the
    consolidated view is active (``None``), they land under the tenant's
    default entity — never NULL, so a freshly-created invoice is always visible
    in some entity-scoped view (a NULL ``entity_id`` would vanish from every
    per-entity list while showing only in the consolidated one).

    ``GLAccount`` is the exception and does NOT use this — a NULL there means
    "shared chart across all entities", which is the intended default for new
    accounts; that endpoint assigns ``entity_id`` explicitly.

    **A deactivated entity refuses the write.** ``get_entity_id`` validates
    only that the id *exists*, which is right for reads — a retired
    subsidiary's history has to stay reachable, and narrowing a list to it is
    harmless. Filing a *new* row under it is not: ``/entities`` deactivates an
    entity precisely to stop it accumulating rows, and until this check existed
    a stale ``X-Entity-ID`` (a client that had it selected when an admin
    retired it, or one that never refreshed) kept doing exactly that. The
    refusal is a 409, not a 400 — the header is well-formed and names a real
    entity of this tenant; what changed is the entity's state, so the caller
    re-selects rather than fixing a malformed request.
    """
    if entity_id is None:
        return await resolve_default_entity_id(db)
    is_active = (
        await db.execute(select(Entity.is_active).where(Entity.id == entity_id))
    ).scalar_one_or_none()
    if is_active is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Selected entity is deactivated; new records cannot be filed under it",
        )
    return entity_id


def apply_entity_scope(
    query: Select,
    model: type,
    entity_id: uuid.UUID | None,
    *,
    include_shared: bool = False,
) -> Select:
    """Filter ``query`` to a single entity when one is selected.

    ``entity_id is None`` (consolidated view) returns the query untouched — the
    endpoint sees every entity's rows. Otherwise the query is narrowed to
    ``model.entity_id == entity_id``.

    ``include_shared=True`` also admits rows with a NULL ``entity_id``. Two
    callers want that, for different reasons:

    - the chart of accounts (``GLAccount``), where NULL is a *deliberate*
      "shared across every entity" marker, so an entity's effective chart is
      ``shared ∪ its own``;
    - vendor matching (``services/vendor_matching``), where a NULL is an
      *unstamped* row (pre-multi-entity, or created from an entity-less
      invoice) that must stay matchable from every entity — excluding it would
      silently duplicate the supplier rather than fail loudly.

    Every other table backfills to the default entity and never carries a
    meaningful NULL, so the default (``False``) is correct for them.
    """
    if entity_id is None:
        return query
    col = model.entity_id
    if include_shared:
        return query.where(or_(col == entity_id, col.is_(None)))
    return query.where(col == entity_id)


async def ensure_in_entity_scope(
    db: AsyncSession,
    model: type,
    row_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    *,
    detail: str,
    include_shared: bool = False,
) -> None:
    """404 with ``detail`` unless row ``row_id`` of ``model`` is visible under
    ``entity_id``; a no-op (no query) in the consolidated view.

    The by-id counterpart of :func:`apply_entity_scope`, for routes whose row
    is loaded somewhere the caller's ``X-Entity-ID`` can't reach — inside a
    service (``review.approve_invoice``, ``get_invoice_for_update``) or a
    helper shared with non-request callers. Call it at the top of the handler,
    after auth, before anything reads or writes the row.

    ``detail`` MUST be the route's own missing-row message: an out-of-scope id
    has to be byte-identical to a nonexistent one, or the response becomes an
    oracle that enumerates a sibling subsidiary's ids (the
    ``api/purchase_orders._get_scoped_po`` rule). Where a router already owns
    a scoped loader, scope that loader instead — this is for the rest.
    """
    if entity_id is None:
        return
    found = (
        await db.execute(
            apply_entity_scope(
                select(model.id).where(model.id == row_id),
                model,
                entity_id,
                include_shared=include_shared,
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
