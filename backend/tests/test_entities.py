"""Entity (legal entity / subsidiary) tests — multi-entity Phase 1.

Real-Postgres harness (`realdb`): the two test tenants each start every test
with exactly one Default entity (seeded by conftest after the per-test
TRUNCATE). Covers the `/api/entities` CRUD surface, the single-default and
unique-slug DB constraints, RBAC, and tenant isolation.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.entity import Entity

# Multi-entity is a plan-gated feature (docs/decisions.md §258) and every test
# here stands up a second entity, so the harness orgs run on Scale.
pytestmark = pytest.mark.plan("scale")

# ---------------------------------------------------------------------------
# Baseline: every tenant starts with one Default entity
# ---------------------------------------------------------------------------


async def test_default_entity_exists_per_tenant(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get("/api/entities")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["is_default"] is True
    assert rows[0]["slug"] == "default"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_entity_happy_path(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/entities", json={"name": "US Inc", "slug": "us", "currency": "usd"}
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "us"
    assert body["currency"] == "USD"  # normalized upper
    assert body["is_default"] is False

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        count = (
            await s.execute(select(func.count()).select_from(Entity).where(Entity.slug == "us"))
        ).scalar_one()
    assert count == 1


async def test_create_entity_rejects_bad_slug(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/entities", json={"name": "Bad", "slug": "Has Spaces"})
    assert resp.status_code == 400


async def test_create_entity_rejects_duplicate_slug(realdb):
    async with realdb.client(key="a", role="admin") as c:
        first = await c.post("/api/entities", json={"name": "UK Ltd", "slug": "uk"})
        assert first.status_code == 201
        dup = await c.post("/api/entities", json={"name": "UK Two", "slug": "uk"})
    assert dup.status_code == 409


async def test_create_entity_requires_admin(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/entities", json={"name": "Nope", "slug": "nope"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_update_entity_rename_and_currency(realdb):
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post("/api/entities", json={"name": "Old", "slug": "ent1"})
        eid = created.json()["id"]
        resp = await c.patch(f"/api/entities/{eid}", json={"name": "New Name", "currency": "eur"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "New Name"
    assert body["currency"] == "EUR"


async def test_cannot_deactivate_default_entity(realdb):
    async with realdb.client(key="a", role="admin") as c:
        listing = await c.get("/api/entities")
        default_id = listing.json()[0]["id"]
        resp = await c.patch(f"/api/entities/{default_id}", json={"is_active": False})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DB constraints
# ---------------------------------------------------------------------------


async def test_single_default_entity_enforced(realdb):
    """uq_entities_one_default makes a second default impossible."""
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        s.add(
            Entity(
                organization_id=realdb.info("a").org_id,
                name="Second Default",
                slug="second-default",
                is_default=True,
            )
        )
        with pytest.raises(IntegrityError):
            await s.flush()
        await s.rollback()


async def test_entity_slug_unique(realdb):
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        s.add(Entity(organization_id=realdb.info("a").org_id, name="Dup", slug="default"))
        with pytest.raises(IntegrityError):
            await s.flush()
        await s.rollback()


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_entities_are_tenant_isolated(realdb):
    async with realdb.client(key="a", role="admin") as c:
        await c.post("/api/entities", json={"name": "A-only", "slug": "a-only"})

    async with realdb.client(key="b", role="admin") as c:
        resp = await c.get("/api/entities")
    slugs = {e["slug"] for e in resp.json()}
    assert "a-only" not in slugs  # tenant b never sees tenant a's entity


# ---------------------------------------------------------------------------
# Audit trail (project invariant #3)
# ---------------------------------------------------------------------------


async def _entity_audits(realdb, key="a"):
    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker(key)
    async with mk() as s:
        return (
            (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.entity_type == "entity")
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )


async def test_create_and_update_entity_write_audit_rows(realdb):
    """An Entity is the scope key every entity-scoped money query is filtered
    by, so minting or deactivating one is a config change that belongs on the
    append-only trail. Neither handler wrote one."""
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post("/api/entities", json={"name": "Audited Sub", "slug": "audited-sub"})
        assert created.status_code == 201
        eid = created.json()["id"]
        patched = await c.patch(f"/api/entities/{eid}", json={"is_active": False})
        assert patched.status_code == 200

    rows = await _entity_audits(realdb)
    actions = [r.action for r in rows]
    assert actions == ["entity.created", "entity.updated"]
    assert rows[0].details["slug"] == "audited-sub"
    assert rows[0].actor_id is not None
    assert rows[1].details["changed"] == {"is_active": False}


async def test_no_op_patch_writes_no_audit_row(realdb):
    """A PATCH that changes nothing is not a mutation — don't pad the trail."""
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post("/api/entities", json={"name": "Same", "slug": "same"})
        eid = created.json()["id"]
        await c.patch(f"/api/entities/{eid}", json={"name": "Same"})

    actions = [r.action for r in await _entity_audits(realdb)]
    assert actions == ["entity.created"]


