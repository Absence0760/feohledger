"""Bring an EMPTY control-plane database up to a usable baseline.

Alembic cannot do this. The first revision (`0001_signup`) postdates a schema
that was always built by ``create_all`` — `scripts/seed.py` locally, the pytest
harness in CI — so the chain has no "create organizations / users / roles"
step. Run against an empty database, `alembic upgrade head` probes for
`users`, finds none, treats the database as a tenant, and stamps head over a
schema that does not exist. The deploy then dies one step later, in
`migrate_all_tenants.py`, on `relation "organizations" does not exist` — which
is how the first production deploy found this (issue #566).

So a fresh install is built the same way a fresh tenant is (`database.md`
§ Index parity between the two provisioning paths): ``create_all`` of the
`CONTROL_TABLES`, then Alembic recorded at head, because the ORM already IS the
head schema (`test_migration_model_index_parity.py` holds that). The four
system roles are seeded with it — `provision_tenant` grants a new tenant's
admin the `admin` role only if that row exists, and skips silently when it
does not, so a control plane without them provisions admins who can do nothing.

`scripts/init_control_plane.py` is the entry point; `deploy/deploy.sh` runs it
before `alembic upgrade head`.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.api.deps import ROLE_ADMIN, ROLE_AP_CLERK, ROLE_AP_MANAGER, ROLE_CFO
from app.models import Base
from app.models.user import Role
from app.services.schema_version import record_head
from app.services.tenant_provisioning import CONTROL_TABLES

# Canonical system-role set, keyed by the same ROLE_* constants `app.api.deps`
# composes into ALL_ROLES. `scripts/seed.py` seeds from this map too, and
# `tests/test_rbac.py` asserts it equals ALL_ROLES, so the seeded roles and the
# `require_roles` allow-list cannot drift.
SYSTEM_ROLE_DESCRIPTIONS: dict[str, str] = {
    ROLE_ADMIN: "Full access to all features and user management",
    ROLE_AP_MANAGER: "Review and approve invoices",
    ROLE_AP_CLERK: "Enter and code invoices; prepare requisitions, intake and expenses",
    ROLE_CFO: "Approve high-value invoices and view reports",
}


class ControlPlaneState(enum.Enum):
    """What an init run finds, decided from two facts about the database."""

    # `organizations` exists: an initialised control plane. Nothing to build;
    # `alembic upgrade head` carries it forward as usual.
    PRESENT = "present"
    # Neither `organizations` nor `alembic_version`: a genuinely empty database.
    FRESH = "fresh"
    # `alembic_version` without `organizations`: Alembic ran against an empty
    # database before this bootstrap existed, and stamped head over nothing.
    # Refused rather than repaired — the migrations it "applied" took their
    # tenant-database branches here, so what is in it is not a known state.
    # It can hold no data (there is nowhere to put any), so recreating it is
    # the whole fix.
    STAMPED_EMPTY = "stamped_empty"


def classify(*, has_organizations: bool, has_alembic_version: bool) -> ControlPlaneState:
    if has_organizations:
        return ControlPlaneState.PRESENT
    if has_alembic_version:
        return ControlPlaneState.STAMPED_EMPTY
    return ControlPlaneState.FRESH


async def inspect(engine: AsyncEngine) -> ControlPlaneState:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT to_regclass('public.organizations') IS NOT NULL, "
                    "to_regclass('public.alembic_version') IS NOT NULL"
                )
            )
        ).one()
    return classify(has_organizations=row[0], has_alembic_version=row[1])


def _control_tables() -> list:
    # Tenant tables are filtered out: `invoice_embeddings` alone needs pgvector,
    # which the control database does not have.
    return [table for name, table in Base.metadata.tables.items() if name in CONTROL_TABLES]


async def create_control_tables(engine: AsyncEngine) -> None:
    """``create_all`` of the control-plane tables only. Additive and idempotent
    (``checkfirst``) — it never alters or drops an existing table."""
    tables = _control_tables()
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables, checkfirst=True)
        )


async def _seed_system_roles(conn: AsyncConnection) -> None:
    """Insert any missing system role. Never touches a row that exists — the
    `uq_roles_system_name` partial index is the conflict target."""
    await conn.execute(
        pg_insert(Role)
        .values(
            [
                {"id": uuid.uuid4(), "name": name, "description": description}
                for name, description in SYSTEM_ROLE_DESCRIPTIONS.items()
            ]
        )
        .on_conflict_do_nothing(
            index_elements=[Role.name],
            index_where=Role.organization_id.is_(None),
        )
    )


async def initialise(engine: AsyncEngine, head_revision: str) -> None:
    """Build an empty control plane in ONE transaction: the tables, the system
    roles, and Alembic's version row at ``head_revision``.

    Postgres DDL is transactional, so this either all lands or none of it does.
    Three separate commits would leave a window — tables committed, roles or the
    stamp not — in which the next run sees `organizations`, calls the database
    PRESENT, and either provisions admins with no role or lets
    `alembic upgrade head` replay the whole chain onto it. The version table is
    written here with Alembic's own shape rather than by `alembic stamp` in a
    subprocess for the same reason.
    """
    tables = _control_tables()
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables, checkfirst=True)
        )
        await _seed_system_roles(conn)
        await record_head(conn, head_revision)
