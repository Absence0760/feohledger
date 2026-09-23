"""SSO-only mode — password login is closed when a tenant requires SSO.

`is_sso_only` is deterministic (unit-tested below); the login enforcement is
exercised by calling the `login` handler directly with mocked DB sessions, the
same DB-free pattern as test_auth_error_consistency.py. One realdb test drives
the whole thing over HTTP at the end.
"""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.sso import is_sso_only, sso_only_requested

# Complete IdP blocks, one per protocol. Password sign-in is closed only when
# SSO is on, required, AND the selected protocol's block resolves (§204).
_OIDC_READY = {
    "enabled": True,
    "sso_only": True,
    "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
    "client_id": "feoh",
    "client_secret": "not-a-real-secret",
}
_SAML_READY = {
    "enabled": True,
    "sso_only": True,
    "protocol": "saml",
    "provider": "saml",
    "idp_entity_id": "https://idp.example.com/saml",
    "idp_sso_url": "https://idp.example.com/saml/sso",
    "idp_x509_cert": base64.b64encode(b"fake-but-valid-base64-der-bytes").decode(),
}


def _without(block: dict, *keys: str) -> dict:
    return {k: v for k, v in block.items() if k not in keys}


@pytest.mark.parametrize(
    "settings_dict,expected",
    [
        (None, False),
        ({}, False),
        ({"sso": {}}, False),
        ({"sso": {"sso_only": True}}, False),  # enabled missing => no lockout
        ({"sso": {"enabled": True}}, False),  # sso_only not set
        ({"sso": {"enabled": False, "sso_only": True}}, False),  # SSO off
        ({"sso": _OIDC_READY}, True),
        ({"sso": _SAML_READY}, True),
        # Required, but the IdP block does not resolve: the login page has no
        # SSO button, so the password stays open as the escape hatch.
        ({"sso": {"enabled": True, "sso_only": True}}, False),
        ({"sso": _without(_OIDC_READY, "client_secret")}, False),
        ({"sso": _without(_SAML_READY, "idp_x509_cert")}, False),
        ({"sso": {**_OIDC_READY, "discovery_url": "file:///etc/passwd"}}, False),
        ({"sso": {**_SAML_READY, "idp_x509_cert": "not base64 !!"}}, False),
        # The protocol selects which block must resolve: a complete OIDC block
        # does not satisfy a tenant that is configured for SAML.
        ({"sso": {**_OIDC_READY, "protocol": "saml"}}, False),
    ],
)
def test_is_sso_only(settings_dict, expected):
    assert is_sso_only(settings_dict) is expected


@pytest.mark.parametrize(
    "settings_dict",
    [
        pytest.param({"sso": "yes"}, id="sso-not-an-object"),
        pytest.param(["not", "a", "mapping"], id="settings-not-an-object"),
        pytest.param({"sso": {**_OIDC_READY, "client_id": 12345}}, id="client_id-not-text"),
        pytest.param({"sso": {**_OIDC_READY, "client_secret": "   "}}, id="secret-blank"),
        pytest.param({"sso": {**_OIDC_READY, "discovery_url": "http://[::1"}}, id="bad-ipv6"),
        pytest.param({"sso": {**_OIDC_READY, "provider": ["okta"]}}, id="provider-not-text"),
        pytest.param(
            {"sso": {**_OIDC_READY, "allowed_email_domains": "acme.com"}}, id="allowlist-a-string"
        ),
        pytest.param({"sso": {**_SAML_READY, "idp_x509_cert": 42}}, id="cert-not-text"),
        pytest.param({"sso": {**_SAML_READY, "idp_sso_url": "not-a-url"}}, id="sso-url-shape"),
        pytest.param(
            {"sso": {**_SAML_READY, "idp_x509_cert_multi": "one-cert"}}, id="multi-not-a-list"
        ),
        pytest.param({"sso": {**_SAML_READY, "idp_x509_cert_multi": [None]}}, id="multi-entry"),
        pytest.param({"sso": {**_SAML_READY, "idp_slo_url": {"x": 1}}}, id="slo-not-text"),
        pytest.param({"sso": {**_SAML_READY, "sp_entity_id": 7}}, id="sp-entity-not-text"),
    ],
)
def test_a_malformed_block_does_not_resolve_and_never_raises(settings_dict):
    """`is_sso_only` runs on every password sign-in, over JSONB that
    `PATCH /api/organization` merges with no schema. A malformed value must
    read as "does not resolve", never raise: a raise there would be a 500 on
    every password sign-in in the tenant, which is the lockout again."""
    assert is_sso_only(settings_dict) is False


