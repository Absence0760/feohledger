"""Several landing-page / CFO-dashboard KPIs summed the RAW `Invoice.amount` /
`Payment.amount` column across currencies, as if a EUR row and a USD row were
the same unit — while a correctly-converted `reporting_amount` (or the
equivalent `payment_reporting_amount_sql` resolution for payments) sat right
next to them, unused. This is the same class of bug `total_amount` /
`total_spend` / vendor-spend concentration already had fixed
(`docs/multi-currency.md`); this file pins the remaining surfaces:

  - `GET /api/dashboard` — `aging_reporting`, `monthly_trend[].reporting_amount`,
    `upcoming_total_amount_reporting`, `total_paid_reporting` /
    `total_pending_reporting`.
  - `GET /api/analytics/cfo` — `reporting_accounts_payable_balance`,
    `reporting_avg_daily_outflow`.
  - `GET /api/analytics/by-entity` — `reporting_outstanding_amount`.

Each test seeds one USD row (reporting currency, converts 1:1) alongside one
EUR row carrying a materialized (rate-locked) `reporting_amount` — or, for
payments, a rate-locked `source_amount`/`source_currency` home-currency leg —
and asserts the new field equals the correctly-converted total while the
pre-existing legacy field stays the naive (wrong, cross-currency) sum, so a
regression that silently reverted the fix would fail loudly rather than just
producing "a smaller number".

DO NOT run this file standalone in a concurrent build — the ``realdb`` fixture
truncates all tables sequentially. The orchestrator runs the suite at the end.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.utils.dates import utc_today

TENANT = "a"

# EUR -> USD locked rate used throughout: 1000.00 EUR books as 1086.96 USD.
_EUR_FACE = Decimal("1000.00")
_EUR_AS_USD = Decimal("1086.96")
_USD_FACE = Decimal("1000.00")


async def _default_entity_id(s):
    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _seed_open_invoices(realdb) -> None:
    """One USD + one rate-locked EUR invoice, both `approved` (open AP) with a
    `due_date` of today, so they land in every aging/upcoming/AP-balance
    surface under test."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = utc_today()
    async with mk() as s:
        ent = await _default_entity_id(s)
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number=f"MC-USD-{uuid.uuid4().hex[:6]}",
                vendor_name="Multi-Currency Supply Co",
                amount=_USD_FACE,
                currency="USD",
                status=InvoiceStatus.approved,
                invoice_date=today,
                due_date=today,
            )
        )
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number=f"MC-EUR-{uuid.uuid4().hex[:6]}",
                vendor_name="Multi-Currency Supply Co",
                amount=_EUR_FACE,
                currency="EUR",
                reporting_currency="USD",
                reporting_amount=_EUR_AS_USD,
                status=InvoiceStatus.approved,
                invoice_date=today,
                due_date=today,
            )
        )
        await s.commit()


