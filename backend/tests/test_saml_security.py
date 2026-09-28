"""Security-property tests for the SAML SP path.

These sign REAL SAML assertions with a throwaway key and run them through the
exact settings the router builds (`auth_saml._build_saml_settings` /
`_acs_request_data`), asserting the validator ACCEPTS a well-formed signed
assertion and REJECTS every tampered variant. This is the load-bearing
verification — if any "reject" case starts passing, an attacker is in.

Plus: RelayState single-use/tenant+request_id binding, the one-time token
handoff, and the per-tenant replay-dedup primitive (on webhook_security's
Redis, which uses SET NX — the OIDC fake doesn't cover that).
"""

from __future__ import annotations

import base64
import datetime
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.utils import OneLogin_Saml2_Utils

import app.api.auth_saml as auth_saml
from app.api.auth_saml import (
    SAMLConfigPublic,
    _acs_request_data,
    _assertion_issuer,
    _build_saml_settings,
    _extract_email_and_name,
    _response_in_response_to,
)
from app.services.sso import (
    ResolvedSAMLConfig,
    SSOConfigError,
    SSOValidationError,
    consume_saml_handoff,
    consume_saml_relay_state,
    create_saml_handoff,
    saml_acs_url,
    store_saml_relay_state,
)

IDP = "https://idp.test/metadata"
SP = "https://sp.test/metadata"
ACS = saml_acs_url()
REQUEST_ID = "_req-abc-123"
PAST = "2000-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"

_RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
_SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"
_RSA_SHA1 = "http://www.w3.org/2000/09/xmldsig#rsa-sha1"
_SHA1 = "http://www.w3.org/2000/09/xmldsig#sha1"


# --- key / cert (module-scoped — RSA keygen is slow) ------------------------


@pytest.fixture(scope="module")
def idp_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-idp")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2035, 1, 1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cert_b64 = "".join(ln for ln in cert_pem.splitlines() if "---" not in ln)
    return key_pem, cert_pem, cert_b64


