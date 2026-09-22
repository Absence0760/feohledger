"""Unit tests for SAML config resolution + URL helpers (app/services/sso.py).

Deterministic, no IdP, no crypto — the assertion-signature security
properties live in test_saml_security.py. These cover the config-layer
contract: protocol discrimination, required-field + cert validation, the
per-tenant SP entityID / static ACS URL, and OIDC back-compat.
"""

from __future__ import annotations

import base64

import pytest

from app.services.sso import (
    SSOConfigError,
    resolve_saml_config,
    resolve_sso_config,
    saml_acs_url,
    saml_sp_entity_id,
)

# A syntactically-valid base64 cert body (content irrelevant at the config
# layer — only base64 validity is checked here; real signing happens in the
# security tests).
VALID_CERT_B64 = base64.b64encode(b"fake-but-valid-base64-der-bytes").decode()


def _saml_settings(**overrides) -> dict:
    sso_block = {
        "enabled": True,
        "protocol": "saml",
        "provider": "saml",
        "idp_entity_id": "https://idp.example.com/saml",
        "idp_sso_url": "https://idp.example.com/saml/sso",
        "idp_x509_cert": VALID_CERT_B64,
        "allowed_email_domains": ["acme.com"],
    }
    sso_block.update(overrides)
    return {"sso": sso_block}


# --- resolve_saml_config: returns None for non-SAML tenants -----------------


@pytest.mark.parametrize(
    "settings_dict",
    [
        None,
        {},
        {"sso": {}},
        {"sso": {"enabled": False, "protocol": "saml"}},
        {"sso": {"enabled": True, "protocol": "oidc"}},
        {"sso": {"enabled": True}},  # protocol absent => OIDC default
    ],
)
def test_resolve_saml_returns_none_for_non_saml(settings_dict):
    assert resolve_saml_config(settings_dict, "acme") is None


# --- resolve_saml_config: raises on incomplete / bad trust config -----------


@pytest.mark.parametrize("missing", ["idp_entity_id", "idp_sso_url", "idp_x509_cert"])
def test_resolve_saml_raises_when_required_field_missing(missing):
    settings_dict = _saml_settings()
    del settings_dict["sso"][missing]
    with pytest.raises(SSOConfigError) as exc:
        resolve_saml_config(settings_dict, "acme")
    # Names exactly the key at fault, which `PATCH /api/organization` puts in
    # its 422 (docs/decisions.md §204).
    assert exc.value.fields == (missing,)


@pytest.mark.parametrize("bad_url", ["not-a-url", "ftp://idp.example.com/sso", "https://"])
def test_resolve_saml_raises_when_the_sso_url_is_not_an_http_url(bad_url):
    # The login handler 302s to this. A value it would refuse must not resolve,
    # or the login page would show a SAML button that cannot work.
    with pytest.raises(SSOConfigError) as exc:
        resolve_saml_config(_saml_settings(idp_sso_url=bad_url), "acme")
    assert exc.value.fields == ("idp_sso_url",)


@pytest.mark.parametrize(
    "bad_cert",
    ["", "   ", "not!base64!!!", "-----BEGIN CERTIFICATE-----\n\n-----END CERTIFICATE-----"],
)
def test_resolve_saml_raises_on_empty_or_malformed_cert(bad_cert):
    # An empty / blank / garbage cert must fail closed — it can NEVER reach
    # python3-saml as "no cert => skip signature verification".
    settings_dict = _saml_settings(idp_x509_cert=bad_cert)
    with pytest.raises(SSOConfigError) as exc:
        resolve_saml_config(settings_dict, "acme")
    assert exc.value.fields == ("idp_x509_cert",)


# --- resolve_saml_config: full happy path -----------------------------------


def test_resolve_saml_full_config():
    cfg = resolve_saml_config(_saml_settings(), "acme")
    assert cfg is not None
    assert cfg.provider == "saml"
    assert cfg.idp_entity_id == "https://idp.example.com/saml"
    assert cfg.idp_sso_url == "https://idp.example.com/saml/sso"
    assert cfg.idp_x509_cert == VALID_CERT_B64
    assert cfg.allowed_email_domains == ["acme.com"]
    # sp_entity_id defaults to the per-tenant value when not overridden
    assert cfg.sp_entity_id == saml_sp_entity_id("acme")


def test_resolve_saml_strips_pem_armor():
    pem = f"-----BEGIN CERTIFICATE-----\n{VALID_CERT_B64}\n-----END CERTIFICATE-----"
    cfg = resolve_saml_config(_saml_settings(idp_x509_cert=pem), "acme")
    assert cfg is not None
    assert cfg.idp_x509_cert == VALID_CERT_B64  # armor + whitespace stripped


def test_resolve_saml_provider_defaults_to_saml():
    settings_dict = _saml_settings()
    del settings_dict["sso"]["provider"]
    cfg = resolve_saml_config(settings_dict, "acme")
    assert cfg is not None and cfg.provider == "saml"


def test_resolve_saml_sp_entity_id_override():
    cfg = resolve_saml_config(_saml_settings(sp_entity_id="urn:custom:sp"), "acme")
    assert cfg is not None and cfg.sp_entity_id == "urn:custom:sp"


def test_resolve_saml_cert_multi_for_rotation():
    other = base64.b64encode(b"second-rotation-cert").decode()
    cfg = resolve_saml_config(_saml_settings(idp_x509_cert_multi=[other]), "acme")
    assert cfg is not None and cfg.idp_x509_cert_multi == [other]


# --- URL helpers ------------------------------------------------------------


def test_saml_sp_entity_id_is_per_tenant():
    assert saml_sp_entity_id("acme") != saml_sp_entity_id("techflow")
    assert saml_sp_entity_id("acme").endswith("/acme")


def test_saml_acs_url_is_static_backend_url():
    # One ACS for every tenant — tenant comes from RelayState, not this URL.
    url = saml_acs_url()
    assert url.endswith("/api/auth/saml/acs")
    assert url == saml_acs_url()  # stable


# --- OIDC back-compat: a SAML tenant must not resolve as OIDC ---------------


def test_oidc_resolver_returns_none_for_saml_tenant():
    assert resolve_sso_config(_saml_settings()) is None


def test_oidc_resolver_unchanged_when_protocol_absent():
    # protocol absent => OIDC path, exactly as before this change.
    oidc = {
        "sso": {
            "enabled": True,
            "provider": "okta",
            "discovery_url": "https://acme.okta.com/.well-known/openid-configuration",
            "client_id": "cid",
            "client_secret": "secret",
        }
    }
    cfg = resolve_sso_config(oidc)
    assert cfg is not None and cfg.provider == "okta"
    # ...and the SAML resolver declines it.
    assert resolve_saml_config(oidc, "acme") is None
