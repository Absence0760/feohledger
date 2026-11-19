"""Migration 0136 — an invoice may record "currency unknown" as NULL (§TBD-currency).

The realdb harness builds tenants with ``create_all``, so the nullable column
exists there without the revision ever executing; that is the fresh-tenant half
of the parity this pins (the model must declare ``currency`` nullable). The
other half: on a tenant at the previous schema (NOT NULL) the revision drops the
constraint and turns every blank (``""``, whitespace) into NULL, leaves real
codes alone (no USD backfill or removal, §141), re-runs harmlessly, reverses,
and no-ops on the control plane. Always left upgraded, so later tests see the
head schema.
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

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0136_invoice_currency_null.py"
TENANT = "a"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0136", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chains_after_0135_and_fits_the_version_column():
    mig = _load()
    assert mig.down_revision == "0135_seal_extraction_sso"
    assert len(mig.revision) <= 32
    assert REVISION.stem == mig.revision


def test_the_model_declares_the_column_nullable():
    """Fresh tenants are built by ``create_all``, not Alembic."""
    assert Invoice.__table__.columns["currency"].nullable


async def _run(engine, fn) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    async with engine.begin() as conn:

        def _do(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                fn()

        await conn.run_sync(_do)


async def _nullable(engine) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'invoices' "
                    "AND column_name = 'currency'"
                )
            )
        ).scalar()


async def _currencies(engine, ids) -> dict:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, currency FROM invoices WHERE id = ANY(:ids)"),
                {"ids": list(ids.values())},
            )
        ).all()
    by_id = {row.id: row.currency for row in rows}
    return {label: by_id[iid] for label, iid in ids.items()}


@pytest.mark.asyncio
async def test_0136_turns_blanks_into_null_on_a_tenant_and_reverses(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info(TENANT).db_name))
    ids = {label: uuid.uuid4() for label in ("blank", "spaces", "usd", "eur", "unknown")}
    try:
        # create_all (the harness, and every fresh tenant) built it nullable.
        assert await _nullable(engine) == "YES"

        # Rows written through the ORM at the head schema. The "unknown" one
        # passes `currency=None` explicitly, as the upload does, which must
        # land as NULL rather than the column's Python default.
        currencies = {"blank": "", "spaces": "  ", "usd": "USD", "eur": "EUR", "unknown": None}
        async with realdb.sessionmaker(TENANT)() as s:
            entity_id = (
                await s.execute(text("SELECT id FROM entities WHERE is_default LIMIT 1"))
            ).scalar_one()
            for label, iid in ids.items():
                s.add(
                    Invoice(
                        id=iid,
                        invoice_number=f"MIG136-{label}-{iid.hex[:6]}",
                        vendor_name="Mig Vendor",
                        amount=Decimal("10.00"),
                        currency=currencies[label],
                        status=InvoiceStatus.new,
                        organization_id=realdb.info(TENANT).org_id,
                        entity_id=entity_id,
                        uploaded_by_id=None,  # a migration fixture, not an employee upload
                    )
                )
            await s.commit()
        assert (await _currencies(engine, ids))["unknown"] is None

        # The downgrade turns the unknown row into the pre-0136 sentinel so
        # NOT NULL can come back.
        await _run(engine, mig.downgrade)
        assert await _nullable(engine) == "NO"
        assert (await _currencies(engine, ids))["unknown"] == ""
        await _run(engine, mig.downgrade)  # a second downgrade changes nothing
        assert await _nullable(engine) == "NO"

        await _run(engine, mig.upgrade)
        assert await _nullable(engine) == "YES"
        assert await _currencies(engine, ids) == {
            "blank": None,
            "spaces": None,
            "usd": "USD",  # an old upload default cannot be told from a real USD (§141)
            "eur": "EUR",
            "unknown": None,
        }
        await _run(engine, mig.upgrade)  # a second upgrade changes nothing
        assert await _nullable(engine) == "YES"
    finally:
        # The model declares the column nullable, so the shared tenant must too.
        await _run(engine, mig.upgrade)
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM invoices WHERE id = ANY(:ids)"), {"ids": list(ids.values())}
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_0136_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        async with engine.connect() as conn:
            has_invoices = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'invoices'"
                    )
                )
            ).scalar()
        assert has_invoices is None  # the gate's premise
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        await _run(engine, mig.upgrade)
    finally:
        await engine.dispose()
