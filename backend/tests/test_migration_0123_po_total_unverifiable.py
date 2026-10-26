"""Migration 0123 runs against a real tenant database: down, up, up again.

The realdb harness builds tenants with ``create_all``, so
``purchase_orders.total_unverifiable_reason`` exists there without the revision
ever executing. This proves the revision adds it to a tenant at the previous
schema, leaves existing rows NULL (no backfill), re-runs harmlessly and no-ops on
the control plane. Always left upgraded.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0123_po_total_unverifiable.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0123", REVISION)
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


async def _column_exists(engine) -> bool:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_name = "
                    "'purchase_orders' AND column_name = 'total_unverifiable_reason'"
                )
            )
        ).scalar() is not None


@pytest.mark.asyncio
async def test_0123_adds_the_column_without_backfill(realdb):
    from app.database import _make_tenant_url
    from app.models.procurement import PurchaseOrder

    org_id = realdb.info("a").org_id
    async with realdb.sessionmaker("a")() as s:
        po = PurchaseOrder(
            po_number="PO-MIG-0123", total=100, currency="USD", organization_id=org_id
        )
        s.add(po)
        await s.commit()
        po_id = po.id

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)
        assert not await _column_exists(engine)
        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _column_exists(engine)
        async with engine.connect() as conn:
            reason = (
                await conn.execute(
                    text("SELECT total_unverifiable_reason FROM purchase_orders WHERE id = :id"),
                    {"id": po_id},
                )
            ).scalar()
        assert reason is None
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0123_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        assert not await _column_exists(engine)
    finally:
        await engine.dispose()
