"""Base billing-provider adapter interface + shared types.

A billing adapter wraps an external billing/subscription provider (Stripe
Billing, Chargebee, …). It is the seam between our control-plane ``Plan`` /
``Subscription`` model and the provider's hosted objects. This FIRST SLICE
defines the interface + a deterministic local-first ``mock`` (the default) and a
fail-closed ``stripe_billing`` skeleton with the correct wire shape.

The adapter NEVER moves money on its own and NEVER persists our models — it
talks to the provider and returns normalized DTOs; the caller persists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class ProviderSubscription:
    """Normalized view of a provider-side subscription."""

    external_subscription_id: str
    status: str  # trialing | active | past_due | canceled (provider-mapped)
    plan_code: str


@dataclass(frozen=True)
class CreateSubscriptionRequest:
    organization_id: str
    plan_code: str
    # Exact money — the plan's monthly price, passed through for the provider to
    # price the line item. Decimal, never float.
    monthly_price: Decimal
    currency: str = "USD"
    trial_days: int = 0
    # Stable idempotency key so a retried create never double-subscribes the org
    # at the provider (mirrors the payment-adapter idempotency discipline).
    idempotency_key: str = ""


@dataclass(frozen=True)
class UsageReport:
    organization_id: str
    period: str  # YYYY-MM
    # meter name -> quantity/amount as an exact decimal string (no float).
    meters: dict[str, str] = field(default_factory=dict)
    external_subscription_id: str | None = None


#: The provider-side meter every AI-read-invoice OVERAGE event is reported to
#: (decisions §253). One event per overage invoice, value "1" — the price on the
#: meter is the plan's per-unit overage price. Included reads are never
#: reported: the allowance is applied by OUR count, not by provider tiers
#: (see `services/billing/ai_overage.py`).
AI_INVOICE_OVERAGE_EVENT = "ai_invoice_overage"


@dataclass(frozen=True)
class MeterEvent:
    """One usage event for a provider billing meter.

    ``identifier`` is stable for the unit it reports, so a retry of the same
    report is recognised as the same event by the provider (and by the mock).
    ``value`` is an exact decimal STRING — never a float on a billing surface.
    """

    event_name: str
    customer_id: str | None
    value: str
    identifier: str
    #: Unix seconds the usage belongs to (the provider bills it into the period
    #: containing this instant).
    timestamp: int


@dataclass(frozen=True)
class ProviderInvoice:
    """Normalized view of a provider-side billing invoice / receipt.

    One past charge the platform raised against the org's customer account. Money
    is an exact decimal STRING (never float) — this is a billing surface. The
    hosted URL (if any) is the provider's customer-facing invoice/receipt page.
    """

    external_invoice_id: str
    number: str | None  # provider-facing invoice number (may be absent for drafts)
    period: str | None  # YYYY-MM the invoice covers, when derivable
    amount: str  # exact decimal string, e.g. "49.00"
    currency: str
    status: str  # paid | open | void (provider-mapped)
    hosted_url: str | None  # hosted invoice / receipt page, when the provider offers one
    created_at: str | None  # ISO-8601 timestamp, when known


@dataclass(frozen=True)
class ProviderSetupIntent:
    """Normalized view of a provider-side SetupIntent.

    A SetupIntent collects + saves a payment method against the customer WITHOUT
    a charge. The frontend confirms it with the ``client_secret`` (via the
    provider's JS SDK) to attach a card; nothing here is a secret in the
    long-lived sense (the client_secret is single-use and scoped to one
    intent), and it NEVER carries a PAN.
    """

    external_setup_intent_id: str
    client_secret: str  # single-use secret the frontend confirms the card with
    status: str  # requires_payment_method | requires_confirmation | succeeded (provider-mapped)


@dataclass(frozen=True)
class ProviderPaymentMethod:
    """Normalized view of a saved provider-side card — PII-SAFE metadata only.

    NEVER a full PAN. Only the brand + last 4 + expiry, which is the exact
    metadata a billing UI shows ("Visa ending 4242, exp 12/2030"). The full card
    number lives only at the provider and is never returned here or logged.
    """

    external_payment_method_id: str
    brand: str | None  # visa | mastercard | amex | … (provider-mapped)
    last4: str | None  # last 4 digits of the card — non-PII card metadata
    exp_month: int | None
    exp_year: int | None
    is_default: bool = False


@dataclass(frozen=True)
class BillingWebhookEvent:
    """A verified, deduped provider webhook event."""

    event_id: str
    event_type: str  # e.g. "subscription.updated", "invoice.payment_failed"
    external_subscription_id: str | None
    # Mapped lifecycle status when the event implies one, else None.
    status: str | None = None


class BillingAdapter:
    """Base class for billing providers."""

    provider_name: str = "base"

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    async def ensure_customer(
        self, *, organization_id: str, name: str | None = None, email: str | None = None
    ) -> str:
        """Resolve-or-create the provider-side customer for an org. Returns its id.

        Idempotent at the provider (a stable per-org idempotency key). The caller
        persists the returned id so subsequent calls skip the round-trip.
        """
        raise NotImplementedError

    async def ensure_price(
        self, *, plan_code: str, monthly_price: Decimal, currency: str = "USD"
    ) -> str:
        """Resolve-or-create the recurring provider-side price for a plan. Returns its id."""
        raise NotImplementedError

    async def create_subscription(self, request: CreateSubscriptionRequest) -> ProviderSubscription:
        raise NotImplementedError

    async def get_subscription(self, external_subscription_id: str) -> ProviderSubscription:
        raise NotImplementedError

    async def list_invoices(
        self, *, customer_id: str | None, limit: int = 24
    ) -> list[ProviderInvoice]:
        """List the org's past billing invoices / receipts (newest first).

        ``customer_id`` is the provider-side customer the org is bound to (resolved
        from ``Organization.settings.billing.stripe_customer_id``); ``None`` means
        the org was never provisioned with the provider, in which case there is
        nothing to list — return ``[]``, never raise.

        Safe default: return ``[]``. An adapter without a real billing back-end
        (or that hasn't implemented this) yields an empty list rather than a 500,
        so the read surface degrades gracefully.
        """
        return []

    async def report_usage(self, report: UsageReport) -> None:
        raise NotImplementedError

    async def ensure_overage_price(
        self, *, plan_code: str, unit_price: Decimal, currency: str = "USD"
    ) -> str:
        """Resolve-or-create the per-unit METERED price a plan's AI-read overage
        bills at (attached to the :data:`AI_INVOICE_OVERAGE_EVENT` meter).
        Returns its provider id. Idempotent at the provider."""
        raise NotImplementedError

    async def report_meter_event(self, event: MeterEvent) -> None:
        """Report one usage event. Idempotent on ``event.identifier``: a retry of
        an event the provider already accepted must not count twice. Raises on
        failure so the caller does not advance its reported-so-far marker."""
        raise NotImplementedError

    async def create_setup_intent(self, customer_id: str | None) -> ProviderSetupIntent | None:
        """Start a SetupIntent so the org can add/replace a saved card.

        Returns the ``client_secret`` the frontend confirms the card with (via
        the provider's JS SDK), without charging anything. ``customer_id is None``
        means the org was never provisioned at the provider, so there is no
        customer to attach a method to — return ``None`` rather than raise, and
        the route surfaces a clear "not configured" shape (never a 500).

        Safe default: ``None``. An adapter without a real billing back-end (or
        that hasn't implemented this) yields ``None`` so the surface degrades
        gracefully.
        """
        return None

    async def list_payment_methods(self, customer_id: str | None) -> list[ProviderPaymentMethod]:
        """List the org's saved cards — PII-SAFE metadata only (brand/last4/exp).

        NEVER returns a full PAN. ``customer_id is None`` (never provisioned) →
        ``[]``, never raise.

        Safe default: ``[]``. An adapter without a real billing back-end yields
        an empty list rather than a 500, so the read surface degrades
        gracefully.
        """
        return []

    def parse_webhook(self, headers: dict, body: bytes) -> BillingWebhookEvent | None:
        """Verify the provider HMAC over ``body`` and return a normalized event.

        Returns ``None`` on any verification/parse failure so the webhook route
        can ack 204 silently (no enumeration). Dedupe-by-event-id is the route's
        responsibility via ``services/webhook_security.is_event_already_processed``.
        """
        raise NotImplementedError

    async def test_connection(self) -> bool:
        raise NotImplementedError
