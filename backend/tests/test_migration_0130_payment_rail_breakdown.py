"""Migration 0130 runs against a real tenant database: down, up, up again — and
builds exactly what ``create_all`` builds (decisions §396).

The realdb harness provisions tenants with ``create_all`` (as a fresh tenant
does in production), so ``payments.rail_breakdown`` and its CHECK exist there
without the revision running. This file captures both, drives the revision down
and up twice through Alembic's ``Operations`` on the tenant's own connection,
and compares them, so an existing tenant migrated to head and a new one
provisioned today hold the same column and constraint. It also pins what the
CHECK is for: a breakdown that is not an array is refused by the database, so
the 1099 aggregate's ``jsonb_array_elements`` can never meet one. And that no
payment is backfilled (§141).
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0130_payment_rail_breakdown.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0130", REVISION)
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
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name = 'payments' AND column_name = 'rail_breakdown'"
                )
            )
        }
        constraints = {
            tuple(r)
            for r in await conn.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = to_regclass('public.payments') "
                    "AND conname = 'ck_payments_rail_breakdown_array'"
                )
            )
        }
    return {"columns": columns, "constraints": constraints}


EMPTY = {"columns": set(), "constraints": set()}


def test_revision_chain_and_id_width():
    mig = _load()
    assert mig.revision == "0130_payment_rail_breakdown"
    assert mig.down_revision == "0129_invoice_approver_id"
    assert len(mig.revision) <= 32  # alembic_version.version_num is VARCHAR(32)


@pytest.mark.asyncio
async def test_0130_matches_create_all_and_reruns(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        built = await _schema(engine)
        assert built["columns"] == {("rail_breakdown", "jsonb", "YES", None)}
        [(_, definition)] = built["constraints"]
        assert "jsonb_typeof(rail_breakdown)" in definition and "'array'" in definition

        await _run(engine, mig.downgrade)
        assert await _schema(engine) == EMPTY

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _schema(engine) == built
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0130_backfills_nothing_and_refuses_a_breakdown_that_is_not_an_array(realdb):
    """A payment recorded before 0130 keeps a NULL breakdown: nothing stored
    says how it split. And the CHECK holds the column to an array."""
    from app.database import _make_tenant_url

    mig = _load()
    info = realdb.info("a")
    engine = create_async_engine(_make_tenant_url(info.db_name))
    inv_id, pay_id = uuid.uuid4(), uuid.uuid4()
    try:
        await _run(engine, mig.downgrade)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO invoices (id, correlation_id, organization_id, invoice_number, "
                    "vendor_name, amount, currency, status, created_at, updated_at) VALUES "
                    "(:i, :c, :o, :n, 'v', 250.00, 'USD', 'paid', now(), now())"
                ),
                {"i": inv_id, "c": uuid.uuid4(), "o": info.org_id, "n": f"MIG-{inv_id.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO payments (id, invoice_id, amount, method, status, "
                    "settled_amount_unstorable, created_at, updated_at) "
                    "VALUES (:p, :i, 250.00, NULL, 'completed', false, now(), now())"
                ),
                {"p": pay_id, "i": inv_id},
            )
        await _run(engine, mig.upgrade)
        async with engine.connect() as conn:
            breakdown = (
                await conn.execute(
                    text("SELECT rail_breakdown FROM payments WHERE id = :p"), {"p": pay_id}
                )
            ).scalar_one()
        assert breakdown is None

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE payments SET rail_breakdown = "
                        """'{"method": "card", "amount": "250.00"}'::jsonb WHERE id = :p"""
                    ),
                    {"p": pay_id},
                )
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE payments SET rail_breakdown = "
                    """'[{"method": "card", "amount": "250.00"}]'::jsonb WHERE id = :p"""
                ),
                {"p": pay_id},
            )
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM payments WHERE id = :p"), {"p": pay_id})
            await conn.execute(text("DELETE FROM invoices WHERE id = :i"), {"i": inv_id})
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0130_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        assert await _schema(engine) == EMPTY
    finally:
        await engine.dispose()
