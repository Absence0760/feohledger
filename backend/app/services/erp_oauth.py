"""OAuth 2.0 authorization-code connections for ERPs.

QuickBooks Online, Xero and Sage Business Cloud Accounting offer no
client-credentials grant: the customer's admin consents in the provider's UI,
and we hold a rotating refresh token afterwards. This module is that flow, once,
for every such adapter (``erp_adapters/oauth_base.OAuthErpAdapter``):

* :func:`build_authorize_url` / :func:`consume_state` — the signed, single-use
  ``state`` that carries org + user + provider across the provider's redirect;
* :func:`exchange_code` — the callback's code → token exchange;
* :func:`get_access_token` — what adapters call; refreshes and persists;
* :func:`revoke` — best-effort revocation on disconnect.

The routes live in ``app/api/erp_oauth.py``.

Stored state — ``Organization.settings.erp.oauth``
--------------------------------------------------
::

    {
        "provider": "quickbooks_online",
        "access_token": "...", "refresh_token": "...",
        "expires_at": "<ISO-8601 UTC>",
        "refresh_token_expires_at": "<ISO-8601 UTC>" | absent,
        "external_tenant_id": "<realmId / Xero tenantId / Sage business id>",
        "org_id": "<uuid>", "connection_id": "<random>",
        "client_source": "tenant" | "platform",
        "connected_at": "<ISO-8601 UTC>",
        "needs_reconnect": true | absent,
    }

Only this module writes it (the callback, the refresher, disconnect).
Every read sees it masked to ``{"connected": bool}``
(``erp_adapters/catalog.mask_erp_config``), and a settings save never takes
it from the request and keeps the stored one (``catalog.merge_erp_update``),
even across a switch of ERP type: the block names its ``provider``, and
:func:`get_access_token` refuses any other.

How the refresher finds the org — and why it can't be pointed at another one
---------------------------------------------------------------------------
An adapter is built from ``settings.erp`` alone and holds no DB session, so the
block carries ``org_id``. The refresher never trusts the tokens in the adapter's
config: it re-reads the org row and uses the **stored** block. Since an admin can
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
   short ``SELECT … FOR UPDATE`` (``tenant.lock_organization``-style), and only
   if the stored refresh token is still the one this refresh spent. If it
   changed (the lock expired under a hung call, or a reconnect landed), the
   stored value wins and is used. This is what makes the lock's best-effort
   release (GET-then-DEL, no Lua) safe.

Client credentials
------------------
A tenant may bring its own provider app: ``settings.erp.client_id`` /
``client_secret`` while ``settings.erp.type`` names the provider. Otherwise the
platform app's ``FEOH_`` settings named by the spec. Neither → the provider is
unavailable (fail closed, no fallback). The source used at consent is recorded
as ``client_source`` and the refresher uses the same one: a token issued to one
app cannot be refreshed with another's credentials.

Nothing here logs a token, a code, or the provider's response body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from app import redis as app_redis
from app.config import settings
from app.services.erp_adapters.oauth_base import OAuthProviderSpec

logger = logging.getLogger(__name__)

#: Every provider an adapter has declared, keyed by ``OAuthProviderSpec.key``.
#: The connect routes look providers up here. Populated by
#: :func:`register_oauth_provider` at adapter import time.
OAUTH_PROVIDERS: dict[str, OAuthProviderSpec] = {}

#: The one redirect URI every provider app registers (path under
#: ``FEOH_API_PUBLIC_URL``). The tenant comes back in ``state``, not the URL.
CALLBACK_PATH = "/api/erp/oauth/callback"

#: Refresh when the access token has less than this left. Covers clock skew and
#: a request that starts just before expiry.
REFRESH_MARGIN = timedelta(seconds=120)

_STATE_PREFIX = "erp:oauth:state:"
_LOCK_PREFIX = "erp:oauth:refresh-lock:"
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


async def create_state(
    *, org_id: uuid.UUID, org_slug: str, user_id: uuid.UUID, provider: str, client_source: str
) -> str:
    """Mint a signed ``state`` and record its nonce in Redis (single use).

    The signature stops forgery (the callback is public); the Redis nonce makes
    it single-use and is what an expired or replayed state fails on. Both carry
    the same binding, so neither alone is enough.
    """
    nonce = secrets.token_urlsafe(24)
    payload = {
        "n": nonce,
        "o": str(org_id),
        "s": org_slug,
        "u": str(user_id),
        "p": provider,
        "c": client_source,
        "e": int(time.time()) + _state_ttl(),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    r = await app_redis.get_redis()
    binding = hashlib.sha256(body.encode()).hexdigest()
    await r.setex(f"{_STATE_PREFIX}{nonce}", _state_ttl(), binding)
    return f"{body}.{_state_mac(body)}"


async def consume_state(state: str) -> dict[str, str]:
    """Verify and burn ``state``. Returns ``{org_id, org_slug, user_id, provider,
    client_source}``; raises :class:`OAuthStateError` otherwise."""
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
    """Accept + the spec's ``extra_token_headers`` (e.g. a subscription key)."""
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


