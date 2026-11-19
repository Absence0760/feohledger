"""An invoice whose currency is UNKNOWN can never move money or be counted as
money, through any door (decisions §473, with §35, §160, §200).

`invoices.currency` is NULL for "unknown" since migration 0136; legacy rows can
still hold a blank, a whitespace-only string or a malformed code, and §473 says
every reader judges all of them alike through one predicate —
`payment_runs.payable_currency_code`, its SQL twin
`currency_conversion.invoice_currency_key_sql`, and the queue's
`api/payments._queue_currency_unknown`. This file pins that from the outside:

* a parity table: the Python predicate, the boundary normalizer
  `utils.currency_code.iso_currency_or_none` and the SQL twin agree, input by
  input (the property every rollup-versus-run-builder agreement rests on);
* the money doors refuse it as `currency_unknown`: the run builder,
  `POST /api/payments`, the payment queue, dispatch preflight, the card mint
  (the backstop included) and the ERP push;
* the rollups put it under the `None` group and count it unconverted, never in
  a real currency's group: the dashboard, the CFO analytics, the payment queue's
  `by_currency`; the regulated payment totals exclude it and count it;
* no reporting lock is ever written for it;
* stating a real code afterwards makes the same row payable.

Creation doors (upload, portal submit, email intake, PEPPOL with no code, PO
flip) are pinned to write NULL by their own suites; this file starts from the
row they leave behind, in every stored spelling of "unknown".
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import String, literal, select, update

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.services.currency_conversion import invoice_currency_key_sql
from app.services.payment_runs import payable_currency_code
from app.utils.currency_code import iso_currency_or_none
from app.utils.dates import utc_today

TENANT = "a"

# ---------------------------------------------------------------------------
# Parity: one answer to "does this row record a currency?"
# ---------------------------------------------------------------------------

#: input → the code every predicate must answer (None = unknown).
PARITY_CASES: list[tuple[object, str | None]] = [
    (None, None),
    ("", None),
    ("  ", None),
    ("\t\n", None),
    ("usd", "USD"),
    (" eur ", "EUR"),
    ("\tgbp\n", "GBP"),
    ("US", None),
    ("USDX", None),
    ("ÄÖÜ", None),
    ("12$", None),
    ("uß", None),  # str.upper() would make this "USS"
    ("U S", None),
    ("ДОЛ", None),
]


@pytest.mark.parametrize(("value", "expected"), PARITY_CASES)
def test_python_predicates_agree(value, expected):
    assert payable_currency_code(value) == expected
    assert iso_currency_or_none(value) == expected


def test_non_string_inputs_are_unknown():
    for value in (0, 840, b"USD", ["USD"], Decimal("1")):
        assert payable_currency_code(value) is None  # type: ignore[arg-type]
        assert iso_currency_or_none(value) is None


def test_nbsp_is_trimmed_by_the_boundary_but_not_by_the_run_path():
    """The one documented divergence. `iso_currency_or_none` normalizes a value
    on its way IN (Unicode `strip()`), so what it stores is clean; the run path
    judges what is already stored with the ASCII trim set its SQL twin
    (`btrim`) shares, so a stored NBSP-padded code is unknown to both of those.
    Pinned so neither side drifts silently."""
    assert iso_currency_or_none(" USD") == "USD"
    assert payable_currency_code(" USD") is None


@pytest.mark.asyncio
async def test_sql_twin_agrees_with_python_predicate(realdb):
    values = [v for v, _ in PARITY_CASES if v is None or isinstance(v, str)]
    values += [" USD", " usd"]
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        for value in values:
            sql_answer = (
                await s.execute(select(invoice_currency_key_sql(literal(value, String()))))
            ).scalar_one()
            assert sql_answer == payable_currency_code(value), repr(value)


# ---------------------------------------------------------------------------
# The card mint's own backstop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, "", "   ", "US", "12$"])
async def test_card_issuance_backstop_refuses_every_unknown_spelling(stored):
    """`issue_card_for_invoice` is the last gate before a provider mints a card
    denominated in the invoice's currency. It refused only NULL / blank, so a
    malformed stored code ("US", "12$") reached the adapter as the card's
    currency. It must judge with the run path's predicate."""
    from app.services.card_issuance import issue_card_for_invoice

    invoice = SimpleNamespace(id=uuid.uuid4(), currency=stored, amount=Decimal("10.00"))
    result = await issue_card_for_invoice(
        db=None,
        invoice=invoice,
        organization_id=uuid.uuid4(),
        org_settings={"cards": {"enabled": True, "provider": "mock"}},
        app_settings=SimpleNamespace(card_provider="mock"),
    )
    assert result.success is False
    assert result.failure_reason == "currency_unknown"


