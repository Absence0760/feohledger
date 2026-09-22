"""Unit tests for SSO + SCIM pure-function surfaces.

Covers config resolution, redirect-URI construction, SCIM filter parsing,
SCIM schema shapes. The full OIDC handshake (state → Redis → callback)
needs an integration harness with a real Redis + a fake IdP, which lives
separately.
"""

import pytest

# ---------- SSO config resolution -----------------------------------------


def test_resolve_sso_config_returns_none_when_missing():
    from app.services.sso import resolve_sso_config

    assert resolve_sso_config(None) is None
    assert resolve_sso_config({}) is None
    assert resolve_sso_config({"sso": {"enabled": False}}) is None


def test_resolve_sso_config_returns_none_when_disabled():
    from app.services.sso import resolve_sso_config

    cfg = {
        "sso": {
            "enabled": False,
            "discovery_url": "https://x.okta.com/.well-known/openid-configuration",
            "client_id": "a",
            "client_secret": "b",
        }
    }
    assert resolve_sso_config(cfg) is None


def test_resolve_sso_config_raises_when_partial():
    from app.services.sso import SSOConfigError, resolve_sso_config

    with pytest.raises(SSOConfigError) as exc:
        resolve_sso_config(
            {"sso": {"enabled": True, "client_id": "a", "client_secret": "must-not-echo"}}
        )
    # Every absent key, named, and nothing from the block's values: the message
    # and `fields` both reach `PATCH /api/organization`'s 422 (decisions §204).
    assert exc.value.fields == ("discovery_url",)
    assert "must-not-echo" not in str(exc.value)


def test_resolve_sso_config_returns_full_config():
    from app.services.sso import resolve_sso_config

    cfg = {
        "sso": {
            "enabled": True,
            "provider": "okta",
            "discovery_url": "https://acme.okta.com/.well-known/openid-configuration",
            "client_id": "cid",
            "client_secret": "sec",
            "allowed_email_domains": ["acme.com"],
        }
    }
    resolved = resolve_sso_config(cfg)
    assert resolved is not None
    assert resolved.provider == "okta"
    assert resolved.client_id == "cid"
    assert resolved.allowed_email_domains == ["acme.com"]


# ---------- Per-tenant redirect URI ---------------------------------------


def test_redirect_uri_is_per_tenant():
    """Each tenant gets its own callback URL built from the tenant URL
    template — critical so the callback lands on the tenant origin."""
    from app.services.sso import redirect_uri

    acme = redirect_uri("acme")
    techflow = redirect_uri("techflow")
    assert acme != techflow
    assert "acme" in acme
    assert "techflow" in techflow
    assert acme.endswith("/login/sso-callback")


# ---------- SCIM token hashing --------------------------------------------


def test_scim_token_hashing_is_deterministic():
    from app.services.sso import generate_scim_token, hash_scim_token

    raw, digest = generate_scim_token()
    assert hash_scim_token(raw) == digest
    assert len(digest) == 64  # sha256 hex


def test_scim_token_generation_is_unique():
    from app.services.sso import generate_scim_token

    a_raw, a_digest = generate_scim_token()
    b_raw, b_digest = generate_scim_token()
    assert a_raw != b_raw
    assert a_digest != b_digest


# ---------- SCIM filter parser --------------------------------------------


def test_scim_filter_supports_expected_attributes():
    """Lock the filter surface Okta + Entra actually use."""
    from sqlalchemy import select

    from app.api.scim import _apply_filter
    from app.models.user import User

    base = select(User)
    # Each of these should NOT raise
    _apply_filter(base, 'userName eq "a@b.com"')
    _apply_filter(base, 'emails eq "a@b.com"')
    _apply_filter(base, 'externalId eq "abc123"')
    _apply_filter(base, "active eq true")
    _apply_filter(base, "active eq false")


def test_scim_filter_rejects_unsupported():
    """Unsupported filters must raise a SCIM-compliant 400 (invalidFilter)."""
    from fastapi import HTTPException
    from sqlalchemy import select

    from app.api.scim import _apply_filter
    from app.models.user import User

    base = select(User)
    with pytest.raises(HTTPException) as exc:
        _apply_filter(base, 'phoneNumber eq "555-0100"')
    assert exc.value.status_code == 400


