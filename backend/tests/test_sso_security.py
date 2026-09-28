"""SSO security-property tests — OIDC CSRF / replay protection.

`test_sso_scim.py` covers config resolution + token shape; these tests
pin the security gates between the IdP redirect and the JWT mint.

State + nonce together close the two OIDC replay attacks:
  - State: a 24-byte random string bound to the user's pending login.
    Single-use, expires in `sso_state_ttl_seconds`. Without it, an
    attacker can forge a callback request and land authenticated as
    themselves on the victim's session (CSRF).
  - Nonce: bound to the ID token. Validates that the token the IdP
    returned matches the request we made. Without it, an attacker
    can replay a captured ID token from a different session.

Tests:
  - State is single-use (consume_state deletes the binding)
  - Unknown / tampered state raises SSOValidationError
  - State / nonce are URL-safe and long enough to resist brute force
  - State carries the tenant binding so a leaked state on tenant A
    can't redirect into tenant B
  - validate_id_token rejects a token whose nonce doesn't match
  - SCIM tokens are hashed at rest (sha256), never plaintext
  - SCIM tokens are unique across mintings
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, tuple[float, bytes]] = {}

    async def setex(self, key, ttl, value):
        expiry = time.time() + ttl
        self.store[key] = (
            expiry,
            value if isinstance(value, bytes) else value.encode("utf-8"),
        )

    async def get(self, key):
        item = self.store.get(key)
        if not item:
            return None
        expiry, value = item
        if expiry < time.time():
            del self.store[key]
            return None
        return value

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.sso.get_redis", _get_redis)
    return fake


# ---------------------------------------------------------------------------
# State binding — CSRF protection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_is_single_use(fake_redis):
    """`consume_state` must delete the binding so a second callback
    with the same state fails. Otherwise an attacker who intercepts
    the redirect URL can replay it indefinitely."""
    from app.services import sso

    state, _nonce = await sso.create_state("acme")

    bound = await sso.consume_state(state)
    assert bound["tenant"] == "acme"

    with pytest.raises(sso.SSOValidationError):
        await sso.consume_state(state)


@pytest.mark.asyncio
async def test_state_unknown_value_is_rejected(fake_redis):
    """A state value we never minted must raise — not return an empty
    dict or default to the seeded tenant. A regression that
    "auto-creates" a binding on lookup would defeat the gate."""
    from app.services import sso

    with pytest.raises(sso.SSOValidationError):
        await sso.consume_state("never-minted-state")


@pytest.mark.asyncio
async def test_state_carries_tenant_binding(fake_redis):
    """The state row must capture the tenant slug. Otherwise a
    callback could be redirected into a different tenant's SSO
    config — privilege escalation by mixed-tenant state."""
    from app.services import sso

    state, _nonce = await sso.create_state("techflow")
    bound = await sso.consume_state(state)
    assert bound["tenant"] == "techflow"


@pytest.mark.asyncio
async def test_state_carries_unique_nonce(fake_redis):
    """Every mint must produce a fresh nonce — otherwise two
    concurrent logins could share a nonce and an attacker who
    captured one could replay the other's ID token."""
    from app.services import sso

    s1, n1 = await sso.create_state("acme")
    s2, n2 = await sso.create_state("acme")
    assert s1 != s2
    assert n1 != n2


@pytest.mark.asyncio
async def test_state_and_nonce_are_url_safe_and_long_enough(fake_redis):
    """The values land in URL query strings — they must be URL-safe
    base64 (no padding, no `+`, no `/`). And they must carry enough
    entropy that an attacker can't brute-force the binding within
    the TTL window. 24 random bytes encoded ≈ 32 chars is the floor."""
    from app.services import sso

    state, nonce = await sso.create_state("acme")
    safe_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    for label, val in (("state", state), ("nonce", nonce)):
        assert len(val) >= 32, f"{label} too short: {len(val)} chars"
        assert set(val) <= safe_chars, f"{label} contains non-URL-safe chars"


