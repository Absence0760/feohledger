"""Live Stripe subscriptions behind a plan change (decisions §480).

Against an in-test, stateful fake of the Stripe REST endpoints the adapter
calls (an ``httpx.MockTransport`` — no network, no key; ``stripe-mock`` is
stateless, so it cannot show a subscription created by one call being amended
by the next). Three groups:

* the adapter's wire shape — create anchored on the 1st with the metered item,
  ``error_if_incomplete``, the pinned ``Stripe-Version``; item swap by
  ``usage_type``; cancel with ``invoice_now`` + ``prorate``; a 402 is a
  decline, not an outage; SetupIntent adoption refuses a foreign or unfinished
  intent; webhook parsing reads an invoice's subscription from ``parent``;
* ``change_plan`` on the real-Postgres harness for every path — free → paid
  (no card refused, card → created), paid → paid (updated), paid → free
  (canceled, id cleared), a decline changing nothing, adopting a subscription a
  dead attempt left behind, the per-org lease, and the deployed + ``mock``
  refusal (with the downgrade still allowed);
* the webhook keeping our row in sync — invoice events move status only along
  their narrow edges, the provider's window is copied, ``canceled`` is terminal
  — and the add-card adoption endpoint.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.billing import Plan, Subscription
from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services.billing.plan_change import change_plan
from app.services.billing.provider_sync import (
    LEASE_KEY,
    ProviderLegRefused,
    next_month_start,
)
from app.services.billing.webhook_processing import apply_billing_event
from app.services.billing_adapters.base import (
    BillingPaymentDeclined,
    BillingWebhookEvent,
    CreateSubscriptionRequest,
    SetupIntentRejected,
)
from app.services.billing_adapters.stripe_billing import StripeBillingAdapter

PREFIX = "sbtest_"
OVERAGE = {"ai_invoices": {"included": 500, "overage_unit_price": "0.10"}}
CHANGE_AT = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# A stateful fake of the slice of Stripe the adapter calls.
# ---------------------------------------------------------------------------


class FakeStripe:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.customers: dict[str, dict] = {}
        self.cards: dict[str, list[str]] = {}  # customer -> pm ids, newest first
        self.subscriptions: dict[str, dict] = {}
        self.setup_intents: dict[str, dict] = {}
        self.prices: dict[str, str] = {}  # lookup_key -> price id
        self.replies: dict[str, tuple[int, dict]] = {}  # Idempotency-Key -> answer
        self.decline = False
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n}"

    def add_card(self, customer: str, *, default: bool = True) -> str:
        pm = self._id("pm")
        self.cards.setdefault(customer, []).insert(0, pm)
        if default:
            self.customer(customer)["invoice_settings"]["default_payment_method"] = pm
        return pm

    def customer(self, cid: str) -> dict:
        return self.customers.setdefault(
            cid, {"id": cid, "invoice_settings": {"default_payment_method": None}}
        )

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    @staticmethod
    def form(request: httpx.Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    def _sub_json(self, sub: dict) -> dict:
        items = []
        for item in sub["items"]:
            metered = item["price"].startswith("price_over")
            items.append(
                {
                    "id": item["id"],
                    "price": {
                        "id": item["price"],
                        "recurring": {"usage_type": "metered" if metered else "licensed"},
                    },
                    "current_period_start": sub["period"][0],
                    "current_period_end": sub["period"][1],
                }
            )
        return {
            "id": sub["id"],
            "status": sub["status"],
            "customer": sub["customer"],
            "metadata": sub["metadata"],
            "items": {"data": items},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Stripe's idempotency, as the decline-then-retry path depends on it:
        a request whose `Idempotency-Key` was seen before gets the stored
        answer replayed — a 402 included — without being executed again."""
        self.requests.append(request)
        key = request.headers.get("Idempotency-Key")
        if key and key in self.replies:
            status, body = self.replies[key]
            return httpx.Response(status, json=body)
        response = self._route(request)
        if key:
            self.replies[key] = (response.status_code, response.json())
        return response

    def _route(self, request: httpx.Request) -> httpx.Response:  # noqa: C901 — one router
        path, method = request.url.path, request.method
        if method == "POST" and path == "/v1/customers":
            cid = self._id("cus")
            self.customer(cid)
            return httpx.Response(200, json={"id": cid})
        if path.startswith("/v1/customers/"):
            cid = path.rsplit("/", 1)[1]
            if method == "POST":
                pm = self.form(request)["invoice_settings[default_payment_method]"]
                self.customer(cid)["invoice_settings"]["default_payment_method"] = pm
            return httpx.Response(200, json=self.customer(cid))
        if path == "/v1/prices":
            if method == "GET":
                found = self.prices.get(request.url.params["lookup_keys[]"])
                return httpx.Response(200, json={"data": [{"id": found}] if found else []})
            form = self.form(request)
            metered = "recurring[usage_type]" in form
            price = self._id("price_over" if metered else "price")
            self.prices[form["lookup_key"]] = price
            return httpx.Response(200, json={"id": price})
        if path == "/v1/billing/meters":
            if method == "GET":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"id": "mtr_1"})
        if method == "GET" and path == "/v1/payment_methods":
            cid = request.url.params["customer"]
            data = [
                {"id": pm, "card": {"brand": "visa", "last4": "4242"}}
                for pm in self.cards.get(cid, [])
            ]
            return httpx.Response(200, json={"data": data})
        if method == "GET" and path.startswith("/v1/setup_intents/"):
            seti = self.setup_intents.get(path.rsplit("/", 1)[1])
            if seti is None:
                return httpx.Response(404, json={"error": {"type": "invalid_request_error"}})
            return httpx.Response(200, json=seti)
        if path == "/v1/subscriptions":
            if method == "GET":
                cid = request.url.params["customer"]
                data = [
                    self._sub_json(s) for s in self.subscriptions.values() if s["customer"] == cid
                ]
                return httpx.Response(200, json={"data": data})
            if self.decline:
                return httpx.Response(402, json={"error": {"type": "card_error"}})
            form = self.form(request)
            anchor = int(form["billing_cycle_anchor"])
            sid = self._id("sub")
            items = [{"id": self._id("si"), "price": form["items[0][price]"]}]
            if "items[1][price]" in form:
                items.append({"id": self._id("si"), "price": form["items[1][price]"]})
            self.subscriptions[sid] = {
                "id": sid,
                "status": "active",
                "customer": form["customer"],
                "metadata": {"plan_code": form["metadata[plan_code]"]},
                "items": items,
                "period": (int(CHANGE_AT.timestamp()), anchor),
            }
            return httpx.Response(200, json=self._sub_json(self.subscriptions[sid]))
        if path.startswith("/v1/subscriptions/"):
            sub = self.subscriptions.get(path.rsplit("/", 1)[1])
            if sub is None:
                return httpx.Response(404, json={"error": {}})
            if method == "GET":
                return httpx.Response(200, json=self._sub_json(sub))
            if method == "DELETE":
                if sub["status"] == "canceled":
                    return httpx.Response(400, json={"error": {}})
                sub["status"] = "canceled"
                return httpx.Response(200, json=self._sub_json(sub))
            if self.decline:
                return httpx.Response(402, json={"error": {"type": "card_error"}})
            form = self.form(request)
            by_id = {i["id"]: i for i in sub["items"]}
            for n in range(4):
                item_id = form.get(f"items[{n}][id]")
                price = form.get(f"items[{n}][price]")
                if item_id and form.get(f"items[{n}][deleted]") == "true":
                    sub["items"] = [i for i in sub["items"] if i["id"] != item_id]
                elif item_id and price:
                    by_id[item_id]["price"] = price
                elif price:
                    sub["items"].append({"id": self._id("si"), "price": price})
            sub["metadata"]["plan_code"] = form.get("metadata[plan_code]", "")
            return httpx.Response(200, json=self._sub_json(sub))
        return httpx.Response(500, json={"error": {"message": f"unrouted {method} {path}"}})


