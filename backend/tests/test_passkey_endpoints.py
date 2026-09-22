"""Passkey (WebAuthn) endpoint security + full register/authenticate flow.

`test_webauthn.py` covers the service mechanics against a software
authenticator; this file pins the *endpoint* contract, calling the route
handlers in `app/api/auth.py` directly with fake DBs (the same style as
`test_mfa_enrollment_security.py`):

  - Master switch off ⇒ register / register-verify / authenticate-start all 400
  - The login challenge offers `passkey` only when the user has a credential
  - A full register → authenticate round-trip through the real endpoints mints
    an access token (the assertion is produced by the in-test software
    authenticator from `test_webauthn.py`)
  - A wrong/unknown credential id at authenticate-verify ⇒ 401 (opaque)
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from tests.test_webauthn import RP_ID, SoftAuthenticator, _challenge_from_options


def _fake_user(*, mfa_enabled: bool = False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="user@acme.test",
        full_name="Test User",
        organization_id=uuid.uuid4(),
        is_active=True,
        hashed_password="x",
        mfa_secret=None,
        mfa_enabled=mfa_enabled,
        mfa_enrolled_at=None,
        must_change_password=False,
        roles=[],
    )


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    async def setex(self, key, ttl, value):  # noqa: ARG002
        self.store[key] = value if isinstance(value, bytes) else value.encode()

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture(autouse=True)
def _pin_settings_and_redis(monkeypatch):
    from app.services import webauthn

    monkeypatch.setattr(webauthn.settings, "webauthn_rp_id", RP_ID)
    monkeypatch.setattr(webauthn.settings, "webauthn_origins", "http://localhost:7777")
    monkeypatch.setattr(webauthn.settings, "webauthn_challenge_ttl_seconds", 300)
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webauthn.get_redis", _get_redis)
    return fake


# ---------------------------------------------------------------------------
# Master-switch gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passkey_register_refused_when_master_switch_off():
    from app.api.auth import passkey_register_start

    db = AsyncMock()
    with patch("app.api.auth.settings.mfa_enabled", False):
        with pytest.raises(HTTPException) as exc:
            await passkey_register_start(user=_fake_user(), db=db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_passkey_authenticate_start_refused_when_master_switch_off():
    from app.api.auth import passkey_authenticate_start
    from app.schemas.auth import WebAuthnAuthStartRequest

    db = AsyncMock()
    req = SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={})
    with (
        patch("app.api.auth.settings.mfa_enabled", False),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc:
            await passkey_authenticate_start(
                body=WebAuthnAuthStartRequest(challenge_token="t"), request=req, db=db
            )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Full register → authenticate round-trip through the endpoints
# ---------------------------------------------------------------------------


class _RegDB:
    """Records the WebAuthnCredential added on register-verify.

    A password step-up also loads the account's organization (a password is no
    proof in an SSO-only tenant — `api/auth._password_sign_in_closed`), so an
    `organizations` select is answered with `org`: by default one that has not
    closed password sign-in. Dispatched on the table, since the credential and
    the org lookups both read `scalar_one_or_none`."""

    def __init__(self, existing=None, org=None):
        self.added = []
        self._existing = existing or []
        self._org = org if org is not None else SimpleNamespace(settings={})

    async def execute(self, stmt, *_a, **_k):
        result = MagicMock()
        if stmt.get_final_froms()[0].name == "organizations":
            result.scalar_one_or_none.return_value = self._org
            return result
        result.scalars.return_value.all.return_value = self._existing
        result.scalar_one_or_none.return_value = self._existing[0] if self._existing else None
        return result

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        # Stamp the server-default-ish fields the response reads.
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        obj.created_at = None
        obj.last_used_at = None


@pytest.mark.asyncio
async def test_full_passkey_register_then_authenticate(_pin_settings_and_redis):
    from app.api import auth as auth_mod
    from app.schemas.auth import (
        WebAuthnAuthFinishRequest,
        WebAuthnAuthStartRequest,
        WebAuthnRegisterFinishRequest,
    )
    from app.services import mfa

    user = _fake_user()
    db = _RegDB()
    soft = SoftAuthenticator()

    audit = AsyncMock()
    rate = AsyncMock()
    session_reg = AsyncMock()

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.dispatch_auth_audit", new=audit),
        patch("app.api.auth.check_rate_limit", new=rate),
        patch("app.api.auth.register_session", new=session_reg),
        patch("app.api.auth.create_access_token_with_jti", return_value=("TOKEN", "JTI")),
    ):
        # 1. register start → options + challenge stashed
        start = await auth_mod.passkey_register_start(user=user, db=db)
        reg_response = soft.create(start.options["challenge"])

        # 2. register verify → persists the credential
        cred_meta = await auth_mod.passkey_register_finish(
            body=WebAuthnRegisterFinishRequest(credential=json.loads(reg_response), name="My Key"),
            user=user,
            db=db,
        )
        assert cred_meta.name == "My Key"
        assert len(db.added) == 1
        stored = db.added[0]

        # The user now "has" this passkey — wire a DB that returns it.
        auth_db = _RegDB(existing=[stored])
        # decode_challenge_token needs a real token for this user
        challenge_token = mfa.create_challenge_token(user.id)

        # 3. authenticate start → options for the stored credential
        auth_start = await auth_mod.passkey_authenticate_start(
            body=WebAuthnAuthStartRequest(challenge_token=challenge_token),
            request=SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={}),
            db=auth_db,
        )
        assertion = soft.get(_challenge_from_options(json.dumps(auth_start.options)))

        # The authenticate-verify path looks the user up by id, then the cred.
        verify_db = _AuthVerifyDB(user=user, cred=stored)
        token = await auth_mod.passkey_authenticate_finish(
            body=WebAuthnAuthFinishRequest(
                challenge_token=challenge_token, credential=json.loads(assertion)
            ),
            request=SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={}),
            db=verify_db,
        )

    assert token.access_token == "TOKEN"
    # Counter advanced + last_used stamped on the credential
    assert stored.sign_count == 1
    assert stored.last_used_at is not None
    session_reg.assert_awaited()


class _AuthVerifyDB:
    """First execute() resolves the User; second resolves the credential."""

    def __init__(self, user, cred):
        self._user = user
        self._cred = cred
        self._calls = 0

    async def execute(self, *_a, **_k):
        self._calls += 1
        result = MagicMock()
        if self._calls == 1:
            result.scalar_one_or_none.return_value = self._user
        else:
            result.scalar_one_or_none.return_value = self._cred
        return result

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_authenticate_verify_unknown_credential_is_401(_pin_settings_and_redis):
    from app.api import auth as auth_mod
    from app.schemas.auth import WebAuthnAuthFinishRequest
    from app.services import mfa

    user = _fake_user()
    challenge_token = mfa.create_challenge_token(user.id)
    # DB returns the user but NO matching credential.
    db = _AuthVerifyDB(user=user, cred=None)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.dispatch_auth_audit", new=AsyncMock()),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_authenticate_finish(
                body=WebAuthnAuthFinishRequest(
                    challenge_token=challenge_token,
                    credential={"id": "deadbeef", "rawId": "deadbeef", "response": {}},
                ),
                request=SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={}),
                db=db,
            )
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Step-up on register-start — adding a factor to an account that already has
# one must re-prove control of the account (issue #159). Without it, a stolen
# access token is enough to bind an attacker-controlled authenticator to the
# victim's account.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passkey_register_start_on_a_bare_account_needs_no_step_up():
    """First factor on an account with none — nothing to protect yet, so
    enrollment stays frictionless."""
    from app.api import auth as auth_mod

    user = _fake_user()
    db = _RegDB(existing=[])

    with patch("app.api.auth.settings.mfa_enabled", True):
        start = await auth_mod.passkey_register_start(user=user, db=db)

    assert start.options["challenge"]


@pytest.mark.asyncio
async def test_passkey_register_start_refused_without_step_up_when_totp_is_live():
    """A TOTP-protected account gains a passkey only with a step-up."""
    from app.api import auth as auth_mod

    user = _fake_user(mfa_enabled=True)
    user.mfa_secret = "JBSWY3DPEHPK3PXP"
    db = _RegDB(existing=[])

    with patch("app.api.auth.settings.mfa_enabled", True):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_register_start(user=user, db=db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_passkey_register_start_refused_without_step_up_when_a_passkey_exists():
    """Second passkey on an account whose only factor IS a passkey."""
    from app.api import auth as auth_mod

    user = _fake_user()
    db = _RegDB(existing=[SimpleNamespace(credential_id="abc", rp_id=None)])

    with patch("app.api.auth.settings.mfa_enabled", True):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_register_start(user=user, db=db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_passkey_register_start_allowed_with_a_correct_password_step_up():
    """Positive control — the step-up is a gate, not a wall."""
    from app.api import auth as auth_mod
    from app.schemas.auth import MFAStepUpRequest

    user = _fake_user()
    db = _RegDB(existing=[SimpleNamespace(credential_id="abc", rp_id=None)])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.utils.passwords.pwd_context.verify", return_value=True),
    ):
        start = await auth_mod.passkey_register_start(
            body=MFAStepUpRequest(password="correct"), user=user, db=db
        )
    assert start.options["challenge"]


@pytest.mark.asyncio
async def test_passkey_register_start_refused_for_a_passwordless_sso_account_with_a_passkey():
    """An SSO-only account whose sole factor is a passkey has neither a
    password nor a TOTP secret to challenge — and is refused, NOT exempted.

    Exempting it (an earlier draft did) is a latent auth bypass: a stolen JWT
    could plant an attacker-controlled passkey on an account the attacker
    never proved control of, and it goes live the moment such a user is given
    a password via the admin password-set. The recovery path is exactly that
    admin password-set, after which the normal step-up works — in a tenant that
    has not closed password sign-in. In an `sso_only` tenant a password is no
    step-up proof at all (`api/auth._password_sign_in_closed`), and the route is
    this passkey's own assertion instead."""
    from app.api import auth as auth_mod

    user = _fake_user()
    user.hashed_password = None
    db = _RegDB(existing=[SimpleNamespace(credential_id="abc", rp_id=None)])

    with patch("app.api.auth.settings.mfa_enabled", True):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_register_start(user=user, db=db)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Step-up on DELETE — removing a factor is as sensitive as adding one. Before