@pytest.mark.asyncio
async def test_state_expires_after_configured_ttl(fake_redis, monkeypatch):
    """A stale state — past `sso_state_ttl_seconds` — must be rejected
    just like an unknown one. Tamper with the stored expiry to
    simulate the TTL lapse."""
    from app.services import sso

    state, _ = await sso.create_state("acme")
    key = f"sso:state:{state}"
    expiry, value = fake_redis.store[key]
    fake_redis.store[key] = (time.time() - 1, value)  # already expired

    with pytest.raises(sso.SSOValidationError):
        await sso.consume_state(state)


@pytest.mark.asyncio
async def test_state_does_not_leak_the_tenant_via_predictable_format(fake_redis):
    """A regression where the state itself encoded the tenant slug
    (`state = f"{slug}-{random}"`) would let an attacker who saw a
    redirect URL identify the tenant without consuming the state.
    Check that the state value is opaque random bytes."""
    from app.services import sso

    state, _ = await sso.create_state("acme")
    # The slug must not appear anywhere in the state.
    assert "acme" not in state.lower()


# ---------------------------------------------------------------------------
# Stored payload integrity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_payload_carries_a_timestamp(fake_redis):
    """The serialized payload includes a `ts` field. Operators read
    it for incident response — a regression that dropped it would
    blind the post-incident timeline."""
    from app.services import sso

    state, _ = await sso.create_state("acme")
    raw = fake_redis.store[f"sso:state:{state}"][1].decode("utf-8")
    payload = json.loads(raw)
    assert "ts" in payload
    assert isinstance(payload["ts"], (int, float))


# ---------------------------------------------------------------------------
# SCIM bearer token — hash at rest
# ---------------------------------------------------------------------------


def test_scim_token_hash_is_sha256_hex():
    """The token shown to the admin is plaintext; the value stored in
    org settings must be a sha256 hex digest. A regression that
    persisted plaintext would mean a DB dump compromises every
    tenant's SCIM endpoint."""
    from app.services.sso import generate_scim_token

    raw, digest = generate_scim_token()
    assert raw != digest
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_scim_token_generation_is_unique():
    """Two consecutive mintings must produce different plaintexts
    (and therefore different hashes). Reuse would let an attacker who
    learned one token guess subsequent ones."""
    from app.services.sso import generate_scim_token

    seen = {generate_scim_token()[0] for _ in range(20)}
    assert len(seen) == 20, "scim token generator returned duplicates"


def test_scim_token_hash_is_deterministic_for_the_same_plaintext():
    """`hash_scim_token` must be deterministic so the verify path
    (comparing the request's bearer hash to the stored hash) is just
    a string comparison. If it weren't deterministic, verify would
    have to use the slow `verify` path or fail."""
    from app.services.sso import hash_scim_token

    plaintext = "abc-123-test-token"
    assert hash_scim_token(plaintext) == hash_scim_token(plaintext)


# ---------------------------------------------------------------------------
# ID-token signature + claims verification (joserfc)
#
# These exercise the real crypto path in `validate_id_token`: an RSA key is
# generated, its public half is served as the JWKS, and tokens are signed with
# the private half. So we cover signature verification, the standard OIDC claim
# checks (iss/aud/exp), the nonce gate, AND the algorithm-confusion defence —
# not just the nonce comparison in isolation.
# ---------------------------------------------------------------------------

_ISSUER = "https://idp.test"
_CLIENT_ID = "client-1"
_KID = "test-signing-key-1"
_DISCOVERY = {"jwks_uri": "https://idp.test/jwks", "issuer": _ISSUER}


@pytest.fixture
def signing_key():
    """A fresh RSA signing key whose public JWKS the IdP 'serves'."""
    return RSAKey.generate_key(2048, parameters={"kid": _KID}, private=True)


@pytest.fixture
def patch_jwks(monkeypatch, signing_key):
    """Point `fetch_jwks` at the public half of `signing_key`."""
    public_jwks = {"keys": [signing_key.as_dict(private=False)]}

    async def _fake_fetch_jwks(_url):
        return public_jwks

    monkeypatch.setattr("app.services.sso.fetch_jwks", _fake_fetch_jwks)
    return public_jwks


