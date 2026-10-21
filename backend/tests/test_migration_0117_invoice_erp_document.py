"""Migration 0117 runs against a real tenant database: down, up, and up again.

The realdb harness builds tenants with ``create_all``, so the two columns exist
there without the revision ever executing. Nothing else proves 0117 adds them
to a tenant at the previous schema, backfills the id from the one workflow
instance's ``state_data["erp_reference"]`` (and only there: never ``UNKNOWN``,
never an invoice with two instances, never over a value already set), leaves
the URL NULL, re-runs harmlessly, reverses, and no-ops on the control plane.
Always left upgraded so later tests see the current schema.
"""

from __future__ import annotations

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import WorkflowInstance

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0117_invoice_erp_document.py"


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


async def _columns(engine) -> dict[str, tuple]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name, data_type, character_maximum_length, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'invoices' "
                "AND column_name IN ('erp_document_id', 'erp_document_url')"
            )
        )
        return {name: rest for name, *rest in rows}


async def _seed(mk, org_id, number: str, refs: list[str | None]) -> uuid.UUID:
    """An invoice with one workflow instance per entry in ``refs``."""
    from app.services.workflow_engine import get_or_create_workflow_definition

    inv_id = uuid.uuid4()
    async with mk() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Northwind Supplies",
            amount=Decimal("10.00"),
            currency="USD",
            status=InvoiceStatus.done,
            uploaded_by_id=None,  # seeded directly: no employee created it
        )
        s.add(inv)
        await s.flush()
        defn = await get_or_create_workflow_definition(s, org_id)
        for ref in refs:
            s.add(
                WorkflowInstance(
                    correlation_id=inv.correlation_id,
                    definition_id=defn.id,
                    invoice_id=inv_id,
                    state="completed",
                    state_data={"erp_reference": ref} if ref is not None else {},
                )
            )
        await s.commit()
    return inv_id


@pytest.mark.asyncio
async def test_0117_adds_the_columns_and_backfills_the_unambiguous_id(realdb):
    from app.database import _make_tenant_url

    mk = realdb.sessionmaker("a")
    org = realdb.info("a").org_id
    posted = await _seed(mk, org, "B-1", ["BILL-1"])
    unknown = await _seed(mk, org, "B-2", ["UNKNOWN"])
    twice = await _seed(mk, org, "B-3", ["BILL-3a", "BILL-3b"])
    never = await _seed(mk, org, "B-4", [None])
    empty = await _seed(mk, org, "B-5", [""])
    too_wide = await _seed(mk, org, "B-6", ["X" * 256])

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)
        assert await _columns(engine) == {}

        await _run(engine, mig.upgrade)
        assert await _columns(engine) == {
            "erp_document_id": ["character varying", 255, "YES"],
            "erp_document_url": ["character varying", 1024, "YES"],
        }

        async def _row(inv_id):
            async with engine.connect() as conn:
                return tuple(
                    (
                        await conn.execute(
                            text(
                                "SELECT erp_document_id, erp_document_url "
                                "FROM invoices WHERE id = :id"
                            ),
                            {"id": inv_id},
                        )
                    ).one()
                )

        assert await _row(posted) == ("BILL-1", None)
        assert await _row(unknown) == (None, None)
        assert await _row(twice) == (None, None)
        assert await _row(never) == (None, None)
        assert await _row(empty) == (None, None)
        # Skipped, never truncated into a different id.
        assert await _row(too_wide) == (None, None)

        # Idempotent: a re-run neither fails nor overwrites a value set since.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE invoices SET erp_document_id = 'NEWER' WHERE id = :id"),
                {"id": posted},
            )
        await _run(engine, mig.upgrade)
        assert await _row(posted) == ("NEWER", None)
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0117_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        await _run(engine, mig.upgrade)
        assert await _columns(engine) == {}
    finally:
        await engine.dispose()