@pytest.fixture(scope="module")
def attacker_keypair():
    """A second, unrelated key — for the 'signed under a different cert' case."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "attacker")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(2)
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2035, 1, 1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cert_b64 = "".join(ln for ln in cert_pem.splitlines() if "---" not in ln)
    return key_pem, cert_pem, cert_b64


def _config(cert_b64: str, **overrides) -> ResolvedSAMLConfig:
    kwargs = dict(
        provider="saml",
        idp_entity_id=IDP,
        idp_sso_url="https://idp.test/sso",
        idp_x509_cert=cert_b64,
        sp_entity_id=SP,
        allowed_email_domains=[],
    )
    kwargs.update(overrides)
    return ResolvedSAMLConfig(**kwargs)


def _assertion_xml(
    *,
    issuer=IDP,
    audience=SP,
    recipient=ACS,
    in_response_to=REQUEST_ID,
    not_before=PAST,
    not_on_or_after=FUTURE,
    nameid="user@acme.com",
    email="user@acme.com",
    aid="_a1",
) -> str:
    return (
        f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="{aid}" Version="2.0" IssueInstant="2025-01-01T00:00:00Z">'
        f"<saml:Issuer>{issuer}</saml:Issuer>"
        f"<saml:Subject>"
        f'<saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">'
        f"{nameid}</saml:NameID>"
        f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData NotOnOrAfter="{not_on_or_after}" Recipient="{recipient}"'
        + (f' InResponseTo="{in_response_to}"' if in_response_to else "")
        + "/></saml:SubjectConfirmation></saml:Subject>"
        f'<saml:Conditions NotBefore="{not_before}" NotOnOrAfter="{not_on_or_after}">'
        f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience>"
        f"</saml:AudienceRestriction></saml:Conditions>"
        f'<saml:AuthnStatement AuthnInstant="2025-01-01T00:00:00Z"><saml:AuthnContext>'
        f"<saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:Password"
        f"</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
        f'<saml:AttributeStatement><saml:Attribute Name="email">'
        f"<saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute>"
        f"</saml:AttributeStatement></saml:Assertion>"
    )


def _sign(assertion_xml: str, key_pem: str, cert_pem: str, *, alg="sha256") -> str:
    sign_alg, digest = (_RSA_SHA256, _SHA256) if alg == "sha256" else (_RSA_SHA1, _SHA1)
    signed = OneLogin_Saml2_Utils.add_sign(
        assertion_xml, key_pem, cert_pem, sign_algorithm=sign_alg, digest_algorithm=digest
    )
    if isinstance(signed, bytes):
        signed = signed.decode()
    return signed.replace('<?xml version="1.0"?>', "").strip()


def _response(assertion_block: str, *, destination=ACS, in_response_to=REQUEST_ID, extra="") -> str:
    return (
        f'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_r1" Version="2.0" '
        f'IssueInstant="2025-01-01T00:00:00Z" Destination="{destination}"'
        + (f' InResponseTo="{in_response_to}"' if in_response_to else "")
        + f"><saml:Issuer>{IDP}</saml:Issuer><samlp:Status>"
        f'<samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>'
        f"</samlp:Status>{extra}{assertion_block}</samlp:Response>"
    )


def _run_acs(config: ResolvedSAMLConfig, response_xml: str, request_id=REQUEST_ID):
    b64 = base64.b64encode(response_xml.encode()).decode()
    req = _acs_request_data({"SAMLResponse": b64, "RelayState": "x"})
    auth = OneLogin_Saml2_Auth(req, old_settings=_build_saml_settings(config))
    auth.process_response(request_id=request_id)
    return auth


def _signed_response(keypair, **assertion_kwargs) -> str:
    key_pem, cert_pem, _ = keypair
    return _response(_sign(_assertion_xml(**assertion_kwargs), key_pem, cert_pem))


# --- ACCEPT: well-formed signed assertion -----------------------------------


def test_valid_signed_assertion_accepted(idp_keypair):
    _, _, cert_b64 = idp_keypair
    auth = _run_acs(_config(cert_b64), _signed_response(idp_keypair))
    assert auth.is_authenticated()
    assert auth.get_errors() == []
    assert auth.get_nameid() == "user@acme.com"
    assert _assertion_issuer(auth) == IDP  # the router's issuer-pin reads this


# --- REJECT: each tamper funnels to "not authenticated" ---------------------


def test_unsigned_assertion_rejected(idp_keypair):
    # wantAssertionsSigned=True — an unsigned assertion must not pass.
    _, _, cert_b64 = idp_keypair
    auth = _run_acs(_config(cert_b64), _response(_assertion_xml()))
    assert not auth.is_authenticated()


def test_tampered_signature_rejected(idp_keypair):
    _, _, cert_b64 = idp_keypair
    resp = _signed_response(idp_keypair)
    # Corrupt the SignatureValue payload.
    start = resp.index("<ds:SignatureValue")
    val_start = resp.index(">", start) + 1
    val_end = resp.index("</ds:SignatureValue>", val_start)
    flipped = ("A" if resp[val_start] != "A" else "B") + resp[val_start + 1 : val_end]
    resp = resp[:val_start] + flipped + resp[val_end:]
    auth = _run_acs(_config(cert_b64), resp)
    assert not auth.is_authenticated()


def test_sha1_signature_rejected(idp_keypair):
    # rejectDeprecatedAlgorithm=True — a correctly-signed-but-SHA1 assertion
    # must be rejected (the SAML analog of the OIDC SHA-1/none pin).
    key_pem, cert_pem, cert_b64 = idp_keypair
    resp = _response(_sign(_assertion_xml(), key_pem, cert_pem, alg="sha1"))
    auth = _run_acs(_config(cert_b64), resp)
    assert not auth.is_authenticated()


def test_signed_under_different_cert_rejected(idp_keypair, attacker_keypair):
    # Signature valid, but under a cert that is NOT the pinned idp_x509_cert.
    _, _, cert_b64 = idp_keypair
    auth = _run_acs(_config(cert_b64), _signed_response(attacker_keypair))
    assert not auth.is_authenticated()


def test_wrong_audience_rejected(idp_keypair):
    auth = _run_acs(
        _config(idp_keypair[2]), _signed_response(idp_keypair, audience="https://evil.test/sp")
    )
    assert not auth.is_authenticated()


def test_wrong_destination_rejected(idp_keypair):
    key_pem, cert_pem, cert_b64 = idp_keypair
    resp = _response(
        _sign(_assertion_xml(recipient="https://evil.test/acs"), key_pem, cert_pem),
        destination="https://evil.test/acs",
    )
    auth = _run_acs(_config(cert_b64), resp)
    assert not auth.is_authenticated()


def test_expired_assertion_rejected(idp_keypair):
    auth = _run_acs(_config(idp_keypair[2]), _signed_response(idp_keypair, not_on_or_after=PAST))
    assert not auth.is_authenticated()


def test_not_yet_valid_assertion_rejected(idp_keypair):
    auth = _run_acs(_config(idp_keypair[2]), _signed_response(idp_keypair, not_before=FUTURE))
    assert not auth.is_authenticated()


def test_wrong_inresponseto_rejected(idp_keypair):
    # Stored request_id != the response's InResponseTo => reject (anti-injection).
    auth = _run_acs(_config(idp_keypair[2]), _signed_response(idp_keypair), request_id="_other")
    assert not auth.is_authenticated()


def test_valid_response_has_matching_in_response_to(idp_keypair):
    # The router requires _response_in_response_to(auth) == stored request_id.
    auth = _run_acs(_config(idp_keypair[2]), _signed_response(idp_keypair))
    assert _response_in_response_to(auth) == REQUEST_ID


def test_unsolicited_response_rejected_by_handler(idp_keypair):
    # python3-saml validates InResponseTo only when present, so an unsolicited
    # response (no InResponseTo) is authenticated at the library layer — the
    # ROUTER's explicit presence check is what rejects it. Prove that check
    # fires: _response_in_response_to is None != the stored request_id.
    key_pem, cert_pem, cert_b64 = idp_keypair
    resp = _response(
        _sign(_assertion_xml(in_response_to=None), key_pem, cert_pem), in_response_to=None
    )
    auth = _run_acs(_config(cert_b64), resp)
    assert _response_in_response_to(auth) != REQUEST_ID


def test_xsw_injected_second_assertion_rejected(idp_keypair):
    # XML Signature Wrapping: a valid signed assertion + an injected forged
    # (unsigned) assertion sibling with a different identity. The validator must
    # not honor the forged one.
    key_pem, cert_pem, cert_b64 = idp_keypair
    signed = _sign(_assertion_xml(aid="_a1"), key_pem, cert_pem)
    forged = _assertion_xml(aid="_a2", nameid="attacker@evil.test", email="attacker@evil.test")
    resp = _response(signed, extra=forged)
    auth = _run_acs(_config(cert_b64), resp)
    # Either the whole thing is rejected, or only the signed identity is honored
    # — never the attacker's.
    assert auth.get_nameid() != "attacker@evil.test"
    if auth.is_authenticated():
        assert auth.get_nameid() == "user@acme.com"


def test_wrong_issuer_rejected_at_both_layers(idp_keypair):
    # Signed correctly but Issuer != configured idp_entity_id. python3-saml in
    # strict mode rejects this ("Invalid issuer"), so it never authenticates —
    # AND the router's explicit pin (`_assertion_issuer != idp_entity_id`) would
    # independently reject it if the library check ever regressed. Assert both:
    # the library rejection (primary) and that the pin reads the bad issuer
    # (defence-in-depth is real, not a redundant read).
    config = _config(idp_keypair[2])
    auth = _run_acs(config, _signed_response(idp_keypair, issuer="https://evil.test/idp"))
    assert not auth.is_authenticated()
    assert _assertion_issuer(auth) != config.idp_entity_id


# --- identity extraction ----------------------------------------------------


def test_extract_email_prefers_emailaddress_nameid():
    email, _ = _extract_email_and_name("user@acme.com", {})
    assert email == "user@acme.com"


def test_extract_email_falls_back_to_attribute():
    attrs = {"email": ["u@acme.com"], "displayName": ["U N"]}
    email, name = _extract_email_and_name("opaque-id", attrs)
    assert email == "u@acme.com"
    assert name == "U N"


def test_extract_email_none_when_absent():
    email, _ = _extract_email_and_name("opaque-id", {})
    assert email is None


# --- public config field-allowlist (secret-leak gate) -----------------------


def test_saml_config_public_field_allowlist():
    assert set(SAMLConfigPublic.model_fields) == {"enabled", "provider", "sso_only"}


# --- RelayState + handoff + replay dedup (Redis-backed) ---------------------


class _FakeRedis:
    """Models setex/get/delete AND SET NX EX (webhook_security's dedup uses the
    latter — the OIDC fake doesn't cover it, which the review flagged)."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def setex(self, key, ttl, val):
        self.store[key] = val

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)

    async def set(self, key, val, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.sso.get_redis", _get_redis)
    monkeypatch.setattr("app.services.webhook_security.get_redis", _get_redis)
    return fake


async def test_relay_state_single_use_and_binding(fake_redis):
    await store_saml_relay_state("st1", "acme", "_req-1")
    bound = await consume_saml_relay_state("st1")
    assert bound["tenant"] == "acme"
    assert bound["request_id"] == "_req-1"
    # single-use: a second consume fails
    with pytest.raises(SSOValidationError):
        await consume_saml_relay_state("st1")


async def test_relay_state_unknown_rejected(fake_redis):
    with pytest.raises(SSOValidationError):
        await consume_saml_relay_state("never-minted")


async def test_handoff_single_use(fake_redis):
    code = await create_saml_handoff("jwt-token", True, "acme")
    data = await consume_saml_handoff(code)
    assert data["access_token"] == "jwt-token"
    assert data["must_change_password"] is True
    assert data["tenant"] == "acme"
    with pytest.raises(SSOValidationError):
        await consume_saml_handoff(code)


async def test_replay_dedup_per_tenant(fake_redis):
    from app.services.webhook_security import is_event_already_processed

    # First sight of an assertion id is fresh; a replay is caught.
    assert await is_event_already_processed("saml:acme", "_a1") is False
    assert await is_event_already_processed("saml:acme", "_a1") is True
    # Same assertion id under a DIFFERENT tenant is still fresh (no cross-tenant
    # block — assertion ids are only unique within an issuer).
    assert await is_event_already_processed("saml:techflow", "_a1") is False


# ---------------------------------------------------------------------------
# An unresolvable-but-`enabled` block is a 400/404, never an uncaught 500
# (docs/followups.md "The public SSO entry points answer an unresolvable IdP
# block with a 500, not their documented 400 / 404")
# ---------------------------------------------------------------------------


def _broken_saml_org(slug: str = "acme") -> SimpleNamespace:
    """`enabled: true` but missing its required trust anchor — `resolve_saml_config`
    raises `SSOConfigError` for this shape, not `None`."""
    return SimpleNamespace(
        slug=slug,
        settings={
            "sso": {
                "enabled": True,
                "protocol": "saml",
                "provider": "saml",
                "idp_entity_id": IDP,
                "idp_sso_url": "https://idp.test/sso",
                # idp_x509_cert deliberately omitted -> SSOConfigError
            }
        },
    )


class _FakeSAMLPostRequest:
    """Just enough of `fastapi.Request` for `saml_acs`: a client IP, empty
    headers, and an async `.form()`."""

    def __init__(self, saml_response: str = "irrelevant", relay_state: str = "rs-1"):
        self.client = SimpleNamespace(host="1.2.3.4")
        self.headers: dict[str, str] = {}
        self._form = {"SAMLResponse": saml_response, "RelayState": relay_state}

    async def form(self):
        return self._form


def test_resolve_saml_or_none_absorbs_config_error():
    """The helper the five public routes call: `SSOConfigError` -> `None`,
    exactly like a genuinely absent block."""
    # Sanity: the block really does raise, not resolve to None on its own.
    with pytest.raises(SSOConfigError):
        auth_saml.resolve_saml_config(_broken_saml_org().settings, "acme")
    assert auth_saml._resolve_saml_or_none(_broken_saml_org().settings, "acme") is None


async def test_saml_login_broken_config_is_400_not_500(monkeypatch):
    async def _resolve_slug(slug, host, db):
        return slug

    async def _fetch_org(slug, db):
        return _broken_saml_org()

    monkeypatch.setattr(auth_saml, "resolve_sso_tenant_slug", _resolve_slug)
    monkeypatch.setattr(auth_saml, "_fetch_org_by_slug", _fetch_org)

    with pytest.raises(HTTPException) as exc:
        await auth_saml.saml_login(slug="acme", host=None, db=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "SAML SSO is not configured for this tenant."
    # The public response never names the offending key.
    assert "idp_x509_cert" not in exc.value.detail


async def test_saml_acs_broken_config_is_400_not_500(monkeypatch):
    async def _consume_relay_state(state):
        return {"tenant": "acme", "request_id": REQUEST_ID}

    async def _fetch_org(slug, db):
        return _broken_saml_org()

    monkeypatch.setattr(auth_saml, "consume_saml_relay_state", _consume_relay_state)
    monkeypatch.setattr(auth_saml, "_fetch_org_by_slug", _fetch_org)

    with pytest.raises(HTTPException) as exc:
        await auth_saml.saml_acs(_FakeSAMLPostRequest(), db=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "SAML SSO is not configured for this tenant."
    assert "idp_x509_cert" not in exc.value.detail


async def test_saml_metadata_broken_config_is_404_not_500(monkeypatch):
    async def _fetch_org(slug, db):
        return _broken_saml_org()

    monkeypatch.setattr(auth_saml, "_fetch_org_by_slug", _fetch_org)

    with pytest.raises(HTTPException) as exc:
        await auth_saml.saml_metadata(slug="acme", db=None)
    assert exc.value.status_code == 404
    assert exc.value.detail == "SAML SSO is not configured for this tenant."
    assert "idp_x509_cert" not in exc.value.detail
