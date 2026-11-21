"""Billing adapter dispatcher — registry + selection.

Selection order (mirrors the email / peppol / qms dispatchers):
  1. an explicit ``provider`` argument (the per-org override the caller resolved
     from ``Organization.settings.billing.provider``), else
  2. ``settings.billing_provider`` (``FEOH_BILLING_PROVIDER``, default ``mock``).

An unknown provider falls back to ``mock`` (local-first: a bad config can never
make billing read paths 500). The real ``stripe_billing`` adapter fails CLOSED
inside its own methods when no key is configured — selecting it is safe.
"""

from __future__ import annotations

from app.config import settings
from app.services.billing_adapters.base import BillingAdapter

_ADAPTER_REGISTRY: dict[str, type[BillingAdapter]] = {}


def register_billing_adapter(provider: str):
    """Decorator to register a billing adapter class under ``provider``."""

    def wrapper(cls: type[BillingAdapter]):
        _ADAPTER_REGISTRY[provider] = cls
        return cls

    return wrapper


def get_billing_adapter(provider: str | None = None) -> BillingAdapter:
    """Build the selected billing adapter. Defaults to ``mock`` (local-first)."""
    selected = provider or settings.billing_provider or "mock"

    adapter_cls = _ADAPTER_REGISTRY.get(selected)
    if adapter_cls is None:
        adapter_cls = _ADAPTER_REGISTRY.get("mock")
        if adapter_cls is None:  # pragma: no cover — mock is always registered
            raise ValueError(f"No billing adapter registered for '{selected}'")

    config = {
        "stripe_api_key": settings.billing_stripe_api_key,
        "stripe_webhook_secret": settings.billing_stripe_webhook_secret,
        "stripe_api_base": settings.billing_stripe_api_base,
        "stripe_api_version": settings.billing_stripe_api_version,
    }
    return adapter_cls(config)


def list_available_providers() -> list[str]:
    return sorted(_ADAPTER_REGISTRY.keys())
