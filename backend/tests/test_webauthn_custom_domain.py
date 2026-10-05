"""Passkeys on a tenant vanity host — per-request Relying Party resolution.

`FEOH_WEBAUTHN_RP_ID` used to be a single global, and a WebAuthn credential is
bound to exactly one registrable domain, so a tenant served on
`ap.acmecorp.com` could neither register nor use a passkey. The RP ID and the
allowed-origin list are now resolved per request from the host the ceremony is
actually running on (`app/services/webauthn_rp.py`).

The `Host` header is client-supplied, so this file's centre of gravity is that
it can never *become* an RP ID on its own:

  - the platform host resolves the configured global RP ID + origins
  - a host registered on THIS tenant's `settings.brand.custom_domains` resolves
    to itself
  - an unregistered / forged host falls back to the global — it is never used
  - another tenant's registered vanity domain does not resolve for this tenant
  - register and authenticate resolve identically, and a ceremony begun on one
    host cannot be finished on another
  - a credential bound to a different RP ID is reported as such ("belongs to
    <host>") rather than failing as an opaque signature error

Assertions run through the in-test software authenticator from
`test_webauthn.py`, so the real py_webauthn verification executes throughout.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from tests.test_webauthn import RP_ID, SoftAuthenticator, _challenge_from_options

PLATFORM_ORIGIN = "http://localhost:7777"
VANITY_HOST = "ap.acmecorp.com"
VANITY_ORIGIN = f"https://{VANITY_HOST}"
OTHER_TENANT_HOST = "ap.othercorp.com"


# ---------------------------------------------------------------------------
# Fixtures — an org whose settings carry a registered custom domain
# ---------------------------------------------------------------------------


def _org(custom_domains=None, *, brand_extra=None):
    brand: dict = {"product_name": "Acme AP"}
    if custom_domains is not None:
        brand["custom_domains"] = custom_domains
    if brand_extra:
        brand.update(brand_extra)
    return SimpleNamespace(id=uuid.uuid4(), settings={"brand": brand})


def _user(org_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="user@acme.test",
        full_name="Acme User",
        organization_id=org_id or uuid.uuid4(),
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
        # Real Redis DEL returns the number of keys removed; the email-OTP
        # single-use claim reads it.
        return 0 if self.store.pop(key, None) is None else 1


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    from app.services import webauthn, webauthn_rp

    monkeypatch.setattr(webauthn_rp.settings, "webauthn_rp_id", RP_ID)
    monkeypatch.setattr(webauthn_rp.settings, "webauthn_origins", PLATFORM_ORIGIN)
    monkeypatch.setattr(webauthn.settings, "webauthn_challenge_ttl_seconds", 300)
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webauthn.get_redis", _get_redis)
    return fake


class _AccountDB:
    """Control-plane stand-in.

    `_user_passkeys` selects WebAuthnCredential (scalars().all()); the assertion
    lookup selects one credential; `_load_user_org` selects the Organization.
    The stand-in answers by the model the statement names, so the ordering of
    those reads inside a handler doesn't have to be encoded here.
    """

    def __init__(self, *, org=None, creds=None, user=None):
        self.org = org
        self.creds = list(creds or [])
        self.user = user
        self.added = []
        self.commits = 0

    async def execute(self, stmt, *_a, **_k):
        rendered = str(stmt)
        result = MagicMock()
        if "organizations" in rendered:
            result.scalar_one_or_none.return_value = self.org
            result.scalars.return_value.all.return_value = [self.org] if self.org else []
        elif "webauthn_credentials" in rendered:
            result.scalar_one_or_none.return_value = self.creds[0] if self.creds else None
            result.scalars.return_value.all.return_value = self.creds
        else:  # users
            result.scalar_one_or_none.return_value = self.user
            result.scalars.return_value.all.return_value = [self.user] if self.user else []
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


class _SoftAuthenticatorFor(SoftAuthenticator):
    """The shared software authenticator, but signing for an arbitrary RP ID
    and origin — that is the whole point on a vanity host."""

    def __init__(self, rp_id: str, origin: str):
        super().__init__()
        self._rp_id = rp_id
        self._origin = origin

    def _auth_data(self, **kwargs):
        import hashlib
        import struct

        import cbor2

        include_attested_cred = kwargs.get("include_attested_cred", False)
        rp_id_hash = hashlib.sha256(self._rp_id.encode()).digest()
        flags = 0x01 | 0x04
        if include_attested_cred:
            flags |= 0x40
        auth_data = rp_id_hash + bytes([flags]) + struct.pack(">I", self.sign_count)
        if include_attested_cred:
            cred_id_len = struct.pack(">H", len(self.credential_id))
            auth_data += (
                b"\x00" * 16
                + cred_id_len
                + self.credential_id
                + cbor2.dumps(self._cose_public_key())
            )
        return auth_data

    def create(self, challenge_b64url: str) -> str:
        import json as _json

        import cbor2
        from webauthn.helpers import bytes_to_base64url

        client_data = _json.dumps(
            {"type": "webauthn.create", "challenge": challenge_b64url, "origin": self._origin}
        ).encode()
        auth_data = self._auth_data(include_attested_cred=True)
        attestation_object = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return _json.dumps(
            {
                "id": bytes_to_base64url(self.credential_id),
                "rawId": bytes_to_base64url(self.credential_id),
                "type": "public-key",
                "response": {
                    "clientDataJSON": bytes_to_base64url(client_data),
                    "attestationObject": bytes_to_base64url(attestation_object),
                    "transports": ["internal"],
                },
                "clientExtensionResults": {},
            }
        )

    def get(self, challenge_b64url: str) -> str:
        import hashlib
        import json as _json

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from webauthn.helpers import bytes_to_base64url

        self.sign_count += 1
        client_data = _json.dumps(
            {"type": "webauthn.get", "challenge": challenge_b64url, "origin": self._origin}
        ).encode()
        auth_data = self._auth_data(include_attested_cred=False)
        signature = self.private_key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return _json.dumps(
            {
                "id": bytes_to_base64url(self.credential_id),
                "rawId": bytes_to_base64url(self.credential_id),
                "type": "public-key",
                "response": {
                    "clientDataJSON": bytes_to_base64url(client_data),
                    "authenticatorData": bytes_to_base64url(auth_data),
                    "signature": bytes_to_base64url(signature),
                },
                "clientExtensionResults": {},
            }
        )


# ---------------------------------------------------------------------------
# 1. The resolver — the whole security argument lives here
# ---------------------------------------------------------------------------


def test_platform_host_resolves_the_global_rp_and_origins():
    from app.services import webauthn_rp

    org = _org([VANITY_HOST])
    for host in (None, "localhost", "acme.localhost", "acme.localhost:7777"):
        rp = webauthn_rp.resolve_relying_party(host=host, org_settings=org.settings)
        assert rp.rp_id == RP_ID, host
        assert rp.origins == (PLATFORM_ORIGIN,), host
        assert rp.source == webauthn_rp.RP_SOURCE_PLATFORM
        assert rp.is_custom_domain is False


def test_a_registered_custom_domain_resolves_to_itself():
    from app.services import webauthn_rp

    org = _org([VANITY_HOST])
    rp = webauthn_rp.resolve_relying_party(host=VANITY_HOST, org_settings=org.settings)
    assert rp.rp_id == VANITY_HOST
    assert rp.is_custom_domain is True
    # `https://<host>` is auto-allowed so no per-tenant env change is needed;
    # the configured platform origins stay listed so a local dev origin can
    # still be added by an operator.
    assert rp.origins[0] == VANITY_ORIGIN
    assert PLATFORM_ORIGIN in rp.origins


def test_a_forged_or_unregistered_host_never_becomes_the_rp_id():
    """The core fail-closed property. `Host` is attacker-controlled, so an
    unknown value must resolve to the global config, never to itself."""
    from app.services import webauthn_rp

    org = _org([VANITY_HOST])
    for forged in (
        "evil.example.com",
        "ap.acmecorp.com.evil.example.com",
        "acmecorp.com",  # parent of the registered host, not the registered host
        "AP.ACMECORP.COM.attacker.test",
        "not a host",
        "[::1]",
    ):
        rp = webauthn_rp.resolve_relying_party(host=forged, org_settings=org.settings)
        assert rp.rp_id == RP_ID, forged
        assert rp.origins == (PLATFORM_ORIGIN,), forged
        assert rp.rp_id != forged


def test_another_tenants_custom_domain_does_not_resolve_for_this_tenant():
    """Cross-tenant: the domain list consulted is the one belonging to the org
    that owns the ACCOUNT, so a vanity host registered by someone else is just
    an unknown host here."""
    from app.services import webauthn_rp

    mine = _org([VANITY_HOST])
    theirs = _org([OTHER_TENANT_HOST])

    assert (
        webauthn_rp.resolve_relying_party(host=OTHER_TENANT_HOST, org_settings=mine.settings).rp_id
        == RP_ID
    )
    # Sanity: it IS resolvable for the tenant that actually registered it.
    assert (
        webauthn_rp.resolve_relying_party(
            host=OTHER_TENANT_HOST, org_settings=theirs.settings
        ).rp_id
        == OTHER_TENANT_HOST
    )


def test_host_is_normalized_and_matched_case_insensitively():
    from app.services import webauthn_rp

    org = _org([f"  {VANITY_HOST.upper()}  "])
    for variant in (VANITY_HOST, VANITY_HOST.upper(), f"{VANITY_HOST}:443"):
        rp = webauthn_rp.resolve_relying_party(host=variant, org_settings=org.settings)
        assert rp.rp_id == VANITY_HOST, variant


def test_a_malformed_brand_block_degrades_to_the_platform_rp():
    """A bad settings blob must not break sign-in — it means "no custom
    domain", the same fail-closed answer an unknown host gets."""
    from app.services import webauthn_rp

    for settings_blob in (
        None,
        {},
        {"brand": "not-a-dict"},
        {"brand": {"custom_domains": "not-a-list"}},
        {"brand": {"custom_domains": [None, 17, {"nested": True}]}},
    ):
        rp = webauthn_rp.resolve_relying_party(host=VANITY_HOST, org_settings=settings_blob)
        assert rp.rp_id == RP_ID


