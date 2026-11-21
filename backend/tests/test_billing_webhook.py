"""Inbound billing webhook route + live-Stripe adapter calls + dunning sweep.

The webhook is the highest-value piece (project invariant #9): it verifies the
Stripe-Signature HMAC, dedupes by event id, drives the idempotent Subscription
lifecycle transition, and 204s silently on every rejection path.

Three groups:
  * pure adapter unit tests for the fleshed-out Stripe create/get/report calls
    (a mocked httpx transport — no network);
  * the dunning sweep (control-plane, idempotent, never moves money);
  * the webhook route end-to-end on the real-Postgres harness.

Control-plane Plan/Subscription rows are NOT truncated between tests, so every
real-DB test cleans up in a ``finally`` keyed to its org. Auth-gating of the
route itself is covered by ``test_rbac.py`` (it's in ``ALTERNATE_AUTH``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.pool import NullPool as _NullPool

from app.config import settings
from app.models.billing import Plan, Subscription
from app.models.workflow import AuditLog
from app.services.billing.dunning_sweep import run_dunning_once
from app.services.billing.webhook_processing import apply_billing_event
from app.services.billing_adapters.base import (
    BillingWebhookEvent,
    CreateSubscriptionRequest,
    UsageReport,
)
from app.services.billing_adapters.stripe_billing import (
    BillingNotConfigured,
    BillingProviderError,
    StripeBillingAdapter,
)

_WEBHOOK_SECRET = "whsec_test_billing"


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET, *, timestamp: str | None = None) -> str:
    """Produce a real ``Stripe-Signature`` header: ``t=<ts>,v1=<hmac(t.body)>``.

    Stripe signs the timestamp-prefixed payload ``f"{t}.{body}"`` — NOT the body
    alone — so the test must build the header exactly as Stripe does or it would
    only ever exercise the reject path.

    ``t`` defaults to NOW, as a real delivery's does: the adapter enforces
    Stripe's replay-tolerance window, so a fixed epoch would fail every test
    for the right reason and hide the wrong one.
    """
    if timestamp is None:
        timestamp = str(int(time.time()))
    signed_payload = b"%s.%s" % (timestamp.encode(), body)
    digest = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _stripe_event(
    *, event_id: str, sub_id: str, raw_status: str, event_type="customer.subscription.updated"
) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "data": {"object": {"id": sub_id, "status": raw_status}},
        }
    ).encode()


# ---------------------------------------------------------------------------
# Stripe adapter — fleshed-out live calls (mocked transport, no network).
# ---------------------------------------------------------------------------


def _adapter_with_transport(handler, *, customer="cus_1", price="price_1", key="sk_live"):
    """Build a StripeBillingAdapter whose _client serves canned httpx responses."""
    adapter = StripeBillingAdapter(
        {
            "stripe_api_key": key,
            "stripe_webhook_secret": _WEBHOOK_SECRET,
            "stripe_customer_id": customer,
            "stripe_price_id": price,
        }
    )
    transport = httpx.MockTransport(handler)

    def _client():
        return httpx.AsyncClient(
            base_url="https://api.stripe.test",
            auth=(adapter._api_key, ""),
            transport=transport,
        )

    adapter._client = _client  # type: ignore[method-assign]
    return adapter


@pytest.mark.asyncio
async def test_stripe_create_subscription_maps_status():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = request.content.decode()
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"id": "sub_new", "status": "trialing"})

    adapter = _adapter_with_transport(handler)
    req = CreateSubscriptionRequest(
        organization_id="org-1",
        plan_code="growth",
        monthly_price=Decimal("49.00"),
        trial_days=14,
        idempotency_key="idem-abc",
    )
    result = await adapter.create_subscription(req)
    assert result.external_subscription_id == "sub_new"
    assert result.status == "trialing"  # mapped
    assert result.plan_code == "growth"
    assert captured["path"] == "/v1/subscriptions"
    assert "trial_period_days=14" in captured["body"]
    assert "customer=cus_1" in captured["body"]
    assert captured["idem"] == "idem-abc"  # retried create is safe


@pytest.mark.asyncio
async def test_stripe_create_subscription_requires_customer_and_price():
    adapter = StripeBillingAdapter({"stripe_api_key": "sk", "stripe_webhook_secret": ""})
    req = CreateSubscriptionRequest(
        organization_id="o", plan_code="growth", monthly_price=Decimal("49.00")
    )
    with pytest.raises(BillingNotConfigured):
        await adapter.create_subscription(req)


@pytest.mark.asyncio
async def test_stripe_get_subscription_maps_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/subscriptions/sub_42"
        return httpx.Response(200, json={"id": "sub_42", "status": "past_due"})

    adapter = _adapter_with_transport(handler)
    result = await adapter.get_subscription("sub_42")
    assert result.external_subscription_id == "sub_42"
    assert result.status == "past_due"


@pytest.mark.asyncio
async def test_stripe_report_usage_posts_one_event_per_meter():
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.content.decode())
        return httpx.Response(200, json={"object": "billing.meter_event"})

    adapter = _adapter_with_transport(handler)
    report = UsageReport(
        organization_id="org-9",
        period="2026-06",
        meters={"extractions": "12", "card_rebate_total": "3.50"},
    )
    await adapter.report_usage(report)
    assert len(posts) == 2
    joined = "\n".join(posts)
    # Exact decimal strings, never float (form keys are URL-encoded: [ → %5B).
    assert "payload%5Bvalue%5D=12" in joined
    assert "payload%5Bvalue%5D=3.50" in joined
    assert "event_name=extractions" in joined


@pytest.mark.asyncio
async def test_stripe_report_usage_empty_meters_is_noop():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not POST for empty meters")

    adapter = _adapter_with_transport(handler)
    await adapter.report_usage(UsageReport(organization_id="o", period="2026-06", meters={}))


@pytest.mark.asyncio
async def test_stripe_provider_error_is_pii_free():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "card declined for cus_secret"}})

    adapter = _adapter_with_transport(handler)
    with pytest.raises(BillingProviderError) as exc:
        await adapter.get_subscription("sub_x")
    # The error message names the op + status only — never the response body.
    assert "402" in str(exc.value)
    assert "cus_secret" not in str(exc.value)


# ---------------------------------------------------------------------------
# Stripe-Signature replay window
# ---------------------------------------------------------------------------


def _signature_adapter() -> StripeBillingAdapter:
    return StripeBillingAdapter({"stripe_webhook_secret": _WEBHOOK_SECRET})


def test_stripe_signature_accepts_a_fresh_timestamp():
    body = _stripe_event(event_id="evt_fresh", sub_id="sub_1", raw_status="active")
    assert _signature_adapter()._verify_stripe_signature(_sign(body), body) is True


def test_stripe_signature_rejects_an_old_but_correctly_signed_event():
    """Stripe's verification procedure has two halves and only the digest half
    was implemented, so a captured event verified forever. The Redis dedupe
    covers a REDELIVERY of the same event inside its 72h TTL; it does not stop
    an OLD event being replayed at all — a `customer.subscription.deleted`
    replayed later cancels a subscription the customer has since re-taken."""
    body = _stripe_event(event_id="evt_old", sub_id="sub_1", raw_status="canceled")
    stale = str(int(time.time()) - 365 * 24 * 3600)
    header = _sign(body, timestamp=stale)
    assert _signature_adapter()._verify_stripe_signature(header, body) is False


def test_stripe_signature_rejects_a_far_future_timestamp():
    """A forged far-future `t` would otherwise buy an arbitrarily long window."""
    body = _stripe_event(event_id="evt_future", sub_id="sub_1", raw_status="active")
    ahead = str(int(time.time()) + 24 * 3600)
    header = _sign(body, timestamp=ahead)
    assert _signature_adapter()._verify_stripe_signature(header, body) is False


def test_stripe_signature_rejects_a_non_numeric_timestamp():
    body = _stripe_event(event_id="evt_bad_t", sub_id="sub_1", raw_status="active")
    assert _signature_adapter()._verify_stripe_signature(_sign(body, timestamp="soon"), body) is (
        False
    )


def test_stripe_signature_window_can_be_disabled_for_an_archived_replay(monkeypatch):
    """`<= 0` is the documented escape hatch for an operator replaying an
    archived event during an incident — a knob, never the default."""
    monkeypatch.setattr(settings, "billing_stripe_webhook_max_age_seconds", 0)
    body = _stripe_event(event_id="evt_archived", sub_id="sub_1", raw_status="active")
    stale = str(int(time.time()) - 365 * 24 * 3600)
    assert _signature_adapter()._verify_stripe_signature(_sign(body, timestamp=stale), body) is True


# ---------------------------------------------------------------------------
# Real-DB: webhook route end-to-end + dunning sweep.
# ---------------------------------------------------------------------------


async def _cleanup(realdb, org_id):
    # NB: audit_log is append-only (DB immutability trigger) — never delete it.
    # Tests scope their audit assertions to the subscription id they created, so
    # rows accumulating across tests is fine.
    await realdb.purge_plans("whtest_", org_ids=[org_id])


async def _seed_sub(realdb, *, org_id, status, external_id, period_end=None):
    plan_id = uuid.uuid4()
    sub_id = uuid.uuid4()
    async with realdb.control_sessionmaker()() as s:
        s.add(
            Plan(
                id=plan_id,
                code=f"whtest_{external_id}",
                name="WHTest",
                monthly_price=Decimal("49.00"),
                currency="USD",
            )
        )
        s.add(
            Subscription(
                id=sub_id,
                organization_id=org_id,
                plan_id=plan_id,
                status=status,
                external_subscription_id=external_id,
                current_period_end=period_end,
            )
        )
        await s.commit()
    return sub_id


def _webhook_client(realdb):
    c = realdb.client(key="a", role=None)
    c.headers.pop("X-Tenant-Slug", None)
    return c


async def _audit_actions(realdb, sub_id):
    """Billing audit actions for a specific subscription (entity_id == sub_id).

    Scoped to the subscription so rows from sibling tests (audit_log is
    append-only and never truncated) don't leak into the assertion.
    """
    async with realdb.sessionmaker("a")() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog.action).where(
                        AuditLog.action.like("billing.subscription_%"),
                        AuditLog.entity_id == sub_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


@pytest.fixture
def _audit_engine_on_loop(monkeypatch, realdb):
    """Write ``dispatch_auth_audit``'s tenant audit row on THIS test's loop.

    The billing audit row goes through ``dispatch_auth_audit``, which resolves
    the tenant DB via the global ``control_session_factory`` and then opens it
    via the global ``get_tenant_engine`` pool — both singletons bound to the
    first loop that touched them, so a later realdb test on a new loop hits
    "connection closed / different loop". The payment-webhook suite sidesteps
    this by mocking the engine away (it doesn't assert the audit row); here we DO
    assert it, so instead we (1) patch the resolver to return the tenant db_name
    straight from the per-test mapping (no global control engine) and (2) hand
    the tenant write a per-call NullPool engine on the live loop. Both patches
    are auto-restored by monkeypatch; NullPool engines hold no pooled
    connections, so nothing leaks into the next test's teardown.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.database import _make_tenant_url

    db_name_by_org = {info.org_id: info.db_name for info in realdb.tenants.values()}

    def _engine(db_name: str):
        return create_async_engine(_make_tenant_url(db_name), poolclass=_NullPool)

    async def _resolve(organization_id):
        return db_name_by_org[organization_id]

    monkeypatch.setattr("app.services.audit_dispatch._resolve_tenant_db_name", _resolve)
    monkeypatch.setattr("app.database.get_tenant_engine", _engine)


@pytest.fixture
def _enable_stripe_webhook(monkeypatch):
    monkeypatch.setattr(settings, "billing_webhook_enabled", True)
    monkeypatch.setattr(settings, "billing_provider", "stripe_billing")
    monkeypatch.setattr(settings, "billing_stripe_webhook_secret", _WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "billing_stripe_api_key", "sk_test")


@pytest.mark.asyncio
async def test_webhook_signed_event_drives_transition(
    realdb, _enable_stripe_webhook, _audit_engine_on_loop
):
    org_id = realdb.info("a").org_id
    try:
        sub_id = await _seed_sub(realdb, org_id=org_id, status="active", external_id="sub_live_1")
        body = _stripe_event(
            event_id="evt_1",
            sub_id="sub_live_1",
            raw_status="past_due",
            event_type="customer.subscription.updated",
        )
        async with _webhook_client(realdb) as c:
            resp = await c.post(
                "/api/billing/webhook/stripe_billing",
                content=body,
                headers={"Stripe-Signature": _sign(body)},
            )
        assert resp.status_code == 204
        # Status transitioned + audit row written.
        async with realdb.control_sessionmaker()() as s:
            sub = (
                await s.execute(
                    select(Subscription).where(
                        Subscription.external_subscription_id == "sub_live_1"
                    )
                )
            ).scalar_one()
        assert sub.status == "past_due"
        assert "billing.subscription_past_due" in await _audit_actions(realdb, sub_id)
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_webhook_bad_signature_204_no_change(realdb, _enable_stripe_webhook):
    org_id = realdb.info("a").org_id
    try:
        sub_id = await _seed_sub(realdb, org_id=org_id, status="active", external_id="sub_live_2")
        body = _stripe_event(event_id="evt_2", sub_id="sub_live_2", raw_status="canceled")
        async with _webhook_client(realdb) as c:
            resp = await c.post(
                "/api/billing/webhook/stripe_billing",
                content=body,
                headers={"Stripe-Signature": "deadbeef"},  # wrong HMAC
            )
        assert resp.status_code == 204  # silent
        async with realdb.control_sessionmaker()() as s:
            sub = (
                await s.execute(
                    select(Subscription).where(
                        Subscription.external_subscription_id == "sub_live_2"
                    )
                )
            ).scalar_one()
        assert sub.status == "active"  # unchanged
        assert await _audit_actions(realdb, sub_id) == []
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_webhook_dedupes_by_event_id(realdb, _enable_stripe_webhook, _audit_engine_on_loop):
    org_id = realdb.info("a").org_id
    try:
        sub_id = await _seed_sub(realdb, org_id=org_id, status="active", external_id="sub_live_3")
        body = _stripe_event(event_id="evt_dup", sub_id="sub_live_3", raw_status="past_due")
        headers = {"Stripe-Signature": _sign(body)}
        async with _webhook_client(realdb) as c:
            r1 = await c.post("/api/billing/webhook/stripe_billing", content=body, headers=headers)
            r2 = await c.post("/api/billing/webhook/stripe_billing", content=body, headers=headers)
        assert r1.status_code == r2.status_code == 204
        # Exactly one audit row despite two deliveries (the second deduped).
        assert (await _audit_actions(realdb, sub_id)).count("billing.subscription_past_due") == 1
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_webhook_idempotent_same_status_no_audit(realdb, _enable_stripe_webhook):
    org_id = realdb.info("a").org_id
    try:
        sub_id = await _seed_sub(realdb, org_id=org_id, status="past_due", external_id="sub_live_4")
        # A distinct event id (not deduped) but the same target status → no change.
        body = _stripe_event(event_id="evt_noop", sub_id="sub_live_4", raw_status="past_due")
        async with _webhook_client(realdb) as c:
            resp = await c.post(
                "/api/billing/webhook/stripe_billing",
                content=body,
                headers={"Stripe-Signature": _sign(body)},
            )
        assert resp.status_code == 204
        assert await _audit_actions(realdb, sub_id) == []  # no-op writes no audit row
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_webhook_unknown_subscription_204(realdb, _enable_stripe_webhook):
    org_id = realdb.info("a").org_id
    try:
        body = _stripe_event(event_id="evt_unk", sub_id="sub_does_not_exist", raw_status="active")
        async with _webhook_client(realdb) as c:
            resp = await c.post(
                "/api/billing/webhook/stripe_billing",
                content=body,
                headers={"Stripe-Signature": _sign(body)},
            )
        # Unknown subscription → silent 204, nothing created (no row to inspect).
        assert resp.status_code == 204
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_webhook_disabled_switch_204(realdb, monkeypatch):
    monkeypatch.setattr(settings, "billing_webhook_enabled", False)
    monkeypatch.setattr(settings, "billing_provider", "stripe_billing")
    monkeypatch.setattr(settings, "billing_stripe_webhook_secret", _WEBHOOK_SECRET)
    org_id = realdb.info("a").org_id
    try:
        await _seed_sub(realdb, org_id=org_id, status="active", external_id="sub_live_5")
        body = _stripe_event(event_id="evt_off", sub_id="sub_live_5", raw_status="canceled")
        async with _webhook_client(realdb) as c:
            resp = await c.post(
                "/api/billing/webhook/stripe_billing",
                content=body,
                headers={"Stripe-Signature": _sign(body)},
            )
        assert resp.status_code == 204
        async with realdb.control_sessionmaker()() as s:
            sub = (
                await s.execute(
                    select(Subscription).where(
                        Subscription.external_subscription_id == "sub_live_5"
                    )
                )
            ).scalar_one()
        assert sub.status == "active"  # disabled → no effect
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_webhook_provider_mismatch_204(realdb, _enable_stripe_webhook):
    org_id = realdb.info("a").org_id
    try:
        sub_id = await _seed_sub(realdb, org_id=org_id, status="active", external_id="sub_live_6")
        body = _stripe_event(event_id="evt_pm", sub_id="sub_live_6", raw_status="canceled")
        async with _webhook_client(realdb) as c:
            # configured provider is stripe_billing; POST to a different name.
            resp = await c.post(
                "/api/billing/webhook/some_other_provider",
                content=body,
                headers={"Stripe-Signature": _sign(body)},
            )
        assert resp.status_code == 204
        assert await _audit_actions(realdb, sub_id) == []
    finally:
        await _cleanup(realdb, org_id)


# ---------------------------------------------------------------------------
# Dunning sweep — control-plane, idempotent, money-free.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dunning_cancels_overdue_past_due(realdb, monkeypatch, _audit_engine_on_loop):
    monkeypatch.setattr(settings, "billing_dunning_grace_days", 14)
    org_id = realdb.info("a").org_id
    now = datetime.now(UTC)
    try:
        # Overdue: period ended 30 days ago, still past_due → cancel.
        sub_id = await _seed_sub(
            realdb,
            org_id=org_id,
            status="past_due",
            external_id="sub_overdue",
            period_end=now - timedelta(days=30),
        )
        async with realdb.control_sessionmaker()() as s:
            result = await run_dunning_once(s, now=now)
        assert result.canceled == 1
        assert result.failures == 0
        async with realdb.control_sessionmaker()() as s:
            sub = (
                await s.execute(
                    select(Subscription).where(
                        Subscription.external_subscription_id == "sub_overdue"
                    )
                )
            ).scalar_one()
        assert sub.status == "canceled"
        assert "billing.subscription_canceled" in await _audit_actions(realdb, sub_id)

        # Idempotent: re-running cancels nothing more.
        async with realdb.control_sessionmaker()() as s:
            assert (await run_dunning_once(s, now=now)).canceled == 0
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_dunning_spares_within_grace(realdb, monkeypatch):
    monkeypatch.setattr(settings, "billing_dunning_grace_days", 14)
    org_id = realdb.info("a").org_id
    now = datetime.now(UTC)
    try:
        # Within grace: period ended only 3 days ago → spared.
        await _seed_sub(
            realdb,
            org_id=org_id,
            status="past_due",
            external_id="sub_grace",
            period_end=now - timedelta(days=3),
        )
        async with realdb.control_sessionmaker()() as s:
            result = await run_dunning_once(s, now=now)
        assert result.canceled == 0
        async with realdb.control_sessionmaker()() as s:
            sub = (
                await s.execute(
                    select(Subscription).where(Subscription.external_subscription_id == "sub_grace")
                )
            ).scalar_one()
        assert sub.status == "past_due"  # still in grace
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_apply_billing_event_idempotent_direct(realdb, _audit_engine_on_loop):
    """Direct service test: applying the same target status twice is a no-op."""
    org_id = realdb.info("a").org_id
    try:
        await _seed_sub(realdb, org_id=org_id, status="active", external_id="sub_direct")
        evt = BillingWebhookEvent(
            event_id="e1",
            event_type="t",
            external_subscription_id="sub_direct",
            status="past_due",
        )
        async with realdb.control_sessionmaker()() as s:
            r1 = await apply_billing_event(s, event=evt)
        assert r1.applied is True
        async with realdb.control_sessionmaker()() as s:
            r2 = await apply_billing_event(s, event=evt)
        assert r2.applied is False
        assert r2.reason == "already_in_status"
    finally:
        await _cleanup(realdb, org_id)


# ---------------------------------------------------------------------------
# Boot guard
# ---------------------------------------------------------------------------
#
# The mock billing adapter's `parse_webhook` performs zero signature
# verification (it's a local-only dev double). Serving the mock adapter on
# the public webhook route would accept unauthenticated POSTs that flip a
# Subscription's status — e.g. a caller deriving `mock_sub_<org_id>` from
# their own JWT `org` claim. Mirrors the email-intake / PEPPOL-inbound
# boot-time guards in `app/main.py::lifespan`.


@pytest.mark.asyncio
async def test_boot_refuses_mock_provider_with_webhook_enabled(monkeypatch):
    from app.main import lifespan

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "a-real-non-default-secret-value")
    monkeypatch.setattr(settings, "billing_webhook_enabled", True)
    monkeypatch.setattr(settings, "billing_provider", "mock")

    with pytest.raises(RuntimeError, match="FEOH_BILLING_PROVIDER"):
        async with lifespan(object()):  # pragma: no cover - never enters body
            pass


