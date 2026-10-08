"""OAuth 2.0 authorization-code connections for ERPs (contract; not yet implemented).

The interface ``erp_adapters/oauth_base.OAuthErpAdapter`` calls. The
implementation (authorize redirect, callback, token exchange, refresh with
rotated-token persistence, disconnect) lands with the QuickBooks Online adapter;
see ``backend/docs/quickbooks-online-adapter.md`` § Phase 1.
"""

from __future__ import annotations

from app.services.erp_adapters.oauth_base import OAuthProviderSpec

#: Every provider an adapter has declared, keyed by ``OAuthProviderSpec.key``.
#: The connect routes look providers up here. Populated by
#: :func:`register_oauth_provider` at adapter import time.
OAUTH_PROVIDERS: dict[str, OAuthProviderSpec] = {}


class ErpNotConnectedError(RuntimeError):
    """The org has no usable OAuth connection to this provider.

    Never consented, revoked, or the refresh token expired. The message is a
    fixed string carrying only the provider key: it can reach the append-only
    ``invoice.erp_failed`` audit row the way ``ErpPostResult.message`` does.
    """

    def __init__(self, provider_key: str):
        self.provider_key = provider_key
        super().__init__(f"{provider_key}: not connected (reconnect the ERP in Organization → ERP)")


def register_oauth_provider(spec: OAuthProviderSpec) -> OAuthProviderSpec:
    """Record ``spec`` so the connect routes can find it. Returns it unchanged."""
    OAUTH_PROVIDERS[spec.key] = spec
    return spec


async def get_access_token(spec: OAuthProviderSpec, erp_config: dict) -> str:
    """Return a valid access token for ``erp_config``'s connection to ``spec``."""
    raise NotImplementedError("erp_oauth.get_access_token lands with the QuickBooks adapter")
