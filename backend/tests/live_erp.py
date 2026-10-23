"""Shared plumbing for the opt-in live ERP contract checks.

``test_xero_live_contract.py`` (``pnpm test:erp:xero-live``) and
``test_qbo_live_contract.py`` (``pnpm test:erp:qbo-live``) each read the ERP
connection a LOCAL tenant already holds and ask the real provider whether it
still behaves the way the adapter, and ``tools/fake-erp``, believe. Both need
the same two things, so they live here once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


def load_local_settings() -> None:
    """Give this process the same settings ``python main.py`` would have.

    ``app.config.settings`` was built when the suite imported ``app``, from the
    bare process environment. The tenant's sealed tokens and the app
    credentials live in ``backend/.env`` / ``.env.development``, which only the
    local entrypoint loads, so load them the same way (``.env`` first, neither
    overriding the real environment) and refresh the settings object in place.
    """
    from dotenv import load_dotenv

    from app.config import Settings, settings

    backend = Path(__file__).resolve().parents[1]
    load_dotenv(backend / ".env", override=False)
    load_dotenv(backend / ".env.development", override=False)
    fresh = Settings()
    for name in type(fresh).model_fields:
        setattr(settings, name, getattr(fresh, name))


@asynccontextmanager
async def live_erp_adapter(tenant: str, erp_type: str, *, env_var: str) -> AsyncIterator:
    """The tenant's live adapter, built the way a sync builds it.

    On a NullPool engine bound to the calling test's event loop: every test
    runs on its own loop, and a pooled asyncpg connection cannot cross loops.
    ``dispatch_engine_scope`` points the adapter's own control-plane reads (the
    token store) at the same engine. Opted in but not connected is a FAILURE,
    not a skip: a green run must mean the provider was actually asked.
    """
    load_local_settings()
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.pool import NullPool

    from app.config import settings
    from app.database import dispatch_engine_scope
    from app.db_engine import create_async_engine
    from app.models.organization import Organization
    from app.services.erp_adapters import get_erp_adapter
    from app.services.provider_credentials import provider_config

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with dispatch_engine_scope(control_sessionmaker=sessions):
            async with sessions() as db:
                org = (
                    await db.execute(select(Organization).where(Organization.slug == tenant))
                ).scalar_one_or_none()
                assert org is not None, f"no local tenant '{tenant}' ({env_var})"
                config = await provider_config(org, "erp", db=db) or {}
            assert config.get("type") == erp_type, (
                f"tenant '{tenant}' is not set to {erp_type} — connect it under "
                "Organization → ERP Integration"
            )
            yield get_erp_adapter(config)
    finally:
        await engine.dispose()