# this gate, `DELETE /api/auth/mfa/passkey/{id}` stripped the account's sole
# passkey on a bare bearer token, with no password and no current code.
# ---------------------------------------------------------------------------


class _DeleteDB:
    """Control session for passkey_delete: the credential lookup, the org
    lookup, and the remaining-passkeys lookup, in the order the handler runs
    them. `deleted` records whether the row was actually removed."""

    def __init__(self, cred, org, remaining=None):
        self._cred = cred
        self._org = org
        self._remaining = remaining if remaining is not None else [cred]
        self.deleted = []
        self._calls = 0

    async def execute(self, *_a, **_k):
        self._calls += 1
        result = MagicMock()
        if self._calls == 1:
            result.scalar_one_or_none.return_value = self._cred
        else:
            result.scalar_one_or_none.return_value = self._org
        result.scalars.return_value.all.return_value = self._remaining
        return result

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        pass


def _fake_cred(name: str = "My Key"):
    return SimpleNamespace(id=uuid.uuid4(), name=name, credential_id=b"abc", user_id=None)


@pytest.mark.asyncio
async def test_passkey_delete_refused_on_a_bare_jwt():
    """THE regression. A stolen access token, no step-up credential — the
    passkey must survive."""
    from app.api import auth as auth_mod

    user = _fake_user()
    cred = _fake_cred()
    cred.user_id = user.id
    org = SimpleNamespace(id=user.organization_id, settings={})
    db = _DeleteDB(cred, org)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.dispatch_auth_audit", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_delete(credential_id=str(cred.id), user=user, db=db)

    assert exc.value.status_code == 400
    assert db.deleted == [], "a session-only caller must not strip the factor"


