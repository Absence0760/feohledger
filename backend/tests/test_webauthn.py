"""Unit tests for the WebAuthn / passkey MFA service (services/webauthn.py).

Covers the full register + authenticate ceremonies end-to-end against a small
in-test software authenticator (ES256, ``fmt: none`` attestation), plus the
pure parsing helpers and the challenge stash/consume + origin guards.

The software authenticator below produces exactly the wire shapes the browser's
``navigator.credentials.create()`` / ``.get()`` emit (base64url fields,
``clientDataJSON``, ``attestationObject`` / ``authenticatorData`` + signature),
so ``finish_registration`` / ``finish_authentication`` run the real py_webauthn
verification — no mocking of the verify call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
import uuid

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

RP_ID = "localhost"
ORIGIN = "http://localhost:7777"


def platform_rp():
    """The resolved Relying Party the dev defaults produce.

    Every ceremony function now takes the RP explicitly (no config read inside),
    so tests state it the same way a route does — via the resolver, not a
    hand-built dataclass, so a change to how the platform RP is derived is felt
    here too.
    """
    from app.services import webauthn_rp

    return webauthn_rp.platform_relying_party()


# ---------------------------------------------------------------------------
# In-test software authenticator
# ---------------------------------------------------------------------------


class SoftAuthenticator:
    """Minimal FIDO2 authenticator: ES256 key, self-attestation 'none'."""

    def __init__(self):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.sign_count = 0

    def _cose_public_key(self) -> dict:
        nums = self.private_key.public_key().public_numbers()
        x = nums.x.to_bytes(32, "big")
        y = nums.y.to_bytes(32, "big")
        # COSE_Key map: kty=2(EC2), alg=-7(ES256), crv=1(P-256), x(-2), y(-3)
        return {1: 2, 3: -7, -1: 1, -2: x, -3: y}

    def _auth_data(
        self, *, include_attested_cred: bool, user_present=True, user_verified=True
    ) -> bytes:
        rp_id_hash = hashlib.sha256(RP_ID.encode()).digest()
        flags = 0
        if user_present:
            flags |= 0x01
        if user_verified:
            flags |= 0x04
        if include_attested_cred:
            flags |= 0x40  # AT — attested credential data present
        auth_data = rp_id_hash + bytes([flags]) + struct.pack(">I", self.sign_count)
        if include_attested_cred:
            aaguid = b"\x00" * 16
            cred_id_len = struct.pack(">H", len(self.credential_id))
            cose_key = cbor2.dumps(self._cose_public_key())
            auth_data += aaguid + cred_id_len + self.credential_id + cose_key
        return auth_data

    def create(self, challenge_b64url: str) -> str:
        """Emulate navigator.credentials.create() -> registration response JSON."""
        client_data = json.dumps(
            {"type": "webauthn.create", "challenge": challenge_b64url, "origin": ORIGIN}
        ).encode()
        auth_data = self._auth_data(include_attested_cred=True)
        attestation_object = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return json.dumps(
            {
                "id": bytes_to_base64url(self.credential_id),
                "rawId": bytes_to_base64url(self.credential_id),
                "type": "public-key",
                "response": {
                    "clientDataJSON": bytes_to_base64url(client_data),
                    "attestationObject": bytes_to_base64url(attestation_object),
                    "transports": ["internal", "hybrid"],
                },
                "clientExtensionResults": {},
            }
        )

    def get(self, challenge_b64url: str) -> str:
        """Emulate navigator.credentials.get() -> assertion response JSON."""
        self.sign_count += 1
        client_data = json.dumps(
            {"type": "webauthn.get", "challenge": challenge_b64url, "origin": ORIGIN}
        ).encode()
        auth_data = self._auth_data(include_attested_cred=False)
        client_data_hash = hashlib.sha256(client_data).digest()
        signature = self.private_key.sign(auth_data + client_data_hash, ec.ECDSA(hashes.SHA256()))
        return json.dumps(
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
# Fake Redis (matches the mfa test pattern)
# ---------------------------------------------------------------------------


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


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webauthn.get_redis", _get_redis)
    return fake


@pytest.fixture(autouse=True)
def _webauthn_settings(monkeypatch):
    """Pin RP id / origin to the dev defaults the software authenticator uses."""
    from app.services import webauthn, webauthn_rp

    monkeypatch.setattr(webauthn_rp.settings, "webauthn_rp_id", RP_ID)
    monkeypatch.setattr(webauthn_rp.settings, "webauthn_origins", ORIGIN)
    monkeypatch.setattr(webauthn.settings, "webauthn_challenge_ttl_seconds", 300)


# ---------------------------------------------------------------------------
# Registration ceremony
# ---------------------------------------------------------------------------


def _challenge_from_options(options_json: str) -> str:
    return json.loads(options_json)["challenge"]


def test_registration_round_trip(fake_redis):
    from app.services import webauthn

    user_id = uuid.uuid4()
    options_json = asyncio.run(
        webauthn.begin_registration(
            user_id=user_id,
            user_name="user@acme.com",
            user_display_name="Acme User",
            existing_credential_ids=[],
            rp=platform_rp(),
        )
    )
    assert json.loads(options_json)["rp"]["id"] == RP_ID
    # Challenge stashed in Redis under the registration namespace
    assert f"webauthn:reg_challenge:{user_id}" in fake_redis.store

    auth = SoftAuthenticator()
    response = auth.create(_challenge_from_options(options_json))
    fields = asyncio.run(
        webauthn.finish_registration(user_id=user_id, credential_json=response, rp=platform_rp())
    )
    assert fields["credential_id"] == bytes_to_base64url(auth.credential_id)
    assert fields["public_key"]
    assert fields["sign_count"] == 0
    assert "internal" in fields["transports"]
    # Challenge consumed (single-use)
    assert f"webauthn:reg_challenge:{user_id}" not in fake_redis.store


def test_registration_rejects_replayed_challenge(fake_redis):  # noqa: ARG001
    from app.services import webauthn

    user_id = uuid.uuid4()
    options_json = asyncio.run(
        webauthn.begin_registration(
            user_id=user_id,
            user_name="u@x.com",
            user_display_name="U",
            existing_credential_ids=[],
            rp=platform_rp(),
        )
    )
    auth = SoftAuthenticator()
    response = auth.create(_challenge_from_options(options_json))
    asyncio.run(
        webauthn.finish_registration(user_id=user_id, credential_json=response, rp=platform_rp())
    )
    # Second verify with the same response — challenge already consumed
    with pytest.raises(webauthn.WebAuthnError):
        asyncio.run(
            webauthn.finish_registration(
                user_id=user_id, credential_json=response, rp=platform_rp()
            )
        )


def test_registration_rejects_bad_origin(fake_redis):  # noqa: ARG001
    from app.services import webauthn

    user_id = uuid.uuid4()
    options_json = asyncio.run(
        webauthn.begin_registration(
            user_id=user_id,
            user_name="u@x.com",
            user_display_name="U",
            existing_credential_ids=[],
            rp=platform_rp(),
        )
    )
    auth = SoftAuthenticator()
    response = auth.create(_challenge_from_options(options_json))
    tampered = json.loads(response)
    bad_client_data = json.dumps(
        {
            "type": "webauthn.create",
            "challenge": _challenge_from_options(options_json),
            "origin": "https://evil.example.com",
        }
    ).encode()
    tampered["response"]["clientDataJSON"] = bytes_to_base64url(bad_client_data)
    with pytest.raises(webauthn.WebAuthnError):
        asyncio.run(
            webauthn.finish_registration(
                user_id=user_id, credential_json=json.dumps(tampered), rp=platform_rp()
            )
        )


# ---------------------------------------------------------------------------
# Authentication ceremony
# ---------------------------------------------------------------------------


def _register(webauthn, user_id) -> tuple[SoftAuthenticator, dict]:
    options_json = asyncio.run(
        webauthn.begin_registration(
            user_id=user_id,
            user_name="u@x.com",
            user_display_name="U",
            existing_credential_ids=[],
            rp=platform_rp(),
        )
    )
    auth = SoftAuthenticator()
    response = auth.create(_challenge_from_options(options_json))
    fields = asyncio.run(
        webauthn.finish_registration(user_id=user_id, credential_json=response, rp=platform_rp())
    )
    return auth, fields


def test_authentication_round_trip(fake_redis):  # noqa: ARG001
    from app.services import webauthn

    user_id = uuid.uuid4()
    auth, fields = _register(webauthn, user_id)

    options_json = asyncio.run(
        webauthn.begin_authentication(
            user_id=user_id,
            credentials=[
                {"credential_id": fields["credential_id"], "transports": fields["transports"]}
            ],
            purpose=webauthn.ASSERTION_PURPOSE_LOGIN,
            rp=platform_rp(),
        )
    )
    response = auth.get(_challenge_from_options(options_json))
    new_count = asyncio.run(
        webauthn.finish_authentication(
            user_id=user_id,
            credential_json=response,
            stored_public_key=fields["public_key"],
            stored_sign_count=fields["sign_count"],
            purpose=webauthn.ASSERTION_PURPOSE_LOGIN,
            rp=platform_rp(),
        )
    )
    # Software authenticator bumps its counter on get(); new count must advance
    assert new_count == 1


def test_authentication_rejects_counter_regression(fake_redis):  # noqa: ARG001
    """A cloned authenticator presenting a stale counter is rejected."""
    from app.services import webauthn

    user_id = uuid.uuid4()
    auth, fields = _register(webauthn, user_id)
    options_json = asyncio.run(
        webauthn.begin_authentication(
            user_id=user_id,
            credentials=[{"credential_id": fields["credential_id"], "transports": None}],
            purpose=webauthn.ASSERTION_PURPOSE_LOGIN,
            rp=platform_rp(),
        )
    )
    response = auth.get(_challenge_from_options(options_json))
    # Pretend we've already recorded a HIGHER counter than the assertion reports
    with pytest.raises(webauthn.WebAuthnError):
        asyncio.run(
            webauthn.finish_authentication(
                user_id=user_id,
                credential_json=response,
                stored_public_key=fields["public_key"],
                stored_sign_count=99,
                purpose=webauthn.ASSERTION_PURPOSE_LOGIN,
                rp=platform_rp(),
            )
        )


def test_authentication_rejects_wrong_key(fake_redis):  # noqa: ARG001
    """An assertion verified against a different credential's public key fails."""
    from app.services import webauthn

    user_id = uuid.uuid4()
    auth, fields = _register(webauthn, user_id)
    other_auth, other_fields = _register(webauthn, uuid.uuid4())
    options_json = asyncio.run(
        webauthn.begin_authentication(
            user_id=user_id,
            credentials=[{"credential_id": fields["credential_id"], "transports": None}],
            purpose=webauthn.ASSERTION_PURPOSE_LOGIN,
            rp=platform_rp(),
        )
    )
    response = auth.get(_challenge_from_options(options_json))
    with pytest.raises(webauthn.WebAuthnError):
        asyncio.run(
            webauthn.finish_authentication(
                user_id=user_id,
                credential_json=response,
                stored_public_key=other_fields["public_key"],  # wrong key
                stored_sign_count=0,
                purpose=webauthn.ASSERTION_PURPOSE_LOGIN,
                rp=platform_rp(),
            )
        )


