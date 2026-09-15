"""Tests for the tenant-provisioning service.

Covers the path shared by ``scripts/create_tenant.py`` and the
``/api/signup/complete`` endpoint (``app/services/tenant_provisioning.py``):

* slug -> ``feoh_<slug>`` database-name mapping
* the CONTROL_TABLES / tenant-tables split (so a tenant DB never gets
  control-plane tables, and every tenant table fans out)
* ``_create_postgres_database`` idempotency / duplicate-slug handling
  (mocked asyncpg — no DB needed)
* end-to-end ``provision_tenant`` against a live Postgres (realdb): org +
  admin user creation, password hashing via the shared bcrypt_sha256
  context, admin-role grant, ``must_change_password`` flag, real tenant
  tables created, and re-running for the same slug being idempotent.

The realdb tests create + drop their own throwaway tenant DB (the realdb
fixture only manages this process's own slotted tenant pair), so they clean up
the control-plane org/user rows and the Postgres database they make.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.config import settings
from app.models import Base
from app.models.billing import Subscription
from app.models.organization import Organization
from app.models.user import Role, User, UserRole
from app.services import tenant_provisioning
from app.services.tenant_provisioning import (
    CONTROL_TABLES,
    _create_postgres_database,
    _drop_postgres_database,
    provision_tenant,
)
from app.utils.passwords import pwd_context
from app.utils.slug import SlugError

# ---------------------------------------------------------------------------
# Pure / structural — slug mapping + the control/tenant table split
# ---------------------------------------------------------------------------


def test_control_tables_are_the_expected_control_plane_set():
    # These live in the control plane DB and must never be created inside a
    # tenant DB. A regression here would either leak control tables into tenant
    # DBs or (if a tenant table were added) silently drop a tenant table from
    # provisioning.
    assert CONTROL_TABLES == frozenset(
        {
            "organizations",
            "users",
            "roles",
            "user_roles",
            "email_verifications",
            "assistant_usage",
            "api_keys",
            "api_key_usage",
            "plans",
            "subscriptions",
            "webhook_subscriptions",
            "webhook_deliveries",
            "webauthn_credentials",
        }
    )


def test_control_tables_exist_in_metadata():
    # Guard against a rename: every name in CONTROL_TABLES must be a real table.
    all_tables = set(Base.metadata.tables)
    assert CONTROL_TABLES <= all_tables


def test_tenant_table_set_excludes_control_and_is_nonempty():
    tenant_tables = {n for n in Base.metadata.tables if n not in CONTROL_TABLES}
    # Spot-check that core tenant tables fan out and control tables don't.
    assert "invoices" in tenant_tables
    assert "vendors" in tenant_tables
    assert "payments" in tenant_tables
    # PEPPOL transmission log is tenant-scoped — fans out to every tenant DB.
    assert "peppol_transmissions" in tenant_tables
    assert tenant_tables.isdisjoint(CONTROL_TABLES)
    assert "organizations" not in tenant_tables
    assert "users" not in tenant_tables


def test_db_name_uses_prefix_and_slug():
    # The slug -> feoh_<slug> mapping is the multi-tenancy invariant. Provisioning
    # derives db_name as f"{settings.tenant_db_prefix}{slug}".
    assert settings.tenant_db_prefix == "feoh_"
    slug = "newco"
    assert f"{settings.tenant_db_prefix}{slug}" == "feoh_newco"


# ---------------------------------------------------------------------------
# _create_postgres_database — idempotency / duplicate-slug (mocked asyncpg)
# ---------------------------------------------------------------------------


async def test_create_postgres_database_creates_when_absent():
    conn = AsyncMock()
    # pg_database lookup returns no row -> CREATE DATABASE should run.
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    conn.close = AsyncMock()

    with patch(
        "app.services.tenant_provisioning.asyncpg.connect",
        new=AsyncMock(return_value=conn),
    ):
        await _create_postgres_database("feoh_freshslug")

    # Exactly one CREATE DATABASE for the requested name.
    assert conn.execute.await_count == 1
    created_sql = conn.execute.await_args.args[0]
    assert "CREATE DATABASE" in created_sql
    assert "feoh_freshslug" in created_sql
    conn.close.assert_awaited_once()


@pytest.mark.parametrize(
    "evil_name",
    [
        'feoh_x"; DROP DATABASE feohledger; --',
        "feoh_a b",  # whitespace
        "feoh_a;b",  # statement separator
        "feoh_a)b",  # paren
        "feoh_UPPER",  # uppercase not allowed
        "1ap_x",  # must start with a letter
        "ap",  # too short (< 3 chars)
        "",  # empty
        "feoh_" + "x" * 70,  # over Postgres's 63-char identifier limit
    ],
)
async def test_create_postgres_database_rejects_unsafe_name(evil_name):
    # SQL-injection guard: a db name that isn't a strict lowercase identifier
    # must be rejected BEFORE any connection or DDL — defense-in-depth at the
    # one DDL sink where the value can't be parameterized.
    connect = AsyncMock()
    with patch("app.services.tenant_provisioning.asyncpg.connect", new=connect):
        with pytest.raises(ValueError, match="unsafe tenant database name"):
            await _create_postgres_database(evil_name)
        with pytest.raises(ValueError, match="unsafe tenant database name"):
            await _drop_postgres_database(evil_name)
    # Never even opened a connection to the maintenance DB.
    connect.assert_not_called()


async def test_create_postgres_database_idempotent_when_present():
    # Re-provisioning the same slug must NOT issue a second CREATE DATABASE —
    # this is the duplicate-slug safety on the DB-creation step.
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)  # pg_database row already exists
    conn.execute = AsyncMock()
    conn.close = AsyncMock()

    with patch(
        "app.services.tenant_provisioning.asyncpg.connect",
        new=AsyncMock(return_value=conn),
    ):
        await _create_postgres_database("feoh_existing")

    conn.execute.assert_not_called()
    conn.close.assert_awaited_once()


async def test_create_postgres_database_closes_connection_on_error():
    # The finally: must close the asyncpg connection even when the existence
    # probe blows up, so we don't leak a connection to the maintenance DB.
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("boom"))
    conn.close = AsyncMock()

    with patch(
        "app.services.tenant_provisioning.asyncpg.connect",
        new=AsyncMock(return_value=conn),
    ):
        with pytest.raises(RuntimeError):
            await _create_postgres_database("feoh_explodes")

    conn.close.assert_awaited_once()


async def test_create_postgres_database_parses_host_port_from_url():
    # _create_postgres_database parses host/port/user/password out of the
    # configured asyncpg URL and connects to the "postgres" maintenance DB.
    # Assert it threads those parsed values through verbatim.
    captured: dict = {}

    async def _fake_connect(**kwargs):
        captured.update(kwargs)
        c = MagicMock()
        c.fetchval = AsyncMock(return_value=1)
        c.execute = AsyncMock()
        c.close = AsyncMock()
        return c

    test_url = "postgresql+asyncpg://apuser:apsecret@db.example.com:6543/feohledger"
    with patch.object(settings, "database_url", test_url):
        with patch(
            "app.services.tenant_provisioning.asyncpg.connect",
            new=_fake_connect,
        ):
            await _create_postgres_database("feoh_parsetest")

    assert captured["host"] == "db.example.com"
    assert captured["port"] == 6543
    assert captured["user"] == "apuser"
    assert captured["password"] == "apsecret"
    # Always connects to the maintenance DB, never the target tenant DB.
    assert captured["database"] == "postgres"


async def test_create_postgres_database_url_parse_defaults_port_5432():
    captured: dict = {}

    async def _fake_connect(**kwargs):
        captured.update(kwargs)
        c = MagicMock()
        c.fetchval = AsyncMock(return_value=1)
        c.execute = AsyncMock()
        c.close = AsyncMock()
        return c

    # No explicit port in the URL -> default 5432.
    test_url = "postgresql+asyncpg://u:p@localhost/feohledger"
    with patch.object(settings, "database_url", test_url):
        with patch(
            "app.services.tenant_provisioning.asyncpg.connect",
            new=_fake_connect,
        ):
            await _create_postgres_database("feoh_defaultport")

    assert captured["host"] == "localhost"
    assert captured["port"] == 5432


# ---------------------------------------------------------------------------
# Slug validation happens inside provision_tenant, before any database work
# ---------------------------------------------------------------------------

_CREATE_TENANT_CLI = Path(__file__).resolve().parent.parent / "scripts" / "create_tenant.py"


def _forbid_database_work(monkeypatch):
    """Fail the test if provisioning reaches its first database-touching step."""
    create_db = AsyncMock(side_effect=AssertionError("created a database for an invalid slug"))
    monkeypatch.setattr(tenant_provisioning, "_create_postgres_database", create_db)

    def _no_control_session(*_args, **_kwargs):
        raise AssertionError("opened a control-plane session for an invalid slug")

    monkeypatch.setattr(tenant_provisioning, "control_session_factory", _no_control_session)
    return create_db


@pytest.mark.parametrize(
    "bad_slug",
    [
        "api",  # reserved — with tenants on <slug>.<platform domain>, the API host
        "www",  # reserved
        "1acme",  # must start with a letter
        "Acme",  # upper-case
        "ac--me",  # consecutive hyphens
        "a" * 31,  # too long
    ],
)
async def test_provision_tenant_rejects_invalid_slug_before_any_database_work(
    monkeypatch, bad_slug
):
    # The API callers validate first, but scripts/create_tenant.py passes an
    # operator-typed slug straight through, so the check has to live here.
    create_db = _forbid_database_work(monkeypatch)

    with pytest.raises(SlugError):
        await provision_tenant(
            company_name="Nope Co",
            slug=bad_slug,
            admin_email="admin@nope.test",
            admin_name="Nope Admin",
            admin_password="Aa1-not-used-000000",
        )

    create_db.assert_not_awaited()


async def test_create_tenant_cli_exits_2_on_a_reserved_slug(monkeypatch, capsys):
    """A reserved slug stops the CLI with a readable error, not a traceback."""
    create_db = _forbid_database_work(monkeypatch)
    spec = importlib.util.spec_from_file_location("_create_tenant_cli", _CREATE_TENANT_CLI)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    engine = MagicMock()
    engine.dispose = AsyncMock()
    monkeypatch.setattr(cli, "control_engine", engine)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "create_tenant.py",
            "--name",
            "Api Co",
            "--slug",
            "api",
            "--admin-email",
            "admin@api.test",
            "--admin-password",
            "Aa1-not-used-000000",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        await cli.main()

    assert exc_info.value.code == 2
    assert "invalid slug 'api'" in capsys.readouterr().err
    create_db.assert_not_awaited()
    engine.dispose.assert_awaited_once()


# ---------------------------------------------------------------------------
# End-to-end provision_tenant against a live Postgres (realdb)
# ---------------------------------------------------------------------------


async def _control_mk():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _drop_database(db_name: str) -> None:
    """Drop a tenant DB we created in a test (mirrors the asyncpg URL parsing
    in ``_create_postgres_database``)."""
    import asyncpg

    url = settings.database_url.replace("postgresql+asyncpg://", "")
    userpass, hostdb = url.split("@", 1)
    user, password = userpass.split(":", 1)
    host_port, _ = hostdb.rsplit("/", 1)
    if ":" in host_port:
        host, port_str = host_port.split(":", 1)
        port = int(port_str)
    else:
        host, port = host_port, 5432
    conn = await asyncpg.connect(
        host=host, port=port, user=user, password=password, database="postgres"
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    finally:
        await conn.close()


async def _cleanup_org(slug: str) -> None:
    """Delete the control-plane org + its users + the tenant DB for a slug."""
    engine, mk = await _control_mk()
    db_name = f"{settings.tenant_db_prefix}{slug}"
    try:
        async with mk() as s:
            org = (
                await s.execute(select(Organization).where(Organization.slug == slug))
            ).scalar_one_or_none()
            if org is not None:
                users = (
                    (await s.execute(select(User).where(User.organization_id == org.id)))
                    .scalars()
                    .all()
                )
                for u in users:
                    await s.execute(UserRole.__table__.delete().where(UserRole.user_id == u.id))
                    await s.delete(u)
                # provision_tenant now binds every org to a baseline "free"
                # Subscription (issue #180) — drop it before the org, or the
                # FK to organizations blocks the delete. The shared Plan
                # catalog rows are process-wide and outlive this cleanup.
                await s.execute(
                    Subscription.__table__.delete().where(Subscription.organization_id == org.id)
                )
                await s.delete(org)
                await s.commit()
    finally:
        await engine.dispose()
    await _drop_database(db_name)


@pytest.fixture
def throwaway_slug():
    # Slug that survives slug-format rules (starts with a letter, lowercase,
    # hyphen-only) and is unique per run so concurrent/repeat runs don't clash.
    slug = "prov" + uuid.uuid4().hex[:8]
    return slug


@pytest_asyncio.fixture
async def _provision_on_test_loop():
    """Bind provision_tenant's control_session_factory to an engine created on
    THIS test's event loop.

    The module-global factory's engine binds to whichever loop first touched it
    (the first provisioning test), so subsequent tests on fresh per-test loops
    otherwise fail with 'attached to a different loop'. We patch in a fresh
    factory and dispose its engine after the test.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.services.tenant_provisioning as tp

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with patch.object(tp, "control_session_factory", factory):
        yield
    await engine.dispose()


