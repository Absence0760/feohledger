"""Migration 0118 runs against a real tenant database: down, up, up again.

The realdb harness builds tenants with ``create_all``, so
``contracts.activated_at`` exists there without the revision ever executing.
This proves the revision adds it to a tenant at the previous schema, backfills
it from the evidence that exists (the first ``contract.active`` audit row, else
a status that says the contract has been in force), invents nothing for a draft,
re-runs harmlessly and no-ops on the control plane. Always left upgraded.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.workflow import AuditLog
from tests.test_workspace_closure import _insert_row

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0118_contract_activated_at.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0118", REVISION)
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


async def _activated(engine, contract_id):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT activated_at FROM contracts WHERE id = :id"), {"id": contract_id}
            )
        ).scalar()


@pytest.mark.asyncio
async def test_0118_adds_and_backfills_activated_at(realdb):
    from app.database import _make_tenant_url

    org_id = realdb.info("a").org_id
    first_active = datetime(2026, 3, 1, tzinfo=UTC)
    async with realdb.sessionmaker("a")() as s:
        # Activated through the route, then cancelled: only the audit row knows.
        cancelled = await _insert_row(s, "contracts", org_id, status="cancelled")
        for offset in (0, 30):
            await s.execute(
                insert(AuditLog).values(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    action="contract.active",
                    entity_type="contract",
                    entity_id=cancelled,
                    created_at=first_active + timedelta(days=offset),
                )
            )
        # In force with no audit row (seeded): its status says so.
        seeded = await _insert_row(s, "contracts", org_id, status="active")
        # Never in force.
        draft = await _insert_row(s, "contracts", org_id, status="draft")
        await s.commit()

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)
        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent

        assert await _activated(engine, cancelled) == first_active
        assert await _activated(engine, seeded) is not None
        assert await _activated(engine, draft) is None
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0118_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        async with engine.connect() as conn:
            exists = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'contracts' AND column_name = 'activated_at'"
                    )
                )
            ).scalar()
        assert exists is None
    finally:
        await engine.dispose()
