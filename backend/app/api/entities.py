"""Entity (legal entity / subsidiary) management endpoints.

Multi-entity Phase 1: admin CRUD over the tenant-local ``entities`` table.
Reads are open to any authenticated user (the entity selector needs the list);
mutations are admin-only. The default entity can't be renamed away from its
role or deactivated, and entities with rows can't be deleted — deactivate
instead. See ``docs/multi-entity.md``.
"""

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    get_current_user,
    get_org_id,
    require_entitlement,
    require_roles,
)
from app.models.entity import Entity
from app.models.user import User
from app.schemas.entity import EntityCreate, EntityUpdate
from app.services.audit_dispatch import dispatch_audit
from app.services.billing.plan_catalog import FEATURE_MULTI_ENTITY
from app.tenant import get_tenant_db

router = APIRouter(prefix="/entities", tags=["entities"])

# Lowercase alphanumeric + internal hyphens; no leading/trailing hyphen.
_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _serialize(e: Entity) -> dict:
    return {
        "id": str(e.id),
        "name": e.name,
        "slug": e.slug,
        "currency": e.currency,
        "is_default": e.is_default,
        "is_active": e.is_active,
    }


async def _get_or_404(db: AsyncSession, entity_id: uuid.UUID) -> Entity:
    entity = (await db.execute(select(Entity).where(Entity.id == entity_id))).scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


@router.get("")
async def list_entities(
    active_only: bool = False,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(get_current_user),
):
    query = select(Entity)
    if active_only:
        query = query.where(Entity.is_active)
    # Default first, then alphabetical — matches the selector's preferred order.
    query = query.order_by(Entity.is_default.desc(), Entity.name)
    result = await db.execute(query)
    return [_serialize(e) for e in result.scalars().all()]


@router.post("", status_code=201)
async def create_entity(
    body: EntityCreate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    # Every tenant is provisioned with its one default entity, so any entity
    # this route creates is a second one — a Scale feature (decisions §253).
    # Only CREATION is gated (§258): entities a downgraded tenant already has
    # keep working — read, scope, rename, deactivate, reactivate, set-default —
    # because the money rows scoped to them must stay reachable.
    _entitled: User = Depends(require_entitlement(FEATURE_MULTI_ENTITY)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    slug = body.slug.strip().lower()
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail="Slug must be lowercase alphanumeric with hyphens (e.g. 'us-inc').",
        )
    exists = (await db.execute(select(Entity).where(Entity.slug == slug))).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail="An entity with that slug already exists.")

    entity = Entity(
        organization_id=org_id,
        name=body.name.strip(),
        slug=slug,
        currency=(body.currency.upper() if body.currency else None),
        is_default=False,
        is_active=True,
    )
    db.add(entity)
    await db.flush()

    # An entity is the scope key every entity-scoped money query is filtered
    # by, so minting one is a config change that belongs on the append-only
    # trail. PII-free: name / slug / currency are org config, never personal
    # data.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="entity.created",
        entity_type="entity",
        entity_id=entity.id,
        details={"name": entity.name, "slug": entity.slug, "currency": entity.currency},
    )
    return _serialize(entity)


@router.patch("/{entity_id}")
async def update_entity(
    entity_id: uuid.UUID,
    body: EntityUpdate,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    entity = await _get_or_404(db, entity_id)
    data = body.model_dump(exclude_unset=True)

    if "is_active" in data and data["is_active"] is False and entity.is_default:
        # The default entity is where un-scoped / new rows land — it must stay
        # active so the tenant always has a valid home entity.
        raise HTTPException(status_code=400, detail="The default entity cannot be deactivated.")

    changed: dict[str, object] = {}
    if "name" in data and data["name"] is not None:
        new_name = data["name"].strip()
        if new_name != entity.name:
            changed["name"] = new_name
        entity.name = new_name
    if "currency" in data:
        new_currency = data["currency"].upper() if data["currency"] else None
        if new_currency != entity.currency:
            changed["currency"] = new_currency
        entity.currency = new_currency
    if "is_active" in data and data["is_active"] is not None:
        if data["is_active"] != entity.is_active:
            changed["is_active"] = data["is_active"]
        entity.is_active = data["is_active"]

    await db.flush()
    if changed:
        # Deactivating an entity changes what every scoped query can see —
        # audited like any other config mutation. New values only, PII-free.
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org_id,
            actor_id=user.id,
            action="entity.updated",
            entity_type="entity",
            entity_id=entity.id,
            details={"slug": entity.slug, "changed": changed},
        )
    return _serialize(entity)


@router.post("/{entity_id}/set-default")
async def set_default_entity(
    entity_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Make ``entity_id`` the tenant's default entity.

    Provisioning creates exactly one default (the home for un-scoped / new
    rows) and, until this endpoint, nothing could ever change which one that
    was — the first entity was permanently stuck as default. Exactly one
    entity must be the default at all times (``uq_entities_one_default``, a
    partial unique index), so the old default is unset and the new one set in
    the SAME transaction.

    Both candidate rows are fetched with ``FOR UPDATE`` in a single query,
    ordered by ``id`` — a single statement rather than two sequential locks,
    so two concurrent ``set-default`` calls acquire row locks in the same
    (id-sorted) order and can't deadlock each other, and neither can observe
    a moment with zero or two defaults.
    """
    rows = (
        (
            await db.execute(
                select(Entity)
                .where(or_(Entity.id == entity_id, Entity.is_default.is_(True)))
                .order_by(Entity.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    target = next((e for e in rows if e.id == entity_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    if not target.is_active:
        raise HTTPException(status_code=400, detail="Cannot make an inactive entity the default.")

    if target.is_default:
        return _serialize(target)  # already the default — idempotent no-op

    current_default = next((e for e in rows if e.is_default and e.id != target.id), None)
    previous_default_id = current_default.id if current_default is not None else None
    if current_default is not None:
        # Unset THEN set — both must be flushed in this order, or the second
        # UPDATE trips uq_entities_one_default before the first one's UPDATE
        # has cleared it.
        current_default.is_default = False
        await db.flush()

    target.is_default = True
    await db.flush()

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="entity.default_changed",
        entity_type="entity",
        entity_id=target.id,
        details={
            "slug": target.slug,
            "previous_default_id": str(previous_default_id) if previous_default_id else None,
        },
    )
    return _serialize(target)