# ---------------------------------------------------------------------------
# Set default (POST /api/entities/{id}/set-default)
# ---------------------------------------------------------------------------


async def _default_ids(realdb, key="a") -> list[str]:
    mk = realdb.sessionmaker(key)
    async with mk() as s:
        rows = (
            (await s.execute(select(Entity.id).where(Entity.is_default.is_(True)))).scalars().all()
        )
    return [str(r) for r in rows]


async def test_set_default_swaps_the_default_atomically(realdb):
    """Exactly one entity is default before AND after the call — never zero,
    never two."""
    async with realdb.client(key="a", role="admin") as c:
        listing = await c.get("/api/entities")
        original_default_id = listing.json()[0]["id"]

        created = await c.post("/api/entities", json={"name": "US Inc", "slug": "us-default"})
        new_id = created.json()["id"]

        before = await _default_ids(realdb)
        assert before == [original_default_id]

        resp = await c.post(f"/api/entities/{new_id}/set-default")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == new_id
    assert body["is_default"] is True

    after = await _default_ids(realdb)
    assert after == [new_id]  # exactly one default, and it's the new one

    # The old default lost the flag but is otherwise untouched (still active).
    async with realdb.client(key="a", role="admin") as c:
        listing = await c.get("/api/entities")
    by_id = {e["id"]: e for e in listing.json()}
    assert by_id[original_default_id]["is_default"] is False
    assert by_id[original_default_id]["is_active"] is True


async def test_set_default_is_idempotent_on_the_current_default(realdb):
    async with realdb.client(key="a", role="admin") as c:
        listing = await c.get("/api/entities")
        default_id = listing.json()[0]["id"]
        resp = await c.post(f"/api/entities/{default_id}/set-default")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_default"] is True

    after = await _default_ids(realdb)
    assert after == [default_id]


async def test_set_default_rejects_inactive_entity(realdb):
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post("/api/entities", json={"name": "Dormant", "slug": "dormant"})
        eid = created.json()["id"]
        await c.patch(f"/api/entities/{eid}", json={"is_active": False})
        resp = await c.post(f"/api/entities/{eid}/set-default")
    assert resp.status_code == 400


async def test_set_default_404_on_unknown_entity(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/entities/00000000-0000-0000-0000-000000000000/set-default")
    assert resp.status_code == 404


async def test_set_default_requires_admin(realdb):
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post("/api/entities", json={"name": "US Inc", "slug": "us-perm"})
        new_id = created.json()["id"]

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/entities/{new_id}/set-default")
    assert resp.status_code == 403

    # Confirm the non-admin attempt changed nothing.
    original_default_id = (await _default_ids(realdb))[0]
    assert original_default_id != new_id


async def test_set_default_writes_audit_row(realdb):
    async with realdb.client(key="a", role="admin") as c:
        listing = await c.get("/api/entities")
        original_default_id = listing.json()[0]["id"]
        created = await c.post("/api/entities", json={"name": "US Inc", "slug": "us-audit"})
        new_id = created.json()["id"]
        resp = await c.post(f"/api/entities/{new_id}/set-default")
    assert resp.status_code == 200

    rows = await _entity_audits(realdb)
    default_changed = [r for r in rows if r.action == "entity.default_changed"]
    assert len(default_changed) == 1
    assert default_changed[0].entity_id == uuid.UUID(new_id)
    assert default_changed[0].details["previous_default_id"] == original_default_id


async def test_set_default_tenant_isolated(realdb):
    """Setting tenant A's default cannot touch tenant B's default entity."""
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post("/api/entities", json={"name": "A Sub", "slug": "a-sub"})
        new_id = created.json()["id"]
        await c.post(f"/api/entities/{new_id}/set-default")

    b_defaults = await _default_ids(realdb, key="b")
    async with realdb.client(key="b", role="admin") as c:
        listing = await c.get("/api/entities")
    b_default_row = next(e for e in listing.json() if e["is_default"])
    assert [b_default_row["id"]] == b_defaults
    assert b_default_row["slug"] == "default"  # tenant b's own default, untouched
