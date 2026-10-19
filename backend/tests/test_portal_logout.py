"""Portal logout must only revoke vendor-portal tokens.

`POST /portal/auth/logout` reads the raw `Authorization` header (no dependency)
and blocks the token's jti in the shared Redis blocklist. Without a `typ` guard
it accepted ANY JWT signed with `FEOH_SECRET_KEY` — including an employee
`typ=user` token — so the public portal-logout route could revoke an employee
session. These tests pin the symmetric `typ == "vendor"` guard.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api.deps import create_access_token, create_vendor_access_token, decode_token
from app.api.portal_auth import portal_logout


@pytest.mark.asyncio
async def test_logout_rejects_employee_token_without_blocking_it():
    """An employee `typ=user` JWT must be refused (401) and its jti must NEVER
    reach the blocklist — otherwise a vendor could revoke an employee session
    from the public portal-logout endpoint."""
    employee_token = create_access_token(uuid.uuid4(), uuid.uuid4())

    with patch("app.api.portal_auth.block_token", AsyncMock()) as block:
        with pytest.raises(HTTPException) as exc:
            await portal_logout(authorization=f"Bearer {employee_token}")
    assert exc.value.status_code == 401
    block.assert_not_awaited()


@pytest.mark.asyncio
async def test_logout_accepts_vendor_token_and_blocks_jti():
    """A genuine vendor-portal token is revoked — its jti is blocklisted AND
    dropped from the vendor's tracked-session set.

    (The patch target moved from `block_token` to `end_session` when portal
    sessions became tracked: `end_session` blocklists *and* untracks, so a
    signed-out session can't linger in the set that "sign out my other devices"
    reads. The guarantee under test — the jti stops authenticating — is
    unchanged.)
    """
    vu_id, vendor_id = uuid.uuid4(), uuid.uuid4()
    vendor_token = create_vendor_access_token(vu_id, vendor_id)
    jti = decode_token(vendor_token)["jti"]

    with patch("app.api.portal_auth.end_session", AsyncMock()) as ended:
        await portal_logout(authorization=f"Bearer {vendor_token}")
    ended.assert_awaited_once()
    assert ended.await_args.args[0] == vu_id
    assert ended.await_args.args[1] == jti


# ---------------------------------------------------------------------------
# Portal sessions are tracked, so a supplier password change can end them
#
# The employee surface has tracked every sign-in in Redis since session
# management landed; the portal minted a bare JWT and tracked nothing. That
# made "sign the supplier out of their other devices" impossible to implement
# at all — which is why a portal password change (usually made because the
# supplier believes the old password leaked) left every other session of theirs
# authenticating on the old token until it expired.
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from tests.test_session_management import FakeRedis  # noqa: E402


@pytest.fixture
def portal_redis(monkeypatch):
    """Rich (zset + hash) Redis stand-in — the autouse conftest fake is
    key/value only and can't back the session structures."""
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


def _fake_request():
    return SimpleNamespace(headers={"user-agent": "Chrome on macOS"}, client=None)


@pytest.mark.asyncio
async def test_mint_portal_session_tracks_the_new_jti(portal_redis):
    from app.api.portal_auth import _mint_portal_session
    from app.redis import get_active_sessions

    vu = SimpleNamespace(id=uuid.uuid4(), vendor_id=uuid.uuid4(), must_change_password=False)
    resp = await _mint_portal_session(vu, _fake_request(), method="password")

    jti = decode_token(resp.access_token)["jti"]
    assert await get_active_sessions(vu.id) == [jti]


def _vendor_user(**overrides):
    from app.utils.passwords import pwd_context

    base = dict(
        id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="supplier@vendor.test",
        full_name="Supplier Contact",
        hashed_password=pwd_context.hash("OldPassword123"),
        must_change_password=True,
        mfa_enabled=False,
        locale=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _vendor_db():
    """AsyncMock tenant session whose one SELECT yields the Vendor row the
    response model needs."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(
        return_value=SimpleNamespace(id=uuid.uuid4(), name="Acme Supplies", status="active")
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_portal_password_change_revokes_other_sessions_but_not_the_callers(
    portal_redis, monkeypatch
):
    from app.api.portal_auth import portal_change_password
    from app.schemas.portal import PortalChangePasswordRequest

    audits: list[dict] = []

    async def _capture(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr("app.api.portal_auth.dispatch_auth_audit", _capture)

    vu = _vendor_user()
    # Three live sessions: the browser making the change plus two others.
    mine = create_vendor_access_token(vu.id, vu.vendor_id)
    mine_jti = decode_token(mine)["jti"]
    others = [str(uuid.uuid4()), str(uuid.uuid4())]
    from app.services.session_management import register_session

    await register_session(vu.id, mine_jti, method="password")
    for jti in others:
        await register_session(vu.id, jti, method="password")

    resp = await portal_change_password(
        body=PortalChangePasswordRequest(
            current_password="OldPassword123", new_password="BrandNewPass123"
        ),
        vu=vu,
        db=_vendor_db(),
        authorization=f"Bearer {mine}",
    )

    assert resp.must_change_password is False
    for jti in others:
        assert f"token:blocked:{jti}" in portal_redis.strings
    assert f"token:blocked:{mine_jti}" not in portal_redis.strings
    # The change's own row first, then the forced-logout side effect.
    assert [a["action"] for a in audits] == ["portal.password.changed", "portal.session.revoked"]
    assert audits[0]["details"] == {"via": "change_password", "forced": True}
    assert audits[0]["actor_id"] == vu.id and audits[0]["entity_id"] == vu.id
    assert "BrandNewPass123" not in repr(audits) and "OldPassword123" not in repr(audits)
    assert vu.email not in repr(audits), "a supplier contact's address is third-party PII"
    assert audits[1]["details"] == {
        "scope": "others",
        "revoked": 2,
        "reason": "password_changed",
    }


@pytest.mark.asyncio
async def test_portal_voluntary_password_change_is_audited_not_forced(portal_redis, monkeypatch):
    from app.api.portal_auth import portal_change_password
    from app.schemas.portal import PortalChangePasswordRequest

    audits: list[dict] = []

    async def _capture(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr("app.api.portal_auth.dispatch_auth_audit", _capture)

    vu = _vendor_user(must_change_password=False)
    await portal_change_password(
        body=PortalChangePasswordRequest(
            current_password="OldPassword123", new_password="BrandNewPass123"
        ),
        vu=vu,
        db=_vendor_db(),
        authorization=None,
    )
    changed = [a for a in audits if a["action"] == "portal.password.changed"]
    assert [a["details"] for a in changed] == [{"via": "change_password", "forced": False}]


@pytest.mark.asyncio
async def test_portal_password_change_rejects_a_wrong_current_password(portal_redis, monkeypatch):
    """Negative control — a failed change must revoke nothing and audit no change."""
    from app.api.portal_auth import portal_change_password
    from app.schemas.portal import PortalChangePasswordRequest
    from app.services.session_management import register_session

    audits: list[dict] = []

    async def _capture(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr("app.api.portal_auth.dispatch_auth_audit", _capture)

    vu = _vendor_user()
    other = str(uuid.uuid4())
    await register_session(vu.id, other, method="password")

    with pytest.raises(HTTPException) as exc:
        await portal_change_password(
            body=PortalChangePasswordRequest(
                current_password="WrongPassword1", new_password="BrandNewPass123"
            ),
            vu=vu,
            db=_vendor_db(),
            authorization=None,
        )
    assert exc.value.status_code == 400
    assert f"token:blocked:{other}" not in portal_redis.strings
    assert audits == []
