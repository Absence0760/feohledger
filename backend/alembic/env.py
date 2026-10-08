import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config import settings
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

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


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
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
