"""Stripe Billing adapter — live REST calls over ``httpx``.

Fail-closed posture: every method that would touch Stripe raises
``BillingNotConfigured`` when no API key is present, so selecting this provider
without a key can never silently no-op or fall back to a permissive path. The
real secret arrives via sops (``FEOH_BILLING_STRIPE_API_KEY`` /
``FEOH_BILLING_STRIPE_WEBHOOK_SECRET``) — there is NO hardcoded fallback.

Implemented end-to-end against the Stripe REST API (key as HTTP-Basic username,
form-encoded bodies, ``Idempotency-Key`` on every create so a retry can't
duplicate): per-org customer + per-plan price provisioning
(``ensure_customer`` / ``ensure_price``), ``create_subscription`` /
``get_subscription``, ``report_usage`` (Billing Meter Events, one per meter,
quantities as exact decimal strings), and ``parse_webhook`` (Stripe-Signature
HMAC verify). ``create_subscription`` consumes the resolved ``stripe_customer_id``
+ ``stripe_price_id`` from config (the provisioning service resolves + persists
them); without them it fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import httpx

from app.services.billing_adapters.base import (
    AI_INVOICE_OVERAGE_EVENT,
    BillingAdapter,
    BillingNotConfigured,
    BillingPaymentDeclined,
    BillingProviderError,
    BillingWebhookEvent,
    CreateSubscriptionRequest,
    MeterEvent,
    ProviderInvoice,
    ProviderPaymentMethod,
    ProviderSetupIntent,
    ProviderSubscription,
    SetupIntentRejected,
    UsageReport,
)
from app.services.billing_adapters.dispatcher import register_billing_adapter
from app.services.webhook_security import extract_signature_header

logger = logging.getLogger(__name__)

# Default Stripe REST base. The dispatcher injects an override into config
# (FEOH_BILLING_STRIPE_API_BASE) so a sandbox / test can repoint it; this constant
# is only the last-resort fallback when config carries no value.
_DEFAULT_API_BASE = "https://api.stripe.com"


def _to_minor_units(amount: Decimal) -> int:
    """Convert an exact Decimal money amount to integer minor units (cents).

    Decimal-exact: multiply by 100 and quantize to a whole number with
    ROUND_HALF_UP before converting to int — never via float. Stripe prices are
    integer minor units, so $49.00 -> 4900.
    """
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


__all__ = [
    "BillingNotConfigured",
    "BillingPaymentDeclined",
    "BillingProviderError",
    "StripeBillingAdapter",
]

#: Stripe subscription statuses that are not live any more. Anything else
#: (`active`, `trialing`, `past_due`, `unpaid`, `incomplete`, `paused`) still
#: bills, so `find_live_subscription` treats it as the org's subscription.
_ENDED_STATUSES = frozenset({"canceled", "incomplete_expired"})

#: A Stripe SetupIntent id — the only shape `adopt_setup_intent` puts in a path.
_SETUP_INTENT_ID = re.compile(r"seti_[A-Za-z0-9]{1,250}")


def _ts(value) -> datetime | None:
    """A Stripe Unix timestamp → aware UTC datetime (None when absent)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _period_of(obj: dict) -> tuple[datetime | None, datetime | None]:
    """The subscription's current billing window.

    Since API version 2025-03-31 Stripe reports the window per subscription
    ITEM, not on the subscription; every item of ours shares one cycle (one
    anchor, monthly prices), so the first item's window is the subscription's.
    The top-level fields are read as a fallback for older-shaped payloads.
    """
    items = ((obj.get("items") or {}).get("data")) or []
    for item in items:
        if isinstance(item, dict):
            start, end = _ts(item.get("current_period_start")), _ts(item.get("current_period_end"))
            if start and end:
                return start, end
    return _ts(obj.get("current_period_start")), _ts(obj.get("current_period_end"))


def _subscription_of(payload: dict, *, plan_code: str = "") -> ProviderSubscription:
    start, end = _period_of(payload)
    return ProviderSubscription(
        external_subscription_id=str(payload["id"]),
        status=_STATUS_MAP.get(payload.get("status", ""), "past_due"),
        plan_code=plan_code or str((payload.get("metadata") or {}).get("plan_code") or ""),
        current_period_start=start,
        current_period_end=end,
    )


def _invoice_subscription_id(invoice: dict) -> str | None:
    """The subscription an invoice bills. Since API version 2025-03-31 it sits
    at ``parent.subscription_details.subscription``; the old top-level
    ``subscription`` is read as a fallback."""
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") if isinstance(parent, dict) else None
    sub = (details or {}).get("subscription") if isinstance(details, dict) else None
    if sub is None:
        sub = invoice.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    return str(sub) if sub else None