@pytest.fixture
def fake_stripe(monkeypatch):
    """Route every StripeBillingAdapter through the fake, with a fake key."""
    fake = FakeStripe()
    transport = httpx.MockTransport(fake.handler)

    def _client(self):
        return httpx.AsyncClient(
            base_url="https://api.stripe.test",
            auth=(self._api_key, ""),
            transport=transport,
            headers={"Stripe-Version": self._api_version},
        )

    monkeypatch.setattr(StripeBillingAdapter, "_client", _client)
    monkeypatch.setattr(settings, "billing_provider", "stripe_billing")
    monkeypatch.setattr(settings, "billing_stripe_api_key", "sk_test_fake_not_a_key")
    return fake


def _adapter(fake: FakeStripe, **config) -> StripeBillingAdapter:
    adapter = StripeBillingAdapter(
        {
            "stripe_api_key": "sk_test_fake_not_a_key",
            "stripe_api_version": "2026-11-18.endive",
            **config,
        }
    )
    return adapter


# ---------------------------------------------------------------------------
# Adapter wire shape — no DB.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_anchors_on_the_first_with_the_metered_item(fake_stripe):
    adapter = _adapter(
        fake_stripe,
        stripe_customer_id="cus_x",
        stripe_price_id="price_base",
        stripe_overage_price_id="price_over_1",
    )
    anchor = next_month_start(CHANGE_AT)
    sub = await adapter.create_subscription(
        CreateSubscriptionRequest(
            organization_id="org-1",
            plan_code="growth",
            monthly_price=Decimal("49.00"),
            idempotency_key="feohledger-sub-create-k",
            billing_cycle_anchor=anchor,
        )
    )
    (request,) = fake_stripe.calls("POST", "/v1/subscriptions")
    form = FakeStripe.form(request)
    assert anchor == datetime(2026, 7, 1, tzinfo=UTC)
    assert form["billing_cycle_anchor"] == str(int(anchor.timestamp()))
    assert form["proration_behavior"] == "create_prorations"
    assert form["payment_behavior"] == "error_if_incomplete"
    assert form["items[0][price]"] == "price_base"
    assert form["items[1][price]"] == "price_over_1"
    assert request.headers["Idempotency-Key"] == "feohledger-sub-create-k"
    assert request.headers["Stripe-Version"] == "2026-11-18.endive"
    # The window is read from the item (API 2025-03-31+), not the top level.
    assert sub.current_period_end == anchor
    assert sub.status == "active"


