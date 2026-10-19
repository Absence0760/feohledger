"""Alembic's version row, for a database built by ``create_all`` instead.

Both fresh-database paths build the HEAD schema straight from the ORM rather
than replaying the chain: an empty control plane (`control_plane_bootstrap`)
and every newly provisioned tenant (`tenant_provisioning._create_tenant_tables`).
`test_migration_model_index_parity.py` is what holds the ORM equal to head.

A database built that way has to say so, in the same transaction that builds
it. Left without a version row, the next deploy's `migrate_all_tenants.py` runs
`alembic upgrade head` against it from base — every revision in the chain,
replayed over a schema that already has everything in it. The idempotent DDL
mostly no-ops, but not entirely: the multi-entity revisions add a second,
differently named foreign key beside each one `create_all` built. That is how
CI's day-one job (#575) found it: the first deploy after `add-tenant.sh` was not
the no-op it should have been.
"""

from __future__ import annotations

import functools
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

BACKEND_DIR = Path(__file__).resolve().parents[2]


class AlembicHeadError(RuntimeError):
    """The chain does not have exactly one head, so there is no single
    revision a fresh database can honestly be recorded at."""


@functools.cache
def head_revision() -> str:
    """The chain's single head, from the same `alembic.ini` `alembic upgrade
    head` uses. The script location is pinned to this checkout rather than left
    relative to the working directory, so it resolves the same from the API, a
    script or a test. More than one head is refused rather than guessed."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise AlembicHeadError(f"expected one Alembic head, found {heads}")
    return heads[0]


async def record_head(conn: AsyncConnection, revision: str) -> None:
    """Create Alembic's version table (Alembic's own shape) and record
    ``revision`` — unless the database already records one.

    An existing row is left alone: a database that Alembic is already tracking
    is at whatever revision it says, and `alembic upgrade head` is what moves
    it. Run inside the caller's transaction, so the schema and the row that
    describes it land together or not at all.
    """
    await conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO alembic_version (version_num) "
            "SELECT :v WHERE NOT EXISTS (SELECT 1 FROM alembic_version)"
        ),
        {"v": revision},
    )
