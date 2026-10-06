"""Coverage for issue #280 — `discount_offers.mark_captured` had zero callers,
so `DiscountOffer.captured_amount` was always NULL and the captured-savings
KPI on `GET /api/discounts/dashboard` always read 0, even when a discount was
genuinely accepted and the invoice paid at the discounted amount.

`services/discount_capture.capture_offers_for_settled_payment` is the new
caller, wired into `app/api/payments.py::_execute_single_payment` (both the
adapter-completed leg and the virtual-card leg) and the async webhook-driven
completion path. It recognizes a settled `Payment` whose amount EXACTLY
equals an `accepted` invoice-scoped offer's discounted payoff
(`base_amount - discount_savings(...)`), AND whose currency matches the
offer's own `currency` (checked first — an invoice-scoped offer's currency
can diverge from its invoice's, see `test_currency_mismatched_offer_...`
below), and calls `mark_captured`.

The realistic way AP actually pays the discounted amount today is via the
existing credit-memo netting `create_payment_run_for_invoices` already does
(see `test_payment_run_credit_memo_netting.py`) — recording a credit memo for
the discount amount nets `Payment.amount` down to exactly the discounted
payoff. This suite drives that same path end to end through
`POST /api/payments/runs` + `.../execute` (mock adapter, synchronous
`completed`) and asserts the offer is captured — plus controls proving a
full-amount settlement and a currency-mismatched offer do NOT falsely
capture, and a direct idempotency check on the service function itself.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models.discount import DiscountOffer
from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.models.workflow import AuditLog


async def _default_entity_id(s):
    return (
        await s.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
    ).scalar_one()


async def _seed_vendor_and_approved_invoice(
    mk, org_id, *, number: str, amount: Decimal
) -> tuple[str, str]:
    async with mk() as s:
        entity_id = await _default_entity_id(s)
        vendor = Vendor(organization_id=org_id, name="Capture Test Vendor", entity_id=entity_id)
        s.add(vendor)
        await s.flush()
        inv = Invoice(
            organization_id=org_id,
            entity_id=vendor.entity_id,
            invoice_number=number,
            vendor_name=vendor.name,
            vendor_id=vendor.id,
            amount=amount,
            currency="USD",
            due_date=date.today() + timedelta(days=30),
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(vendor)
        await s.refresh(inv)
        return str(vendor.id), str(inv.id)


async def test_settlement_at_discounted_payoff_captures_the_offer(realdb):
    """The money-path proof: an accepted 2% offer on a $1000 invoice, paid at
    its $980 discounted payoff (via an applied $20 credit memo netting the
    payment run down to it), is recognized and captured on execute — and the
    dynamic-discounting dashboard reflects the real captured savings."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    org_id = info.org_id
    _, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, org_id, number="DISC-CAP-001", amount=Decimal("1000.00")
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        offer_resp = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "invoice",
                "invoice_id": invoice_id,
                "tiers": [{"days": 10, "percent": "2.00"}],
            },
        )
        assert offer_resp.status_code == 201, offer_resp.text
        offer_id = offer_resp.json()["id"]

        accept_resp = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={})
        assert accept_resp.status_code == 200, accept_resp.text
        assert accept_resp.json()["status"] == "accepted"

        # $1000.00 base * 2% = $20.00 — net it off via a credit memo so the
        # payment run's existing credit-memo-netting math lands the actual
        # `Payment.amount` on the discounted payoff, exactly like a real AP
        # workflow that negotiated the discount into the amount actually paid.
        memo_resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-DISC-CAP-1",
                "vendor_id": (await c.get(f"/api/invoices/{invoice_id}")).json()["vendor_id"],
                "amount": "20.00",
                "invoice_id": invoice_id,
            },
        )
        assert memo_resp.status_code == 201, memo_resp.text

        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_body = run_resp.json()
        assert run_body["total_amount"] == "980.00"
        run_id = run_body["id"]

    # Segregation of duties: the run's own creator can't also execute it —
    # a different user does, mirroring test_payment_compliance_hold.py.
    async with realdb.client(key="a", role="admin") as exec_c:
        exec_resp = await exec_c.post(f"/api/payments/runs/{run_id}/execute")
    assert exec_resp.status_code == 200, exec_resp.text
    assert exec_resp.json()["payments_completed"] == 1

    async with mk() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        assert payment.status == "completed"
        assert payment.amount == Decimal("980.00")

        offer = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == uuid.UUID(offer_id)))
        ).scalar_one()
        assert offer.status == "captured"
        assert offer.captured_amount == Decimal("20.00")
        assert offer.captured_at is not None

        audit = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "discount_offer.captured",
                    AuditLog.entity_id == uuid.UUID(offer_id),
                )
            )
        ).scalar_one()
        assert audit.details["captured_amount"] == "20.00"
        assert audit.details["invoice_id"] == invoice_id

    # The dynamic-discounting dashboard KPI — the exact figure issue #280
    # said always read 0 — now reflects the real captured offer.
    async with realdb.client(key="a", role="ap_manager") as c:
        dash_resp = await c.get("/api/discounts/dashboard")
    assert dash_resp.status_code == 200, dash_resp.text
    dash = dash_resp.json()
    assert dash["captured_count"] >= 1
    assert Decimal(str(dash["captured_amount"])) >= Decimal("20.00")


