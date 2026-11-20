"""Multi-entity Phase 3 — per-entity workflow selection.

``workflow_engine.get_or_create_workflow_definition`` resolves the active
``WorkflowDefinition`` that governs a new invoice with entity precedence:

1. the invoice's own entity's active definition (prefer ``is_default``), else
2. a shared / org-wide active definition (``entity_id IS NULL``).

When none exists it auto-creates the org-wide default with ``entity_id`` NULL, so
single-entity tenants keep getting exactly one org-wide definition (backward
compatible). The ``uq_workflow_definitions_one_default`` partial unique index
enforces one ``is_default`` per ``(organization_id, entity_id)`` — treating the
shared (NULL) bucket as a single sentinel.

Exercised against real Postgres (the ``realdb`` harness) because the index +
COALESCE-on-NULL semantics can only be proven against the live DB.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.entity import Entity
from app.models.invoice import Invoice
from app.models.workflow import WorkflowDefinition
from app.services.workflow_engine import (
    create_workflow_instance,
    get_or_create_workflow_definition,
)


async def _default_entity_id(session) -> uuid.UUID:
    return (await session.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _make_entity(session, org_id: uuid.UUID, slug: str) -> uuid.UUID:
    eid = uuid.uuid4()
    session.add(
        Entity(
            id=eid,
            organization_id=org_id,
            name=slug.title(),
            slug=slug,
            is_default=False,
            is_active=True,
        )
    )
    await session.flush()
    return eid


def _add_def(
    session,
    org_id: uuid.UUID,
    *,
    entity_id: uuid.UUID | None,
    steps: dict,
    is_default: bool = False,
    is_active: bool = True,
    name: str = "WF",
) -> WorkflowDefinition:
    defn = WorkflowDefinition(
        id=uuid.uuid4(),
        name=name,
        steps_config=steps,
        is_active=is_active,
        is_default=is_default,
        organization_id=org_id,
        entity_id=entity_id,
    )
    session.add(defn)
    return defn


def _invoice(org_id: uuid.UUID, entity_id: uuid.UUID | None, number: str) -> Invoice:
    return Invoice(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        invoice_number=number,
        vendor_name="Vendor",
        amount=Decimal("100.00"),
        currency="USD",
        organization_id=org_id,
        entity_id=entity_id,
    )


# ---------------------------------------------------------------------------
# Entity-aware selection precedence
# ---------------------------------------------------------------------------


async def test_entity_specific_definition_wins_over_shared(realdb):
    info = realdb.info("a")
    org_id = info.org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        entity_b = await _make_entity(s, org_id, "subsidiary-b")
        _add_def(
            s,
            org_id,
            entity_id=None,
            steps={"steps": [{"number": 1, "type": "extraction"}], "tag": "shared"},
            is_default=True,
            name="Org default",
        )
        _add_def(
            s,
            org_id,
            entity_id=entity_b,
            steps={"steps": [{"number": 1, "type": "extraction"}], "tag": "entity-b"},
            is_default=True,
            name="B-specific",
        )
        await s.flush()

        inv_b = _invoice(org_id, entity_b, "INV-B")
        s.add(inv_b)
        await s.flush()
        instance = await create_workflow_instance(s, inv_b)

        assert instance.steps_config_snapshot["tag"] == "entity-b"


async def test_entity_without_own_definition_falls_back_to_shared(realdb):
    info = realdb.info("a")
    org_id = info.org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        entity_a = await _default_entity_id(s)
        entity_b = await _make_entity(s, org_id, "subsidiary-b")
        _add_def(
            s,
            org_id,
            entity_id=None,
            steps={"steps": [{"number": 1, "type": "extraction"}], "tag": "shared"},
            is_default=True,
            name="Org default",
        )
        # entity_b has its own definition; entity_a (default) has none.
        _add_def(
            s,
            org_id,
            entity_id=entity_b,
            steps={"steps": [{"number": 1, "type": "extraction"}], "tag": "entity-b"},
            is_default=True,
            name="B-specific",
        )
        await s.flush()

        inv_a = _invoice(org_id, entity_a, "INV-A")
        s.add(inv_a)
        await s.flush()
        instance = await create_workflow_instance(s, inv_a)

        # Entity A has no definition of its own → shared/org default.
        assert instance.steps_config_snapshot["tag"] == "shared"


async def test_entity_default_preferred_over_non_default_for_same_entity(realdb):
    info = realdb.info("a")
    org_id = info.org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        entity_b = await _make_entity(s, org_id, "subsidiary-b")
        _add_def(
            s,
            org_id,
            entity_id=entity_b,
            steps={"steps": [{"number": 1, "type": "extraction"}], "tag": "b-extra"},
            is_default=False,
            name="B extra",
        )
        _add_def(
            s,
            org_id,
            entity_id=entity_b,
            steps={"steps": [{"number": 1, "type": "extraction"}], "tag": "b-default"},
            is_default=True,
            name="B default",
        )
        await s.flush()

        defn = await get_or_create_workflow_definition(s, org_id, entity_b)
        assert defn.steps_config["tag"] == "b-default"


# ---------------------------------------------------------------------------
# Auto-create path
# ---------------------------------------------------------------------------


async def test_auto_create_makes_shared_default_and_instance_uses_it(realdb):
    info = realdb.info("a")
    org_id = info.org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        entity_a = await _default_entity_id(s)
        # No definitions exist at all.
        assert (
            await s.execute(
                select(WorkflowDefinition).where(WorkflowDefinition.organization_id == org_id)
            )
        ).scalars().first() is None

        inv = _invoice(org_id, entity_a, "INV-AUTO")
        s.add(inv)
        await s.flush()
        instance = await create_workflow_instance(s, inv)
        await s.flush()

        defn = (
            await s.execute(
                select(WorkflowDefinition).where(WorkflowDefinition.organization_id == org_id)
            )
        ).scalar_one()
        # Auto-created definition is shared (entity NULL) + the default.
        assert defn.entity_id is None
        assert defn.is_default is True
        assert instance.definition_id == defn.id
        assert instance.steps_config_snapshot == defn.steps_config


# ---------------------------------------------------------------------------
# Uniqueness index: one is_default per (org, entity)
# ---------------------------------------------------------------------------


async def test_index_allows_one_shared_default_plus_one_per_entity(realdb):
    info = realdb.info("a")
    org_id = info.org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        entity_b = await _make_entity(s, org_id, "subsidiary-b")
        _add_def(s, org_id, entity_id=None, steps={"steps": []}, is_default=True)
        _add_def(s, org_id, entity_id=entity_b, steps={"steps": []}, is_default=True)
        # A non-default extra in the shared bucket is fine.
        _add_def(s, org_id, entity_id=None, steps={"steps": []}, is_default=False)
        await s.flush()  # no IntegrityError


async def test_index_rejects_second_shared_default(realdb):
    info = realdb.info("a")
    org_id = info.org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        _add_def(s, org_id, entity_id=None, steps={"steps": []}, is_default=True)
        _add_def(s, org_id, entity_id=None, steps={"steps": []}, is_default=True)
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_index_rejects_second_default_for_same_entity(realdb):
    info = realdb.info("a")
    org_id = info.org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        entity_b = await _make_entity(s, org_id, "subsidiary-b")
        _add_def(s, org_id, entity_id=entity_b, steps={"steps": []}, is_default=True)
        _add_def(s, org_id, entity_id=entity_b, steps={"steps": []}, is_default=True)
        with pytest.raises(IntegrityError):
            await s.flush()
