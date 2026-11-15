"""Migration 0134 — an expense records who created it (decisions §434).

The realdb harness builds tenants with ``create_all``, so the column exists
there without the revision ever executing; that is the fresh-tenant half of the
parity this pins (the model must declare the column 0134 adds, with the same
type and nullability). The other half: the revision adds the column to a tenant
at the previous schema, leaves existing rows NULL (no backfill, §141), re-runs
harmlessly, drops it on downgrade, and no-ops on the control plane. Always left
upgraded, so later tests see the head schema.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.expense import Expense

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0134_expense_creator.py"
TENANT = "a"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0134", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chains_after_0133_and_fits_the_version_column():
    mig = _load()
    assert mig.down_revision == "0133_invoice_split_po_ids"
    assert len(mig.revision) <= 32
    assert REVISION.stem == mig.revision


def test_the_model_carries_the_column_the_migration_adds():
    """Fresh tenants are built by ``create_all``, not Alembic."""
    mig = _load()
    assert "created_by_user_id uuid" in mig.ADD_COLUMN_SQL
    assert "IF NOT EXISTS" in mig.ADD_COLUMN_SQL
    assert "IF EXISTS" in mig.DROP_COLUMN_SQL
    column = Expense.__table__.columns["created_by_user_id"]
    assert column.nullable
    assert not column.foreign_keys  # control-plane id in a tenant table


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
        row = (
            await conn.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'expenses' "
                    "AND column_name = 'created_by_user_id'"
                )
            )
        ).one_or_none()
    return tuple(row) if row is not None else None


@pytest.mark.asyncio
async def test_0134_adds_the_column_on_a_tenant_without_backfill_and_reverses(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info(TENANT).db_name))
    expense_id = uuid.uuid4()
    try:
        # create_all (the harness, and every fresh tenant) built the same column.
        assert await _column(engine) == ("uuid", "YES")

        await _run(engine, mig.downgrade)
        assert await _column(engine) is None
        await _run(engine, mig.downgrade)  # dropping an absent column is a no-op

        # A line written at the previous schema, then the upgrade.
        async with engine.begin() as conn:
            entity_id = (
                await conn.execute(text("SELECT id FROM entities WHERE is_default LIMIT 1"))
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO expenses (id, expense_date, amount, currency, payment_method, "
                    "status, reimbursable, organization_id, entity_id, created_at, updated_at) "
                    "VALUES (:id, :d, :a, 'USD', 'out_of_pocket', 'draft', true, :org, :ent, "
                    "now(), now())"
                ),
                {
                    "id": expense_id,
                    "d": date(2026, 6, 1),
                    "a": Decimal("10.00"),
                    "org": realdb.info(TENANT).org_id,
                    "ent": entity_id,
                },
            )

        await _run(engine, mig.upgrade)
        assert await _column(engine) == ("uuid", "YES")
        await _run(engine, mig.upgrade)  # a second upgrade changes nothing
        assert await _column(engine) == ("uuid", "YES")
    finally:
        # The model carries the column, so the shared test tenant must too.
        await _run(engine, mig.upgrade)
        await engine.dispose()

    # No honest author to recover: the pre-existing line stays NULL (§141).
    async with realdb.sessionmaker(TENANT)() as s:
        row = (await s.execute(select(Expense).where(Expense.id == expense_id))).scalar_one()
        assert row.created_by_user_id is None


@pytest.mark.asyncio
async def test_0134_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        async with engine.connect() as conn:
            has_expenses = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'expenses'"
                    )
                )
            ).scalar()
        assert has_expenses is None  # the gate's premise
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        await _run(engine, mig.upgrade)
        assert await _column(engine) is None
    finally:
        await engine.dispose()
