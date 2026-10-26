"""Phase 5: the OAuth ERP connection-health sweep (decisions §295).

It reads the refresh-token expiry the provider last REPORTED (rotated on every
refresh), keeps a quiet connection alive, and tells the org's admins — in-app
and by email, through `notify_event` — before the consent expires and once it
is lost, each notice exactly once per (expiry, lead) or per lost connection.
The org row is real (`realdb`); the refresher and the notification fan-out are
faked at their seams, which have their own suites.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.organization import Organization
from app.services import erp_connection_health as health
from app.services import erp_oauth

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _leads(monkeypatch):
    monkeypatch.setattr(settings, "erp_connection_expiry_notice_days", [30, 7, 1])
    monkeypatch.setattr(settings, "erp_connection_keepalive_days", 45)


def test_due_lead_picks_the_most_urgent_lead_not_yet_sent():
    at = "2026-11-01T00:00:00+00:00"
    assert health.due_lead(40, None, at) is None
    assert health.due_lead(29.5, None, at) == 30
    assert health.due_lead(29.5, {"expires_at": at, "lead_days": 30}, at) is None
    assert health.due_lead(6, {"expires_at": at, "lead_days": 30}, at) == 7
    assert health.due_lead(6, {"expires_at": at, "lead_days": 7}, at) is None
    # A new expiry (a rotation, a reconnect) re-arms every lead.
    assert health.due_lead(6, {"expires_at": "older", "lead_days": 1}, at) == 7


def test_expiring_soon_is_inside_the_first_lead():
    soon = {"refresh_token_expires_at": (NOW + timedelta(days=10)).isoformat()}
    later = {"refresh_token_expires_at": (NOW + timedelta(days=60)).isoformat()}
    assert health.expiring_soon(soon, now=NOW) is True
    assert health.expiring_soon(later, now=NOW) is False
    assert health.expiring_soon({}, now=NOW) is False


async def _connect(realdb, **oauth) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
        org.settings = {
            **(org.settings or {}),
            "erp": {
                "type": "quickbooks_online",
                "integration_method": "direct",
                "oauth": {
                    "provider": "quickbooks_online",
                    "external_tenant_id": f"health-{org.id}",
                    "org_id": str(org.id),
                    "connection_id": "conn-1",
                    **oauth,
                },
            },
        }
        await s.commit()


async def _oauth(realdb) -> dict:
    async with realdb.control_sessionmaker()() as s:
        value = (
            await s.execute(
                select(Organization.settings).where(Organization.id == realdb.info("a").org_id)
            )
        ).scalar_one()
    return value["erp"]["oauth"]


async def _org(realdb) -> Organization:
    async with realdb.control_sessionmaker()() as s:
        return await s.get(Organization, realdb.info("a").org_id)


async def _check(realdb, *, notified=True, refresh=None, now=NOW):
    result = health.ConnectionHealthResult()
    notify = AsyncMock(return_value=notified)
    refresher = refresh or AsyncMock(return_value="tok")
    with (
        patch.object(health, "notify_org_admins", notify),
        patch.object(health.erp_oauth, "get_access_token", refresher),
        patch.object(health, "_resolved_erp", AsyncMock(return_value={"x": 1})),
    ):
        await health.check_connection(await _org(realdb), now=now, result=result)
    return result, notify, refresher


async def test_an_expiring_connection_is_announced_once_per_lead(realdb, monkeypatch):
    monkeypatch.setattr(settings, "erp_connection_keepalive_days", 0)  # no keep-alive here
    expires = (NOW + timedelta(days=20)).isoformat()
    await _connect(realdb, refresh_token_expires_at=expires)

    result, notify, _ = await _check(realdb)
    assert result.expiry_notices == 1
    (rendered,) = notify.await_args.args[1:]
    assert "2026-11-09" in rendered.title  # the date the provider reported
    assert (await _oauth(realdb))[health.EXPIRY_NOTICE_KEY] == {
        "expires_at": erp_oauth.parse_timestamp(expires).isoformat(),
        "lead_days": 30,
    }

    # A daily sweep does not repeat it…
    result, notify, _ = await _check(realdb)
    assert result.expiry_notices == 0 and notify.await_count == 0
    # …until the next, more urgent lead is crossed.
    result, notify, _ = await _check(realdb, now=NOW + timedelta(days=14))
    assert result.expiry_notices == 1
    assert (await _oauth(realdb))[health.EXPIRY_NOTICE_KEY]["lead_days"] == 7


async def test_nobody_told_leaves_the_marker_unwritten(realdb, monkeypatch):
    monkeypatch.setattr(settings, "erp_connection_keepalive_days", 0)
    await _connect(realdb, refresh_token_expires_at=(NOW + timedelta(days=3)).isoformat())
    result, _, _ = await _check(realdb, notified=False)
    assert result.expiry_notices == 0
    assert health.EXPIRY_NOTICE_KEY not in await _oauth(realdb)


async def test_a_quiet_connection_is_kept_alive_instead_of_warned(realdb):
    await _connect(realdb, refresh_token_expires_at=(NOW + timedelta(days=40)).isoformat())

    async def rotate(spec, config, *, force_refresh=False, **_):
        assert force_refresh is True
        # What a rotation does: the provider reports a later expiry.
        async with realdb.control_sessionmaker()() as s:
            org = await s.get(Organization, realdb.info("a").org_id)
            erp = dict(org.settings["erp"])
            erp["oauth"] = {
                **erp["oauth"],
                "refresh_token_expires_at": (NOW + timedelta(days=100)).isoformat(),
            }
            org.settings = {**org.settings, "erp": erp}
            await s.commit()
        return "tok-2"

    result, notify, refresher = await _check(realdb, refresh=AsyncMock(side_effect=rotate))
    assert result.refreshed == 1
    assert result.expiry_notices == 0 and notify.await_count == 0


async def test_a_refresh_that_cannot_extend_still_warns(realdb):
    """Intuit's lifetime cap: the refresh succeeds but the expiry does not move."""
    await _connect(realdb, refresh_token_expires_at=(NOW + timedelta(days=5)).isoformat())
    result, notify, _ = await _check(realdb)
    assert result.refreshed == 1 and result.expiry_notices == 1
    assert (await _oauth(realdb))[health.EXPIRY_NOTICE_KEY]["lead_days"] == 7


async def test_a_lost_connection_is_announced_once(realdb):
    await _connect(
        realdb,
        needs_reconnect=True,
        reconnect_reason=erp_oauth.RECONNECT_REVOKED,
        refresh_token_expires_at=(NOW + timedelta(days=50)).isoformat(),
    )
    result, notify, refresher = await _check(realdb)
    assert result.reconnect_notices == 1
    refresher.assert_not_awaited()  # nothing to refresh with
    (rendered,) = notify.await_args.args[1:]
    assert "revoked" in rendered.body_text
    result, notify, _ = await _check(realdb)
    assert result.reconnect_notices == 0 and notify.await_count == 0


async def test_the_sweep_counts_one_orgs_failure_and_carries_on(realdb, monkeypatch):
    monkeypatch.setattr(settings, "erp_connection_keepalive_days", 0)
    await _connect(realdb, refresh_token_expires_at=(NOW + timedelta(days=2)).isoformat())
    with patch.object(health, "notify_org_admins", AsyncMock(side_effect=RuntimeError("smtp"))):
        result = await health.run_connection_health_once(now=NOW)
    assert result.failures >= 1