async def test_provision_tenant_creates_org_user_and_tenant_tables(
    realdb, throwaway_slug, _provision_on_test_loop
):
    slug = throwaway_slug
    db_name = f"{settings.tenant_db_prefix}{slug}"
    try:
        result = await provision_tenant(
            company_name="Throwaway Corp",
            slug=slug,
            admin_email=f"admin@{slug}.test",
            admin_name="Throwaway Admin",
            admin_password="Sup3rSecret!pw",
        )

        # Result carries the slug -> feoh_<slug> mapping.
        assert result.db_name == db_name
        assert isinstance(result.organization_id, uuid.UUID)
        assert isinstance(result.user_id, uuid.UUID)

        engine, mk = await _control_mk()
        try:
            async with mk() as s:
                org = (
                    await s.execute(
                        select(Organization).where(Organization.id == result.organization_id)
                    )
                ).scalar_one()
                assert org.slug == slug
                assert org.db_name == db_name
                assert org.name == "Throwaway Corp"
                assert org.plan == "free"

                user = (await s.execute(select(User).where(User.id == result.user_id))).scalar_one()
                assert user.email == f"admin@{slug}.test"
                assert user.full_name == "Throwaway Admin"
                assert user.is_active is True
                assert user.organization_id == result.organization_id
                # Default forces a password change on first login.
                assert user.must_change_password is True
                # Password is hashed (never stored plaintext) and verifies via
                # the shared bcrypt_sha256 context.
                assert user.hashed_password != "Sup3rSecret!pw"
                assert pwd_context.verify("Sup3rSecret!pw", user.hashed_password)

                # The admin role was granted (the realdb control plane seeds the
                # RBAC roles, so provision_tenant attaches the user to "admin").
                admin_role = (
                    await s.execute(select(Role).where(Role.name == "admin"))
                ).scalar_one()
                ur_count = (
                    await s.execute(
                        select(func.count())
                        .select_from(UserRole)
                        .where(
                            UserRole.user_id == result.user_id,
                            UserRole.role_id == admin_role.id,
                        )
                    )
                ).scalar_one()
                assert ur_count == 1

                # Issue #180: provisioning must bind a live "free" Subscription
                # so the org isn't locked out of GET /api/billing/subscription
                # and change-plan (which 404s "no live subscription" without a
                # starting row) forever.
                from app.models.billing import Plan, Subscription

                sub = (
                    await s.execute(
                        select(Subscription).where(
                            Subscription.organization_id == result.organization_id
                        )
                    )
                ).scalar_one()
                assert sub.status == "active"
                plan = (await s.execute(select(Plan).where(Plan.id == sub.plan_id))).scalar_one()
                assert plan.code == "free"
        finally:
            await engine.dispose()

        # The tenant DB exists and the tenant tables fanned out (migrations
        # equivalent: create_all over every non-control table).
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.database import _make_tenant_url

        tenant_engine = create_async_engine(_make_tenant_url(db_name))
        try:
            async with tenant_engine.connect() as conn:
                for tbl in ("invoices", "vendors", "payments", "entities", "peppol_transmissions"):
                    present = await conn.exec_driver_sql(f"SELECT to_regclass('{tbl}')")
                    assert present.scalar() is not None, f"tenant table {tbl} missing"
                # The PEPPOL idempotency guard + message-id dedupe must be built
                # by create_all on a fresh tenant — identical to the migration.
                peppol_indexes = await conn.exec_driver_sql(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'peppol_transmissions'"
                )
                built = {row[0] for row in peppol_indexes}
                assert "uq_peppol_one_live_per_invoice_direction" in built
                assert "uq_peppol_message_id" in built
                # And a control-plane table must NOT have been created here.
                orgs_here = await conn.exec_driver_sql("SELECT to_regclass('organizations')")
                assert orgs_here.scalar() is None
                # Multi-entity: provisioning seeds exactly one Default entity,
                # owned by the new org, and the business tables carry entity_id.
                default_count = await conn.exec_driver_sql(
                    "SELECT count(*) FROM entities WHERE is_default"
                )
                assert default_count.scalar() == 1
                default_org = await conn.exec_driver_sql(
                    "SELECT organization_id FROM entities WHERE is_default"
                )
                assert default_org.scalar() == result.organization_id
                has_col = await conn.exec_driver_sql(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'invoices' AND column_name = 'entity_id'"
                )
                assert has_col.scalar() == 1
                # Every tenant is provisioned with a real, ENABLED default
                # workflow. Without one, the first invoice fell through to
                # `get_or_create_workflow_definition`'s lazy fallback — whose
                # approval step used to be disabled, so `complete_invoice`
                # skipped every branch and transitioned the invoice straight
                # to the terminal `done`: no approval, no approval signature,
                # no `invoice.approved` audit row, no segregation check and no
                # CFO gate, for every self-service tenant.
                # Scoped to the tenant's DEFAULT entity, matching what
                # migration 0029 backfilled onto every existing tenant — a
                # fresh tenant seeded into the shared (NULL) bucket would be
                # the only shape whose definitions live there, and the
                # one-active-per-scope invariant would behave differently for
                # it than for every migrated tenant.
                wf_count = await conn.exec_driver_sql(
                    "SELECT count(*) FROM workflow_definitions wd "
                    "JOIN entities e ON e.id = wd.entity_id AND e.is_default "
                    "WHERE wd.is_default AND wd.is_active"
                )
                assert wf_count.scalar() == 1
                approval_enabled = await conn.exec_driver_sql(
                    "SELECT bool_or((step->>'enabled')::boolean) "
                    "FROM workflow_definitions, "
                    "     jsonb_array_elements(steps_config->'steps') AS step "
                    "WHERE is_default AND step->>'type' = 'approval'"
                )
                assert approval_enabled.scalar() is True
                # Perf: the vendor-scoped history index (bank-change /
                # stat-anomaly / price-variance lookups in invoice_warnings.py)
                # and the duplicate-detection functional index must be built by
                # create_all on a fresh tenant — identical to migration 0072.
                invoice_indexes = await conn.exec_driver_sql(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'invoices'"
                )
                built_invoice_indexes = {row[0] for row in invoice_indexes}
                assert "ix_invoices_vendor_id_created_at" in built_invoice_indexes
                assert "ix_invoices_invoice_number_norm" in built_invoice_indexes
                # Perf: the PO-matching lookups (services.po_matching.
                # match_invoice_to_po, called for every invoice with a
                # po_number) must also be indexed by create_all — identical
                # to migration 0073.
                po_indexes = await conn.exec_driver_sql(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'purchase_orders'"
                )
                built_po_indexes = {row[0] for row in po_indexes}
                assert "ix_purchase_orders_po_number" in built_po_indexes
                assert "ix_purchase_orders_vendor_id" in built_po_indexes
                gr_indexes = await conn.exec_driver_sql(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'goods_receipts'"
                )
                assert "ix_goods_receipts_po_id" in {row[0] for row in gr_indexes}
                po_li_indexes = await conn.exec_driver_sql(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'po_line_items'"
                )
                assert "ix_po_line_items_po_id" in {row[0] for row in po_li_indexes}
                gr_li_indexes = await conn.exec_driver_sql(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'gr_line_items'"
                )
                assert "ix_gr_line_items_gr_id" in {row[0] for row in gr_li_indexes}
        finally:
            await tenant_engine.dispose()
    finally:
        await _cleanup_org(slug)


