"""Billing-provider adapters — unified interface for provider plugins.

``mock`` is the local-first DEFAULT (deterministic, no network/credential);
``stripe_billing`` is a fail-closed skeleton (live key via sops, no fallback).
Select via ``FEOH_BILLING_PROVIDER`` or the per-org
``Organization.settings.billing.provider`` override. See ``backend/docs/billing.md``.
"""

# Import adapters so they register themselves with the dispatcher.
from app.services.billing_adapters import mock_adapter as _mock  # noqa: F401
from app.services.billing_adapters import stripe_billing as _stripe  # noqa: F401
from app.services.billing_adapters.base import (
    AI_INVOICE_OVERAGE_EVENT,
    BillingAdapter,
    BillingWebhookEvent,
    CreateSubscriptionRequest,
    MeterEvent,
    ProviderInvoice,
    ProviderPaymentMethod,
    ProviderSetupIntent,
    ProviderSubscription,
    UsageReport,
)
from app.services.billing_adapters.dispatcher import (
    get_billing_adapter,
    list_available_providers,
    register_billing_adapter,
)

__all__ = [
    "AI_INVOICE_OVERAGE_EVENT",
    "BillingAdapter",
    "BillingWebhookEvent",
    "CreateSubscriptionRequest",
    "MeterEvent",
    "ProviderInvoice",
    "ProviderPaymentMethod",
    "ProviderSetupIntent",
    "ProviderSubscription",
    "UsageReport",
    "get_billing_adapter",
    "list_available_providers",
    "register_billing_adapter",
]
