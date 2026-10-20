"""Migration 0116 runs against a real tenant database: down, up, and up again.

The realdb harness builds tenants with ``create_all``, so
``exceptions.finding_fingerprint`` exists there without the revision ever
executing. Nothing else proves 0116 adds it to a tenant at the previous schema
(at the width the SHA-256 hex digest needs, keeping the rows already there),
re-runs harmlessly, reverses, and no-ops on the control plane. Always left
upgraded so later tests see the current schema.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.exception import Exception as ExceptionRow

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0116_exception_fingerprint.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0116", REVISION)
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


async def _column(engine) -> tuple | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT data_type, character_maximum_length, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'exceptions' "
                    "AND column_name = 'finding_fingerprint'"
                )
            )
        ).one_or_none()


@pytest.mark.asyncio
async def test_0116_adds_the_column_to_a_tenant_at_the_previous_schema(realdb):
    from app.database import _make_tenant_url

    async with realdb.sessionmaker("a")() as s:
        row = ExceptionRow(
            organization_id=realdb.info("a").org_id,
            exception_type="duplicate",
            severity="warning",
            description="Possible duplicate",
            status="dismissed",
        )
        s.add(row)
        await s.commit()
        row_id: uuid.UUID = row.id

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)
        assert await _column(engine) is None

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent: IF NOT EXISTS
        data_type, width, nullable = await _column(engine)
        assert (data_type, width, nullable) == ("character varying", 64, "YES")

        # Nothing is backfilled: a row closed before 0116 recorded no judgement,
        # so it suppresses nothing.
        async with engine.connect() as conn:
            fingerprint = (
                await conn.execute(
                    text("SELECT finding_fingerprint FROM exceptions WHERE id = :id"),
                    {"id": row_id},
                )
            ).scalar_one()
        assert fingerprint is None
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0116_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        await _run(engine, mig.upgrade)
        assert await _column(engine) is None
    finally:
        await engine.dispose()
