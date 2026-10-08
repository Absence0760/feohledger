"""Base class for ERPs connected through an OAuth 2.0 authorization-code grant.

QuickBooks Online, Xero and Sage Business Cloud Accounting have no
client-credentials grant: the customer's admin has to consent in the
provider's own UI, and we hold a rotating refresh token afterwards. That flow
is one reusable piece (``services/erp_oauth``), not three copies inside the
adapters (``backend/docs/quickbooks-online-adapter.md`` § Phase 1).

An adapter on this flow subclasses :class:`OAuthErpAdapter`, declares its
:class:`OAuthProviderSpec`, and calls ``await self.access_token()`` for a
bearer token. It never reads, refreshes or stores tokens itself.

Connection state lives in ``settings.erp.oauth``::

    {
        "access_token": "...",
        "refresh_token": "...",
        "expires_at": "<ISO-8601 UTC>",
        "external_tenant_id": "...",  # QBO realmId / Xero tenantId / Sage business id
    }

Only ``services/erp_oauth`` writes that block. An adapter reads
``external_tenant_id`` through :meth:`OAuthErpAdapter.external_tenant_id`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

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


class OAuthErpAdapter(ErpAdapter):
    """An ERP adapter whose credentials come from a consented OAuth connection."""

    oauth_provider: ClassVar[OAuthProviderSpec]

    async def access_token(self) -> str:
        """A bearer token valid for at least the next request.

        Refreshes and persists a rotated refresh token when needed. Raises
        ``erp_oauth.ErpNotConnectedError`` when the org never completed the
        consent flow or the provider revoked it.
        """
        from app.services import erp_oauth

        return await erp_oauth.get_access_token(self.oauth_provider, self.config)

    def external_tenant_id(self) -> str:
        """The provider-side company id captured at consent (QBO ``realmId`` etc.)."""
        from app.services import erp_oauth

        oauth = self.config.get("oauth") or {}
        value = oauth.get("external_tenant_id")
        if not value:
            raise erp_oauth.ErpNotConnectedError(self.oauth_provider.key)
        return str(value)
