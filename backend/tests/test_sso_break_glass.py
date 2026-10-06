"""Operator break-glass for an SSO-only tenant (`scripts/sso_break_glass.py`).

`is_sso_only` closes the password whenever the IdP block *resolves*, a local
check by design (§204). A complete block whose IdP is down or whose client
secret expired therefore locks every member out, and the setting that would
reopen the password is behind the sign-in it blocks. This is the documented way
back in: it clears `sso_only` and nothing else, and it records the lift in the
tenant's audit trail before changing anything.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services import audit_dispatch
from app.services.sso import is_sso_only
from app.services.sso_break_glass import (
    ACTION,
    AuditWriteFailed,
    TenantNotFound,
    lift_sso_only,
)

SECRET = "break-glass-secret-that-must-not-echo"
OIDC_SSO_ONLY = {
    "enabled": True,
    "sso_only": True,
    "provider": "entra",
    "discovery_url": "https://login.example.com/.well-known/openid-configuration",
    "client_id": "feoh",
    "client_secret": SECRET,
    "scim_bearer_hash": "cd" * 32,
    "scim_groups": {"g1": {"displayName": "Finance", "members": []}},
    "scim_group_role_map": {"Finance": "cfo"},
}


@pytest.fixture
def fake_redis(monkeypatch):
    """A successful login registers a session, which needs the zset+hash Redis
    stand-in rather than the key/value-only autouse stub (as test_sso_only)."""
    from tests.test_session_management import FakeRedis

    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


async def _seed(realdb, block) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()
        org.settings = {**(org.settings or {}), "company": {"address": "1 Main"}, "sso": block}
        flag_modified(org, "settings")
        await s.commit()


async def _settings(realdb) -> dict:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()
    return dict(org.settings or {})


async def _audit_rows(realdb) -> list[AuditLog]:
    async with realdb.sessionmaker("a")() as s:
        return list(
            (await s.execute(select(AuditLog).where(AuditLog.action == ACTION))).scalars().all()
        )


@pytest.mark.asyncio
async def test_lifts_sso_only_and_nothing_else(realdb):
    await _seed(realdb, dict(OIDC_SSO_ONLY))
    assert is_sso_only(await _settings(realdb))

    async with realdb.control_sessionmaker()() as s:
        result = await lift_sso_only(s, realdb.info("a").slug, reason="TICKET-1 secret expired")

    assert result.lifted is True
    assert result.password_was_closed is True
    after = await _settings(realdb)
    assert after["sso"] == {**OIDC_SSO_ONLY, "sso_only": False}
    assert after["company"] == {"address": "1 Main"}
    assert not is_sso_only(after)

    rows = await _audit_rows(realdb)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_id is None
    assert row.entity_type == "organization"
    assert row.entity_id == realdb.info("a").org_id
    assert row.details == {
        "source": "operator_break_glass",
        "password_was_closed": True,
        "reason": "TICKET-1 secret expired",
    }
    assert SECRET not in str(row.details)


@pytest.mark.asyncio
async def test_the_password_opens_again(realdb, fake_redis):
    """End to end: the password the 403 refused before the lift signs in after."""
    await _seed(realdb, dict(OIDC_SSO_ONLY))
    # The harness seeds every user with this password (tests/conftest.py).
    login = {"email": realdb.email("a", "admin"), "password": "Passw0rd!xyz"}
    async with realdb.client(key="a", role=None) as c:
        refused = await c.post("/api/auth/login", json=login)
    assert refused.status_code == 403, refused.text

    async with realdb.control_sessionmaker()() as s:
        await lift_sso_only(s, realdb.info("a").slug)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/auth/login", json=login)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block",
    [
        pytest.param({**OIDC_SSO_ONLY, "sso_only": False}, id="sso_only-off"),
        pytest.param(None, id="no-sso-block"),
    ],
)
async def test_nothing_to_lift_writes_nothing(realdb, block):
    await _seed(realdb, block)
    before = await _settings(realdb)
    async with realdb.control_sessionmaker()() as s:
        result = await lift_sso_only(s, realdb.info("a").slug)
    assert result.lifted is False
    assert await _settings(realdb) == before
    assert await _audit_rows(realdb) == []


@pytest.mark.asyncio
async def test_an_unresolvable_block_is_lifted_and_says_the_password_was_open(realdb):
    """`sso_only` over a block that does not resolve never closed the password
    (§204's escape hatch); the lift still clears it, and the row says so."""
    await _seed(realdb, {"enabled": True, "sso_only": True})
    async with realdb.control_sessionmaker()() as s:
        result = await lift_sso_only(s, realdb.info("a").slug)
    assert result.lifted is True
    assert result.password_was_closed is False
    assert (await _audit_rows(realdb))[0].details["password_was_closed"] is False


@pytest.mark.asyncio
async def test_unknown_slug(realdb):
    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(TenantNotFound):
            await lift_sso_only(s, "no-such-tenant-zz")


@pytest.mark.asyncio
async def test_no_audit_row_means_no_lift(realdb, monkeypatch):
    """An operator edit to a tenant's sign-in policy with no record is what
    this replaces, so a failed audit write leaves the tenant as it was."""
    await _seed(realdb, dict(OIDC_SSO_ONLY))

    async def _fail(**_kwargs):
        raise ConnectionError("audit store down")

    monkeypatch.setattr(audit_dispatch, "_write_auth_audit", _fail)
    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(AuditWriteFailed):
            await lift_sso_only(s, realdb.info("a").slug)
    assert (await _settings(realdb))["sso"]["sso_only"] is True


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "sso_break_glass.py"
    spec = importlib.util.spec_from_file_location("sso_break_glass_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_script_exit_codes(realdb, capsys, monkeypatch):
    script = _load_script()
    args = script.build_parser().parse_args(["--slug", realdb.info("a").slug, "--reason", "T-9"])
    assert args.reason == "T-9"

    await _seed(realdb, dict(OIDC_SSO_ONLY))
    assert await script.run(realdb.info("a").slug, "T-9") == 0
    assert "Cleared sso_only" in capsys.readouterr().out
    assert (await _settings(realdb))["sso"]["sso_only"] is False

    assert await script.run(realdb.info("a").slug, None) == 0
    assert "Nothing changed" in capsys.readouterr().out

    assert await script.run("no-such-tenant-zz", None) == 1

    await _seed(realdb, dict(OIDC_SSO_ONLY))

    async def _fail(**_kwargs):
        raise ConnectionError("audit store down")

    monkeypatch.setattr(audit_dispatch, "_write_auth_audit", _fail)
    assert await script.run(realdb.info("a").slug, None) == 2
    assert "NOT cleared" in capsys.readouterr().err
    assert (await _settings(realdb))["sso"]["sso_only"] is True