async def test_provision_tenant_respects_must_change_password_false(
    realdb, throwaway_slug, _provision_on_test_loop
):
    slug = throwaway_slug
    try:
        result = await provision_tenant(
            company_name="Internal Co",
            slug=slug,
            admin_email=f"admin@{slug}.test",
            admin_name="Internal Admin",
            admin_password="Sup3rSecret!pw",
            plan="enterprise",
            must_change_password=False,
        )
        engine, mk = await _control_mk()
        try:
            async with mk() as s:
                org = (
                    await s.execute(
                        select(Organization).where(Organization.id == result.organization_id)
                    )
                ).scalar_one()
                assert org.plan == "enterprise"
                user = (await s.execute(select(User).where(User.id == result.user_id))).scalar_one()
                assert user.must_change_password is False
        finally:
            await engine.dispose()
    finally:
        await _cleanup_org(slug)


async def test_provision_tenant_duplicate_slug_raises_and_keeps_one_org(
    realdb, throwaway_slug, _provision_on_test_loop
):
    slug = throwaway_slug
    try:
        await provision_tenant(
            company_name="First Co",
            slug=slug,
            admin_email=f"first@{slug}.test",
            admin_name="First Admin",
            admin_password="Sup3rSecret!pw",
        )

        # Re-provisioning the same slug must fail (Organization.slug is unique,
        # Organization.db_name is unique) — provisioning is NOT silently
        # idempotent at the org level, it surfaces the collision.
        with pytest.raises(Exception):
            await provision_tenant(
                company_name="Second Co",
                slug=slug,
                admin_email=f"second@{slug}.test",
                admin_name="Second Admin",
                admin_password="Sup3rSecret!pw",
            )

        # Exactly one org for the slug survived (the first one); the failed
        # second attempt left no partial org row.
        engine, mk = await _control_mk()
        try:
            async with mk() as s:
                count = (
                    await s.execute(
                        select(func.count())
                        .select_from(Organization)
                        .where(Organization.slug == slug)
                    )
                ).scalar_one()
                assert count == 1
                org = (
                    await s.execute(select(Organization).where(Organization.slug == slug))
                ).scalar_one()
                assert org.name == "First Co"
        finally:
            await engine.dispose()
    finally:
        await _cleanup_org(slug)


