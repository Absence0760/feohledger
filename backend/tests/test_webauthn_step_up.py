"""WebAuthn-assertion step-up — the third proof for a factor change.

`test_webauthn.py` covers the ceremony mechanics and `test_passkey_endpoints.py`
the login/register endpoint contract. This file pins the *step-up* behaviour:

  - `POST /auth/mfa/step-up/passkey` mints an assertion challenge into its own
    Redis slot, keyed by the operation it will authorize
  - a passwordless SSO-only account whose sole factor is a passkey can once
    again manage its factors (register, delete, TOTP enroll, TOTP disable) —
    the carve-out that made it impossible is closed by this assertion path
  - **purpose binding**: a step-up assertion cannot be replayed as a login and
    a login assertion cannot satisfy a step-up
  - **operation binding**: an assertion collected to authorize one
    factor-management operation cannot authorize a different one
  - the challenge is single-use, and a wrong / unknown credential still fails

Assertions are produced by the in-test software authenticator from
`test_webauthn.py`, so the real py_webauthn verification runs throughout — no
mocking of the signature check.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from tests.test_webauthn import (
    RP_ID,
    SoftAuthenticator,
    _challenge_from_options,
    platform_rp,
)

ORIGIN = "http://localhost:7777"


def _sso_only_user():
    """An SSO-only account: no password, no TOTP secret. Before the assertion
    path existed this account could not satisfy a step-up at all, so it was
    locked out of its own factor management."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="sso@acme.test",
        full_name="SSO User",
        organization_id=uuid.uuid4(),
        is_active=True,
        hashed_password=None,
        mfa_secret=None,
        mfa_enabled=False,
        mfa_enrolled_at=None,
        must_change_password=False,
        roles=[],
        locale=None,
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
def fake_redis(monkeypatch):
    from app.services import webauthn

    monkeypatch.setattr(webauthn.settings, "webauthn_rp_id", RP_ID)
    monkeypatch.setattr(webauthn.settings, "webauthn_origins", ORIGIN)
    monkeypatch.setattr(webauthn.settings, "webauthn_challenge_ttl_seconds", 300)
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webauthn.get_redis", _get_redis)
    return fake


class _CredDB:
    """Control-plane stand-in: serves the account's passkeys to `_user_passkeys`
    and the single matching row to the assertion lookup.

    A password step-up also loads the account's organization (a password is no
    proof in an SSO-only tenant — `api/auth._password_sign_in_closed`), so an
    `organizations` select is answered with `org`: by default one that has not
    closed password sign-in."""

    def __init__(self, creds=None, org=None):
        self.creds = list(creds or [])
        self.added = []
        self.commits = 0
        self.org = org if org is not None else SimpleNamespace(settings={})

    async def execute(self, stmt, *_a, **_k):
        result = MagicMock()
        if stmt.get_final_froms()[0].name == "organizations":
            result.scalar_one_or_none.return_value = self.org
            return result
        result.scalars.return_value.all.return_value = self.creds
        result.scalar_one_or_none.return_value = self.creds[0] if self.creds else None
        return result

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        obj.created_at = None
        obj.last_used_at = None

    async def delete(self, obj):
        self.creds = [c for c in self.creds if c is not obj]


class _LoginVerifyDB:
    """First `execute()` resolves the User, the second the credential — the
    shape `passkey_authenticate_finish` expects."""

    def __init__(self, user, cred):
        self._user = user
        self._cred = cred
        self._calls = 0

    async def execute(self, *_a, **_k):
        self._calls += 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._user if self._calls == 1 else self._cred
        return result

    async def commit(self):
        pass


async def _register_passkey(user) -> tuple[SoftAuthenticator, SimpleNamespace]:
    """Run the real registration ceremony and return the authenticator plus a
    stand-in for the persisted `WebAuthnCredential` row."""
    from app.services import webauthn

    options_json = await webauthn.begin_registration(
        user_id=user.id,
        user_name=user.email,
        user_display_name=user.full_name,
        existing_credential_ids=[],
        rp=platform_rp(),
    )
    soft = SoftAuthenticator()
    fields = await webauthn.finish_registration(
        user_id=user.id,
        credential_json=soft.create(_challenge_from_options(options_json)),
        rp=platform_rp(),
    )
    cred = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        credential_id=fields["credential_id"],
        public_key=fields["public_key"],
        sign_count=fields["sign_count"],
        transports=fields["transports"],
        rp_id=fields["rp_id"],
        name="Laptop",
        created_at=None,
        last_used_at=None,
    )
    return soft, cred


async def _step_up_assertion(user, db, soft, operation: str) -> dict:
    """Full step-up ceremony: ask for a challenge bound to `operation`, sign it."""
    from app.api import auth as auth_mod
    from app.schemas.auth import WebAuthnStepUpStartRequest

    start = await auth_mod.passkey_step_up_start(
        body=WebAuthnStepUpStartRequest(operation=operation), user=user, db=db
    )
    return json.loads(soft.get(_challenge_from_options(json.dumps(start.options))))


