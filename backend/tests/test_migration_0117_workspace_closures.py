"""Migration 0117 runs against a real control plane: up, up again, and a
downgrade that deliberately keeps the table.

The realdb harness builds its control plane with ``create_all``, so
``workspace_closures`` exists there without the revision ever executing.
Nothing else proves 0117 creates it on a control plane at the previous schema,
re-runs harmlessly, keeps its rows on a downgrade, and no-ops on a tenant
database. Always left
upgraded so later tests see the current schema. Whole-schema equality with the
model is `test_schema_build_parity.py`'s job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0117_workspace_closures.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0117", REVISION)
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


async def _table_exists(engine) -> bool:
    async with engine.connect() as conn:
        return bool(
            (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'workspace_closures'"
                    )
                )
            ).scalar()
        )


@pytest.mark.asyncio
async def test_0117_creates_the_table_on_a_control_plane_at_the_previous_schema(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        # The previous schema: the table absent.
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS workspace_closures"))
        assert not await _table_exists(engine)

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent: IF NOT EXISTS
        assert await _table_exists(engine)
        async with engine.connect() as conn:
            unique = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM pg_constraint "
                        "WHERE conname = 'workspace_closures_organization_id_key'"
                    )
                )
            ).scalar()
            fks = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint c JOIN pg_class t "
                        "ON t.oid = c.conrelid WHERE t.relname = 'workspace_closures' "
                        "AND c.contype = 'f'"
                    )
                )
            ).scalar()
        # One closure per organisation, and no foreign key: both rows it names
        # are deleted by the closure it records.
        assert unique == 1
        assert fks == 0
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0117_downgrade_keeps_the_closure_records(realdb):
    """Each row is the only evidence a deleted workspace's closure was asked
    for, and the operator's backup queue: a downgrade must not drop them."""
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        assert await _table_exists(engine)
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0117_is_a_no_op_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.upgrade)
        assert not await _table_exists(engine)
        await _run(engine, mig.downgrade)
    finally:
        await engine.dispose()