async def test_provision_tenant_org_can_upgrade_out_of_free(
    realdb, throwaway_slug, _provision_on_test_loop
):
    """Issue #180: before the baseline Subscription, `change_plan` 404'd with
    "no live subscription" for EVERY org, since none ever had a starting
    subscription to move from — an admin could never upgrade at all, and
    `public_api` (the only entitlement gated in the app) was permanently
    unreachable. Prove a freshly provisioned org starts un-entitled (free
    grants nothing) but CAN move to a paid plan and gain the entitlement.
    """
    from app.models.billing import Plan, Subscription
    from app.services.billing.entitlements import get_entitlements
    from app.services.billing.plan_change import change_plan

    slug = throwaway_slug
    try:
        result = await provision_tenant(
            company_name="Upgrade Co",
            slug=slug,
            admin_email=f"admin@{slug}.test",
            admin_name="Upgrade Admin",
            admin_password="Sup3rSecret!pw",
        )

        engine, mk = await _control_mk()
        try:
            async with mk() as s:
                org = (
                    await s.execute(
                        select(Organization).where(Organization.id == result.organization_id)
                    )
                ).scalar_one()

                # Free grants nothing — the public API stays gated by default.
                entitlements = await get_entitlements(s, org.id)
                assert entitlements == {}

                # The upgrade that used to 404 now succeeds.
                change_result = await change_plan(
                    s, org=org, new_plan_code="growth", actor_id=result.user_id
                )
                await s.commit()
                assert change_result.changed is True
                assert change_result.new_plan_code == "growth"

                entitlements_after = await get_entitlements(s, org.id)
                assert entitlements_after.get("public_api") is True

                # Still exactly one live subscription — the plan was moved in
                # place, not duplicated.
                live_count = (
                    await s.execute(
                        select(func.count())
                        .select_from(Subscription)
                        .where(
                            Subscription.organization_id == org.id,
                            Subscription.status != "canceled",
                        )
                    )
                ).scalar_one()
                assert live_count == 1
                plan = (await s.execute(select(Plan).where(Plan.code == "growth"))).scalar_one()
                assert plan.entitlements.get("public_api") is True
        finally:
            await engine.dispose()
    finally:
        await _cleanup_org(slug)