# ---------------------------------------------------------------------------
# Challenge minting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_up_start_refused_when_master_switch_off():
    from app.api import auth as auth_mod
    from app.schemas.auth import WebAuthnStepUpStartRequest

    with patch("app.api.auth.settings.mfa_enabled", False):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_step_up_start(
                body=WebAuthnStepUpStartRequest(operation="passkey_register"),
                user=_sso_only_user(),
                db=_CredDB(),
            )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_step_up_start_refused_without_a_registered_passkey():
    """Nothing to challenge — opaque 400, no challenge minted."""
    from app.api import auth as auth_mod
    from app.schemas.auth import WebAuthnStepUpStartRequest

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_step_up_start(
                body=WebAuthnStepUpStartRequest(operation="passkey_register"),
                user=_sso_only_user(),
                db=_CredDB(),
            )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_step_up_challenge_lands_in_its_own_operation_scoped_slot(fake_redis):
    """The Redis namespace is the binding mechanism — assert it directly."""
    from app.api import auth as auth_mod
    from app.schemas.auth import WebAuthnStepUpStartRequest

    user = _sso_only_user()
    _, cred = await _register_passkey(user)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
    ):
        await auth_mod.passkey_step_up_start(
            body=WebAuthnStepUpStartRequest(operation="passkey_delete"),
            user=user,
            db=_CredDB([cred]),
        )

    assert f"webauthn:stepup_challenge:passkey_delete:{user.id}" in fake_redis.store
    # Emphatically NOT in the login slot.
    assert f"webauthn:auth_challenge:{user.id}" not in fake_redis.store


@pytest.mark.asyncio
async def test_step_up_start_rejects_an_unknown_operation():
    """The operation set is closed at the schema — an unbounded operation would
    be an unbounded challenge namespace."""
    from pydantic import ValidationError

    from app.schemas.auth import WebAuthnStepUpStartRequest

    with pytest.raises(ValidationError):
        WebAuthnStepUpStartRequest(operation="delete_all_the_things")


# ---------------------------------------------------------------------------
# The restored capability — a passwordless passkey-only account manages factors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sso_only_account_registers_a_second_passkey_with_an_assertion():
    from app.api import auth as auth_mod
    from app.schemas.auth import MFAStepUpRequest

    user = _sso_only_user()
    soft, cred = await _register_passkey(user)
    db = _CredDB([cred])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
    ):
        assertion = await _step_up_assertion(user, db, soft, "passkey_register")
        start = await auth_mod.passkey_register_start(
            body=MFAStepUpRequest(assertion=assertion), user=user, db=db
        )

    assert start.options["challenge"]
    # Clone detection depends on the counter actually advancing in the DB.
    assert cred.sign_count == 1
    assert cred.last_used_at is not None
    assert db.commits >= 1


@pytest.mark.asyncio
async def test_sso_only_account_is_still_refused_without_any_proof():
    """Regression guard on the gate itself: the assertion is the way through,
    not a bypass — an empty body is still a 400."""
    from app.api import auth as auth_mod

    user = _sso_only_user()
    _, cred = await _register_passkey(user)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
        patch("app.api.auth.dispatch_auth_audit", new=AsyncMock()) as audit,
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_register_start(user=user, db=_CredDB([cred]))
    assert exc.value.status_code == 400
    audit.assert_awaited()


@pytest.mark.asyncio
async def test_sso_only_account_starts_totp_enrollment_with_an_assertion():
    from app.api import auth as auth_mod
    from app.schemas.auth import MFAStepUpRequest

    user = _sso_only_user()
    soft, cred = await _register_passkey(user)
    db = _CredDB([cred])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
        patch("app.services.mfa.stash_pending_totp_secret", new=AsyncMock()) as stash,
    ):
        assertion = await _step_up_assertion(user, db, soft, "totp_enroll")
        out = await auth_mod.enroll_mfa_start(
            body=MFAStepUpRequest(assertion=assertion), user=user, db=db
        )

    assert out.secret
    stash.assert_awaited()


@pytest.mark.asyncio
async def test_passwordless_account_disables_totp_with_an_assertion():
    """`/mfa/disable` used to demand a password, which an SSO-only account does
    not have — so a passkey-holding SSO account could never turn TOTP off."""
    from app.api import auth as auth_mod
    from app.schemas.auth import MFADisableRequest

    user = _sso_only_user()
    user.mfa_enabled = True
    user.mfa_secret = "JBSWY3DPEHPK3PXP"
    soft, cred = await _register_passkey(user)
    db = _CredDB([cred])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
        patch("app.api.auth._load_user_org", new=AsyncMock(return_value=None)),
        patch("app.services.mfa.clear_pending_totp_secret", new=AsyncMock()),
    ):
        assertion = await _step_up_assertion(user, db, soft, "totp_disable")
        me = await auth_mod.disable_mfa(
            body=MFADisableRequest(assertion=assertion), user=user, db=db
        )

    assert me.mfa_enabled is False
    assert user.mfa_secret is None