async def _seed_completed_payments(realdb) -> None:
    """One USD invoice + payment (invoice currency == reporting currency, so
    `Payment.amount` is directly usable) and one EUR invoice + payment carrying
    a rate-locked home-currency (`source_amount`/`source_currency`) leg — the
    shape `international_payments.prepare_international_payment` writes."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    today = utc_today()
    async with mk() as s:
        ent = await _default_entity_id(s)

        usd_invoice = Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=f"MCP-USD-{uuid.uuid4().hex[:6]}",
            vendor_name="Multi-Currency Payment Co",
            amount=_USD_FACE,
            currency="USD",
            status=InvoiceStatus.paid,
            invoice_date=today,
        )
        s.add(usd_invoice)
        await s.flush()
        s.add(
            Payment(
                entity_id=ent,
                invoice_id=usd_invoice.id,
                amount=_USD_FACE,
                method="ach",
                provider="mock",
                status="completed",
                completed_at=today,
            )
        )

        eur_invoice = Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=f"MCP-EUR-{uuid.uuid4().hex[:6]}",
            vendor_name="Multi-Currency Payment Co",
            amount=_EUR_FACE,
            currency="EUR",
            status=InvoiceStatus.paid,
            invoice_date=today,
        )
        s.add(eur_invoice)
        await s.flush()
        s.add(
            Payment(
                entity_id=ent,
                invoice_id=eur_invoice.id,
                # `Payment.amount` is denominated in the INVOICE's currency —
                # the face EUR amount actually paid to the vendor.
                amount=_EUR_FACE,
                # The home-currency (USD) debit leg, locked at submission —
                # what `payment_reporting_amount_sql` reads.
                source_amount=_EUR_AS_USD,
                source_currency="USD",
                method="wire",
                provider="mock",
                status="completed",
                completed_at=today,
            )
        )
        await s.commit()


# ---------------------------------------------------------------------------
# GET /api/dashboard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_aging_reporting_converts_mixed_currency_invoices(realdb):
    await _seed_open_invoices(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        body = (await c.get("/api/dashboard")).json()

    # Both invoices have `due_date == today` → the "current" bucket.
    # Legacy `aging` is the naive face-value sum: 1000.00 + 1000.00 = 2000.00.
    assert Decimal(str(body["aging"]["current"])) == Decimal("2000.00")
    # `aging_reporting` uses the locked reporting_amount for the EUR row:
    # 1000.00 (USD) + 1086.96 (EUR converted) = 2086.96.
    assert Decimal(str(body["aging_reporting"]["current"])) == _USD_FACE + _EUR_AS_USD


@pytest.mark.asyncio
async def test_dashboard_monthly_trend_reporting_amount_converts_mixed_currency(realdb):
    await _seed_open_invoices(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        body = (await c.get("/api/dashboard")).json()

    this_month = utc_today().strftime("%Y-%m")
    row = next(r for r in body["monthly_trend"] if r["month"] == this_month)
    assert Decimal(str(row["amount"])) == Decimal("2000.00")
    assert Decimal(str(row["reporting_amount"])) == _USD_FACE + _EUR_AS_USD


@pytest.mark.asyncio
async def test_dashboard_upcoming_total_amount_reporting_converts_mixed_currency(realdb):
    await _seed_open_invoices(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        body = (await c.get("/api/dashboard")).json()

    assert Decimal(str(body["upcoming_total_amount"])) == Decimal("2000.00")
    assert Decimal(str(body["upcoming_total_amount_reporting"])) == _USD_FACE + _EUR_AS_USD
    assert body["upcoming_unconverted_count"] == 0


@pytest.mark.asyncio
async def test_dashboard_total_paid_pending_reporting_converts_mixed_currency(realdb):
    await _seed_completed_payments(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        body = (await c.get("/api/dashboard")).json()

    # Legacy `total_paid` sums raw `Payment.amount` — 1000.00 (USD) + 1000.00
    # (EUR face value) = 2000.00, a two-currency mixture read as one number.
    assert Decimal(str(body["total_paid"])) == Decimal("2000.00")
    # `total_paid_reporting` resolves the EUR leg through its locked
    # `source_amount` (1086.96 USD) instead: 1000.00 + 1086.96 = 2086.96.
    assert Decimal(str(body["total_paid_reporting"])) == _USD_FACE + _EUR_AS_USD
    assert body["total_paid_unconverted_count"] == 0


# ---------------------------------------------------------------------------
# GET /api/analytics/cfo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cfo_ap_balance_reporting_converts_mixed_currency_invoices(realdb):
    await _seed_open_invoices(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/cfo")).json()

    assert Decimal(str(body["accounts_payable_balance"])) == Decimal("2000.00")
    rollup = body["reporting_accounts_payable_balance"]
    assert rollup["reporting_currency"] == "USD"
    assert Decimal(str(rollup["total_amount"])) == _USD_FACE + _EUR_AS_USD
    assert rollup["unconverted_count"] == 0


@pytest.mark.asyncio
async def test_cfo_working_capital_reporting_converts_mixed_currency_payments(realdb):
    await _seed_completed_payments(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/cfo?period_days=365")).json()

    # Legacy `avg_daily_outflow` is derived from the naive 2000.00 total;
    # `reporting_avg_daily_outflow` from the correctly-converted 2086.96.
    legacy = Decimal(str(body["avg_daily_outflow"]))
    reporting = Decimal(str(body["reporting_avg_daily_outflow"]))
    assert reporting > legacy
    expected = ((_USD_FACE + _EUR_AS_USD) / Decimal(365)).quantize(Decimal("0.01"))
    assert reporting == expected
    assert body["reporting_avg_daily_outflow_unconverted_count"] == 0


# ---------------------------------------------------------------------------
# GET /api/analytics/by-entity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_by_entity_outstanding_reporting_converts_mixed_currency_invoices(realdb):
    await _seed_open_invoices(realdb)
    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/by-entity")).json()

    consolidated = body["consolidated"]
    assert Decimal(str(consolidated["outstanding_amount"])) == Decimal("2000.00")
    assert Decimal(str(consolidated["reporting_outstanding_amount"])) == (_USD_FACE + _EUR_AS_USD)
    assert consolidated["reporting_currency"] == "USD"
    assert consolidated["reporting_outstanding_unconverted_count"] == 0

    default_row = next(r for r in body["entities"] if r["is_default"])
    assert Decimal(str(default_row["reporting_outstanding_amount"])) == (_USD_FACE + _EUR_AS_USD)


@pytest.mark.asyncio
async def test_by_entity_spend_reporting_converts_and_matches_the_cfo_tile(realdb):
    """`total_spend` is a naive SUM across currencies, and the web labelled it
    with the entity's currency (or the org's). `reporting_total_spend` is the
    converted counterpart — and for the consolidated row it must equal `/cfo`'s
    `reporting_spend`, whose population and rollup it reuses."""
    await _seed_open_invoices(realdb)
    # A foreign invoice with NO rate lock: counted at face value, and disclosed.
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=await _default_entity_id(s),
                invoice_number=f"MC-GBP-{uuid.uuid4().hex[:6]}",
                vendor_name="Multi-Currency Supply Co",
                amount=Decimal("10.00"),
                currency="GBP",
                status=InvoiceStatus.approved,
                invoice_date=utc_today(),
                due_date=utc_today(),
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/analytics/by-entity")).json()
        cfo = (await c.get("/api/analytics/cfo")).json()

    consolidated = body["consolidated"]
    # Legacy field unchanged: the naive cross-currency sum.
    assert Decimal(str(consolidated["total_spend"])) == Decimal("2010.00")
    # Converted where a lock exists, face value (and counted) where none does.
    expected = _USD_FACE + _EUR_AS_USD + Decimal("10.00")
    assert Decimal(str(consolidated["reporting_total_spend"])) == expected
    assert consolidated["reporting_total_spend_unconverted_count"] == 1
    assert consolidated["reporting_currency"] == "USD"

    # The consolidated row and the `/cfo` spend tile are one figure.
    assert Decimal(str(cfo["reporting_spend"]["total_amount"])) == Decimal(
        str(consolidated["reporting_total_spend"])
    )
    assert cfo["reporting_spend"]["unconverted_count"] == 1

    default_row = next(r for r in body["entities"] if r["is_default"])
    assert Decimal(str(default_row["reporting_total_spend"])) == expected
    assert default_row["reporting_total_spend_unconverted_count"] == 1