async def _database_exists(db_name: str) -> bool:
    import asyncpg

    from app.services.tenant_provisioning import _parse_maintenance_dsn

    conn = await asyncpg.connect(**_parse_maintenance_dsn())
    try:
        return bool(await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name))
    finally:
        await conn.close()


async def test_provision_tenant_drops_orphan_db_when_provisioning_fails(
    throwaway_slug, _provision_on_test_loop
):
    """If the control-plane insert / tenant-table step fails AFTER the database
    was created, provision_tenant drops the orphan DB so a partial failure
    doesn't leak databases or squat the slug's namespace."""
    import app.services.tenant_provisioning as tp

    slug = throwaway_slug
    db_name = f"{settings.tenant_db_prefix}{slug}"

    async def _boom(**_kwargs):
        raise RuntimeError("simulated provisioning failure after DB creation")

    with patch.object(tp, "_provision_into", _boom):
        with pytest.raises(RuntimeError):
            await provision_tenant(
                company_name="Doomed Co",
                slug=slug,
                admin_email=f"admin@{slug}.test",
                admin_name="Doomed Admin",
                admin_password="Sup3rSecret!pw",
            )

    # The database created at the start of provisioning must have been dropped.
    assert await _database_exists(db_name) is False


# ---------------------------------------------------------------------------
# Role-name uniqueness (the constraint provision_tenant's role lookup relies on)
# ---------------------------------------------------------------------------