def test_sso_only_requested_is_the_two_flags_alone():
    """The request, not the verdict: what the admin asked for, whether or not
    the IdP block can deliver it. The write-time refusal and the login warning
    key on it; nothing that closes the password does."""
    broken = {"sso": {"enabled": True, "sso_only": True}}
    assert sso_only_requested(broken) is True
    assert is_sso_only(broken) is False
    assert sso_only_requested({"sso": {"enabled": False, "sso_only": True}}) is False
    assert sso_only_requested({"sso": "yes"}) is False


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
    org = SimpleNamespace(id=user.organization_id, settings={"sso": _SAML_READY})

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

SSO_ONLY_SETTINGS = {"sso": _SAML_READY}
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
@pytest.mark.parametrize(
    "sso_block",
    [
        pytest.param({"enabled": False, "sso_only": True}, id="sso-switched-off"),
        pytest.param({"enabled": True, "sso_only": True}, id="idp-unresolvable"),
        pytest.param(_without(_SAML_READY, "idp_x509_cert"), id="saml-cert-missing"),
    ],
)
async def test_the_password_still_proves_a_step_up_when_sso_is_not_really_enforced(sso_block):
    """The step-up reads the SAME predicate as login: `sso_only` without
    `sso.enabled` is not SSO-only, and neither is `sso_only` over an IdP block
    that does not resolve (that is the escape hatch, §204). There the password
    still proves a step-up, so the two doors cannot disagree about whether the
    password is an authenticator."""
    from app.api.auth import enroll_mfa_start
    from app.schemas.auth import MFAStepUpRequest

    pw = "Correct-Horse-9"
    user = _account_with_totp(pw)
    org = SimpleNamespace(id=user.organization_id, settings={"sso": sso_block})

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
# One predicate, four readers (docs/decisions.md §201, §204)
#
# The profile page stops offering the password as a step-up proof where the
# server stops accepting it, and the login page hides the password form where
# sign-in refuses it. Both are honest only if every reader computes the same
# thing: login's refusal, the step-up's password drop, `/auth/me` and the
# public `/auth/{sso,saml}/config` echo.
# ---------------------------------------------------------------------------

# Every shape a hand-copied rule could get wrong. The unresolvable ones are the
# shapes the readers used to disagree on: the echo said "open" (no SSO button,
# password form shown) while sign-in and the step-up refused the password, so
# nobody in the tenant could start a session. They are open everywhere now.
_PREDICATE_CASES = [
    pytest.param(None, False, id="no-settings"),
    pytest.param({"sso": {"sso_only": True}}, False, id="sso_only-without-enabled"),
    pytest.param({"sso": {"enabled": False, "sso_only": True}}, False, id="sso-switched-off"),
    pytest.param({"sso": {"enabled": True}}, False, id="sso-on-password-still-open"),
    pytest.param({"sso": _OIDC_READY}, True, id="sso-only-idp-resolves"),
    pytest.param({"sso": _SAML_READY}, True, id="sso-only-saml-resolves"),
    pytest.param(
        {"sso": {"enabled": True, "sso_only": True}}, False, id="sso-only-idp-unresolvable"
    ),
    pytest.param(
        {"sso": _without(_OIDC_READY, "discovery_url")}, False, id="sso-only-oidc-incomplete"
    ),
    pytest.param(
        {"sso": _without(_SAML_READY, "idp_sso_url")}, False, id="sso-only-saml-incomplete"
    ),
    pytest.param(
        {"sso": {**_SAML_READY, "idp_x509_cert": "not base64 !!"}},
        False,
        id="sso-only-saml-bad-cert",
    ),
]


