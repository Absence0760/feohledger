"""Route-level wiring tests for the SAML ACS / exchange handlers.

The crypto verification lives in test_saml_security.py; this drives the actual
FastAPI routes (in-process, no DB / no Keycloak) to verify the handler GLUE:
RelayState-derived tenant, the 303 -> bridge -> one-time-code -> /exchange token
handoff, and — critically — that every failure path writes a PII-safe
auth.saml.login.failure audit row and lands on the SPA bridge with a coded
refusal (decisions §359): one generic `saml_login_unverified` for every
verification failure, so the bridge says no more than the ACS used to.

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


async def _post_acs(client, response_xml: str, relay_state: str = "st-int-1"):
    import base64

    b64 = base64.b64encode(response_xml.encode()).decode()
    return await client.post(
        "/api/auth/saml/acs",
        data={"SAMLResponse": b64, "RelayState": relay_state},
        follow_redirects=False,
    )


async def _refusal_on_the_bridge(client, acs) -> tuple[int, dict]:
    """A refused ACS POST: a 303 to the tenant's SPA bridge carrying a one-time
    code (never JSON at the ACS), which `/exchange` answers with the coded
    refusal and no token — once."""
    assert acs.status_code == 303
    loc = acs.headers["location"]
    assert loc.startswith("http://acme.localhost:7777/login/saml-callback?code=")
    code = loc.split("code=", 1)[1]
    ex = await client.post("/api/auth/saml/exchange", json={"code": code})
    assert "access_token" not in ex.json()
    again = await client.post("/api/auth/saml/exchange", json={"code": code})
    assert again.status_code == 400
    return ex.status_code, ex.json()["detail"]


def _failure_reasons(saml_wired) -> list[str]:
    assert not [a for a in saml_wired["audits"] if a["action"] == "auth.saml.login.success"]
    failures = [a for a in saml_wired["audits"] if a["action"] == "auth.saml.login.failure"]
    for row in failures:
        # PII-safe: reason code only, no email / NameID / raw assertion.
        assert set(row["details"]) <= {"tenant", "ip", "reason"}
    return [row["details"]["reason"] for row in failures]


UNVERIFIED = {
    "code": "saml_login_unverified",
    "message": "SAML login could not be verified.",
    "params": {},
}


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
        assert await _refusal_on_the_bridge(client, acs) == (400, UNVERIFIED)

    assert _failure_reasons(saml_wired) == ["assertion_invalid"]


async def test_acs_unknown_relaystate_is_generic_400(saml_wired):
    # No RelayState seeded — tenant can't be recovered, so it fails generically
    # WITHOUT an org-scoped audit row (nothing to scope it to, like OIDC), and
    # stays a 400 at the ACS: there is no workspace origin to send it to.
    async with _client() as client:
        acs = await _post_acs(client, _signed_response(saml_wired["keypair"]))
        assert acs.status_code == 400
    assert saml_wired["audits"] == []


@pytest.mark.parametrize(
    ("assertion_kwargs", "reason"),
    [
        # A tampered or foreign-signed document: python3-saml rejects it.
        ({"audience": "https://someone-else.test/metadata"}, "assertion_invalid"),
        # Signed by the pinned key, but the Issuer is not this workspace's IdP:
        # python3-saml refuses it before the handler's own pin is reached
        # (both layers: test_saml_security.test_wrong_issuer_rejected_at_both_layers).
        ({"issuer": "https://other-idp.test/metadata"}, "assertion_invalid"),
        # Verified, but names no subject / address to sign in as.
        ({"nameid": "opaque-subject-1", "email": ""}, "missing_email"),
    ],
)
async def test_acs_verification_failures_are_one_refusal_on_the_bridge(
    saml_wired, assertion_kwargs, reason
):
    """Every verification failure is stated identically on the bridge — the
    same code, status and sentence — so whoever holds a forged or captured
    assertion learns nothing about which check stopped it. Only the audit row
    tells them apart, unchanged."""
    await store_saml_relay_state("st-int-1", "acme", REQUEST_ID)
    async with _client() as client:
        acs = await _post_acs(client, _signed_response(saml_wired["keypair"], **assertion_kwargs))
        assert await _refusal_on_the_bridge(client, acs) == (400, UNVERIFIED)
    assert _failure_reasons(saml_wired) == [reason]


async def test_acs_unsolicited_response_is_the_one_refusal(saml_wired):
    """A response with no InResponseTo (IdP-initiated / unsolicited)."""
    from tests.test_saml_security import _assertion_xml, _response, _sign

    key_pem, cert_pem, _ = saml_wired["keypair"]
    unsolicited = _response(
        _sign(_assertion_xml(in_response_to=None), key_pem, cert_pem), in_response_to=None
    )
    await store_saml_relay_state("st-int-1", "acme", REQUEST_ID)
    async with _client() as client:
        acs = await _post_acs(client, unsolicited)
        assert await _refusal_on_the_bridge(client, acs) == (400, UNVERIFIED)
    assert _failure_reasons(saml_wired) == ["unsolicited"]


async def test_acs_replayed_assertion_is_the_one_refusal(saml_wired):
    """The same signed response a second time, under a fresh RelayState bound to
    the same request: the first signs in, the replay is refused on the bridge
    exactly as a forged signature is."""
    response = _signed_response(saml_wired["keypair"])
    await store_saml_relay_state("st-int-1", "acme", REQUEST_ID)
    await store_saml_relay_state("st-int-2", "acme", REQUEST_ID)
    async with _client() as client:
        first = await _post_acs(client, response, "st-int-1")
        assert first.status_code == 303
        replay = await _post_acs(client, response, "st-int-2")
        assert await _refusal_on_the_bridge(client, replay) == (400, UNVERIFIED)
    failures = [a for a in saml_wired["audits"] if a["action"] == "auth.saml.login.failure"]
    assert [row["details"]["reason"] for row in failures] == ["replay"]
    assert set(failures[0]["details"]) <= {"tenant", "ip", "reason"}


async def test_acs_session_without_request_binding_is_the_one_refusal(saml_wired):
    """Server-minted state that never bound an AuthnRequest: refused before
    python3-saml could skip InResponseTo, unaudited as before."""
    await store_saml_relay_state("st-int-1", "acme", "")
    async with _client() as client:
        acs = await _post_acs(client, _signed_response(saml_wired["keypair"]))
        assert await _refusal_on_the_bridge(client, acs) == (400, UNVERIFIED)
    assert saml_wired["audits"] == []


async def test_acs_unconfigured_since_login_is_stated_on_the_bridge(saml_wired, monkeypatch):
    """SAML switched off (or the plan lost `sso`) between /login and the ACS."""

    async def _no_sso(db, org):
        return {}

    monkeypatch.setattr(auth_saml, "sign_in_settings", _no_sso)
    await store_saml_relay_state("st-int-1", "acme", REQUEST_ID)
    async with _client() as client:
        acs = await _post_acs(client, _signed_response(saml_wired["keypair"]))
        status, detail = await _refusal_on_the_bridge(client, acs)
    assert status == 400
    assert detail["code"] == "saml_not_configured"
    assert saml_wired["audits"] == []


@pytest.mark.parametrize(
    ("raised", "reason", "code", "message"),
    [
        (
            "DeactivatedAccount",
            "inactive",
            "sso_account_inactive",
            "This account has been deactivated. Contact your administrator.",
        ),
        (
            "EmailDomainNotAllowed",
            "domain_blocked",
            "sso_email_domain_blocked",
            "Your email domain isn't allowed to sign in to this workspace.",
        ),
        (
            "UnsafeEmailAddress",
            "unsafe_email",
            "sso_email_unusable",
            "The identity provider supplied an unusable email address.",
        ),
    ],
)
async def test_acs_identity_refusals_are_coded_like_the_oidc_callback(
    saml_wired, monkeypatch, raised, reason, code, message
):
    """The refusals a person can act on are stated as the OIDC callback states
    them — the same code and English — with the audit reason unchanged."""
    import app.services.identity_provisioning as ip

    exc_type = getattr(ip, raised)

    def _make():
        if raised == "DeactivatedAccount":
            return exc_type(uuid.uuid4())
        return exc_type("user@acme.com")

    def _check_refuses(email_raw, allowed):
        if raised == "DeactivatedAccount":
            return email_raw
        raise _make()

    async def _jit_refuses(db, org_, email, sub, provider, claims):
        raise _make()

    monkeypatch.setattr(auth_saml, "extract_and_check_email", _check_refuses)
    monkeypatch.setattr(auth_saml, "jit_provision", _jit_refuses)
    await store_saml_relay_state("st-int-1", "acme", REQUEST_ID)
    async with _client() as client:
        acs = await _post_acs(client, _signed_response(saml_wired["keypair"]))
        status, detail = await _refusal_on_the_bridge(client, acs)
    assert status == 403
    assert detail == {"code": code, "message": message, "params": {}}
    assert _failure_reasons(saml_wired) == [reason]


async def test_exchange_answers_an_unknown_parked_refusal_as_unverified(saml_wired):
    """A refusal record this build does not know (written by a newer one) is
    never read as a token: fail closed to the one unverified refusal."""
    from app.services.sso import create_saml_refusal_handoff

    code = await create_saml_refusal_handoff("some_future_refusal", "acme")
    async with _client() as client:
        ex = await client.post("/api/auth/saml/exchange", json={"code": code})
    assert ex.status_code == 400
    assert ex.json()["detail"] == UNVERIFIED


async def test_acs_address_in_another_workspace_lands_on_the_bridge_coded(saml_wired, monkeypatch):
    """The asserted address names an account in another workspace (decisions
    §291). Not raw JSON at the ACS and not a 500: a 303 to the bridge with a
    one-time code, which `/exchange` answers with the coded 403 and no token,
    plus one PII-free failure row in THIS workspace."""
    from app.services.identity_provisioning import EmailInOtherWorkspace

    async def _jit_refuses(db, org_, email, sub, provider, claims):
        raise EmailInOtherWorkspace()

    monkeypatch.setattr(auth_saml, "jit_provision", _jit_refuses)
    await store_saml_relay_state("st-int-1", "acme", REQUEST_ID)
    async with _client() as client:
        acs = await _post_acs(client, _signed_response(saml_wired["keypair"]))
        assert acs.status_code == 303
        loc = acs.headers["location"]
        assert loc.startswith("http://acme.localhost:7777/login/saml-callback?code=")
        code = loc.split("code=", 1)[1]

        ex = await client.post("/api/auth/saml/exchange", json={"code": code})
        assert ex.status_code == 403
        detail = ex.json()["detail"]
        assert detail["code"] == "sso_email_in_other_workspace"
        assert detail["params"] == {}
        assert "access_token" not in ex.json()

        # Single-use, like a token handoff.
        again = await client.post("/api/auth/saml/exchange", json={"code": code})
        assert again.status_code == 400

    assert not [a for a in saml_wired["audits"] if a["action"] == "auth.saml.login.success"]
    failures = [a for a in saml_wired["audits"] if a["action"] == "auth.saml.login.failure"]
    assert len(failures) == 1
    assert failures[0]["details"]["reason"] == "sso_email_in_other_workspace"
    assert set(failures[0]["details"]) <= {"tenant", "ip", "reason"}
