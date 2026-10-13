"""Migration 0111 runs against a real tenant database: down, up, up again — and
builds exactly what ``create_all`` builds.

The realdb harness provisions tenants with ``create_all`` (as a fresh tenant
does in production), so the schema 0111 adds exists there without the revision
running. This file captures that schema, drives the revision down and up twice
through Alembic's ``Operations`` on the tenant's own connection, and compares:
columns (type, nullability), indexes and unique constraints must match, so an
existing tenant migrated to head and a new one provisioned today are the same.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0111_commerce_inventory_sync.py"
TABLES = ("commerce_inventory_syncs", "commerce_sku_mappings")
LINE_COLUMNS = {
    ("po_line_items", "catalog_item_id"),
    ("po_line_items", "sku"),
    ("gr_line_items", "catalog_item_id"),
    ("gr_line_items", "sku"),
}


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0111", REVISION)
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


async def _schema(engine) -> dict:
    async with engine.connect() as conn:
        columns = {
            (r[0], r[1], r[2], r[3])
            for r in await conn.execute(
                text(
                    "SELECT table_name, column_name, data_type, is_nullable "
                    "FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND (table_name = ANY(:t) OR (table_name IN "
                    "('po_line_items', 'gr_line_items') AND column_name IN "
                    "('catalog_item_id', 'sku')))"
                ),
                {"t": list(TABLES)},
            )
        }
        indexes = {
            (r[0], r[1])
            for r in await conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
                    "AND (tablename = ANY(:t) OR (tablename IN ('po_line_items', "
                    "'gr_line_items') AND indexname LIKE '%catalog_item_id'))"
                ),
                {"t": list(TABLES)},
            )
        }
    return {"columns": columns, "indexes": indexes}


@pytest.mark.asyncio
async def test_0111_matches_create_all_and_reruns(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        built = await _schema(engine)
        assert {(t, c) for t, c, *_ in built["columns"]} >= LINE_COLUMNS
        assert {t for t, *_ in built["columns"]} >= set(TABLES)

        await _run(engine, mig.downgrade)
        gone = await _schema(engine)
        assert gone == {"columns": set(), "indexes": set()}

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        migrated = await _schema(engine)
        assert migrated == built
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0111_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        assert (await _schema(engine)) == {"columns": set(), "indexes": set()}
    finally:
        await engine.dispose()