def _sign(signing_key, claims, *, alg="RS256", key=None):
    """Sign `claims` into a compact JWS the IdP would return as the id_token."""
    return jwt.encode({"alg": alg, "kid": _KID}, claims, key or signing_key)


def _claims(**overrides):
    base = {
        "iss": _ISSUER,
        "aud": _CLIENT_ID,
        "exp": int(time.time()) + 300,
        "email": "user@x.test",
        "nonce": "the-real-nonce",
    }
    base.update(overrides)
    return base


async def _validate(token, *, expected_nonce="the-real-nonce"):
    from app.services import sso

    return await sso.validate_id_token(
        id_token=token,
        discovery_doc=_DISCOVERY,
        client_id=_CLIENT_ID,
        expected_nonce=expected_nonce,
    )


@pytest.mark.asyncio
async def test_id_token_validate_accepts_valid_token(patch_jwks, signing_key):
    """Positive control — a correctly signed token with matching
    iss/aud/exp/nonce returns the decoded claims. Without this the
    rejection tests below could pass for the wrong reason (everything
    rejected)."""
    claims = await _validate(_sign(signing_key, _claims()))
    assert claims["email"] == "user@x.test"


@pytest.mark.asyncio
async def test_id_token_validate_rejects_wrong_nonce(patch_jwks, signing_key):
    """The bound nonce from `consume_state` must equal the nonce in the
    IdP-returned ID token. Without this check, a captured ID token from
    a different session can be replayed against our callback."""
    from app.services import sso

    token = _sign(signing_key, _claims(nonce="the-real-nonce"))
    with pytest.raises(sso.SSOValidationError, match="modified in transit"):
        await _validate(token, expected_nonce="something-completely-different")


@pytest.mark.asyncio
async def test_id_token_validate_rejects_tampered_signature(patch_jwks, signing_key):
    """A token whose body was altered after signing must fail signature
    verification — the whole point of validating the JWS."""
    from app.services import sso

    token = _sign(signing_key, _claims())
    header_b64, payload_b64, sig_b64 = token.split(".")
    # Swap in a different (validly-signed) payload while keeping the original
    # signature — the signature no longer covers these bytes.
    forged_payload = _sign(signing_key, _claims(email="attacker@evil.test")).split(".")[1]
    tampered = f"{header_b64}.{forged_payload}.{sig_b64}"
    with pytest.raises(sso.SSOValidationError, match="could not be verified"):
        await _validate(tampered)


@pytest.mark.asyncio
async def test_id_token_validate_rejects_expired_token(patch_jwks, signing_key):
    """An expired token must be rejected by the exp validator even though
    the signature is valid — a replayed-but-stale token is still a replay."""
    from app.services import sso

    token = _sign(signing_key, _claims(exp=int(time.time()) - 10))
    with pytest.raises(sso.SSOValidationError, match="could not be verified"):
        await _validate(token)


@pytest.mark.asyncio
async def test_id_token_validate_rejects_wrong_audience(patch_jwks, signing_key):
    """A token minted for a different client_id must be rejected —
    otherwise a token issued for another relying party at the same IdP
    could be replayed against us (audience confusion)."""
    from app.services import sso

    token = _sign(signing_key, _claims(aud="some-other-client"))
    with pytest.raises(sso.SSOValidationError, match="could not be verified"):
        await _validate(token)


@pytest.mark.asyncio
async def test_id_token_validate_rejects_wrong_issuer(patch_jwks, signing_key):
    """A token whose `iss` doesn't match the discovery document's issuer
    must be rejected — pins the token to the IdP we actually configured."""
    from app.services import sso

    token = _sign(signing_key, _claims(iss="https://evil-idp.test"))
    with pytest.raises(sso.SSOValidationError, match="could not be verified"):
        await _validate(token)


