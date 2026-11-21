"""Plan-change proration + per-org provisioning slice.

Three groups:
  * pure proration math — Decimal-exact, the rounding rule, upgrade / downgrade /
    no-op (no DB);
  * provisioning resolution — the mock adapter returns deterministic ids +
    persists them on `Organization.settings.billing`; the live Stripe adapter
    creates customer/price (mocked httpx, no network) and create_subscription
    succeeds with the resolved ids + still fails closed without a key;
  * plan-change service/endpoint on the real-Postgres harness — applies the
    proration, idempotent no-op, writes the audit row, RBAC 403.

Control-plane Plan/Subscription rows are NOT truncated between tests, so every
real-DB test cleans up in a ``finally`` keyed to its org.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.billing import Plan, Subscription
from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services.billing.plan_change import PlanChangeError, change_plan
from app.services.billing.proration import compute_proration
from app.services.billing.provisioning import provision_org_billing
from app.services.billing_adapters.base import CreateSubscriptionRequest
from app.services.billing_adapters.mock_adapter import MockBillingAdapter
from app.services.billing_adapters.stripe_billing import (
    BillingNotConfigured,
    StripeBillingAdapter,
    _to_minor_units,
)

# ---------------------------------------------------------------------------
# Pure proration math — no DB.
# ---------------------------------------------------------------------------

_P_START = datetime(2026, 6, 1, tzinfo=UTC)
_P_END = datetime(2026, 7, 1, tzinfo=UTC)  # 30-day period


def test_proration_upgrade_midperiod_is_positive_decimal():
    # 49 → 99 with 15 of 30 days remaining: (99-49) * 15/30 = 25.00 extra charge.
    r = compute_proration(
        old_monthly=Decimal("49.00"),
        new_monthly=Decimal("99.00"),
        period_start=_P_START,
        period_end=_P_END,
        change_at=datetime(2026, 6, 16, tzinfo=UTC),
    )
    assert r.amount == Decimal("25.00")
    assert isinstance(r.amount, Decimal)
    assert r.unused_days == 15
    assert r.period_days == 30


def test_proration_downgrade_midperiod_is_negative_credit():
    # 99 → 49 with 15 of 30 days remaining: (49-99) * 15/30 = -25.00 credit.
    r = compute_proration(
        old_monthly=Decimal("99.00"),
        new_monthly=Decimal("49.00"),
        period_start=_P_START,
        period_end=_P_END,
        change_at=datetime(2026, 6, 16, tzinfo=UTC),
    )
    assert r.amount == Decimal("-25.00")


def test_proration_same_price_is_zero_no_division():
    r = compute_proration(
        old_monthly=Decimal("49.00"),
        new_monthly=Decimal("49.00"),
        period_start=_P_START,
        period_end=_P_END,
        change_at=datetime(2026, 6, 16, tzinfo=UTC),
    )
    assert r.amount == Decimal("0.00")


def test_proration_rounding_is_half_up_2dp():
    # Construct a case whose exact product needs rounding. (10.00) * 1/3 day-frac
    # → 3.333..., rounds HALF_UP to 3.33. Use a 3-day period, 1 day remaining.
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 4, tzinfo=UTC)  # 3 days
    r = compute_proration(
        old_monthly=Decimal("0.00"),
        new_monthly=Decimal("10.00"),
        period_start=start,
        period_end=end,
        change_at=datetime(2026, 6, 3, tzinfo=UTC),  # 1 day remaining
    )
    assert r.unused_days == 1
    assert r.period_days == 3
    # 10.00 * 1 / 3 = 3.3333... → 3.33
    assert r.amount == Decimal("3.33")


def test_proration_half_up_rounds_away_from_zero():
    # Force an exact .005 boundary: 0.01 * 1/2 = 0.005 → HALF_UP → 0.01.
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 3, tzinfo=UTC)  # 2 days
    r = compute_proration(
        old_monthly=Decimal("0.00"),
        new_monthly=Decimal("0.01"),
        period_start=start,
        period_end=end,
        change_at=datetime(2026, 6, 2, tzinfo=UTC),  # 1 day remaining
    )
    assert r.amount == Decimal("0.01")


def test_proration_no_days_remaining_is_zero():
    # change_at at/after period end → 0 unused days → 0.00, no division by zero.
    r = compute_proration(
        old_monthly=Decimal("49.00"),
        new_monthly=Decimal("99.00"),
        period_start=_P_START,
        period_end=_P_END,
        change_at=_P_END,
    )
    assert r.amount == Decimal("0.00")
    assert r.unused_days == 0


def test_proration_degenerate_window_is_zero():
    # Inverted / zero-length window → 0.00 (defensive, no crash).
    r = compute_proration(
        old_monthly=Decimal("49.00"),
        new_monthly=Decimal("99.00"),
        period_start=_P_END,
        period_end=_P_START,
        change_at=_P_START,
    )
    assert r.amount == Decimal("0.00")
    assert r.period_days == 0


def test_to_minor_units_is_decimal_exact():
    assert _to_minor_units(Decimal("49.00")) == 4900
    assert _to_minor_units(Decimal("0.00")) == 0
    assert _to_minor_units(Decimal("12.34")) == 1234
    assert isinstance(_to_minor_units(Decimal("1.00")), int)


# ---------------------------------------------------------------------------
# Provisioning — mock adapter (pure) + live Stripe adapter (mocked httpx).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_adapter_provisions_deterministic_ids():
    adapter = MockBillingAdapter()
    cust = await adapter.ensure_customer(organization_id="org-1", name="Acme")
    price = await adapter.ensure_price(plan_code="growth", monthly_price=Decimal("49.00"))
    assert cust == "mock_cus_org-1"
    assert price == "mock_price_growth"


def _stripe_with_transport(handler, *, key="sk_live", customer=None, price=None):
    config = {"stripe_api_key": key, "stripe_webhook_secret": ""}
    if customer:
        config["stripe_customer_id"] = customer
    if price:
        config["stripe_price_id"] = price
    adapter = StripeBillingAdapter(config)
    transport = httpx.MockTransport(handler)

    def _client():
        return httpx.AsyncClient(
            base_url="https://api.stripe.test", auth=(adapter._api_key, ""), transport=transport
        )

    adapter._client = _client  # type: ignore[method-assign]
    return adapter


@pytest.mark.asyncio
async def test_stripe_ensure_customer_creates_and_sends_idempotency_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = request.content.decode()
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"id": "cus_new"})

    adapter = _stripe_with_transport(handler)
    cid = await adapter.ensure_customer(organization_id="org-9", name="Acme", email="a@b.com")
    assert cid == "cus_new"
    assert captured["path"] == "/v1/customers"
    assert "metadata%5Borganization_id%5D=org-9" in captured["body"]
    assert captured["idem"] == "ap-customer-org-9"  # retry-safe


@pytest.mark.asyncio
async def test_stripe_ensure_price_sends_minor_units_recurring():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # The account-wide lookup by key finds nothing yet.
            captured["lookup"] = request.url.params["lookup_keys[]"]
            return httpx.Response(200, json={"data": []})
        captured["body"] = request.content.decode()
        captured["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"id": "price_new"})

    adapter = _stripe_with_transport(handler)
    pid = await adapter.ensure_price(
        plan_code="growth", monthly_price=Decimal("49.00"), currency="USD"
    )
    assert pid == "price_new"
    assert "unit_amount=4900" in captured["body"]  # 49.00 → 4900 cents, exact
    assert "recurring%5Binterval%5D=month" in captured["body"]
    assert captured["idem"] == "ap-price-growth-4900-usd"
    # Shared across orgs: one price per plan + amount, found again by this key.
    assert captured["lookup"] == "feohledger_plan_growth_4900_usd"
    assert "lookup_key=feohledger_plan_growth_4900_usd" in captured["body"]


@pytest.mark.asyncio
async def test_stripe_create_subscription_succeeds_with_resolved_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "customer=cus_resolved" in body
        assert "items%5B0%5D%5Bprice%5D=price_resolved" in body
        return httpx.Response(200, json={"id": "sub_ok", "status": "active"})

    adapter = _stripe_with_transport(handler, customer="cus_resolved", price="price_resolved")
    req = CreateSubscriptionRequest(
        organization_id="o", plan_code="growth", monthly_price=Decimal("49.00")
    )
    result = await adapter.create_subscription(req)
    assert result.external_subscription_id == "sub_ok"
    assert result.status == "active"


@pytest.mark.asyncio
async def test_stripe_provisioning_fails_closed_without_key():
    adapter = StripeBillingAdapter({"stripe_api_key": "", "stripe_webhook_secret": ""})
    with pytest.raises(BillingNotConfigured):
        await adapter.ensure_customer(organization_id="o")
    with pytest.raises(BillingNotConfigured):
        await adapter.ensure_price(plan_code="growth", monthly_price=Decimal("49.00"))


# ---------------------------------------------------------------------------
# Real-DB: provisioning persistence + plan-change service/endpoint.
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


async def _cleanup(realdb, org_id):
    await realdb.purge_plans("prtest_", org_ids=[org_id])
    async with realdb.control_sessionmaker()() as s:
        # Clear any billing block we wrote onto the org settings.
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        if org.settings and "billing" in (org.settings or {}):
            new_settings = dict(org.settings)
            new_settings.pop("billing", None)
            org.settings = new_settings
            flag_modified(org, "settings")
        await s.commit()


async def _seed_plan(realdb, *, code, price="49.00", entitlements=None):
    plan_id = uuid.uuid4()
    async with realdb.control_sessionmaker()() as s:
        s.add(
            Plan(
                id=plan_id,
                code=code,
                name=code.title(),
                monthly_price=Decimal(price),
                currency="USD",
                entitlements=entitlements or {},
                trial_days=0,
            )
        )
        await s.commit()
    return plan_id


async def _seed_sub(realdb, *, org_id, plan_id, status="active"):
    sub_id = uuid.uuid4()
    async with realdb.control_sessionmaker()() as s:
        s.add(
            Subscription(
                id=sub_id,
                organization_id=org_id,
                plan_id=plan_id,
                status=status,
                current_period_start=datetime(2026, 6, 1, tzinfo=UTC),
                current_period_end=datetime(2026, 7, 1, tzinfo=UTC),
            )
        )
        await s.commit()
    return sub_id


async def _seed_sub_without_period(realdb, *, org_id, plan_id, created_at):
    """A subscription carrying NO billing window — the shape EVERY row had
    before `plan_catalog.ensure_subscription` started stamping one, and the
    shape every pre-existing row still has."""
    sub_id = uuid.uuid4()
    async with realdb.control_sessionmaker()() as s:
        s.add(
            Subscription(
                id=sub_id,
                organization_id=org_id,
                plan_id=plan_id,
                status="active",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        await s.commit()
    return sub_id


async def _audit_actions(realdb, sub_id):
    async with realdb.sessionmaker("a")() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog.action).where(
                        AuditLog.action.like("billing.%"),
                        AuditLog.entity_id == sub_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


@pytest.mark.asyncio
async def test_provisioning_resolves_and_persists_mock_ids(realdb):
    org_id = realdb.info("a").org_id
    try:
        plan_id = await _seed_plan(realdb, code="prtest_growth")
        async with realdb.control_sessionmaker()() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            plan = (await s.execute(select(Plan).where(Plan.id == plan_id))).scalar_one()
            ids = await provision_org_billing(s, org=org, plan=plan)
        assert ids.customer_id == f"mock_cus_{org_id}"
        assert ids.price_id == "mock_price_prtest_growth"
        # Persisted onto settings.billing.
        async with realdb.control_sessionmaker()() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
        billing = org.settings["billing"]
        assert billing["stripe_customer_id"] == f"mock_cus_{org_id}"
        assert billing["plan_price_ids"]["prtest_growth"] == "mock_price_prtest_growth"
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_plan_change_applies_proration_and_audits(realdb, _audit_engine_on_loop):
    org_id = realdb.info("a").org_id
    try:
        old_id = await _seed_plan(realdb, code="prtest_basic", price="49.00")
        await _seed_plan(realdb, code="prtest_scale", price="99.00")
        sub_id = await _seed_sub(realdb, org_id=org_id, plan_id=old_id)
        async with realdb.control_sessionmaker()() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            result = await change_plan(
                s,
                org=org,
                new_plan_code="prtest_scale",
                actor_id=None,
                change_at=datetime(2026, 6, 16, tzinfo=UTC),
            )
        assert result.changed is True
        assert result.old_plan_code == "prtest_basic"
        assert result.new_plan_code == "prtest_scale"
        # (99-49) * 15/30 = 25.00 upgrade charge.
        assert result.proration.amount == Decimal("25.00")
        assert "billing.plan_changed" in await _audit_actions(realdb, sub_id)
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_production_created_subscription_carries_a_billing_window(realdb):
    """`ensure_subscription` is the ONLY place a Subscription is built outside
    tests, and it used to leave both period columns NULL — which is what made
    every plan change below prorate 0.00."""
    from app.services.billing.period import add_months
    from app.services.billing.plan_catalog import ensure_subscription

    org_id = realdb.info("a").org_id
    try:
        await _seed_plan(realdb, code="prtest_seeded", price="49.00")
        async with realdb.control_sessionmaker()() as s:
            sub = await ensure_subscription(s, organization_id=org_id, plan_code="prtest_seeded")
            await s.commit()
        assert sub is not None
        assert sub.current_period_start is not None
        assert sub.current_period_end is not None
        assert sub.current_period_end == add_months(sub.current_period_start, 1)
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_plan_change_prorates_a_subscription_with_no_stored_window(
    realdb, _audit_engine_on_loop
):
    """The reproduced bug: a real subscription had no billing window, so
    `change_plan` passed `now`/`now` into `compute_proration`, its
    degenerate-window guard fired, and a $49 → $99 upgrade halfway through the
    month adjusted nothing — while recording "0.00" as the correct figure on an
    immutable audit row.

    The window is now resolved from the subscription's own start (here
    `created_at`, the only anchor a legacy row has) and persisted."""
    org_id = realdb.info("a").org_id
    try:
        old_id = await _seed_plan(realdb, code="prtest_nowin_basic", price="49.00")
        await _seed_plan(realdb, code="prtest_nowin_scale", price="99.00")
        sub_id = await _seed_sub_without_period(
            realdb, org_id=org_id, plan_id=old_id, created_at=datetime(2026, 6, 1, tzinfo=UTC)
        )
        async with realdb.control_sessionmaker()() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            result = await change_plan(
                s,
                org=org,
                new_plan_code="prtest_nowin_scale",
                actor_id=None,
                change_at=datetime(2026, 6, 16, tzinfo=UTC),
            )
        # June 1 → July 1 is 30 days; 15 remain. (99-49) * 15/30 = 25.00.
        assert result.proration.period_days == 30
        assert result.proration.unused_days == 15
        assert result.proration.amount == Decimal("25.00")

        # ...and the resolved window is persisted, so the summary endpoint and
        # the dunning grace clock stop reading NULL.
        async with realdb.control_sessionmaker()() as s:
            sub = (
                await s.execute(select(Subscription).where(Subscription.id == sub_id))
            ).scalar_one()
        assert sub.current_period_start == datetime(2026, 6, 1, tzinfo=UTC)
        assert sub.current_period_end == datetime(2026, 7, 1, tzinfo=UTC)
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_plan_change_rolls_a_stale_window_forward_before_prorating(
    realdb, _audit_engine_on_loop
):
    """A subscription whose stored window expired months ago must prorate
    against the period it is ACTUALLY in — clamping into a stale window leaves
    zero unused days and silently zeroes the adjustment again."""
    org_id = realdb.info("a").org_id
    try:
        old_id = await _seed_plan(realdb, code="prtest_stale_basic", price="49.00")
        await _seed_plan(realdb, code="prtest_stale_scale", price="99.00")
        # Stored window: Jan 1 → Feb 1. Change lands mid-June.
        sub_id = uuid.uuid4()
        async with realdb.control_sessionmaker()() as s:
            s.add(
                Subscription(
                    id=sub_id,
                    organization_id=org_id,
                    plan_id=old_id,
                    status="active",
                    current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
                    current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
                )
            )
            await s.commit()

        async with realdb.control_sessionmaker()() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            result = await change_plan(
                s,
                org=org,
                new_plan_code="prtest_stale_scale",
                actor_id=None,
                change_at=datetime(2026, 6, 16, tzinfo=UTC),
            )
        assert result.proration.period_days == 30  # June 1 → July 1
        assert result.proration.unused_days == 15
        assert result.proration.amount == Decimal("25.00")
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_plan_change_same_plan_is_idempotent_noop(realdb):
    org_id = realdb.info("a").org_id
    try:
        plan_id = await _seed_plan(realdb, code="prtest_solo", price="49.00")
        sub_id = await _seed_sub(realdb, org_id=org_id, plan_id=plan_id)
        async with realdb.control_sessionmaker()() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            result = await change_plan(s, org=org, new_plan_code="prtest_solo", actor_id=None)
        assert result.changed is False
        assert result.reason == "already_on_plan"
        assert result.proration.amount == Decimal("0.00")
        # No audit row for a no-op.
        assert await _audit_actions(realdb, sub_id) == []
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_plan_change_no_live_subscription_raises(realdb):
    org_id = realdb.info("a").org_id
    try:
        await _seed_plan(realdb, code="prtest_x", price="49.00")
        async with realdb.control_sessionmaker()() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            with pytest.raises(PlanChangeError):
                await change_plan(s, org=org, new_plan_code="prtest_x", actor_id=None)
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_plan_change_unknown_plan_raises(realdb):
    org_id = realdb.info("a").org_id
    try:
        plan_id = await _seed_plan(realdb, code="prtest_known", price="49.00")
        await _seed_sub(realdb, org_id=org_id, plan_id=plan_id)
        async with realdb.control_sessionmaker()() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            with pytest.raises(PlanChangeError):
                await change_plan(s, org=org, new_plan_code="prtest_does_not_exist", actor_id=None)
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_change_plan_endpoint_happy_path(realdb, _audit_engine_on_loop):
    org_id = realdb.info("a").org_id
    try:
        old_id = await _seed_plan(realdb, code="prtest_e_basic", price="49.00")
        await _seed_plan(realdb, code="prtest_e_scale", price="99.00")
        await _seed_sub(realdb, org_id=org_id, plan_id=old_id)
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post("/api/billing/change-plan", json={"plan_code": "prtest_e_scale"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["changed"] is True
        assert body["new_plan_code"] == "prtest_e_scale"
        # Proration is an exact decimal string, not a float.
        assert isinstance(body["proration"]["amount"], str)
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_change_plan_endpoint_idempotent_retry(realdb, _audit_engine_on_loop):
    """A second POST of the SAME change is a no-op (changed=false) — no double
    charge."""
    org_id = realdb.info("a").org_id
    try:
        old_id = await _seed_plan(realdb, code="prtest_r_basic", price="49.00")
        await _seed_plan(realdb, code="prtest_r_scale", price="99.00")
        await _seed_sub(realdb, org_id=org_id, plan_id=old_id)
        async with realdb.client(key="a", role="admin") as c:
            r1 = await c.post("/api/billing/change-plan", json={"plan_code": "prtest_r_scale"})
            r2 = await c.post("/api/billing/change-plan", json={"plan_code": "prtest_r_scale"})
        assert r1.status_code == r2.status_code == 200
        assert r1.json()["changed"] is True
        assert r2.json()["changed"] is False  # already on the plan now
    finally:
        await _cleanup(realdb, org_id)


@pytest.mark.asyncio
async def test_change_plan_endpoint_rbac_403_for_clerk(realdb):
    org_id = realdb.info("a").org_id
    try:
        old_id = await _seed_plan(realdb, code="prtest_rb_basic", price="49.00")
        await _seed_plan(realdb, code="prtest_rb_scale", price="99.00")
        await _seed_sub(realdb, org_id=org_id, plan_id=old_id)
        async with realdb.client(key="a", role="ap_clerk") as c:
            resp = await c.post("/api/billing/change-plan", json={"plan_code": "prtest_rb_scale"})
        assert resp.status_code == 403
    finally:
        await _cleanup(realdb, org_id)