def new_connection_block(
    *,
    spec: OAuthProviderSpec,
    token_response: dict,
    external_tenant_id: str,
    org_id: uuid.UUID,
    client_source: str,
) -> dict[str, Any]:
    """The ``settings.erp.oauth`` block for a fresh consent."""
    return {
        "provider": spec.key,
        **_token_fields(token_response),
        "external_tenant_id": external_tenant_id,
        "org_id": str(org_id),
        "connection_id": secrets.token_urlsafe(16),
        "client_source": client_source,
        "connected_at": _iso(datetime.now(UTC)),
    }


async def revoke(spec: OAuthProviderSpec, erp_settings: dict, oauth: dict) -> bool:
    """Best-effort revocation of the stored refresh token. True when the provider
    confirmed it; False when it has no revoke endpoint or the call failed."""
    url = revoke_endpoint(spec)
    token = oauth.get("refresh_token") or oauth.get("access_token")
    if not url or not token:
        return False
    creds = resolve_client_credentials(spec, erp_settings, source=oauth.get("client_source"))
    if creds is None:
        return False
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                url,
                data={"token": str(token)},
                headers=token_headers(spec, erp_settings),
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


def _stored_block(
    spec: OAuthProviderSpec, org_settings: Any, connection_id: str
) -> tuple[dict, dict]:
    """``(settings.erp, settings.erp.oauth)`` when it is the connection named."""
    erp = (org_settings or {}).get("erp") if isinstance(org_settings, dict) else None
    oauth = erp.get("oauth") if isinstance(erp, dict) else None
    if (
        not isinstance(oauth, dict)
        or oauth.get("provider") != spec.key
        or not hmac.compare_digest(str(oauth.get("connection_id") or ""), connection_id)
        or oauth.get("needs_reconnect")
        or not oauth.get("refresh_token")
    ):
        raise ErpNotConnectedError(spec.key)
    return erp, oauth


async def _read_stored(
    spec: OAuthProviderSpec, org_id: uuid.UUID, connection_id: str
) -> tuple[dict, dict]:
    from app.database import control_session_factory
    from app.models.organization import Organization

    async with control_session_factory() as session:
        org_settings = (
            await session.execute(select(Organization.settings).where(Organization.id == org_id))
        ).scalar_one_or_none()
    return _stored_block(spec, org_settings, connection_id)


async def _compare_and_swap(
    spec: OAuthProviderSpec,
    org_id: uuid.UUID,
    connection_id: str,
    spent_refresh_token: str,
    updates: dict[str, Any],
) -> dict:
    """Write ``updates`` into the stored block iff it still holds
    ``spent_refresh_token``. Returns the block now stored either way."""
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
        oauth = erp.get("oauth")
        if (
            not isinstance(oauth, dict)
            or oauth.get("provider") != spec.key
            or not hmac.compare_digest(str(oauth.get("connection_id") or ""), connection_id)
        ):
            await session.rollback()
            raise ErpNotConnectedError(spec.key)
        if oauth.get("refresh_token") != spent_refresh_token:
            # Someone else already rotated it; theirs is the live token.
            await session.rollback()
            return dict(oauth)
        new_oauth = {**oauth, **updates}
        erp["oauth"] = new_oauth
        current["erp"] = erp
        org.settings = current
        flag_modified(org, "settings")
        await session.commit()
        return new_oauth


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
        logger.warning("erp_oauth: refresh-lock release failed; TTL will reap it")


async def get_access_token(
    spec: OAuthProviderSpec, erp_config: dict, *, rejected_token: str | None = None
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
                await _compare_and_swap(
                    spec, org_id, connection_id, spent, {"needs_reconnect": True}
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
