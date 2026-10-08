"""Multi-entity Phase 2 — request-scope primitives.

Covers the building blocks in ``app.tenant``: ``get_entity_id`` header
resolution + validation, ``get_write_entity_id`` default-entity fallback,
``resolve_default_entity_id``, and the pure ``apply_entity_scope`` query
filter (including the GLAccount shared-chart carve-out).

Endpoint-level scoping (lists/aggregates honouring the header) lives in the
per-area suites; here we exercise the primitives directly so a regression in
the foundation surfaces in isolation.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.entity import Entity
from app.models.gl_account import GLAccount
from app.models.invoice import Invoice
from app.tenant import (
    apply_entity_scope,
    get_entity_id,
    get_write_entity_id,
    resolve_default_entity_id,
)


async def _default_entity_id(realdb, key: str = "a") -> uuid.UUID:
    mk = realdb.sessionmaker(key)
    async with mk() as s:
        return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _org_id(realdb, key: str = "a") -> uuid.UUID:
    """The tenant's own organization id, read off its default entity.

    ``entities.organization_id`` is NOT NULL, so a test that inserts a
    subsidiary has to carry the same org the harness provisioned.
    """
    mk = realdb.sessionmaker(key)
    async with mk() as s:
        return (
            await s.execute(select(Entity.organization_id).where(Entity.is_default))
        ).scalar_one()


# ---------------------------------------------------------------------------
# get_entity_id — header resolution + validation
# ---------------------------------------------------------------------------


async def test_get_entity_id_absent_is_consolidated(realdb):
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        assert await get_entity_id(None, s) is None


async def test_get_entity_id_literal_all_is_consolidated(realdb):
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        assert await get_entity_id("all", s) is None
        assert await get_entity_id("  ALL  ", s) is None  # trimmed + case-insensitive


async def test_get_entity_id_valid_entity_returns_uuid(realdb):
    default_id = await _default_entity_id(realdb)
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        assert await get_entity_id(str(default_id), s) == default_id


async def test_get_entity_id_malformed_is_400(realdb):
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        with pytest.raises(HTTPException) as exc:
            await get_entity_id("not-a-uuid", s)
    assert exc.value.status_code == 400


async def test_get_entity_id_unknown_uuid_is_400_not_silent_all(realdb):
    """An id that doesn't exist in this tenant must 400 — never fall through to
    the consolidated view, which would silently widen the scope."""
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        with pytest.raises(HTTPException) as exc:
            await get_entity_id(str(uuid.uuid4()), s)
    assert exc.value.status_code == 400


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_get_entity_id_rejects_other_tenants_entity(realdb):
    """Tenant B's entity id is unknown to tenant A → 400, not a cross-tenant
    read. The entities table is tenant-local, so this falls out naturally."""
    # Create an entity in tenant B.
    async with realdb.client(key="b", role="admin") as c:
        created = await c.post("/api/entities", json={"name": "B Co", "slug": "b-co"})
    b_entity_id = created.json()["id"]

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        with pytest.raises(HTTPException) as exc:
            await get_entity_id(b_entity_id, s)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# resolve_default_entity_id / get_write_entity_id
# ---------------------------------------------------------------------------


async def test_resolve_default_entity_id(realdb):
    default_id = await _default_entity_id(realdb)
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        assert await resolve_default_entity_id(s) == default_id


async def test_get_write_entity_id_falls_back_to_default(realdb):
    """Consolidated view (None) → new rows land under the default entity, never
    NULL."""
    default_id = await _default_entity_id(realdb)
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        assert await get_write_entity_id(None, s) == default_id


async def test_get_write_entity_id_honours_selected(realdb):
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        chosen = uuid.uuid4()
        # When an entity is selected the value passes through untouched (it was
        # already validated by get_entity_id upstream).
        assert await get_write_entity_id(chosen, s) == chosen


async def test_get_write_entity_id_refuses_a_deactivated_entity(realdb):
    """A retired subsidiary must stop *accumulating* rows.

    ``/entities`` deactivates an entity precisely so nothing new is filed under
    it, but ``get_entity_id`` validates only that the id EXISTS — so a stale
    ``X-Entity-ID`` (a client that had it selected when an admin retired it)
    kept writing there. Reads are deliberately unaffected; see the sibling test.
    """
    org_id = await _org_id(realdb)
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        retired = Entity(
            organization_id=org_id,
            name="Retired Sub",
            slug=f"retired-{uuid.uuid4().hex[:8]}",
            is_active=False,
        )
        s.add(retired)
        await s.commit()
        with pytest.raises(HTTPException) as exc:
            await get_write_entity_id(retired.id, s)
        assert exc.value.status_code == 409
        # The refusal names the state, never the entity's own name.
        assert "deactivated" in exc.value.detail.lower()
        assert "Retired Sub" not in exc.value.detail


async def test_get_entity_id_still_resolves_a_deactivated_entity_for_reads(realdb):
    """Deactivating an entity must not make its history unreachable.

    The write path refuses (above); the READ path keeps resolving, so a
    retired subsidiary's invoices, payments and audit trail can still be
    listed and exported — which is the whole reason a deactivation is not a
    delete.
    """
    org_id = await _org_id(realdb)
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        retired = Entity(
            organization_id=org_id,
            name="Retired Sub",
            slug=f"retired-{uuid.uuid4().hex[:8]}",
            is_active=False,
        )
        s.add(retired)
        await s.commit()
        assert await get_entity_id(str(retired.id), s) == retired.id


async def test_get_write_entity_id_allows_an_active_entity(realdb):
    """The guard must bite on `is_active is False` only — not on truthiness.

    Without this the refusal could not be told apart from one that also
    rejected the ordinary case, and the fallback path below would mask it.
    """
    org_id = await _org_id(realdb)
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        live = Entity(
            organization_id=org_id,
            name="Live Sub",
            slug=f"live-{uuid.uuid4().hex[:8]}",
            is_active=True,
        )
        s.add(live)
        await s.commit()
        assert await get_write_entity_id(live.id, s) == live.id


# ---------------------------------------------------------------------------
# apply_entity_scope — pure query filter
# ---------------------------------------------------------------------------


def _compiled(query) -> str:
    return str(query.compile(compile_kwargs={"literal_binds": False}))


def test_apply_entity_scope_none_is_passthrough():
    base = select(Invoice)
    scoped = apply_entity_scope(base, Invoice, None)
    assert _compiled(scoped) == _compiled(base)  # untouched


def test_apply_entity_scope_filters_by_entity():
    eid = uuid.uuid4()
    scoped = apply_entity_scope(select(Invoice), Invoice, eid)
    sql = _compiled(scoped)
    assert "invoices.entity_id =" in sql
    assert "IS NULL" not in sql  # no shared carve-out for ordinary tables


def test_apply_entity_scope_include_shared_admits_null():
    """GLAccount: a scoped chart is shared (NULL) ∪ the entity's own."""
    eid = uuid.uuid4()
    scoped = apply_entity_scope(select(GLAccount), GLAccount, eid, include_shared=True)
    sql = _compiled(scoped)
    assert "gl_accounts.entity_id =" in sql
    assert "gl_accounts.entity_id IS NULL" in sql
    assert " OR " in sql


def test_apply_entity_scope_include_shared_passthrough_when_none():
    base = select(GLAccount)
    scoped = apply_entity_scope(base, GLAccount, None, include_shared=True)
    assert _compiled(scoped) == _compiled(base)
