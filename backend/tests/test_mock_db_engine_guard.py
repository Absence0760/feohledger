"""The conftest guard that refuses a real database connection from a mock-DB test.

A mock-DB test that silently reached `app.database`'s module-global engines
bound a pooled asyncpg connection to its own event loop; a later test in the
same process then drew it on a new loop and failed with "got Future attached to
a different loop" — far from the cause, and only when the files shared a process
(`docs/known-issues.md`, the payment mock tests reaching `provider_credentials`).
`_refuse_real_engine_in_mock_tests` makes the offending test fail instead, at
the call that tried.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.database import control_session_factory, forget_tenant_engine, get_tenant_engine
from app.services import provider_credentials
from tests.conftest import RealEngineInMockTestError


async def test_control_plane_session_is_refused_in_a_mock_test(request):
    with pytest.raises(RealEngineInMockTestError, match="not a realdb test"):
        async with control_session_factory() as session:
            await session.execute(text("SELECT 1"))
    assert request.node._feoh_real_engine_hits
    request.node._feoh_real_engine_hits.clear()


async def test_tenant_engine_is_refused_in_a_mock_test(request):
    with pytest.raises(RealEngineInMockTestError):
        async with get_tenant_engine("feoh_guard_probe").connect() as conn:
            await conn.execute(text("SELECT 1"))
    request.node._feoh_real_engine_hits.clear()
    await forget_tenant_engine("feoh_guard_probe")


async def test_provider_credentials_without_the_fixture_is_refused(request):
    """The path that found this: the accessor opens its own control session
    when no ``db`` is passed."""
    with pytest.raises(RealEngineInMockTestError):
        await provider_credentials.resolve_block(
            uuid.uuid4(), {"payments": {"provider": "mock"}}, "payments"
        )
    request.node._feoh_real_engine_hits.clear()


@pytest.mark.usefixtures("provider_store_from_settings")
async def test_provider_store_fixture_keeps_the_accessor_off_the_engine():
    cfg = await provider_credentials.resolve_block(
        uuid.uuid4(), {"payments": {"provider": "mock"}}, "payments"
    )
    assert cfg == {"provider": "mock"}


async def test_realdb_test_is_exempt(realdb):  # noqa: ARG001
    async with control_session_factory() as session:
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
