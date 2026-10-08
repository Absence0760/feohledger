"""The AI-read-invoice meter (decisions §253): the counting rule, the allowance
and cap arithmetic, and the billing-adapter wire shape that bills the overage.

Pure tests need no services. The counting tests use the `realdb` harness, since
the rule is a SQL filter and only a real database proves it. The end-to-end
enforcement / reporting / notice paths live in `test_ai_invoice_enforcement.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import delete

from app.models.usage import ExtractionUsage
from app.services.billing import ai_invoice_meter as meter
from app.services.billing.ai_invoice_meter import (
    PAUSE_ALLOWANCE_REACHED,
    PAUSE_SPEND_CAP_REACHED,
    AiAllowance,
    allowance_for_plan,
    allowance_from_component,
    billable_overage_units,
    cap_units,
    count_ai_invoices,
    decide,
    is_invoice_counted,
    parse_spend_cap,
    period_of,
    project_month_end,
    summarize,
)
from app.services.billing.ai_overage import event_timestamp, overage_identifier
from app.services.billing.ai_usage_notices import due_notices
from app.services.billing.plan_catalog import DEFAULT_PLAN_CATALOG, METER_AI_INVOICES
from app.services.billing.usage_rollup import UsageRollup, rollup_usage
from app.services.billing_adapters import mock_adapter
from app.services.billing_adapters.base import AI_INVOICE_OVERAGE_EVENT, MeterEvent
from app.services.billing_adapters.mock_adapter import MockBillingAdapter
from app.services.billing_adapters.stripe_billing import (
    BillingNotConfigured,
    StripeBillingAdapter,
)
from app.services.notification_templates import render_ai_invoice_usage

FREE = AiAllowance(included=100, overage_unit_price=None, plan_code="free")
GROWTH = AiAllowance(included=500, overage_unit_price=Decimal("0.10"), plan_code="growth")


# ---------------------------------------------------------------------------
# Which providers count
# ---------------------------------------------------------------------------


def test_every_registered_extraction_adapter_has_an_explicit_billing_decision():
    """A new adapter must be classified, not silently default either way."""
    from app.services.extraction_adapters import dispatcher

    dispatcher._ensure_builtin_adapters()
    registered = set(dispatcher._ADAPTER_REGISTRY)
    billable = meter.BILLABLE_EXTRACTION_PROVIDERS
    free = meter.NON_BILLABLE_EXTRACTION_PROVIDERS
    assert not billable & free
    assert registered == billable | free, (
        f"unclassified: {sorted(registered - (billable | free))}; "
        f"stale: {sorted((billable | free) - registered)}"
    )


@pytest.mark.parametrize(
    ("program_type", "provider", "expected"),
    [
        ("platform", "claude_vision", True),
        ("platform", "openai_vision", True),
        ("platform", "aws_textract", True),
        # The keyless dev / e2e reader: counting it would trip Free in e2e.
        ("platform", "mock", False),
        # Self-hosted: no per-call cost to pass on.
        ("platform", "ollama", False),
        # A structured e-invoice never reaches a model.
        ("platform", "einvoice", False),
        # BYOK is the customer's own model bill.
        ("byok", "claude_vision", False),
        # An unknown provider is never billed by default.
        ("platform", "something_new", False),
    ],
)
def test_is_billable_read(program_type, provider, expected):
    assert meter.is_billable_read(program_type=program_type, provider=provider) is expected


def test_period_is_the_utc_calendar_month():
    # 01:00 on 1 October in UTC+2 is still 30 September in UTC.
    plus_two = timezone(timedelta(hours=2))
    assert period_of(datetime(2026, 10, 1, 1, 0, tzinfo=plus_two)) == "2026-09"
    assert period_of(datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)) == "2026-09"
    assert period_of(datetime(2026, 10, 1, 0, 0, tzinfo=UTC)) == "2026-10"


# ---------------------------------------------------------------------------
# Allowance parsing
# ---------------------------------------------------------------------------


def test_catalog_allowances_parse_exactly():
    by_code = {p["code"]: p for p in DEFAULT_PLAN_CATALOG}
    growth = allowance_from_component(by_code["growth"]["usage_components"][METER_AI_INVOICES])
    assert growth.included == 500 and growth.overage_unit_price == Decimal("0.10")
    free = allowance_from_component(by_code["free"]["usage_components"][METER_AI_INVOICES])
    assert free.included == 100 and free.overage_unit_price is None
    assert free.metered and not free.bills_overage


def test_no_plan_reads_as_the_free_allowance():
    allowance = allowance_for_plan(None)
    assert allowance.included == 100 and allowance.overage_unit_price is None


def test_a_plan_without_the_component_is_unmetered():
    plan = SimpleNamespace(code="enterprise_x", usage_components={})
    assert not allowance_for_plan(plan).metered


@pytest.mark.parametrize(
    "component",
    [
        {"included": "100", "overage_unit_price": None},
        {"included": -1, "overage_unit_price": None},
        {"included": True, "overage_unit_price": None},
        {"included": 10, "overage_unit_price": "free"},
        {"included": 10, "overage_unit_price": "-0.10"},
        {"overage_unit_price": "0.10"},
        "not a dict",
    ],
)
def test_a_malformed_component_is_unmetered_never_a_pause(component):
    allowance = allowance_from_component(component, plan_code="broken")
    assert not allowance.metered and not allowance.bills_overage


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("25.00", Decimal("25.00")),
        ("0", Decimal("0")),
        ("-1", None),  # out of range is ignored, never a zero cap
        ("lots", None),
        ("NaN", None),
    ],
)
def test_parse_spend_cap(raw, expected):
    settings = {} if raw is None else {"billing": {"monthly_spend_cap": raw}}
    assert parse_spend_cap(settings) == expected


# ---------------------------------------------------------------------------
# The gate decision (pure)
# ---------------------------------------------------------------------------


def test_within_the_allowance_reads():
    assert decide(used=99, already_counted=False, allowance=FREE, cap=None).allowed


def test_free_pauses_at_the_limit():
    result = decide(used=100, already_counted=False, allowance=FREE, cap=None)
    assert not result.allowed and result.reason == PAUSE_ALLOWANCE_REACHED


def test_a_reread_of_a_counted_invoice_is_never_refused():
    assert decide(used=10_000, already_counted=True, allowance=FREE, cap=None).allowed
    assert decide(used=10_000, already_counted=True, allowance=GROWTH, cap=Decimal("0")).allowed


def test_paid_tier_bills_overage_instead_of_pausing():
    assert decide(used=5_000, already_counted=False, allowance=GROWTH, cap=None).allowed


def test_paid_tier_pauses_at_the_spending_cap():
    cap = Decimal("1.00")  # 10 overage reads at $0.10
    assert decide(used=509, already_counted=False, allowance=GROWTH, cap=cap).allowed
    result = decide(used=510, already_counted=False, allowance=GROWTH, cap=cap)
    assert not result.allowed and result.reason == PAUSE_SPEND_CAP_REACHED


def test_a_zero_cap_pauses_a_paid_tier_at_its_allowance():
    assert decide(used=499, already_counted=False, allowance=GROWTH, cap=Decimal("0")).allowed
    result = decide(used=500, already_counted=False, allowance=GROWTH, cap=Decimal("0"))
    assert result.reason == PAUSE_SPEND_CAP_REACHED


def test_unmetered_never_pauses():
    unmetered = AiAllowance(included=None, overage_unit_price=None)
    assert decide(used=10**9, already_counted=False, allowance=unmetered, cap=None).allowed


def test_cap_units_floor_so_the_bill_never_passes_the_cap():
    assert cap_units(GROWTH, Decimal("1.05")) == 10
    assert cap_units(GROWTH, None) is None
    assert cap_units(FREE, Decimal("5")) is None  # Free bills nothing


def test_billable_overage_is_clamped_to_the_cap_even_past_the_gate():
    """The gate is unlocked; a race can count past the cap. The bill cannot."""
    assert billable_overage_units(530, GROWTH, None) == 30
    assert billable_overage_units(530, GROWTH, Decimal("2.00")) == 20
    assert billable_overage_units(150, FREE, None) == 0


def test_summary_money_is_exact_and_projection_is_clamped():
    now = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)  # 15 of 30 days elapsed
    summary = summarize(used=530, allowance=GROWTH, cap=Decimal("10.00"), now=now)
    assert summary.overage_units == 30
    assert summary.overage_amount == Decimal("3.00")
    # 530 at half-month pace projects 1060 → 560 overage, capped at 100 units.
    assert summary.projected_overage_amount == Decimal("10.00")
    assert isinstance(summary.overage_amount, Decimal)
    assert not summary.paused


def test_project_month_end_is_integer_and_never_below_used():
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert project_month_end(5, now=now) == 5 * 60  # half a day of a 30-day month
    assert project_month_end(0, now=now) == 0


# ---------------------------------------------------------------------------
# Notices + overage identity (pure)
# ---------------------------------------------------------------------------


def test_due_notices():
    assert due_notices(79, FREE, None) == []
    assert due_notices(80, FREE, None) == ["80"]
    assert due_notices(100, FREE, None) == ["80", "100"]
    # 80% of 101 is 80.8 — reached at 81, never via a float.
    odd = AiAllowance(included=101, overage_unit_price=None)
    assert due_notices(80, odd, None) == [] and due_notices(81, odd, None) == ["80"]
    assert due_notices(510, GROWTH, Decimal("1.00")) == ["80", "100", "cap"]
    assert due_notices(509, GROWTH, Decimal("1.00")) == ["80", "100"]
    assert due_notices(10**6, AiAllowance(included=None, overage_unit_price=None), None) == []


def test_notice_text_carries_no_record_or_person():
    rendered = render_ai_invoice_usage(
        notice="100",
        period="2026-09",
        used=500,
        included=500,
        overage_unit_price=Decimal("0.10"),
        spend_cap=None,
    )
    assert "USD 0.10" in rendered.body_text
    capped = render_ai_invoice_usage(
        notice="cap",
        period="2026-09",
        used=510,
        included=500,
        overage_unit_price=Decimal("0.10"),
        spend_cap=Decimal("1.00"),
    )
    assert "USD 1.00" in capped.body_text and "paused" in capped.body_text


def test_overage_identifier_is_stable_and_within_the_provider_limit():
    org = uuid.uuid4()
    ident = overage_identifier(org, "2026-09", 1234)
    assert ident == f"ai-overage:{org}:2026-09:1234"
    assert len(ident) <= 100


def test_event_timestamp_window():
    now = datetime(2026, 9, 10, tzinfo=UTC)
    assert event_timestamp("2026-09", now=now) == int(now.timestamp())
    # After the month: its last second, so it bills into the month it was used in.
    after = datetime(2026, 10, 2, tzinfo=UTC)
    assert event_timestamp("2026-09", now=after) == int(
        datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC).timestamp()
    )
    # Too old for the provider (35 days) → cannot be reported any more.
    assert event_timestamp("2026-09", now=datetime(2026, 11, 20, tzinfo=UTC)) is None
    # A future period is never reported.
    assert event_timestamp("2026-10", now=now) is None


def test_rollup_emits_the_ai_invoices_meter():
    rollup = UsageRollup(organization_id="o", period="2026-09", ai_invoices=7)
    assert rollup.as_meters()[METER_AI_INVOICES] == "7"


# ---------------------------------------------------------------------------
# Billing adapters
# ---------------------------------------------------------------------------


def _event(identifier="ai-overage:o:2026-09:1", customer="cus_1"):
    return MeterEvent(
        event_name=AI_INVOICE_OVERAGE_EVENT,
        customer_id=customer,
        value="1",
        identifier=identifier,
        timestamp=1_790_000_000,
    )


async def test_mock_adapter_records_meter_events_once_per_identifier():
    mock_adapter.RECORDED_METER_EVENTS.clear()
    adapter = MockBillingAdapter()
    await adapter.report_meter_event(_event("a"))
    await adapter.report_meter_event(_event("a"))  # a retry
    await adapter.report_meter_event(_event("b", customer=None))
    assert [e.identifier for e in mock_adapter.RECORDED_METER_EVENTS] == ["a", "b"]
    assert await adapter.ensure_overage_price(plan_code="growth", unit_price=Decimal("0.10")) == (
        "mock_overage_price_growth"
    )
    mock_adapter.RECORDED_METER_EVENTS.clear()


def _stripe(handler, **config):
    adapter = StripeBillingAdapter({"stripe_api_key": "sk_test_x", **config})
    transport = httpx.MockTransport(handler)

    def _client():
        return httpx.AsyncClient(
            base_url="https://api.stripe.test", auth=(adapter._api_key, ""), transport=transport
        )

    adapter._client = _client  # type: ignore[method-assign]
    return adapter


async def test_stripe_meter_event_payload_is_idempotent_on_its_identifier():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"object": "billing.meter_event"})

    await _stripe(handler).report_meter_event(_event("ai-overage:o:2026-09:7"))
    (req,) = captured
    assert req.url.path == "/v1/billing/meter_events"
    form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
    assert form == {
        "event_name": AI_INVOICE_OVERAGE_EVENT,
        "payload[stripe_customer_id]": "cus_1",
        "payload[value]": "1",  # a decimal STRING, never a float
        "identifier": "ai-overage:o:2026-09:7",
        "timestamp": "1790000000",
    }
    # The idempotency key IS the identifier, so a retried report replays the
    # original success instead of failing as a duplicate.
    assert req.headers["Idempotency-Key"] == "ai-overage:o:2026-09:7"


async def test_stripe_meter_event_fails_closed_without_customer_or_key():
    with pytest.raises(BillingNotConfigured):
        await _stripe(lambda r: httpx.Response(200, json={})).report_meter_event(
            _event(customer=None)
        )
    with pytest.raises(BillingNotConfigured):
        await StripeBillingAdapter({}).report_meter_event(_event())


async def test_stripe_overage_price_is_per_unit_metered_on_the_overage_meter():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/billing/meters" and request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/billing/meters":
            return httpx.Response(200, json={"id": "mtr_1"})
        return httpx.Response(200, json={"id": "price_over"})

    price_id = await _stripe(handler).ensure_overage_price(
        plan_code="scale", unit_price=Decimal("0.07")
    )
    assert price_id == "price_over"
    meter_create = seen[1]
    meter_form = {k: v[0] for k, v in parse_qs(meter_create.content.decode()).items()}
    assert meter_form["event_name"] == AI_INVOICE_OVERAGE_EVENT
    assert meter_form["default_aggregation[formula]"] == "sum"
    assert meter_form["customer_mapping[event_payload_key]"] == "stripe_customer_id"
    assert meter_create.headers["Idempotency-Key"]

    price_req = seen[2]
    form = {k: v[0] for k, v in parse_qs(price_req.content.decode()).items()}
    assert form["recurring[usage_type]"] == "metered"
    assert form["recurring[meter]"] == "mtr_1"
    # $0.07 → 7 minor units, exact; per-unit (no graduated tiers — the allowance
    # is applied by our own calendar-month count).
    assert form["unit_amount_decimal"] == "7"
    assert "billing_scheme" not in form and "tiers_mode" not in form
    assert price_req.headers["Idempotency-Key"] == "feohledger-ai-overage-price-scale-7-usd"


async def test_stripe_overage_price_reuses_an_existing_meter():
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [{"id": "mtr_old", "event_name": AI_INVOICE_OVERAGE_EVENT}]},
            )
        posts.append(request.url.path)
        return httpx.Response(200, json={"id": "price_over"})

    await _stripe(handler).ensure_overage_price(plan_code="growth", unit_price=Decimal("0.10"))
    assert posts == ["/v1/prices"]


async def test_stripe_subscription_carries_the_overage_item_when_resolved():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"id": "sub_1", "status": "active"})

    from app.services.billing_adapters.base import CreateSubscriptionRequest

    adapter = _stripe(
        handler,
        stripe_customer_id="cus_1",
        stripe_price_id="price_base",
        stripe_overage_price_id="price_over",
    )
    await adapter.create_subscription(
        CreateSubscriptionRequest(
            organization_id="o", plan_code="growth", monthly_price=Decimal("49.00")
        )
    )
    assert bodies[0]["items[0][price]"] == ["price_base"]
    assert bodies[0]["items[1][price]"] == ["price_over"]


# ---------------------------------------------------------------------------
# The count itself — real Postgres
# ---------------------------------------------------------------------------


async def _clean_usage(realdb, org_id):
    async with realdb.sessionmaker("a")() as s:
        await s.execute(delete(ExtractionUsage).where(ExtractionUsage.organization_id == org_id))
        await s.commit()


@pytest.mark.asyncio
async def test_count_is_distinct_platform_successful_billable_reads(realdb):
    org_id = realdb.info("a").org_id
    period = "2026-09"
    counted_a, counted_b = uuid.uuid4(), uuid.uuid4()

    def row(invoice_id, *, provider="claude_vision", program="platform", ok=True, p=period):
        return ExtractionUsage(
            id=uuid.uuid4(),
            invoice_id=invoice_id,
            provider=provider,
            program_type=program,
            period=p,
            success=ok,
            organization_id=org_id,
        )

    try:
        async with realdb.sessionmaker("a")() as s:
            s.add_all(
                [
                    row(counted_a),
                    row(counted_a),  # re-read same month → still one invoice
                    row(counted_b, provider="openai_vision"),
                    row(uuid.uuid4(), provider="mock"),
                    row(uuid.uuid4(), provider="ollama"),
                    row(uuid.uuid4(), provider="einvoice"),
                    row(uuid.uuid4(), program="byok"),
                    row(uuid.uuid4(), ok=False),  # our failed read is not usage
                    row(uuid.uuid4(), p="2026-08"),  # another month
                    row(counted_a, p="2026-10"),  # same invoice next month counts there
                ]
            )
            await s.commit()

        async with realdb.sessionmaker("a")() as s:
            assert await count_ai_invoices(s, organization_id=org_id, period=period) == 2
            assert await count_ai_invoices(s, organization_id=org_id, period="2026-10") == 1
            assert await is_invoice_counted(
                s, organization_id=org_id, period=period, invoice_id=counted_a
            )
            assert not await is_invoice_counted(
                s, organization_id=org_id, period="2026-08", invoice_id=counted_a
            )
            rollup = await rollup_usage(s, organization_id=org_id, period=period)
        # The raw platform counter still counts rows; the priced meter does not.
        assert rollup.extractions == 8
        assert rollup.extractions_platform == 7
        assert rollup.ai_invoices == 2
    finally:
        await _clean_usage(realdb, org_id)
