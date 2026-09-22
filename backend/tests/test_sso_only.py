"""SSO-only mode — password login is closed when a tenant requires SSO.

`is_sso_only` is deterministic (unit-tested below); the login enforcement is
exercised by calling the `login` handler directly with mocked DB sessions, the
same DB-free pattern as test_auth_error_consistency.py.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.sso import is_sso_only


@pytest.mark.parametrize(
    "settings_dict,expected",
    [
        (None, False),
        ({}, False),
        ({"sso": {}}, False),
        ({"sso": {"sso_only": True}}, False),  # enabled missing => no lockout
        ({"sso": {"enabled": True}}, False),  # sso_only not set
        ({"sso": {"enabled": False, "sso_only": True}}, False),  # SSO off
        ({"sso": {"enabled": True, "sso_only": True}}, True),
    ],
)
def test_is_sso_only(settings_dict, expected):
    assert is_sso_only(settings_dict) is expected


def _fake_request(ip: str = "203.0.113.1"):
    req = MagicMock()
    req.client = SimpleNamespace(host=ip)
    req.headers = {}
    return req


def _db_user_then_org(user, org):
    """Login does two queries: the user lookup, then the org load. Model both."""
    user_result = MagicMock()
    user_result.scalar_one_or_none = MagicMock(return_value=user)
    org_result = MagicMock()
    org_result.scalar_one_or_none = MagicMock(return_value=org)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[user_result, org_result, org_result, org_result])
    return db


def _user_with_password(pw: str):
    from app.utils.passwords import pwd_context

    return SimpleNamespace(
        id=uuid.uuid4(),
        email="u@acme.com",
        organization_id=uuid.uuid4(),
        is_active=True,
        mfa_enabled=False,
        hashed_password=pwd_context.hash(pw),
        must_change_password=False,
        full_name="U",
    )


@pytest.mark.asyncio
async def test_login_rejected_when_sso_only():
    """A correct password in an sso_only org is refused with 403 + audit —
    password users can't bypass SSO."""
    from app.api.auth import login
    from app.schemas.auth import LoginRequest

    pw = "Correct-Horse-9"
    user = _user_with_password(pw)
    org = SimpleNamespace(
        id=user.organization_id,
        settings={"sso": {"enabled": True, "sso_only": True, "provider": "saml"}},
    )

    audits: list[dict] = []

    async def _audit(**kwargs):
        audits.append(kwargs)

    with patch("app.api.auth.dispatch_auth_audit", _audit):
        with pytest.raises(HTTPException) as exc:
            await login(
                body=LoginRequest(email="u@acme.com", password=pw),
                request=_fake_request(),
                db=_db_user_then_org(user, org),
            )

    assert exc.value.status_code == 403
    assert "single sign-on" in exc.value.detail.lower()
    assert any(a["details"].get("reason") == "sso_only" for a in audits)


@pytest.mark.asyncio
async def test_login_allowed_when_not_sso_only():
    """The same correct password succeeds when the org doesn't require SSO —
    proving the gate is the flag, not a blanket block."""
    from app.api.auth import login
    from app.schemas.auth import LoginRequest

    pw = "Correct-Horse-9"
    user = _user_with_password(pw)
    org = SimpleNamespace(
        id=user.organization_id,
        settings={"sso": {"enabled": True, "provider": "saml"}},  # sso_only absent
    )

    with (
        patch("app.api.auth.dispatch_auth_audit", AsyncMock()),
        patch("app.api.auth.register_session", AsyncMock()),
    ):
        result = await login(
            body=LoginRequest(email="u@acme.com", password=pw),
            request=_fake_request(),
            db=_db_user_then_org(user, org),
        )

    # Not a 403 — a real token response.
    assert getattr(result, "access_token", None)


# ---------------------------------------------------------------------------
# Step-up — a password that cannot sign in cannot authorize a factor change
#
# `login` refuses a correct password in an sso_only tenant, "even for users who
# still carry a password hash". The step-up gate in front of every factor change
# (TOTP enroll / disable, passkey register / delete) used to accept that same
# hash as proof, so it went on authenticating a security-sensitive operation —
# on raw bcrypt, for a pre-c6a91396 row that sign-in could never upgrade. The
# password is now dropped before it is checked; the code and passkey proofs are
# untouched.
# ---------------------------------------------------------------------------

SSO_ONLY_SETTINGS = {"sso": {"enabled": True, "sso_only": True, "provider": "saml"}}
TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _account_with_totp(pw: str):
    """A member with a password hash AND a live TOTP factor — the account whose
    factor change needs a step-up at all."""
    user = _user_with_password(pw)
    user.mfa_enabled = True
    user.mfa_secret = TOTP_SECRET
    user.mfa_enrolled_at = None
    user.roles = []
    user.locale = None
    return user


