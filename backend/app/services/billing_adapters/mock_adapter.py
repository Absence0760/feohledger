"""Mock billing adapter — the local-first DEFAULT.

Deterministic, in-process, no network and no credential. ``pnpm dev`` and the
test suite run entirely against this: creating a subscription returns a stable
synthetic ``external_subscription_id`` derived from the org id, usage reports are
accepted and dropped (nothing to bill locally), and ``parse_webhook`` understands
a simple dev JSON envelope so the webhook route can be exercised without Stripe.

It never moves money — there is no money to move locally.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from app.services.billing_adapters.base import (
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
from app.services.billing_adapters.dispatcher import register_billing_adapter

#: Every meter event the mock has accepted, process-wide (adapter instances are
#: built per call, so instance state would vanish). Tests clear it themselves.
RECORDED_METER_EVENTS: list[MeterEvent] = []


@register_billing_adapter("mock")
class MockBillingAdapter(BillingAdapter):
    provider_name = "mock"

    async def ensure_customer(
        self, *, organization_id: str, name: str | None = None, email: str | None = None
    ) -> str:
        # Deterministic synthetic id derived from the org — stable across retries.
        return f"mock_cus_{organization_id}"

    async def ensure_price(
        self, *, plan_code: str, monthly_price: Decimal, currency: str = "USD"
    ) -> str:
        # Deterministic synthetic id derived from the plan — stable across retries.
        return f"mock_price_{plan_code}"

    async def create_subscription(self, request: CreateSubscriptionRequest) -> ProviderSubscription:
        # Deterministic synthetic id so a retried create is idempotent locally.
        return ProviderSubscription(
            external_subscription_id=f"mock_sub_{request.organization_id}",
            status="trialing" if request.trial_days > 0 else "active",
            plan_code=request.plan_code,
        )

    async def get_subscription(self, external_subscription_id: str) -> ProviderSubscription:
        return ProviderSubscription(
            external_subscription_id=external_subscription_id,
            status="active",
            plan_code="",
        )

    async def list_invoices(
        self, *, customer_id: str | None, limit: int = 24
    ) -> list[ProviderInvoice]:
        """Deterministic synthetic billing invoices so the dev UI has data.

        Returns ``[]`` when the org was never provisioned (no ``customer_id``),
        mirroring the live adapter's no-customer case. Otherwise it fabricates a
        short, stable run of monthly receipts ending in the current month — newest
        first, the latest ``open`` and the rest ``paid`` — all $49.00 (the default
        local plan price). No money moves; this is a read surface.
        """
        if not customer_id:
            return []
        now = datetime.now(UTC)
        count = max(0, min(limit, 6))
        invoices: list[ProviderInvoice] = []
        for offset in range(count):
            # Walk back month-by-month from the current month.
            year = now.year
            month = now.month - offset
            while month <= 0:
                month += 12
                year -= 1
            period = f"{year:04d}-{month:02d}"
            created = datetime(year, month, 1, tzinfo=UTC)
            invoices.append(
                ProviderInvoice(
                    external_invoice_id=f"mock_in_{customer_id}_{period}",
                    number=f"MOCK-{period}",
                    period=period,
                    amount="49.00",
                    currency="USD",
                    status="open" if offset == 0 else "paid",
                    hosted_url=None,
                    created_at=created.isoformat(),
                )
            )
        return invoices

    async def report_usage(self, report: UsageReport) -> None:
        # No-op: nothing to bill locally. Kept so the call site is identical to
        # the live adapter.
        return None

    async def ensure_overage_price(
        self, *, plan_code: str, unit_price: Decimal, currency: str = "USD"
    ) -> str:
        return f"mock_overage_price_{plan_code}"

    async def report_meter_event(self, event: MeterEvent) -> None:
        """Record the event in-process so tests (and a curious dev) can see
        exactly what WOULD have been billed. Deduped on ``identifier``, the
        same idempotency the live provider gives a retried event — so a test
        asserting "reported once" means the same thing against both."""
        if any(e.identifier == event.identifier for e in RECORDED_METER_EVENTS):
            return None
        RECORDED_METER_EVENTS.append(event)
        return None

    async def create_setup_intent(self, customer_id: str | None) -> ProviderSetupIntent | None:
        """Deterministic synthetic SetupIntent so the dev UI can exercise the
        add-card flow. No customer (never provisioned) → ``None``, mirroring the
        live adapter's no-customer case. The ``client_secret`` is synthetic and
        single-use-shaped — no real card can be confirmed against it locally."""
        if not customer_id:
            return None
        return ProviderSetupIntent(
            external_setup_intent_id=f"mock_seti_{customer_id}",
            client_secret=f"mock_seti_{customer_id}_secret",
            status="requires_payment_method",
        )

    async def list_payment_methods(self, customer_id: str | None) -> list[ProviderPaymentMethod]:
        """Deterministic synthetic saved card (visa ****4242) so the dev UI has
        data. PII-safe — brand/last4/exp only, never a PAN. No customer →
        ``[]``."""
        if not customer_id:
            return []
        return [
            ProviderPaymentMethod(
                external_payment_method_id=f"mock_pm_{customer_id}",
                brand="visa",
                last4="4242",
                exp_month=12,
                exp_year=2030,
                is_default=True,
            )
        ]

    def parse_webhook(self, headers: dict, body: bytes) -> BillingWebhookEvent | None:
        """Parse a dev JSON envelope:
        ``{"id": "...", "type": "...", "subscription": "...", "status": "..."}``.
        Returns ``None`` on malformed input (the route then 204s silently)."""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        event_id = payload.get("id")
        event_type = payload.get("type")
        if not event_id or not event_type:
            return None
        return BillingWebhookEvent(
            event_id=str(event_id),
            event_type=str(event_type),
            external_subscription_id=payload.get("subscription"),
            status=payload.get("status"),
        )

    async def test_connection(self) -> bool:
        return True
