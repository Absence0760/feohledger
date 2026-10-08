"""Platform billing & metering — first slice.

Covers:
  * usage rollup math is Decimal-exact (pure-ish; uses the real control DB)
  * the mock billing adapter is the default + is deterministic
  * the stripe_billing adapter fails closed without a key
  * entitlement helpers allow/deny by plan
  * GET /api/billing/subscription returns the org's plan + status + usage
  * the /api/v1 public surface is plan-gated (402 without `public_api`, 200 with)

The adapter + entitlement-helper tests are pure (no DB) and always run. The
endpoint + rollup tests use the real-Postgres harness. Control-plane rows
(plans / subscriptions / extraction_usage) are NOT truncated between tests, so
every test that inserts them cleans up in a ``finally`` keyed to its org.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import delete

from app.models.billing import Plan, Subscription
from app.models.usage import ExtractionUsage
from app.models.virtual_card import CardRebate
from app.services.billing.entitlements import has_entitlement
from app.services.billing.usage_rollup import CurrencyTotal, UsageRollup, rollup_usage
from app.services.billing_adapters import get_billing_adapter
from app.services.billing_adapters.base import (
    CreateSubscriptionRequest,
    ProviderInvoice,
    ProviderPaymentMethod,
    ProviderSetupIntent,
)
from app.services.billing_adapters.mock_adapter import MockBillingAdapter
from app.services.billing_adapters.stripe_billing import (
    BillingNotConfigured,
    StripeBillingAdapter,
)

PERIOD = "2026-06"


# ---------------------------------------------------------------------------
# Pure unit tests — no DB.
# ---------------------------------------------------------------------------


def test_get_billing_adapter_defaults_to_mock():
    adapter = get_billing_adapter()
    assert isinstance(adapter, MockBillingAdapter)
    assert adapter.provider_name == "mock"


def test_unknown_provider_falls_back_to_mock():
    assert isinstance(get_billing_adapter("does-not-exist"), MockBillingAdapter)


def test_stripe_adapter_selectable_by_name():
    assert isinstance(get_billing_adapter("stripe_billing"), StripeBillingAdapter)


@pytest.mark.asyncio
async def test_mock_adapter_is_deterministic():
    adapter = MockBillingAdapter()
    org_id = "org-123"
    req = CreateSubscriptionRequest(
        organization_id=org_id, plan_code="growth", monthly_price=Decimal("49.00"), trial_days=14
    )
    a = await adapter.create_subscription(req)
    b = await adapter.create_subscription(req)
    assert a.external_subscription_id == b.external_subscription_id == f"mock_sub_{org_id}"
    assert a.status == "trialing"  # trial_days > 0
    assert await adapter.test_connection() is True


def test_mock_adapter_parses_dev_webhook_envelope():
    adapter = MockBillingAdapter()
    body = (
        b'{"id": "evt_1", "type": "subscription.updated", '
        b'"subscription": "sub_1", "status": "active"}'
    )
    evt = adapter.parse_webhook({}, body)
    assert evt is not None
    assert evt.event_id == "evt_1"
    assert evt.event_type == "subscription.updated"
    assert evt.status == "active"
    # Malformed → None (route 204s silently).
    assert adapter.parse_webhook({}, b"not json") is None
    assert adapter.parse_webhook({}, b'{"type": "x"}') is None  # missing id


@pytest.mark.asyncio
async def test_stripe_adapter_fails_closed_without_key():
    adapter = StripeBillingAdapter({"stripe_api_key": "", "stripe_webhook_secret": ""})
    req = CreateSubscriptionRequest(
        organization_id="o", plan_code="growth", monthly_price=Decimal("49.00")
    )
    with pytest.raises(BillingNotConfigured):
        await adapter.create_subscription(req)
    # No key → test_connection reports unhealthy (never a false green).
    assert await adapter.test_connection() is False


def test_stripe_webhook_rejected_without_valid_hmac():
    # No secret configured → signature verification fails closed → None. Also a
    # well-formed header with the wrong secret → None.
    adapter = StripeBillingAdapter({"stripe_api_key": "sk_test", "stripe_webhook_secret": ""})
    assert adapter.parse_webhook({"Stripe-Signature": "t=1,v1=deadbeef"}, b'{"id":"x"}') is None
    adapter2 = StripeBillingAdapter({"stripe_api_key": "sk", "stripe_webhook_secret": "whsec"})
    # Bare-hex (non-Stripe) header is rejected — Stripe signs t.body, not body.
    bare = adapter2.parse_webhook({"Stripe-Signature": "deadbeef"}, b'{"id":"x","type":"y"}')
    assert bare is None


def test_stripe_webhook_accepts_valid_hmac():
    import hashlib
    import hmac
    import time

    secret = "whsec_test"
    body = (
        b'{"id": "evt_9", "type": "customer.subscription.updated", '
        b'"data": {"object": {"id": "sub_9", "status": "past_due"}}}'
    )
    # Stripe signs the timestamp-prefixed payload `t.body`, header `t=...,v1=...`,
    # and `t` must be inside the replay-tolerance window — so sign as of NOW,
    # exactly as a real delivery does.
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), b"%s.%s" % (ts.encode(), body), hashlib.sha256).hexdigest()
    adapter = StripeBillingAdapter({"stripe_api_key": "sk", "stripe_webhook_secret": secret})
    evt = adapter.parse_webhook({"Stripe-Signature": f"t={ts},v1={sig}"}, body)
    assert evt is not None
    assert evt.event_id == "evt_9"
    assert evt.external_subscription_id == "sub_9"
    assert evt.status == "past_due"  # mapped


def test_has_entitlement_truthiness():
    ents = {"public_api": True, "max_seats": 25, "disabled_feature": False}
    assert has_entitlement(ents, "public_api") is True
    assert has_entitlement(ents, "max_seats") is True  # truthy non-bool
    assert has_entitlement(ents, "disabled_feature") is False
    assert has_entitlement(ents, "missing") is False
    assert has_entitlement({}, "public_api") is False  # no plan → no entitlement


def test_usage_rollup_as_meters_serialises_decimal_as_string():
    r = UsageRollup(
        organization_id="o",
        period=PERIOD,
        extractions=3,
        extractions_platform=2,
        card_rebate_totals=(CurrencyTotal(currency="USD", amount=Decimal("12.34")),),
    )
    meters = r.as_meters()
    assert meters["extractions"] == "3"
    assert meters["extractions_platform"] == "2"
    assert meters["card_rebate_total.USD"] == "12.34"
    assert all(isinstance(v, str) for v in meters.values())


def test_the_rebate_meter_names_the_currency_it_is_denominated_in():
    """It was one cross-currency `sum(card_rebates.amount)` — a quantity in no
    currency at all, on a meter a later slice prices. There is no rate to turn
    a mixed scalar into a charge with, so the currency is in the meter NAME.
    """
    r = UsageRollup(
        organization_id="o",
        period=PERIOD,
        card_rebate_totals=(
            CurrencyTotal(currency="EUR", amount=Decimal("7.00")),
            CurrencyTotal(currency="USD", amount=Decimal("10.00")),
        ),
    )
    meters = r.as_meters()
    assert meters["card_rebate_total.EUR"] == "7.00"
    assert meters["card_rebate_total.USD"] == "10.00"
    # Never a bare, mixed key — 17.00 is the figure this fix removes.
    assert "card_rebate_total" not in meters
    assert "17.00" not in meters.values()


def test_no_rebates_emits_no_rebate_meter_rather_than_an_unstated_zero():
    """Zero rebates in an unstated currency is not a fact. One shape either
    way: a consumer reads every `card_rebate_total.` key and finds none."""
    meters = UsageRollup(organization_id="o", period=PERIOD, extractions=1).as_meters()
    assert [k for k in meters if k.startswith("card_rebate_total")] == []
    assert meters["extractions"] == "1"


def test_usage_meters_are_tenant_tables_not_control_plane():
    """`rollup_usage` is handed a TENANT session, and that has to stay true.

    `extraction_usage` / `card_rebates` are absent from `CONTROL_TABLES`, which
    is exactly what makes `tenant_provisioning` create them in every tenant DB
    and in none of the control plane — `to_regclass('extraction_usage')` is NULL
    in `feohledger` however that database was built (`alembic upgrade head`, or
    `scripts/seed.py`, which creates only `CONTROL_TABLES` there).

    Three docstrings used to call these "control-plane meters", and that claim is
    what makes a control-plane session look like the right thing to hand
    `rollup_usage`. Moving either table into `CONTROL_TABLES` without also
    switching `api/billing.get_subscription` off `get_tenant_db` would report
    zero usage on the billing surface, silently.
    """
    from app.models.usage import ExtractionUsage as _ExtractionUsage
    from app.models.virtual_card import CardRebate as _CardRebate
    from app.services.tenant_provisioning import CONTROL_TABLES

    assert _ExtractionUsage.__tablename__ not in CONTROL_TABLES
    assert _CardRebate.__tablename__ not in CONTROL_TABLES
    # …while the billing model the meters feed IS control-plane.
    assert Plan.__tablename__ in CONTROL_TABLES
    assert Subscription.__tablename__ in CONTROL_TABLES


@pytest.mark.asyncio
async def test_mock_list_invoices_is_deterministic():
    adapter = MockBillingAdapter()
    a = await adapter.list_invoices(customer_id="mock_cus_org-1")
    b = await adapter.list_invoices(customer_id="mock_cus_org-1")
    assert a == b  # frozen dataclasses → value-equality, fully deterministic
    assert len(a) == 6  # capped synthetic run
    # Newest first: latest is open, the rest paid; money is an exact string.
    assert a[0].status == "open"
    assert all(inv.status == "paid" for inv in a[1:])
    assert all(inv.amount == "49.00" for inv in a)
    assert all(isinstance(inv.amount, str) for inv in a)
    assert all(isinstance(inv, ProviderInvoice) for inv in a)
    # No customer (never provisioned) → empty list, not an error.
    assert await adapter.list_invoices(customer_id=None) == []
    assert await adapter.list_invoices(customer_id="") == []


@pytest.mark.asyncio
async def test_stripe_list_invoices_fails_closed_without_key():
    adapter = StripeBillingAdapter({"stripe_api_key": "", "stripe_webhook_secret": ""})
    with pytest.raises(BillingNotConfigured):
        await adapter.list_invoices(customer_id="cus_1")


@pytest.mark.asyncio
async def test_stripe_list_invoices_maps_shape():
    """Stripe REST list → normalized ProviderInvoice, against a mocked transport
    (no network). Amount is exact decimal-string from integer minor units; status
    is mapped; no-customer short-circuits to [] without a call."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "in_2",
                        "number": "AP-0002",
                        "status": "open",
                        "total": 4900,
                        "currency": "usd",
                        "created": 1717200000,
                        "period_start": 1717200000,
                        "hosted_invoice_url": "https://pay.stripe.test/in_2",
                    },
                    {
                        "id": "in_1",
                        "number": "AP-0001",
                        "status": "paid",
                        "total": 12999,
                        "currency": "usd",
                        "created": 1714521600,
                        "invoice_pdf": "https://pay.stripe.test/in_1.pdf",
                    },
                ],
            },
        )

    adapter = StripeBillingAdapter({"stripe_api_key": "sk_live", "stripe_webhook_secret": "whsec"})
    transport = httpx.MockTransport(handler)
    adapter._client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        base_url="https://api.stripe.test", auth=(adapter._api_key, ""), transport=transport
    )

    invoices = await adapter.list_invoices(customer_id="cus_42", limit=10)
    assert captured["path"] == "/v1/invoices"
    assert captured["query"]["customer"] == "cus_42"
    assert captured["query"]["limit"] == "10"
    assert [i.external_invoice_id for i in invoices] == ["in_2", "in_1"]
    assert invoices[0].amount == "49.00"  # 4900 minor units, exact
    assert invoices[1].amount == "129.99"  # 12999 minor units, exact
    assert invoices[0].status == "open"
    assert invoices[1].status == "paid"
    assert invoices[0].currency == "USD"
    assert invoices[0].hosted_url == "https://pay.stripe.test/in_2"
    assert invoices[1].hosted_url == "https://pay.stripe.test/in_1.pdf"
    assert invoices[0].period == "2024-06"
    assert all(isinstance(i.amount, str) for i in invoices)

    # No customer → [] without any HTTP call (handler never re-invoked).
    captured.clear()
    assert await adapter.list_invoices(customer_id=None) == []
    assert captured == {}