def test_authentication_missing_challenge(fake_redis):  # noqa: ARG001
    from app.services import webauthn

    user_id = uuid.uuid4()
    auth, fields = _register(webauthn, user_id)
    # No begin_authentication → no stashed challenge
    response = auth.get(bytes_to_base64url(os.urandom(32)))
    with pytest.raises(webauthn.WebAuthnError):
        asyncio.run(
            webauthn.finish_authentication(
                user_id=user_id,
                credential_json=response,
                stored_public_key=fields["public_key"],
                stored_sign_count=0,
                purpose=webauthn.ASSERTION_PURPOSE_LOGIN,
                rp=platform_rp(),
            )
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_extract_credential_id():
    from app.services import webauthn

    cid = bytes_to_base64url(b"abc123")
    assert webauthn.extract_credential_id(json.dumps({"id": cid})) == cid
    assert webauthn.extract_credential_id(json.dumps({"rawId": cid})) == cid
    assert webauthn.extract_credential_id("not json") is None
    assert webauthn.extract_credential_id(json.dumps({})) is None


def test_allowed_origins_falls_back(monkeypatch):
    """The origin list now lives on the RP resolver, not the ceremony module."""
    from app.services import webauthn_rp

    monkeypatch.setattr(webauthn_rp.settings, "webauthn_origins", "")
    assert webauthn_rp.platform_origins() == ("http://localhost:7777",)
    monkeypatch.setattr(webauthn_rp.settings, "webauthn_origins", "https://a.com, https://b.com")
    assert webauthn_rp.platform_origins() == ("https://a.com", "https://b.com")


def test_round_trips_public_key_is_decodable(fake_redis):  # noqa: ARG001
    """The stored base64url public key must decode back to bytes for verify."""
    from app.services import webauthn

    user_id = uuid.uuid4()
    _, fields = _register(webauthn, user_id)
    assert base64url_to_bytes(fields["public_key"])  # no exception


def test_verify_origin_exact_and_wildcard(monkeypatch):
    """A wildcard entry (https://*.base) admits every tenant subdomain so a
    multi-tenant deployment doesn't need an env change per tenant — while
    suffix look-alikes, scheme downgrades, ports, and userinfo tricks stay
    rejected, and the bare base needs its own exact entry."""
    from app.services import webauthn, webauthn_rp

    monkeypatch.setattr(
        webauthn_rp.settings,
        "webauthn_origins",
        "https://app.example.com, https://*.app.example.com",
    )
    assert webauthn._verify_origin_ok("https://app.example.com", webauthn_rp.platform_origins())
    assert webauthn._verify_origin_ok(
        "https://acme.app.example.com", webauthn_rp.platform_origins()
    )
    assert webauthn._verify_origin_ok("https://a.b.app.example.com", webauthn_rp.platform_origins())

    monkeypatch.setattr(webauthn_rp.settings, "webauthn_origins", "https://*.app.example.com")
    assert not webauthn._verify_origin_ok("https://app.example.com", webauthn_rp.platform_origins())
    assert not webauthn._verify_origin_ok(
        "https://evilapp.example.com", webauthn_rp.platform_origins()
    )
    assert not webauthn._verify_origin_ok(
        "http://acme.app.example.com", webauthn_rp.platform_origins()
    )
    assert not webauthn._verify_origin_ok(
        "https://acme.app.example.com:8443", webauthn_rp.platform_origins()
    )
    assert not webauthn._verify_origin_ok(
        "https://evil.com@acme.app.example.com", webauthn_rp.platform_origins()
    )
    assert not webauthn._verify_origin_ok(
        "https://evil.com/?x=.app.example.com", webauthn_rp.platform_origins()
    )
    assert not webauthn._verify_origin_ok(
        "https://foo..app.example.com", webauthn_rp.platform_origins()
    )
    assert not webauthn._verify_origin_ok(
        "https://.app.example.com", webauthn_rp.platform_origins()
    )

    # Malformed wildcard entries never match anything.
    monkeypatch.setattr(webauthn_rp.settings, "webauthn_origins", "https://*.")
    assert not webauthn._verify_origin_ok(
        "https://acme.app.example.com", webauthn_rp.platform_origins()
    )
