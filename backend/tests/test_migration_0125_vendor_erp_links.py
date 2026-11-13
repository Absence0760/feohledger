"""Migration 0125 runs against a real tenant database: down, up, up again — and
builds exactly what ``create_all`` builds.

The realdb harness provisions tenants with ``create_all`` (as a fresh tenant
does in production), so ``vendor_erp_links`` exists there without the revision
running. This file captures that table, drives the revision down and up twice
through Alembic's ``Operations`` on the tenant's own connection, and compares
columns (type, nullability, default), indexes and constraints (the FK's name
and ON DELETE included), so an existing tenant migrated to head and a new one
provisioned today hold the same table.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0125_vendor_erp_links.py"
TABLE = "vendor_erp_links"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0125", REVISION)
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
            tuple(r)
            for r in await conn.execute(
                text(
                    "SELECT column_name, data_type, character_maximum_length, is_nullable, "
                    "column_default FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t"
                ),
                {"t": TABLE},
            )
        }
        indexes = {
            tuple(r)
            for r in await conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = :t"
                ),
                {"t": TABLE},
            )
        }
        constraints = {
            tuple(r)
            for r in await conn.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = to_regclass('public.' || :t)"
                ),
                {"t": TABLE},
            )
        }
    return {"columns": columns, "indexes": indexes, "constraints": constraints}


EMPTY = {"columns": set(), "indexes": set(), "constraints": set()}


@pytest.mark.asyncio
async def test_0125_matches_create_all_and_reruns(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        built = await _schema(engine)
        assert {c for c, *_ in built["columns"]} == {
            "id",
            "organization_id",
            "vendor_id",
            "realm",
            "erp_vendor_id",
            "created_at",
            "updated_at",
        }
        assert (
            "vendor_erp_links_vendor_id_fkey",
            "FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON DELETE CASCADE",
        ) in built["constraints"]
        assert (
            "uq_vendor_erp_links_vendor_realm",
            "UNIQUE (vendor_id, realm)",
        ) in built["constraints"]

        await _run(engine, mig.downgrade)
        assert await _schema(engine) == EMPTY

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _schema(engine) == built
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0125_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        assert await _schema(engine) == EMPTY
    finally:
        await engine.dispose()