# ---------------------------------------------------------------------------
# Real-DB doors
# ---------------------------------------------------------------------------

#: Every stored spelling of "unknown" the column can hold (String(3)).
UNKNOWN_SPELLINGS = [None, "", "   ", "US", "12$"]


async def _seed(realdb, *, currency, number: str, amount: str = "750.00") -> uuid.UUID:
    """An approved invoice from an ACTIVE vendor — payable in every respect
    except its currency, so the only refusal left is `currency_unknown`."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = uuid.uuid4()
    async with mk() as s:
        vendor = Vendor(
            organization_id=org_id,
            name=f"Unknown Currency Vendor {inv_id.hex[:6]}",
            status="active",
        )
        s.add(vendor)
        await s.flush()
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                correlation_id=uuid.uuid4(),
                invoice_number=number,
                vendor_name=vendor.name,
                vendor_id=vendor.id,
                amount=Decimal(amount),
                currency=currency,
                status=InvoiceStatus.approved,
                invoice_date=utc_today(),
                due_date=utc_today() + timedelta(days=10),
            )
        )
        await s.commit()
    return inv_id


async def _payments_for(realdb, inv_id) -> list[Payment]:
    async with realdb.sessionmaker(TENANT)() as s:
        return list(
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", UNKNOWN_SPELLINGS)
async def test_run_builder_refuses(realdb, stored):
    inv_id = await _seed(realdb, currency=stored, number=f"UC-RUN-{stored!r}")
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/payments/runs", json={"items": [{"invoice_id": str(inv_id)}]})
    assert 400 <= resp.status_code < 500, resp.text
    assert "currency_unknown" in resp.text
    assert await _payments_for(realdb, inv_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", UNKNOWN_SPELLINGS)
async def test_single_payment_refuses(realdb, stored):
    inv_id = await _seed(realdb, currency=stored, number=f"UC-PAY-{stored!r}")
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/payments", json={"invoice_id": str(inv_id), "method": "ach"})
    assert resp.status_code == 409, resp.text
    assert "currency_unknown" in resp.text
    assert await _payments_for(realdb, inv_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", UNKNOWN_SPELLINGS)
async def test_payment_queue_blocks_the_row_and_groups_it_under_none(realdb, stored):
    unknown_id = await _seed(realdb, currency=stored, number="UC-Q-UNKNOWN", amount="750.00")
    await _seed(realdb, currency="USD", number="UC-Q-USD", amount="100.00")
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get("/api/payments/queue")
        ids = await c.get("/api/payments/queue/ids")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    row = next(i for i in body["items"] if i["invoice_number"] == "UC-Q-UNKNOWN")
    assert row["blocked"] is True
    assert row["blocked_reason"] == "currency_unknown"
    # "Select all" never stages it.
    assert ids.status_code == 200, ids.text
    assert str(unknown_id) not in set(ids.json()["ids"])

    groups = {g["currency"]: g for g in body["by_currency"]}
    assert groups["USD"]["count"] == 1
    assert Decimal(groups["USD"]["total_amount"]) == Decimal("100.00")
    assert groups[None]["count"] == 1
    assert body["unconverted_count"] >= 1
    # No real-currency group absorbed it, under any spelling.
    assert set(groups) == {"USD", None}


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", UNKNOWN_SPELLINGS)
async def test_dispatch_preflight_refuses(realdb, stored):
    from app.services.payment_runs import dispatch_preflight

    inv_id = await _seed(realdb, currency=stored, number=f"UC-PRE-{stored!r}")
    async with realdb.sessionmaker(TENANT)() as s:
        invoice = await s.get(Invoice, inv_id)
        payment = Payment(
            id=uuid.uuid4(),
            invoice_id=inv_id,
            organization_id=invoice.organization_id,
            amount=invoice.amount,
            method="ach",
        )
        reason = await dispatch_preflight(s, payment, invoice, as_of=utc_today())
    assert reason == "currency_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", UNKNOWN_SPELLINGS)
async def test_erp_push_refuses(realdb, stored):
    inv_id = await _seed(realdb, currency=stored, number=f"UC-ERP-{stored!r}")
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert resp.status_code == 409, resp.text
    assert "currency_unknown" in resp.text
    async with realdb.sessionmaker(TENANT)() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.approved


@pytest.mark.asyncio
async def test_no_reporting_lock_is_written():
    """`materialize_reporting_amount` never locks an unknown-currency row and
    never asks the FX adapter for a rate (there is no pair to price)."""
    from app.services.currency_conversion import materialize_reporting_amount

    class _NoFx:
        def __getattr__(self, name):
            raise AssertionError(f"FX adapter consulted ({name}) for an unknown currency")

    for stored in UNKNOWN_SPELLINGS:
        inv = Invoice(
            invoice_number="UC-LOCK",
            vendor_name="v",
            amount=Decimal("10.00"),
            currency=stored,
            organization_id=uuid.uuid4(),
        )
        await materialize_reporting_amount(inv, reporting_currency="USD", fx_adapter=_NoFx())
        assert inv.reporting_amount is None, repr(stored)
        assert inv.reporting_currency is None, repr(stored)


@pytest.mark.asyncio
async def test_dashboard_rollup_groups_unknown_under_none(realdb):
    await _seed(realdb, currency=None, number="UC-DASH-NULL", amount="750.00")
    await _seed(realdb, currency="", number="UC-DASH-BLANK", amount="50.00")
    await _seed(realdb, currency="USD", number="UC-DASH-USD", amount="100.00")
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get("/api/dashboard")
    assert resp.status_code == 200, resp.text
    rollup = resp.json()["reporting"]
    groups = {g["currency"]: g for g in rollup["by_currency"]}
    assert set(groups) == {"USD", None}
    assert groups["USD"]["count"] == 1
    assert Decimal(str(groups["USD"]["original_amount"])) == Decimal("100.00")
    assert groups["USD"]["unconverted_count"] == 0
    # Both spellings of unknown land in ONE group, each counted unconverted.
    assert groups[None]["count"] == 2
    assert groups[None]["unconverted_count"] == 2
    assert rollup["unconverted_count"] == 2


@pytest.mark.asyncio
async def test_cfo_spend_groups_unknown_under_none(realdb):
    await _seed(realdb, currency=None, number="UC-CFO-NULL", amount="750.00")
    await _seed(realdb, currency="USD", number="UC-CFO-USD", amount="100.00")
    async with realdb.client(key=TENANT, role="cfo") as c:
        resp = await c.get("/api/analytics/cfo")
    assert resp.status_code == 200, resp.text
    spend = resp.json()["reporting_spend"]
    groups = {g["currency"]: g for g in spend["by_currency"]}
    assert set(groups) == {"USD", None}
    assert groups["USD"]["count"] == 1
    assert groups["USD"]["unconverted_count"] == 0
    assert groups[None]["unconverted_count"] == groups[None]["count"] == 1
    assert spend["unconverted_count"] == 1


@pytest.mark.asyncio
async def test_payment_totals_exclude_a_payment_on_an_unknown_currency_invoice(realdb):
    """The regulated-figure resolver (shared with the 1099 report and the risk
    score) has no face-value fallback: a payment whose invoice records no
    currency is excluded from `total_paid` and counted, never added as USD."""
    unknown_id = await _seed(realdb, currency=None, number="UC-SUM-NULL", amount="750.00")
    usd_id = await _seed(realdb, currency="USD", number="UC-SUM-USD", amount="100.00")
    async with realdb.sessionmaker(TENANT)() as s:
        for inv_id, amount in ((unknown_id, "750.00"), (usd_id, "100.00")):
            await s.execute(
                update(Invoice).where(Invoice.id == inv_id).values(status=InvoiceStatus.paid)
            )
            s.add(
                Payment(
                    invoice_id=inv_id,
                    organization_id=realdb.info(TENANT).org_id,
                    amount=Decimal(amount),
                    method="ach",
                    status="completed",
                )
            )
        await s.commit()
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.get("/api/payments/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(str(body["total_paid"])) == Decimal("100.00")
    assert body["unconverted_payment_count"] == 1


@pytest.mark.asyncio
async def test_stating_a_real_currency_makes_it_payable_and_counted(realdb):
    inv_id = await _seed(realdb, currency=None, number="UC-FIXED", amount="750.00")
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(update(Invoice).where(Invoice.id == inv_id).values(currency="USD"))
        await s.commit()
    async with realdb.client(key=TENANT, role="admin") as c:
        queue = (await c.get("/api/payments/queue")).json()
        row = next(i for i in queue["items"] if i["invoice_number"] == "UC-FIXED")
        assert row["blocked"] is False
        assert {g["currency"] for g in queue["by_currency"]} == {"USD"}
        resp = await c.post("/api/payments/runs", json={"items": [{"invoice_id": str(inv_id)}]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["total_amount"] == "750.00"
    assert len(await _payments_for(realdb, inv_id)) == 1