@pytest.mark.asyncio
async def test_mock_setup_intent_is_deterministic():
    adapter = MockBillingAdapter()
    a = await adapter.create_setup_intent("mock_cus_org-1")
    b = await adapter.create_setup_intent("mock_cus_org-1")
    assert a == b  # frozen dataclass → value-equality, fully deterministic
    assert isinstance(a, ProviderSetupIntent)
    assert a.external_setup_intent_id == "mock_seti_mock_cus_org-1"
    assert a.client_secret == "mock_seti_mock_cus_org-1_secret"
    assert a.status == "requires_payment_method"
    # No customer (never provisioned) → None, not an error.
    assert await adapter.create_setup_intent(None) is None
    assert await adapter.create_setup_intent("") is None


@pytest.mark.asyncio
async def test_mock_list_payment_methods_is_deterministic_and_pii_safe():
    adapter = MockBillingAdapter()
    a = await adapter.list_payment_methods("mock_cus_org-1")
    b = await adapter.list_payment_methods("mock_cus_org-1")
    assert a == b
    assert len(a) == 1
    pm = a[0]
    assert isinstance(pm, ProviderPaymentMethod)
    assert pm.brand == "visa"
    assert pm.last4 == "4242"
    assert pm.exp_month == 12
    assert pm.exp_year == 2030
    assert pm.is_default is True
    # PII-safe: no attribute carries a full PAN.
    assert not any("pan" in f.lower() for f in vars(pm))
    # No customer → empty list.
    assert await adapter.list_payment_methods(None) == []
    assert await adapter.list_payment_methods("") == []


