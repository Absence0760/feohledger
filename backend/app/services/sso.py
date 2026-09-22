"""SSO helpers — OIDC config resolution, state/nonce management, token exchange.

One generic OIDC flow covers both Okta and Microsoft Entra (and any other
OIDC-compliant IdP) because all the provider-specific data comes from the
discovery document. Tenant-scoped config lives on Organization.settings.sso:

    {
      "enabled": true,
      "provider": "okta" | "entra" | "oidc",     # label only, drives UI copy
      "discovery_url": "https://<tenant>.okta.com/.well-known/openid-configuration",
      "client_id": "...",
      "client_secret": "...",                    # encrypted at rest via pg
      "scim_bearer_hash": "<sha256 hex>",        # per-tenant SCIM API token
      "allowed_email_domains": ["acme.com"]      # optional JIT allowlist
    }
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.redis import get_redis
from app.schemas.organization import looks_like_http_url
from app.tenant import resolve_tenant_slug_by_custom_domain
from app.utils.url_safety import UnsafeUrlError, assert_public_url_async

logger = logging.getLogger(__name__)

STATE_PREFIX = "sso:state:"
DISCOVERY_CACHE_PREFIX = "sso:discovery:"
JWKS_CACHE_PREFIX = "sso:jwks:"
# Discovery / JWKS caches — safe to cache for a day. Both providers rotate
# signing keys on the order of weeks, so a day is conservative.
DISCOVERY_CACHE_TTL = 86400

# OIDC ID tokens are asymmetrically signed (RFC 7518). Pin verification to the
# asymmetric algorithm set so a forged token can't downgrade the signature to
# HMAC — the classic alg-confusion attack, where an attacker signs with the
# IdP's *public* key bytes as an HMAC secret — or to `alg:none`. joserfc raises
# UnsupportedAlgorithmError (a JoseError) for any header `alg` outside this set,
# so the catch below turns that into a generic rejection. Never add HS*/none.
ID_TOKEN_ALGORITHMS = [
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
    "EdDSA",
]


class SSOConfigError(ValueError):
    """Raised when a tenant's SSO config is missing or invalid.

    ``fields`` names the config keys at fault. It holds key names only, never
    values, because the block carries the OIDC client secret and
    ``PATCH /api/organization`` puts these names in its 422 body
    (docs/decisions.md §204). Every message raised with it follows the same
    rule.
    """

    def __init__(self, message: str, *, fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.fields = fields


class SSOValidationError(ValueError):
    """Raised when the IdP response fails validation (bad state, bad token)."""


@dataclass
class ResolvedSSOConfig:
    provider: str
    discovery_url: str
    client_id: str
    client_secret: str
    allowed_email_domains: list[str]


# The settings.sso block is shared by both SSO protocols, discriminated by a
# `protocol` key. Absent / "oidc" => the existing OIDC path (back-compat);
# "saml" => the SAML SP path. resolve_sso_config and resolve_saml_config each
# return None for the other protocol so neither can be driven by the wrong one.
SAML_PROTOCOL = "saml"

# The keys each protocol's IdP block cannot resolve without.
OIDC_REQUIRED_FIELDS = ("discovery_url", "client_id", "client_secret")
SAML_REQUIRED_FIELDS = ("idp_entity_id", "idp_sso_url", "idp_x509_cert")


def _sso_block(org_settings: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """``org_settings["sso"]``, or an empty mapping when either level is not one.

    The block is free-form JSONB that ``PATCH /api/organization`` merges without
    a schema, and ``is_sso_only`` reads it on every password sign-in. A
    malformed row therefore has to read as "no SSO configured" and never raise.
    """
    if not isinstance(org_settings, Mapping):
        return {}
    sso = org_settings.get("sso")
    return sso if isinstance(sso, Mapping) else {}


def _settings_protocol(sso: Mapping[str, Any]) -> str:
    raw = sso.get("protocol")
    return raw.lower() if isinstance(raw, str) and raw else "oidc"


def _required_text(sso: Mapping[str, Any], names: tuple[str, ...]) -> list[str]:
    """The values of ``names``, each of which must be a non-blank string.

    Raises one ``SSOConfigError`` naming every key that is absent, blank or not
    text, so an admin can fix the whole block after a single refusal.
    """
    bad = tuple(
        name for name in names if not (isinstance(sso.get(name), str) and sso.get(name).strip())
    )
    if bad:
        raise SSOConfigError(
            f"SSO is enabled but these keys are missing, blank or not text: {', '.join(bad)}.",
            fields=bad,
        )
    return [sso[name] for name in names]


def _optional_text(sso: Mapping[str, Any], name: str, default: str | None) -> str | None:
    """``sso[name]`` when set, ``default`` when falsy; refuses a value that is not text."""
    value = sso.get(name)
    if not value:
        return default
    if not isinstance(value, str):
        raise SSOConfigError(f"SSO {name} must be text.", fields=(name,))
    return value


def _email_domains(sso: Mapping[str, Any]) -> list[str]:
    """The optional JIT allowlist. A malformed one is refused, not ignored,
    because ignoring it would admit every domain."""
    raw = sso.get("allowed_email_domains")
    if not raw:
        return []
    if not isinstance(raw, list) or not all(isinstance(d, str) for d in raw):
        raise SSOConfigError(
            "SSO allowed_email_domains must be a list of domains.",
            fields=("allowed_email_domains",),
        )
    return list(raw)


def sso_only_requested(org_settings: Mapping[str, Any] | None) -> bool:
    """Has the tenant ASKED for SSO-only (``sso.enabled`` and ``sso.sso_only``)?

    This is the request, not the verdict. Whether password sign-in is actually
    closed is ``is_sso_only``, which also needs the IdP config to resolve. Use
    this one only to ask what the admin configured, e.g. when validating a
    write or noting that a request is not being honoured.
    """
    sso = _sso_block(org_settings)
    return bool(sso.get("enabled") and sso.get("sso_only"))


def check_sso_idp_config(org_settings: Mapping[str, Any] | None) -> None:
    """Raise ``SSOConfigError`` unless the IdP block of the protocol that
    ``settings.sso`` selects resolves. Does nothing when SSO is switched off.

    It runs the same code ``resolve_sso_config`` and ``resolve_saml_config``
    run, so "resolves" means exactly what the public config endpoints and the
    authorize / login handlers mean by it. Every check is local: presence,
    type, URL shape and base64. There is no DNS lookup and no discovery fetch,
    which is what makes it cheap enough for the password sign-in path, where
    ``is_sso_only`` calls it.
    """
    sso = _sso_block(org_settings)
    if not sso.get("enabled"):
        return
    if _settings_protocol(sso) == SAML_PROTOCOL:
        _validated_saml_block(sso)
    else:
        resolve_sso_config(org_settings)


def is_sso_only(org_settings: Mapping[str, Any] | None) -> bool:
    """True when password sign-in is closed for the whole org: SSO is enabled,
    the tenant requires it (``sso_only``, shared by OIDC and SAML), AND the IdP
    config of the selected protocol resolves.

    The last condition is the escape hatch. A tenant whose IdP block does not
    resolve has no SSO button on its login page, because the public
    ``/auth/{sso,saml}/config`` endpoints report SSO as off. If the password
    were closed as well, nobody could start a session. So an unresolvable block
    keeps the password open until it is fixed, and ``PATCH /api/organization``
    refuses to save one in the first place (docs/decisions.md §204).

    This is the one statement of the rule. Login's refusal, the step-up's
    password drop, ``/auth/me``'s ``password_sign_in_closed`` (all through
    ``api/auth._org_closes_password_sign_in``) and the config endpoints'
    ``sso_only`` echo all call it, so none of them can disagree.

    "Resolves" is a local completeness check, not a liveness check. A complete
    block pointing at an IdP that is down still closes the password.
    """
    if not sso_only_requested(org_settings):
        return False
    try:
        check_sso_idp_config(org_settings)
    except SSOConfigError:
        return False
    return True


async def resolve_sso_tenant_slug(
    slug: str | None,
    host: str | None,
    db: AsyncSession,
) -> str | None:
    """Which tenant is this public SSO entry-point request for?

    Two sources, in order:

    1. an explicit ``?slug=`` — the platform-subdomain path, unchanged; and
    2. the request ``Host``, matched against the per-org
       ``settings.brand.custom_domains`` list.

    The second exists because a white-label tenant served on its own vanity
    hostname has no slug to put in the query string: the SPA there sends no
    ``X-Tenant-Slug`` at all (that header is what SUPPRESSES the backend's
    ``Host`` lookup), so before this the SSO and SAML buttons had to be hidden
    on a vanity host entirely.

    The mapping is the SAME resolver ``app.tenant.get_tenant_slug`` uses —
    imported, never re-implemented — so a hostname can never resolve one way
    for a page load and another way for a login. Returns ``None`` when nothing
    resolves; the caller turns that into the *same* response an unknown
    ``?slug=`` already produces, so this adds no new distinguishable outcome.

    Unlike the authenticated paths there is no JWT to cross-check here (these
    endpoints are pre-authentication by definition). That is safe because a
    forged ``Host`` can only select a tenant that has *registered* that exact
    hostname — the attacker picks which tenant's own IdP they are bounced to,
    which is the same choice ``?slug=`` already gives anyone, and the
    handshake's state/nonce (OIDC) or RelayState (SAML) is minted against that
    tenant and consumed against it.
    """
    if slug:
        return slug
    return await resolve_tenant_slug_by_custom_domain(db, host)


def _assert_sso_url_shape(url: str, *, what: str) -> None:
    """Cheap, DNS-free sanity check on an SSO URL.

    Runs on the *resolve* path, which the PUBLIC `GET /api/auth/sso/config`
    endpoint reaches on every request — so it deliberately does no name
    resolution. `_assert_sso_url_public` below is the real SSRF guard and runs
    at each fetch site (off-thread), which is where a request actually leaves
    the process.

    ``urlparse`` raises ``ValueError`` on some malformed input (an unclosed IPv6
    bracket). That becomes the same ``SSOConfigError``, because this also runs
    on the password sign-in path through ``is_sso_only``, and a bad row there
    must not surface as a 500.
    """
    message = f"SSO {what} must be an http(s) URL."
    try:
        parsed = urlparse(url or "")
        hostname = parsed.hostname
    except ValueError as exc:
        raise SSOConfigError(message, fields=(what,)) from exc
    if parsed.scheme not in ("http", "https") or not hostname:
        raise SSOConfigError(message, fields=(what,))


async def _assert_sso_url_public(url: str, *, what: str, error: type[ValueError]) -> None:
    """SSRF guard for a URL this process is about to fetch server-side.

    The discovery URL is admin-supplied, and the token / JWKS endpoints come
    from whatever that URL served — so all three are attacker-influenced input
    to a server-side request, reachable unauthenticated through
    `GET /api/auth/sso/authorize?slug=<tenant>` after a public self-signup.
    Without this, pointing `discovery_url` at `http://169.254.169.254/...` makes
    the backend fetch cloud instance credentials on demand. Same
    `assert_public_url` every other admin-supplied URL this app fetches goes
    through (branding logo, chat webhooks, ERP / enrichment base URLs).

    Resolution goes through the awaitable form of the guard (`loop.getaddrinfo`),
    so it never blocks the event loop.

    **Non-deployed environments log instead of refusing.** The documented
    local IdP is Keycloak on `http://localhost:8088` (`pnpm idp:up` +
    `pnpm idp:seed`), and a loopback address is exactly what the guard exists
    to reject — enforcing it everywhere would make local-first SSO impossible
    (root `CLAUDE.md` guard rail 7). `settings.is_deployed` is the same
    discriminator the extraction-provider fallback uses; the *shape* check
    above is unconditional either way, so a `file://` URL is refused in dev too.
    """
    try:
        await assert_public_url_async(url)
    except UnsafeUrlError as exc:
        if settings.is_deployed:
            # PII-free: names the field, never the URL (it can carry a tenant
            # identifier) and never the resolved address.
            logger.warning("SSO %s is not publicly routable; refusing to fetch it", what)
            raise error(f"SSO {what} must be a publicly routable http(s) URL.") from exc
        logger.warning(
            "SSO %s resolves to a non-public address; allowed because this is not a "
            "deployed environment (local IdP)",
            what,
        )


def _pinned_endpoint(discovery_doc: dict, key: str) -> str:
    """Return `discovery_doc[key]`, pinned to the document's own issuer host.

    The discovery document is fetched from the tenant's configured
    `discovery_url`, but every endpoint inside it is then used verbatim — so a
    compromised or mis-served document could point the token POST (which
    carries the client secret and the auth code) or the JWKS fetch (which
    supplies the key the ID token is verified against) at an arbitrary host.
    OIDC Discovery requires the document to be served under its own `issuer`,
    so the issuer's netloc is the host-of-record; this mirrors the check
    `api/auth_sso.py` already applies to `authorization_endpoint`.
    """
    raw = discovery_doc.get(key)
    issuer = discovery_doc.get("issuer")
    if not isinstance(raw, str) or not raw or not isinstance(issuer, str) or not issuer:
        raise SSOValidationError("Identity provider configuration is incomplete.")
    issuer_netloc = urlparse(issuer).netloc
    parsed = urlparse(raw)
    if (
        not issuer_netloc
        or parsed.scheme not in ("http", "https")
        or parsed.netloc != issuer_netloc
    ):
        logger.warning(
            "SSO discovery: %s host %r does not match issuer host %r",
            key,
            parsed.netloc,
            issuer_netloc,
        )
        raise SSOValidationError("Identity provider configuration is inconsistent.")
    return raw


def resolve_sso_config(org_settings: Mapping[str, Any] | None) -> ResolvedSSOConfig | None:
    """Pull + validate the OIDC SSO block from Organization.settings. Returns
    None if OIDC SSO isn't configured for this tenant (incl. when the tenant is
    configured for SAML instead).

    Raises ``SSOConfigError``, and nothing else, for any block it cannot use:
    ``is_sso_only`` reaches this on the password sign-in path through
    ``check_sso_idp_config``."""
    sso = _sso_block(org_settings)
    if not sso.get("enabled"):
        return None
    if _settings_protocol(sso) == SAML_PROTOCOL:
        # SAML tenant — resolved via resolve_saml_config, not the OIDC path.
        return None
    discovery, client_id, client_secret = _required_text(sso, OIDC_REQUIRED_FIELDS)
    _assert_sso_url_shape(discovery, what="discovery_url")
    return ResolvedSSOConfig(
        provider=_optional_text(sso, "provider", "oidc"),
        discovery_url=discovery,
        client_id=client_id,
        client_secret=client_secret,
        allowed_email_domains=_email_domains(sso),
    )


def _brand_sso_callback_base(org_settings: Mapping[str, Any] | None) -> str:
    """The per-org SSO callback base URL override, or ``""`` when unset.

    Lives on ``settings.brand.sso_callback_base_url`` (managed by
    ``PUT /api/organization/branding``). Re-validated here on the way out even
    though the branding endpoint validates on the way in — a row edited
    straight in the database has never been through the API, and this value
    becomes a 302 target and an OIDC ``redirect_uri``. Same shape rule the
    branding schema enforces, imported rather than restated.

    Anything unusable (missing, wrong type, not an http(s) URL) reads as
    "unset", so a malformed row degrades to the global template rather than
    breaking every SSO login for that tenant.
    """
    if not isinstance(org_settings, Mapping):
        return ""
    brand = org_settings.get("brand")
    if not isinstance(brand, Mapping):
        return ""
    raw = brand.get("sso_callback_base_url")
    if not isinstance(raw, str):
        return ""
    candidate = raw.strip()
    if not candidate or not looks_like_http_url(candidate):
        return ""
    return candidate


def sso_callback_base(tenant_slug: str, org_settings: Mapping[str, Any] | None = None) -> str:
    """Base URL (no trailing slash) that this tenant's SSO callbacks land on.

    Two sources, in order:

    1. the per-org opt-in override ``settings.brand.sso_callback_base_url``, and
    2. the global ``FEOH_TENANT_URL_TEMPLATE``.

    ``{slug}`` is substituted when present and the value used verbatim when it
    is not — the same rule ``app/utils/tenant_urls.tenant_base_url`` applies,
    because a vanity host is a complete base URL with no slug in it while the
    global template is slug-shaped by construction.

    **Why this is not just a call to that resolver.** The two values built from
    this base — the OIDC ``redirect_uri`` and the SAML bridge URL — are
    *registered at the customer's IdP*. Reading the per-org
    ``tenant_url_template`` (which an admin sets to fix invite and password-reset
    links) would silently re-point them and break every SSO login until the
    operator re-registered the app. So SSO gets its own, separately opt-in
    override; unset means the global template, byte-for-byte as before. See
    ``docs/decisions.md`` §91 and ``docs/founder-runbooks/custom-domain-provisioning.md``.

    The value reaches a 302/303 ``Location`` (the SAML ACS bridge redirect), so
    it is admin-only to write and re-validated to an http(s) URL here. That is
    not a new trust grant: a tenant admin already controls ``sso.idp_sso_url``,
    which ``saml_login`` 302s to directly, and the handoff code carried to this
    base is single-use and scoped to that same tenant's own user.
    """
    base = _brand_sso_callback_base(org_settings)
    if not base:
        base = settings.tenant_url_template or "http://{slug}.localhost:7777"
    if "{slug}" in base:
        base = base.replace("{slug}", tenant_slug)
    return base.rstrip("/")


def redirect_uri(tenant_slug: str, org_settings: Mapping[str, Any] | None = None) -> str:
    """Build the per-tenant OIDC callback URL.

    Each tenant registers their Okta/Entra app with *their own* subdomain as
    the redirect URI — e.g. acme.app.com for tenant `acme`. That way the
    callback lands on the tenant origin and our localStorage JWT works
    without cross-origin hops. `sso_callback_base` is the single source of
    truth for what that URL looks like: the global tenant URL template, or the
    tenant's own opt-in `settings.brand.sso_callback_base_url` once the
    operator has re-registered the app at the IdP.

    `org_settings` is optional so the value is identical to the pre-override
    behaviour when a caller has no org in hand; every live call site passes it.
    """
    return f"{sso_callback_base(tenant_slug, org_settings)}{settings.sso_redirect_path}"


# ---------------------------------------------------------------------------
# SAML 2.0 (Service-Provider) config
# ---------------------------------------------------------------------------


@dataclass
class ResolvedSAMLConfig:
    provider: str
    idp_entity_id: str
    idp_sso_url: str
    # Trust anchor: the IdP's signing cert(s), normalized to bare base64 (PEM
    # armor + whitespace stripped). x509_certs[0] is primary; the rest support
    # zero-downtime IdP cert rotation. NEVER a cert embedded in the assertion.
    idp_x509_cert: str
    idp_x509_cert_multi: list[str] = field(default_factory=list)
    sp_entity_id: str = ""
    idp_slo_url: str | None = None
    allowed_email_domains: list[str] = field(default_factory=list)


def saml_sp_entity_id(tenant_slug: str) -> str:
    """Per-tenant SP EntityID (== SAML Audience the IdP must assert). A stable
    per-tenant URI derived from the backend public URL so each tenant's IdP only
    trusts that tenant's SP. Path form (no query string) so it drops cleanly
    into an IdP's client-id field. Admins may override via
    settings.sso.sp_entity_id."""
    base = settings.api_public_url.rstrip("/")
    return f"{base}/api/auth/saml/sp/{tenant_slug}"


def saml_acs_url() -> str:
    """Static Assertion Consumer Service URL the IdP POST-binds the SAMLResponse
    to. One ACS serves every tenant; the tenant is recovered from the
    server-minted RelayState, never from this URL. The IdP-asserted Destination
    is validated against this exact value."""
    base = settings.api_public_url.rstrip("/")
    return f"{base}/api/auth/saml/acs"


def _normalize_x509_cert(raw: object, *, field: str = "idp_x509_cert") -> str:
    """Strip PEM armor + whitespace and confirm the result is non-empty,
    valid base64. Raises SSOConfigError on empty/blank/garbage (and on a value
    that is not text at all) so a missing or malformed cert can NEVER reach
    python3-saml as "no cert => skip the signature check" — the load-bearing
    trust control."""
    if not isinstance(raw, str):
        raise SSOConfigError(f"SAML {field} must be PEM or base64 text.", fields=(field,))
    if not raw.strip():
        raise SSOConfigError(f"SAML {field} is empty.", fields=(field,))
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip() and "-----" not in ln]
    b64 = "".join(lines) if lines else raw.strip()
    try:
        decoded = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SSOConfigError(f"SAML {field} is not valid base64/PEM.", fields=(field,)) from exc
    if not decoded:
        raise SSOConfigError(f"SAML {field} decoded to empty bytes.", fields=(field,))
    return b64


def _validated_saml_block(sso: Mapping[str, Any]) -> ResolvedSAMLConfig:
    """Everything ``resolve_saml_config`` can refuse, for an enabled SAML block.

    Split out because it needs no tenant slug: the slug only feeds the derived
    default SP EntityID, which cannot fail. ``check_sso_idp_config`` calls this
    directly, so ``is_sso_only`` does not need a slug to answer. The result's
    ``sp_entity_id`` is the block's own override, or ``""`` when unset.
    """
    idp_entity_id, idp_sso_url, raw_cert = _required_text(sso, SAML_REQUIRED_FIELDS)
    # The login handler 302s here. Refusing a non-URL at resolve time means a
    # block that passes cannot hand the login page an SSO button that fails.
    _assert_sso_url_shape(idp_sso_url, what="idp_sso_url")
    cert = _normalize_x509_cert(raw_cert)
    raw_multi = sso.get("idp_x509_cert_multi") or []
    if not isinstance(raw_multi, list):
        raise SSOConfigError(
            "SAML idp_x509_cert_multi must be a list of certificates.",
            fields=("idp_x509_cert_multi",),
        )
    cert_multi = [_normalize_x509_cert(c, field="idp_x509_cert_multi") for c in raw_multi]
    return ResolvedSAMLConfig(
        provider=_optional_text(sso, "provider", "saml"),
        idp_entity_id=idp_entity_id,
        idp_sso_url=idp_sso_url,
        idp_x509_cert=cert,
        idp_x509_cert_multi=cert_multi,
        sp_entity_id=_optional_text(sso, "sp_entity_id", ""),
        idp_slo_url=_optional_text(sso, "idp_slo_url", None),
        allowed_email_domains=_email_domains(sso),
    )


def resolve_saml_config(
    org_settings: Mapping[str, Any] | None, tenant_slug: str
) -> ResolvedSAMLConfig | None:
    """Pull + validate the SAML SSO block from Organization.settings. Returns
    None when SAML SSO isn't configured for this tenant (incl. OIDC tenants).
    Raises SSOConfigError, and nothing else, when SAML is enabled but the IdP
    trust config is incomplete or malformed or the signing cert is
    missing/malformed."""
    sso = _sso_block(org_settings)
    if not sso.get("enabled"):
        return None
    if _settings_protocol(sso) != SAML_PROTOCOL:
        return None
    config = _validated_saml_block(sso)
    if not config.sp_entity_id:
        config.sp_entity_id = saml_sp_entity_id(tenant_slug)
    return config


# ---------------------------------------------------------------------------
# Discovery + JWKS (cached in Redis)
# ---------------------------------------------------------------------------


async def fetch_discovery(discovery_url: str) -> dict[str, Any]:
    """Return the OIDC provider's discovery document, cached in Redis.

    The URL is admin-supplied, so it goes through the SSRF guard before any
    request — and *before* the cache read too, so a value that was poisoned
    into the cache under an older build can't be served back.
    """
    _assert_sso_url_shape(discovery_url, what="discovery_url")
    await _assert_sso_url_public(discovery_url, what="discovery_url", error=SSOConfigError)
    r = await get_redis()
    key = f"{DISCOVERY_CACHE_PREFIX}{hashlib.sha256(discovery_url.encode()).hexdigest()}"
    cached = await r.get(key)
    if cached:
        return json.loads(cached)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(discovery_url)
        resp.raise_for_status()
        doc = resp.json()

    await r.setex(key, DISCOVERY_CACHE_TTL, json.dumps(doc))
    return doc


async def fetch_jwks(jwks_uri: str) -> dict[str, Any]:
    """Return the IdP's JWKS, cached in Redis.

    `jwks_uri` comes from the discovery document (already pinned to its issuer
    host by `_pinned_endpoint`); the SSRF guard here is the second, independent
    rung — this function is also a direct-call surface.
    """
    _assert_sso_url_shape(jwks_uri, what="jwks_uri")
    await _assert_sso_url_public(jwks_uri, what="jwks_uri", error=SSOValidationError)
    r = await get_redis()
    key = f"{JWKS_CACHE_PREFIX}{hashlib.sha256(jwks_uri.encode()).hexdigest()}"
    cached = await r.get(key)
    if cached:
        return json.loads(cached)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        jwks = resp.json()

    await r.setex(key, DISCOVERY_CACHE_TTL, json.dumps(jwks))
    return jwks


# ---------------------------------------------------------------------------
# State + nonce (CSRF / replay protection)
# ---------------------------------------------------------------------------


async def create_state(tenant_slug: str) -> tuple[str, str]:
    """Mint a state + nonce, store binding in Redis, return both.

    State defends the callback against CSRF; nonce defends the ID token
    against replay. Both are single-use and expire in ~10 minutes.
    """
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    r = await get_redis()
    payload = json.dumps({"tenant": tenant_slug, "nonce": nonce, "ts": time.time()})
    await r.setex(f"{STATE_PREFIX}{state}", settings.sso_state_ttl_seconds, payload)
    return state, nonce


async def consume_state(state: str) -> dict[str, Any]:
    """Look up + delete the state binding. Raises if state is unknown/expired."""
    r = await get_redis()
    key = f"{STATE_PREFIX}{state}"
    raw = await r.get(key)
    if not raw:
        raise SSOValidationError("Login session expired or was tampered with. Please try again.")
    await r.delete(key)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# SAML RelayState + one-time token handoff
# ---------------------------------------------------------------------------

SAML_RELAYSTATE_PREFIX = "saml:relaystate:"
SAML_HANDOFF_PREFIX = "saml:handoff:"


async def store_saml_relay_state(state: str, tenant_slug: str, request_id: str) -> None:
    """Bind a SAML RelayState to its tenant AND the AuthnRequest ID, atomically,
    as a single single-use Redis record. The ACS recovers BOTH from the one
    consume — so InResponseTo can be enforced against the exact request that was
    issued, and the tenant is taken from server-minted state (never the IdP)."""
    r = await get_redis()
    payload = json.dumps({"tenant": tenant_slug, "request_id": request_id, "ts": time.time()})
    await r.setex(f"{SAML_RELAYSTATE_PREFIX}{state}", settings.sso_state_ttl_seconds, payload)


async def consume_saml_relay_state(state: str) -> dict[str, Any]:
    """Look up + delete the RelayState binding (single-use). Raises if unknown
    or expired. Returns {tenant, request_id}."""
    r = await get_redis()
    key = f"{SAML_RELAYSTATE_PREFIX}{state}"
    raw = await r.get(key)
    if not raw:
        raise SSOValidationError("Login session expired or was tampered with. Please try again.")
    await r.delete(key)
    return json.loads(raw)


async def create_saml_handoff(
    access_token: str, must_change_password: bool, tenant_slug: str
) -> str:
    """Stash a freshly-minted JWT behind a one-time code so the ACS can
    303-redirect WITHOUT putting the token in the URL. The SPA bridge POSTs the
    code to /exchange and gets the token in the response body — mirroring the
    OIDC POST-to-callback shape (token never transits a URL / Referer / history)."""
    code = secrets.token_urlsafe(32)
    r = await get_redis()
    payload = json.dumps(
        {
            "access_token": access_token,
            "must_change_password": must_change_password,
            "tenant": tenant_slug,
        }
    )
    await r.setex(f"{SAML_HANDOFF_PREFIX}{code}", settings.saml_handoff_ttl_seconds, payload)
    return code


async def consume_saml_handoff(code: str) -> dict[str, Any]:
    """Look up + delete the handoff record (single-use). Raises if expired."""
    r = await get_redis()
    key = f"{SAML_HANDOFF_PREFIX}{code}"
    raw = await r.get(key)
    if not raw:
        raise SSOValidationError("This login link has expired. Please sign in again.")
    await r.delete(key)
    return json.loads(raw)


def saml_bridge_url(tenant_slug: str, org_settings: Mapping[str, Any] | None = None) -> str:
    """Per-tenant SPA bridge route the ACS 303-redirects to after minting the
    one-time handoff code. Lands on the tenant origin so the stored JWT works
    without a cross-origin hop, exactly like the OIDC callback page.

    Shares `sso_callback_base` with the OIDC `redirect_uri`, so a tenant that
    has completed the IdP re-registration gets BOTH protocols landing on its
    own hostname from the one opt-in setting."""
    return f"{sso_callback_base(tenant_slug, org_settings)}{settings.saml_acs_path}"


# ---------------------------------------------------------------------------
# Authorize URL + token exchange
# ---------------------------------------------------------------------------


# `build_authorize_url` used to live here and was reached by nothing but its own
# test. It read `discovery_doc["authorization_endpoint"]` VERBATIM — the one
# endpoint accessor in this module with no host pinning, while its siblings
# (`exchange_code_for_tokens`, `fetch_jwks`) both go through `_pinned_endpoint`
# + `_assert_sso_url_public`. Unreachable code cannot be caught drifting, and a
# future caller reaching for the obvious-looking helper would have reintroduced
# an open-redirect / credential-phishing primitive off a mis-served discovery
# document. It was deleted rather than hardened: the live path in
# `api/auth_sso.py::sso_authorize` already rebuilds the URL from individually
# validated components, and that inline data-flow shape is what CodeQL's
# py/url-redirection query recognises as a sanitizer — hoisting it behind a
# function call here would trade a real static-analysis guarantee for tidiness.


async def exchange_code_for_tokens(
    discovery_doc: dict,
    client_id: str,
    client_secret: str,
    code: str,
    tenant_slug: str,
    org_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """POST to the token endpoint with the auth code. Returns the token bundle.

    The redirect_uri here MUST exactly match the one sent during authorize —
    OIDC token exchange validates it as a defence against code-injection attacks.
    That is why `org_settings` is threaded in: the per-org callback override
    changes the value, so the two legs have to read the same source.

    The endpoint itself is pinned to the discovery document's own issuer host and
    SSRF-guarded before the POST — this request carries the client secret and the
    authorization code, so a redirected token endpoint hands both to an attacker.
    """
    token_endpoint = _pinned_endpoint(discovery_doc, "token_endpoint")
    await _assert_sso_url_public(token_endpoint, what="token_endpoint", error=SSOValidationError)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(tenant_slug, org_settings),
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        logger.warning("Token exchange failed: %s %s", resp.status_code, resp.text[:200])
        raise SSOValidationError("Identity provider rejected the login. Please try again.")
    return resp.json()


async def validate_id_token(
    id_token: str, discovery_doc: dict, client_id: str, expected_nonce: str
) -> dict[str, Any]:
    """Verify the ID token signature + standard claims. Returns decoded claims."""
    jwks = await fetch_jwks(_pinned_endpoint(discovery_doc, "jwks_uri"))
    try:
        key_set = KeySet.import_key_set(jwks)
        # decode() verifies the signature against the JWKS, restricted to the
        # pinned asymmetric algorithms. The claims registry then enforces the
        # standard OIDC checks — issuer + audience match, and (via the built-in
        # exp validator) that the token has not expired.
        token = jwt.decode(id_token, key_set, algorithms=ID_TOKEN_ALGORITHMS)
        claims_registry = jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": discovery_doc["issuer"]},
            aud={"essential": True, "value": client_id},
            exp={"essential": True},
        )
        claims_registry.validate(token.claims)
    except (JoseError, KeyError, TypeError) as exc:
        # JoseError covers signature / claim / algorithm failures. KeyError and
        # TypeError cover a malformed JWKS from the IdP (a JSON object without a
        # "keys" field, or a non-object) — KeySet.import_key_set raises those
        # bare, and without catching them the handler would 500 and leak the
        # JWKS URL + contents in the traceback. All fail closed to the same
        # generic rejection.
        logger.warning("ID token validation failed: %s", exc.__class__.__name__)
        raise SSOValidationError("Identity provider token could not be verified.") from exc

    claims = token.claims
    if claims.get("nonce") != expected_nonce:
        raise SSOValidationError("Login was modified in transit. Please try again.")

    return dict(claims)


# ---------------------------------------------------------------------------
# SCIM bearer token
# ---------------------------------------------------------------------------


def generate_scim_token() -> tuple[str, str]:
    """Mint a SCIM bearer token. Returns (plaintext, sha256_hex).

    Callers store ONLY the hex digest in org settings. The plaintext is shown
    to the admin once at generation time and never persisted.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_scim_token(raw)


def hash_scim_token(raw: str) -> str:
    """The ONE place a SCIM bearer token becomes its stored digest.

    Mint (`generate_scim_token`) and verify (`api/scim.py::get_scim_tenant`)
    are the two halves of one credential check, and both used to inline
    `hashlib.sha256(...).hexdigest()` themselves. They agreed, but nothing made
    them agree: changing the mint side alone — the estate is moving this class
    of secret toward `bcrypt_sha256`, see `models/api_key.py` — would lock
    every tenant out of SCIM provisioning with no failing test.
    `tests/test_sso_scim.py` guards that no other module spells the recipe out.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
