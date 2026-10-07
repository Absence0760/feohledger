"""Route-level wiring tests for the SAML ACS / exchange handlers.

The crypto verification lives in test_saml_security.py; this drives the actual
FastAPI routes (in-process, no DB / no Keycloak) to verify the handler GLUE:
RelayState-derived tenant, the 303 -> bridge -> one-time-code -> /exchange token
handoff, and — critically — that every failure path writes a PII-safe
auth.saml.login.failure audit row and returns one generic error.

Collaborators that need a real DB (org fetch, jit_provision) are monkeypatched;
resolve_saml_config + the full python3-saml verification run for real.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
import pytest

import app.api.auth_saml as auth_saml
from app.database import get_control_db
from app.main import app
from app.services.sso import store_saml_relay_state
from tests.test_saml_security import (
    IDP,
    REQUEST_ID,
    SP,
    _signed_response,
)

# Plan-gated surface (docs/decisions.md §253/§258): realdb tests run with the
# harness orgs on a plan granting it; DB-free tests answer the plan lookup via
# the `all_plan_features` fixture.
pytestmark = pytest.mark.usefixtures("all_plan_features")


@pytest.fixture(scope="module")
def idp_keypair():
    """RSA key + self-signed cert for the synthetic IdP (mirrors the security
    suite's fixture; local because pytest fixtures aren't importable)."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

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


@dataclass
class _FakeOrg:
    id: uuid.UUID
    slug: str
    settings: dict


@dataclass
class _FakeUser:
    id: uuid.UUID
    organization_id: uuid.UUID
    must_change_password: bool = False


@pytest.fixture
def saml_wired(monkeypatch, idp_keypair):
    """Wire the ACS route against a synthetic SAML-configured tenant.

    Patches app.services.sso.get_redis (NOT covered by the autouse fixture,
    which only patches webhook_security + app.redis) so RelayState + handoff
    share one in-memory store, and stubs the DB-backed collaborators.
    """
    from tests.conftest import _FakeRedis

    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.sso.get_redis", _get_redis)

    _, _, cert_b64 = idp_keypair
    org = _FakeOrg(
        id=uuid.uuid4(),
        slug="acme",
        settings={
            "sso": {
                "enabled": True,
                "protocol": "saml",
                "provider": "saml",
                "idp_entity_id": IDP,
                "idp_sso_url": "https://idp.test/sso",
                "idp_x509_cert": cert_b64,
                "sp_entity_id": SP,
            }
        },
    )

    async def _fetch_org(slug, db):
        return org

    async def _jit(db, org_, email, sub, provider, claims):
        return _FakeUser(id=uuid.uuid4(), organization_id=org_.id)

    async def _register(user_id, jti, **kwargs):
        return None

    audits: list[dict] = []

    async def _audit(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(auth_saml, "_fetch_org_by_slug", _fetch_org)
    monkeypatch.setattr(auth_saml, "jit_provision", _jit)
    monkeypatch.setattr(auth_saml, "register_session", _register)
    monkeypatch.setattr(auth_saml, "dispatch_auth_audit", _audit)

    async def _fake_db():
        yield None

    app.dependency_overrides[get_control_db] = _fake_db
    yield {"audits": audits, "keypair": idp_keypair, "cert_b64": cert_b64}
    app.dependency_overrides.pop(get_control_db, None)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    )


async def _post_acs(client, response_xml: str):
    import base64

    b64 = base64.b64encode(response_xml.encode()).decode()
    return await client.post(
        "/api/auth/saml/acs",
        data={"SAMLResponse": b64, "RelayState": "st-int-1"},
        follow_redirects=False,
    )


async def test_acs_happy_path_303_then_exchange(saml_wired):
    # Seed a RelayState bound to {tenant, request_id} as /login would have.
    await store_saml_relay_state("st-int-1", "acme", REQUEST_ID)
    async with _client() as client:
        acs = await _post_acs(client, _signed_response(saml_wired["keypair"]))
        # 303 to the per-tenant SPA bridge with a one-time ?code= (no token in URL)
        assert acs.status_code == 303
        loc = acs.headers["location"]
        assert loc.startswith("http://acme.localhost:7777/login/saml-callback?code=")
        assert "token" not in loc
        code = loc.split("code=", 1)[1]

        # Exchange the code for the JWT — returned in the BODY, not a URL.
        ex = await client.post("/api/auth/saml/exchange", json={"code": code})
        assert ex.status_code == 200
        body = ex.json()
        assert body["access_token"]
        assert body["tenant_slug"] == "acme"

        # ...and the code is single-use.
        ex2 = await client.post("/api/auth/saml/exchange", json={"code": code})
        assert ex2.status_code == 400

    # Success wrote exactly one success audit, no PII.
    successes = [a for a in saml_wired["audits"] if a["action"] == "auth.saml.login.success"]
    assert len(successes) == 1
    assert "email" not in successes[0]["details"]


async def test_acs_bad_signature_audits_failure_without_pii(saml_wired):
    await store_saml_relay_state("st-int-1", "acme", REQUEST_ID)
    # Build an attacker-signed response inline: a valid signature, but under a
    # key/cert that is NOT the tenant's pinned idp_x509_cert.
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "attacker")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subj)
        .issuer_name(subj)
        .public_key(key.public_key())
        .serial_number(9)
        .not_valid_before(datetime.datetime(2020, 1, 1))
        .not_valid_after(datetime.datetime(2035, 1, 1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cert_b64 = "".join(ln for ln in cert_pem.splitlines() if "---" not in ln)

    forged = _signed_response((key_pem, cert_pem, cert_b64))
    async with _client() as client:
        acs = await _post_acs(client, forged)
        assert acs.status_code == 400
        assert acs.json()["detail"] == "SAML login could not be verified."

    failures = [a for a in saml_wired["audits"] if a["action"] == "auth.saml.login.failure"]
    assert len(failures) == 1
    details = failures[0]["details"]
    # PII-safe: reason code only, no email / NameID / raw assertion.
    assert details["reason"] in {"assertion_invalid", "issuer_mismatch", "unsolicited"}
    assert "email" not in details
    assert set(details) <= {"tenant", "ip", "reason"}


async def test_acs_unknown_relaystate_is_generic_400(saml_wired):
    # No RelayState seeded — tenant can't be recovered, so it fails generically
    # WITHOUT an org-scoped audit row (nothing to scope it to, like OIDC).
    async with _client() as client:
        acs = await _post_acs(client, _signed_response(saml_wired["keypair"]))
        assert acs.status_code == 400
    assert saml_wired["audits"] == []