@pytest.mark.asyncio
async def test_stripe_setup_intent_fails_closed_without_key():
    adapter = StripeBillingAdapter({"stripe_api_key": "", "stripe_webhook_secret": ""})
    with pytest.raises(BillingNotConfigured):
        await adapter.create_setup_intent("cus_1")


@pytest.mark.asyncio
async def test_stripe_list_payment_methods_fails_closed_without_key():
    adapter = StripeBillingAdapter({"stripe_api_key": "", "stripe_webhook_secret": ""})
    with pytest.raises(BillingNotConfigured):
        await adapter.list_payment_methods("cus_1")


@pytest.mark.asyncio
async def test_stripe_setup_intent_maps_shape():
    """Stripe /v1/setup_intents POST → normalized ProviderSetupIntent, against a
    mocked transport (no network). No-customer short-circuits to None."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "id": "seti_123",
                "client_secret": "seti_123_secret_abc",
                "status": "requires_payment_method",
            },
        )

    adapter = StripeBillingAdapter({"stripe_api_key": "sk_live", "stripe_webhook_secret": "whsec"})
    transport = httpx.MockTransport(handler)
    adapter._client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        base_url="https://api.stripe.test", auth=(adapter._api_key, ""), transport=transport
    )

    intent = await adapter.create_setup_intent("cus_42")
    assert captured["path"] == "/v1/setup_intents"
    assert intent is not None
    assert intent.external_setup_intent_id == "seti_123"
    assert intent.client_secret == "seti_123_secret_abc"
    assert intent.status == "requires_payment_method"

    captured.clear()
    assert await adapter.create_setup_intent(None) is None
    assert captured == {}


@pytest.mark.asyncio
async def test_stripe_list_payment_methods_maps_shape_pii_safe():
    """Stripe /v1/payment_methods GET → normalized cards, against a mocked
    transport. Brand/last4/exp only — never a full PAN."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "pm_1",
                        "card": {
                            "brand": "visa",
                            "last4": "4242",
                            "exp_month": 12,
                            "exp_year": 2030,
                        },
                    },
                    {
                        "id": "pm_2",
                        "card": {
                            "brand": "mastercard",
                            "last4": "5454",
                            "exp_month": 6,
                            "exp_year": 2029,
                        },
                    },
                ],
            },
        )

    adapter = StripeBillingAdapter({"stripe_api_key": "sk_live", "stripe_webhook_secret": "whsec"})
    transport = httpx.MockTransport(handler)
    adapter._client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        base_url="https://api.stripe.test", auth=(adapter._api_key, ""), transport=transport
    )

    methods = await adapter.list_payment_methods("cus_42")
    assert captured["path"] == "/v1/payment_methods"
    assert captured["query"]["customer"] == "cus_42"
    assert captured["query"]["type"] == "card"
    assert [m.external_payment_method_id for m in methods] == ["pm_1", "pm_2"]
    assert methods[0].brand == "visa"
    assert methods[0].last4 == "4242"
    assert methods[0].exp_month == 12
    assert methods[0].exp_year == 2030
    assert methods[1].brand == "mastercard"

    captured.clear()
    assert await adapter.list_payment_methods(None) == []
    assert captured == {}