def test_a_vanity_host_under_the_platform_rp_keeps_the_platform_rp():
    """`acme.app.example.com` under an `app.example.com` platform RP already
    works with existing passkeys — narrowing it to its own RP ID would strand
    them for no gain."""
    from app.services import webauthn_rp

    with patch.object(webauthn_rp.settings, "webauthn_rp_id", "app.example.com"):
        org = _org(["acme.app.example.com"])
        rp = webauthn_rp.resolve_relying_party(
            host="acme.app.example.com", org_settings=org.settings
        )
        assert rp.rp_id == "app.example.com"
        assert rp.source == webauthn_rp.RP_SOURCE_PLATFORM


def test_requires_tenant_domain_lookup_only_for_off_platform_hosts():
    """The org is loaded only when the host could change the answer — the
    ordinary subdomain path must not pay an extra query."""
    from app.services import webauthn_rp

    assert webauthn_rp.requires_tenant_domain_lookup(None) is False
    assert webauthn_rp.requires_tenant_domain_lookup("localhost") is False
    assert webauthn_rp.requires_tenant_domain_lookup("acme.localhost:7777") is False
    assert webauthn_rp.requires_tenant_domain_lookup(VANITY_HOST) is True


def test_effective_rp_id_treats_null_as_the_platform_rp():
    """Rows predating the column (and any written by an old worker mid-deploy)
    were necessarily registered under the global RP ID."""
    from app.services import webauthn_rp

    assert webauthn_rp.effective_rp_id(None) == RP_ID
    assert webauthn_rp.effective_rp_id("") == RP_ID
    assert webauthn_rp.effective_rp_id("  AP.ACMECORP.COM ") == VANITY_HOST


