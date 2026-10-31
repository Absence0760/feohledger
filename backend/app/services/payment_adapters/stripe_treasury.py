"""Stripe Treasury adapter — ACH + wire via Stripe's OutboundPayments API.

Stripe Treasury is a banking-as-a-service product layered on top of
Stripe Issuing's FinancialAccounts. Money sits in a FinancialAccount;
an OutboundPayment moves it to an external US bank account or to
another Stripe Treasury account. Method is selected via the
`payment_method_data.us_bank_account.network` field (`ach` or
`us_domestic_wire`); RTP is not currently supported by Stripe
Treasury (which is why we don't list it in `supported_methods`).

API docs: https://stripe.com/docs/api/treasury/outbound_payments

Auth: Bearer token (the platform's restricted API key).
Idempotency: every POST accepts `Idempotency-Key`. Stripe stores the
same response for 24h, so a retry of `correlation_id` returns the
existing OutboundPayment ID rather than minting a duplicate.

Webhooks: Stripe signs every webhook with `Stripe-Signature` —
HMAC-SHA256 over `<timestamp>.<raw body>`. We verify the timestamp
is within 5 minutes (replay protection) and the signature matches.
The signing secret is per-endpoint, configured in
`Organization.settings.payments.webhook_secret`.

Local dev: Stripe sandbox accounts use a separate `sk_test_...`
key. The base URL is the same (`api.stripe.com`); test vs live is
selected entirely by the credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal

import httpx

from app.services.payment_adapters.base import (
    CorridorQuote,
    PaymentAdapter,
    PaymentPayload,
    PaymentResult,
    PaymentStatus,
    WebhookEvent,
    minor_units_to_decimal,
    to_minor_units,
)
from app.services.payment_adapters.dispatcher import register_payment_adapter

logger = logging.getLogger(__name__)

API_BASE = "https://api.stripe.com/v1"
TIMEOUT = 15.0
SIGNATURE_TOLERANCE_SECONDS = 5 * 60

# Stripe's OutboundPayment.status set → ours. They distinguish
# `processing` from `posted`; we collapse the former into our
# `processing` and treat `posted` as `completed`.
_STATUS_MAP: dict[str, PaymentStatus] = {
    "processing": PaymentStatus.processing,
    "posted": PaymentStatus.completed,
    "returned": PaymentStatus.failed,
    "failed": PaymentStatus.failed,
    "canceled": PaymentStatus.cancelled,
}


@register_payment_adapter("stripe_treasury")
class StripeTreasuryAdapter(PaymentAdapter):
    provider_name = "stripe_treasury"
    # Stripe Treasury today supports ACH + domestic wire only.
    # International + RTP not in the product. Check supersedes the
    # OutboundPayments path entirely via Issuing's Print Check
    # feature — handled by the dedicated checkeeper adapter.
    supported_methods = ("ach", "wire")

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_key: str = config.get("api_key", "")
        # Required: the FinancialAccount the payment debits from.
        self.financial_account_id: str = config.get("financial_account_id", "")
        self.webhook_secret: str = config.get("webhook_secret", "")
        # Stripe API base. Live Stripe, unless the OPERATOR sets
        # FEOH_STRIPE_API_BASE (the local stripe-mock container; see
        # backend/docs/payments.md § Local testing). Never from `config`: this is
        # the org's `settings.payments` block, any tenant admin writes it, and
        # the sealed secret key rides on every request — a per-org `api_base`
        # handed that key to whatever host the admin named (decisions §339
        # follow-up). The key is ignored if present.
        from app.config import settings

        self.api_base: str = settings.stripe_api_base or API_BASE

    # ------------------------------------------------------------------
    # create_payment
    # ------------------------------------------------------------------

    async def create_payment(self, payload: PaymentPayload) -> PaymentResult:
        if not self.api_key:
            return PaymentResult(
                success=False,
                status=PaymentStatus.failed,
                failure_reason="stripe_treasury_not_configured",
            )
        if payload.method not in self.supported_methods:
            return PaymentResult(
                success=False,
                status=PaymentStatus.failed,
                failure_reason=(
                    f"method '{payload.method}' is not supported by Stripe Treasury "
                    f"(supports: {', '.join(self.supported_methods)})"
                ),
            )

        # Stripe expects integer minor-units. We persist Decimal but
        # the wire format is the currency's smallest unit. `to_minor_units`
        # resolves its ISO-4217 exponent rather than assuming cents, and rounds
        # ROUND_HALF_UP (not Decimal's default banker's rounding) to match the
        # rest of the money path — e.g. international_payments — so a .x5 minor
        # cent never rounds *down*. Exact inverse of the
        # `minor_units_to_decimal` in `parse_webhook`.
        amount_minor = to_minor_units(payload.amount, payload.currency)
        network = "us_domestic_wire" if payload.method == "wire" else "ach"

        # External-account ID lives on vendor_bank.counterparty_id by
        # convention — orgs set it up via Stripe dashboard or our
        # vendor-management UI and store the `ba_...` id there.
        external_account = (payload.vendor_bank or {}).get("counterparty_id")
        if not external_account:
            return PaymentResult(
                success=False,
                status=PaymentStatus.failed,
                failure_reason="stripe_treasury_no_external_account",
            )

        # Stripe accepts form-encoded bodies on POST.
        body = {
            "financial_account": self.financial_account_id,
            "amount": str(amount_minor),
            "currency": payload.currency.lower(),
            "destination_payment_method": external_account,
            "destination_payment_method_options[us_bank_account][network]": network,
            "description": (payload.description or payload.invoice_number)[:500],
            "metadata[invoice_id]": payload.invoice_id,
            "metadata[correlation_id]": payload.correlation_id,
        }
        if (payload.metadata or {}).get("organization_id"):
            body["metadata[organization_id]"] = payload.metadata["organization_id"]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Idempotency-Key": payload.correlation_id,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.post(
                    f"{self.api_base}/treasury/outbound_payments", data=body, headers=headers
                )
        except httpx.RequestError as exc:
            return PaymentResult(
                success=False,
                status=PaymentStatus.failed,
                failure_reason=f"stripe_transport_error:{exc.__class__.__name__}",
            )

        if response.status_code >= 400:
            # Stripe returns `{"error": {"code": "...", "message": "..."}}`
            # — we only keep the code. The message can echo back
            # account-shaped strings from the request (invariant #7).
            try:
                err = response.json().get("error", {}) or {}
            except ValueError:
                err = {}
            return PaymentResult(
                success=False,
                status=PaymentStatus.failed,
                failure_reason=f"stripe_api_error:{err.get('code') or response.status_code}",
            )

        data = response.json()
        provider_id = data.get("id", "")
        status_str = data.get("status", "processing")
        return PaymentResult(
            success=True,
            status=_STATUS_MAP.get(status_str, PaymentStatus.submitted),
            provider_payment_id=provider_id,
            reference=data.get("transaction") or data.get("id"),
            raw_response=data,
        )

    # ------------------------------------------------------------------
    # get_payment_status
    # ------------------------------------------------------------------

    async def get_payment_status(self, provider_payment_id: str) -> PaymentStatus:
        if not self.api_key:
            return PaymentStatus.failed
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(
                f"{self.api_base}/treasury/outbound_payments/{provider_payment_id}",
                headers=headers,
            )
        if response.status_code >= 400:
            return PaymentStatus.failed
        return _STATUS_MAP.get(response.json().get("status", ""), PaymentStatus.submitted)

    # ------------------------------------------------------------------
    # parse_webhook
    # ------------------------------------------------------------------

    def parse_webhook(self, headers: dict, body: bytes) -> WebhookEvent | None:
        """Verify Stripe-Signature and translate the event into the
        common WebhookEvent shape. Returns None on:
          - missing / malformed signature header
          - signature mismatch
          - timestamp outside the 5-minute tolerance window
          - body that doesn't decode as JSON
          - event type we don't care about
        """
        if not self.webhook_secret:
            return None
        sig_header = headers.get("Stripe-Signature") or headers.get("stripe-signature")
        if not sig_header:
            return None

        # Stripe's signature header: `t=<unix_ts>,v1=<hex>[,v0=<hex>]`.
        # We only verify v1 — v0 is deprecated.
        parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
        ts = parts.get("t")
        provided = parts.get("v1")
        if not ts or not provided:
            return None
        try:
            ts_int = int(ts)
        except ValueError:
            return None
        # Replay-protection window.
        if abs(time.time() - ts_int) > SIGNATURE_TOLERANCE_SECONDS:
            return None

        signed_payload = f"{ts}.".encode() + body
        expected = hmac.new(
            self.webhook_secret.encode(), signed_payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, provided):
            return None

        try:
            event = json.loads(body.decode())
        except (UnicodeDecodeError, ValueError):
            return None

        # We care about treasury.outbound_payment.* events. Others
        # are dropped silently — the inbound handler returns 204.
        event_type = event.get("type", "")
        if not event_type.startswith("treasury.outbound_payment."):
            return None

        obj = (event.get("data") or {}).get("object") or {}
        provider_payment_id = obj.get("id")
        status_str = obj.get("status", "")
        if not provider_payment_id:
            return None

        # Stripe puts the event id at the top level of the envelope.
        event_id = event.get("id") or f"{provider_payment_id}:{status_str}"
        return WebhookEvent(
            provider_payment_id=provider_payment_id,
            status=_STATUS_MAP.get(status_str, PaymentStatus.submitted),
            event_id=str(event_id),
            reference=obj.get("transaction"),
            failure_reason=(obj.get("returned_details") or {}).get("code"),
            occurred_at=event.get("created") and str(event["created"]),
            # What Stripe says it actually settled. OutboundPayment.amount is
            # in the smallest currency unit — the same scale `create_payment`
            # sends — and `currency` comes back lowercase (the verifier
            # compares case-insensitively).
            currency=(obj.get("currency") or None),
            amount=minor_units_to_decimal(obj.get("amount"), obj.get("currency")),
            raw=event,
        )

    async def test_connection(self) -> bool:
        if not self.api_key:
            return False
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(
                f"{self.api_base}/treasury/financial_accounts",
                headers=headers,
                params={"limit": 1},
            )
        return response.status_code < 400

    async def quote_payment(self, payload: PaymentPayload) -> CorridorQuote:
        """Stripe Treasury fees are tiered + provider-specific; we
        publish a conservative static quote here so the optimizer
        can rank against alternatives. Real fees come from
        `treasury.outbound_payment.created` webhooks at submission."""
        if payload.method not in self.supported_methods:
            return CorridorQuote(
                provider=self.provider_name,
                method=payload.method,
                available=False,
                unavailable_reason=(f"method '{payload.method}' not supported by stripe_treasury"),
            )
        # Stripe Treasury pricing (sandbox-default): ACH $0.25 flat;
        # wire $10 flat. Tiered enterprise contracts override these
        # via config.
        flat = Decimal("0.25") if payload.method == "ach" else Decimal("10.00")
        override = (self.config.get("flat_fees") or {}).get(payload.method)
        if override is not None:
            flat = Decimal(str(override))
        eta = 1 if payload.method == "ach" else 0
        return CorridorQuote(
            provider=self.provider_name,
            method=payload.method,
            available=True,
            flat_fee=flat,
            pct_fee=Decimal("0"),
            eta_business_days=eta,
        )