# ---------------------------------------------------------------------------
# Real-DB: usage rollup math + endpoint + plan gating.
# ---------------------------------------------------------------------------


async def _cleanup_billing(realdb, org_id):
    """Remove the plan/subscription (control plane) + usage (tenant DB) rows a
    test created — neither is truncated by the fixture. All billing tests act as
    tenant "a"."""
    # The shared teardown owner (`RealDB.purge_plans`) deletes the subscription
    # children before the plans; see conftest for why it takes a prefix.
    await realdb.purge_plans("test_", org_ids=[org_id])
    # extraction_usage / card_rebates are per-tenant tables — clean them in the
    # tenant DB, not the control plane.
    async with realdb.sessionmaker("a")() as s:
        await s.execute(delete(ExtractionUsage).where(ExtractionUsage.organization_id == org_id))
        await s.execute(delete(CardRebate).where(CardRebate.organization_id == org_id))
        await s.commit()


async def _seed_plan(realdb, *, code, entitlements, price="49.00"):
    plan_id = uuid.uuid4()
    async with realdb.control_sessionmaker()() as s:
        s.add(
            Plan(
                id=plan_id,
                code=code,
                name=code.title(),
                monthly_price=Decimal(price),
                currency="USD",
                entitlements=entitlements,
                trial_days=14,
            )
        )
        await s.commit()
    return plan_id


