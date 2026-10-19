"""Migration 0112 brings an already-provisioned control plane onto decisions §281:
Free gains ``erp_integrations``, Scale gains ``erp_merge``, Growth is untouched.

``ensure_plan_catalog`` never rewrites an existing row, so this revision is the
only thing that reaches a control plane provisioned before §281. It merges one
key per row rather than rewriting the column, so an operator's other edits to a
plan's grants survive — pinned below with an extra key on the Free row.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0112_free_plan_erp.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0112", REVISION)
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


async def _grants(engine) -> dict[str, dict]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT code, entitlements FROM plans WHERE code IN ('free', 'growth', 'scale')")
        )
        return {code: dict(ent or {}) for code, ent in rows}


async def _set(engine, code: str, entitlements: dict) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE plans SET entitlements = CAST(:e AS jsonb) WHERE code = :c"),
            {"e": json.dumps(entitlements), "c": code},
        )


@pytest.mark.asyncio
async def test_0112_adds_the_two_grants_keeps_other_edits_and_reruns(realdb):
    from app.services.billing.plan_catalog import DEFAULT_PLAN_CATALOG, ensure_plan_catalog

    async with realdb.control_sessionmaker()() as s:
        await ensure_plan_catalog(s)
        await s.commit()

    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    catalog = {spec["code"]: spec["entitlements"] for spec in DEFAULT_PLAN_CATALOG}
    try:
        # The pre-§281 rows, plus an operator's own key on Free.
        await _set(engine, "free", {"max_seats": 5})
        scale_before = {k: v for k, v in catalog["scale"].items() if k != "erp_merge"}
        await _set(engine, "scale", scale_before)
        growth_before = (await _grants(engine))["growth"]

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        after = await _grants(engine)
        assert after["free"] == {"max_seats": 5, "erp_integrations": True}
        assert after["scale"] == {**scale_before, "erp_merge": True}
        assert after["growth"] == growth_before
        assert "erp_merge" not in after["growth"]

        await _run(engine, mig.downgrade)
        reverted = await _grants(engine)
        assert reverted["free"] == {"max_seats": 5}
        assert reverted["scale"] == scale_before
        assert reverted["growth"] == growth_before
    finally:
        for code in ("free", "scale"):
            await _set(engine, code, catalog[code])
        await engine.dispose()


async def _catalog_engine(realdb):
    from app.services.billing.plan_catalog import DEFAULT_PLAN_CATALOG, ensure_plan_catalog

    async with realdb.control_sessionmaker()() as s:
        await ensure_plan_catalog(s)
        await s.commit()
    catalog = {spec["code"]: spec["entitlements"] for spec in DEFAULT_PLAN_CATALOG}
    return create_async_engine(realdb.control_db_url()), catalog


@pytest.mark.asyncio
async def test_0112_keeps_an_operators_explicit_false(realdb):
    """The revision only ADDS an absent key. An operator who already answered
    the question on that row — `false` included — keeps the answer."""
    mig = _load()
    engine, catalog = await _catalog_engine(realdb)
    try:
        await _set(engine, "free", {"erp_integrations": False})
        scale_off = {**catalog["scale"], "erp_merge": False}
        await _set(engine, "scale", scale_off)

        await _run(engine, mig.upgrade)
        after = await _grants(engine)
        assert after["free"] == {"erp_integrations": False}
        assert after["scale"] == scale_off
    finally:
        for code in ("free", "scale"):
            await _set(engine, code, catalog[code])
        await engine.dispose()


@pytest.mark.asyncio
async def test_0112_is_a_no_op_when_the_rows_are_missing(realdb):
    """A control plane with no `free` / `scale` row (catalog never provisioned)
    gets nothing invented: the UPDATE matches no row. The rows are moved aside
    by code rather than deleted, so subscriptions pointing at them are kept."""
    mig = _load()
    engine, catalog = await _catalog_engine(realdb)
    aside = {"free": "mig0112_aside_free", "scale": "mig0112_aside_scale"}
    try:
        async with engine.begin() as conn:
            for code, tmp in aside.items():
                await conn.execute(
                    text("UPDATE plans SET code = :t, entitlements = '{}'::jsonb WHERE code = :c"),
                    {"t": tmp, "c": code},
                )
        await _run(engine, mig.upgrade)
        async with engine.connect() as conn:
            rows = dict(
                (
                    await conn.execute(
                        text("SELECT code, entitlements FROM plans WHERE code = ANY(:c)"),
                        {"c": [*aside, *aside.values()]},
                    )
                ).all()
            )
        assert set(rows) == set(aside.values())
        assert all(ent == {} for ent in rows.values())
    finally:
        async with engine.begin() as conn:
            for code, tmp in aside.items():
                await conn.execute(
                    text("UPDATE plans SET code = :c WHERE code = :t"), {"t": tmp, "c": code}
                )
        for code in ("free", "scale"):
            await _set(engine, code, catalog[code])
        await engine.dispose()


def test_entitlements_column_cannot_hold_null():
    """The revision COALESCEs a NULL `entitlements` anyway, but the column is
    NOT NULL, so no row can reach that branch — pinned here so a relaxed
    column has to revisit 0112's assumption."""
    from app.models.billing import Plan

    assert Plan.__table__.c.entitlements.nullable is False


@pytest.mark.asyncio
async def test_0112_is_a_no_op_on_a_tenant_database(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'plans'"
                    )
                )
            ).scalar() is None
    finally:
        await engine.dispose()
