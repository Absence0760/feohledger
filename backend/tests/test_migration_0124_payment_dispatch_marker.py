"""Migration 0124 runs against a real tenant database: down, up, up again.

The realdb harness builds tenants with ``create_all``, so
``payments.dispatch_attempted_at`` exists there without the revision ever
executing. This proves the revision adds it to a tenant at the previous schema,
stamps exactly the rows a crashed dispatch pass can leave behind (``pending`` on
an ``executing`` run) and nothing else, re-runs harmlessly without moving a
stamp, and no-ops on the control plane. Always left upgraded.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0124_payment_dispatch_marker.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0124", REVISION)
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
                    "'payments' AND column_name = 'dispatch_attempted_at'"
                )
            )
        ).scalar() is not None


async def _stamps(engine, ids) -> dict:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, dispatch_attempted_at FROM payments WHERE id = ANY(:ids)"),
                {"ids": list(ids)},
            )
        ).all()
    return {row[0]: row[1] for row in rows}


@pytest.mark.asyncio
async def test_0124_adds_the_column_and_stamps_only_a_crashed_pass(realdb):
    from app.database import _make_tenant_url
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.payment import Payment, PaymentRun

    org_id = realdb.info("a").org_id
    async with realdb.sessionmaker("a")() as s:
        invoices = [
            Invoice(
                organization_id=org_id,
                invoice_number=f"MIG-0124-{n}",
                vendor_name="Migration Vendor",
                amount=Decimal("10.00"),
                currency="USD",
                status=InvoiceStatus.approved,
            )
            for n in range(3)
        ]
        stuck = PaymentRun(organization_id=org_id, status="executing")
        draft = PaymentRun(organization_id=org_id, status="draft")
        s.add_all([*invoices, stuck, draft])
        await s.flush()
        in_doubt = Payment(
            invoice_id=invoices[0].id,
            payment_run_id=stuck.id,
            amount=Decimal("10.00"),
            status="pending",
        )
        recorded = Payment(
            invoice_id=invoices[1].id,
            payment_run_id=stuck.id,
            amount=Decimal("10.00"),
            status="completed",
        )
        never_run = Payment(
            invoice_id=invoices[2].id,
            payment_run_id=draft.id,
            amount=Decimal("10.00"),
            status="pending",
        )
        s.add_all([in_doubt, recorded, never_run])
        await s.commit()
        ids = (in_doubt.id, recorded.id, never_run.id)

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)
        assert not await _column_exists(engine)
        await _run(engine, mig.upgrade)
        assert await _column_exists(engine)
        first = await _stamps(engine, ids)
        assert first[in_doubt.id] is not None, "a pending row on an executing run is in doubt"
        assert first[recorded.id] is None, "a recorded outcome is not in doubt"
        assert first[never_run.id] is None, "a draft run was never dispatched"

        await _run(engine, mig.upgrade)  # idempotent: no stamp moves
        assert await _stamps(engine, ids) == first
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0124_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        assert not await _column_exists(engine)
    finally:
        await engine.dispose()