async def _config_echoes(org) -> list:
    """What the login page reads: both public config endpoints, for `org`."""
    from app.api import auth_saml, auth_sso

    async def _resolve(slug, host, db):
        return org, "acme"

    with (
        patch.object(auth_sso, "_resolve_org", _resolve),
        patch.object(auth_saml, "_resolve_org", _resolve),
    ):
        return [
            await auth_sso.sso_config(slug="acme", host=None, db=None),
            await auth_saml.saml_config(slug="acme", host=None, db=None),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("org_settings,closed", _PREDICATE_CASES)
async def test_every_reader_reports_exactly_the_predicate_login_enforces(org_settings, closed):
    """`password_sign_in_closed` on `/auth/me` is true exactly when a CORRECT
    password fails the step-up, is refused at sign-in, and the login page is
    told to hide the password form: the same answer from all four, for every
    shape of `settings.sso`."""
    from app.api.auth import _step_up_satisfied, get_me, login
    from app.schemas.auth import LoginRequest, MFAStepUpRequest

    pw = "Correct-Horse-9"
    user = _account_with_totp(pw)
    org = SimpleNamespace(id=user.organization_id, settings=org_settings)

    me = await get_me(user=user, db=_control_db(org))
    assert me.password_sign_in_closed is closed

    # The login page's own rule (`routes/login/+page.svelte`): hide the password
    # form when either endpoint says `enabled && sso_only`. Where it does, the
    # page shows that protocol's SSO button, so a closed password always has a
    # way in beside it.
    echoes = await _config_echoes(org)
    assert any(e.enabled and e.sso_only for e in echoes) is closed
    assert all(e.enabled for e in echoes if e.sso_only)

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
async def test_has_password_reflects_the_account_row_not_the_org_setting():
    """`/auth/me`'s `has_password` is `User.hashed_password is not None` — set
    by whether THIS account was ever given a local credential (never true for
    one JIT-provisioned by OIDC/SAML or created by SCIM,
    `identity_provisioning.py` / `api/scim.py`). It is independent of
    `password_sign_in_closed`, which is a property of the ORG: an org that has
    not closed password sign-in still has SSO-provisioned members with no hash
    to change. docs/followups.md (c)."""
    from app.api.auth import get_me

    org = SimpleNamespace(id=uuid.uuid4(), settings={})  # not SSO-only

    account = _account_with_totp("Correct-Horse-9")
    account.organization_id = org.id

    me = await get_me(user=account, db=_control_db(org))
    assert me.has_password is True
    assert me.password_sign_in_closed is False

    account.hashed_password = None
    me = await get_me(user=account, db=_control_db(org))
    assert me.has_password is False
    assert me.password_sign_in_closed is False


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


# ---------------------------------------------------------------------------
# The escape hatch (docs/decisions.md §204)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sso_block,warned",
    [
        pytest.param({"enabled": True, "sso_only": True}, True, id="requested-not-honoured"),
        pytest.param({"enabled": True}, False, id="not-requested"),
        pytest.param({"enabled": False, "sso_only": True}, False, id="sso-switched-off"),
    ],
)
async def test_a_password_let_through_by_the_escape_hatch_is_logged(sso_block, warned, caplog):
    """The tenant asked for SSO-only and is not getting it. The sign-in
    succeeds, and says so in the log: an org that believes it enforces SSO
    and does not is something an operator has to hear about. The log line
    names the org and nothing from the block, which carries the client
    secret."""
    import logging

    from app.api.auth import login
    from app.schemas.auth import LoginRequest

    pw = "Correct-Horse-9"
    user = _user_with_password(pw)
    block = {**sso_block, "client_secret": "must-never-be-logged"}
    org = SimpleNamespace(id=user.organization_id, settings={"sso": block})

    with (
        caplog.at_level(logging.WARNING, logger="app.api.auth"),
        patch("app.api.auth.dispatch_auth_audit", AsyncMock()),
        patch("app.api.auth.register_session", AsyncMock()),
    ):
        result = await login(
            body=LoginRequest(email=user.email, password=pw),
            request=_fake_request(),
            db=_db_user_then_org(user, org),
        )

    assert getattr(result, "access_token", None)
    hatch = [r for r in caplog.records if "does not resolve" in r.getMessage()]
    assert bool(hatch) is warned
    for record in hatch:
        assert str(org.id) in record.getMessage()
    assert "must-never-be-logged" not in caplog.text