def test_platform_rp_id_never_resolves_blank():
    from app.services import webauthn_rp

    with patch.object(webauthn_rp.settings, "webauthn_rp_id", ""):
        assert webauthn_rp.platform_rp_id() == webauthn_rp.DEFAULT_RP_ID


# ---------------------------------------------------------------------------
# 2. Register and authenticate resolve the SAME RP ID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_then_authenticate_on_a_vanity_host_round_trips():
    """End-to-end on `ap.acmecorp.com`: the credential is stored bound to that
    host and the login ceremony verifies against the same RP ID."""
    from app.api import auth as auth_mod
    from app.schemas.auth import (
        WebAuthnAuthFinishRequest,
        WebAuthnAuthStartRequest,
        WebAuthnRegisterFinishRequest,
    )
    from app.services import mfa

    org = _org([VANITY_HOST])
    user = _user(org.id)
    db = _AccountDB(org=org, user=user)
    soft = _SoftAuthenticatorFor(VANITY_HOST, VANITY_ORIGIN)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.dispatch_auth_audit", new=AsyncMock()),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
        patch("app.api.auth.register_session", new=AsyncMock()),
        patch("app.api.auth.create_access_token_with_jti", return_value=("TOKEN", "JTI")),
    ):
        start = await auth_mod.passkey_register_start(user=user, db=db, host=VANITY_HOST)
        assert start.options["rp"]["id"] == VANITY_HOST

        cred_meta = await auth_mod.passkey_register_finish(
            body=WebAuthnRegisterFinishRequest(
                credential=json.loads(soft.create(start.options["challenge"])), name="Laptop"
            ),
            user=user,
            db=db,
            host=VANITY_HOST,
        )
        assert cred_meta.rp_id == VANITY_HOST
        assert cred_meta.usable_here is True
        stored = db.added[0]
        assert stored.rp_id == VANITY_HOST

        auth_db = _AccountDB(org=org, user=user, creds=[stored])
        challenge_token = mfa.create_challenge_token(user.id)
        auth_start = await auth_mod.passkey_authenticate_start(
            body=WebAuthnAuthStartRequest(challenge_token=challenge_token),
            request=SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={}),
            db=auth_db,
            host=VANITY_HOST,
        )
        token = await auth_mod.passkey_authenticate_finish(
            body=WebAuthnAuthFinishRequest(
                challenge_token=challenge_token,
                credential=json.loads(
                    soft.get(_challenge_from_options(json.dumps(auth_start.options)))
                ),
            ),
            request=SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={}),
            db=auth_db,
            host=VANITY_HOST,
        )
    assert token.access_token == "TOKEN"


