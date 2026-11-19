"""`POST /api/payments/runs` (and the AI Cash-Flow Copilot's draft-run route,
which shares `services.payment_runs.create_payment_run_for_invoices` verbatim)
must refuse to batch invoices of different currencies into one run.

`PaymentRun.total_amount` is a single bare `Numeric` column with no currency
of its own, and the CFO-approval-threshold check compares it against a bare
org-wide number. Before this fix, `create_payment_run_for_invoices` summed
`net_amount` across every invoice in the batch regardless of currency — a
$6,000 USD invoice and a €6,000 EUR invoice became one meaningless "$12,000"
total, which could misfire (or fail to fire) the CFO gate on a face-value
coincidence across currencies. Same currency-mixing bug class as
`services/budget_service.py` (f1224025) and this session's sibling fixes in
`structuring.py` / `contract_spend.py` / `invoice_warnings.py`.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor

pytestmark = pytest.mark.asyncio


async def _default_entity_id(session, org_id):
    return (
        await session.execute(
            select(Entity.id).where(Entity.organization_id == org_id, Entity.is_default)
        )
    ).scalar_one()


async def _seed_vendor_and_invoice(mk, org_id, entity_id, *, number, amount, currency):
    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="Currency Test Vendor")
        s.add(vendor)
        await s.flush()
        inv = Invoice(
            organization_id=org_id,
            entity_id=entity_id,
            invoice_number=number,
            vendor_name=vendor.name,
            vendor_id=vendor.id,
            amount=Decimal(amount),
            currency=currency,
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return str(inv.id)


async def test_payment_run_refuses_mixed_currency_invoices(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s, info.org_id)

    usd_id = await _seed_vendor_and_invoice(
        mk, info.org_id, ent, number="MIX-USD", amount="6000.00", currency="USD"
    )
    eur_id = await _seed_vendor_and_invoice(
        mk, info.org_id, ent, number="MIX-EUR", amount="6000.00", currency="EUR"
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": usd_id}, {"invoice_id": eur_id}]},
        )
    assert resp.status_code == 422, resp.text
    assert "currency" in resp.json()["detail"].lower()

    # No PaymentRun/Payment rows must have been persisted on the refused path.
    async with mk() as s:
        from app.models.payment import Payment, PaymentRun

        runs = (
            (await s.execute(select(PaymentRun).where(PaymentRun.organization_id == info.org_id)))
            .scalars()
            .all()
        )
        assert not any(r.total_amount == Decimal("12000.00") for r in runs)
        payments = (
            (await s.execute(select(Payment).where(Payment.invoice_id.in_([usd_id, eur_id]))))
            .scalars()
            .all()
        )
        assert payments == []


async def test_payment_run_allows_same_currency_invoices(realdb):
    """The new guard must not reject the ordinary, same-currency case."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s, info.org_id)

    inv_a = await _seed_vendor_and_invoice(
        mk, info.org_id, ent, number="SAME-1", amount="300.00", currency="USD"
    )
    inv_b = await _seed_vendor_and_invoice(
        mk, info.org_id, ent, number="SAME-2", amount="200.00", currency="USD"
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": inv_a}, {"invoice_id": inv_b}]},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["total_amount"] == "500.00"
    assert resp.json()["payment_count"] == 2


# ---------------------------------------------------------------------------
# An invoice that records no currency is refused, never read as USD
# (decisions §453 removed the same substitution from `/draft-run`).
# ---------------------------------------------------------------------------


async def _blank_currency(mk, invoice_id, value: str | None = None) -> None:
    """An invoice that records no currency: NULL, the one spelling since
    migration 0136 (an upload extraction found no code in), or the pre-0136
    `""` sentinel. Written past the API the way a concurrent change would land
    between `/draft-run`'s read and the builder's lock."""
    from sqlalchemy import update

    async with mk() as s:
        await s.execute(update(Invoice).where(Invoice.id == invoice_id).values(currency=value))
        await s.commit()


async def _payment_rows(mk, invoice_ids):
    from app.models.payment import Payment

    async with mk() as s:
        return (
            (await s.execute(select(Payment).where(Payment.invoice_id.in_(invoice_ids))))
            .scalars()
            .all()
        )


@pytest.mark.parametrize("blank", [None, "", "   "])
async def test_payment_run_refuses_an_invoice_with_no_currency(realdb, blank):
    """A blank currency used to be read as USD and staged; now it is the coded
    `currency_unknown` 409 naming the invoice, and nothing is booked. Batched
    with a USD invoice it is not a "second currency" (the mixed-currency 422)
    either: it is no currency."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    tag = "N" if blank is None else len(blank)
    async with mk() as s:
        ent = await _default_entity_id(s, info.org_id)
    blank_id = await _seed_vendor_and_invoice(
        mk, info.org_id, ent, number=f"NOCUR-{tag}", amount="100.00", currency="USD"
    )
    usd_id = await _seed_vendor_and_invoice(
        mk, info.org_id, ent, number=f"NOCUR-USD-{tag}", amount="100.00", currency="USD"
    )
    await _blank_currency(mk, blank_id, blank)

    async with realdb.client(key="a", role="admin") as c:
        alone = await c.post("/api/payments/runs", json={"items": [{"invoice_id": blank_id}]})
        batched = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": blank_id}, {"invoice_id": usd_id}]},
        )
    for resp in (alone, batched):
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail.startswith("currency_unknown:"), detail
        assert f"NOCUR-{tag}" in detail
        assert f"NOCUR-USD-{tag}" not in detail
    assert await _payment_rows(mk, [blank_id, usd_id]) == []


async def test_standalone_payment_refuses_an_invoice_with_no_currency(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s, info.org_id)
    inv_id = await _seed_vendor_and_invoice(
        mk, info.org_id, ent, number="NOCUR-SOLO", amount="100.00", currency="USD"
    )
    await _blank_currency(mk, inv_id)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments", json={"invoice_id": inv_id, "method": "ach"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"].startswith("currency_unknown:")
    assert "NOCUR-SOLO" in resp.json()["detail"]
    assert await _payment_rows(mk, [inv_id]) == []


async def test_queue_marks_a_currency_less_invoice_blocked_with_a_code(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s, info.org_id)
    inv_id = await _seed_vendor_and_invoice(
        mk, info.org_id, ent, number="NOCUR-QUEUE", amount="100.00", currency="USD"
    )
    await _blank_currency(mk, inv_id, " ")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/payments/queue")
        ids = await c.get("/api/payments/queue/ids")
    assert resp.status_code == 200, resp.text
    row = next(i for i in resp.json()["items"] if i["id"] == inv_id)
    assert row["blocked"] is True
    assert row["blocked_reason"] == "currency_unknown"
    assert row["required_method"] is None
    assert inv_id not in ids.json()["ids"]


async def test_currency_blanked_after_the_run_is_built_stops_dispatch(realdb):
    """The window the builder cannot close: a draft run waits for sign-off and
    the invoice PATCH blanks the currency meanwhile. Dispatch refuses BEFORE the
    adapter call with a retry-safe reason, rather than sending the order as USD,
    and `/retry-failed` keeps skipping it until a currency is recorded."""
    from app.models.payment import Payment
    from app.services.payment_runs import is_retry_safe

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s, info.org_id)
    inv_id = await _seed_vendor_and_invoice(
        mk, info.org_id, ent, number="NOCUR-MIDRUN", amount="100.00", currency="USD"
    )

    async with realdb.client(key="a", role="admin") as c:
        run_resp = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": inv_id, "method": "ach"}]}
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]

    await _blank_currency(mk, inv_id)

    async with realdb.client(key="a", role="ap_manager") as c2:
        exec_resp = await c2.post(f"/api/payments/runs/{run_id}/execute")
        assert exec_resp.status_code == 200, exec_resp.text
        assert exec_resp.json()["payments_completed"] == 0

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == inv_id))
        ).scalar_one()
        assert payment.status == "failed"
        assert payment.failure_reason == "currency_unknown"
        assert payment.provider_payment_id is None
        assert is_retry_safe(payment)

    async with realdb.client(key="a", role="ap_manager") as c3:
        retry = await c3.post(f"/api/payments/runs/{run_id}/retry-failed")
    assert retry.status_code == 200, retry.text
    assert retry.json()["payments_retried"] == 0, retry.json()
    assert "currency_unknown" in retry.json()["skip_reasons"], retry.json()


async def test_queue_rollup_keeps_an_unknown_currency_invoice_in_its_own_group(realdb):
    """The payment queue's whole-set rollup (`_payment_queue_rollup`) groups a
    NULL-currency invoice under a `None` key of its own (decisions §473): the
    700.00 is never part of the USD group's 1000.00, and it counts once in
    `unconverted_count`. The reporting total still adds its face value — the
    flagged fallback every spend rollup makes — which is why the count is there."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s, info.org_id)
    usd_id = await _seed_vendor_and_invoice(
        mk, info.org_id, ent, number="GRP-USD", amount="1000.00", currency="USD"
    )
    unknown_id = await _seed_vendor_and_invoice(
        mk, info.org_id, ent, number="GRP-NULL", amount="700.00", currency="USD"
    )
    await _blank_currency(mk, unknown_id, None)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/payments/queue")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    groups = {g["currency"]: g for g in body["by_currency"]}
    assert set(groups) == {"USD", None}
    assert groups["USD"]["count"] == 1
    assert Decimal(groups["USD"]["total_amount"]) == Decimal("1000.00")
    assert groups[None]["count"] == 1
    assert Decimal(groups[None]["total_amount"]) == Decimal("700.00")
    # The unknown group sorts last.
    assert [g["currency"] for g in body["by_currency"]] == ["USD", None]
    assert body["unconverted_count"] == 1
    assert body["currency"] == "USD"
    assert Decimal(body["total_amount"]) == Decimal("1700.00")
    ids = {row["id"] for row in body["items"]}
    assert {usd_id, unknown_id} <= ids
