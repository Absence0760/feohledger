"""Migration 0114 runs against a real tenant database: down, up, and up again.

The realdb harness builds tenants with ``create_all``, so ``gl_accounts.code``
is already ``varchar(100)`` there without the revision ever executing. Nothing
else proves 0114 widens a tenant still at 50, re-runs harmlessly, narrows back
when every code fits, refuses to narrow when one would be truncated (two long
QuickBooks paths would collapse into each other), and no-ops on the control
plane. The revision is driven through Alembic's ``Operations`` on the test
tenant's own connection, and always left upgraded so later tests see the
current schema.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.gl_account import GLAccount

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0114_gl_code_width.py"

#: 51 characters, from the QuickBooks sandbox company itself.
LONG_PATH = "Landscaping Services:Job Materials:Decks and Patios"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0114", REVISION)
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


async def _widths(engine) -> dict[str, int]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'gl_accounts' "
                "AND column_name IN ('code', 'parent_code')"
            )
        )
        return {name: width for name, width in rows}


@pytest.mark.asyncio
async def test_0114_widens_a_tenant_at_50_and_reruns(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)  # nothing over 50: narrows cleanly
        assert await _widths(engine) == {"code": 50, "parent_code": 50}

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent: re-widening to 100 is a no-op
        assert await _widths(engine) == {"code": 100, "parent_code": 100}
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["code", "parent_code"])
async def test_0114_downgrade_refuses_to_truncate_a_long_code(realdb, column):
    from app.database import _make_tenant_url

    async with realdb.sessionmaker("a")() as s:
        fields = {"code": "6100", "name": "Decks and Patios", column: LONG_PATH}
        s.add(GLAccount(organization_id=realdb.info("a").org_id, **fields))
        await s.commit()

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        with pytest.raises(RuntimeError, match="longer than 50"):
            await _run(engine, mig.downgrade)
        # Refused before any DDL: the column and the row are untouched.
        assert await _widths(engine) == {"code": 100, "parent_code": 100}
        async with engine.connect() as conn:
            kept = (await conn.execute(text(f"SELECT {column} FROM gl_accounts"))).scalar_one()
        assert kept == LONG_PATH
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0114_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        assert await _widths(engine) == {}
    finally:
        await engine.dispose()
