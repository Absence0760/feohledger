"""``invoices.po_number`` is indexed, and the budget PO-relief query can use it — migration 0137.

``services/budget_service._invoice_po_attribution`` (the relief that stops an
invoiced PO counting in both ``committed`` and ``actual``) joins ``invoices`` to
``purchase_orders`` on ``po_number`` and narrows the invoice side with
``Invoice.po_number IN (<numbers these budgets' converted POs hold>)``, on
``GET /budgets/check`` before every requisition submit. Until 0137 nothing
indexed the invoice side of that join.

What was measured: nothing was timed. This file pins the SHAPE (the index
exists under one name on both build paths, and the relief predicate is an
``Index Cond`` on it), not a latency. The real-volume plan choice over a
40k-invoice tenant was not taken; see ``docs/followups.md`` history and the
migration docstring.

What this file pins:

1. **The model and the migration name the same index**, so a tenant built by
   ``create_all`` and one brought up by Alembic match.
2. **The revision applies to a real tenant DB, is idempotent, and reverses.**
3. **The relief query's invoice predicate is served by the index.** Asserted
   the way ``tests/test_invoice_budget_dimension_indexes.py`` learned to
   (its docstring, point 4): every OTHER droppable index on ``invoices`` is
   dropped inside a rolled-back transaction and seq scans are off, so the
   planner is not choosing between two viable index scans on leftover
   statistics. Two more things make the answer deterministic here:

   * hash and merge joins are off too. Either can consume an unconditioned
     full scan of the index (a merge join wants its order), which would put the
     index's NAME in the plan without it serving the predicate. Only a nested
     loop is left, and the assertion demands an ``Index Cond`` on
     ``po_number``.
   * a few thousand invoices with distinct numbers are inserted in the same
     rolled-back transaction, so the planner's row estimate for ``invoices``
     comes from the table's real size rather than from whatever the shared
     harness left behind. Nothing is ANALYZEd (that would leak ``reltuples``
     past the rollback).

Real-Postgres harness (``realdb``) for 2 and 3.
"""

from __future__ import annotations

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.invoice import Invoice
from app.services.budget_service import _invoice_po_attribution

TENANT = "a"
INDEX = "ix_invoices_po_number"
SEEDED_INVOICES = 3000

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0137_invoice_po_number_index.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0137", REVISION)
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


async def _index_def(engine) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :n"
                ),
                {"n": INDEX},
            )
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# 1. model <-> migration
# ---------------------------------------------------------------------------


def test_the_revision_and_the_model_name_the_same_index():
    mig = _load()
    assert mig.INDEX_NAME == INDEX
    assert mig.revision == "0137_invoice_po_number_index"
    assert len(mig.revision) <= 32  # alembic_version.version_num is VARCHAR(32)
    model = {ix.name: ix for ix in Invoice.__table__.indexes}
    assert INDEX in model
    assert [c.name for c in model[INDEX].columns] == ["po_number"]
    # po_number is not unique: several POs and several invoices may share one.
    assert not model[INDEX].unique


# ---------------------------------------------------------------------------
# 2. the revision against a real tenant DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_0137_adds_the_index_on_a_tenant_and_reverses(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info(TENANT).db_name))
    try:
        # create_all (the harness, and every freshly provisioned tenant) built it.
        fresh = await _index_def(engine)
        assert fresh is not None
        assert "(po_number)" in fresh and "UNIQUE" not in fresh

        await _run(engine, mig.downgrade)
        assert await _index_def(engine) is None
        await _run(engine, mig.downgrade)  # dropping an absent index is a no-op

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # IF NOT EXISTS: a re-run is a no-op
        # The migration builds exactly what create_all built.
        assert await _index_def(engine) == fresh
    finally:
        # Always leave the tenant upgraded for the rest of the run.
        await _run(engine, mig.upgrade)
        await engine.dispose()


# ---------------------------------------------------------------------------
# 3. the relief query can use the index
# ---------------------------------------------------------------------------


async def _other_invoice_indexes(session) -> list[str]:
    """Every droppable index on ``invoices`` except the one under test.
    Constraint-backed indexes are kept: ``DROP INDEX`` refuses them, and none
    is on ``po_number``."""
    rows = await session.execute(
        text(
            """
            SELECT c.relname
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            WHERE i.indrelid = 'invoices'::regclass
              AND c.relname <> :keep
              AND NOT EXISTS (
                  SELECT 1 FROM pg_constraint con WHERE con.conindid = i.indexrelid
              )
            """
        ),
        {"keep": INDEX},
    )
    return list(rows.scalars().all())


def _index_node_with_cond(plan_lines: list[str]) -> str | None:
    """The plan text of the node scanning ``INDEX`` plus its detail lines, if
    one of those carries an ``Index Cond`` on ``po_number``."""
    for i, line in enumerate(plan_lines):
        if INDEX not in line:
            continue
        indent = len(line) - len(line.lstrip())
        node = [line]
        for detail in plan_lines[i + 1 :]:
            if detail.lstrip().startswith("->") or len(detail) - len(detail.lstrip()) <= indent:
                break
            node.append(detail)
        if any("Index Cond" in d and "po_number" in d for d in node):
            return "\n".join(node)
    return None


@pytest.mark.asyncio
async def test_relief_attribution_uses_the_po_number_index(realdb):
    """The invoice side of ``_invoice_po_attribution`` is an ``Index Cond`` on
    ``ix_invoices_po_number`` once the planner has no other way in. Without the
    index (or with the predicate reshaped so it no longer applies) the only
    path left is the disabled seq scan, and this fails."""
    sql = str(
        select(_invoice_po_attribution([uuid.uuid4()])).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        try:
            s.add_all(
                Invoice(
                    id=uuid.uuid4(),
                    invoice_number=f"IDX-PROBE-{n}",
                    vendor_name="Index Probe Supply",
                    amount=Decimal("10.00"),
                    currency="USD",
                    status="paid",
                    po_number=f"IDX-PO-{n}",
                    organization_id=org_id,
                )
                for n in range(SEEDED_INVOICES)
            )
            await s.flush()
            await s.execute(text("SET LOCAL enable_seqscan = off"))
            await s.execute(text("SET LOCAL enable_hashjoin = off"))
            await s.execute(text("SET LOCAL enable_mergejoin = off"))
            competitors = await _other_invoice_indexes(s)
            assert competitors, "expected other indexes on invoices to isolate against"
            for name in competitors:
                await s.execute(text(f'DROP INDEX "{name}"'))
            plan = list((await s.execute(text(f"EXPLAIN {sql}"))).scalars().all())
        finally:
            # DDL and the seeded rows are transactional, so this restores both.
            # The tenant DBs are shared across the tests in this process: a
            # leaked DROP would silently de-index the rest of the run.
            await s.rollback()
        restored_rows = await s.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'invoices'")
        )
        restored = set(restored_rows.scalars())

    plan_text = "\n".join(plan)
    assert _index_node_with_cond(plan) is not None, (
        f"relief attribution did not use {INDEX} with an Index Cond on po_number:\n{plan_text}"
    )
    assert set(competitors) <= restored, "rollback did not restore the dropped indexes"
