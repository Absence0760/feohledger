"""Migration 0133 — ``invoices.split_po_ids`` (decisions §433).

The multi-PO split's PO set moves out of the ``po_match`` snapshot a warning
refresh rewrites, into a column only the split resolver writes. Pinned here:

  - the chain and the id length;
  - model ↔ migration parity: the realdb harness builds tenants with
    ``create_all`` (as every freshly provisioned tenant is), so the column there
    comes from the model and must match the DDL's type;
  - the backfill fills an invoice whose ``po_match`` still names ``po_ids`` and
    leaves every other row (no snapshot, a single-PO match, an empty list, an
    already-filled column) as it was; a re-run is a no-op;
  - a split a refresh already rewrote is recovered from its ``auto_resolved``
    agent decision's combined reference, resolved in the invoice's entity; an
    escalated decision recovers nothing; the restated literals are the app's;
  - downgrade drops the column and re-runs harmlessly; the control plane is
    untouched.

Always left upgraded so later tests see the current schema.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.agent_decision import AgentDecision
from app.models.entity import Entity
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.procurement import PurchaseOrder

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0133_invoice_split_po_ids.py"
TENANT = "a"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0133", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(engine, fn) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    async with engine.begin() as conn:

        def _do(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                fn()

        await conn.run_sync(_do)


async def _column_type(engine) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'invoices' "
                    "AND column_name = 'split_po_ids'"
                )
            )
        ).scalar_one_or_none()


def test_the_chain_and_the_id():
    mig = _load()
    assert mig.revision == "0133_invoice_split_po_ids"
    assert mig.down_revision == "0132_invoice_erp_doc_id_idx"
    assert len(mig.revision) <= 32
    assert REVISION.stem == mig.revision


def test_the_model_carries_the_column_the_ddl_adds():
    mig = _load()
    column = Invoice.__table__.c[mig.COLUMN]
    assert isinstance(column.type, JSONB)
    assert column.nullable


def test_the_restated_literals_are_the_apps():
    from app.services.exception_agents.base import ACTION_AUTO_RESOLVED
    from app.services.exception_agents.resolvers.multi_po_split import MultiPOSplitResolver

    mig = _load()
    assert mig.SPLIT_AGENT_TYPE == MultiPOSplitResolver.agent_type
    assert mig.ACTION_AUTO_RESOLVED == ACTION_AUTO_RESOLVED


async def _seed(realdb, po_match) -> uuid.UUID:
    async with realdb.sessionmaker(TENANT)() as s:
        inv = Invoice(
            invoice_number=f"INV-0133-{uuid.uuid4().hex[:8]}",
            vendor_name="Split Supplies",
            amount=Decimal("100.00"),
            currency="USD",
            po_number="PO-A,PO-B",
            po_match=po_match,
            status=InvoiceStatus.approved,
            organization_id=realdb.info(TENANT).org_id,
            entity_id=(await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one(),
        )
        s.add(inv)
        await s.commit()
        return inv.id


async def _split_ids(engine, invoice_id):
    async with engine.connect() as conn:
        value = (
            await conn.execute(
                text("SELECT split_po_ids FROM invoices WHERE id = :i"), {"i": invoice_id}
            )
        ).scalar_one()
    return json.loads(value) if isinstance(value, str) else value


@pytest.mark.asyncio
async def test_0133_adds_the_column_backfills_splits_and_reverses(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    a, b, kept = (str(uuid.uuid4()) for _ in range(3))
    split = await _seed(
        realdb, {"status": "matched", "match_type": "multi-po-split", "po_ids": [a, b]}
    )
    single = await _seed(realdb, {"status": "matched", "po_id": a})
    empty = await _seed(realdb, {"status": "matched", "po_ids": []})
    none = await _seed(realdb, None)
    filled = await _seed(realdb, {"status": "matched", "po_ids": [a]})

    engine = create_async_engine(_make_tenant_url(realdb.info(TENANT).db_name))
    try:
        assert await _column_type(engine) == "jsonb"  # create_all built it from the model

        await _run(engine, mig.downgrade)
        assert await _column_type(engine) is None
        await _run(engine, mig.downgrade)  # dropping an absent column is a no-op

        # Every row is now as a pre-0133 tenant holds it: only the snapshot
        # can name a split's POs.
        await _run(engine, mig.upgrade)
        assert await _column_type(engine) == "jsonb"
        assert await _split_ids(engine, split) == [a, b]
        assert await _split_ids(engine, filled) == [a]
        assert await _split_ids(engine, single) is None
        assert await _split_ids(engine, empty) is None
        assert await _split_ids(engine, none) is None

        # A column the resolver already filled is never overwritten by a re-run.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE invoices SET split_po_ids = CAST(:v AS jsonb) WHERE id = :i"),
                {"v": json.dumps([kept]), "i": filled},
            )
        await _run(engine, mig.upgrade)
        assert await _split_ids(engine, split) == [a, b]
        assert await _split_ids(engine, filled) == [kept]
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


async def _decided_split(realdb, s, invoice_id, combined_ref, action) -> None:
    """The decision row the split agent writes, as it writes it."""
    org_id = realdb.info(TENANT).org_id
    exc = APException(
        invoice_id=invoice_id,
        exception_type="po_mismatch",
        severity="error",
        description="Referenced PO not found",
        status="resolved",
        organization_id=org_id,
    )
    s.add(exc)
    await s.flush()
    s.add(
        AgentDecision(
            exception_id=exc.id,
            invoice_id=invoice_id,
            exception_type="po_mismatch",
            action_taken=action,
            confidence=Decimal("0.9"),
            changes={"po_number": {"old": "PO-GONE", "new": combined_ref}},
            agent_type="multi_po_split_v1",
            organization_id=org_id,
        )
    )


@pytest.mark.asyncio
async def test_0133_recovers_a_refreshed_split_from_its_agent_decision(realdb):
    """A split refreshed before 0133 has lost `po_match.po_ids`; its
    `auto_resolved` decision still names the combined reference it wrote."""
    from app.database import _make_tenant_url

    mig = _load()
    tag = uuid.uuid4().hex[:6]
    n1, n2 = f"PO-{tag}-1", f"PO-{tag}-2"
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        home = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        sibling = Entity(name=f"Sibling {tag}", slug=f"sibling-{tag}", organization_id=org_id)
        s.add(sibling)
        await s.flush()
        pos = {}
        for key, number, entity_id in (
            ("one", n1, home),
            ("two", n2, home),
            ("elsewhere", n1, sibling.id),  # same number, another entity
        ):
            po = PurchaseOrder(
                po_number=number,
                total=Decimal("50.00"),
                currency="USD",
                status="open",
                organization_id=org_id,
                entity_id=entity_id,
            )
            s.add(po)
            await s.flush()
            pos[key] = str(po.id)
        await s.commit()

    refreshed = await _seed(realdb, {"status": "no_po", "issues": []})
    escalated = await _seed(realdb, {"status": "no_po", "issues": []})
    async with realdb.sessionmaker(TENANT)() as s:
        await _decided_split(realdb, s, refreshed, f"{n1},{n2}", "auto_resolved")
        await _decided_split(realdb, s, escalated, f"{n1},{n2}", "escalated")
        await s.commit()

    engine = create_async_engine(_make_tenant_url(realdb.info(TENANT).db_name))
    try:
        await _run(engine, mig.downgrade)
        await _run(engine, mig.upgrade)
        assert await _split_ids(engine, refreshed) == sorted([pos["one"], pos["two"]])
        assert await _split_ids(engine, escalated) is None
        await _run(engine, mig.upgrade)  # a re-run changes nothing
        assert await _split_ids(engine, refreshed) == sorted([pos["one"], pos["two"]])
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0133_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        await _run(engine, mig.upgrade)
        assert await _column_type(engine) is None
    finally:
        await engine.dispose()