async def _seed_subscription(realdb, *, org_id, plan_id, status="active"):
    async with realdb.control_sessionmaker()() as s:
        s.add(
            Subscription(
                id=uuid.uuid4(),
                organization_id=org_id,
                plan_id=plan_id,
                status=status,
                current_period_start=datetime(2026, 6, 1, tzinfo=UTC),
                current_period_end=datetime(2026, 6, 30, tzinfo=UTC),
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_rollup_usage_is_decimal_exact(realdb):
    org_id = realdb.info("a").org_id
    try:
        # extraction_usage is a per-tenant table — seed + read it on the tenant DB.
        async with realdb.sessionmaker("a")() as s:
            for i in range(3):
                s.add(
                    ExtractionUsage(
                        id=uuid.uuid4(),
                        invoice_id=uuid.uuid4(),
                        provider="mock",
                        program_type="platform" if i < 2 else "byok",
                        period=PERIOD,
                        organization_id=org_id,
                    )
                )
            await s.commit()

        async with realdb.sessionmaker("a")() as s:
            rollup = await rollup_usage(s, organization_id=org_id, period=PERIOD)
        assert rollup.extractions == 3
        assert rollup.extractions_platform == 2
        # No rebates at all → no currency group, rather than a zero in an
        # unstated currency.
        assert rollup.card_rebate_totals == ()
        # A period with no activity returns a zero-filled rollup, never None.
        async with realdb.sessionmaker("a")() as s:
            empty = await rollup_usage(s, organization_id=org_id, period="1999-01")
        assert empty.extractions == 0
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_subscription_endpoint_returns_plan_status_and_usage(realdb):
    org_id = realdb.info("a").org_id
    try:
        plan_id = await _seed_plan(
            realdb, code="test_growth", entitlements={"public_api": True, "max_seats": 25}
        )
        await _seed_subscription(realdb, org_id=org_id, plan_id=plan_id, status="active")
        # Seed one extraction so usage-to-date is non-zero for the current period
        # (extraction_usage is per-tenant — seed it in the tenant DB).
        period = datetime.now(UTC).strftime("%Y-%m")
        async with realdb.sessionmaker("a")() as s:
            s.add(
                ExtractionUsage(
                    id=uuid.uuid4(),
                    invoice_id=uuid.uuid4(),
                    provider="mock",
                    program_type="platform",
                    period=period,
                    organization_id=org_id,
                )
            )
            await s.commit()

        async with realdb.client(key="a", role="admin") as c:
            resp = await c.get("/api/billing/subscription")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["plan"]["code"] == "test_growth"
        assert body["plan"]["monthly_price"] == "49.00"  # exact string, not float
        assert body["plan"]["entitlements"]["public_api"] is True
        assert body["subscription"]["status"] == "active"
        assert body["subscription"]["externally_managed"] is False
        assert body["provider"] == "mock"
        assert body["usage"]["extractions"] == "1"
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_subscription_endpoint_admin_or_cfo_only(realdb):
    org_id = realdb.info("a").org_id
    try:
        async with realdb.client(key="a", role="ap_clerk") as c:
            resp = await c.get("/api/billing/subscription")
        assert resp.status_code == 403
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.get("/api/billing/subscription")
        assert resp.status_code == 200
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_no_subscription_yields_null_plan(realdb):
    org_id = realdb.info("a").org_id
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.get("/api/billing/subscription")
        assert resp.status_code == 200
        body = resp.json()
        assert body["plan"] is None
        assert body["subscription"] is None
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_list_plans_returns_active_plans_ordered_by_price(realdb):
    """GET /api/billing/plans is the plan-change picker's data source: only
    active plans, cheapest first, money as an exact string — never float."""
    org_id = realdb.info("a").org_id
    try:
        await _seed_plan(realdb, code="test_pricey", entitlements={}, price="199.00")
        await _seed_plan(realdb, code="test_cheap", entitlements={"public_api": True}, price="9.00")
        # An inactive plan must never appear in the picker.
        async with realdb.control_sessionmaker()() as s:
            s.add(
                Plan(
                    id=uuid.uuid4(),
                    code="test_retired",
                    name="Retired",
                    monthly_price=Decimal("1.00"),
                    currency="USD",
                    entitlements={},
                    trial_days=0,
                    is_active=False,
                )
            )
            await s.commit()

        async with realdb.client(key="a", role="admin") as c:
            resp = await c.get("/api/billing/plans")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        codes = [p["code"] for p in body["plans"]]
        assert "test_retired" not in codes
        assert "test_cheap" in codes and "test_pricey" in codes
        # Cheapest-first ordering, at least relative to each other.
        assert codes.index("test_cheap") < codes.index("test_pricey")
        cheap = next(p for p in body["plans"] if p["code"] == "test_cheap")
        assert cheap["monthly_price"] == "9.00"
        assert isinstance(cheap["monthly_price"], str)  # exact string, not float
        assert cheap["entitlements"]["public_api"] is True
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_list_plans_admin_or_cfo_only(realdb):
    org_id = realdb.info("a").org_id
    try:
        async with realdb.client(key="a", role="ap_clerk") as c:
            resp = await c.get("/api/billing/plans")
        assert resp.status_code == 403
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.get("/api/billing/plans")
        assert resp.status_code == 200
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_public_api_is_plan_gated(realdb):
    """The /api/v1 surface 402s when the plan lacks `public_api`, 200s when it
    includes it. Demonstrates require_api_entitlement composing with the scope."""
    from app.services.api_keys import KEY_BRAND  # noqa: F401

    org_id = realdb.info("a").org_id
    try:
        # Mint a read key for tenant A. Minting is itself plan-gated
        # (docs/decisions.md §258), so hold Scale just long enough to mint,
        # then drop the subscription so the key is exercised plan-less.
        await realdb.subscribe("a", "scale")
        async with realdb.client(key="a", role="admin") as c:
            mint = await c.post("/api/api-keys", json={"name": "billing-test"})
        assert mint.status_code == 201, mint.text
        api_key = mint.json()["key"]
        async with realdb.control_sessionmaker()() as s:
            await s.execute(delete(Subscription).where(Subscription.organization_id == org_id))
            await s.commit()

        def _key_client():
            cc = realdb.client(key="a", role=None)
            cc.headers.pop("X-Tenant-Slug", None)
            cc.headers["X-API-Key"] = api_key
            return cc

        # No plan at all → no entitlement → 402.
        async with _key_client() as c:
            resp = await c.get("/api/v1/invoices")
        assert resp.status_code == 402, resp.text

        # Plan WITHOUT public_api → still 402.
        plan_no = await _seed_plan(realdb, code="test_free", entitlements={"public_api": False})
        await _seed_subscription(realdb, org_id=org_id, plan_id=plan_no, status="active")
        async with _key_client() as c:
            resp = await c.get("/api/v1/invoices")
        assert resp.status_code == 402

        # Swap to a plan WITH public_api → 200.
        async with realdb.control_sessionmaker()() as s:
            await s.execute(delete(Subscription).where(Subscription.organization_id == org_id))
            await s.commit()
        plan_yes = await _seed_plan(realdb, code="test_scale", entitlements={"public_api": True})
        await _seed_subscription(realdb, org_id=org_id, plan_id=plan_yes, status="active")
        async with _key_client() as c:
            resp = await c.get("/api/v1/invoices")
        assert resp.status_code == 200, resp.text
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_billing_invoices_endpoint_returns_mock_list(realdb):
    """GET /api/billing/invoices returns the adapter's invoices with money as
    exact strings. The default `mock` provider yields data once a customer id is
    persisted on settings.billing — mirroring a provisioned org."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.organization import Organization

    org_id = realdb.info("a").org_id
    try:
        # Persist a customer id so the mock adapter has something to list.
        async with realdb.control_sessionmaker()() as s:
            org = await s.get(Organization, uuid.UUID(str(org_id)))
            settings_dict = dict(org.settings or {})
            settings_dict["billing"] = {
                **(settings_dict.get("billing") or {}),
                "stripe_customer_id": "mock_cus_test",
            }
            org.settings = settings_dict
            flag_modified(org, "settings")
            await s.commit()

        async with realdb.client(key="a", role="admin") as c:
            resp = await c.get("/api/billing/invoices")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"] == "mock"
        assert len(body["invoices"]) == 6
        first = body["invoices"][0]
        assert first["amount"] == "49.00"  # exact string, not float
        assert isinstance(first["amount"], str)
        assert first["status"] == "open"
        assert first["currency"] == "USD"
        assert all(inv["amount"] == "49.00" for inv in body["invoices"])
    finally:
        async with realdb.control_sessionmaker()() as s:
            org = await s.get(Organization, uuid.UUID(str(org_id)))
            settings_dict = dict(org.settings or {})
            settings_dict.pop("billing", None)
            org.settings = settings_dict
            flag_modified(org, "settings")
            await s.commit()
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_billing_invoices_endpoint_no_customer_yields_empty_list(realdb):
    """An org never provisioned with the provider (no customer id) → empty list,
    HTTP 200, never a 500."""
    org_id = realdb.info("a").org_id
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.get("/api/billing/invoices")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["invoices"] == []
        assert body["provider"] == "mock"
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_billing_invoices_endpoint_admin_or_cfo_only(realdb):
    org_id = realdb.info("a").org_id
    try:
        async with realdb.client(key="a", role="ap_clerk") as c:
            resp = await c.get("/api/billing/invoices")
        assert resp.status_code == 403
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.get("/api/billing/invoices")
        assert resp.status_code == 200
    finally:
        await _cleanup_billing(realdb, org_id)


async def _set_customer_id(realdb, org_id, customer_id):
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.organization import Organization

    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, uuid.UUID(str(org_id)))
        settings_dict = dict(org.settings or {})
        settings_dict["billing"] = {
            **(settings_dict.get("billing") or {}),
            "stripe_customer_id": customer_id,
        }
        org.settings = settings_dict
        flag_modified(org, "settings")
        await s.commit()


async def _clear_billing_settings(realdb, org_id):
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.organization import Organization

    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, uuid.UUID(str(org_id)))
        settings_dict = dict(org.settings or {})
        settings_dict.pop("billing", None)
        org.settings = settings_dict
        flag_modified(org, "settings")
        await s.commit()


@pytest.mark.asyncio
async def test_setup_intent_endpoint_returns_client_secret(realdb):
    """POST /api/billing/payment-method/setup-intent returns the mock adapter's
    deterministic client_secret once a customer id is persisted."""
    org_id = realdb.info("a").org_id
    try:
        await _set_customer_id(realdb, org_id, "mock_cus_test")
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post("/api/billing/payment-method/setup-intent")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"] == "mock"
        assert body["configured"] is True
        assert body["client_secret"] == "mock_seti_mock_cus_test_secret"
        assert body["setup_intent_id"] == "mock_seti_mock_cus_test"
    finally:
        await _clear_billing_settings(realdb, org_id)
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_setup_intent_endpoint_no_customer_yields_not_configured(realdb):
    """No customer id → configured=false, null client_secret, HTTP 200 (never 500)."""
    org_id = realdb.info("a").org_id
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post("/api/billing/payment-method/setup-intent")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["configured"] is False
        assert body["client_secret"] is None
        assert body["setup_intent_id"] is None
        assert body["provider"] == "mock"
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_setup_intent_endpoint_admin_or_cfo_only(realdb):
    org_id = realdb.info("a").org_id
    try:
        async with realdb.client(key="a", role="ap_clerk") as c:
            resp = await c.post("/api/billing/payment-method/setup-intent")
        assert resp.status_code == 403
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.post("/api/billing/payment-method/setup-intent")
        assert resp.status_code == 200
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_payment_methods_endpoint_returns_pii_safe_list(realdb):
    """GET /api/billing/payment-methods returns brand/last4/exp only — never a PAN."""
    org_id = realdb.info("a").org_id
    try:
        await _set_customer_id(realdb, org_id, "mock_cus_test")
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.get("/api/billing/payment-methods")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"] == "mock"
        assert len(body["payment_methods"]) == 1
        pm = body["payment_methods"][0]
        assert pm["brand"] == "visa"
        assert pm["last4"] == "4242"
        assert pm["exp_month"] == 12
        assert pm["exp_year"] == 2030
        assert pm["is_default"] is True
        # PII-safe: no full PAN field is serialized.
        assert "pan" not in {k.lower() for k in pm}
        assert "number" not in {k.lower() for k in pm}
    finally:
        await _clear_billing_settings(realdb, org_id)
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_payment_methods_endpoint_no_customer_yields_empty_list(realdb):
    org_id = realdb.info("a").org_id
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.get("/api/billing/payment-methods")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["payment_methods"] == []
        assert body["provider"] == "mock"
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_payment_methods_endpoint_admin_or_cfo_only(realdb):
    org_id = realdb.info("a").org_id
    try:
        async with realdb.client(key="a", role="ap_clerk") as c:
            resp = await c.get("/api/billing/payment-methods")
        assert resp.status_code == 403
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.get("/api/billing/payment-methods")
        assert resp.status_code == 200
    finally:
        await _cleanup_billing(realdb, org_id)


@pytest.mark.asyncio
async def test_rollup_groups_rebates_by_the_currency_of_their_card(realdb):
    """The join is the point: `card_rebates` has no currency column, so a
    rebate's denomination is only knowable through the card it accrued on.

    Pre-fix this was one `sum(card_rebates.amount)` = 22.00, a figure in no
    currency, on a meter a later slice prices.
    """
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.virtual_card import VirtualCard

    org_id = realdb.info("a").org_id
    try:
        async with realdb.sessionmaker("a")() as s:
            for currency, amount in (("USD", "10.00"), ("EUR", "7.00"), ("USD", "5.00")):
                inv = Invoice(
                    id=uuid.uuid4(),
                    invoice_number=f"BILL-{uuid.uuid4().hex[:8]}",
                    vendor_name="V",
                    amount=Decimal(amount),
                    currency=currency,
                    status=InvoiceStatus.paid,
                    organization_id=org_id,
                    correlation_id=uuid.uuid4(),
                )
                s.add(inv)
                await s.flush()
                card = VirtualCard(
                    id=uuid.uuid4(),
                    invoice_id=inv.id,
                    organization_id=org_id,
                    card_provider="mock",
                    provider_card_id=f"card_{uuid.uuid4().hex[:10]}",
                    amount_limit=Decimal(amount),
                    status="active",
                    currency=currency,
                )
                s.add(card)
                await s.flush()
                s.add(
                    CardRebate(
                        virtual_card_id=card.id,
                        organization_id=org_id,
                        amount=Decimal(amount),
                        rate=Decimal("0.0100"),
                        status="confirmed",
                        period=PERIOD,
                    )
                )
            await s.commit()

        async with realdb.sessionmaker("a")() as s:
            rollup = await rollup_usage(s, organization_id=org_id, period=PERIOD)

        # Sorted by code, exact, and per-currency — the two USD rebates summed
        # together, the EUR one kept apart.
        assert rollup.card_rebate_totals == (
            CurrencyTotal(currency="EUR", amount=Decimal("7.00")),
            CurrencyTotal(currency="USD", amount=Decimal("15.00")),
        )
        assert all(isinstance(t.amount, Decimal) for t in rollup.card_rebate_totals)

        meters = rollup.as_meters()
        assert meters["card_rebate_total.USD"] == "15.00"
        assert meters["card_rebate_total.EUR"] == "7.00"
        assert "card_rebate_total" not in meters
    finally:
        await _cleanup_billing(realdb, org_id)
