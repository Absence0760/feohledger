import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool

from alembic import context
from app.config import settings
from app.db_engine import create_async_engine
from app.models import Base
from app.tenant_url import make_tenant_url

config = context.config

# Support per-tenant migrations via FEOH_MIGRATE_TENANT env var. The tenant URL
# is built by the app's one builder, which keeps the control URL's query string
# (e.g. a TLS option) — a bare rsplit("/") dropped it, so tenant migrations ran
# with different connection options from the control-plane one.
tenant_db = os.environ.get("FEOH_MIGRATE_TENANT")
if tenant_db:
    url = make_tenant_url(settings.database_url, tenant_db)
else:
    url = settings.database_url

# set_main_option goes through configparser, which reads `%` as interpolation:
# a percent-encoded password (`p%40ss`) would raise "invalid interpolation
# syntax" before any migration ran. Doubling it is configparser's escape.
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

# disable_existing_loggers=False: fileConfig's default disables every logger
# that already exists, so an in-process run (a test driving alembic.command, or
# any caller importing the app first) silenced the app's loggers for the rest of
# the process, which the log-redaction tests then read as empty output.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

#: Where a tenant run exposes its organization's ``settings.erp`` to revisions
#: (``config.attributes``). A tenant database holds no organization settings,
#: and a revision that stamps something by the ERP connection the tenant has
#: now (0131: the Intacct entity, docs/decisions.md §412) reads it here.
TENANT_ORGANIZATION_ERP = "tenant_organization_erp"


def _tenant_organization_erp(db_name: str):
    """A lookup of ``db_name``'s ``organizations.settings->'erp'`` on the control
    plane, run only when a revision calls it, and once.

    Called from inside a revision, which Alembic runs synchronously on the
    tenant connection's greenlet, so the control-plane read is awaited with
    ``await_only``. Through the app's engine factory, so it authenticates the
    way the migration itself does. None: no organization names this database,
    or it has no ERP block. A read that fails raises, and fails the run."""
    cached: list = []

    def lookup():
        if not cached:
            from sqlalchemy import text
            from sqlalchemy.util import await_only

            async def fetch():
                engine = create_async_engine(settings.database_url, poolclass=pool.NullPool)
                try:
                    async with engine.connect() as conn:
                        sql = "SELECT settings->'erp' FROM organizations WHERE db_name = :d"
                        result = await conn.execute(text(sql), {"d": db_name})
                        return result.scalar_one_or_none()
                finally:
                    await engine.dispose()

            cached.append(await_only(fetch()))
        return cached[0]

    return lookup


if tenant_db:
    config.attributes[TENANT_ORGANIZATION_ERP] = _tenant_organization_erp(tenant_db)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Through the app's one engine factory, so a migration authenticates the
    # way the app does (FEOH_DATABASE_IAM_AUTH: a fresh IAM token per connection)
    # — `async_engine_from_config` would bypass it. `url` is the unescaped value
    # set above; the ini copy exists for offline mode and `alembic` itself.
    connectable = create_async_engine(url, poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