async def test_duplicate_system_role_name_is_rejected(realdb):
    """Two system roles (organization_id NULL) with the same name are rejected
    by ``uq_roles_system_name``. Without it, a re-seeded control plane could
    accumulate duplicate ``admin`` rows and ``provision_tenant``'s
    ``scalar_one_or_none()`` role lookup raised ``MultipleResultsFound``."""
    from sqlalchemy.exc import IntegrityError

    engine, mk = await _control_mk()
    try:
        async with mk() as s:
            # "admin" already exists as a system role in the seeded control
            # plane — a second one must violate the partial unique index.
            s.add(Role(name="admin", description="dup system admin"))
            with pytest.raises(IntegrityError):
                await s.flush()
            await s.rollback()
    finally:
        await engine.dispose()


async def test_org_scoped_roles_unique_within_org_not_across(realdb):
    """Org-scoped custom roles are unique only within an org: two different
    orgs may both define ``Approver`` (``uq_roles_org_name`` keys on
    ``(organization_id, name)``), but one org cannot define it twice."""
    from sqlalchemy.exc import IntegrityError

    engine, mk = await _control_mk()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    try:
        async with mk() as s:
            # Same name across two different orgs is allowed — flush succeeds.
            s.add_all(
                [
                    Role(name="Approver", organization_id=org_a),
                    Role(name="Approver", organization_id=org_b),
                ]
            )
            await s.flush()
            # A second "Approver" in org_a violates uq_roles_org_name.
            s.add(Role(name="Approver", organization_id=org_a))
            with pytest.raises(IntegrityError):
                await s.flush()
            # Roll back everything — the cross-org pair was only flushed, never
            # committed, so this test persists nothing.
            await s.rollback()
    finally:
        await engine.dispose()


async def test_organization_slug_exists_precheck(realdb, throwaway_slug, _provision_on_test_loop):  # noqa: ARG001
    """The --skip-existing pre-check used by deploy/add-tenant.sh: False before
    provisioning, True after — so a partially-failed tenant add (e.g. Caddy
    reload died after the tenant landed) can be re-run without tripping
    provision_tenant's deliberate duplicate-slug raise."""
    from app.services.tenant_provisioning import organization_slug_exists

    slug = throwaway_slug
    assert not await organization_slug_exists(slug)
    try:
        await provision_tenant(
            company_name="Precheck Corp",
            slug=slug,
            admin_email=f"admin@{slug}.test",
            admin_name="Precheck Admin",
            admin_password="Sup3rSecret!pw",
        )
        assert await organization_slug_exists(slug)
    finally:
        await _cleanup_org(slug)
