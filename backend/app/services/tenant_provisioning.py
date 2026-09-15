"""Tenant provisioning — create org, admin user, DB, and tables.

Shared by scripts/create_tenant.py (CLI) and the self-service signup
endpoint (app/api/signup.py). Splitting the work into explicit phases lets
the signup flow run each step inside the request lifecycle and surface
partial-failure diagnostics without re-running everything on retry.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass

import asyncpg
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.database import _make_tenant_url, control_session_factory
from app.models import Base
from app.models.organization import Organization
from app.models.user import Role, User, UserRole
from app.services.billing.plan_catalog import ensure_plan_catalog, ensure_subscription
from app.utils.passwords import hash_password
from app.utils.slug import validate_slug_format

logger = logging.getLogger(__name__)

# Postgres identifiers can't be parameterized, so a tenant DB name is the one
# value that has to be interpolated into CREATE/DROP DATABASE DDL. Guard the
# sink with a strict allowlist: every legitimate name is "<prefix><slug>" where
# the prefix is lowercase ASCII and the slug already passed utils.slug
# (^[a-z][a-z0-9-]{2,29}$). This is defense-in-depth: provision_tenant validates
# the slug before any DB work, whichever caller it came from, and this guard
# stays at the DDL sink so no future path to it can bypass that. Capped at
# Postgres's 63-char identifier limit.
_SAFE_DB_NAME = re.compile(r"^[a-z][a-z0-9_-]{2,62}$")


def _assert_safe_db_name(db_name: str) -> None:
    """Reject any tenant DB name that isn't a strict lowercase identifier
    before it reaches CREATE/DROP DATABASE DDL (SQL-injection guard)."""
    if not _SAFE_DB_NAME.fullmatch(db_name):
        raise ValueError(f"unsafe tenant database name: {db_name!r}")


# Tables that live in the CONTROL plane DB and must NOT be created inside a
# tenant DB. Anything not in this set belongs to the tenant schema.
CONTROL_TABLES: frozenset[str] = frozenset(
    {
        "organizations",
        "users",
        "roles",
        "user_roles",
        "email_verifications",
        # Programmatic API keys authenticate an org's access to the public
        # /api/v1 surface. They live alongside organizations/users in the
        # control plane (keyed by org_id), NOT in any tenant DB.
        "api_keys",
        # Per-key, per-day request meter for the public /api/v1 surface (feeds
        # billing). Keyed off api_keys/organizations — control-plane only, never
        # fanned to tenant DBs.
        "api_key_usage",
        # Assistant token meter is a control-plane billing table (see
        # app/models/assistant.py). Excluded here so tenant DBs don't get it;
        # the conversation/message tables ARE tenant-scoped and stay in.
        "assistant_usage",
        # Platform billing & metering (see app/models/billing.py). A plan is a
        # sellable tier; a subscription binds an org to a plan. Keyed off
        # organizations — control-plane only, never fanned to tenant DBs.
        "plans",
        "subscriptions",
        # Outbound webhook subscriptions + delivery log (see app/models/
        # webhook.py). Keyed off organizations — the push counterpart of the
        # /api/v1 pull surface. Control-plane only, never fanned to tenant DBs.
        "webhook_subscriptions",
        "webhook_deliveries",
        # WebAuthn / passkey credentials (see app/models/webauthn_credential.py).
        # An additional MFA factor bound to a control-plane User — keyed by
        # user_id, the same placement as User.mfa_secret. Control-plane only,
        # never fanned to tenant DBs.
        "webauthn_credentials",
    }
)


@dataclass
class ProvisioningResult:
    organization_id: uuid.UUID
    user_id: uuid.UUID
    db_name: str


def _parse_maintenance_dsn() -> dict:
    """Parse host/port/user/password out of the configured async URL, for an
    asyncpg connection to the 'postgres' maintenance DB."""
    url = settings.database_url.replace("postgresql+asyncpg://", "")
    userpass, hostdb = url.split("@", 1)
    user, password = userpass.split(":", 1)
    host_port, _ = hostdb.rsplit("/", 1)
    if ":" in host_port:
        host, port_str = host_port.split(":", 1)
        port = int(port_str)
    else:
        host, port = host_port, 5432
    return {"host": host, "port": port, "user": user, "password": password, "database": "postgres"}


async def _create_postgres_database(db_name: str) -> bool:
    """Create a new Postgres database (CREATE DATABASE cannot be in a transaction).

    Returns True if this call created the database, False if it already existed
    — so the caller knows whether it's safe to drop on a later failure.
    """
    _assert_safe_db_name(db_name)
    conn = await asyncpg.connect(**_parse_maintenance_dsn())
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if exists:
            logger.info("Database already exists: %s", db_name)
            return False
        await conn.execute(f'CREATE DATABASE "{db_name}"')
        logger.info("Created database: %s", db_name)
        return True
    finally:
        await conn.close()


async def _drop_postgres_database(db_name: str) -> None:
    """Drop a tenant DB we created during a provisioning attempt that then
    failed, so a partial failure doesn't leak orphan databases."""
    _assert_safe_db_name(db_name)
    conn = await asyncpg.connect(**_parse_maintenance_dsn())
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        logger.info("Dropped orphan database after failed provisioning: %s", db_name)
    finally:
        await conn.close()


