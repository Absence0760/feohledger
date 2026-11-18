"""Real-DB coverage for admin "Invite User" (``POST /api/admin/users``).

The invite used to generate a temporary password, email it and return it on
the response — a working credential in an inbox and on the admin's screen
until somebody signed in. It now provisions the user with NO password and
emails a single-use, expiring set-password link (``api/set_password_links``,
``docs/decisions.md`` §283), redeemed by the same ``POST
/api/auth/reset-password`` as forgot-password. The link is never on the
response: whoever holds it chooses the password.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import select

from tests.set_password_mail import capture_outbox, link_mode, link_token


async def _user_by_email(realdb, email):
    from app.models.user import User

    async with realdb.control_sessionmaker()() as s:
        return (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_invite_emails_a_set_password_link_and_returns_no_credential(
    realdb, monkeypatch, caplog
):
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "tenant_url_template", "https://{slug}.feohledger.test", raising=True)
    sent = capture_outbox(monkeypatch)
    caplog.set_level(logging.DEBUG)

    slug = realdb.info("a").slug
    email = f"new-hire-{uuid.uuid4().hex[:8]}@acme.test"

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/admin/users",
            json={"email": email, "full_name": "New Hire", "role_names": ["ap_clerk"]},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        try:
            assert "temporary_password" not in body
            assert body["set_password_link_expires_at"]

            assert len(sent) == 1
            msg = sent[0]
            assert msg.to == email
            token = link_token(msg)
            assert f"https://{slug}.feohledger.test/login/reset-password?token=" in msg.body_text
            assert link_mode(msg) == "set"
            assert "Password:" not in msg.body_text
            # The link is never handed back to the admin, nor logged.
            assert token not in resp.text
            assert token not in caplog.text

            stored = await _user_by_email(realdb, email)
            assert stored.hashed_password is None
            assert stored.must_change_password is False
        finally:
            await c.delete(f"/api/admin/users/{body['id']}")


@pytest.mark.asyncio
async def test_invite_link_sets_the_password_once_and_the_user_signs_in(realdb, monkeypatch):
    from tests.test_session_management import FakeRedis

    # A successful login registers a session, which needs the zset+hash
    # stand-in rather than the key/value-only autouse stub.
    session_redis = FakeRedis()

    async def _get_redis():
        return session_redis

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    sent = capture_outbox(monkeypatch)
    email = f"new-hire-{uuid.uuid4().hex[:8]}@acme.test"

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/admin/users",
            json={"email": email, "full_name": "New Hire", "role_names": ["ap_clerk"]},
        )
        assert resp.status_code == 201, resp.text
        new_id = resp.json()["id"]
    token = link_token(sent[0])

    try:
        async with realdb.client(key="a", role=None) as anon:
            # No password yet: sign-in is refused until the link is used.
            refused = await anon.post(
                "/api/auth/login", json={"email": email, "password": "Anything-Goes1"}
            )
            assert refused.status_code == 401

            check = await anon.post("/api/auth/reset-password/check", json={"token": token})
            assert check.status_code == 200, check.text
            assert check.json()["email"] == email

            done = await anon.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "OwnerChosen-Pass9"},
            )
            assert done.status_code == 200, done.text

            replay = await anon.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "Another-Chosen9"},
            )
            assert replay.status_code == 400

            login = await anon.post(
                "/api/auth/login", json={"email": email, "password": "OwnerChosen-Pass9"}
            )
            assert login.status_code == 200, login.text
            assert login.json()["must_change_password"] is False
    finally:
        async with realdb.client(key="a", role="admin") as c:
            await c.delete(f"/api/admin/users/{new_id}")


@pytest.mark.asyncio
async def test_invite_email_failure_creates_no_user(realdb, monkeypatch):
    """The email is the link's only channel, so a failed send is a 502 and the
    account is rolled back — never a user nobody can sign in as."""
    capture_outbox(monkeypatch, fail=True)
    email = f"new-hire-{uuid.uuid4().hex[:8]}@acme.test"

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/admin/users",
            json={"email": email, "full_name": "New Hire", "role_names": []},
        )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["code"] == "set_password_email_failed"
    assert await _user_by_email(realdb, email) is None


@pytest.mark.asyncio
async def test_invite_without_a_tenant_url_is_refused_before_creating(realdb, monkeypatch):
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "tenant_url_template", "", raising=True)
    sent = capture_outbox(monkeypatch)
    email = f"new-hire-{uuid.uuid4().hex[:8]}@acme.test"

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/admin/users",
            json={"email": email, "full_name": "New Hire", "role_names": []},
        )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "set_password_link_unavailable"
    assert sent == []
    assert await _user_by_email(realdb, email) is None