def test_next_month_start_rolls_the_year():
    assert next_month_start(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == datetime(
        2027, 1, 1, tzinfo=UTC
    )
    # Exactly on the 1st still anchors in the FUTURE (Stripe refuses a past anchor).
    assert next_month_start(datetime(2026, 6, 1, tzinfo=UTC)) == datetime(2026, 7, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_dispatcher_pins_the_stripe_api_version(monkeypatch):
    from app.services.billing_adapters import get_billing_adapter

    captured = {}

    def handler(request):
        captured["version"] = request.headers.get("Stripe-Version")
        return httpx.Response(200, json={"id": "sub_1", "status": "active"})

    adapter = get_billing_adapter("stripe_billing")
    adapter.config = {**adapter.config, "stripe_api_key": "sk_test_fake_not_a_key"}
    real_client = adapter._client

    def _client():
        client = real_client()
        client._transport = httpx.MockTransport(handler)
        return client

    adapter._client = _client  # type: ignore[method-assign]
    await adapter.get_subscription("sub_1")
    assert captured["version"] == settings.billing_stripe_api_version
    assert settings.billing_stripe_api_version  # pinned, not the account default


@pytest.mark.asyncio
async def test_change_swaps_items_by_usage_type_and_invoices_now(fake_stripe):
    fake_stripe.subscriptions["sub_9"] = {
        "id": "sub_9",
        "status": "active",
        "customer": "cus_x",
        "metadata": {},
        "items": [{"id": "si_base", "price": "price_a"}, {"id": "si_met", "price": "price_over_a"}],
        "period": (0, 1),
    }
    adapter = _adapter(fake_stripe)
    await adapter.change_subscription_plan(
        "sub_9",
        price_id="price_b",
        overage_price_id="price_over_b",
        plan_code="scale",
        idempotency_key="k1",
    )
    form = FakeStripe.form(fake_stripe.calls("POST", "/v1/subscriptions/sub_9")[0])
    assert form["items[0][id]"] == "si_base" and form["items[0][price]"] == "price_b"
    assert form["items[1][id]"] == "si_met" and form["items[1][price]"] == "price_over_b"
    assert form["proration_behavior"] == "always_invoice"
    assert form["payment_behavior"] == "error_if_incomplete"

    # A plan with no overage drops the metered item rather than leaving it billing.
    await adapter.change_subscription_plan(
        "sub_9", price_id="price_c", overage_price_id=None, plan_code="flat", idempotency_key="k2"
    )
    form = FakeStripe.form(fake_stripe.calls("POST", "/v1/subscriptions/sub_9")[1])
    assert form["items[1][deleted]"] == "true"
    assert [i["price"] for i in fake_stripe.subscriptions["sub_9"]["items"]] == ["price_c"]


@pytest.mark.asyncio
async def test_cancel_invoices_usage_now_and_is_idempotent(fake_stripe):
    fake_stripe.subscriptions["sub_c"] = {
        "id": "sub_c",
        "status": "active",
        "customer": "cus_x",
        "metadata": {},
        "items": [{"id": "si", "price": "price_a"}],
        "period": (0, 1),
    }
    adapter = _adapter(fake_stripe)
    first = await adapter.cancel_subscription("sub_c")
    (delete,) = fake_stripe.calls("DELETE", "/v1/subscriptions/sub_c")
    assert dict(delete.url.params) == {"invoice_now": "true", "prorate": "true"}
    assert first.status == "canceled"
    # A second cancel answers with the canceled subscription, not an error.
    again = await adapter.cancel_subscription("sub_c")
    assert again.status == "canceled"


@pytest.mark.asyncio
async def test_a_402_is_a_decline_not_an_outage(fake_stripe):
    fake_stripe.decline = True
    adapter = _adapter(fake_stripe, stripe_customer_id="cus_x", stripe_price_id="price_a")
    with pytest.raises(BillingPaymentDeclined):
        await adapter.create_subscription(
            CreateSubscriptionRequest(
                organization_id="o", plan_code="g", monthly_price=Decimal("1")
            )
        )


@pytest.mark.asyncio
async def test_default_card_is_promoted_from_the_newest_saved_card(fake_stripe):
    adapter = _adapter(fake_stripe)
    assert await adapter.ensure_default_payment_method("cus_none") is None
    pm = fake_stripe.add_card("cus_y", default=False)
    assert await adapter.ensure_default_payment_method("cus_y") == pm
    assert fake_stripe.customer("cus_y")["invoice_settings"]["default_payment_method"] == pm


@pytest.mark.asyncio
async def test_setup_intent_adoption_refuses_foreign_and_unfinished(fake_stripe):
    adapter = _adapter(fake_stripe)
    fake_stripe.setup_intents["seti_ok"] = {
        "id": "seti_ok",
        "customer": "cus_me",
        "status": "succeeded",
        "payment_method": "pm_new",
    }
    fake_stripe.setup_intents["seti_other"] = {
        "id": "seti_other",
        "customer": "cus_them",
        "status": "succeeded",
        "payment_method": "pm_x",
    }
    fake_stripe.setup_intents["seti_wip"] = {
        "id": "seti_wip",
        "customer": "cus_me",
        "status": "requires_payment_method",
        "payment_method": None,
    }
    assert await adapter.adopt_setup_intent("cus_me", "seti_ok") == "pm_new"
    assert fake_stripe.customer("cus_me")["invoice_settings"]["default_payment_method"] == "pm_new"
    for seti, reason in (
        ("seti_other", "not_found"),
        ("seti_missing", "not_found"),
        ("seti_wip", "incomplete"),
        ("pi_not_a_seti", "not_found"),
        # Never interpolated into a path: this would GET /v1/customers/cus_x.
        ("seti_/../../customers/cus_x", "not_found"),
    ):
        with pytest.raises(SetupIntentRejected) as exc:
            await adapter.adopt_setup_intent("cus_me", seti)
        assert exc.value.reason == reason
    # Another customer's card was never made ours.
    assert fake_stripe.customer("cus_me")["invoice_settings"]["default_payment_method"] == "pm_new"


def _signed(adapter: StripeBillingAdapter, payload: dict) -> tuple[dict, bytes]:
    import hashlib
    import hmac
    import time

    body = json.dumps(payload).encode()
    t = str(int(time.time()))
    digest = hmac.new(b"whsec_fake", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={t},v1={digest}"}, body


def test_parse_webhook_reads_an_invoices_subscription_from_parent():
    adapter = StripeBillingAdapter({"stripe_webhook_secret": "whsec_fake"})
    headers, body = _signed(
        adapter,
        {
            "id": "evt_inv",
            "type": "invoice.payment_failed",
            "data": {
                "object": {
                    "id": "in_1",
                    "status": "open",
                    "parent": {"subscription_details": {"subscription": "sub_42"}},
                }
            },
        },
    )
    event = adapter.parse_webhook(headers, body)
    # The INVOICE's id and status are not the subscription's.
    assert event.external_subscription_id == "sub_42"
    assert event.status is None
    assert event.event_type == "invoice.payment_failed"


def test_parse_webhook_carries_the_subscription_window():
    adapter = StripeBillingAdapter({"stripe_webhook_secret": "whsec_fake"})
    start, end = 1782864000, 1785542400  # 2026-07-01, 2026-08-01 UTC
    headers, body = _signed(
        adapter,
        {
            "id": "evt_sub",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_42",
                    "status": "active",
                    "items": {"data": [{"current_period_start": start, "current_period_end": end}]},
                }
            },
        },
    )
    event = adapter.parse_webhook(headers, body)
    assert event.status == "active"
    assert event.current_period_start == datetime(2026, 7, 1, tzinfo=UTC)
    assert event.current_period_end == datetime(2026, 8, 1, tzinfo=UTC)


def test_parse_webhook_ignores_other_objects_statuses():
    adapter = StripeBillingAdapter({"stripe_webhook_secret": "whsec_fake"})
    headers, body = _signed(
        adapter,
        {
            "id": "evt_pi",
            "type": "payment_intent.canceled",
            "data": {"object": {"id": "pi_1", "status": "canceled"}},
        },
    )
    event = adapter.parse_webhook(headers, body)
    assert event.external_subscription_id is None and event.status is None


# ---------------------------------------------------------------------------
# Real-DB: change_plan against the fake.
# ---------------------------------------------------------------------------


@pytest.fixture
def _audit_engine_on_loop(monkeypatch, realdb):
    """Write dispatch_auth_audit's tenant row on THIS test's loop (see
    test_billing_webhook.py for the full rationale)."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.database import _make_tenant_url

    db_name_by_org = {info.org_id: info.db_name for info in realdb.tenants.values()}

    def _engine(db_name: str):
        return create_async_engine(_make_tenant_url(db_name), poolclass=NullPool)

    async def _resolve(organization_id):
        return db_name_by_org[organization_id]

    monkeypatch.setattr("app.services.audit_dispatch._resolve_tenant_db_name", _resolve)
    monkeypatch.setattr("app.database.get_tenant_engine", _engine)


@pytest.fixture
async def org_a(realdb):
    """Org "a" with no billing block; cleaned up (plans, subs, settings) after."""
    org_id = realdb.info("a").org_id
    await _reset(realdb, org_id)
    yield org_id
    await _reset(realdb, org_id)


async def _reset(realdb, org_id):
    await realdb.purge_plans(PREFIX, org_ids=[org_id])
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        if "billing" in (org.settings or {}):
            new_settings = dict(org.settings)
            new_settings.pop("billing", None)
            org.settings = new_settings
            flag_modified(org, "settings")
        await s.commit()


async def _plan(realdb, code, price, *, overage=True):
    async with realdb.control_sessionmaker()() as s:
        plan = Plan(
            id=uuid.uuid4(),
            code=f"{PREFIX}{code}",
            name=code.title(),
            monthly_price=Decimal(price),
            currency="USD",
            usage_components=OVERAGE
            if overage
            else {"ai_invoices": {"included": 100, "overage_unit_price": None}},
            entitlements={},
            trial_days=14,
        )
        s.add(plan)
        await s.commit()
        return plan.id


async def _sub(realdb, org_id, plan_id, *, external_id=None, status="active"):
    async with realdb.control_sessionmaker()() as s:
        sub = Subscription(
            id=uuid.uuid4(),
            organization_id=org_id,
            plan_id=plan_id,
            status=status,
            current_period_start=datetime(2026, 6, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 7, 1, tzinfo=UTC),
            external_subscription_id=external_id,
        )
        s.add(sub)
        await s.commit()
        return sub.id


async def _set_billing(realdb, org_id, **values):
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        new_settings = dict(org.settings or {})
        new_settings["billing"] = {**(new_settings.get("billing") or {}), **values}
        org.settings = new_settings
        flag_modified(org, "settings")
        await s.commit()


async def _row(realdb, sub_id) -> Subscription:
    async with realdb.control_sessionmaker()() as s:
        return (await s.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()


async def _org_billing(realdb, org_id) -> dict:
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        return dict((org.settings or {}).get("billing") or {})


async def _change(realdb, org_id, code, *, at=CHANGE_AT):
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        return await change_plan(
            s, org=org, new_plan_code=f"{PREFIX}{code}", actor_id=None, change_at=at
        )


async def _audit_details(realdb, sub_id) -> list[dict]:
    async with realdb.sessionmaker("a")() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog.details).where(
                        AuditLog.action == "billing.plan_changed", AuditLog.entity_id == sub_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


@pytest.mark.asyncio
async def test_free_to_paid_without_a_card_is_refused_and_changes_nothing(
    realdb, fake_stripe, org_a
):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)

    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "growth")
    assert exc.value.code == "payment_method_required"
    assert fake_stripe.calls("POST", "/v1/subscriptions") == []
    row = await _row(realdb, sub_id)
    assert row.plan_id == free and row.external_subscription_id is None
    # The lease is released, so the retry after adding a card is not blocked.
    assert LEASE_KEY not in await _org_billing(realdb, org_a)


@pytest.mark.asyncio
async def test_endpoint_answers_payment_method_required_as_a_coded_409(realdb, fake_stripe, org_a):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    await _sub(realdb, org_a, free)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/billing/change-plan", json={"plan_code": f"{PREFIX}growth"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "payment_method_required"


@pytest.mark.asyncio
async def test_free_to_paid_creates_the_stripe_subscription(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")

    result = await _change(realdb, org_a, "growth")

    assert result.changed is True
    (create,) = fake_stripe.calls("POST", "/v1/subscriptions")
    form = FakeStripe.form(create)
    assert form["customer"] == "cus_org"
    assert form["items[1][price]"].startswith("price_over")  # metered overage item
    assert "trial_period_days" not in form  # an upgrade from Free is not a trial
    row = await _row(realdb, sub_id)
    assert row.external_subscription_id == next(iter(fake_stripe.subscriptions))
    assert row.status == "active"
    # The provider's window is what the row now carries: now → the 1st.
    assert row.current_period_end == datetime(2026, 7, 1, tzinfo=UTC)
    # Our estimate is over the calendar month the provider prorates in:
    # 49.00 * 14 / 30 (June 16 12:00 → July 1, floored days).
    assert result.proration.period_days == 30
    assert result.proration.unused_days == 14
    assert result.proration.amount == Decimal("22.87")
    (details,) = await _audit_details(realdb, sub_id)
    assert details["provider"] == "stripe_billing"
    assert details["provider_action"] == "created"
    assert details["external_subscription_id"] == row.external_subscription_id


@pytest.mark.asyncio
async def test_paid_to_paid_updates_items_and_paid_to_free_cancels(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    await _plan(realdb, "scale", "199.00")
    sub_id = await _sub(realdb, org_a, growth)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    fake_stripe.subscriptions["sub_live"] = {
        "id": "sub_live",
        "status": "active",
        "customer": "cus_org",
        "metadata": {},
        "items": [{"id": "si_b", "price": "price_old"}, {"id": "si_m", "price": "price_over_old"}],
        "period": (
            int(datetime(2026, 6, 1, tzinfo=UTC).timestamp()),
            int(datetime(2026, 7, 1, tzinfo=UTC).timestamp()),
        ),
    }
    async with realdb.control_sessionmaker()() as s:
        row = (await s.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()
        row.external_subscription_id = "sub_live"
        await s.commit()

    up = await _change(realdb, org_a, "scale")
    assert up.changed is True
    assert fake_stripe.calls("POST", "/v1/subscriptions") == []  # amended, not created
    (update,) = fake_stripe.calls("POST", "/v1/subscriptions/sub_live")
    assert FakeStripe.form(update)["items[0][id]"] == "si_b"
    assert (await _row(realdb, sub_id)).external_subscription_id == "sub_live"

    down = await _change(realdb, org_a, "free", at=CHANGE_AT + timedelta(days=1))
    assert down.changed is True
    assert fake_stripe.subscriptions["sub_live"]["status"] == "canceled"
    row = await _row(realdb, sub_id)
    # The canceled subscription's id is dropped, so its `deleted` webhook can't
    # cancel the org's now-free row.
    assert row.external_subscription_id is None
    assert row.status == "active"
    actions = [d["provider_action"] for d in await _audit_details(realdb, sub_id)]
    assert sorted(actions) == ["canceled", "updated"]


@pytest.mark.asyncio
async def test_a_declined_upgrade_changes_nothing(realdb, fake_stripe, org_a):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    fake_stripe.decline = True

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/billing/change-plan", json={"plan_code": f"{PREFIX}growth"})
    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"]["code"] == "billing_payment_declined"
    row = await _row(realdb, sub_id)
    assert row.plan_id == free and row.external_subscription_id is None
    assert fake_stripe.subscriptions == {}


@pytest.mark.asyncio
async def test_after_a_decline_a_new_card_is_charged_not_the_replayed_decline(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    """Stripe replays a stored 402 for the same Idempotency-Key for 24 h, and a
    decline changes nothing on our row — so the key must change with the card,
    or the customer who replaced it would be refused again."""
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    fake_stripe.decline = True
    with pytest.raises(BillingPaymentDeclined):
        await _change(realdb, org_a, "growth")

    # Same card, issuer now approves: still the replayed decline (Stripe's
    # behaviour, not ours to change) — the remedy the UI offers is a new card.
    fake_stripe.decline = False
    with pytest.raises(BillingPaymentDeclined):
        await _change(realdb, org_a, "growth")

    fake_stripe.add_card("cus_org")  # the customer replaces the card
    result = await _change(realdb, org_a, "growth")
    assert result.changed is True
    assert (await _row(realdb, sub_id)).external_subscription_id in fake_stripe.subscriptions


@pytest.mark.asyncio
async def test_a_subscription_a_dead_attempt_left_is_adopted_not_duplicated(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    # Stripe created it; our commit never happened.
    fake_stripe.subscriptions["sub_orphan"] = {
        "id": "sub_orphan",
        "status": "active",
        "customer": "cus_org",
        "metadata": {},
        "items": [{"id": "si_b", "price": "price_x"}],
        "period": (0, 1),
    }

    await _change(realdb, org_a, "growth")

    assert fake_stripe.calls("POST", "/v1/subscriptions") == []
    assert len(fake_stripe.subscriptions) == 1
    assert (await _row(realdb, sub_id)).external_subscription_id == "sub_orphan"
    (details,) = await _audit_details(realdb, sub_id)
    assert details["provider_action"] == "adopted"


@pytest.mark.asyncio
async def test_a_held_lease_refuses_and_an_expired_one_is_taken_over(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    fake_stripe.add_card("cus_org")
    held = {"token": "other", "expires_at": (CHANGE_AT + timedelta(minutes=1)).isoformat()}
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org", plan_change_lease=held)

    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "growth")
    assert exc.value.code == "plan_change_in_progress"
    assert fake_stripe.subscriptions == {}
    assert (await _org_billing(realdb, org_a))[LEASE_KEY] == held  # not ours to drop

    await _change(realdb, org_a, "growth", at=CHANGE_AT + timedelta(minutes=5))
    assert (await _row(realdb, sub_id)).external_subscription_id is not None
    assert LEASE_KEY not in await _org_billing(realdb, org_a)


@pytest.mark.asyncio
async def test_deployed_with_mock_refuses_a_paid_plan_but_not_the_way_down(
    realdb, org_a, monkeypatch, _audit_engine_on_loop
):
    monkeypatch.setattr(settings, "billing_provider", "mock")
    monkeypatch.setattr(settings, "environment", "production")
    free = await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/billing/change-plan", json={"plan_code": f"{PREFIX}growth"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "billing_not_configured"
    assert (await _row(realdb, sub_id)).plan_id == free
    # Nothing was provisioned for a change that was refused.
    assert "stripe_customer_id" not in await _org_billing(realdb, org_a)

    # An org somehow already on a paid tier can always leave it.
    async with realdb.control_sessionmaker()() as s:
        row = (await s.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()
        row.plan_id = growth
        await s.commit()
    result = await _change(realdb, org_a, "free")
    assert result.changed is True


@pytest.mark.asyncio
async def test_an_unregistered_provider_name_counts_as_mock_when_deployed(
    realdb, org_a, monkeypatch
):
    """`stripe` (not `stripe_billing`) falls back to the mock adapter; the
    refusal reads the RESOLVED adapter, so the typo can't hand out paid plans."""
    monkeypatch.setattr(settings, "billing_provider", "stripe")
    monkeypatch.setattr(settings, "environment", "production")
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    await _sub(realdb, org_a, free)
    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "growth")
    assert exc.value.code == "billing_not_configured"