@pytest.mark.asyncio
async def test_registration_on_a_forged_host_is_bound_to_the_platform_rp():
    """A ceremony arriving on an unregistered host still works — under the
    GLOBAL RP ID. The forged value never reaches the credential."""
    from app.api import auth as auth_mod
    from app.schemas.auth import WebAuthnRegisterFinishRequest

    org = _org([VANITY_HOST])
    user = _user(org.id)
    db = _AccountDB(org=org, user=user)
    soft = _SoftAuthenticatorFor(RP_ID, PLATFORM_ORIGIN)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.dispatch_auth_audit", new=AsyncMock()),
    ):
        start = await auth_mod.passkey_register_start(user=user, db=db, host="evil.example.com")
        assert start.options["rp"]["id"] == RP_ID
        await auth_mod.passkey_register_finish(
            body=WebAuthnRegisterFinishRequest(
                credential=json.loads(soft.create(start.options["challenge"]))
            ),
            user=user,
            db=db,
            host="evil.example.com",
        )
    assert db.added[0].rp_id == RP_ID


@pytest.mark.asyncio
async def test_a_ceremony_cannot_be_started_on_one_host_and_finished_on_another():
    """The RP ID is recorded with the challenge, so a half-host-swapped
    ceremony is refused rather than storing a credential bound to a domain the
    authenticator never signed."""
    from app.api import auth as auth_mod
    from app.schemas.auth import WebAuthnRegisterFinishRequest

    org = _org([VANITY_HOST])
    user = _user(org.id)
    db = _AccountDB(org=org, user=user)
    soft = _SoftAuthenticatorFor(VANITY_HOST, VANITY_ORIGIN)

    with patch("app.api.auth.settings.mfa_enabled", True):
        start = await auth_mod.passkey_register_start(user=user, db=db, host=VANITY_HOST)
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_register_finish(
                body=WebAuthnRegisterFinishRequest(
                    credential=json.loads(soft.create(start.options["challenge"]))
                ),
                user=user,
                db=db,
                host=None,  # platform host — a different RP
            )
    assert exc.value.status_code == 400
    assert not db.added


# ---------------------------------------------------------------------------
# 3. A cross-host credential is REPORTED, not silently broken
# ---------------------------------------------------------------------------


def _platform_cred(user_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        credential_id="Y3JlZA",
        public_key="cGs",
        sign_count=0,
        transports="internal",
        rp_id=RP_ID,
        name="Laptop",
        created_at=None,
        last_used_at=None,
    )