# ---------- SCIM schema contracts -----------------------------------------


def test_scim_user_schema_required_fields():
    """Lock the outbound SCIM User schema — IdPs validate against this."""
    from app.schemas.scim import USER_SCHEMA, SCIMUser

    user = SCIMUser(
        id="uuid-123",
        userName="x@example.com",
    )
    data = user.model_dump()
    assert USER_SCHEMA in data["schemas"]
    assert data["userName"] == "x@example.com"
    # active defaults to True per SCIM convention
    assert data["active"] is True


def test_scim_list_response_envelope():
    """/Users GET wraps results in a ListResponse — not a bare array."""
    from app.schemas.scim import LIST_RESPONSE_SCHEMA, SCIMListResponse, SCIMUser

    resp = SCIMListResponse(
        totalResults=1,
        itemsPerPage=1,
        Resources=[SCIMUser(id="u1", userName="x@y.com")],
    )
    data = resp.model_dump()
    assert LIST_RESPONSE_SCHEMA in data["schemas"]
    assert data["totalResults"] == 1
    assert data["startIndex"] == 1
    assert isinstance(data["Resources"], list)


def test_scim_error_envelope():
    from app.schemas.scim import ERROR_SCHEMA, SCIMError

    err = SCIMError(status="404", detail="not found")
    data = err.model_dump()
    assert ERROR_SCHEMA in data["schemas"]
    assert data["status"] == "404"


def test_scim_patch_op_shape():
    from app.schemas.scim import SCIMPatchOp, SCIMPatchRequest

    req = SCIMPatchRequest(Operations=[SCIMPatchOp(op="replace", path="active", value=False)])
    assert req.Operations[0].op == "replace"
    assert req.Operations[0].path == "active"


# ---------- auth-sso config endpoint shape --------------------------------


def test_sso_config_public_omits_secrets():
    """The unauthenticated /api/auth/sso/config endpoint must NEVER leak
    client_secret or the SCIM bearer — it's reachable without auth."""
    from app.api.auth_sso import SSOConfigPublic

    fields = set(SSOConfigPublic.model_fields.keys())
    # Allowed fields only (sso_only is a non-secret policy flag for the login UI)
    assert fields == {"enabled", "provider", "sso_only"}


# ---------- redirect_uri threaded through the OIDC handshake --------------
#
# Regression guard: an earlier draft of build_authorize_url and
# exchange_code_for_tokens dropped the tenant slug, so they fell back to a
# zero-arg redirect_uri() call that crashed at runtime. Lock down that the
# slug is threaded through and the same URI is sent in both legs of the
# handshake (OIDC token exchange validates redirect_uri matches authorize).


async def test_sso_authorize_uses_per_tenant_redirect_uri(monkeypatch):
    """The authorize leg is `api/auth_sso.py::sso_authorize`, not a helper in
    `services/sso` — the helper that used to live there read the discovery
    doc's `authorization_endpoint` verbatim, was reached by nothing but this
    test, and was deleted. Assert against the path that actually runs."""
    from app.api import auth_sso

    class _Org:
        slug = "acme"
        settings = {
            "sso": {
                "enabled": True,
                "discovery_url": "https://idp.example/.well-known/openid-configuration",
                "client_id": "client-123",
                "client_secret": "sec",
            }
        }

    async def _fake_org(slug, db):
        return _Org()

    async def _fake_discovery(url):
        return {
            "issuer": "https://idp.example",
            "authorization_endpoint": "https://idp.example/authorize",
        }

    async def _fake_state(slug):
        return "state-x", "nonce-y"

    monkeypatch.setattr(auth_sso, "_fetch_org_by_slug", _fake_org)
    monkeypatch.setattr(auth_sso, "fetch_discovery", _fake_discovery)
    monkeypatch.setattr(auth_sso, "create_state", _fake_state)

    resp = await auth_sso.sso_authorize(slug="acme", db=None)
    url = resp.headers["location"]

    assert url.startswith("https://idp.example/authorize?")
    from urllib.parse import quote

    from app.services.sso import redirect_uri

    assert quote(redirect_uri("acme"), safe="") in url
    assert "state=state-x" in url
    assert "nonce=nonce-y" in url
    assert "client_id=client-123" in url