@pytest.mark.asyncio
async def test_local_mock_still_changes_plans(realdb, org_a, monkeypatch, _audit_engine_on_loop):
    """Guard rail 7: with `mock` in local dev a paid plan is one click away."""
    monkeypatch.setattr(settings, "billing_provider", "mock")
    monkeypatch.setattr(settings, "environment", "development")
    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    result = await _change(realdb, org_a, "growth")
    assert result.changed is True
    row = await _row(realdb, sub_id)
    assert row.external_subscription_id is None  # nothing live was created
    assert LEASE_KEY not in await _org_billing(realdb, org_a)  # no lease for mock


# ---------------------------------------------------------------------------
# Real-DB: the webhook keeping the row in sync.
# ---------------------------------------------------------------------------


async def _apply(realdb, event: BillingWebhookEvent):
    async with realdb.control_sessionmaker()() as s:
        return await apply_billing_event(s, event=event)


@pytest.mark.asyncio
async def test_invoice_events_move_status_only_along_their_edges(
    realdb, org_a, _audit_engine_on_loop
):
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_wh", status="active")

    def ev(n, kind):
        return BillingWebhookEvent(
            event_id=f"evt_{n}", event_type=kind, external_subscription_id="sub_wh"
        )

    assert (await _apply(realdb, ev(1, "invoice.paid"))).applied is False  # active stays active
    assert (await _apply(realdb, ev(2, "invoice.payment_failed"))).new_status == "past_due"
    assert (await _row(realdb, sub_id)).status == "past_due"
    assert (await _apply(realdb, ev(3, "invoice.paid"))).new_status == "active"
    assert (await _row(realdb, sub_id)).status == "active"

    async with realdb.control_sessionmaker()() as s:
        row = (await s.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()
        row.status = "trialing"
        await s.commit()
    # A paid ($0 trial) invoice never ends a trial early; a failure doesn't touch it.
    assert (await _apply(realdb, ev(4, "invoice.paid"))).applied is False
    assert (await _apply(realdb, ev(5, "invoice.payment_failed"))).applied is False
    assert (await _row(realdb, sub_id)).status == "trialing"


@pytest.mark.asyncio
async def test_subscription_events_sync_the_window_and_canceled_is_terminal(
    realdb, org_a, _audit_engine_on_loop
):
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_win", status="active")
    july, aug = datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)

    renewal = BillingWebhookEvent(
        event_id="evt_r",
        event_type="customer.subscription.updated",
        external_subscription_id="sub_win",
        status="active",
        current_period_start=july,
        current_period_end=aug,
    )
    result = await _apply(realdb, renewal)
    assert result.applied is True and result.reason == "period_synced"
    row = await _row(realdb, sub_id)
    assert (row.current_period_start, row.current_period_end) == (july, aug)

    # A late event from the PREVIOUS period (Stripe delivers out of order)
    # never moves the window backwards.
    stale = BillingWebhookEvent(
        event_id="evt_stale",
        event_type="customer.subscription.updated",
        external_subscription_id="sub_win",
        status="active",
        current_period_start=datetime(2026, 6, 1, tzinfo=UTC),
        current_period_end=july,
    )
    assert (await _apply(realdb, stale)).applied is False
    row = await _row(realdb, sub_id)
    assert (row.current_period_start, row.current_period_end) == (july, aug)

    deleted = BillingWebhookEvent(
        event_id="evt_d",
        event_type="customer.subscription.deleted",
        external_subscription_id="sub_win",
        status="canceled",
    )
    assert (await _apply(realdb, deleted)).new_status == "canceled"
    late = BillingWebhookEvent(
        event_id="evt_late",
        event_type="customer.subscription.updated",
        external_subscription_id="sub_win",
        status="active",
    )
    assert (await _apply(realdb, late)).reason == "subscription_canceled"
    assert (await _row(realdb, sub_id)).status == "canceled"


