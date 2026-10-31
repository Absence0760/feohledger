"""OAuth 2.0 authorization-code connections for ERPs.

QuickBooks Online, Xero and Sage Business Cloud Accounting offer no
client-credentials grant: the customer's admin consents in the provider's UI,
and we hold a rotating refresh token afterwards. This module is that flow, once,
for every such adapter (``erp_adapters/oauth_base.OAuthErpAdapter``):

* :func:`build_authorize_url` / :func:`consume_state` — the signed, single-use
  ``state`` that carries org + user + provider across the provider's redirect,
  bound to the starting browser by :data:`BROWSER_COOKIE` (below);
* :func:`exchange_code` — the callback's code → token exchange;
* :func:`get_access_token` — what adapters call; refreshes and persists;
* :func:`revoke` — best-effort revocation on disconnect.

The routes live in ``app/api/erp_oauth.py``.

Bound to the browser that started it (login-CSRF)
-------------------------------------------------
The callback is public, so a signed state alone let a rogue tenant admin start
a connect and have someone else consent, linking that person's books to the
admin's tenant. ``/authorize`` therefore sets :data:`BROWSER_COOKIE` (random,
HttpOnly, host-only on the API host — :func:`browser_cookie_attributes`) in
the bearer-authenticated response, the state carries its SHA-256
(:func:`browser_binding`), and the callback compares the two
(:func:`browser_matches`, constant time). Only the admin's own browser can
make that bearer request, so only it holds the cookie.

Stored state — two halves, written together
-------------------------------------------
The consent METADATA lives in ``Organization.settings.erp.oauth``::

    {
        "provider": "quickbooks_online",
        "expires_at": "<ISO-8601 UTC>",
        "refresh_token_expires_at": "<ISO-8601 UTC>" | absent,
        "external_tenant_id": "<realmId / Xero tenantId / Sage business id>",
        "org_id": "<uuid>", "connection_id": "<random>",
        "client_source": "tenant" | "platform",
        "connected_at": "<ISO-8601 UTC>",
        "needs_reconnect": true | absent,
    }

The TOKENS are sealed in ``provider_credentials`` with the block's other
secrets, at the service-only paths ``oauth.access_token`` and
``oauth.refresh_token`` (``provider_credentials.SERVICE_SECRET_FIELDS``,
decisions §266). Only this module writes either half (the callback, the
refresher, disconnect), always through ``provider_credentials.update_secrets``
and under the org row lock, so the two cannot disagree. The tokens are read
here alone — never merged into an adapter's config — and every settings read
sees the metadata as ``{"connected": bool}`` (``catalog.public_erp_config``).
A settings save never takes the metadata from the request and keeps the stored
block (``catalog.merge_erp_update``), even across a switch of ERP type: the
block names its ``provider``, and :func:`get_access_token` refuses any other.

How the refresher finds the org — and why it can't be pointed at another one
---------------------------------------------------------------------------
An adapter is built from ``settings.erp`` alone and holds no DB session, so the
block carries ``org_id``. The adapter's config carries no tokens at all: the
refresher re-reads the org row and the sealed tokens and uses those. Since an admin can
hand ``POST /organization/test-erp`` an arbitrary config, ``org_id`` alone would
let tenant A's admin borrow tenant B's QuickBooks token. So the block also
carries ``connection_id`` — 128 random bits minted at consent, never returned by
any endpoint — and the stored block must carry the same value, for the same
provider, or the call fails closed with :class:`ErpNotConnectedError`. A
reconnect mints a new one, so a stale config can't reach a newer connection.

Concurrent refresh — one refresh per connection, never two burned tokens
-----------------------------------------------------------------------
Intuit and Xero rotate the refresh token on every use. Two workers refreshing
the same connection at once would each spend the stored token, and whichever
persisted second would overwrite the first's (possibly then the only valid)
token. Two layers:

1. **A Redis lock** per ``(org, provider)`` (``SET NX EX``) serialises the
   provider round trip. It is not a Postgres row lock on purpose: holding
   ``organizations`` ``FOR UPDATE`` across a 15-second HTTP call would stall
   every other settings writer for the tenant (the reason ``post_commit``
   exists). A caller that loses the lock polls the stored block until the
   winner has written a fresh token (bounded by :data:`_LOCK_WAIT_SECONDS`).
2. **A compare-and-swap write.** The rotated tokens are persisted under a
   short ``SELECT … FOR UPDATE`` of the org row — the lock every credential
   writer takes first — and only if the sealed refresh token is still the one
   this refresh spent. If it
   changed (the lock expired under a hung call, or a reconnect landed), the
   stored value wins and is used. This is what makes the lock's best-effort
   release (GET-then-DEL, no Lua) safe.

Client credentials
------------------
A tenant may bring its own provider app: ``settings.erp.client_id`` plus the
sealed ``client_secret`` while ``settings.erp.type`` names the provider. Otherwise the
platform app's ``FEOH_`` settings named by the spec. Neither → the provider is
unavailable (fail closed, no fallback). The source used at consent is recorded
as ``client_source`` and the refresher uses the same one: a token issued to one
app cannot be refreshed with another's credentials.

Tokens at rest
--------------
Sealed under the app KMS key like every provider credential
(``services/provider_credentials``). When the store cannot be opened (KMS
unreachable, a bad envelope) this module raises
:class:`ErpCredentialUnreadableError` — a refusal, never a garbage token sent
to the provider, never a fall-back to ``mock``, and never a ``needs_reconnect``
mark (the connection may be fine; the store is not). The adapters turn it into
a failed result, so an ERP send lands at ``failed`` and is retryable.

Nothing here logs a token, a code, or the provider's response body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy import select

from app import redis as app_redis
from app.config import settings
from app.services import provider_credentials
from app.services.credential_crypto import CredentialCryptoError
from app.services.erp_adapters.oauth_base import OAuthProviderSpec

logger = logging.getLogger(__name__)

#: Every provider an adapter has declared, keyed by ``OAuthProviderSpec.key``.
#: The connect routes look providers up here. Populated by
#: :func:`register_oauth_provider` at adapter import time.
OAUTH_PROVIDERS: dict[str, OAuthProviderSpec] = {}

#: The one redirect URI every provider app registers (path under
#: ``FEOH_API_PUBLIC_URL``). The tenant comes back in ``state``, not the URL.
CALLBACK_PATH = "/api/erp/oauth/callback"

#: The HttpOnly cookie that binds a connect to the browser that started it
#: (:func:`browser_cookie_attributes`). Set by the ``/authorize`` response on the
#: API host; its hash rides in ``state``; the callback compares and clears it.
#: The ``__Host-`` prefix makes the browser refuse any version of it that is not
#: ``Secure``, ``Path=/`` and host-only, so a sibling subdomain of the same site
#: cannot toss in a value of its own choosing (the weakness of a plain
#: double-submit cookie).
BROWSER_COOKIE = "__Host-feoh_erp_oauth_nonce"

#: Refresh when the access token has less than this left. Covers clock skew and
#: a request that starts just before expiry.
REFRESH_MARGIN = timedelta(seconds=120)

#: Where the tokens are sealed (``provider_credentials.SERVICE_SECRET_FIELDS``).
ACCESS_TOKEN_PATH = "oauth.access_token"
REFRESH_TOKEN_PATH = "oauth.refresh_token"
TOKEN_PATHS: tuple[str, ...] = (ACCESS_TOKEN_PATH, REFRESH_TOKEN_PATH)

_STATE_PREFIX = "erp:oauth:state:"
_LOCK_PREFIX = "erp:oauth:refresh-lock:"
_REALM_LOCK_PREFIX = "erp:oauth:realm-claim:"
_LOCK_TTL_SECONDS = 30
_LOCK_WAIT_SECONDS = 20.0
_LOCK_POLL_SECONDS = 0.1
_HTTP_TIMEOUT = 15.0


class ErpNotConnectedError(RuntimeError):
    """The org has no usable OAuth connection to this provider.

    Never consented, revoked, or the refresh token expired. The message is a
    fixed string carrying only the provider key: it can reach the append-only
    ``invoice.erp_failed`` audit row the way ``ErpPostResult.message`` does.
    """

    def __init__(self, provider_key: str):
        self.provider_key = provider_key
        super().__init__(f"{provider_key}: not connected (reconnect the ERP in Organization → ERP)")


class ErpTokenRefreshError(ErpNotConnectedError):
    """The provider could not be reached to refresh the token (network / 5xx).

    The connection is still valid — nothing is marked for reconnect. A subclass
    of :class:`ErpNotConnectedError` so an adapter's one ``except`` turns
    either into a failed, PII-free result.
    """

    def __init__(self, provider_key: str, reason: str):
        RuntimeError.__init__(self, f"{provider_key}: token refresh failed ({reason})")
        self.provider_key = provider_key
        self.reason = reason


class ErpCredentialUnreadableError(ErpNotConnectedError):
    """The sealed tokens or client secret could not be opened on this server.

    ``credential_crypto.CredentialCryptoError`` underneath: KMS unreachable or a
    bad envelope. Fixed text, no value.
    """

    def __init__(self, provider_key: str):
        RuntimeError.__init__(
            self,
            f"{provider_key}: stored ERP credentials could not be opened "
            "(the credential store is unavailable)",
        )
        self.provider_key = provider_key


class OAuthStateError(ValueError):
    """``state`` is forged, expired, or already used. ``code`` is a stable token."""

    def __init__(self, code: str, *, org_slug: str | None = None):
        super().__init__(code)
        self.code = code
        #: The tenant slug when the signature verified (so the callback can
        #: still send the browser home); None for a forged state.
        self.org_slug = org_slug


def register_oauth_provider(spec: OAuthProviderSpec) -> OAuthProviderSpec:
    """Record ``spec`` so the connect routes can find it. Returns it unchanged."""
    OAUTH_PROVIDERS[spec.key] = spec
    return spec


def load_providers() -> dict[str, OAuthProviderSpec]:
    """Import every built-in adapter (each registers its spec) and return the map."""
    from app.services.erp_adapters.dispatcher import load_builtin_adapters

    load_builtin_adapters()
    return OAUTH_PROVIDERS


# ---------------------------------------------------------------------------
# Endpoints and client credentials
# ---------------------------------------------------------------------------


def _override(setting_name: str | None) -> str:
    if not setting_name:
        return ""
    return str(getattr(settings, setting_name, "") or "").strip()


def authorize_endpoint(spec: OAuthProviderSpec) -> str:
    return _override(spec.authorize_url_setting) or spec.authorize_url


def token_endpoint(spec: OAuthProviderSpec) -> str:
    return _override(spec.token_url_setting) or spec.token_url


def revoke_endpoint(spec: OAuthProviderSpec) -> str | None:
    return _override(spec.revoke_url_setting) or spec.revoke_url


def callback_url() -> str:
    """The redirect URI registered with every provider app."""
    return f"{settings.api_public_url.rstrip('/')}{CALLBACK_PATH}"


@dataclass(frozen=True)
class ClientCredentials:
    client_id: str
    client_secret: str
    source: str  # "tenant" | "platform"


def _tenant_credentials(spec: OAuthProviderSpec, erp_settings: Any) -> ClientCredentials | None:
    if not isinstance(erp_settings, dict) or erp_settings.get("type") != spec.key:
        return None
    cid = str(erp_settings.get("client_id") or "").strip()
    secret = str(erp_settings.get("client_secret") or "").strip()
    if cid and secret:
        return ClientCredentials(cid, secret, "tenant")
    return None


def _platform_credentials(spec: OAuthProviderSpec) -> ClientCredentials | None:
    cid = _override(spec.client_id_setting)
    secret = _override(spec.client_secret_setting)
    if cid and secret:
        return ClientCredentials(cid, secret, "platform")
    return None


def resolve_client_credentials(
    spec: OAuthProviderSpec, erp_settings: Any, *, source: str | None = None
) -> ClientCredentials | None:
    """The app credentials to use, or None when the provider is unavailable.

    ``source`` pins one side (the refresher passes the consent's
    ``client_source``); None prefers the tenant's own app, then the platform's.
    ``erp_settings`` is the RESOLVED block (``provider_credentials.
    provider_config`` / :func:`resolved_erp`), so the tenant's sealed
    ``client_secret`` is already in it.
    """
    if source == "tenant":
        return _tenant_credentials(spec, erp_settings)
    if source == "platform":
        return _platform_credentials(spec)
    return _tenant_credentials(spec, erp_settings) or _platform_credentials(spec)


# ---------------------------------------------------------------------------
# State — signed, single-use, short TTL
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _state_mac(body: str) -> str:
    key = hashlib.sha256(b"feoh-erp-oauth-state\x00" + settings.secret_key.encode()).digest()
    return _b64(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())


def _state_ttl() -> int:
    return int(settings.erp_oauth_state_ttl_seconds)


def new_browser_nonce() -> str:
    """A fresh value for the :data:`BROWSER_COOKIE` nonce cookie."""
    return secrets.token_urlsafe(32)


def browser_binding(cookie_value: str) -> str:
    """What ``state`` carries for a browser nonce: its SHA-256, never the value.

    The state travels through the provider and the address bar; the cookie is
    HttpOnly. Only the hash crosses into the URL, so the state alone cannot be
    turned into a matching cookie.
    """
    return hashlib.sha256(cookie_value.encode()).hexdigest()


def browser_matches(cookie_value: str | None, binding: str) -> bool:
    """Does the callback's cookie belong to the browser that started the connect?

    Constant-time, and false for a missing cookie or a state minted without a
    binding (there is no such state after this change, but an absent field
    must never read as a match).
    """
    if not cookie_value or not binding:
        return False
    return hmac.compare_digest(browser_binding(cookie_value), binding)


def browser_cookie_attributes() -> dict[str, Any]:
    """``Response.set_cookie`` keyword arguments for :data:`BROWSER_COOKIE`.

    Host-only on the API's public host (no ``Domain``), ``Path=/`` (which the
    ``__Host-`` prefix requires), HttpOnly, and alive as long as the ``state``
    it pairs with. Only credentialed requests and top-level navigations to the
    API host carry it, and the SPA makes exactly one credentialed request.

    ``SameSite=Lax`` + ``Secure`` everywhere a real deployment runs: the app
    (``<slug>.<domain>``) and the API (``api.<domain>``) are same-site, so the
    credentialed ``/authorize`` fetch may set it, and the provider's top-level
    GET redirect to the callback carries it.

    A loopback API (local dev) is the one exception, and it is NOT "Secure off":
    ``acme.localhost`` and ``localhost`` are different sites, and a browser
    refuses a ``SameSite=Lax`` cookie set by a cross-site fetch (measured in
    Chromium). ``SameSite=None`` is the only value that crosses, and it
    requires ``Secure`` — which browsers accept from ``http://localhost``,
    a potentially trustworthy origin. Gated on the configured API URL being a
    loopback host AND the environment not being a deployed one, so a
    misconfigured deployment keeps ``Lax`` (and its connects fail closed at the
    callback) rather than silently loosening the cookie.
    """
    loopback = not settings.is_deployed and is_loopback_host(
        urlsplit(settings.api_public_url).hostname
    )
    return {
        "max_age": _state_ttl(),
        "path": "/",
        "httponly": True,
        "secure": True,
        "samesite": "none" if loopback else "lax",
    }


def is_loopback_host(host: str | None) -> bool:
    """``localhost``, ``*.localhost`` or a loopback IP literal."""
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


async def create_state(
    *,
    org_id: uuid.UUID,
    org_slug: str,
    user_id: uuid.UUID,
    provider: str,
    client_source: str,
    browser_nonce: str,
) -> str:
    """Mint a signed ``state`` and record its nonce in Redis (single use).

    The signature stops forgery (the callback is public); the Redis nonce makes
    it single-use and is what an expired or replayed state fails on. Both carry
    the same binding, so neither alone is enough.

    ``browser_nonce`` is the value of the :data:`BROWSER_COOKIE` the
    ``/authorize`` response sets in the admin's browser; the state carries its
    hash (``b``), and the callback refuses a browser without the matching
    cookie (login-CSRF: a consent link handed to someone else links nothing).
    """
    if not browser_nonce:
        raise ValueError("a browser nonce is required")
    nonce = secrets.token_urlsafe(24)
    payload = {
        "n": nonce,
        "o": str(org_id),
        "s": org_slug,
        "u": str(user_id),
        "p": provider,
        "c": client_source,
        "b": browser_binding(browser_nonce),
        "e": int(time.time()) + _state_ttl(),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    r = await app_redis.get_redis()
    binding = hashlib.sha256(body.encode()).hexdigest()
    await r.setex(f"{_STATE_PREFIX}{nonce}", _state_ttl(), binding)
    return f"{body}.{_state_mac(body)}"


async def consume_state(state: str) -> dict[str, str]:
    """Verify and burn ``state``. Returns ``{org_id, org_slug, user_id, provider,
    client_source, browser_binding}``; raises :class:`OAuthStateError` otherwise."""
    if not isinstance(state, str) or state.count(".") != 1 or len(state) > 2048:
        raise OAuthStateError("invalid_state")
    body, mac = state.split(".")
    if not hmac.compare_digest(mac, _state_mac(body)):
        raise OAuthStateError("invalid_state")
    try:
        payload = json.loads(_unb64(body))
        nonce = str(payload["n"])
        slug = str(payload["s"])
        expires = int(payload["e"])
    except (ValueError, KeyError, TypeError):
        raise OAuthStateError("invalid_state") from None
    r = await app_redis.get_redis()
    # GETDEL, one round trip: two concurrent callbacks with the same state
    # can't both read the nonce before either deletes it.
    stored = await r.getdel(f"{_STATE_PREFIX}{nonce}")
    if isinstance(stored, bytes):
        stored = stored.decode()
    if expires < int(time.time()):
        raise OAuthStateError("state_expired", org_slug=slug)
    if not stored or not hmac.compare_digest(stored, hashlib.sha256(body.encode()).hexdigest()):
        raise OAuthStateError("state_expired", org_slug=slug)
    return {
        "org_id": str(payload["o"]),
        "org_slug": slug,
        "user_id": str(payload["u"]),
        "provider": str(payload["p"]),
        "client_source": str(payload.get("c") or ""),
        "browser_binding": str(payload.get("b") or ""),
    }


def build_authorize_url(spec: OAuthProviderSpec, creds: ClientCredentials, state: str) -> str:
    params = {
        "client_id": creds.client_id,
        "response_type": "code",
        "redirect_uri": callback_url(),
        "state": state,
        **spec.extra_authorize_params,
    }
    if spec.scopes:  # Blackbaud's SKY API defines none: send no scope at all
        params["scope"] = " ".join(spec.scopes)
    return f"{authorize_endpoint(spec)}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Token endpoint calls
# ---------------------------------------------------------------------------


class _TokenEndpointError(Exception):
    def __init__(self, status: int, invalid_grant: bool):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.invalid_grant = invalid_grant


def token_headers(spec: OAuthProviderSpec, erp_settings: Any) -> dict[str, str]:
    """Accept + the spec's ``extra_token_headers`` (e.g. a subscription key).

    ``erp_settings`` is the resolved block, so a sealed secret the headers need
    (Blackbaud's subscription key) is already in it.
    """
    headers = {"Accept": "application/json"}
    if spec.extra_token_headers is not None:
        extra = spec.extra_token_headers(erp_settings if isinstance(erp_settings, dict) else {})
        headers.update({str(k): str(v) for k, v in (extra or {}).items() if v})
    return headers


async def _token_request(
    spec: OAuthProviderSpec,
    creds: ClientCredentials,
    form: dict[str, str],
    *,
    erp_settings: Any = None,
) -> dict:
    headers = token_headers(spec, erp_settings)
    data = dict(form)
    auth = None
    if spec.token_auth == "body":
        data["client_id"] = creds.client_id
        data["client_secret"] = creds.client_secret
    else:
        auth = httpx.BasicAuth(creds.client_id, creds.client_secret)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(token_endpoint(spec), data=data, headers=headers, auth=auth)
    if resp.status_code != 200:
        invalid_grant = False
        if resp.status_code in (400, 401):
            try:
                invalid_grant = (resp.json() or {}).get("error") == "invalid_grant"
            except Exception:  # noqa: BLE001 — a non-JSON error body is just "not invalid_grant"
                invalid_grant = False
        raise _TokenEndpointError(resp.status_code, invalid_grant)
    body = resp.json()
    if not isinstance(body, dict) or not body.get("access_token"):
        raise _TokenEndpointError(resp.status_code, False)
    return body


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _token_fields(body: dict, *, previous_refresh: str | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    try:
        expires_in = int(body.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    fields: dict[str, Any] = {
        "access_token": str(body["access_token"]),
        # A provider that does not rotate omits refresh_token on refresh; keep ours.
        "refresh_token": str(body.get("refresh_token") or previous_refresh or ""),
        "expires_at": _iso(now + timedelta(seconds=expires_in)),
    }
    rt_expires = body.get("x_refresh_token_expires_in") or body.get("refresh_token_expires_in")
    try:
        if rt_expires:
            fields["refresh_token_expires_at"] = _iso(now + timedelta(seconds=int(rt_expires)))
    except (TypeError, ValueError):
        pass
    return fields


async def exchange_code(
    spec: OAuthProviderSpec, creds: ClientCredentials, code: str, *, erp_settings: Any = None
) -> dict:
    """Exchange an authorization code. Returns the provider's token response.

    Raises :class:`ErpTokenRefreshError` (never the provider's body) on failure.
    """
    try:
        return await _token_request(
            spec,
            creds,
            {"grant_type": "authorization_code", "code": code, "redirect_uri": callback_url()},
            erp_settings=erp_settings,
        )
    except _TokenEndpointError as exc:
        raise ErpTokenRefreshError(spec.key, f"HTTP {exc.status}") from None
    except httpx.HTTPError:
        raise ErpTokenRefreshError(spec.key, "network_error") from None


def _split_token_fields(fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """``_token_fields`` output as (JSONB metadata, sealed-store paths → token)."""
    meta = {k: v for k, v in fields.items() if k not in ("access_token", "refresh_token")}
    sealed = {
        ACCESS_TOKEN_PATH: str(fields.get("access_token") or ""),
        REFRESH_TOKEN_PATH: str(fields.get("refresh_token") or ""),
    }
    return meta, {k: v for k, v in sealed.items() if v}


def new_connection(
    *,
    spec: OAuthProviderSpec,
    token_response: dict,
    external_tenant_id: str,
    org_id: uuid.UUID,
    client_source: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """A fresh consent: the ``settings.erp.oauth`` metadata block, and the
    tokens to seal (``provider_credentials`` path → value)."""
    meta, sealed = _split_token_fields(_token_fields(token_response))
    block = {
        "provider": spec.key,
        **meta,
        "external_tenant_id": external_tenant_id,
        "org_id": str(org_id),
        "connection_id": secrets.token_urlsafe(16),
        "client_source": client_source,
        "connected_at": _iso(datetime.now(UTC)),
    }
    return block, sealed


def with_tokens(oauth: Any, sealed: dict[str, object]) -> dict:
    """The metadata block with the sealed tokens merged in (in memory only)."""
    out = dict(oauth) if isinstance(oauth, dict) else {}
    for path in TOKEN_PATHS:
        value = sealed.get(path)
        if value:
            out[path.split(".", 1)[1]] = value
    return out


async def resolved_erp(org_id: uuid.UUID, org_settings: Any, db) -> tuple[dict, dict]:
    """``(settings.erp resolved for an adapter, the OAuth block WITH its tokens)``.

    One read of the sealed store. Raises ``CredentialCryptoError`` when it
    cannot be opened; the caller decides what that means for it.
    """
    sealed = await provider_credentials.load_secrets(org_id, "erp", db=db)
    public = (org_settings or {}).get("erp") if isinstance(org_settings, dict) else None
    if not isinstance(public, dict):
        public = {}
    erp = provider_credentials.inject_secrets(
        "erp", provider_credentials.strip_secrets("erp", public), sealed
    )
    return erp, with_tokens(public.get("oauth"), sealed)


async def revoke(spec: OAuthProviderSpec, erp_settings: dict, oauth: dict) -> bool:
    """Best-effort revocation of the refresh token. True when the provider
    confirmed it; False when it has no revoke endpoint or the call failed.

    ``erp_settings`` is the resolved block and ``oauth`` carries the tokens
    (:func:`resolved_erp`), both read before the disconnect cleared them.
    """
    url = revoke_endpoint(spec)
    token = oauth.get("refresh_token") or oauth.get("access_token")
    if not url or not token:
        return False
    creds = resolve_client_credentials(spec, erp_settings, source=oauth.get("client_source"))
    headers = token_headers(spec, erp_settings)
    if creds is None:
        return False
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            body = {"token": str(token)}
            resp = await client.post(
                url,
                **({"json": body} if spec.revoke_json else {"data": body}),
                headers=headers,
                auth=httpx.BasicAuth(creds.client_id, creds.client_secret),
            )
        return resp.status_code in (200, 204)
    except httpx.HTTPError:
        return False


# ---------------------------------------------------------------------------
# get_access_token — the adapter-facing call
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


#: Public name for reading a stored ISO-8601 timestamp of the OAuth block
#: (``expires_at``, ``refresh_token_expires_at``) as an aware datetime.
parse_timestamp = _parse_dt


def _is_fresh(oauth: dict, *, rejected_token: str | None = None) -> bool:
    if rejected_token and oauth.get("access_token") == rejected_token:
        return False
    expires_at = _parse_dt(oauth.get("expires_at"))
    if expires_at is None or not oauth.get("access_token"):
        return False
    return expires_at - REFRESH_MARGIN > datetime.now(UTC)


def _identity(spec: OAuthProviderSpec, erp_config: dict) -> tuple[uuid.UUID, str]:
    """The (org_id, connection_id) the adapter's config claims, or raise."""
    oauth = (erp_config or {}).get("oauth") if isinstance(erp_config, dict) else None
    if not isinstance(oauth, dict) or oauth.get("provider") != spec.key:
        raise ErpNotConnectedError(spec.key)
    try:
        org_id = uuid.UUID(str(oauth.get("org_id")))
    except (TypeError, ValueError):
        raise ErpNotConnectedError(spec.key) from None
    connection_id = oauth.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id:
        raise ErpNotConnectedError(spec.key)
    return org_id, connection_id


def _is_connection(spec: OAuthProviderSpec, oauth: Any, connection_id: str) -> bool:
    """Is the stored metadata block the connection to ``spec`` named?"""
    return (
        isinstance(oauth, dict)
        and oauth.get("provider") == spec.key
        and hmac.compare_digest(str(oauth.get("connection_id") or ""), connection_id)
    )


async def _resolved_or_unreadable(
    spec: OAuthProviderSpec, org_id: uuid.UUID, org_settings: Any, session
) -> tuple[dict, dict]:
    try:
        return await resolved_erp(org_id, org_settings, session)
    except CredentialCryptoError:
        logger.warning("erp_oauth: %s sealed credentials could not be opened", spec.key)
        raise ErpCredentialUnreadableError(spec.key) from None


async def _read_stored(
    spec: OAuthProviderSpec, org_id: uuid.UUID, connection_id: str
) -> tuple[dict, dict]:
    """``(settings.erp resolved, the OAuth block with its tokens)`` when the
    stored connection is the one named and still usable."""
    from app.database import control_session_factory
    from app.models.organization import Organization

    async with control_session_factory() as session:
        org_settings = (
            await session.execute(select(Organization.settings).where(Organization.id == org_id))
        ).scalar_one_or_none()
        meta = ((org_settings or {}).get("erp") or {}) if isinstance(org_settings, dict) else {}
        oauth_meta = meta.get("oauth") if isinstance(meta, dict) else None
        if not _is_connection(spec, oauth_meta, connection_id) or oauth_meta.get("needs_reconnect"):
            raise ErpNotConnectedError(spec.key)
        erp, oauth = await _resolved_or_unreadable(spec, org_id, org_settings, session)
        # The two reads are separate statements: a reconnect committing between
        # them would pair this connection's metadata with the NEW consent's
        # tokens (another company's books). Re-read the metadata after the
        # tokens; a changed connection is not the one this adapter was built for.
        recheck = (
            await session.execute(select(Organization.settings).where(Organization.id == org_id))
        ).scalar_one_or_none()
        recheck_erp = (recheck or {}).get("erp") if isinstance(recheck, dict) else None
        recheck_oauth = recheck_erp.get("oauth") if isinstance(recheck_erp, dict) else None
        if not _is_connection(spec, recheck_oauth, connection_id):
            raise ErpNotConnectedError(spec.key)
    if not oauth.get("refresh_token"):
        raise ErpNotConnectedError(spec.key)
    return erp, oauth


async def _compare_and_swap(
    spec: OAuthProviderSpec,
    org_id: uuid.UUID,
    connection_id: str,
    spent_refresh_token: str,
    updates: dict[str, Any],
) -> dict:
    """Write ``updates`` iff the sealed refresh token is still
    ``spent_refresh_token``. Returns the block (with tokens) now stored either way.

    Tokens in ``updates`` go to the sealed store through
    ``provider_credentials.update_secrets``; the rest is metadata in
    ``settings.erp.oauth``. Both under the org row lock, in one transaction.
    A token rotation is the system keeping a connection alive, not a change of
    credential by a person, so it writes no audit row; consent and disconnect
    do.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.database import control_session_factory
    from app.models.organization import Organization

    async with control_session_factory() as session:
        org = (
            await session.execute(
                select(Organization).where(Organization.id == org_id).with_for_update()
            )
        ).scalar_one_or_none()
        if org is None:
            raise ErpNotConnectedError(spec.key)
        current = dict(org.settings or {})
        erp = dict(current.get("erp") or {})
        if not _is_connection(spec, erp.get("oauth"), connection_id):
            await session.rollback()
            raise ErpNotConnectedError(spec.key)
        try:
            _, stored = await _resolved_or_unreadable(spec, org_id, current, session)
        except ErpCredentialUnreadableError:
            await session.rollback()
            raise
        if stored.get("refresh_token") != spent_refresh_token:
            # Someone else already rotated it; theirs is the live token.
            await session.rollback()
            return stored
        meta, sealed = _split_token_fields(updates)
        try:
            if sealed:
                await provider_credentials.update_secrets(session, org_id, "erp", sealed, [])
        except CredentialCryptoError:
            await session.rollback()
            logger.warning("erp_oauth: %s rotated token could not be sealed", spec.key)
            raise ErpCredentialUnreadableError(spec.key) from None
        erp["oauth"] = {**erp["oauth"], **meta}
        current["erp"] = erp
        org.settings = current
        flag_modified(org, "settings")
        await session.commit()
        return {**stored, **updates}


async def _acquire_lock(key: str) -> str | None:
    token = secrets.token_hex(16)
    r = await app_redis.get_redis()
    ok = await r.set(key, token, nx=True, ex=_LOCK_TTL_SECONDS)
    return token if ok else None


async def _release_lock(key: str, token: str) -> None:
    # GET-then-DEL is not atomic; the compare-and-swap write above is what
    # keeps a release racing a lock expiry harmless.
    r = await app_redis.get_redis()
    try:
        held = await r.get(key)
        if isinstance(held, bytes):
            held = held.decode()
        if held == token:
            await r.delete(key)
    except Exception:  # noqa: BLE001 — the lock's TTL reaps it
        logger.warning("erp_oauth: lock release failed; TTL will reap it")


def realm_claim_lock_key(provider: str, external_tenant_id: str) -> str:
    return f"{_REALM_LOCK_PREFIX}{provider}:{external_tenant_id}"


@asynccontextmanager
async def realm_claim(provider: str, external_tenant_id: str) -> AsyncIterator[bool]:
    """Serialise "is this company linked elsewhere? → link it" across tenants.

    The callback's uniqueness check runs under ITS org's row lock, which does
    not stop a second tenant's callback for the same provider company running
    the same check at the same moment: both would see "unclaimed" and both
    would link it. This is a Redis lock keyed on ``(provider, company id)``
    (``SET NX EX``) held from the check through the commit. Yields False when
    another connect holds it — the caller refuses as ``already_linked`` rather
    than waiting, since the holder is about to claim the company. Released in
    ``finally``; the TTL reaps a crashed holder.
    """
    key = realm_claim_lock_key(provider, external_tenant_id)
    token = await _acquire_lock(key)
    if token is None:
        yield False
        return
    try:
        yield True
    finally:
        await _release_lock(key, token)


#: Why a connection needs the admin's consent again (``settings.erp.oauth.
#: reconnect_reason``), recorded when the provider refuses the refresh token:
#: past the expiry it last reported, or refused before it (revoked at the
#: provider, or a lifetime cap it never reported).
RECONNECT_EXPIRED = "refresh_token_expired"
RECONNECT_REVOKED = "refresh_token_revoked"


def reconnect_reason(oauth: dict, *, now: datetime | None = None) -> str:
    """:data:`RECONNECT_EXPIRED` when the stored refresh-token expiry has
    passed, else :data:`RECONNECT_REVOKED`."""
    expires = _parse_dt(oauth.get("refresh_token_expires_at"))
    moment = now or datetime.now(UTC)
    return RECONNECT_EXPIRED if expires is not None and expires <= moment else RECONNECT_REVOKED


async def update_connection_metadata(
    spec_key: str, org_id: uuid.UUID, connection_id: str, updates: dict[str, Any]
) -> bool:
    """Merge NON-secret ``updates`` into ``settings.erp.oauth`` — only while
    the stored connection is still ``connection_id`` for ``spec_key``. Under
    the org row lock, like every settings writer. Returns whether it wrote.

    For bookkeeping the connection-health sweep keeps (which expiry notice was
    sent); never a token, and never a key the refresher or the connect flow
    owns.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.database import control_session_factory
    from app.models.organization import Organization

    forbidden = set(updates) & {
        "provider",
        "connection_id",
        "org_id",
        "external_tenant_id",
        "access_token",
        "refresh_token",
        "expires_at",
        "refresh_token_expires_at",
        "client_source",
        "needs_reconnect",
    }
    if forbidden:
        raise ValueError(f"not connection metadata this writer may set: {sorted(forbidden)}")
    async with control_session_factory() as session:
        org = (
            await session.execute(
                select(Organization).where(Organization.id == org_id).with_for_update()
            )
        ).scalar_one_or_none()
        current = dict((org.settings if org else None) or {})
        erp = dict(current.get("erp") or {})
        oauth = erp.get("oauth")
        if (
            org is None
            or not isinstance(oauth, dict)
            or oauth.get("provider") != spec_key
            or not hmac.compare_digest(str(oauth.get("connection_id") or ""), connection_id)
        ):
            await session.rollback()
            return False
        erp["oauth"] = {**oauth, **updates}
        current["erp"] = erp
        org.settings = current
        flag_modified(org, "settings")
        await session.commit()
        return True


async def get_access_token(
    spec: OAuthProviderSpec,
    erp_config: dict,
    *,
    rejected_token: str | None = None,
    force_refresh: bool = False,
) -> str:
    """Return a valid access token for ``erp_config``'s connection to ``spec``.

    Reads the stored connection (never the config's copy of the tokens),
    refreshes it when it expires within :data:`REFRESH_MARGIN` — or when
    ``rejected_token`` is still the stored one, i.e. the provider just answered
    401 to it — and persists the rotated refresh token. See the module
    docstring for the locking design.

    Raises :class:`ErpNotConnectedError` when there is no usable connection
    (and marks it ``needs_reconnect`` when the provider refused the refresh
    token), or :class:`ErpTokenRefreshError` when the provider was unreachable.
    """
    import asyncio

    org_id, connection_id = _identity(spec, erp_config)
    _, oauth = await _read_stored(spec, org_id, connection_id)
    if force_refresh:
        # Refresh now even though the access token is fresh — the
        # connection-health sweep keeping a quiet connection's refresh token
        # alive (§295). Treating the stored access token as the rejected one
        # reuses the lock and compare-and-swap below unchanged: a refresh a
        # concurrent caller already landed satisfies this one too.
        rejected_token = str(oauth.get("access_token") or "") or None
    if _is_fresh(oauth, rejected_token=rejected_token):
        return str(oauth["access_token"])

    lock_key = f"{_LOCK_PREFIX}{org_id}:{spec.key}"
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        lock = await _acquire_lock(lock_key)
        if lock is not None:
            break
        if time.monotonic() > deadline:
            raise ErpTokenRefreshError(spec.key, "refresh_lock_timeout")
        await asyncio.sleep(_LOCK_POLL_SECONDS)
        _, oauth = await _read_stored(spec, org_id, connection_id)
        if _is_fresh(oauth, rejected_token=rejected_token):
            return str(oauth["access_token"])

    # From here — the provider's rotation and its persistence — runs as ONE
    # shielded task. Intuit rotates the refresh token on every use, so a caller
    # cancelled by its own timeout (the dispatch-time bill check, decisions
    # §296) after Intuit answered but before the new token was stored would
    # strand the connection. Shielded, the refresh completes and persists, and
    # releases its lock, whatever happens to the caller.
    return await _shielded(
        _refresh_holding_lock(spec, org_id, connection_id, rejected_token, lock_key, lock)
    )


#: Strong references to in-flight shielded refreshes (asyncio holds only a
#: weak one), each retrieving its own exception so a refresh whose caller gave
#: up never logs "Task exception was never retrieved".
_refresh_tasks: set = set()


async def _shielded(coro):
    import asyncio

    task = asyncio.ensure_future(coro)
    _refresh_tasks.add(task)

    def _done(t) -> None:
        _refresh_tasks.discard(t)
        if not t.cancelled():
            t.exception()

    task.add_done_callback(_done)
    return await asyncio.shield(task)


async def _refresh_holding_lock(
    spec: OAuthProviderSpec,
    org_id: uuid.UUID,
    connection_id: str,
    rejected_token: str | None,
    lock_key: str,
    lock: str,
) -> str:
    """The refresh :func:`get_access_token` runs once it holds the lock."""
    try:
        # Re-read under the lock: the previous holder may have just refreshed.
        erp, oauth = await _read_stored(spec, org_id, connection_id)
        if _is_fresh(oauth, rejected_token=rejected_token):
            return str(oauth["access_token"])

        creds = resolve_client_credentials(spec, erp, source=oauth.get("client_source"))
        if creds is None:
            raise ErpNotConnectedError(spec.key)
        spent = str(oauth["refresh_token"])
        try:
            body = await _token_request(
                spec,
                creds,
                {"grant_type": "refresh_token", "refresh_token": spent},
                erp_settings=erp,
            )
        except _TokenEndpointError as exc:
            if exc.invalid_grant:
                # Revoked at the provider, or past its lifetime. Surface it as
                # "reconnect required" instead of retrying a dead token forever.
                reason = reconnect_reason(oauth)
                await _compare_and_swap(
                    spec,
                    org_id,
                    connection_id,
                    spent,
                    {"needs_reconnect": True, "reconnect_reason": reason},
                )
                # Rotation is routine and unaudited; a connection going down is
                # not. Best-effort, after the commit.
                from app.services.audit_dispatch import dispatch_auth_audit

                await dispatch_auth_audit(
                    organization_id=org_id,
                    actor_id=None,
                    action="organization.erp_reconnect_required",
                    entity_id=org_id,
                    entity_type="organization",
                    details={"provider": spec.key, "reason": reason},
                )
                logger.warning("erp_oauth: %s refresh refused for org %s", spec.key, org_id)
                raise ErpNotConnectedError(spec.key) from None
            raise ErpTokenRefreshError(spec.key, f"HTTP {exc.status}") from None
        except httpx.HTTPError:
            raise ErpTokenRefreshError(spec.key, "network_error") from None

        stored = await _compare_and_swap(
            spec, org_id, connection_id, spent, _token_fields(body, previous_refresh=spent)
        )
        return str(stored["access_token"])
    finally:
        await _release_lock(lock_key, lock)