@pytest.mark.asyncio
async def test_id_token_validate_rejects_alg_confusion_hs256(patch_jwks, signing_key):
    """Algorithm-confusion attack: an attacker who only has the IdP's
    PUBLIC key forges an HS256 token using the public key bytes as the
    HMAC secret. If we verified with the algorithm taken from the token
    header, the public key would 'verify' it. Pinning to asymmetric
    algorithms must reject it outright."""
    from app.services import sso

    public_modulus = base64.urlsafe_b64decode(signing_key.as_dict(private=False)["n"] + "==")
    forged = _sign(
        signing_key,
        _claims(),
        alg="HS256",
        key=OctKey.import_key(public_modulus),
    )
    with pytest.raises(sso.SSOValidationError, match="could not be verified"):
        await _validate(forged)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_jwks",
    [
        {"token_endpoint": "https://idp.test/token"},  # JSON object, no "keys"
        ["not", "an", "object"],  # JSON array instead of an object
    ],
    ids=["missing-keys", "not-an-object"],
)
async def test_id_token_validate_rejects_malformed_jwks(monkeypatch, signing_key, bad_jwks):
    """A malformed JWKS from the IdP (a JSON object without `keys`, or a
    non-object) must fail closed to the generic rejection — not raise a
    bare KeyError/TypeError that 500s and leaks the JWKS URL + contents
    in the traceback. `KeySet.import_key_set` raises those un-wrapped."""
    from app.services import sso

    async def _fake_fetch_jwks(_url):
        return bad_jwks

    monkeypatch.setattr("app.services.sso.fetch_jwks", _fake_fetch_jwks)
    token = _sign(signing_key, _claims())
    with pytest.raises(sso.SSOValidationError, match="could not be verified"):
        await _validate(token)


@pytest.mark.asyncio
async def test_id_token_validate_rejects_alg_none(patch_jwks):
    """An unsigned (`alg: none`) token must never be accepted. Built by
    hand — `{header}.{payload}.` with an empty signature — since a JWS
    library rightly refuses to *emit* one."""
    from app.services import sso

    def _b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    unsigned = f"{_b64({'alg': 'none', 'kid': _KID})}.{_b64(_claims())}."
    with pytest.raises(sso.SSOValidationError, match="could not be verified"):
        await _validate(unsigned)


# ---------------------------------------------------------------------------
# Redirect URI is per-tenant — prevents tenant-mixed callbacks
# ---------------------------------------------------------------------------


def test_redirect_uri_includes_tenant_slug():
    """The OIDC redirect URI registered with the IdP is per-tenant,
    so an IdP misconfigured for tenant A can't be reused to phish
    tenant B's users by sending them to a callback under A's
    domain that's bound to B's state. Pin the slug appears in the URI."""
    from app.services.sso import redirect_uri

    uri_acme = redirect_uri("acme")
    uri_tf = redirect_uri("techflow")
    assert uri_acme != uri_tf
    assert "acme" in uri_acme
    assert "techflow" in uri_tf


# ---------------------------------------------------------------------------
# SSRF: admin-supplied SSO URLs are fetched server-side
#
# `settings.sso.discovery_url` is entered by a tenant admin and fetched by the
# backend; the document it serves then supplies `token_endpoint` (POSTed with
# the client secret + auth code) and `jwks_uri` (the key the ID token is
# verified against). All three were used verbatim, and the discovery fetch is
# reachable unauthenticated via `GET /api/auth/sso/authorize?slug=<tenant>`
# after a public self-signup — so pointing it at `http://169.254.169.254/...`
# made the backend read cloud instance credentials on demand.
#
# Two independent controls, both pinned here:
#   * `assert_public_url` (the guard every other admin-supplied URL this app
#     fetches already goes through), enforced in DEPLOYED environments;
#   * netloc pinning of token_endpoint / jwks_uri to the discovery document's
#     own issuer, mirroring what `api/auth_sso.py` does for
#     `authorization_endpoint`.
# ---------------------------------------------------------------------------


