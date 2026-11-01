"""A deploy against an EMPTY control-plane database must produce a usable one.

The first production deploy (#566, `docs/decisions.md` §274) ran
`alembic upgrade head && migrate_all_tenants.py` against a fresh RDS database:
Alembic stamped head over a schema it never built — its chain has no revision
that creates the control plane — and the tenant fan-out then died on
`relation "organizations" does not exist`.

The end-to-end cases run the deploy's own migration file,
`scripts/deploy_migrate.sh` (`init_control_plane.py`, then `alembic upgrade
head`, then `migrate_all_tenants.py`), against a scratch database on the local
Postgres, because the defect lived in how those three interact with an empty
database — something a mocked connection cannot show. Skipped when Postgres is
unreachable, like the realdb harness. Each name is uniquified per run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.api.deps import ALL_ROLES
from app.config import settings
from app.database import _make_tenant_url
from app.services.control_plane_bootstrap import (
    SYSTEM_ROLE_DESCRIPTIONS,
    ControlPlaneState,
    classify,
)
from app.services.tenant_provisioning import CONTROL_TABLES, _parse_maintenance_dsn

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

DEPLOY_MIGRATE_SH = BACKEND / "scripts" / "deploy_migrate.sh"
# What every deploy entry point runs: one file, never the sequence restated.
DEPLOY_MIGRATE = "sh scripts/deploy_migrate.sh"
SEQUENCE = (
    "python scripts/init_control_plane.py",
    "alembic upgrade head",
    "python scripts/migrate_all_tenants.py",
)


# ── The decision ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("has_organizations", "has_alembic_version", "expected"),
    [
        (True, True, ControlPlaneState.PRESENT),
        # A control plane seed.py built and Alembic never touched.
        (True, False, ControlPlaneState.PRESENT),
        (False, False, ControlPlaneState.FRESH),
        # The #566 state: stamped head over nothing.
        (False, True, ControlPlaneState.STAMPED_EMPTY),
    ],
)
def test_classify(has_organizations, has_alembic_version, expected):
    assert (
        classify(has_organizations=has_organizations, has_alembic_version=has_alembic_version)
        is expected
    )


def test_system_roles_are_exactly_the_rbac_roles():
    assert set(SYSTEM_ROLE_DESCRIPTIONS) == set(ALL_ROLES)


# ── Every migration entry point runs the one file ──────────────────────────


def test_the_migration_file_runs_the_bootstrap_first_and_fails_fast():
    """`init_control_plane.py` before Alembic, every tenant after, and `set -e`
    so a failed step stops the deploy before any later one runs."""
    lines = [
        ln.strip()
        for ln in DEPLOY_MIGRATE_SH.read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert lines[0] == "set -eu"
    assert [ln for ln in lines if ln.startswith(("python ", "alembic "))] == list(SEQUENCE)


@pytest.mark.parametrize(
    "path",
    [
        "deploy/deploy.sh",
        ".github/workflows/aws-deploy.yml",
        "scripts/day-one/stack.sh",
        "docs/minimal-deployment.md",
        "docs/production-deployment.md",
        "docs/backup-disaster-recovery.md",
    ],
)
def test_every_deploy_migration_runs_the_shared_file(path):
    text = (REPO / path).read_text()
    assert "scripts/deploy_migrate.sh" in text
    # No entry point restates the sequence — a copy is what drifts (#575).
    for step in ("alembic upgrade head && ", "init_control_plane.py && "):
        assert step not in text, f"{path} restates the migration sequence"


# ── End to end against a scratch database ──────────────────────────────────


@pytest.fixture
async def scratch_control_db():
    dsn = _parse_maintenance_dsn()
    try:
        maint = await asyncpg.connect(**dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"requires a live Postgres (pnpm db:up): {exc}")
    name = f"cpboot_{uuid.uuid4().hex[:12]}"
    await maint.execute(f'CREATE DATABASE "{name}"')
    try:
        yield name, {**dsn, "database": name}
    finally:
        await maint.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await maint.close()


def _run(command: str, db_name: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "FEOH_DATABASE_URL": _make_tenant_url(db_name)}
    env.pop("FEOH_MIGRATE_TENANT", None)
    # The deploy's own file, unmodified. In the image `python` and `alembic` are
    # on PATH; here a shim directory points both at this interpreter.
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    (shims / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (shims / "alembic").write_text(f'#!/bin/sh\nexec "{sys.executable}" -m alembic "$@"\n')
    for shim in shims.iterdir():
        shim.chmod(0o755)
    env["PATH"] = f"{shims}:{env.get('PATH', '/usr/bin:/bin')}"
    return subprocess.run(
        ["sh", "-c", command],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


async def _tables(dsn: dict) -> set[str]:
    conn = await asyncpg.connect(**dsn)
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        return {r["table_name"] for r in rows}
    finally:
        await conn.close()


async def test_deploy_command_initialises_an_empty_control_plane(scratch_control_db, tmp_path):
    name, dsn = scratch_control_db

    result = _run(DEPLOY_MIGRATE, name, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Empty control-plane database" in result.stdout
    assert "Control plane initialised at" in result.stdout
    assert "No tenants found in control plane." in result.stdout

    tables = await _tables(dsn)
    assert CONTROL_TABLES <= tables
    # Only the control plane: no tenant table leaked in.
    assert tables - CONTROL_TABLES == {"alembic_version"}

    conn = await asyncpg.connect(**dsn)
    try:
        roles = {
            r["name"]
            for r in await conn.fetch("SELECT name FROM roles WHERE organization_id IS NULL")
        }
        version = await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()
    assert roles == set(ALL_ROLES)
    heads = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    assert version == heads


async def test_second_deploy_is_a_no_op(scratch_control_db, tmp_path):
    name, dsn = scratch_control_db
    assert _run(DEPLOY_MIGRATE, name, tmp_path).returncode == 0
    before = await _tables(dsn)

    again = _run(DEPLOY_MIGRATE, name, tmp_path)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "Control plane already initialised." in again.stdout
    assert await _tables(dsn) == before

    conn = await asyncpg.connect(**dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM roles") == len(ALL_ROLES)
    finally:
        await conn.close()


async def test_a_later_deploy_restores_a_missing_system_role(scratch_control_db, tmp_path):
    """An initialised control plane is not a bare no-op: a system role that went
    missing is restored, since provision_tenant refuses to provision without the
    system admin role (#575 silent-skip audit)."""
    name, dsn = scratch_control_db
    assert _run(DEPLOY_MIGRATE, name, tmp_path).returncode == 0
    conn = await asyncpg.connect(**dsn)
    try:
        await conn.execute("DELETE FROM roles WHERE name = 'admin' AND organization_id IS NULL")
    finally:
        await conn.close()

    again = _run(DEPLOY_MIGRATE, name, tmp_path)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "restored 1 missing system role(s)" in again.stdout

    conn = await asyncpg.connect(**dsn)
    try:
        roles = {r["name"] for r in await conn.fetch("SELECT name FROM roles")}
    finally:
        await conn.close()
    assert roles == set(ALL_ROLES)


async def test_a_database_stamped_while_empty_is_refused(scratch_control_db, tmp_path):
    """The state the first production deploy left behind."""
    name, dsn = scratch_control_db
    conn = await asyncpg.connect(**dsn)
    try:
        await conn.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        await conn.execute("INSERT INTO alembic_version VALUES ('0111_commerce_inventory_sync')")
    finally:
        await conn.close()

    result = _run(DEPLOY_MIGRATE, name, tmp_path)
    assert result.returncode == 2
    assert "DROP DATABASE feohledger" in result.stderr
    # It stopped before Alembic, and changed nothing.
    assert await _tables(dsn) == {"alembic_version"}


async def test_a_failed_tenant_migration_fails_the_run(monkeypatch):
    """`deploy.sh` rolls new code only if migrations exit 0 — a tenant that
    failed used to print FAILED and still exit 0."""
    import scripts.migrate_all_tenants as mat

    class _Result:
        def all(self):
            return [("feoh_ok",), ("feoh_broken",)]

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, _stmt):
            return _Result()

    class _Engine:
        async def dispose(self):
            return None

    attempted = []

    def fake_run(argv, env, **_kw):
        attempted.append(env["FEOH_MIGRATE_TENANT"])
        rc = 1 if env["FEOH_MIGRATE_TENANT"] == "feoh_broken" else 0
        return subprocess.CompletedProcess(argv, rc, "", "boom" if rc else "")

    monkeypatch.setattr(mat, "control_session_factory", lambda: _Session())
    monkeypatch.setattr(mat, "control_engine", _Engine())
    monkeypatch.setattr(mat.subprocess, "run", fake_run)

    assert await mat.main() == 1
    assert attempted == ["feoh_ok", "feoh_broken"]


# Builds its own engine against the scratch control-plane database.
@pytest.mark.real_db_engine
async def test_initialise_is_all_or_nothing(scratch_control_db, monkeypatch):
    """A failure after the tables are built must leave NO tables behind —
    otherwise the next run calls the database PRESENT with no roles and no
    version row (review of #570)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    import app.services.control_plane_bootstrap as cpb

    name, dsn = scratch_control_db

    async def boom(_conn):
        raise RuntimeError("role seed failed")

    monkeypatch.setattr(cpb, "_seed_system_roles", boom)
    engine = create_async_engine(_make_tenant_url(name))
    try:
        with pytest.raises(RuntimeError):
            await cpb.initialise(engine, "0111_commerce_inventory_sync")
        assert await cpb.inspect(engine) is ControlPlaneState.FRESH
    finally:
        await engine.dispose()
    assert await _tables(dsn) == set()


def test_the_deployed_url_is_untouched_by_these_tests():
    """Guard the fixture: every case above targets a scratch database, never the
    configured control plane."""
    assert "cpboot_" not in settings.database_url