# The workflow every freshly provisioned tenant starts on. Distinct from
# `workflow_engine.DEFAULT_STEPS_CONFIG`, which is the fail-closed BACKSTOP for
# a tenant that somehow has no active definition — this is the real, fully
# enabled pipeline a new customer expects to find.
PROVISIONED_STEPS_CONFIG: dict = {
    "steps": [
        {
            "number": 1,
            "type": "extraction",
            "name": "Data Extraction",
            "enabled": True,
            "config": {"auto_approve_enabled": False, "auto_approve_threshold": 0.95},
        },
        {
            "number": 2,
            "type": "approval",
            "name": "Manager Approval",
            "enabled": True,
            "config": {
                "required": True,
                "approver_id": None,
                "approver_strategy": "manual",
                "require_segregation": True,
            },
        },
        {
            "number": 3,
            "type": "erp_export",
            "name": "ERP Export",
            "enabled": True,
            "config": {"erp_system": "default"},
        },
    ]
}


async def _create_tenant_tables(db_name: str, organization_id: uuid.UUID | None = None) -> None:
    tenant_url = _make_tenant_url(db_name)
    engine = create_async_engine(tenant_url)
    tenant_tables = [
        table for name, table in Base.metadata.tables.items() if name not in CONTROL_TABLES
    ]
    async with engine.begin() as conn:
        # pgvector extension is required by the RAG embeddings table
        # (app.models.invoice_embedding). CREATE EXTENSION must run before
        # create_all, because the Vector column type resolves at DDL time.
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=tenant_tables, checkfirst=True
            )
        )
        # SOX: install the DB-level append-only guard on audit_log. Migration
        # 0022 does the same across existing tenants; this keeps tenants created
        # outside Alembic (fresh provisioning + the test harness) consistent.
        # Statements run one-at-a-time — asyncpg rejects multi-command strings.
        from app.services.audit_immutability import install_statements

        for stmt in install_statements():
            await conn.exec_driver_sql(stmt)

        # Multi-entity: every tenant gets a single Default entity that new rows
        # belong to (migration 0029 does this for existing tenants). Idempotent
        # — the uq_entities_one_default index also guards against a second one.
        if organization_id is not None:
            await conn.execute(
                text(
                    "INSERT INTO entities "
                    "(id, organization_id, name, slug, is_default, is_active) "
                    "SELECT :id, :org, 'Default', 'default', true, true "
                    "WHERE NOT EXISTS (SELECT 1 FROM entities WHERE is_default)"
                ),
                {"id": uuid.uuid4(), "org": organization_id},
            )

            # Every tenant gets a real default workflow. Without one, the
            # first invoice triggers `get_or_create_workflow_definition`'s
            # lazy fallback — and a tenant whose workflow came from that
            # fallback is a tenant nobody configured. Seeding it here means
            # the shipped pipeline (extraction → approval → ERP export) is
            # what a fresh tenant actually runs, and the fallback stays a
            # backstop. Idempotent: `uq_workflow_definitions_one_default`
            # also guards a second shared default.
            # `entity_id` is the tenant's DEFAULT entity, not NULL — matching
            # what migration 0029 backfilled onto every existing tenant's
            # definitions. A fresh tenant seeded into the shared (NULL) bucket
            # would be the only tenant shape whose definitions live there, and
            # the one-active-per-scope invariant would then behave differently
            # for it than for every migrated tenant.
            await conn.execute(
                text(
                    "INSERT INTO workflow_definitions "
                    "(id, organization_id, entity_id, name, description, "
                    " steps_config, is_active, is_default) "
                    "SELECT :id, :org, e.id, 'Default Workflow', :descr, "
                    "       CAST(:steps AS jsonb), true, true "
                    "FROM entities e WHERE e.is_default "
                    "AND NOT EXISTS (SELECT 1 FROM workflow_definitions WHERE is_default)"
                ),
                {
                    "id": uuid.uuid4(),
                    "org": organization_id,
                    "descr": "Full pipeline: extraction \u2192 approval \u2192 ERP export.",
                    "steps": json.dumps(PROVISIONED_STEPS_CONFIG),
                },
            )
    await engine.dispose()
    logger.info("Created tenant tables in: %s", db_name)