async def test_settlement_at_full_amount_does_not_falsely_capture(realdb):
    """Control: an accepted offer whose invoice is instead paid at the FULL
    (undiscounted) amount must NOT be captured — a false capture would
    misreport savings that were never actually realized, the same class of
    bug this issue closes, just inverted.

    Since a run takes an accepted, unexpired discount itself
    (`payment_runs.payable_amounts`), the full amount is paid only once the
    offer's deadline has passed — so the offer is back-dated past its 10-day
    tier before the run is built."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    org_id = info.org_id
    _, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, org_id, number="DISC-CAP-002", amount=Decimal("500.00")
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        offer_resp = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "invoice",
                "invoice_id": invoice_id,
                "tiers": [{"days": 10, "percent": "2.00"}],
            },
        )
        offer_id = offer_resp.json()["id"]
        accept_resp = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={})
        assert accept_resp.status_code == 200, accept_resp.text

    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        offer.valid_from = date.today() - timedelta(days=30)
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        # No credit memo, and the deadline has passed — the run pays $500.00.
        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
        assert run_resp.status_code == 201, run_resp.text
        assert run_resp.json()["total_amount"] == "500.00"
        run_id = run_resp.json()["id"]

    async with realdb.client(key="a", role="admin") as exec_c:
        exec_resp = await exec_c.post(f"/api/payments/runs/{run_id}/execute")
    assert exec_resp.status_code == 200, exec_resp.text
    assert exec_resp.json()["payments_completed"] == 1

    async with mk() as s:
        offer = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == uuid.UUID(offer_id)))
        ).scalar_one()
        assert offer.status == "accepted"  # untouched — never captured
        assert offer.captured_amount is None
        assert offer.captured_at is None


async def test_currency_mismatched_offer_does_not_falsely_capture(realdb):
    """A code-review finding on this same fix: `POST /api/discounts/offers`
    lets the caller set an explicit `currency` independent of the invoice
    (`api/discounts.py::create_offer` only falls back to `invoice.currency`
    when the body omits one) — so an invoice-scoped offer's `currency` can
    diverge from its own invoice's. `Payment.amount` is always denominated in
    the INVOICE's currency. Here the offer is minted with `currency: "EUR"`
    on a USD invoice; its `base_amount` still defaults from the invoice's
    bare number (1000.00), so a USD payment landing on the numerically
    identical discounted payoff (980.00) must NOT be treated as proof of a
    real discounted settlement — that would misattribute EUR savings to a
    USD payment purely from a numeric coincidence across currencies."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    org_id = info.org_id
    _, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, org_id, number="DISC-CAP-004", amount=Decimal("1000.00")
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        offer_resp = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "invoice",
                "invoice_id": invoice_id,
                "currency": "EUR",  # diverges from the invoice's USD
                "tiers": [{"days": 10, "percent": "2.00"}],
            },
        )
        assert offer_resp.status_code == 201, offer_resp.text
        assert offer_resp.json()["currency"] == "EUR"
        offer_id = offer_resp.json()["id"]

        accept_resp = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={})
        assert accept_resp.status_code == 200, accept_resp.text

        # Same $20 credit-memo netting as the happy-path test — nets the USD
        # payment down to 980.00, numerically identical to the (EUR-labeled)
        # offer's discounted payoff, but denominated in a different currency.
        memo_resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-DISC-CAP-4",
                "vendor_id": (await c.get(f"/api/invoices/{invoice_id}")).json()["vendor_id"],
                "amount": "20.00",
                "invoice_id": invoice_id,
            },
        )
        assert memo_resp.status_code == 201, memo_resp.text

        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
        assert run_resp.status_code == 201, run_resp.text
        assert run_resp.json()["total_amount"] == "980.00"
        run_id = run_resp.json()["id"]

    async with realdb.client(key="a", role="admin") as exec_c:
        exec_resp = await exec_c.post(f"/api/payments/runs/{run_id}/execute")
    assert exec_resp.status_code == 200, exec_resp.text
    assert exec_resp.json()["payments_completed"] == 1

    async with mk() as s:
        offer = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == uuid.UUID(offer_id)))
        ).scalar_one()
        assert offer.status == "accepted"  # untouched — currency mismatch refused the match
        assert offer.captured_amount is None
        assert offer.captured_at is None


