"""Migration 0109 runs against a real tenant database: down, up, and up again.

The realdb harness builds tenants with `create_all`, so the columns 0109 adds
exist there without the revision ever executing — nothing else proves its DDL
runs, re-runs (`IF NOT EXISTS`), reverses, and no-ops on the control plane.
The revision is driven through Alembic's `Operations` on the test tenant's own
connection, and always left upgraded so later tests see the current schema.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0109_goods_receipt_entry.py"

_COLUMNS = (
    ("goods_receipts", "source"),
    ("goods_receipts", "recorded_by_user_id"),
    ("goods_receipts", "idempotency_key"),
    ("gr_line_items", "po_line_item_id"),
)
_INDEXES = ("uq_goods_receipts_org_idempotency_key", "ix_gr_line_items_po_line_item_id")


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0109", REVISION)
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


async def _schema(engine) -> tuple[set, set]:
    async with engine.connect() as conn:
        cols = {
            (r[0], r[1])
            for r in await conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name IN ('goods_receipts', 'gr_line_items')"
                )
            )
        }
        idx = {
            r[0]
            for r in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
        }
    return cols, idx


@pytest.mark.asyncio
async def test_0109_downgrades_upgrades_and_reruns_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)
        cols, idx = await _schema(engine)
        assert not set(_COLUMNS) & cols
        assert not set(_INDEXES) & idx

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent: IF NOT EXISTS throughout
        cols, idx = await _schema(engine)
        assert set(_COLUMNS) <= cols
        assert set(_INDEXES) <= idx

        async with engine.connect() as conn:
            definition = (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = 'uq_goods_receipts_org_idempotency_key'"
                    )
                )
            ).scalar_one()
        assert "UNIQUE" in definition
        assert "idempotency_key IS NOT NULL" in definition
    finally:
        # Whatever happened above, leave the tenant on the current schema.
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0109_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        cols, _ = await _schema(engine)
        assert cols == set()
    finally:
        await engine.dispose()