@pytest.mark.asyncio
async def test_passkey_delete_refused_with_a_wrong_password():
    from app.api import auth as auth_mod
    from app.schemas.auth import MFAStepUpRequest

    user = _fake_user()
    cred = _fake_cred()
    cred.user_id = user.id
    org = SimpleNamespace(id=user.organization_id, settings={})
    db = _DeleteDB(cred, org)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.utils.passwords.pwd_context.verify", return_value=False),
        patch("app.api.auth.dispatch_auth_audit", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_delete(
                credential_id=str(cred.id),
                body=MFAStepUpRequest(password="guess"),
                user=user,
                db=db,
            )

    assert exc.value.status_code == 400
    assert db.deleted == []


@pytest.mark.asyncio
async def test_passkey_delete_allowed_with_a_correct_password():
    """Positive control — the gate is a gate, not a wall."""
    from app.api import auth as auth_mod
    from app.schemas.auth import MFAStepUpRequest

    user = _fake_user()
    cred = _fake_cred()
    cred.user_id = user.id
    org = SimpleNamespace(id=user.organization_id, settings={})
    db = _DeleteDB(cred, org)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.utils.passwords.pwd_context.verify", return_value=True),
        patch("app.api.auth.dispatch_auth_audit", new=AsyncMock()),
    ):
        await auth_mod.passkey_delete(
            credential_id=str(cred.id),
            body=MFAStepUpRequest(password="correct"),
            user=user,
            db=db,
        )

    assert db.deleted == [cred]


@pytest.mark.asyncio
async def test_passkey_delete_unknown_id_is_404_before_the_step_up():
    """An id that isn't this user's stays an opaque 404 — the step-up refusal
    must not become an existence oracle, and a garbage id must not burn the
    account's step-up throttle."""
    from app.api import auth as auth_mod

    user = _fake_user()
    org = SimpleNamespace(id=user.organization_id, settings={})
    db = _DeleteDB(None, org, remaining=[])

    with patch("app.api.auth.settings.mfa_enabled", True):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_delete(credential_id=str(uuid.uuid4()), user=user, db=db)
    assert exc.value.status_code == 404