def _control_db(org):
    """Serves the two lookups a step-up makes: the account's passkeys (none —
    read through `scalars()`) and its organization (`scalar_one_or_none()`)."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalar_one_or_none.return_value = org
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("offered", ["Correct-Horse-9", "a-wrong-guess"])
async def test_a_password_is_no_step_up_proof_in_an_sso_only_tenant(offered):
    """Right or wrong, the password gets the same refusal — it is never checked,
    so the answer cannot say which it was — and the refusal names the proofs
    that DO work rather than asking for the password again."""
    from app.api.auth import STEP_UP_SSO_ONLY_DETAIL, enroll_mfa_start
    from app.schemas.auth import MFAStepUpRequest
    from app.services import mfa as mfa_service

    user = _account_with_totp("Correct-Horse-9")
    org = SimpleNamespace(id=user.organization_id, settings=SSO_ONLY_SETTINGS)
    verify = AsyncMock(return_value=True)
    audit = AsyncMock()

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.services.mfa.verify_password", verify),
        patch("app.api.auth.dispatch_auth_audit", audit),
    ):
        with pytest.raises(HTTPException) as exc:
            await enroll_mfa_start(
                body=MFAStepUpRequest(password=offered), user=user, db=_control_db(org)
            )

    assert exc.value.status_code == 400
    assert exc.value.detail == STEP_UP_SSO_ONLY_DETAIL
    verify.assert_not_awaited()
    # Still a failed step-up on the trail, PII-free as ever.
    (call,) = audit.await_args_list
    assert call.kwargs["action"] == "auth.mfa.step_up.failure"
    assert call.kwargs["details"] == {"operation": "totp_enroll"}
    # Nothing moved: no candidate secret, the live factor untouched.
    assert await mfa_service.read_pending_totp_secret(user.id) is None
    assert user.mfa_secret == TOTP_SECRET


@pytest.mark.asyncio
async def test_disabling_totp_with_a_password_is_refused_in_an_sso_only_tenant():
    """`/mfa/disable` rides the same gate — the most sensitive factor change
    there is must not be the one a closed password still opens."""
    from app.api.auth import STEP_UP_SSO_ONLY_DETAIL, disable_mfa
    from app.schemas.auth import MFADisableRequest

    pw = "Correct-Horse-9"
    user = _account_with_totp(pw)
    org = SimpleNamespace(id=user.organization_id, settings=SSO_ONLY_SETTINGS)

    with patch("app.api.auth.dispatch_auth_audit", AsyncMock()):
        with pytest.raises(HTTPException) as exc:
            await disable_mfa(body=MFADisableRequest(password=pw), user=user, db=_control_db(org))

    assert exc.value.status_code == 400
    assert exc.value.detail == STEP_UP_SSO_ONLY_DETAIL
    assert user.mfa_enabled is True
    assert user.mfa_secret == TOTP_SECRET


@pytest.mark.asyncio
async def test_an_authenticator_code_still_proves_a_step_up_in_an_sso_only_tenant():
    """Positive control: closing the password closes nothing else. An account
    with a live TOTP factor holds the authenticator, so it is never left
    without a proof it can offer."""
    import pyotp

    from app.api.auth import enroll_mfa_start
    from app.schemas.auth import MFAStepUpRequest

    user = _account_with_totp("Correct-Horse-9")
    org = SimpleNamespace(id=user.organization_id, settings=SSO_ONLY_SETTINGS)

    with patch("app.api.auth.settings.mfa_enabled", True):
        resp = await enroll_mfa_start(
            body=MFAStepUpRequest(code=pyotp.TOTP(TOTP_SECRET).now()),
            user=user,
            db=_control_db(org),
        )

    assert resp.secret != TOTP_SECRET
    assert user.mfa_secret == TOTP_SECRET, "the live factor survives until verify"


@pytest.mark.asyncio
async def test_the_password_still_proves_a_step_up_when_sso_is_not_really_enforced():
    """The step-up reads the SAME predicate as login: `sso_only` without
    `sso.enabled` is not SSO-only (a broken IdP config keeps the password open
    as the escape hatch), so there the password still proves a step-up — the
    two doors cannot disagree about whether the password is an authenticator."""
    from app.api.auth import enroll_mfa_start
    from app.schemas.auth import MFAStepUpRequest

    pw = "Correct-Horse-9"
    user = _account_with_totp(pw)
    org = SimpleNamespace(
        id=user.organization_id, settings={"sso": {"enabled": False, "sso_only": True}}
    )

    with patch("app.api.auth.settings.mfa_enabled", True):
        resp = await enroll_mfa_start(
            body=MFAStepUpRequest(password=pw), user=user, db=_control_db(org)
        )

    assert resp.secret != TOTP_SECRET


@pytest.mark.asyncio
async def test_a_step_up_without_a_password_does_not_load_the_org():
    """The org is consulted only when a password was actually offered, so the
    code and passkey paths pay no extra query for a rule that cannot bind them."""
    import pyotp

    from app.api.auth import enroll_mfa_start
    from app.schemas.auth import MFAStepUpRequest

    user = _account_with_totp("Correct-Horse-9")
    closed = AsyncMock(return_value=True)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth._password_sign_in_closed", closed),
    ):
        await enroll_mfa_start(
            body=MFAStepUpRequest(code=pyotp.TOTP(TOTP_SECRET).now()),
            user=user,
            db=_control_db(None),
        )

    closed.assert_not_awaited()


# ---------------------------------------------------------------------------
# `/auth/me` publishes the step-up's own predicate (docs/decisions.md §201)
#
# The profile page stops offering the password as a step-up proof where the
# server stops accepting it. For that to be honest the page must learn exactly
# the rule `_step_up_satisfied` enforces — not the public `/auth/{sso,saml}/
# config` echo, which reports `sso_only` only when the IdP config resolves and
# so says "open" for a tenant whose broken config still closes the password.
# ---------------------------------------------------------------------------

# Every shape a hand-copied rule could get wrong. The last is the one the public
# config echo gets wrong: SSO switched on and required, with no IdP it can
# resolve — the password is closed at sign-in and at the step-up all the same.
_OIDC_READY = {
    "enabled": True,
    "sso_only": True,
    "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
    "client_id": "feoh",
    "client_secret": "not-a-real-secret",
}
_PREDICATE_CASES = [
    pytest.param(None, False, id="no-settings"),
    pytest.param({"sso": {"sso_only": True}}, False, id="sso_only-without-enabled"),
    pytest.param({"sso": {"enabled": False, "sso_only": True}}, False, id="sso-switched-off"),
    pytest.param({"sso": {"enabled": True}}, False, id="sso-on-password-still-open"),
    pytest.param({"sso": _OIDC_READY}, True, id="sso-only-idp-resolves"),
    pytest.param(
        {"sso": {"enabled": True, "sso_only": True}}, True, id="sso-only-idp-unresolvable"
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("org_settings,closed", _PREDICATE_CASES)
async def test_me_reports_exactly_the_predicate_the_step_up_and_login_enforce(org_settings, closed):
    """`password_sign_in_closed` on `/auth/me` is true exactly when a CORRECT
    password fails the step-up and is refused at sign-in — the same answer from
    all three, for every shape of `settings.sso`, including the one where the
    public config echo disagrees."""
    from app.api.auth import _step_up_satisfied, get_me, login
    from app.schemas.auth import LoginRequest, MFAStepUpRequest

    pw = "Correct-Horse-9"
    user = _account_with_totp(pw)
    org = SimpleNamespace(id=user.organization_id, settings=org_settings)

    me = await get_me(user=user, db=_control_db(org))
    assert me.password_sign_in_closed is closed

    proved = await _step_up_satisfied(
        _control_db(org),
        user,
        MFAStepUpRequest(password=pw),
        operation="totp_enroll",
        rp=None,  # only an assertion reads it; none is offered
    )
    assert proved is (not closed)

    signer = _user_with_password(pw)
    with (
        patch("app.api.auth.dispatch_auth_audit", AsyncMock()),
        patch("app.api.auth.register_session", AsyncMock()),
    ):
        try:
            await login(
                body=LoginRequest(email=signer.email, password=pw),
                request=_fake_request(),
                db=_db_user_then_org(signer, org),
            )
            refused = False
        except HTTPException as exc:
            assert exc.status_code == 403
            refused = True
    assert refused is closed


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["enroll", "disable"])
@pytest.mark.parametrize(
    "org_settings,expected",
    [
        pytest.param(SSO_ONLY_SETTINGS, "sso_only", id="sso-only"),
        pytest.param({"sso": {"enabled": True}}, "generic", id="password-open"),
    ],
)
async def test_a_refused_code_names_only_the_proofs_the_tenant_accepts(
    endpoint, org_settings, expected
):
    """The profile page sends an authenticator code where the password is no
    proof (§201). A mistyped one must not be answered with the generic sentence,
    which asks for the password the page no longer shows — in an SSO-only
    tenant every refusal names only the code and the passkey. Where the password
    is open the generic sentence is still the right one."""
    from app.api.auth import (
        STEP_UP_FAILURE_DETAIL,
        STEP_UP_SSO_ONLY_DETAIL,
        disable_mfa,
        enroll_mfa_start,
    )
    from app.schemas.auth import MFADisableRequest, MFAStepUpRequest

    user = _account_with_totp("Correct-Horse-9")
    org = SimpleNamespace(id=user.organization_id, settings=org_settings)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        # The code is "wrong" by construction rather than by guessing one that
        # is not the current TOTP value.
        patch("app.api.auth.mfa.step_up_verified", AsyncMock(return_value=False)),
        patch("app.api.auth.dispatch_auth_audit", AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc:
            if endpoint == "enroll":
                await enroll_mfa_start(
                    body=MFAStepUpRequest(code="123456"), user=user, db=_control_db(org)
                )
            else:
                await disable_mfa(
                    body=MFADisableRequest(code="123456"), user=user, db=_control_db(org)
                )

    assert exc.value.status_code == 400
    if expected == "sso_only":
        assert exc.value.detail == STEP_UP_SSO_ONLY_DETAIL
        assert "password" not in exc.value.detail.lower()
    else:
        assert exc.value.detail == STEP_UP_FAILURE_DETAIL
    assert user.mfa_enabled is True
    assert user.mfa_secret == TOTP_SECRET