# ---------------------------------------------------------------------------
# Real-DB: adopting a confirmed card.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_card_endpoint_adopts_the_mock_intent_and_refuses_others(
    realdb, org_a, monkeypatch, _audit_engine_on_loop
):
    monkeypatch.setattr(settings, "billing_provider", "mock")
    await _set_billing(realdb, org_a, stripe_customer_id="mock_cus_a")
    async with realdb.client(key="a", role="admin") as c:
        ok = await c.post(
            "/api/billing/payment-method/default", json={"setup_intent_id": "mock_seti_mock_cus_a"}
        )
        wrong = await c.post(
            "/api/billing/payment-method/default",
            json={"setup_intent_id": "mock_seti_someone_else"},
        )
    assert ok.status_code == 200, ok.text
    assert ok.json()["payment_methods"][0]["is_default"] is True
    assert wrong.status_code == 404
    assert wrong.json()["detail"]["code"] == "setup_intent_not_found"
    async with realdb.sessionmaker("a")() as s:
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(
                        AuditLog.action == "billing.payment_method_updated"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert actions  # the adoption is on the trail; the refusal is not a change


@pytest.mark.asyncio
async def test_default_card_endpoint_is_admin_or_cfo_only(realdb, org_a):
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/billing/payment-method/default", json={"setup_intent_id": "seti_1"}
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Money-path review: the edges where Stripe and our row can part ways.
# ---------------------------------------------------------------------------


def _live_sub(fake: FakeStripe, sid: str, customer: str = "cus_org", status: str = "active"):
    fake.subscriptions[sid] = {
        "id": sid,
        "status": status,
        "customer": customer,
        "metadata": {},
        "items": [{"id": f"si_{sid}", "price": "price_x"}],
        "period": (
            int(datetime(2026, 6, 1, tzinfo=UTC).timestamp()),
            int(datetime(2026, 7, 1, tzinfo=UTC).timestamp()),
        ),
    }


@pytest.mark.asyncio
async def test_leaving_a_paid_plan_cancels_a_subscription_the_row_lost(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    """A row on a paid plan with no provider id (a create that died before
    recording, or a detach whose cancel failed) still has a Stripe subscription
    charging it; moving to Free must find it by customer and cancel it."""
    await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth)  # no external id
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    _live_sub(fake_stripe, "sub_lost")

    await _change(realdb, org_a, "free")

    assert fake_stripe.subscriptions["sub_lost"]["status"] == "canceled"
    (details,) = await _audit_details(realdb, sub_id)
    assert details["provider_action"] == "canceled"


@pytest.mark.asyncio
async def test_a_failed_cancel_leaves_the_row_paid_and_detached_and_the_retry_heals_it(
    realdb, fake_stripe, org_a, monkeypatch, _audit_engine_on_loop
):
    """The provider id is detached BEFORE the cancel (so the cancel's own
    `deleted` webhook can never land on the row). If the cancel then fails, the
    row stays on its paid plan — still what Stripe bills — and the retry finds
    the subscription by customer."""
    free = await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_keep")
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    _live_sub(fake_stripe, "sub_keep")

    real_cancel = StripeBillingAdapter.cancel_subscription

    async def failing_cancel(self, external_subscription_id):
        from app.services.billing_adapters.base import BillingProviderError

        raise BillingProviderError("Stripe cancel_subscription failed: HTTP 500")

    monkeypatch.setattr(StripeBillingAdapter, "cancel_subscription", failing_cancel)
    with pytest.raises(Exception):  # noqa: B017 — the provider error propagates as-is
        await _change(realdb, org_a, "free")
    row = await _row(realdb, sub_id)
    assert row.plan_id == growth and row.external_subscription_id is None
    assert fake_stripe.subscriptions["sub_keep"]["status"] == "active"
    assert LEASE_KEY not in await _org_billing(realdb, org_a)

    monkeypatch.setattr(StripeBillingAdapter, "cancel_subscription", real_cancel)
    await _change(realdb, org_a, "free", at=CHANGE_AT + timedelta(minutes=1))
    assert fake_stripe.subscriptions["sub_keep"]["status"] == "canceled"
    assert (await _row(realdb, sub_id)).plan_id == free


@pytest.mark.asyncio
async def test_a_mock_override_cannot_repoint_a_row_stripe_still_bills(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    await _plan(realdb, "free", "0.00", overage=False)
    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_billed")
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org", provider="mock")
    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "free")
    assert exc.value.code == "billing_not_configured"
    assert (await _row(realdb, sub_id)).plan_id == growth


@pytest.mark.asyncio
async def test_a_change_that_outlived_its_lease_is_not_recorded(
    realdb, fake_stripe, org_a, monkeypatch, _audit_engine_on_loop
):
    """If a slow change's lease runs out and another change takes it over, the
    slow one must not record over the change that now owns the org."""
    import app.services.billing.plan_change as plan_change_module

    free = await _plan(realdb, "free", "0.00", overage=False)
    await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, free)
    await _set_billing(realdb, org_a, stripe_customer_id="cus_org")
    fake_stripe.add_card("cus_org")
    real_leg = plan_change_module.apply_plan_at_provider

    async def leg_then_taken_over(*args, **kwargs):
        change = await real_leg(*args, **kwargs)
        other = {"token": "someone-else", "expires_at": "2099-01-01T00:00:00+00:00"}
        await _set_billing(realdb, org_a, plan_change_lease=other)
        return change

    monkeypatch.setattr(plan_change_module, "apply_plan_at_provider", leg_then_taken_over)
    with pytest.raises(ProviderLegRefused) as exc:
        await _change(realdb, org_a, "growth")
    assert exc.value.code == "plan_change_in_progress"
    assert (await _row(realdb, sub_id)).plan_id == free
    # The lease that now owns the org is not ours to drop.
    assert (await _org_billing(realdb, org_a))[LEASE_KEY]["token"] == "someone-else"