def _id_of(value) -> str | None:
    """A Stripe reference that may be a bare id or an expanded object."""
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value else None


# Map Stripe subscription statuses → our four-state lifecycle. ``incomplete`` /
# ``unpaid`` collapse to ``past_due`` (dunning is a later slice).
_STATUS_MAP = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
    "incomplete": "past_due",
    "incomplete_expired": "canceled",
    "canceled": "canceled",
}

# Map Stripe invoice statuses → our three-state billing-invoice view.
# ``draft`` collapses to ``open`` (not yet finalized, still owed);
# ``uncollectible`` collapses to ``open`` (still owed, just written off at Stripe).
_INVOICE_STATUS_MAP = {
    "draft": "open",
    "open": "open",
    "paid": "paid",
    "uncollectible": "open",
    "void": "void",
}


def _from_minor_units(amount_minor: int) -> str:
    """Convert integer minor units (cents) to an exact decimal STRING.

    Decimal-exact: ``4900`` → ``"49.00"`` via ``Decimal`` quantize, never float.
    Stripe invoice amounts (`amount_due` / `total`) are integer minor units.
    """
    value = (Decimal(amount_minor) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(value)


@register_billing_adapter("stripe_billing")
class StripeBillingAdapter(BillingAdapter):
    provider_name = "stripe_billing"

    @property
    def _api_key(self) -> str:
        return (self.config or {}).get("stripe_api_key") or ""

    @property
    def _webhook_secret(self) -> str:
        return (self.config or {}).get("stripe_webhook_secret") or ""

    @property
    def _api_base(self) -> str:
        return (self.config or {}).get("stripe_api_base") or _DEFAULT_API_BASE

    @property
    def _api_version(self) -> str:
        """The pinned ``Stripe-Version`` (``FEOH_BILLING_STRIPE_API_VERSION``).

        Pinned rather than left to the account default so the wire shapes this
        module parses (period bounds per item, an invoice's subscription under
        ``parent``) cannot change under it when the account's default moves."""
        return (self.config or {}).get("stripe_api_version") or ""

    @property
    def _timeout(self) -> float:
        return float((self.config or {}).get("timeout_seconds", 15.0))

    def _require_key(self) -> None:
        if not self._api_key:
            # Fail closed — never proceed against Stripe without a key.
            raise BillingNotConfigured("Stripe billing is not configured (no API key)")

    def _client(self) -> httpx.AsyncClient:
        """An authenticated client for the Stripe REST API.

        Stripe authenticates with the secret key as HTTP Basic username (no
        password) and consumes ``application/x-www-form-urlencoded`` bodies.
        Factored out so unit tests can patch ``StripeBillingAdapter._client``
        with a transport that serves canned responses (no network).
        """
        headers = {"Stripe-Version": self._api_version} if self._api_version else None
        return httpx.AsyncClient(
            base_url=self._api_base,
            auth=(self._api_key, ""),
            timeout=self._timeout,
            headers=headers,
        )

    async def ensure_customer(
        self, *, organization_id: str, name: str | None = None, email: str | None = None
    ) -> str:
        """Resolve-or-create the Stripe ``customer`` for an org. Returns its id.

        Idempotent at the provider: a stable ``Idempotency-Key`` keyed on the org
        id means a retried create returns the original customer rather than a
        duplicate. Stripe has no upsert, so the caller persists the returned id
        (on ``Organization.settings.billing.stripe_customer_id``) and passes it
        back on the next call to skip this round-trip entirely.

        PII boundary: only the org name (a business name, not personal data) and
        an admin contact email are sent — never bank/tax/PAN data.
        """
        self._require_key()
        form: dict[str, str] = {"metadata[organization_id]": str(organization_id)}
        if name:
            form["name"] = name
        if email:
            form["email"] = email
        # The "ap-" prefix is frozen and deliberately survived the FeohLedger
        # rename: this string IS the idempotency key Stripe already has on
        # record for previously-created customers. Renaming it would make the
        # next retry look like a brand-new request and create a DUPLICATE
        # customer. It is an opaque key, never shown to anyone.
        headers = {"Idempotency-Key": f"ap-customer-{organization_id}"}
        async with self._client() as client:
            resp = await client.post("/v1/customers", data=form, headers=headers)
        payload = self._json_or_raise(resp, "ensure_customer")
        return str(payload["id"])

    async def ensure_price(
        self, *, plan_code: str, monthly_price: Decimal, currency: str = "USD"
    ) -> str:
        """Resolve-or-create the recurring Stripe ``price`` for a plan. Returns its id.

        Maps our internal ``Plan`` to a Stripe monthly recurring price. The
        unit amount is the plan's monthly price in the smallest currency unit
        (cents) — computed with exact Decimal math, never float. Idempotent via
        an ``Idempotency-Key`` keyed on the plan code + amount + currency, so a
        retried create returns the original price. The caller persists the id
        (on ``Plan``-keyed ``Organization.settings.billing.plan_price_ids``).
        """
        self._require_key()
        unit_amount = _to_minor_units(monthly_price)
        cur = currency.lower()
        lookup_key = f"feohledger_plan_{plan_code}_{unit_amount}_{cur}"
        form = {
            "unit_amount": str(unit_amount),
            "currency": cur,
            "recurring[interval]": "month",
            "product_data[name]": plan_code,
            "metadata[plan_code]": plan_code,
            "lookup_key": lookup_key,
            # An ARCHIVED price can still hold the key (`_find_price` only
            # returns active ones); without this the create would 400 forever.
            "transfer_lookup_key": "true",
        }
        # Frozen prefix — see ensure_customer: renaming an idempotency key
        # already registered with Stripe would duplicate the price on retry.
        headers = {"Idempotency-Key": f"ap-price-{plan_code}-{unit_amount}-{cur}"}
        async with self._client() as client:
            existing = await self._find_price(client, lookup_key)
            if existing:
                return existing
            resp = await client.post("/v1/prices", data=form, headers=headers)
        payload = self._json_or_raise(resp, "ensure_price")
        return str(payload["id"])

    async def _find_price(self, client: httpx.AsyncClient, lookup_key: str) -> str | None:
        """The active price already carrying ``lookup_key``, if any.

        The ``Idempotency-Key`` only dedupes a retry inside Stripe's 24-hour
        window; the price ids are cached per ORG (`settings.billing`), so
        without this every org provisioned on a later day minted its own copy
        of the same plan's product and price. The lookup key is the
        account-wide name for "this plan at this amount", so every org shares
        one price per plan (and a price change gets a new key, never a reprice
        of the old one)."""
        resp = await client.get(
            "/v1/prices", params={"lookup_keys[]": lookup_key, "active": "true", "limit": "1"}
        )
        for price in self._json_or_raise(resp, "find_price").get("data") or []:
            if isinstance(price, dict) and price.get("id"):
                return str(price["id"])
        return None

    async def create_subscription(self, request: CreateSubscriptionRequest) -> ProviderSubscription:
        """Create a subscription at Stripe and normalize the result.

        The provider-side ``customer`` and ``price`` ids are resolved upstream
        (`provisioning.provision_org_billing`) and ride in ``config``; without
        them this fails closed. ``payment_behavior=error_if_incomplete``: if
        the first charge fails Stripe answers 402 and creates NOTHING, so a
        declined card never leaves a half-made subscription behind and the
        plan change is refused (:class:`BillingPaymentDeclined`). The
        ``Idempotency-Key`` makes a retried create return the original.
        """
        self._require_key()
        customer = (self.config or {}).get("stripe_customer_id")
        price = (self.config or {}).get("stripe_price_id")
        if not customer or not price:
            raise BillingNotConfigured(
                "Stripe create_subscription requires stripe_customer_id + stripe_price_id"
            )
        form: dict[str, str] = {
            "customer": str(customer),
            "items[0][price]": str(price),
            "payment_behavior": "error_if_incomplete",
            "metadata[organization_id]": str(request.organization_id),
            "metadata[plan_code]": request.plan_code,
        }
        # A plan that bills AI-read overage needs its metered price on the
        # subscription too, or Stripe records the meter events and invoices none
        # of them (`provisioning.provision_org_billing` resolves it).
        overage_price = (self.config or {}).get("stripe_overage_price_id")
        if overage_price:
            form["items[1][price]"] = str(overage_price)
        if request.trial_days > 0:
            form["trial_period_days"] = str(request.trial_days)
        if request.billing_cycle_anchor is not None:
            # The stretch until the anchor is prorated onto the first invoice
            # (Stripe's default `create_prorations`, stated so it can't drift).
            form["billing_cycle_anchor"] = str(int(request.billing_cycle_anchor.timestamp()))
            form["proration_behavior"] = "create_prorations"
        request_headers = {}
        if request.idempotency_key:
            request_headers["Idempotency-Key"] = request.idempotency_key
        async with self._client() as client:
            resp = await client.post("/v1/subscriptions", data=form, headers=request_headers)
        payload = self._json_or_raise(resp, "create_subscription")
        return _subscription_of(payload, plan_code=request.plan_code)

    async def get_subscription(self, external_subscription_id: str) -> ProviderSubscription:
        self._require_key()
        async with self._client() as client:
            resp = await client.get(f"/v1/subscriptions/{external_subscription_id}")
        payload = self._json_or_raise(resp, "get_subscription")
        return _subscription_of(payload)

    async def find_live_subscription(self, customer_id: str) -> ProviderSubscription | None:
        self._require_key()
        params = {"customer": str(customer_id), "status": "all", "limit": "20"}
        async with self._client() as client:
            resp = await client.get("/v1/subscriptions", params=params)
        payload = self._json_or_raise(resp, "list_subscriptions")
        live = [
            obj
            for obj in payload.get("data") or []
            if isinstance(obj, dict) and obj.get("status") not in _ENDED_STATUSES
        ]
        if len(live) > 1:
            # One org is one subscription; two means one is billed for nothing.
            # Adopt the newest (Stripe lists newest first) and say so loudly —
            # the other needs an operator, and refusing would block the org.
            logger.error(
                "billing: customer has %d live provider subscriptions; adopting the newest",
                len(live),
            )
        return _subscription_of(live[0]) if live else None

    async def change_subscription_plan(
        self,
        external_subscription_id: str,
        *,
        price_id: str,
        overage_price_id: str | None,
        plan_code: str,
        idempotency_key: str,
    ) -> ProviderSubscription:
        """Move the subscription's items onto a new plan's prices.

        The items are told apart by the price's ``recurring.usage_type``: the
        flat base price is ``licensed``, the AI-read overage price is
        ``metered``. The base item's price is swapped; the metered item's is
        swapped, added, or removed to match the new plan. ``always_invoice``
        bills the prorated difference now (the change "applies immediately",
        as the plan picker says) and ``error_if_incomplete`` makes Stripe leave
        the subscription untouched if that charge fails (402 →
        :class:`BillingPaymentDeclined`).
        """
        self._require_key()
        async with self._client() as client:
            resp = await client.get(f"/v1/subscriptions/{external_subscription_id}")
            current = self._json_or_raise(resp, "get_subscription")
            base_item: str | None = None
            metered_item: str | None = None
            for item in (current.get("items") or {}).get("data") or []:
                if not isinstance(item, dict):
                    continue
                recurring = (item.get("price") or {}).get("recurring") or {}
                if recurring.get("usage_type") == "metered":
                    metered_item = metered_item or str(item.get("id"))
                else:
                    base_item = base_item or str(item.get("id"))

            form: dict[str, str] = {
                "proration_behavior": "always_invoice",
                "payment_behavior": "error_if_incomplete",
                "metadata[plan_code]": plan_code,
            }
            index = 0
            if base_item:
                form[f"items[{index}][id]"] = base_item
            form[f"items[{index}][price]"] = str(price_id)
            index += 1
            if metered_item and overage_price_id:
                form[f"items[{index}][id]"] = metered_item
                form[f"items[{index}][price]"] = str(overage_price_id)
            elif metered_item:
                form[f"items[{index}][id]"] = metered_item
                form[f"items[{index}][deleted]"] = "true"
            elif overage_price_id:
                form[f"items[{index}][price]"] = str(overage_price_id)
            resp = await client.post(
                f"/v1/subscriptions/{external_subscription_id}",
                data=form,
                headers={"Idempotency-Key": idempotency_key},
            )
            payload = self._json_or_raise(resp, "update_subscription")
        return _subscription_of(payload, plan_code=plan_code)

    async def cancel_subscription(self, external_subscription_id: str) -> ProviderSubscription:
        """Cancel now: ``prorate`` credits the unused part of the period to the
        customer's Stripe balance, ``invoice_now`` bills un-invoiced metered
        overage on a final invoice (without it, overage reported this period
        would never be billed). A second cancel of a canceled subscription is
        answered with the canceled subscription, not an error."""
        self._require_key()
        params = {"invoice_now": "true", "prorate": "true"}
        async with self._client() as client:
            resp = await client.delete(
                f"/v1/subscriptions/{external_subscription_id}", params=params
            )
            if 400 <= resp.status_code < 500 and resp.status_code != 402:
                check = await client.get(f"/v1/subscriptions/{external_subscription_id}")
                if check.status_code < 400:
                    existing = self._json_or_raise(check, "get_subscription")
                    if existing.get("status") in _ENDED_STATUSES:
                        return _subscription_of(existing)
            payload = self._json_or_raise(resp, "cancel_subscription")
        return _subscription_of(payload)

    async def _customer_default_pm(self, client: httpx.AsyncClient, customer_id: str) -> str | None:
        resp = await client.get(f"/v1/customers/{customer_id}")
        customer = self._json_or_raise(resp, "get_customer")
        return _id_of((customer.get("invoice_settings") or {}).get("default_payment_method"))

    async def _set_customer_default_pm(
        self, client: httpx.AsyncClient, customer_id: str, payment_method_id: str
    ) -> None:
        resp = await client.post(
            f"/v1/customers/{customer_id}",
            data={"invoice_settings[default_payment_method]": payment_method_id},
        )
        self._json_or_raise(resp, "set_default_payment_method")

    async def ensure_default_payment_method(self, customer_id: str | None) -> str | None:
        self._require_key()
        if not customer_id:
            return None
        async with self._client() as client:
            default = await self._customer_default_pm(client, customer_id)
            if default:
                return default
            resp = await client.get(
                "/v1/payment_methods",
                params={"customer": str(customer_id), "type": "card", "limit": "1"},
            )
            cards = self._json_or_raise(resp, "list_payment_methods").get("data") or []
            newest = _id_of(cards[0]) if cards and isinstance(cards[0], dict) else None
            if not newest:
                return None
            await self._set_customer_default_pm(client, customer_id, newest)
            return newest

    async def adopt_setup_intent(self, customer_id: str | None, setup_intent_id: str) -> str:
        """Verify the SetupIntent before trusting the card it carries: it must
        exist, belong to THIS org's customer (an id from another customer is
        refused as not-found, never adopted), and have succeeded."""
        self._require_key()
        # The id goes into a URL path: only a well-formed SetupIntent id may,
        # or `seti_/../customers/…` would send an arbitrary GET with our key.
        if not customer_id or not _SETUP_INTENT_ID.fullmatch(setup_intent_id):
            raise SetupIntentRejected("not_found")
        async with self._client() as client:
            resp = await client.get(f"/v1/setup_intents/{setup_intent_id}")
            if resp.status_code == 404:
                raise SetupIntentRejected("not_found")
            intent = self._json_or_raise(resp, "get_setup_intent")
            if _id_of(intent.get("customer")) != str(customer_id):
                raise SetupIntentRejected("not_found")
            payment_method = _id_of(intent.get("payment_method"))
            if intent.get("status") != "succeeded" or not payment_method:
                raise SetupIntentRejected("incomplete")
            await self._set_customer_default_pm(client, customer_id, payment_method)
        return payment_method

    async def list_invoices(
        self, *, customer_id: str | None, limit: int = 24
    ) -> list[ProviderInvoice]:
        """List the org's Stripe invoices (newest first) via the REST API.

        Fails closed without a key (``BillingNotConfigured``), like the sibling
        calls. ``customer_id is None`` means the org was never provisioned at the
        provider — there is nothing to list, so return ``[]`` (the route surfaces
        an empty list, not an error). Amounts come back as exact decimal strings
        from integer minor units — never float.
        """
        self._require_key()
        if not customer_id:
            return []
        # Stripe caps `limit` at 100; keep our own ceiling modest for the UI.
        capped = max(1, min(int(limit), 100))
        params = {"customer": str(customer_id), "limit": str(capped)}
        async with self._client() as client:
            resp = await client.get("/v1/invoices", params=params)
        payload = self._json_or_raise(resp, "list_invoices")
        out: list[ProviderInvoice] = []
        for obj in payload.get("data") or []:
            if not isinstance(obj, dict):
                continue
            raw_status = obj.get("status") or ""
            # `total` is the authoritative minor-units amount on a Stripe invoice.
            amount_minor = obj.get("total")
            amount = (
                _from_minor_units(int(amount_minor)) if isinstance(amount_minor, int) else "0.00"
            )
            created_raw = obj.get("created")
            created_at = (
                datetime.fromtimestamp(int(created_raw), tz=UTC).isoformat()
                if isinstance(created_raw, int)
                else None
            )
            period = obj.get("period_start")
            period_str = (
                datetime.fromtimestamp(int(period), tz=UTC).strftime("%Y-%m")
                if isinstance(period, int)
                else None
            )
            out.append(
                ProviderInvoice(
                    external_invoice_id=str(obj.get("id") or ""),
                    number=obj.get("number"),
                    period=period_str,
                    amount=amount,
                    currency=str(obj.get("currency") or "usd").upper(),
                    status=_INVOICE_STATUS_MAP.get(raw_status, "open"),
                    # Prefer the hosted invoice page; fall back to the PDF link.
                    hosted_url=obj.get("hosted_invoice_url") or obj.get("invoice_pdf"),
                    created_at=created_at,
                )
            )
        return out

    async def report_usage(self, report: UsageReport) -> None:
        """Report per-meter usage to Stripe via the Billing Meter Events API.

        One meter event per meter in the rollup. Quantities are exact decimal
        strings end-to-end (never float) — Stripe accepts a string ``value``.
        The org id is sent as the meter-event ``stripe_customer_id`` payload key
        so Stripe attributes the usage to the right customer.
        """
        self._require_key()
        if not report.meters:
            return
        customer = (self.config or {}).get("stripe_customer_id")
        if not customer:
            raise BillingNotConfigured("Stripe report_usage requires stripe_customer_id")
        async with self._client() as client:
            for meter_name, value in report.meters.items():
                form = {
                    "event_name": meter_name,
                    "payload[stripe_customer_id]": str(customer),
                    "payload[value]": str(value),
                    "identifier": f"{report.organization_id}:{report.period}:{meter_name}",
                }
                resp = await client.post("/v1/billing/meter_events", data=form)
                self._json_or_raise(resp, "report_usage")

    async def _ensure_overage_meter(self, client: httpx.AsyncClient) -> str:
        """Resolve-or-create the Billing Meter the overage events are reported to.

        A meter's ``event_name`` is unique among ACTIVE meters at Stripe, so the
        lookup is by name first; the create carries a stable ``Idempotency-Key``
        so two concurrent provisionings return one meter. ``sum`` aggregation:
        every event carries value ``"1"``, so the period's sum IS the number of
        overage invoices.
        """
        resp = await client.get("/v1/billing/meters", params={"status": "active", "limit": "100"})
        listing = self._json_or_raise(resp, "list_meters")
        for meter in listing.get("data") or []:
            if isinstance(meter, dict) and meter.get("event_name") == AI_INVOICE_OVERAGE_EVENT:
                return str(meter["id"])
        form = {
            "display_name": "AI-read invoices past the plan allowance",
            "event_name": AI_INVOICE_OVERAGE_EVENT,
            "default_aggregation[formula]": "sum",
            "customer_mapping[type]": "by_id",
            "customer_mapping[event_payload_key]": "stripe_customer_id",
            "value_settings[event_payload_key]": "value",
        }
        headers = {"Idempotency-Key": f"feohledger-meter-{AI_INVOICE_OVERAGE_EVENT}"}
        resp = await client.post("/v1/billing/meters", data=form, headers=headers)
        return str(self._json_or_raise(resp, "create_meter")["id"])

    async def ensure_overage_price(
        self, *, plan_code: str, unit_price: Decimal, currency: str = "USD"
    ) -> str:
        """Resolve-or-create the plan's per-unit METERED overage price.

        ``billing_scheme`` is Stripe's default ``per_unit``, deliberately NOT a
        graduated tier with the allowance free: the allowance is applied by our
        own calendar-month count (``ai_invoice_meter``), and only invoices past
        it are ever reported. A graduated price would apply the allowance a
        second time, over STRIPE's billing period — which is anchored on the
        subscription's start day, not the UTC calendar month the meter counts
        in — so the two windows would disagree on which reads were free.

        ``unit_amount_decimal`` is the unit price in minor units as an exact
        decimal string (``0.07`` → ``"7"``), so a sub-cent price stays exact.
        """
        self._require_key()
        cur = currency.lower()
        minor = format((unit_price * 100).normalize(), "f")
        lookup_key = f"feohledger_ai_overage_{plan_code}_{minor}_{cur}"
        async with self._client() as client:
            existing = await self._find_price(client, lookup_key)
            if existing:
                return existing
            meter_id = await self._ensure_overage_meter(client)
            form = {
                "currency": cur,
                "unit_amount_decimal": minor,
                "recurring[interval]": "month",
                "recurring[usage_type]": "metered",
                "recurring[meter]": meter_id,
                "product_data[name]": f"{plan_code} AI-read invoice overage",
                "metadata[plan_code]": plan_code,
                "metadata[meter_event]": AI_INVOICE_OVERAGE_EVENT,
                "lookup_key": lookup_key,
                "transfer_lookup_key": "true",
            }
            headers = {"Idempotency-Key": f"feohledger-ai-overage-price-{plan_code}-{minor}-{cur}"}
            resp = await client.post("/v1/prices", data=form, headers=headers)
            payload = self._json_or_raise(resp, "ensure_overage_price")
        return str(payload["id"])

    async def report_meter_event(self, event: MeterEvent) -> None:
        """POST one Billing Meter Event.

        Sent with BOTH ``identifier`` (Stripe's event-level uniqueness) and an
        ``Idempotency-Key`` equal to it: a retry inside Stripe's idempotency
        window replays the original success instead of failing as a duplicate,
        so a retried report is a no-op rather than an error that would stall
        the caller's reported-so-far marker.
        """
        self._require_key()
        if not event.customer_id:
            raise BillingNotConfigured("Stripe report_meter_event requires a customer id")
        form = {
            "event_name": event.event_name,
            "payload[stripe_customer_id]": str(event.customer_id),
            "payload[value]": str(event.value),
            "identifier": event.identifier,
            "timestamp": str(int(event.timestamp)),
        }
        headers = {"Idempotency-Key": event.identifier}
        async with self._client() as client:
            resp = await client.post("/v1/billing/meter_events", data=form, headers=headers)
        self._json_or_raise(resp, "report_meter_event")

    async def create_setup_intent(self, customer_id: str | None) -> ProviderSetupIntent | None:
        """Create a Stripe SetupIntent so the org can save/replace a card.

        Fails closed without a key (``BillingNotConfigured``), like the sibling
        calls. ``customer_id is None`` (never provisioned) → ``None`` (the route
        surfaces a clear not-configured shape, not an error). Returns the
        SetupIntent's ``client_secret`` the frontend confirms the card with — no
        money moves and no PAN is involved.
        """
        self._require_key()
        if not customer_id:
            return None
        form = {
            "customer": str(customer_id),
            "payment_method_types[]": "card",
            "usage": "off_session",
        }
        async with self._client() as client:
            resp = await client.post("/v1/setup_intents", data=form)
        payload = self._json_or_raise(resp, "create_setup_intent")
        return ProviderSetupIntent(
            external_setup_intent_id=str(payload.get("id") or ""),
            client_secret=str(payload.get("client_secret") or ""),
            status=str(payload.get("status") or "requires_payment_method"),
        )

    async def list_payment_methods(self, customer_id: str | None) -> list[ProviderPaymentMethod]:
        """List the org's saved Stripe cards — PII-SAFE metadata only.

        Fails closed without a key. ``customer_id is None`` (never provisioned) →
        ``[]``. Maps each Stripe ``payment_method`` to brand/last4/exp ONLY —
        never the full card number (Stripe never returns a PAN here anyway).
        """
        self._require_key()
        if not customer_id:
            return []
        params = {"customer": str(customer_id), "type": "card", "limit": "100"}
        async with self._client() as client:
            resp = await client.get("/v1/payment_methods", params=params)
            payload = self._json_or_raise(resp, "list_payment_methods")
            # The card subscription invoices charge, so the list can say which.
            default = await self._customer_default_pm(client, str(customer_id))
        out: list[ProviderPaymentMethod] = []
        for obj in payload.get("data") or []:
            if not isinstance(obj, dict):
                continue
            card = obj.get("card") or {}
            exp_month = card.get("exp_month")
            exp_year = card.get("exp_year")
            out.append(
                ProviderPaymentMethod(
                    external_payment_method_id=str(obj.get("id") or ""),
                    brand=card.get("brand"),
                    # Stripe's `card.last4` is non-PII card metadata, never the PAN.
                    last4=card.get("last4"),
                    exp_month=int(exp_month) if isinstance(exp_month, int) else None,
                    exp_year=int(exp_year) if isinstance(exp_year, int) else None,
                    is_default=bool(default) and obj.get("id") == default,
                )
            )
        return out

    @staticmethod
    def _json_or_raise(resp: httpx.Response, op: str) -> dict:
        """Parse a Stripe JSON response or raise a PII-free provider error."""
        if resp.status_code == 402:
            # A declined charge (card_error). Distinct from an outage: Stripe
            # made no change, and the remedy is the customer's card.
            raise BillingPaymentDeclined(f"Stripe {op} declined: HTTP 402")
        if resp.status_code >= 400:
            # PII-free: status code + op only — never echo the body (it can
            # contain customer detail).
            raise BillingProviderError(f"Stripe {op} failed: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise BillingProviderError(f"Stripe {op} returned non-JSON") from exc

    @staticmethod
    def _timestamp_in_window(raw_timestamp: str) -> bool:
        """Is the header's ``t=`` within the configured replay window?

        Fail-closed on a non-numeric value. Rejects a timestamp too far in the
        FUTURE as well as too far in the past — a clock-skewed or forged
        far-future ``t`` would otherwise buy an attacker an arbitrarily long
        replay window.
        """
        from app.config import settings

        max_age = int(settings.billing_stripe_webhook_max_age_seconds)
        if max_age <= 0:
            return True
        try:
            sent_at = int(raw_timestamp)
        except (TypeError, ValueError):
            return False
        return abs(int(datetime.now(UTC).timestamp()) - sent_at) <= max_age

    def _verify_stripe_signature(self, raw_header: str | None, body: bytes) -> bool:
        """Verify Stripe's ``Stripe-Signature`` scheme (not a bare body HMAC).

        The header is ``t=<unix_ts>,v1=<hex>[,v1=<hex>...]`` and the signed
        payload is ``f"{t}.{body}"`` (HMAC-SHA256 with the endpoint signing
        secret). We recompute that and constant-time-compare against each
        provided ``v1`` digest. Fail-closed: empty secret, missing/garbled
        header, or no match → ``False``.

        **``t`` is checked, not just signed over.** Stripe's own verification
        procedure has two steps, and only the digest half was implemented: the
        timestamp must also be compared against now, within a tolerance
        (`FEOH_BILLING_STRIPE_WEBHOOK_MAX_AGE_SECONDS`, default 300s — the same
        ±5-minute window `/api/approvals/slack` and `/api/approvals/teams`
        already enforce). Without it a captured, correctly-signed event verifies
        forever, and the Redis dedupe only covers its own 72h TTL — so a
        `customer.subscription.deleted` replayed later cancels a subscription
        the customer has since re-taken. The window is not a duplicate of the
        dedupe: dedupe stops the SAME delivery twice, the window stops an OLD
        delivery at all.

        ``<= 0`` disables the age check — deliberate, for an operator replaying
        an archived event during an incident. It is a knob, not the default.

        (``webhook_security.verify_hmac_sha256`` can't be reused directly here —
        it HMACs the body alone, whereas Stripe signs the timestamp-prefixed
        payload. Reusing it would always reject a real Stripe event.)
        """
        if not self._webhook_secret or not raw_header:
            return False
        timestamp: str | None = None
        v1_signatures: list[str] = []
        for part in raw_header.split(","):
            key, _, value = part.partition("=")
            key = key.strip()
            value = value.strip()
            if key == "t":
                timestamp = value
            elif key == "v1" and value:
                v1_signatures.append(value)
        if not timestamp or not v1_signatures:
            return False
        if not self._timestamp_in_window(timestamp):
            return False
        try:
            signed_payload = b"%s.%s" % (timestamp.encode("utf-8"), body)
            expected = hmac.new(
                self._webhook_secret.encode("utf-8"), signed_payload, hashlib.sha256
            ).hexdigest()
        except Exception:  # noqa: BLE001 — malformed input must fail closed
            return False
        return any(hmac.compare_digest(expected, sig) for sig in v1_signatures)

    def parse_webhook(self, headers: dict, body: bytes) -> BillingWebhookEvent | None:
        """Verify the Stripe-Signature HMAC over the raw body and normalize.

        Fail-closed: a missing secret/signature or a bad HMAC returns ``None``
        (route 204s silently). Dedupe-by-event-id is the route's job via
        ``webhook_security.is_event_already_processed``.
        """
        signature = extract_signature_header(headers, "Stripe-Signature", "stripe-signature")
        if not self._verify_stripe_signature(signature, body):
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        event_id = payload.get("id")
        event_type = payload.get("type")
        if not event_id or not event_type:
            return None
        obj = (payload.get("data") or {}).get("object") or {}
        if not isinstance(obj, dict):
            return None
        event_type = str(event_type)
        if event_type.startswith("customer.subscription."):
            # The object IS the subscription: its status and window are the
            # provider's own, so both are carried for the sync.
            raw_status = obj.get("status")
            start, end = _period_of(obj)
            return BillingWebhookEvent(
                event_id=str(event_id),
                event_type=event_type,
                external_subscription_id=_id_of(obj.get("id")),
                status=_STATUS_MAP.get(raw_status) if raw_status else None,
                current_period_start=start,
                current_period_end=end,
            )
        if event_type.startswith("invoice."):
            # The object is an INVOICE: its id and status are the invoice's, not
            # the subscription's, so neither may be read as a lifecycle status.
            # The subscription it bills is carried; `apply_billing_event` decides
            # what the event type means for it.
            return BillingWebhookEvent(
                event_id=str(event_id),
                event_type=event_type,
                external_subscription_id=_invoice_subscription_id(obj),
                status=None,
            )
        # Any other event (customer.*, payment_method.*, …) has no lifecycle
        # effect here.
        return BillingWebhookEvent(
            event_id=str(event_id), event_type=event_type, external_subscription_id=None
        )

    async def test_connection(self) -> bool:
        # Fail closed without a key rather than reporting healthy.
        return bool(self._api_key)