async def organization_slug_exists(slug: str) -> bool:
    """True when a control-plane Organization already claims this slug.

    The cheap pre-check that makes provisioning wrappers re-runnable
    (``scripts/create_tenant.py --skip-existing`` → ``deploy/add-tenant.sh``):
    ``provision_tenant`` itself deliberately raises on a duplicate slug, so a
    wrapper that wants skip-if-present semantics asks first.
    """
    async with control_session_factory() as session:
        result = await session.execute(select(Organization.id).where(Organization.slug == slug))
        return result.scalar_one_or_none() is not None


async def provision_tenant(
    *,
    company_name: str,
    slug: str,
    admin_email: str,
    admin_name: str,
    admin_password: str,
    plan: str = "free",
    must_change_password: bool = True,
) -> ProvisioningResult:
    """End-to-end: DB + org + admin user + tenant tables.

    On success, the admin can log in immediately at the tenant subdomain
    and will be forced to change their password on first login (unless
    must_change_password is disabled for internal tenants).

    Raises ``SlugError`` before any database work when ``slug`` fails
    ``utils.slug.validate_slug_format``. The API callers validate first to
    return a 422, but the check lives here so every caller gets it — the
    ``scripts/create_tenant.py`` CLI (and ``deploy/add-tenant.sh`` behind it)
    passes an operator-typed slug straight through, and with tenants on
    ``<slug>.<platform domain>`` a reserved one such as ``api`` would claim an
    infrastructure host.
    """
    validate_slug_format(slug)
    db_name = f"{settings.tenant_db_prefix}{slug}"

    created_db = await _create_postgres_database(db_name)
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    try:
        return await _provision_into(
            db_name=db_name,
            org_id=org_id,
            user_id=user_id,
            company_name=company_name,
            slug=slug,
            admin_email=admin_email,
            admin_name=admin_name,
            admin_password=admin_password,
            plan=plan,
            must_change_password=must_change_password,
        )
    except Exception:
        # The control-plane insert or tenant-table creation failed after the DB
        # was created — drop the orphan so retries (and the namespace) stay clean.
        # Only drop a DB this call created; never a pre-existing one.
        if created_db:
            try:
                await _drop_postgres_database(db_name)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to drop orphan database after provisioning error")
        raise


async def _provision_into(
    *,
    db_name: str,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    company_name: str,
    slug: str,
    admin_email: str,
    admin_name: str,
    admin_password: str,
    plan: str,
    must_change_password: bool,
) -> ProvisioningResult:
    async with control_session_factory() as session:
        org = Organization(
            id=org_id,
            name=company_name,
            slug=slug,
            plan=plan,
            db_name=db_name,
        )
        user = User(
            id=user_id,
            email=admin_email,
            full_name=admin_name,
            hashed_password=await hash_password(admin_password),
            is_active=True,
            organization_id=org_id,
            must_change_password=must_change_password,
        )

        # Grant the admin role if it exists (created by seed.py). In a fresh
        # DB without seeds, skip silently — RBAC roles can be provisioned
        # later by an admin endpoint.
        admin_role = (
            await session.execute(select(Role).where(Role.name == "admin"))
        ).scalar_one_or_none()

        session.add(org)
        session.add(user)
        await session.flush()

        if admin_role is not None:
            session.add(UserRole(user_id=user_id, role_id=admin_role.id))

        # Baseline billing so this org isn't permanently locked out of every
        # entitlement-gated feature (issue #180) and can actually use
        # POST /api/billing/change-plan later — that endpoint 404s "no live
        # subscription" without a starting row to change FROM. Every new org
        # starts on the real "free" plan/subscription regardless of what the
        # `plan` param above says — `plan` is a free-text legacy display
        # string on `Organization.plan` (callers have long passed values like
        # "pro" that were never a real billing tier), not a `Plan.code`;
        # upgrading to a paid, entitled tier is what change-plan is for.
        await ensure_plan_catalog(session)
        await ensure_subscription(session, organization_id=org_id, plan_code="free")

        await session.commit()
        logger.info("Provisioned org %s (%s) + admin user %s", slug, org_id, admin_email)

    await _create_tenant_tables(db_name, organization_id=org_id)

    return ProvisioningResult(
        organization_id=org_id,
        user_id=user_id,
        db_name=db_name,
    )