async def test_capture_is_idempotent_on_repeat_settlement(realdb):
    """A repeat call for the same settlement (a retry, or a reconciliation
    re-run touching the same invoice/payment) must not double-count the
    savings or raise on an already-`captured` offer — `mark_captured`'s own
    status guard, backstopped by the service only ever querying `accepted`
    offers in the first place."""
    from app.services.discount_capture import capture_offers_for_settled_payment

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    org_id = info.org_id
    _, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, org_id, number="DISC-CAP-003", amount=Decimal("1000.00")
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        offer_resp = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "invoice",
                "invoice_id": invoice_id,
                "tiers": [{"days": 10, "percent": "2.00"}],
            },
        )
        offer_id = offer_resp.json()["id"]
        accept_resp = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={})
        assert accept_resp.status_code == 200, accept_resp.text

    now = datetime.now(UTC)

    # First settlement recognition: captures the offer.
    async with mk() as s:
        first = await capture_offers_for_settled_payment(
            s,
            invoice_id=uuid.UUID(invoice_id),
            payment_amount=Decimal("980.00"),
            invoice_currency="USD",
            now=now,
        )
        await s.commit()
    assert len(first) == 1
    assert first[0].captured_amount == Decimal("20.00")
    first_captured_at = first[0].captured_at

    # Repeat call — same invoice, same discounted amount, later timestamp —
    # must be a clean no-op: nothing captured, nothing changed.
    async with mk() as s:
        second = await capture_offers_for_settled_payment(
            s,
            invoice_id=uuid.UUID(invoice_id),
            payment_amount=Decimal("980.00"),
            invoice_currency="USD",
            now=datetime.now(UTC),
        )
        await s.commit()
    assert second == []

    async with mk() as s:
        offer = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == uuid.UUID(offer_id)))
        ).scalar_one()
        assert offer.status == "captured"
        assert offer.captured_amount == Decimal("20.00")  # unchanged, not doubled
        assert offer.captured_at == first_captured_at  # unchanged, not re-stamped


# --------------------------------------------------------------------------- #
# One payment realizes at most one discount; a void un-realizes it
# --------------------------------------------------------------------------- #


async def _accepted_offer(c, invoice_id: str, *, percent: str = "2.00") -> str:
    resp = await c.post(
        "/api/discounts/offers",
        json={
            "scope": "invoice",
            "invoice_id": invoice_id,
            "tiers": [{"days": 10, "percent": percent}],
        },
    )
    assert resp.status_code == 201, resp.text
    offer_id = resp.json()["id"]
    accept = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={})
    assert accept.status_code == 200, accept.text
    return offer_id