@pytest.mark.asyncio
async def test_login_on_a_vanity_host_names_the_host_the_passkey_belongs_to():
    from app.api import auth as auth_mod
    from app.schemas.auth import WebAuthnAuthStartRequest
    from app.services import mfa

    org = _org([VANITY_HOST])
    user = _user(org.id)
    db = _AccountDB(org=org, user=user, creds=[_platform_cred(user.id)])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_authenticate_start(
                body=WebAuthnAuthStartRequest(challenge_token=mfa.create_challenge_token(user.id)),
                request=SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={}),
                db=db,
                host=VANITY_HOST,
            )
    assert exc.value.status_code == 400
    # Legible, not opaque: names where the passkey lives and where they are.
    assert RP_ID in exc.value.detail
    assert VANITY_HOST in exc.value.detail
    assert exc.value.detail != "No passkey registered"


@pytest.mark.asyncio
async def test_step_up_on_a_vanity_host_names_the_host_too():
    from app.api import auth as auth_mod
    from app.schemas.auth import WebAuthnStepUpStartRequest

    org = _org([VANITY_HOST])
    user = _user(org.id)
    db = _AccountDB(org=org, user=user, creds=[_platform_cred(user.id)])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_step_up_start(
                body=WebAuthnStepUpStartRequest(operation="passkey_register"),
                user=user,
                db=db,
                host=VANITY_HOST,
            )
    assert exc.value.status_code == 400
    assert RP_ID in exc.value.detail


@pytest.mark.asyncio
async def test_an_account_with_no_passkey_at_all_stays_opaque():
    """The "belongs to another host" message only appears when there IS a
    passkey to explain — an account with none keeps the old opaque answer so
    the response can't be used to probe factor enrollment."""
    from app.api import auth as auth_mod
    from app.schemas.auth import WebAuthnAuthStartRequest
    from app.services import mfa

    org = _org([VANITY_HOST])
    user = _user(org.id)
    db = _AccountDB(org=org, user=user, creds=[])

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.passkey_authenticate_start(
                body=WebAuthnAuthStartRequest(challenge_token=mfa.create_challenge_token(user.id)),
                request=SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={}),
                db=db,
                host=VANITY_HOST,
            )
    assert exc.value.detail == "No passkey registered"


@pytest.mark.asyncio
async def test_passkey_list_reports_where_each_credential_belongs():
    from app.api import auth as auth_mod

    org = _org([VANITY_HOST])
    user = _user(org.id)
    platform_cred = _platform_cred(user.id)
    vanity_cred = _platform_cred(user.id)
    vanity_cred.rp_id = VANITY_HOST
    legacy_cred = _platform_cred(user.id)
    legacy_cred.rp_id = None  # pre-migration row

    db = _AccountDB(org=org, user=user, creds=[platform_cred, vanity_cred, legacy_cred])
    rows = await auth_mod.passkey_list(user=user, db=db, host=VANITY_HOST)

    by_rp = {r.rp_id: r.usable_here for r in rows}
    assert by_rp == {RP_ID: False, VANITY_HOST: True}
    # All three are listed — deleting one must be possible from anywhere.
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_a_cross_host_assertion_is_refused_at_verify():
    """Defence in depth behind the filtering: even if a credential bound to
    another RP is presented directly, the verify path refuses it."""
    from app.api import auth as auth_mod
    from app.services import webauthn, webauthn_rp

    org = _org([VANITY_HOST])
    user = _user(org.id)
    db = _AccountDB(org=org, user=user, creds=[_platform_cred(user.id)])
    vanity_rp = webauthn_rp.resolve_relying_party(host=VANITY_HOST, org_settings=org.settings)

    with pytest.raises(webauthn.WebAuthnError):
        await auth_mod._verify_presented_assertion(
            db,
            user.id,
            json.dumps({"id": "Y3JlZA", "rawId": "Y3JlZA", "response": {}}),
            purpose=webauthn.ASSERTION_PURPOSE_LOGIN,
            rp=vanity_rp,
        )