async def test_sso_authorize_refuses_an_authorize_host_off_the_configured_idp(monkeypatch):
    """The pinning the deleted helper did NOT do: a mis-served discovery
    document cannot pivot the browser redirect to another host."""
    from fastapi import HTTPException

    from app.api import auth_sso

    class _Org:
        slug = "acme"
        settings = {
            "sso": {
                "enabled": True,
                "discovery_url": "https://idp.example/.well-known/openid-configuration",
                "client_id": "client-123",
                "client_secret": "sec",
            }
        }

    async def _fake_org(slug, db):
        return _Org()

    async def _evil_discovery(url):
        return {
            "issuer": "https://idp.example",
            "authorization_endpoint": "https://attacker.example/authorize",
        }

    async def _fake_state(slug):
        return "state-x", "nonce-y"

    monkeypatch.setattr(auth_sso, "_fetch_org_by_slug", _fake_org)
    monkeypatch.setattr(auth_sso, "fetch_discovery", _evil_discovery)
    monkeypatch.setattr(auth_sso, "create_state", _fake_state)

    with pytest.raises(HTTPException) as exc:
        await auth_sso.sso_authorize(slug="acme", db=None)
    assert exc.value.status_code == 400
    # Opaque to the caller — it must not echo the attacker host back.
    assert "attacker.example" not in str(exc.value.detail)


async def test_exchange_code_posts_matching_redirect_uri(monkeypatch):
    """OIDC requires the token-exchange redirect_uri to exactly equal the one
    sent during authorize. Fake out httpx to capture the POST body and assert
    redirect_uri matches what build_authorize_url would send."""
    from app.services import sso as sso_module

    captured: dict = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"id_token": "fake", "access_token": "fake"}

    class _FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, data, headers):
            captured["url"] = url
            captured["data"] = data
            return _FakeResp()

    monkeypatch.setattr(sso_module.httpx, "AsyncClient", _FakeClient)

    # `issuer` is required now: the token endpoint is pinned to the discovery
    # document's own host before the POST (SSRF / endpoint-redirection guard),
    # so a document without an anchor is refused. It has always been present in
    # a real OIDC discovery document — this fixture just omitted it.
    discovery = {
        "issuer": "https://idp.example",
        "token_endpoint": "https://idp.example/token",
    }
    await sso_module.exchange_code_for_tokens(
        discovery, "client-id", "client-secret", "auth-code", "acme"
    )

    assert captured["url"] == "https://idp.example/token"
    assert captured["data"]["redirect_uri"] == sso_module.redirect_uri("acme")
    assert captured["data"]["code"] == "auth-code"
    assert captured["data"]["grant_type"] == "authorization_code"


# ---------- SCIM token mint endpoint --------------------------------------


def test_scim_token_response_shape_omits_hash():
    """The mint endpoint must return the plaintext token (so the admin can
    paste it into the IdP) but never the stored hash itself — only a short
    prefix for UI identification."""
    from app.api.organization import SCIMTokenResponse

    fields = set(SCIMTokenResponse.model_fields.keys())
    assert fields == {"token", "bearer_hash_prefix"}


def test_scim_token_hash_has_exactly_one_owner():
    """Drift guard. `hash_scim_token` is the single spelling of the SCIM
    bearer-token digest; mint and verify are two halves of one credential
    check, so a module that re-inlines `sha256(...).hexdigest()` for a token
    can drift from the other half and lock every tenant out of SCIM with no
    failing test. Scoped to the two modules that own this credential."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    owner = root / "services" / "sso.py"

    offenders = []
    for path in (root / "api" / "scim.py", owner):
        text = path.read_text()
        hits = text.count('hashlib.sha256(raw.encode("utf-8")).hexdigest()')
        hits += text.count('hashlib.sha256(token.encode("utf-8")).hexdigest()')
        # The owner is allowed exactly one — its own body.
        allowed = 1 if path == owner else 0
        if hits > allowed:
            offenders.append(f"{path.name}: {hits} inline digest(s), allowed {allowed}")

    assert not offenders, "SCIM token digest re-inlined outside hash_scim_token: " + "; ".join(
        offenders
    )
