"""Guards for the realdb harness's per-test tenant reset (`_reset_tenant_tables`).

The reset DELETEs only the tenant tables that hold a row, under a
transaction-local `session_replication_role = replica`, instead of TRUNCATEing
every tenant table (which fsynced a fresh relfilenode per table and index —
seconds per test). These tests pin what that rewrite must still guarantee:

* every test starts with every tenant table empty except the one Default entity;
* a write to any table — including the append-only `audit_log`, whose
  `audit_log_no_delete` trigger would refuse an ordinary DELETE — is gone after
  the reset;
* the probe covers every table physically present in a tenant DB, so nothing
  can escape it by not being in `Base.metadata` (bar `alembic_version`, the
  provisioning stamp, which the reset must keep);
* no tenant table owns a sequence, because the DELETE path has no counterpart to
  TRUNCATE's RESTART IDENTITY. If this one fails, teach the reset to restart
  the new sequence; do not delete the assertion.

See backend/CLAUDE.md § Test databases.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import _make_tenant_url
from app.models import Base
from app.models.entity import Entity
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.services.tenant_provisioning import CONTROL_TABLES
from tests.conftest import _reset_tenant_tables

# `asyncio_mode = "auto"` (pyproject) runs the async tests below — no marks.


def _tenant_tables() -> list[str]:
    return [n for n in Base.metadata.tables if n not in CONTROL_TABLES]


async def _non_empty_tables(conn) -> dict[str, int]:
    counts = {}
    for name in _tenant_tables():
        n = (await conn.exec_driver_sql(f'SELECT count(*) FROM "{name}"')).scalar()
        if n:
            counts[name] = n
    return counts


async def _assert_baseline(db_name: str, org_id: uuid.UUID) -> None:
    engine = create_async_engine(_make_tenant_url(db_name))
    try:
        async with engine.connect() as conn:
            assert await _non_empty_tables(conn) == {"entities": 1}
            row = (
                await conn.execute(
                    text("SELECT organization_id, slug, is_default, is_active FROM entities")
                )
            ).one()
            assert tuple(row) == (org_id, "default", True, True)
    finally:
        await engine.dispose()


async def test_every_test_starts_from_the_reset_baseline(realdb):
    """Order-independent: true at the start of ANY realdb test, so whatever ran
    before this one in the process (in any order) left nothing behind."""
    for info in realdb.tenants.values():
        await _assert_baseline(info.db_name, info.org_id)


async def test_reset_empties_what_a_test_wrote_including_the_audit_log(realdb):
    info = realdb.tenants["a"]
    async with realdb.sessionmaker("a")() as s:
        s.add(Vendor(name="Reset Probe Ltd", organization_id=info.org_id))
        s.add(
            AuditLog(
                organization_id=info.org_id,
                action="reset.probe",
                entity_type="vendor",
            )
        )
        s.add(Entity(organization_id=info.org_id, name="Second", slug="second"))
        await s.commit()

    engine = create_async_engine(_make_tenant_url(info.db_name))
    try:
        # The append-only trigger is live in this DB: an ordinary DELETE is
        # refused, which is exactly what the reset must get past.
        async with engine.connect() as conn:
            triggers = (
                await conn.exec_driver_sql(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'audit_log'::regclass AND NOT tgisinternal"
                )
            ).scalars()
            assert "audit_log_no_delete" in set(triggers)
        async with engine.begin() as conn:
            dirty = await _reset_tenant_tables(conn, _tenant_tables(), info.org_id)
        assert {"vendors", "audit_log", "entities"} <= set(dirty)
    finally:
        await engine.dispose()

    await _assert_baseline(info.db_name, info.org_id)


#: Tables a tenant DB holds that are not test data, so the reset must keep them.
#: `alembic_version` is the head revision `_create_tenant_tables` records on a
#: fresh tenant (`services/schema_version.record_head`, #575); emptying it would
#: make the DB read as unstamped.
_KEPT_BY_THE_RESET = {"alembic_version"}


async def test_the_probe_covers_every_table_in_a_tenant_db(realdb):
    probed = set(_tenant_tables())
    for info in realdb.tenants.values():
        engine = create_async_engine(_make_tenant_url(info.db_name))
        try:
            async with engine.connect() as conn:
                physical = set(
                    (
                        await conn.exec_driver_sql(
                            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                        )
                    ).scalars()
                )
                stamped = (
                    await conn.exec_driver_sql("SELECT count(*) FROM alembic_version")
                ).scalar()
        finally:
            await engine.dispose()
        unprobed = physical - probed - _KEPT_BY_THE_RESET
        assert not unprobed, f"tables the reset never empties: {sorted(unprobed)}"
        # The kept table survived every earlier test's reset with its one row.
        assert stamped == 1


async def test_no_tenant_table_owns_a_sequence(realdb):
    """TRUNCATE ... RESTART IDENTITY reset sequences; the DELETE reset does not.
    That is only equivalent while no tenant table owns one."""
    info = realdb.tenants["a"]
    engine = create_async_engine(_make_tenant_url(info.db_name))
    try:
        async with engine.connect() as conn:
            owned = (
                await conn.exec_driver_sql(
                    "SELECT d.refobjid::regclass::text || ' -> ' || d.objid::regclass::text "
                    "FROM pg_depend d JOIN pg_class s ON s.oid = d.objid "
                    "WHERE s.relkind = 'S' AND d.classid = 'pg_class'::regclass "
                    "AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')"
                )
            ).scalars()
            assert list(owned) == []
    finally:
        await engine.dispose()