@pytest.mark.asyncio
async def test_boot_refuses_unregistered_provider_with_webhook_enabled(monkeypatch):
    """The typo case the `== "mock"` check could never see.

    `get_billing_adapter` falls back to `mock` for ANY unregistered name, and
    `MockBillingAdapter.parse_webhook` verifies no signature. The registered
    Stripe adapter is named `stripe_billing`; `FEOH_BILLING_PROVIDER=stripe`
    is one plausible keystroke away, is not `"mock"`, and matches the route's
    own `provider != settings.billing_provider` guard — so it booted clean and
    served `POST /api/billing/webhook/stripe` as an unauthenticated
    subscription-lifecycle mutator. Same allowlist shape the audit-shipping
    guard already uses (`docs/decisions.md` §26, §29).
    """
    from app.main import lifespan

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "a-real-non-default-secret-value")
    monkeypatch.setattr(settings, "billing_webhook_enabled", True)
    monkeypatch.setattr(settings, "billing_provider", "stripe")  # registered: stripe_billing
    monkeypatch.setattr(settings, "extraction_reaper_enabled", False)

    with pytest.raises(RuntimeError, match="no registered adapter"):
        async with lifespan(object()):  # pragma: no cover - never enters body
            pass


@pytest.mark.asyncio
async def test_unregistered_provider_is_refused_at_the_route_too(realdb, monkeypatch):
    """Second line of defence: even if a process somehow serves an unregistered
    name, the route refuses once the resolved adapter disagrees with it — so the
    signature-free mock parser is never reached with a real request body."""
    monkeypatch.setattr(settings, "billing_webhook_enabled", True)
    monkeypatch.setattr(settings, "billing_provider", "stripe")  # registered: stripe_billing

    org_id = realdb.info("a").org_id
    try:
        sub_id = await _seed_sub(
            realdb, org_id=org_id, status="active", external_id="sub_forge_target"
        )
        # An UNSIGNED body in the mock adapter's dev-envelope shape: exactly
        # what the fallback would have happily parsed and applied.
        body = json.dumps(
            {
                "id": "evt_forged",
                "type": "customer.subscription.updated",
                "subscription": "sub_forge_target",
                "status": "canceled",
            }
        ).encode()
        async with _webhook_client(realdb) as c:
            resp = await c.post("/api/billing/webhook/stripe", content=body)
        assert resp.status_code == 204  # opaque, like every other rejection

        async with realdb.control_sessionmaker()() as s:
            sub = (
                await s.execute(select(Subscription).where(Subscription.id == sub_id))
            ).scalar_one()
            assert sub.status == "active", (
                "an unsigned body mutated the subscription — the unregistered "
                "provider name resolved to the signature-free mock adapter"
            )
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_boot_allows_mock_provider_with_webhook_disabled(monkeypatch):
    """The documented local-first default: mock provider + the webhook route OFF
    (both defaults) must never trip the guard — a fresh `pnpm dev` clone boots fine."""
    from app.main import lifespan

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "a-real-non-default-secret-value")
    monkeypatch.setattr(settings, "billing_webhook_enabled", False)
    monkeypatch.setattr(settings, "billing_provider", "mock")
    monkeypatch.setattr(settings, "extraction_reaper_enabled", False)

    async with lifespan(object()):
        pass


@pytest.mark.asyncio
async def test_boot_allows_live_provider_with_webhook_enabled(monkeypatch):
    """A deployed env with a real provider configured is unaffected by the guard."""
    from app.main import lifespan

    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "secret_key", "a-real-non-default-secret-value")
    monkeypatch.setattr(settings, "billing_webhook_enabled", True)
    monkeypatch.setattr(settings, "billing_provider", "stripe_billing")
    monkeypatch.setattr(settings, "extraction_reaper_enabled", False)

    async with lifespan(object()):
        pass
