"""Inter-company invoice routing (multi-entity) — HTTP + service.

Covers ``POST /api/invoices/{id}/route-intercompany`` and the underlying
``services.intercompany.route_intercompany_invoice``:

  - routing creates a mirror payable under the counterparty entity with the
    exact Decimal amount and a bidirectional ``intercompany_mirror_id`` link
  - idempotency: a second call returns the SAME mirror and the invoice count
    is unchanged (no duplicate payable)
  - self-billing (counterparty == own entity) is rejected (400)
  - RBAC: an ap_clerk is 403
  - segregation of duties crosses the entity boundary: everyone implicated in
    the source payable (its uploader and its ``segregation_actor_ids``) is
    refused the mirror's approval, and the routing actor is its uploader
  - migration ``0100_mirror_implicated_backfill`` gives a mirror routed before
    that rule exactly what routing gives one today, orienting each pair by the
    routing's own audit row, on every non-terminal status, with an audit row of
    its own — and its SQL is evaluated against the Python rule it restates

Runs against the opt-in ``realdb`` fixture (skips without ``pnpm db:up``).
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import itertools
import json
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services import intercompany as intercompany_module
from app.services.approval_chain import implicated_actors, violates_segregation
from app.services.intercompany import inherited_actor_ids
from app.services.workflow_engine import VALID_TRANSITIONS


async def _default_entity_id(mk) -> uuid.UUID:
    async with mk() as s:
        return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _make_entity(mk, org_id, *, name: str, slug: str) -> uuid.UUID:
    eid = uuid.uuid4()
    async with mk() as s:
        s.add(Entity(id=eid, organization_id=org_id, name=name, slug=slug))
        await s.commit()
    return eid


async def _seed_invoice(
    mk,
    org_id,
    *,
    entity_id: uuid.UUID,
    amount: str = "1234.56",
    number: str = "IC-ORIGIN-1",
    uploaded_by_id: uuid.UUID | None = None,
    segregation_actor_ids: list[str] | None = None,
) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                entity_id=entity_id,
                invoice_number=number,
                vendor_name="Intercompany Vendor",
                amount=Decimal(amount),
                currency="USD",
                status=InvoiceStatus.approved,
                uploaded_by_id=uploaded_by_id,
                segregation_actor_ids=segregation_actor_ids,
            )
        )
        await s.commit()
    return inv_id


# ---------------------------------------------------------------------------
# Happy path — mirror created under counterparty, exact amount, linked both ways
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_creates_linked_mirror_under_counterparty(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    origin_entity = await _default_entity_id(mk)
    counterparty = await _make_entity(mk, info.org_id, name="Subsidiary B", slug="ic-sub-b")
    origin_id = await _seed_invoice(mk, info.org_id, entity_id=origin_entity, amount="1234.56")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{origin_id}/route-intercompany",
            json={"counterparty_entity_id": str(counterparty)},
        )
    assert resp.status_code == 200, resp.text
    mirror_json = resp.json()
    mirror_id = uuid.UUID(mirror_json["id"])
    assert mirror_id != origin_id
    # Mirror's counterparty points back at the origin's entity.
    assert mirror_json["counterparty_entity_id"] == str(origin_entity)
    assert mirror_json["intercompany_mirror_id"] == str(origin_id)

    # Read both rows back to prove durability + exact amount + bidirectional link.
    async with mk() as s:
        mirror = (await s.execute(select(Invoice).where(Invoice.id == mirror_id))).scalar_one()
        origin = (await s.execute(select(Invoice).where(Invoice.id == origin_id))).scalar_one()

    # Mirror lives under the counterparty entity, with the EXACT Decimal amount.
    assert mirror.entity_id == counterparty
    assert mirror.amount == Decimal("1234.56")
    assert mirror.currency == "USD"
    assert mirror.status == InvoiceStatus.new
    assert mirror.invoice_number == "IC-IC-ORIGIN-1"
    # Bidirectional link.
    assert mirror.intercompany_mirror_id == origin_id
    assert mirror.counterparty_entity_id == origin_entity
    assert origin.intercompany_mirror_id == mirror_id
    # The routing actor CREATED this payable, so they are its uploader for
    # segregation of duties. Without the stamp the mirror is a NULL-uploader row
    # (`approval_chain.violates_segregation` reads NULL as "no employee
    # creator") and the one person who caused a live liability under another
    # entity could also sign it off.
    assert mirror.uploaded_by_id == info.users["ap_manager"]
    # The source was seeded with nobody implicated, so the mirror inherits
    # nobody: NULL, the shape every creation path writes for "nobody beyond the
    # uploader" — never `[]`.
    assert mirror.segregation_actor_ids is None


# ---------------------------------------------------------------------------
# Idempotency — second call returns the same mirror, no duplicate payable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_is_idempotent(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    origin_entity = await _default_entity_id(mk)
    counterparty = await _make_entity(mk, info.org_id, name="Subsidiary C", slug="ic-sub-c")
    origin_id = await _seed_invoice(
        mk, info.org_id, entity_id=origin_entity, number="IC-IDEMPOTENT-1"
    )

    async def _count() -> int:
        async with mk() as s:
            return (await s.execute(select(func.count(Invoice.id)))).scalar_one()

    before = await _count()

    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.post(
            f"/api/invoices/{origin_id}/route-intercompany",
            json={"counterparty_entity_id": str(counterparty)},
        )
        assert first.status_code == 200, first.text
        second = await c.post(
            f"/api/invoices/{origin_id}/route-intercompany",
            json={"counterparty_entity_id": str(counterparty)},
        )
        assert second.status_code == 200, second.text

    # Same mirror returned both times.
    assert first.json()["id"] == second.json()["id"]
    # Exactly one mirror created (origin + 1 mirror = before + 1).
    assert await _count() == before + 1


# ---------------------------------------------------------------------------
# Segregation of duties crosses the entity boundary
#
# The mirror's vendor, amount and currency are the source's, copied verbatim, so
# whoever shaped the source shaped the mirror. Before the fix the mirror carried
# only its router as uploader and `segregation_actor_ids=None`, so the employee
# who uploaded the source payable — or any template author / material editor on
# its implicated set — could approve the mirror under the counterparty entity.
# ---------------------------------------------------------------------------


async def _route(realdb, origin_id: uuid.UUID, counterparty: uuid.UUID, *, role: str) -> uuid.UUID:
    async with realdb.client(key="a", role=role) as c:
        resp = await c.post(
            f"/api/invoices/{origin_id}/route-intercompany",
            json={"counterparty_entity_id": str(counterparty)},
        )
    assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["id"])


async def _make_reviewable(mk, invoice_id: uuid.UUID) -> None:
    """Put the mirror where an approval is legal, so a 403 below can only be the
    segregation refusal and a 200 proves the invoice was approvable at all."""
    async with mk() as s:
        inv = await s.get(Invoice, invoice_id)
        inv.status = InvoiceStatus.ready_for_review
        await s.commit()


@pytest.mark.asyncio
async def test_the_source_uploader_cannot_approve_the_mirror(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    origin_entity = await _default_entity_id(mk)
    counterparty = await _make_entity(mk, info.org_id, name="Subsidiary H", slug="ic-sub-h")
    source_uploader = info.users["ap_manager"]
    origin_id = await _seed_invoice(
        mk,
        info.org_id,
        entity_id=origin_entity,
        number="IC-SOD-UPLOADER-1",
        uploaded_by_id=source_uploader,
    )

    # A DIFFERENT employee routes it, so the source's uploader is not the
    # mirror's uploader — the only thing that can refuse them is inheritance.
    mirror_id = await _route(realdb, origin_id, counterparty, role="admin")

    async with mk() as s:
        mirror = await s.get(Invoice, mirror_id)
    assert mirror.uploaded_by_id == info.users["admin"]
    assert mirror.segregation_actor_ids == [str(source_uploader)]

    await _make_reviewable(mk, mirror_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        refused = await c.post(f"/api/invoices/{mirror_id}/approve", json={})
    assert refused.status_code == 403, refused.text
    assert "segregation" in refused.json()["detail"].lower()

    # The control is a set of implicated people, not a lock on the queue: an
    # employee with no hand in either invoice still approves.
    async with realdb.client(key="a", role="cfo") as c:
        allowed = await c.post(f"/api/invoices/{mirror_id}/approve", json={})
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_the_mirror_inherits_the_source_implicated_set_too(realdb):
    """Both of the source's columns travel, not just its uploader. Carrying one
    without the other would bar a source *uploader* while leaving a source
    *editor* (a recurring template's author or material editor, stamped on the
    source's `segregation_actor_ids`) free to sign the mirror."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    origin_entity = await _default_entity_id(mk)
    counterparty = await _make_entity(mk, info.org_id, name="Subsidiary I", slug="ic-sub-i")
    template_editor = info.users["cfo"]
    origin_id = await _seed_invoice(
        mk,
        info.org_id,
        entity_id=origin_entity,
        number="IC-SOD-SET-1",
        uploaded_by_id=info.users["ap_manager"],
        segregation_actor_ids=[str(template_editor)],
    )

    mirror_id = await _route(realdb, origin_id, counterparty, role="admin")

    async with mk() as s:
        mirror = await s.get(Invoice, mirror_id)
    assert mirror.uploaded_by_id == info.users["admin"]
    assert mirror.segregation_actor_ids == sorted(
        [str(info.users["ap_manager"]), str(template_editor)]
    )
    # Someone with no hand in the source is still free.
    assert violates_segregation(mirror, uuid.uuid4(), {}) is False

    await _make_reviewable(mk, mirror_id)
    async with realdb.client(key="a", role="cfo") as c:
        refused = await c.post(f"/api/invoices/{mirror_id}/approve", json={})
    assert refused.status_code == 403, refused.text
    assert "segregation" in refused.json()["detail"].lower()


@pytest.mark.asyncio
async def test_a_router_who_uploaded_the_source_is_named_once(realdb):
    """When the router is also the source's uploader they land in the mirror's
    `uploaded_by_id` and are dropped from the set — named once, so the two
    columns never look like they disagree about someone."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    origin_entity = await _default_entity_id(mk)
    counterparty = await _make_entity(mk, info.org_id, name="Subsidiary J", slug="ic-sub-j")
    origin_id = await _seed_invoice(
        mk,
        info.org_id,
        entity_id=origin_entity,
        number="IC-SOD-SELF-1",
        uploaded_by_id=info.users["ap_manager"],
    )

    mirror_id = await _route(realdb, origin_id, counterparty, role="ap_manager")

    async with mk() as s:
        mirror = await s.get(Invoice, mirror_id)
    assert mirror.uploaded_by_id == info.users["ap_manager"]
    assert mirror.segregation_actor_ids is None


# ---------------------------------------------------------------------------
# Self-billing is rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_billing_is_rejected(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    origin_entity = await _default_entity_id(mk)
    origin_id = await _seed_invoice(mk, info.org_id, entity_id=origin_entity, number="IC-SELF-1")

    async def _count() -> int:
        async with mk() as s:
            return (await s.execute(select(func.count(Invoice.id)))).scalar_one()

    before = await _count()

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{origin_id}/route-intercompany",
            json={"counterparty_entity_id": str(origin_entity)},
        )
    assert resp.status_code == 400, resp.text
    # No mirror created.
    assert await _count() == before


# ---------------------------------------------------------------------------
# RBAC — ap_clerk cannot route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ap_clerk_forbidden(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    origin_entity = await _default_entity_id(mk)
    counterparty = await _make_entity(mk, info.org_id, name="Subsidiary D", slug="ic-sub-d")
    origin_id = await _seed_invoice(mk, info.org_id, entity_id=origin_entity, number="IC-RBAC-1")

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            f"/api/invoices/{origin_id}/route-intercompany",
            json={"counterparty_entity_id": str(counterparty)},
        )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Concurrency — two simultaneous routing calls must yield exactly ONE mirror
#
# Before the fix the endpoint loaded the origin with a plain SELECT, so two
# concurrent callers both observed `intercompany_mirror_id IS NULL` and each
# INSERTed a live mirror payable under the counterparty entity. The orphaned
# second payable could then be approved and paid on its own — a real double
# liability. The endpoint now takes the origin FOR UPDATE, which serializes the
# racers so the loser re-reads the (now-set) link and returns the SAME mirror.
#
# This needs genuinely separate DB connections — a single mocked session cannot
# model two backends contending for a row lock — so it uses the realdb
# per-key session makers, like tests/test_payment_concurrency.py.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_routing_creates_exactly_one_mirror(realdb):
    import asyncio

    from fastapi import HTTPException

    from app.api.invoices import route_intercompany
    from app.schemas.invoice import RouteIntercompanyRequest

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    origin_entity = await _default_entity_id(mk)
    counterparty = await _make_entity(mk, info.org_id, name="Subsidiary E", slug="ic-sub-e")
    origin_id = await _seed_invoice(
        mk, info.org_id, entity_id=origin_entity, amount="4321.00", number="IC-RACE-1"
    )

    async def _count() -> int:
        async with mk() as s:
            return (await s.execute(select(func.count(Invoice.id)))).scalar_one()

    before = await _count()
    user = SimpleNamespace(id=info.users["ap_manager"], full_name="Racer", roles=["ap_manager"])
    body = RouteIntercompanyRequest(counterparty_entity_id=str(counterparty))

    async def _route_once():
        session_mk = realdb.sessionmaker("a")
        async with session_mk() as db:
            try:
                resp = await route_intercompany(invoice_id=origin_id, body=body, db=db, user=user)
                await db.commit()
                return ("ok", resp.id)
            except HTTPException as exc:
                await db.rollback()
                return ("http", exc.status_code)

    results = await asyncio.gather(_route_once(), _route_once())

    # Exactly ONE new invoice row: the single mirror payable. Two would be a
    # duplicate liability payable independently of the first.
    assert await _count() == before + 1, results

    # Both racers agree on which mirror is THE mirror (or the loser 409s off the
    # DB backstop) — neither may walk away holding a second, orphaned payable.
    mirror_ids = {r[1] for r in results if r[0] == "ok"}
    assert len(mirror_ids) == 1, results
    conflicts = [r for r in results if r[0] == "http"]
    assert all(r[1] == 409 for r in conflicts), results

    # And the origin points at that one mirror, bidirectionally.
    async with mk() as s:
        origin = await s.get(Invoice, origin_id)
        assert origin.intercompany_mirror_id is not None
        assert str(origin.intercompany_mirror_id) == next(iter(mirror_ids))
        mirror = await s.get(Invoice, origin.intercompany_mirror_id)
        assert mirror.entity_id == counterparty
        assert mirror.intercompany_mirror_id == origin_id
        assert mirror.amount == Decimal("4321.00")


# ---------------------------------------------------------------------------
# Once routed, a re-route with a DIFFERENT counterparty must not silently
# re-point the origin — that would leave it claiming an entity its only mirror
# doesn't sit under, with no second mirror ever generated.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reroute_does_not_repoint_counterparty(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    origin_entity = await _default_entity_id(mk)
    first_cp = await _make_entity(mk, info.org_id, name="Subsidiary F", slug="ic-sub-f")
    other_cp = await _make_entity(mk, info.org_id, name="Subsidiary G", slug="ic-sub-g")
    origin_id = await _seed_invoice(mk, info.org_id, entity_id=origin_entity, number="IC-REROUTE-1")

    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.post(
            f"/api/invoices/{origin_id}/route-intercompany",
            json={"counterparty_entity_id": str(first_cp)},
        )
        assert first.status_code == 200, first.text
        second = await c.post(
            f"/api/invoices/{origin_id}/route-intercompany",
            json={"counterparty_entity_id": str(other_cp)},
        )
        assert second.status_code == 200, second.text

    # Same mirror, still under the ORIGINAL counterparty.
    assert first.json()["id"] == second.json()["id"]
    async with mk() as s:
        origin = await s.get(Invoice, origin_id)
        assert origin.counterparty_entity_id == first_cp
        mirror = await s.get(Invoice, origin.intercompany_mirror_id)
        assert mirror.entity_id == first_cp


# ---------------------------------------------------------------------------
# The DB-level backstop is declared on the model, so freshly provisioned
# tenants (`create_all`, not Alembic) get it too — migration 0075 covers the
# already-provisioned ones.
# ---------------------------------------------------------------------------


def test_mirror_unique_index_declared_on_model():
    idx = {i.name: i for i in Invoice.__table__.indexes}
    mirror_idx = idx.get("uq_invoice_intercompany_mirror")
    assert mirror_idx is not None, sorted(idx)
    assert mirror_idx.unique is True
    assert [c.name for c in mirror_idx.columns] == ["intercompany_mirror_id"]
    predicate = str(mirror_idx.dialect_options["postgresql"]["where"])
    assert "intercompany_mirror_id IS NOT NULL" in predicate


# ---------------------------------------------------------------------------
# Migration 0100 — mirrors routed before §192 inherit their source's set
#
# A mirror routed before §192 carries no `segregation_actor_ids` (SQL NULL if
# routed before 0097 added the column, JSON null between 0097 and §192), and one
# routed before §131 carries no uploader either — so the source's uploader or
# editors, and for the oldest mirrors the router, could approve the mirror or
# clear a payment-blocking exception on it. The backfill writes what routing
# writes today, orienting each pair by the routing's own audit row
# (docs/decisions.md §198).
#
# Each test routes a real mirror through the endpoint — so the routing audit
# rows are genuine — then rewinds the mirror's columns to the legacy shape and
# runs the migration's own SQL against the real tenant DB.
# ---------------------------------------------------------------------------

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0100_mirror_implicated_backfill.py"
)


def _migration():
    """Import the migration for its SQL (it never runs ``op`` here)."""
    spec = importlib.util.spec_from_file_location("_mig_0100", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _rewind(
    mk,
    invoice_id: uuid.UUID,
    *,
    set_shape: str = "json_null",
    clear_uploader: bool = False,
    status: InvoiceStatus | None = None,
) -> None:
    """Put a freshly routed mirror back into a pre-§192 shape.

    ``json_null`` is what a mirror routed between migration 0097 and §192 holds
    (``segregation_actor_ids=None`` persists as JSON ``null`` on a ``JSONB``
    column); ``sql_null`` is a mirror routed before 0097 added the column.
    ``clear_uploader`` rewinds further, to before §131 stamped the router.
    """
    set_sql = {"json_null": "'null'::jsonb", "sql_null": "NULL", "empty": "'[]'::jsonb"}[set_shape]
    assignments = [f"segregation_actor_ids = {set_sql}"]
    if clear_uploader:
        assignments.append("uploaded_by_id = NULL")
    params: dict = {"id": invoice_id}
    if status is not None:
        assignments.append("status = :status")
        params["status"] = status.value
    async with mk() as s:
        await s.execute(
            text(f"UPDATE invoices SET {', '.join(assignments)} WHERE id = :id"), params
        )
        await s.commit()


async def _backfill(mk) -> None:
    async with mk() as s:
        await s.execute(text(_migration().BACKFILL_SQL))
        await s.commit()


async def _load(mk, invoice_id: uuid.UUID) -> Invoice:
    async with mk() as s:
        return await s.get(Invoice, invoice_id)


async def _backfill_rows(mk, invoice_id: uuid.UUID) -> list:
    async with mk() as s:
        rows = await s.execute(
            select(AuditLog)
            .where(AuditLog.entity_id == invoice_id)
            .where(AuditLog.action == _migration().BACKFILL_ACTION)
        )
        return list(rows.scalars().all())


async def _routed_pair(
    realdb,
    *,
    slug: str,
    number: str,
    router: str = "admin",
    source_uploader: str | None = "ap_manager",
    source_set: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    origin_entity = await _default_entity_id(mk)
    counterparty = await _make_entity(mk, info.org_id, name=f"Subsidiary {slug}", slug=slug)
    origin_id = await _seed_invoice(
        mk,
        info.org_id,
        entity_id=origin_entity,
        number=number,
        uploaded_by_id=info.users[source_uploader] if source_uploader else None,
        segregation_actor_ids=source_set,
    )
    mirror_id = await _route(realdb, origin_id, counterparty, role=router)
    return origin_id, mirror_id


@pytest.mark.asyncio
async def test_backfill_gives_a_legacy_mirror_what_routing_gives_it_today(realdb):
    """The whole point: the mirror ends up exactly as routing today leaves it,
    the source's uploader is refused its approval, and the trail says when and
    why the set arrived. A second run changes nothing."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    source_uploader = info.users["ap_manager"]
    template_editor = info.users["cfo"]
    origin_id, mirror_id = await _routed_pair(
        realdb, slug="ic-bf-a", number="IC-BF-A", source_set=[str(template_editor)]
    )
    routed_today = (await _load(mk, mirror_id)).segregation_actor_ids
    assert routed_today == sorted([str(source_uploader), str(template_editor)])

    await _rewind(mk, mirror_id, set_shape="json_null")
    legacy = await _load(mk, mirror_id)
    assert legacy.segregation_actor_ids is None
    # The hole: the source's uploader is free to sign the mirror.
    assert violates_segregation(legacy, source_uploader, {}) is False

    await _backfill(mk)

    mirror = await _load(mk, mirror_id)
    assert mirror.segregation_actor_ids == routed_today
    assert mirror.uploaded_by_id == info.users["admin"]
    assert violates_segregation(mirror, source_uploader, {}) is True
    assert violates_segregation(mirror, template_editor, {}) is True

    [row] = await _backfill_rows(mk, mirror_id)
    # Nobody acted (§141's rule): the audit row names no actor.
    assert row.actor_id is None
    assert row.correlation_id == mirror.correlation_id
    assert row.details["origin_invoice_id"] == str(origin_id)
    assert row.details["revision"] == _migration().revision
    assert row.details["changes"] == {"segregation_actor_ids": {"old": None, "new": routed_today}}

    # Through the real approval route, not just the predicate.
    await _make_reviewable(mk, mirror_id)
    async with realdb.client(key="a", role="ap_manager") as c:
        refused = await c.post(f"/api/invoices/{mirror_id}/approve", json={})
    assert refused.status_code == 403, refused.text
    assert "segregation" in refused.json()["detail"].lower()

    # Idempotent: nothing left to change, so no second row either.
    await _backfill(mk)
    assert (await _load(mk, mirror_id)).segregation_actor_ids == routed_today
    assert len(await _backfill_rows(mk, mirror_id)) == 1


@pytest.mark.asyncio
async def test_backfill_names_the_router_of_a_pre_131_mirror_from_the_routing_record(realdb):
    """A mirror routed before §131 has no uploader either, so its router was
    free too. The router is named on the routing's audit row, so that is copied
    in as well — and, as routing does, dropped from the set."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    _origin_id, mirror_id = await _routed_pair(
        realdb, slug="ic-bf-b", number="IC-BF-B", source_set=[str(info.users["cfo"])]
    )
    await _rewind(mk, mirror_id, set_shape="sql_null", clear_uploader=True)

    await _backfill(mk)

    mirror = await _load(mk, mirror_id)
    assert mirror.uploaded_by_id == info.users["admin"]
    assert mirror.segregation_actor_ids == sorted(
        [str(info.users["ap_manager"]), str(info.users["cfo"])]
    )
    assert violates_segregation(mirror, info.users["admin"], {}) is True
    [row] = await _backfill_rows(mk, mirror_id)
    assert row.details["changes"]["uploaded_by_id"] == {
        "old": None,
        "new": str(info.users["admin"]),
    }


@pytest.mark.asyncio
async def test_backfill_names_a_router_who_uploaded_the_source_once(realdb):
    """Routing drops the mirror's uploader from the set; so does the backfill.
    With a router who also uploaded the source there is nobody left, so the set
    stays empty and a post-§131 mirror is not touched at all."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    _o, post_131 = await _routed_pair(realdb, slug="ic-bf-c", number="IC-BF-C", router="ap_manager")
    _o, pre_131 = await _routed_pair(realdb, slug="ic-bf-d", number="IC-BF-D", router="ap_manager")
    await _rewind(mk, post_131)
    await _rewind(mk, pre_131, set_shape="sql_null", clear_uploader=True)

    await _backfill(mk)

    untouched = await _load(mk, post_131)
    assert untouched.uploaded_by_id == info.users["ap_manager"]
    assert untouched.segregation_actor_ids is None
    assert await _backfill_rows(mk, post_131) == []

    stamped = await _load(mk, pre_131)
    assert stamped.uploaded_by_id == info.users["ap_manager"]
    assert stamped.segregation_actor_ids is None
    [row] = await _backfill_rows(mk, pre_131)
    assert set(row.details["changes"]) == {"uploaded_by_id"}


@pytest.mark.asyncio
async def test_backfill_leaves_a_mirror_that_already_carries_a_set_alone(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    _o, mirror_id = await _routed_pair(realdb, slug="ic-bf-e", number="IC-BF-E")
    # Not what routing would write — so an overwrite would be visible.
    kept = [str(info.users["cfo"])]
    async with mk() as s:
        await s.execute(
            text("UPDATE invoices SET segregation_actor_ids = CAST(:v AS jsonb) WHERE id = :id"),
            {"v": json.dumps(kept), "id": mirror_id},
        )
        await s.commit()

    await _backfill(mk)

    assert (await _load(mk, mirror_id)).segregation_actor_ids == kept
    assert await _backfill_rows(mk, mirror_id) == []


@pytest.mark.asyncio
async def test_backfill_reaches_every_status_with_a_decision_ahead_and_no_terminal_one(realdb):
    """Not "not yet past approval": the set also gates clearing a
    payment-blocking exception (§169), a decision that only exists after
    approval, and `paid` can be voided back to `approved`. Only a status with no
    successor has nothing left for the set to gate."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    mirrors: dict[InvoiceStatus, uuid.UUID] = {}
    for status in InvoiceStatus:
        _o, mirror_id = await _routed_pair(
            realdb, slug=f"ic-bf-s-{status.value}", number=f"IC-BF-S-{status.value}"
        )
        await _rewind(mk, mirror_id, status=status)
        mirrors[status] = mirror_id

    await _backfill(mk)

    source_uploader = info.users["ap_manager"]
    for status, mirror_id in mirrors.items():
        mirror = await _load(mk, mirror_id)
        if VALID_TRANSITIONS[status]:
            assert mirror.segregation_actor_ids == [str(source_uploader)], status
        else:
            assert mirror.segregation_actor_ids is None, status
            assert await _backfill_rows(mk, mirror_id) == [], status

    # An approved mirror's remaining decision is the exception queue's: its
    # source's uploader can no longer clear a fraud flag standing between it
    # and a payment run.
    from app.services.exception_lifecycle import REFUSAL_IMPLICATED, segregation_refusal

    approved = await _load(mk, mirrors[InvoiceStatus.approved])
    flag = SimpleNamespace(exception_type="fraud_flag", raised_by_user_id=None)
    assert (
        segregation_refusal(flag, approved, source_uploader, action="resolve", org_settings=None)
        == REFUSAL_IMPLICATED
    )


@pytest.mark.asyncio
async def test_backfill_with_nobody_implicated_in_the_source_changes_nothing(realdb):
    """An email-intake or portal source has no uploader and no set, so there is
    nothing to inherit — the mirror keeps its empty set and gets no row."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    _o, mirror_id = await _routed_pair(
        realdb, slug="ic-bf-f", number="IC-BF-F", source_uploader=None
    )
    await _rewind(mk, mirror_id, set_shape="empty")

    await _backfill(mk)

    mirror = await _load(mk, mirror_id)
    assert mirror.uploaded_by_id == info.users["admin"]
    assert mirror.segregation_actor_ids == []
    assert await _backfill_rows(mk, mirror_id) == []


@pytest.mark.asyncio
async def test_backfill_orients_each_pair_by_the_routing_record(realdb):
    """The FK is set on both rows and the entity columns are symmetric, so only
    the routing's `role: mirror` audit row says which side is the mirror. The
    origin must never inherit its own mirror's router; an ordinary invoice is
    not touched; a renamed mirror is still found; and two invoices linked with
    no routing record are left alone rather than guessed at."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    origin_id, mirror_id = await _routed_pair(realdb, slug="ic-bf-g", number="IC-BF-G")
    await _rewind(mk, mirror_id)
    # The origin is linked too and carries no set — the shape a misoriented
    # backfill would stamp with the router.
    await _rewind(mk, origin_id)
    async with mk() as s:
        # `invoice_number` is editable, which is why it cannot orient a pair.
        await s.execute(
            text("UPDATE invoices SET invoice_number = 'RENAMED' WHERE id = :id"),
            {"id": mirror_id},
        )
        await s.commit()

    default_entity = await _default_entity_id(mk)
    ordinary_id = await _seed_invoice(
        mk, info.org_id, entity_id=default_entity, number="IC-BF-ORDINARY"
    )
    first = await _seed_invoice(
        mk,
        info.org_id,
        entity_id=default_entity,
        number="IC-BF-UNROUTED",
        uploaded_by_id=info.users["ap_manager"],
    )
    second = await _seed_invoice(
        mk, info.org_id, entity_id=default_entity, number="IC-IC-BF-UNROUTED"
    )
    async with mk() as s:
        await s.execute(
            text("UPDATE invoices SET intercompany_mirror_id = :b WHERE id = :a"),
            {"a": first, "b": second},
        )
        await s.execute(
            text("UPDATE invoices SET intercompany_mirror_id = :a WHERE id = :b"),
            {"a": first, "b": second},
        )
        await s.commit()

    await _backfill(mk)

    assert (await _load(mk, mirror_id)).segregation_actor_ids == [str(info.users["ap_manager"])]
    origin = await _load(mk, origin_id)
    assert origin.segregation_actor_ids is None
    assert origin.uploaded_by_id == info.users["ap_manager"]
    assert await _backfill_rows(mk, origin_id) == []
    assert (await _load(mk, ordinary_id)).segregation_actor_ids is None
    assert (await _load(mk, second)).segregation_actor_ids is None
    assert await _backfill_rows(mk, second) == []


@pytest.mark.asyncio
async def test_the_backfill_sql_is_the_routing_rule(realdb):
    """The migration states `intercompany.inherited_actor_ids` — and through it
    `approval_chain.implicated_actors` — in SQL. Evaluate both on the same
    inputs, including every shape the column actually holds (SQL NULL, JSON
    null, `[]`, unsorted), so the two cannot disagree."""
    mig = _migration()
    a, b, c, d = (str(uuid.UUID(int=n)) for n in (0xA, 0xB, 0xC, 0xD))
    source_sets = [None, "null", "[]", json.dumps([a]), json.dumps([b, a]), json.dumps([a, c])]
    uploaders = [None, a, c]
    excluded = [None, a, b, c, d]
    sql = text(
        "SELECT "
        + mig.INHERITED_SET_SQL.format(
            source_set="CAST(CAST(:source_set AS text) AS jsonb)",
            source_uploader="CAST(CAST(:source_uploader AS text) AS uuid)",
            mirror_uploader="CAST(CAST(:mirror_uploader AS text) AS uuid)",
        )
    )

    async with realdb.sessionmaker("a")() as s:
        for raw_set, uploader, mirror_uploader in itertools.product(
            source_sets, uploaders, excluded
        ):
            got = (
                await s.execute(
                    sql,
                    {
                        "source_set": raw_set,
                        "source_uploader": uploader,
                        "mirror_uploader": mirror_uploader,
                    },
                )
            ).scalar_one()
            if isinstance(got, str):
                got = json.loads(got)
            source = SimpleNamespace(
                uploaded_by_id=uuid.UUID(uploader) if uploader else None,
                segregation_actor_ids=json.loads(raw_set) if raw_set else None,
            )
            router = uuid.UUID(mirror_uploader) if mirror_uploader else None
            case = (raw_set, uploader, mirror_uploader)
            assert got == inherited_actor_ids(source, uploader_id=router), case
            if router is None:
                assert got == (sorted(implicated_actors(source)) or None), case


def test_implicated_actors_reads_only_the_two_columns_0100_copies():
    """Migration 0100 copied the source's `uploaded_by_id` and
    `segregation_actor_ids` onto legacy mirrors. If `implicated_actors` grows a
    third input, those mirrors will not carry it — an applied migration never
    re-runs — so this fails until someone decides whether they need another
    backfill."""
    tree = ast.parse(inspect.getsource(implicated_actors))
    param = tree.body[0].args.args[0].arg
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and getattr(node.value, "id", None) == param:
            read.add(node.attr)
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "getattr"
            and getattr(node.args[0], "id", None) == param
        ):
            read.add(node.args[1].value)
    assert read == {"uploaded_by_id", "segregation_actor_ids"}


def test_backfill_terminal_statuses_are_the_state_machine_s():
    """A migration cannot import the app, so it carries the terminal set as a
    literal; this keeps the literal equal to what `VALID_TRANSITIONS` says."""
    terminal = {status.value for status, successors in VALID_TRANSITIONS.items() if not successors}
    assert set(VALID_TRANSITIONS) == set(InvoiceStatus)
    assert set(_migration().TERMINAL_STATUSES) == terminal


def test_backfill_orients_on_the_action_routing_writes():
    source = inspect.getsource(intercompany_module.route_intercompany_invoice)
    assert f'action="{_migration().ROUTED_ACTION}"' in source
    assert '"role": "mirror"' in source
    assert '"origin_invoice_id": str(invoice.id)' in source


def test_backfill_revision_matches_its_filename():
    """The e2e schema guard reads the head off the last filename, so the stem
    and the id must agree."""
    assert _migration().revision == _MIGRATION_PATH.stem


@pytest.mark.asyncio
async def test_backfill_upgrade_runs_on_a_tenant_and_no_ops_on_the_control_plane(realdb):
    """Drive the real `upgrade()` through Alembic's operations proxy, not just
    its SQL, on both database shapes."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    mig = _migration()

    def _upgrade(sync_conn) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            mig.upgrade()

    mk = realdb.sessionmaker("a")
    _o, mirror_id = await _routed_pair(realdb, slug="ic-bf-h", number="IC-BF-H")
    await _rewind(mk, mirror_id)
    async with mk() as s:
        await (await s.connection()).run_sync(_upgrade)
        await s.commit()
    assert (await _load(mk, mirror_id)).segregation_actor_ids == [
        str(realdb.info("a").users["ap_manager"])
    ]

    async with realdb.control_sessionmaker()() as s:
        await (await s.connection()).run_sync(_upgrade)
        await s.commit()
        has_invoices = (
            await s.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'invoices'"
                )
            )
        ).scalar()
    assert has_invoices is None