# ---------------------------------------------------------------------------
# 4. Login offers `passkey` only where it can actually be used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_still_challenges_mfa_but_omits_an_unusable_passkey_method():
    """The GATE counts every passkey (fail closed — a vanity-host passkey is
    still a second factor); the OFFER is narrowed to what this host can
    challenge. `email` is always offered, so nobody is stranded."""
    from app.api import auth as auth_mod
    from app.schemas.auth import LoginRequest

    org = _org([VANITY_HOST])
    user = _user(org.id)
    user.hashed_password = "hashed"
    db = _AccountDB(org=org, user=user, creds=[_platform_cred(user.id)])
    request = SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={})

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.check_rate_limit", new=AsyncMock()),
        patch("app.api.auth.check_auth_failures", new=AsyncMock()),
        patch("app.api.auth.clear_auth_failures", new=AsyncMock()),
        patch("app.api.auth.verify_password", new=AsyncMock(return_value=True)),
        patch("app.api.auth.dispatch_auth_audit", new=AsyncMock()),
    ):
        on_vanity = await auth_mod.login(
            body=LoginRequest(email=user.email, password="pw"),
            request=request,
            db=db,
            host=VANITY_HOST,
        )
        on_platform = await auth_mod.login(
            body=LoginRequest(email=user.email, password="pw"),
            request=request,
            db=db,
            host="acme.localhost:7777",
        )

    # Still challenged on both — the passkey is a live factor either way.
    assert on_vanity.mfa_required is True
    assert "passkey" not in on_vanity.methods
    assert "email" in on_vanity.methods
    assert "passkey" in on_platform.methods


# ---------------------------------------------------------------------------
# 5. The migration — column + backfill, against the real control-plane DB
# ---------------------------------------------------------------------------


_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0091_webauthn_credential_rp_id.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("_mig_0091", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_control_plane_gated_and_reads_the_configured_rp_id():
    """`webauthn_credentials` is control-plane (it hangs off `users.id`), so the
    revision must no-op on a tenant DB — and it must backfill to whatever RP ID
    this deployment has been running under, not a hardcoded dev default."""
    import inspect

    from app.services import webauthn_rp

    mig = _migration_module()
    assert "table_name = 'organizations'" in inspect.getsource(mig._is_control_db)
    assert "if not _is_control_db():" in inspect.getsource(mig.upgrade)
    assert "if not _is_control_db():" in inspect.getsource(mig.downgrade)
    # Backfilled value == what the runtime resolver returns, or NULL and the
    # resolver's fallback would disagree with the column.
    assert mig.configured_rp_id() == webauthn_rp.platform_rp_id()
    with patch.object(webauthn_rp.settings, "webauthn_rp_id", "  APP.EXAMPLE.COM "):
        assert mig.configured_rp_id() == "app.example.com"
    with patch.object(webauthn_rp.settings, "webauthn_rp_id", ""):
        assert mig.configured_rp_id() == mig._FALLBACK_RP_ID


@pytest.mark.asyncio
async def test_migration_backfills_existing_rows_and_is_idempotent(realdb):
    """A row written before the column existed is stamped with the global RP ID
    — provably what it was registered under, since per-host resolution did not
    exist. Re-running changes nothing."""
    mig = _migration_module()
    marker = f"rp-backfill-{uuid.uuid4().hex}"

    async with realdb.control_sessionmaker()() as session:
        user_id = (
            await session.execute(text("SELECT id FROM users ORDER BY created_at LIMIT 1"))
        ).scalar_one()
        cred_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO webauthn_credentials "
                "(id, user_id, credential_id, public_key, sign_count, name, rp_id) "
                "VALUES (:id, :uid, :cid, 'pk', 0, 'Legacy', NULL)"
            ),
            {"id": cred_id, "uid": user_id, "cid": marker},
        )
        await session.commit()

        try:
            # The column add is a no-op here (create_all already made it) — which
            # is exactly the idempotency the migration promises.
            await session.execute(text(mig.ADD_COLUMN_SQL))
            await session.execute(text(mig.BACKFILL_SQL), {"rp_id": mig.configured_rp_id()})
            await session.commit()

            stamped = (
                await session.execute(
                    text("SELECT rp_id FROM webauthn_credentials WHERE id = :id"),
                    {"id": cred_id},
                )
            ).scalar_one()
            assert stamped == mig.configured_rp_id()

            # Re-run: matches no rows, changes nothing.
            await session.execute(text(mig.ADD_COLUMN_SQL))
            await session.execute(text(mig.BACKFILL_SQL), {"rp_id": "something-else"})
            await session.commit()
            again = (
                await session.execute(
                    text("SELECT rp_id FROM webauthn_credentials WHERE id = :id"),
                    {"id": cred_id},
                )
            ).scalar_one()
            assert again == mig.configured_rp_id()
        finally:
            await session.execute(
                text("DELETE FROM webauthn_credentials WHERE id = :id"), {"id": cred_id}
            )
            await session.commit()