# ---------------------------------------------------------------------------
# Purpose binding — the security property this feature turns on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_up_assertion_cannot_be_replayed_as_a_login():
    """An assertion collected to authorize a factor change must not mint an
    access token. `clientDataJSON.type` is `webauthn.get` for both ceremonies,
    so the challenge namespace is the only thing separating them."""
    from app.api import auth as auth_mod
    from app.schemas.auth import WebAuthnAuthFinishRequest
    from app.services import mfa

    user = _sso_only_user()
    soft, cred = await _register_passkey(user)
    db = _CredDB([cred])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
        patch("app.api.auth.dispatch_auth_audit", new=AsyncMock()),
    ):
        assertion = await _step_up_assertion(user, db, soft, "passkey_register")

        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_authenticate_finish(
                body=WebAuthnAuthFinishRequest(
                    challenge_token=mfa.create_challenge_token(user.id), credential=assertion
                ),
                request=SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={}),
                db=_LoginVerifyDB(user=user, cred=cred),
            )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_assertion_cannot_satisfy_a_step_up():
    """The converse: a passkey LOGIN assertion — which an attacker could
    observe from a legitimate sign-in — must not authorize a factor change."""
    from app.api import auth as auth_mod
    from app.schemas.auth import MFAStepUpRequest, WebAuthnAuthStartRequest
    from app.services import mfa

    user = _sso_only_user()
    soft, cred = await _register_passkey(user)
    db = _CredDB([cred])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
        patch("app.api.auth.dispatch_auth_audit", new=AsyncMock()),
    ):
        login_start = await auth_mod.passkey_authenticate_start(
            body=WebAuthnAuthStartRequest(challenge_token=mfa.create_challenge_token(user.id)),
            request=SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={}),
            db=db,
        )
        login_assertion = json.loads(
            soft.get(_challenge_from_options(json.dumps(login_start.options)))
        )

        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_register_start(
                body=MFAStepUpRequest(assertion=login_assertion), user=user, db=db
            )
        assert exc.value.status_code == 400

        # Positive control, so the rejection above can't be passed off as "this
        # authenticator / account just never satisfies a step-up": the SAME
        # authenticator, given a challenge minted for THIS operation, does.
        step_up_assertion = await _step_up_assertion(user, db, soft, "passkey_register")
        start = await auth_mod.passkey_register_start(
            body=MFAStepUpRequest(assertion=step_up_assertion), user=user, db=db
        )
    assert start.options["challenge"]


@pytest.mark.asyncio
async def test_step_up_assertion_is_bound_to_its_operation():
    """An assertion collected for `passkey_register` must not authorize
    `passkey_delete` — otherwise one consented prompt would silently grant
    every factor-management action."""
    from app.api import auth as auth_mod

    user = _sso_only_user()
    soft, cred = await _register_passkey(user)
    db = _CredDB([cred])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
    ):
        assertion = await _step_up_assertion(user, db, soft, "passkey_register")

        satisfied = await auth_mod._step_up_satisfied(
            db,
            user,
            SimpleNamespace(password=None, code=None, assertion=assertion),
            operation="passkey_delete",
            rp=platform_rp(),
        )
    assert satisfied is False


@pytest.mark.asyncio
async def test_step_up_assertion_is_single_use():
    """Replaying the same assertion against the same operation fails — the
    challenge is consumed on first verify."""
    from app.api import auth as auth_mod

    user = _sso_only_user()
    soft, cred = await _register_passkey(user)
    db = _CredDB([cred])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
    ):
        assertion = await _step_up_assertion(user, db, soft, "passkey_register")
        body = SimpleNamespace(password=None, code=None, assertion=assertion)

        assert await auth_mod._step_up_satisfied(
            db, user, body, operation="passkey_register", rp=platform_rp()
        )
        assert not await auth_mod._step_up_satisfied(
            db, user, body, operation="passkey_register", rp=platform_rp()
        )


@pytest.mark.asyncio
async def test_step_up_assertion_from_an_unregistered_credential_is_refused():
    """A signature from an authenticator this account never registered is not a
    proof of anything."""
    from app.api import auth as auth_mod

    user = _sso_only_user()
    soft, cred = await _register_passkey(user)
    db = _CredDB([cred])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
    ):
        assertion = await _step_up_assertion(user, db, soft, "passkey_register")
        # Same signed assertion, but the account has no credential on file.
        satisfied = await auth_mod._step_up_satisfied(
            _CredDB([]),
            user,
            SimpleNamespace(password=None, code=None, assertion=assertion),
            operation="passkey_register",
            rp=platform_rp(),
        )
    assert satisfied is False


@pytest.mark.asyncio
async def test_password_step_up_still_works_alongside_the_assertion_path():
    """Positive control that the new proof is additive, not a replacement."""
    from app.api import auth as auth_mod
    from app.schemas.auth import MFAStepUpRequest

    user = _sso_only_user()
    user.hashed_password = "hashed"
    _, cred = await _register_passkey(user)
    db = _CredDB([cred])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
        patch("app.utils.passwords.pwd_context.verify", return_value=True),
    ):
        start = await auth_mod.passkey_register_start(
            body=MFAStepUpRequest(password="correct horse"), user=user, db=db
        )
    assert start.options["challenge"]