async def _pay_at_discounted_payoff(realdb, invoice_id: str, *, memo: str) -> str:
    """Net a $1000 invoice to $980 via a $20 credit memo, then run + execute
    it (creator and executor differ — run segregation). Returns the completed
    payment's id."""
    async with realdb.client(key="a", role="ap_manager") as c:
        vendor_id = (await c.get(f"/api/invoices/{invoice_id}")).json()["vendor_id"]
        memo_resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": memo,
                "vendor_id": vendor_id,
                "amount": "20.00",
                "invoice_id": invoice_id,
            },
        )
        assert memo_resp.status_code == 201, memo_resp.text
        run_resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
        assert run_resp.status_code == 201, run_resp.text
        assert run_resp.json()["total_amount"] == "980.00"
        run_id = run_resp.json()["id"]
    async with realdb.client(key="a", role="admin") as exec_c:
        exec_resp = await exec_c.post(f"/api/payments/runs/{run_id}/execute")
    assert exec_resp.status_code == 200, exec_resp.text
    assert exec_resp.json()["payments_completed"] == 1
    async with realdb.sessionmaker("a")() as s:
        payment = (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()
        assert payment.status == "completed"
        return str(payment.id)


async def test_one_settlement_never_captures_two_offers(realdb):
    """Two `accepted` offers with the same tier on one invoice — a supplier's
    re-sent offer, both accepted — share one discounted payoff. ONE payment at
    that payoff realizes ONE discount: the vendor was short-paid $20 once.
    Capturing both booked $40 of savings off a $20 deduction, and the
    dashboard's `captured_amount` reported money that was never saved."""
    mk = realdb.sessionmaker("a")
    _, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, realdb.info("a").org_id, number="DISC-CAP-DUP", amount=Decimal("1000.00")
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        first = await _accepted_offer(c, invoice_id)
        second = await _accepted_offer(c, invoice_id)

    await _pay_at_discounted_payoff(realdb, invoice_id, memo="CM-DISC-DUP")

    async with mk() as s:
        a = await s.get(DiscountOffer, uuid.UUID(first))
        b = await s.get(DiscountOffer, uuid.UUID(second))
        # The earlier acceptance is the one the settlement realizes.
        assert (a.status, b.status) == ("captured", "accepted")
        assert a.captured_amount == Decimal("20.00")
        assert b.captured_amount is None
        captured_rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "discount_offer.captured",
                        AuditLog.entity_id.in_([a.id, b.id]),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(captured_rows) == 1


async def test_voiding_the_settling_payment_reverses_the_capture(realdb):
    """A captured discount is savings realized ON a settlement. Voiding that
    payment returns the invoice to `approved` — nothing was paid, so nothing
    was saved — but the offer used to stay `captured` with its
    `captured_amount`, so the dashboard kept reporting $20 of savings on an
    invoice that is now unpaid, and re-paying it at the discounted payoff
    could capture nothing (the offer was no longer `accepted`)."""
    mk = realdb.sessionmaker("a")
    _, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, realdb.info("a").org_id, number="DISC-CAP-VOID", amount=Decimal("1000.00")
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)

    payment_id = await _pay_at_discounted_payoff(realdb, invoice_id, memo="CM-DISC-VOID")
    async with mk() as s:
        assert (await s.get(DiscountOffer, uuid.UUID(offer_id))).status == "captured"

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/payments/{payment_id}/void",
            json={"reason": "Wrong account; re-paying."},
        )
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.status == "accepted"
        assert offer.captured_amount is None
        assert offer.captured_at is None
        audit = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "discount_offer.capture_reversed",
                    AuditLog.entity_id == uuid.UUID(offer_id),
                )
            )
        ).scalar_one()
        assert audit.details["payment_id"] == payment_id
        assert audit.details["reversed_amount"] == "20.00"
        assert (await s.get(Invoice, uuid.UUID(invoice_id))).status == InvoiceStatus.approved

    async with realdb.client(key="a", role="ap_manager") as c:
        dash = (await c.get("/api/discounts/dashboard")).json()
    assert dash["captured_count"] == 0
    assert Decimal(str(dash["captured_amount"])) == Decimal("0")


async def test_voiding_a_payment_that_never_settled_leaves_captures_alone(realdb):
    """Only a `completed` payment can have realized a discount, so voiding one
    that never settled reverses nothing — the guard that keeps a void of some
    other in-flight payment from un-capturing a discount it did not realize."""
    from app.services.discount_capture import reverse_captures_for_voided_payment

    mk = realdb.sessionmaker("a")
    _, invoice_id = await _seed_vendor_and_approved_invoice(
        mk, realdb.info("a").org_id, number="DISC-CAP-VOID2", amount=Decimal("1000.00")
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
    await _pay_at_discounted_payoff(realdb, invoice_id, memo="CM-DISC-VOID2")

    async with mk() as s:
        reversed_ = await reverse_captures_for_voided_payment(
            s,
            invoice_id=uuid.UUID(invoice_id),
            voided_payment_id=uuid.uuid4(),
            previous_status="processing",
        )
        await s.commit()
    assert reversed_ == []
    async with mk() as s:
        assert (await s.get(DiscountOffer, uuid.UUID(offer_id))).status == "captured"
