"""Base class for ERPs connected through an OAuth 2.0 authorization-code grant.

QuickBooks Online, Xero and Sage Business Cloud Accounting have no
client-credentials grant: the customer's admin has to consent in the
provider's own UI, and we hold a rotating refresh token afterwards. That flow
is one reusable piece (``services/erp_oauth``), not three copies inside the
adapters (``backend/docs/quickbooks-online-adapter.md`` § Phase 1).

An adapter on this flow subclasses :class:`OAuthErpAdapter`, declares its
:class:`OAuthProviderSpec`, and calls ``await self.access_token()`` for a
bearer token. It never reads, refreshes or stores tokens itself.

Connection metadata lives in ``settings.erp.oauth``::

    {
        "provider": "<OAuthProviderSpec.key>",
        "expires_at": "<ISO-8601 UTC>",
        "external_tenant_id": "...",  # QBO realmId / Xero tenantId / Sage business id
        # plus org_id, connection_id, client_source, connected_at,
        # refresh_token_expires_at, needs_reconnect: see services/erp_oauth
    }

The tokens are sealed in ``provider_credentials`` and never reach an adapter's
config. Only ``services/erp_oauth`` writes either half. An adapter reads
``external_tenant_id`` through :meth:`OAuthErpAdapter.external_tenant_id`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import ClassVar

import httpx

from app.services.erp_adapters.base import ErpAdapter


@dataclass(frozen=True)
class OAuthProviderSpec:
    """Static description of one provider's authorization-code endpoints.

    ``client_id_setting`` / ``client_secret_setting`` name the ``FEOH_``
    attributes on ``app.config.settings`` holding the platform app's
    credentials (one app serves every tenant). A tenant may instead bring its
    own app by saving ``client_id`` / ``client_secret`` in ``settings.erp``;
    ``services/erp_oauth`` prefers those when both are set. Neither present
    means the provider is unavailable and fails closed (no hardcoded fallback).
    """

    key: str
    display_name: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id_setting: str
    client_secret_setting: str
    #: Extra query parameters on the authorize redirect (e.g. Sage's
    #: ``filter=apiv3.1``). Never secrets.
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    #: The provider's token-revocation endpoint, or None when it has none.
    #: Disconnect calls it best-effort before clearing local state.
    revoke_url: str | None = None
    #: How the revoke endpoint takes the token: a form body (``token=…``,
    #: RFC 7009 — Xero, Sage) or, when True, a JSON object
    #: (``{"token": …}``), which is what Intuit documents. A provider sent the
    #: wrong one can answer 4xx, and Disconnect then leaves the grant live.
    revoke_json: bool = False
    #: How the token endpoint takes the client credentials: ``"basic"`` (HTTP
    #: Basic auth — Intuit, Xero) or ``"body"`` (form fields — Sage).
    token_auth: str = "basic"
    #: The callback query parameter carrying the provider's company id
    #: (Intuit's ``realmId``). None when the id has to be looked up after the
    #: token exchange instead — override
    #: :meth:`OAuthErpAdapter.resolve_external_tenant_id` for that.
    external_tenant_id_param: str | None = None
    #: The token-response field carrying the company id (Blackbaud's
    #: ``environment_id``). Checked after ``external_tenant_id_param``. A
    #: provider needing an API call instead (Xero ``GET /connections``, Sage's
    #: business lookup) overrides ``resolve_external_tenant_id``.
    external_tenant_id_token_field: str | None = None
    #: Extra headers on every token-endpoint call (code exchange, refresh,
    #: revoke), built from the tenant's stored ``settings.erp`` — e.g.
    #: Blackbaud's ``Bb-Api-Subscription-Key``. Return ``{}`` for none. The
    #: values may be secrets; ``services/erp_oauth`` never logs them.
    extra_token_headers: Callable[[dict], dict[str, str]] | None = None
    #: Names of OPERATOR-controlled ``FEOH_`` settings that, when non-empty,
    #: replace ``authorize_url`` / ``token_url`` / ``revoke_url`` — so local dev
    #: and e2e can point the whole flow at fake-erp, the way
    #: ``erp_d365_token_url`` does. Trusted (env-level, not tenant config).
    authorize_url_setting: str | None = None
    token_url_setting: str | None = None
    revoke_url_setting: str | None = None


class RefreshOnUnauthorized(httpx.Auth):
    """Replay a request once with a refreshed token when the provider answers 401.

    A provider can reject a stored access token before its recorded expiry (the
    customer re-consented elsewhere, an admin revoked a session). Refreshing only
    inside the expiry margin then fails every call until the old token would
    have expired. This flow reads the bearer the request carried, asks
    ``erp_oauth`` to refresh only if that token is still the stored one (a
    concurrent caller may already have), and retries once with the new one.

    When the refresh itself fails, its exception propagates exactly as it would
    from ``access_token()``: a revoked consent (``erp_oauth`` has then marked the
    connection ``needs_reconnect``) is ``ErpNotConnectedError``, which
    ``services/erp._is_final`` treats as final, and a provider outage is the
    retryable ``ErpTokenRefreshError``. Swallowing either into the provider's 401
    would misclassify "not connected" as a retryable HTTP failure and hide the
    cause. A 401 means the provider did not process the request, so raising here
    cannot orphan a write.

    An adapter builds its headers once per client block, so after a refresh the
    remaining requests in that block still carry the rejected bearer. The flow
    remembers each replacement and swaps it in before sending, instead of
    spending a 401 and a token lookup on every one of them.
    """

    def __init__(self, adapter: OAuthErpAdapter) -> None:
        self._adapter = adapter
        self._replaced: dict[str, str] = {}

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        sent = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if sent in self._replaced:
            sent = self._replaced[sent]
            request.headers["Authorization"] = f"Bearer {sent}"
        response = yield request
        if response.status_code != 401 or not sent:
            return
        from app.services import erp_oauth

        token = await erp_oauth.get_access_token(
            self._adapter.oauth_provider, self._adapter.config, rejected_token=sent
        )
        if token == sent:
            return
        self._replaced[sent] = token
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class OAuthErpAdapter(ErpAdapter):
    """An ERP adapter whose credentials come from a consented OAuth connection."""

    oauth_provider: ClassVar[OAuthProviderSpec]

    def oauth_client(self, *, timeout: float) -> httpx.AsyncClient:
        """An HTTP client for this connection's API calls: a 401 refreshes the
        token and replays the request once (:class:`RefreshOnUnauthorized`)."""
        return httpx.AsyncClient(timeout=timeout, auth=RefreshOnUnauthorized(self))

    async def access_token(self) -> str:
        """A bearer token valid for at least the next request.

        Refreshes and persists a rotated refresh token when needed. Raises
        ``erp_oauth.ErpNotConnectedError`` when the org never completed the
        consent flow or the provider revoked it.
        """
        from app.services import erp_oauth

        return await erp_oauth.get_access_token(self.oauth_provider, self.config)

    @classmethod
    async def resolve_external_tenant_id(
        cls,
        *,
        access_token: str,
        token_response: dict,
        callback_params: dict[str, str],
        erp_settings: dict | None = None,
    ) -> str | None:
        """The provider-side company id for a fresh consent, or None.

        Called once by the OAuth callback after the code exchange. The default
        reads the callback query parameter named by
        ``oauth_provider.external_tenant_id_param`` (QuickBooks' ``realmId``),
        then the token-response field ``external_tenant_id_token_field``
        (Blackbaud's ``environment_id``).
        A provider that only reveals it through an API call (Xero's
        ``GET /connections``, Sage's ``/businesses``) overrides this, and so
        does one whose callback parameter is unsigned and must be confirmed
        under the new token (QuickBooks). None refuses the connection
        (``erp_error=no_external_tenant``).

        ``erp_settings`` is the org's resolved ``settings.erp`` as saved before
        the consent (QuickBooks reads its ``environment`` to pick the API host).
        """
        spec = cls.oauth_provider
        if spec.external_tenant_id_param:
            value = str(callback_params.get(spec.external_tenant_id_param) or "").strip()
            if value:
                return value
        if spec.external_tenant_id_token_field:
            value = str(token_response.get(spec.external_tenant_id_token_field) or "").strip()
            if value:
                return value
        return None

    def external_tenant_id(self) -> str:
        """The provider-side company id captured at consent (QBO ``realmId`` etc.)."""
        from app.services import erp_oauth

        oauth = self.config.get("oauth") or {}
        value = oauth.get("external_tenant_id")
        if not value:
            raise erp_oauth.ErpNotConnectedError(self.oauth_provider.key)
        return str(value)