@pytest.mark.asyncio
async def test_the_cancels_own_deleted_webhook_cannot_cancel_the_free_row(
    realdb, org_a, _audit_engine_on_loop
):
    """Two connections: a plan change holds the row and detaches the provider id;
    the cancel's `customer.subscription.deleted` arrives meanwhile. It waits on
    the row lock and, after the change commits, no longer matches."""
    import asyncio

    from sqlalchemy import text

    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_echo")
    holder = realdb.control_sessionmaker()()
    try:
        row = (
            await holder.execute(
                select(Subscription).where(Subscription.id == sub_id).with_for_update()
            )
        ).scalar_one()
        row.external_subscription_id = None
        await holder.flush()

        deleted = BillingWebhookEvent(
            event_id="evt_echo",
            event_type="customer.subscription.deleted",
            external_subscription_id="sub_echo",
            status="canceled",
        )
        webhook = asyncio.create_task(_apply(realdb, deleted))

        async def _until_blocked():
            async with realdb.control_sessionmaker()() as probe:
                while True:
                    waiting = (
                        await probe.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                            )
                        )
                    ).scalar_one()
                    if waiting:
                        return
                    await asyncio.sleep(0.02)

        # The real signal: the webhook's SELECT … FOR UPDATE is waiting on us.
        await asyncio.wait_for(_until_blocked(), timeout=10)
        await holder.commit()
        result = await asyncio.wait_for(webhook, timeout=10)
    finally:
        await holder.close()

    assert result.applied is False and result.reason == "unknown_subscription"
    row = await _row(realdb, sub_id)
    assert row.status == "active"


@pytest.mark.asyncio
async def test_dunning_leaves_a_live_row_to_the_provider_until_it_cancels(
    realdb, fake_stripe, org_a, _audit_engine_on_loop
):
    from app.services.billing.dunning_sweep import run_dunning_once

    growth = await _plan(realdb, "growth", "49.00")
    sub_id = await _sub(realdb, org_a, growth, external_id="sub_dun", status="past_due")
    _live_sub(fake_stripe, "sub_dun", status="past_due")
    far_future = datetime(2027, 1, 1, tzinfo=UTC)  # well past the grace window

    async with realdb.control_sessionmaker()() as s:
        result = await run_dunning_once(s, now=far_future)
    # Stripe is still retrying: canceling our row would drop the features while
    # Stripe keeps charging for them.
    assert result.canceled == 0
    assert (await _row(realdb, sub_id)).status == "past_due"

    fake_stripe.subscriptions["sub_dun"]["status"] = "canceled"  # webhook lost
    async with realdb.control_sessionmaker()() as s:
        result = await run_dunning_once(s, now=far_future)
    assert result.canceled == 1
    assert (await _row(realdb, sub_id)).status == "canceled"
