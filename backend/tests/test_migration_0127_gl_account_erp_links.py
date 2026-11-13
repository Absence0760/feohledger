"""Migration 0127 runs against a real tenant database: down, up, up again — and
builds exactly what ``create_all`` builds.

The realdb harness provisions tenants with ``create_all`` (as a fresh tenant
does in production), so ``gl_account_erp_links`` exists there without the revision
running. This file captures that table, drives the revision down and up twice
through Alembic's ``Operations`` on the tenant's own connection, and compares
columns (type, nullability, default), indexes and constraints (the FK's name
and ON DELETE, and the CHECK, included), so an existing tenant migrated to head and a new one
provisioned today hold the same table.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0127_gl_account_erp_links.py"
TABLE = "gl_account_erp_links"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0127", REVISION)
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
async def test_0127_matches_create_all_and_reruns(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        built = await _schema(engine)
        assert {c for c, *_ in built["columns"]} == {
            "id",
            "organization_id",
            "gl_account_id",
            "realm",
            "erp_account_id",
            "erp_link_confirmed_at",
            "erp_link_confirmed_by",
            "erp_link_candidates",
            "created_at",
            "updated_at",
        }
        assert (
            "gl_account_erp_links_gl_account_id_fkey",
            "FOREIGN KEY (gl_account_id) REFERENCES gl_accounts(id) ON DELETE CASCADE",
        ) in built["constraints"]
        assert (
            "uq_gl_account_erp_links_account_realm",
            "UNIQUE (gl_account_id, realm)",
        ) in built["constraints"]
        assert "ck_gl_account_erp_links_state" in {name for name, _ in built["constraints"]}
        assert "ix_gl_account_erp_links_organization_id_realm" in {
            name for name, _ in built["indexes"]
        }

        await _run(engine, mig.downgrade)
        assert await _schema(engine) == EMPTY

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _schema(engine) == built
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0127_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        assert await _schema(engine) == EMPTY
    finally:
        await engine.dispose()


def test_0127_chains_after_0126():
    mig = _load()
    assert (mig.revision, mig.down_revision) == (
        "0127_gl_account_erp_links",
        "0126_uploader_audit_backfill",
    )
    assert len(mig.revision) <= 32
