"""Rebate yield divides a windowed numerator by a windowed denominator.

`GET /api/analytics/cfo` computes `total_spend` over the trailing `period_days`
and hands `months_in_period` describing that same window to
`compute_rebate_yield`. The rebate sum carried **no date predicate at all**, so
the numerator was every rebate the tenant had ever booked:

* `yield_pct` = lifetime rebates / this window's spend;
* `annualised_rebates` = lifetime rebates x 12 / this window's months — a
  three-year total multiplied by twelve when the CFO looks at a 30-day view.

`CardRebate` carries `created_at`, so the window was available and simply not
applied.

DO NOT run this file standalone in a concurrent build — the ``realdb`` fixture
truncates all tables sequentially. The orchestrator runs the suite at the end.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.virtual_card import CardRebate, VirtualCard
from app.utils.dates import utc_today

TENANT = "a"


async def _seed(realdb) -> None:
    """$10,000 of spend in the last 30 days, one rebate inside the window and
    one booked two years ago."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = utc_today()
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        # (label, days ago, invoice amount, rebate amount). The two-year-old
        # invoice sits outside BOTH windows under test, so only its rebate can
        # move the numbers.
        for label, days_ago, spend, rebate in (
            ("recent", 5, "10000.00", "100.00"),
            ("ancient", 730, "350000.00", "3500.00"),
        ):
            invoice = Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number=f"REB-{label.upper()}-{uuid.uuid4().hex[:6]}",
                vendor_name="Rebate Window Co",
                amount=Decimal(spend),
                currency="USD",
                status=InvoiceStatus.approved,
                invoice_date=today - timedelta(days=days_ago),
            )
            s.add(invoice)
            await s.flush()
            card = VirtualCard(
                organization_id=org_id,
                entity_id=ent,
                invoice_id=invoice.id,
                provider_card_id=f"vc-rebate-{label}-{uuid.uuid4().hex[:6]}",
                card_provider="mock",
                amount_limit=Decimal(spend),
                amount_charged=Decimal(spend),
                status="charged",
                currency="USD",
            )
            s.add(card)
            await s.flush()
            s.add(
                CardRebate(
                    organization_id=org_id,
                    virtual_card_id=card.id,
                    amount=Decimal(rebate),
                    rate=Decimal("0.0100"),
                    status="confirmed",
                    created_at=datetime.now(UTC) - timedelta(days=days_ago),
                )
            )
        await s.commit()


@pytest.mark.asyncio
async def test_rebate_yield_counts_only_rebates_inside_the_window(realdb):
    await _seed(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/cfo?period_days=30")).json()
    rebate = body["rebate_yield"]

    # Only the in-window $100 counts — not the $3,500 booked two years ago
    # (pre-fix: 3600.00).
    assert Decimal(str(rebate["rebates_total"])) == Decimal("100.00")
    # 100 / 10000 = 1.00% (pre-fix: 36.00%).
    assert Decimal(str(rebate["yield_pct"])) == Decimal("1.00")
    # One month in the window → 100 x 12 (pre-fix: 43200.00).
    assert Decimal(str(rebate["annualised_rebates"])) == Decimal("1200.00")


@pytest.mark.asyncio
async def test_a_rebate_outside_every_window_never_inflates_the_run_rate(realdb):
    """A 365-day view still excludes the two-year-old rebate."""
    await _seed(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/cfo?period_days=365")).json()
    assert Decimal(str(body["rebate_yield"]["rebates_total"])) == Decimal("100.00")


@pytest.mark.asyncio
async def test_annualised_rebates_use_the_whole_window_not_a_floored_month_count(realdb):
    """`months_in_period` was `period_days // 30`, so a 59-day window counted
    as ONE month and its rebates were multiplied by 12 — nearly double the
    run-rate. The window is 59/30 months; $100 over it annualises to
    100 x 12 x 30 / 59 = 610.17, not 1200.00."""
    await _seed(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/cfo?period_days=59")).json()
    rebate = body["rebate_yield"]
    assert Decimal(str(rebate["rebates_total"])) == Decimal("100.00")
    assert Decimal(str(rebate["annualised_rebates"])) == Decimal("610.17")


@pytest.mark.asyncio
async def test_rebate_yield_divides_by_reporting_currency_spend(realdb):
    """The numerator is reporting-currency rebates only (decisions §62), so the
    denominator must be reporting-currency spend too. It was the naive
    cross-currency `total_spend`: one ¥1,000,000 invoice locked at $6,700 made
    the window's "spend" 1,010,000 and the 1.00%-ish yield read 0.01%."""
    await _seed(realdb)
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number=f"REB-JPY-{uuid.uuid4().hex[:6]}",
                vendor_name="Rebate Window Co",
                amount=Decimal("1000000.00"),
                currency="JPY",
                reporting_amount=Decimal("6700.00"),
                reporting_currency="USD",
                status=InvoiceStatus.approved,
                invoice_date=utc_today() - timedelta(days=5),
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/cfo?period_days=30")).json()
    rebate = body["rebate_yield"]
    # 10,000 USD + the yen invoice's locked 6,700 USD.
    assert rebate["total_spend"] == "16700.00"
    assert rebate["total_spend"] == body["reporting_spend"]["total_amount"]
    # 100 / 16,700 = 0.5988…% → 0.60 (pre-fix: 100 / 1,010,000 → 0.01).
    assert Decimal(str(rebate["yield_pct"])) == Decimal("0.60")
