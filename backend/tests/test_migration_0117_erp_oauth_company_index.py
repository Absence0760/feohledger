"""Migration 0117 runs against a real control plane: down, up, up again.

The harness builds the control plane with ``create_all``, so the realm index
exists there without the revision ever executing. This proves the revision
creates the same index on a control plane at the previous schema, re-runs
harmlessly, reverses, refuses a control plane that already holds two tenants
on one company (naming neither), and no-ops on a tenant database. Always left
upgraded so later tests see the current schema.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0117_erp_oauth_company_index.py"
INDEX = "uq_organizations_erp_oauth_company"


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


async def _indexdef(engine) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": INDEX}
            )
        ).scalar_one_or_none()


async def test_0117_builds_the_same_index_create_all_does(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        built = await _indexdef(engine)  # what create_all made from the model
        assert built is not None and "UNIQUE" in built
        await _run(engine, mig.downgrade)
        assert await _indexdef(engine) is None
        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent: IF NOT EXISTS
        assert await _indexdef(engine) == built
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


async def test_0117_refuses_two_tenants_on_one_company(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    oauth = {"erp": {"oauth": {"provider": "quickbooks_online", "external_tenant_id": "dup-co"}}}
    a, b = realdb.info("a").org_id, realdb.info("b").org_id
    try:
        await _run(engine, mig.downgrade)
        async with engine.begin() as conn:
            for org_id in (a, b):
                await conn.execute(
                    text(
                        "UPDATE organizations SET settings = COALESCE(settings, '{}'::jsonb) "
                        "|| CAST(:s AS jsonb) WHERE id = :id"
                    ),
                    {"s": json.dumps(oauth), "id": org_id},
                )
        with pytest.raises(RuntimeError, match="more than one tenant"):
            await _run(engine, mig.upgrade)
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE organizations SET settings = settings - 'erp' WHERE id IN (:a, :b)"),
                {"a": a, "b": b},
            )
        await _run(engine, mig.upgrade)
        await engine.dispose()


async def test_0117_is_a_no_op_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        assert await _indexdef(engine) is None
    finally:
        await engine.dispose()
