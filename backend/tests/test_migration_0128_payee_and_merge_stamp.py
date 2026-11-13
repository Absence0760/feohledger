"""Migration 0128 runs against a real tenant database: down, up, up again — and
builds exactly what ``create_all`` builds.

The realdb harness provisions tenants with ``create_all`` (as a fresh tenant
does in production), so ``payments.payee_*`` and ``vendors.merged_*`` exist
there without the revision running. This captures them, drives the revision
down and up twice through Alembic's ``Operations`` on the tenant's own
connection, and compares type, length, nullability and default, so an existing
tenant migrated to head and a new one provisioned today hold the same columns.
It also proves the revision backfills nothing (decisions §383, §141): a payment
and a vendor written before it read unstamped afterwards. Always left upgraded.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0128_payee_and_merge_stamp.py"
COLUMNS = {
    "payments": ("payee_vendor_id", "payee_bank_fingerprint", "payee_recorded_at"),
    "vendors": ("merged_into_vendor_id", "merged_by_user_id", "merged_at"),
}


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0128", REVISION)
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


async def _columns(engine) -> set[tuple]:
    found: set[tuple] = set()
    async with engine.connect() as conn:
        for table, names in COLUMNS.items():
            found |= {
                (table, *r)
                for r in await conn.execute(
                    text(
                        "SELECT column_name, data_type, character_maximum_length, "
                        "is_nullable, column_default FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :t "
                        "AND column_name = ANY(:names)"
                    ),
                    {"t": table, "names": list(names)},
                )
            }
    return found


async def _row(engine, sql: str, row_id) -> tuple:
    async with engine.connect() as conn:
        return tuple((await conn.execute(text(sql), {"id": row_id})).one())


def test_the_revision_names_the_model_columns():
    """The DDL list and the ORM agree on every column 0128 owns."""
    from app.models.payment import Payment
    from app.models.vendor import Vendor

    mig = _load()
    for table, model in (("payments", Payment), ("vendors", Vendor)):
        assert tuple(name for name, _ in mig.COLUMNS[table]) == COLUMNS[table]
        for name in COLUMNS[table]:
            assert name in model.__table__.columns, (table, name)


@pytest.mark.asyncio
async def test_0128_matches_create_all_reruns_and_backfills_nothing(realdb):
    from app.database import _make_tenant_url
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.payment import Payment
    from app.models.vendor import Vendor

    org_id = realdb.info("a").org_id
    async with realdb.sessionmaker("a")() as s:
        vendor = Vendor(organization_id=org_id, name="Migration Vendor", status="inactive")
        s.add(vendor)
        await s.flush()
        inv = Invoice(
            organization_id=org_id,
            invoice_number="MIG-0128-1",
            vendor_name="Migration Vendor",
            vendor_id=vendor.id,
            amount=Decimal("10.00"),
            currency="USD",
            status=InvoiceStatus.paid,
        )
        s.add(inv)
        await s.flush()
        paid = Payment(invoice_id=inv.id, amount=Decimal("10.00"), status="completed")
        s.add(paid)
        await s.commit()
        paid_id, vendor_id = paid.id, vendor.id

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        built = await _columns(engine)
        assert built == {
            ("payments", "payee_vendor_id", "uuid", None, "YES", None),
            ("payments", "payee_bank_fingerprint", "character varying", 80, "YES", None),
            ("payments", "payee_recorded_at", "timestamp with time zone", None, "YES", None),
            ("vendors", "merged_into_vendor_id", "uuid", None, "YES", None),
            ("vendors", "merged_by_user_id", "uuid", None, "YES", None),
            ("vendors", "merged_at", "timestamp with time zone", None, "YES", None),
        }

        await _run(engine, mig.downgrade)
        assert await _columns(engine) == set()

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _columns(engine) == built
        assert await _row(
            engine,
            "SELECT payee_vendor_id, payee_bank_fingerprint, payee_recorded_at "
            "FROM payments WHERE id = :id",
            paid_id,
        ) == (None, None, None), "no payee is backfilled onto a payment paid before 0128"
        assert await _row(
            engine,
            "SELECT merged_into_vendor_id, merged_by_user_id, merged_at "
            "FROM vendors WHERE id = :id",
            vendor_id,
        ) == (None, None, None), "no merge is inferred for a vendor retired before 0128"
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0128_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        assert await _columns(engine) == set()
    finally:
        await engine.dispose()


def test_0128_chains_after_0127():
    mig = _load()
    assert (mig.revision, mig.down_revision) == (
        "0128_payee_and_merge_stamp",
        "0127_gl_account_erp_links",
    )
    assert len(mig.revision) <= 32