@pytest.fixture
def fake_redis(monkeypatch):
    """A successful login registers a session, which needs the zset+hash
    Redis stand-in rather than the key/value-only autouse stub."""
    from tests.test_session_management import FakeRedis

    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


async def _write_sso_block(realdb, block: dict | None) -> None:
    """Write `settings.sso` straight to the row, the way a DB edit or a
    pre-§204 save would, so no API-side validation is in the way."""
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.organization import Organization

    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()
        settings = dict(org.settings or {})
        if block is None:
            settings.pop("sso", None)
        else:
            settings["sso"] = block
        org.settings = settings
        flag_modified(org, "settings")
        await s.commit()


async def _throwaway_user(realdb, password: str) -> str:
    """A fresh account, so signing in cannot disturb a seeded role user."""
    from app.models.user import User
    from app.utils.passwords import pwd_context

    email = f"ssoonly-{uuid.uuid4().hex[:10]}@{realdb.info('a').slug}.test"
    async with realdb.control_sessionmaker()() as s:
        s.add(
            User(
                id=uuid.uuid4(),
                email=email,
                full_name="SSO-only probe",
                hashed_password=pwd_context.hash(password),
                is_active=True,
                organization_id=realdb.info("a").org_id,
                must_change_password=False,
            )
        )
        await s.commit()
    return email


@pytest.mark.asyncio
async def test_an_unresolvable_sso_only_block_does_not_lock_the_tenant_out(realdb, fake_redis):
    """The lockout, over HTTP against a real control plane. A block that asks
    for SSO-only with no IdP behind it used to get a login page with a password
    form and no SSO button, and a 403 for every password it submitted. Now the
    page and the server agree it is open. With a block that resolves, they
    agree it is closed, and the page has the SSO button to offer instead."""
    pw = "Correct-Horse-9"
    email = await _throwaway_user(realdb, pw)
    slug = realdb.info("a").slug
    try:
        await _write_sso_block(realdb, {"enabled": True, "sso_only": True})
        async with realdb.client(key="a", role=None) as c:
            oidc = (await c.get(f"/api/auth/sso/config?slug={slug}")).json()
            saml = (await c.get(f"/api/auth/saml/config?slug={slug}")).json()
            signed_in = await c.post("/api/auth/login", json={"email": email, "password": pw})
            assert signed_in.status_code == 200, signed_in.text
            token = signed_in.json()["access_token"]
            me = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert oidc["sso_only"] is False and saml["sso_only"] is False
        assert me.status_code == 200, me.text
        assert me.json()["password_sign_in_closed"] is False

        await _write_sso_block(realdb, _SAML_READY)
        async with realdb.client(key="a", role=None) as c:
            saml = (await c.get(f"/api/auth/saml/config?slug={slug}")).json()
            refused = await c.post("/api/auth/login", json={"email": email, "password": pw})
        assert saml == {"enabled": True, "provider": "saml", "sso_only": True}
        assert refused.status_code == 403, refused.text
    finally:
        await _write_sso_block(realdb, None)
