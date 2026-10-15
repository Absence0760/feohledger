"""Commerce / inventory platform adapters (decisions §273).

Pushes received quantities into the business's own commerce platform; see
``base.py`` for the interface and ``backend/docs/commerce-integration.md`` for
how to add a provider.
"""

from __future__ import annotations

from typing import Any

from app.services.commerce_adapters.base import (
    CommerceAdapter,
    CommerceAuthError,
    CommerceConfigError,
    CommerceError,
    CommerceItemRef,
    CommerceLocation,
    CommerceProviderSpec,
    CommerceRateLimitedError,
    CommerceRejectedError,
    CommerceTransientError,
    CommerceUnknownLocationError,
    CommerceUnmappedSkuError,
    InventoryAdjustment,
    InventoryAdjustmentResult,
)
from app.services.commerce_adapters.registry import (
    MOCK_PROVIDER,
    UnknownCommerceAdapterError,
    list_available_adapters,
    register_commerce_adapter,
)


def get_commerce_adapter(config: dict[str, Any]) -> CommerceAdapter:
    """The adapter for a RESOLVED ``settings.commerce`` block.

    ``config`` is what ``provider_credentials.resolve_block(…, "commerce")`` /
    ``provider_config(org, "commerce")`` returns: the configuration with the
    sealed secrets merged back in (decisions §266). Raises
    :class:`UnknownCommerceAdapterError` for an unregistered provider — never a
    fall back to ``mock``.
    """
    from app.services.commerce_adapters.registry import adapter_class

    cls = adapter_class((config or {}).get("provider"))
    return cls(dict(config or {}))


__all__ = [
    "MOCK_PROVIDER",
    "CommerceAdapter",
    "CommerceAuthError",
    "CommerceConfigError",
    "CommerceError",
    "CommerceItemRef",
    "CommerceLocation",
    "CommerceProviderSpec",
    "CommerceRateLimitedError",
    "CommerceRejectedError",
    "CommerceTransientError",
    "CommerceUnknownLocationError",
    "CommerceUnmappedSkuError",
    "InventoryAdjustment",
    "InventoryAdjustmentResult",
    "UnknownCommerceAdapterError",
    "get_commerce_adapter",
    "list_available_adapters",
    "register_commerce_adapter",
]