@pytest.fixture
def deployed(monkeypatch):
    """Pretend this process is a deployed stack (the guard's enforcing mode)."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "environment", "production")
    return app_settings


@pytest.mark.parametrize(
    "bad",
    [
        "ftp://idp.example/.well-known/openid-configuration",
        "file:///etc/passwd",
        "not-a-url",
        "https://",
    ],
)
def test_resolve_sso_config_rejects_a_non_http_discovery_url(bad):
    """Refused at resolve time, before anything is fetched — and in every
    environment, since no local IdP needs a non-http(s) scheme."""
    from app.services.sso import SSOConfigError, resolve_sso_config

    cfg = {
        "sso": {
            "enabled": True,
            "discovery_url": bad,
            "client_id": "cid",
            "client_secret": "sec",
        }
    }
    with pytest.raises(SSOConfigError):
        resolve_sso_config(cfg)


@pytest.mark.asyncio
async def test_fetch_discovery_refuses_an_internal_address_when_deployed(deployed, monkeypatch):
    """The cloud metadata endpoint is the payload that matters: no HTTP request
    may leave the process, and nothing may be read from cache either."""
    from app.services import sso

    def _boom(*_a, **_kw):
        raise AssertionError("fetch_discovery must not open a client for an internal URL")

    monkeypatch.setattr(sso.httpx, "AsyncClient", _boom)

    with pytest.raises(sso.SSOConfigError):
        await sso.fetch_discovery("http://169.254.169.254/.well-known/openid-configuration")


@pytest.mark.asyncio
async def test_fetch_jwks_refuses_an_internal_address_when_deployed(deployed, monkeypatch):
    from app.services import sso

    def _boom(*_a, **_kw):
        raise AssertionError("fetch_jwks must not open a client for an internal URL")

    monkeypatch.setattr(sso.httpx, "AsyncClient", _boom)

    with pytest.raises(sso.SSOValidationError):
        await sso.fetch_jwks("http://127.0.0.1:9000/jwks")


@pytest.mark.asyncio
async def test_internal_sso_url_is_allowed_outside_a_deployed_environment():
    """Local-first (root CLAUDE.md guard rail 7): the documented local IdP is
    Keycloak on `http://localhost:8088`, which is exactly the shape the guard
    rejects. Outside a deployed environment it logs instead of refusing."""
    from app.config import settings as app_settings
    from app.services import sso

    assert not app_settings.is_deployed  # the test env is "development"
    await sso._assert_sso_url_public(
        "http://localhost:8088/realms/feohledger/.well-known/openid-configuration",
        what="discovery_url",
        error=sso.SSOConfigError,
    )


def test_pinned_endpoint_accepts_an_endpoint_on_the_issuer_host():
    from app.services import sso

    doc = {"issuer": "https://idp.test", "token_endpoint": "https://idp.test/oauth2/token"}
    assert sso._pinned_endpoint(doc, "token_endpoint") == "https://idp.test/oauth2/token"


@pytest.mark.parametrize(
    "doc",
    [
        {"issuer": "https://idp.test", "token_endpoint": "https://evil.test/token"},
        {"issuer": "https://idp.test", "token_endpoint": "http://169.254.169.254/token"},
        {"issuer": "https://idp.test"},  # endpoint missing entirely
        {"token_endpoint": "https://idp.test/token"},  # issuer missing — no anchor
        {"issuer": "not-a-url", "token_endpoint": "https://idp.test/token"},
    ],
    ids=["other-host", "metadata-host", "no-endpoint", "no-issuer", "issuer-not-a-url"],
)
def test_pinned_endpoint_rejects_anything_off_the_issuer_host(doc):
    from app.services import sso

    with pytest.raises(sso.SSOValidationError):
        sso._pinned_endpoint(doc, "token_endpoint")


@pytest.mark.asyncio
async def test_exchange_code_refuses_a_token_endpoint_on_another_host(monkeypatch):
    """This POST carries the client secret AND the authorization code — a
    redirected token endpoint hands both to the attacker."""
    from app.services import sso

    class _Boom:
        def __init__(self, *_a, **_kw):
            raise AssertionError("no request may be made to an unpinned token endpoint")

    monkeypatch.setattr(sso.httpx, "AsyncClient", _Boom)

    doc = {"issuer": "https://idp.example", "token_endpoint": "https://evil.example/token"}
    with pytest.raises(sso.SSOValidationError):
        await sso.exchange_code_for_tokens(doc, "cid", "secret", "code", "acme")


@pytest.mark.asyncio
async def test_validate_id_token_refuses_a_jwks_uri_on_another_host(signing_key):
    """The JWKS supplies the key the signature is checked against, so an
    attacker-controlled one verifies an attacker-signed ID token."""
    from app.services import sso

    async def _boom(_url):
        raise AssertionError("no JWKS fetch may be made off the issuer host")

    doc = {"issuer": _ISSUER, "jwks_uri": "https://evil.test/jwks"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.services.sso.fetch_jwks", _boom)
        with pytest.raises(sso.SSOValidationError):
            await sso.validate_id_token(
                id_token=_sign(signing_key, _claims()),
                discovery_doc=doc,
                client_id=_CLIENT_ID,
                expected_nonce="the-real-nonce",
            )


# ---------------------------------------------------------------------------
# An unresolvable-but-`enabled` block is a 400, never an uncaught 500
# (docs/followups.md "The public SSO entry points answer an unresolvable IdP
# block with a 500, not their documented 400 / 404")
# ---------------------------------------------------------------------------


def _broken_oidc_org(slug: str = "acme"):
    """`enabled: true` but missing a required OIDC key — `resolve_sso_config`
    raises `SSOConfigError` for this shape, not `None`."""
    from types import SimpleNamespace

    return SimpleNamespace(
        slug=slug,
        settings={
            "sso": {
                "enabled": True,
                "discovery_url": "https://idp.example/.well-known/openid-configuration",
                "client_id": "client-123",
                # client_secret deliberately omitted -> SSOConfigError
            }
        },
    )


def test_resolve_sso_or_none_absorbs_config_error():
    """The helper `sso_authorize` / `sso_callback` call: `SSOConfigError` ->
    `None`, exactly like a genuinely absent block."""
    from app.api import auth_sso
    from app.services.sso import SSOConfigError

    # Sanity: the block really does raise, not resolve to None on its own.
    with pytest.raises(SSOConfigError):
        auth_sso.resolve_sso_config(_broken_oidc_org().settings)
    assert auth_sso._resolve_sso_or_none(_broken_oidc_org().settings) is None


@pytest.mark.asyncio
async def test_sso_authorize_broken_config_is_400_not_500(monkeypatch):
    from fastapi import HTTPException

    from app.api import auth_sso

    async def _resolve_slug(slug, host, db):
        return slug

    async def _fetch_org(slug, db):
        return _broken_oidc_org()

    monkeypatch.setattr(auth_sso, "resolve_sso_tenant_slug", _resolve_slug)
    monkeypatch.setattr(auth_sso, "_fetch_org_by_slug", _fetch_org)

    with pytest.raises(HTTPException) as exc:
        await auth_sso.sso_authorize(slug="acme", host=None, db=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "SSO is not configured for this tenant."
    # The public response never names the offending key.
    assert "client_secret" not in exc.value.detail


@pytest.mark.asyncio
async def test_sso_callback_broken_config_is_400_not_500(monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api import auth_sso

    async def _consume_state(state):
        return {"tenant": "acme", "nonce": "nonce-y"}

    async def _fetch_org(slug, db):
        return _broken_oidc_org()

    monkeypatch.setattr(auth_sso, "consume_state", _consume_state)
    monkeypatch.setattr(auth_sso, "_fetch_org_by_slug", _fetch_org)

    fake_request = SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"), headers={})
    body = auth_sso.SSOCallbackRequest(code="the-code", state="the-state")

    with pytest.raises(HTTPException) as exc:
        await auth_sso.sso_callback(body, fake_request, db=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "SSO is not configured for this tenant."
    assert "client_secret" not in exc.value.detail
