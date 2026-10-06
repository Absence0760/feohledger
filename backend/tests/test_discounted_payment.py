"""An accepted early-payment discount changes what is PAID, not just what is
reported.

Accepting a `DiscountOffer` is the buyer agreeing to pay the discounted amount
before the deadline. Until migration 0104 nothing in the payment path read the
offer: a run paid the invoice net of credit memos, and the only way to pay the
discounted figure was to record a credit memo for the savings by hand.

`payment_runs.payable_amounts` is now the one answer every money path books
and re-checks against — the run builder, the standalone `POST /api/payments`,
dispatch (re-asked on the day the money moves) and `/retry-failed`. Pinned
here, against real Postgres rows and the real HTTP surface:

* the run, the standalone payment and the queue all carry the discounted
  figure, exactly (Decimal), with the offer linked on the payment and a
  `discount_offer.applied` audit row;
* the settlement captures exactly the booked offer, stamped with the payment,
  and a void reverses exactly that capture;
* a deadline that passes while the run waits fails the payment retry-safe as
  `discount_changed` before any adapter call — never a silent re-price — and
  `/retry-failed` will not re-send it;
* a discount already taken through a credit memo (the old way) is not
  deducted a second time;
* an offer that cannot be honoured (expired, other currency, base amount no
  longer the invoice's, vendor-scoped) pays the invoice in full.

Runs against the opt-in `realdb` fixture.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.credit_memo import CreditMemo
from app.models.discount import DiscountOffer
from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.utils.dates import utc_today

TENANT = "a"


async def _default_entity_id(s):
    return (
        await s.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
    ).scalar_one()


async def _seed_invoice(mk, org_id, *, amount="1037.00", currency="USD") -> tuple[str, str]:
    """An approved invoice from an active vendor. Not a round figure, so the
    round-amount fraud rule never puts a payment-blocking flag on it."""
    async with mk() as s:
        entity_id = await _default_entity_id(s)
        vendor = Vendor(organization_id=org_id, name="Discount Pay Vendor", entity_id=entity_id)
        s.add(vendor)
        await s.flush()
        inv = Invoice(
            organization_id=org_id,
            entity_id=entity_id,
            invoice_number=f"DISC-PAY-{uuid.uuid4().hex[:8]}",
            vendor_name=vendor.name,
            vendor_id=vendor.id,
            amount=Decimal(amount),
            currency=currency,
            due_date=utc_today() + timedelta(days=30),
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        return str(vendor.id), str(inv.id)


async def _accepted_offer(c, invoice_id: str, *, percent: str = "2.00", days: int = 10) -> str:
    resp = await c.post(
        "/api/discounts/offers",
        json={
            "scope": "invoice",
            "invoice_id": invoice_id,
            "tiers": [{"days": days, "percent": percent}],
        },
    )
    assert resp.status_code == 201, resp.text
    offer_id = resp.json()["id"]
    accept = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={})
    assert accept.status_code == 200, accept.text
    return offer_id


async def _backdate(mk, offer_id: str, days: int) -> None:
    """Move an offer's reference date into the past, so its tier deadline is."""
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        offer.valid_from = utc_today() - timedelta(days=days)
        await s.commit()


async def _create_run(realdb, invoice_id: str) -> dict:
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _execute(realdb, run_id: str) -> dict:
    # Run segregation: a different user from the creator executes.
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _payment(mk, invoice_id: str) -> Payment:
    async with mk() as s:
        return (
            (
                await s.execute(
                    select(Payment)
                    .where(Payment.invoice_id == uuid.UUID(invoice_id))
                    .order_by(Payment.created_at)
                )
            )
            .scalars()
            .all()
        )[-1]


# --------------------------------------------------------------------------- #
# The run pays the discounted figure, end to end
# --------------------------------------------------------------------------- #


async def test_run_books_executes_and_captures_the_discounted_amount(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)

    # 2 % of 1,037.00 = 20.74 exactly (half-up to the cent).
    run = await _create_run(realdb, invoice_id)
    assert run["total_amount"] == "1016.26"
    assert run["discount_total"] == "20.74"

    payment = await _payment(mk, invoice_id)
    assert payment.amount == Decimal("1016.26")
    assert payment.discount_amount == Decimal("20.74")
    assert str(payment.discount_offer_id) == offer_id

    async with mk() as s:
        applied = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "discount_offer.applied",
                    AuditLog.entity_id == uuid.UUID(offer_id),
                )
            )
        ).scalar_one()
    assert applied.details == {
        "invoice_id": invoice_id,
        "payment_id": str(payment.id),
        "payment_run_id": run["id"],
        "invoice_amount": "1037.00",
        "net_before_discount": "1037.00",
        "discount_amount": "20.74",
        "payment_amount": "1016.26",
        "pay_by": applied.details["pay_by"],
    }
    assert date.fromisoformat(applied.details["pay_by"]) >= utc_today()

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        detail = (await c.get(f"/api/payments/runs/{run['id']}")).json()
    assert detail["discount_total"] == "20.74"
    (row,) = detail["payments"]
    assert row["amount"] == "1016.26"
    assert row["discount_amount"] == "20.74"
    assert row["invoice_amount"] == "1037.00"

    result = await _execute(realdb, run["id"])
    assert result["payments_completed"] == 1

    payment = await _payment(mk, invoice_id)
    assert payment.status == "completed"
    assert payment.amount == Decimal("1016.26")
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.status == "captured"
        assert offer.captured_amount == Decimal("20.74")
        assert offer.captured_by_payment_id == payment.id


async def test_the_queue_shows_what_a_run_would_pay(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await _accepted_offer(c, invoice_id)
        items = (await c.get("/api/payments/queue", params={"page_size": 100})).json()["items"]
    (row,) = [i for i in items if i["id"] == invoice_id]
    assert row["amount"] == "1037.00"  # the invoice, unchanged
    assert row["accepted_discount_amount"] == "20.74"
    assert row["payable_amount"] == "1016.26"
    assert date.fromisoformat(row["accepted_discount_pay_by"]) >= utc_today()


async def test_an_undiscounted_queue_row_pays_its_net(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        items = (await c.get("/api/payments/queue", params={"page_size": 100})).json()["items"]
    (row,) = [i for i in items if i["id"] == invoice_id]
    assert row["accepted_discount_amount"] is None
    assert row["accepted_discount_pay_by"] is None
    assert row["payable_amount"] == "1037.00"


# --------------------------------------------------------------------------- #
# The standalone payment takes it the same way
# --------------------------------------------------------------------------- #


async def test_standalone_payment_pays_the_discounted_amount_and_refuses_the_gross(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        gross = await c.post(
            "/api/payments",
            json={"invoice_id": invoice_id, "amount": "1037.00", "method": "ach"},
        )
        assert gross.status_code == 422, gross.text
        net = await c.post(
            "/api/payments",
            json={"invoice_id": invoice_id, "amount": "1016.26", "method": "ach"},
        )
    assert net.status_code == 201, net.text
    body = net.json()
    assert body["discount_offer_id"] == offer_id
    assert Decimal(str(body["discount_amount"])) == Decimal("20.74")

    payment = await _payment(mk, invoice_id)
    assert payment.amount == Decimal("1016.26")
    assert payment.discount_amount == Decimal("20.74")
    async with mk() as s:
        applied = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "discount_offer.applied",
                    AuditLog.entity_id == uuid.UUID(offer_id),
                )
            )
        ).scalar_one()
    assert applied.details["payment_run_id"] is None
    assert applied.details["payment_amount"] == "1016.26"


# --------------------------------------------------------------------------- #
# The deadline is re-checked on the day the money moves
# --------------------------------------------------------------------------- #


async def test_a_deadline_missed_while_the_run_waited_refuses_dispatch_retry_safe(realdb):
    """Booked at the discount, then the tier's deadline passes before execute
    (a draft waiting for CFO sign-off). Moving the discounted figure would
    short-pay a supplier who considers the offer dead; moving the full figure
    would be money nobody approved. Neither: `discount_changed`, before the
    adapter is called, and `/retry-failed` keeps skipping it."""
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
    run = await _create_run(realdb, invoice_id)
    assert run["total_amount"] == "1016.26"

    await _backdate(mk, offer_id, days=30)

    from app.services.payment_adapters.mock_adapter import MockPaymentAdapter

    with patch.object(MockPaymentAdapter, "create_payment") as create:
        result = await _execute(realdb, run["id"])
    create.assert_not_called()
    assert result["payments_completed"] == 0

    payment = await _payment(mk, invoice_id)
    assert payment.status == "failed"
    assert payment.failure_reason == "discount_changed"
    async with mk() as s:
        assert (await s.get(DiscountOffer, uuid.UUID(offer_id))).status == "accepted"
        assert (await s.get(Invoice, uuid.UUID(invoice_id))).status == InvoiceStatus.approved

    async with realdb.client(key=TENANT, role="admin") as c:
        retry = await c.post(f"/api/payments/runs/{run['id']}/retry-failed")
    assert retry.status_code == 200, retry.text
    assert retry.json()["payments_retried"] == 0
    assert retry.json()["skip_reasons"] == ["discount_changed"]

    # A fresh run books what is owed now: the full amount, no offer. (A
    # `failed` payment is terminal, so it claims nothing.)
    fresh = await _create_run(realdb, invoice_id)
    assert fresh["total_amount"] == "1037.00"
    assert fresh["discount_total"] == "0"


async def test_an_offer_accepted_after_booking_is_not_paid_past(realdb):
    """The run was booked (and approved) at the full figure; a discount accepted
    afterwards changes what is owed, so dispatch refuses rather than move a
    figure the supplier has agreed to reduce."""
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    run = await _create_run(realdb, invoice_id)
    assert run["total_amount"] == "1037.00"
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await _accepted_offer(c, invoice_id)

    result = await _execute(realdb, run["id"])
    assert result["payments_completed"] == 0
    payment = await _payment(mk, invoice_id)
    assert payment.failure_reason == "discount_changed"


async def test_an_offer_already_expired_at_booking_pays_in_full(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
    await _backdate(mk, offer_id, days=30)

    run = await _create_run(realdb, invoice_id)
    assert run["total_amount"] == "1037.00"
    payment = await _payment(mk, invoice_id)
    assert payment.discount_offer_id is None
    assert payment.discount_amount is None

    await _execute(realdb, run["id"])
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.status == "accepted"  # nothing was saved, nothing captured


# --------------------------------------------------------------------------- #
# Void reverses exactly its own capture
# --------------------------------------------------------------------------- #


async def test_voiding_a_discounted_payment_reverses_its_own_capture(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
    run = await _create_run(realdb, invoice_id)
    await _execute(realdb, run["id"])
    payment = await _payment(mk, invoice_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/payments/{payment.id}/void", json={"reason": "Re-paying."})
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.status == "accepted"
        assert offer.captured_amount is None
        assert offer.captured_by_payment_id is None
        audit = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "discount_offer.capture_reversed",
                    AuditLog.entity_id == uuid.UUID(offer_id),
                )
            )
        ).scalar_one()
        assert audit.details["reversed_amount"] == "20.74"


# --------------------------------------------------------------------------- #
# Never deducted twice, never deducted when it can't be honoured
# --------------------------------------------------------------------------- #


async def test_a_discount_already_taken_by_credit_memo_is_not_deducted_again(realdb):
    """The documented way to pay a discount before 0104 was an applied credit
    memo for the savings (and the in-app help said so). That memo is already
    in the net, so the offer must not come off a second time (short-paying the
    supplier by the discount); the settlement still captures it through the
    amount match. Whenever the memo was recorded: guessing the other way on an
    amount coincidence would short-pay the supplier."""
    mk = realdb.sessionmaker(TENANT)
    vendor_id, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
        memo = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": f"CM-{uuid.uuid4().hex[:6]}",
                "vendor_id": vendor_id,
                "amount": "20.74",
                "invoice_id": invoice_id,
            },
        )
        assert memo.status_code == 201, memo.text

    run = await _create_run(realdb, invoice_id)
    assert run["total_amount"] == "1016.26"
    payment = await _payment(mk, invoice_id)
    assert payment.discount_offer_id is None

    await _execute(realdb, run["id"])
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.status == "captured"
        assert offer.captured_amount == Decimal("20.74")


@pytest.mark.parametrize("variant", ["currency", "base", "vendor_scope", "no_tier"])
async def test_an_offer_that_cannot_be_honoured_pays_in_full(realdb, variant):
    from app.services.payment_runs import payable_amount

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id, invoice_id = await _seed_invoice(mk, org_id)
    async with mk() as s:
        inv = await s.get(Invoice, uuid.UUID(invoice_id))
        s.add(
            DiscountOffer(
                organization_id=org_id,
                entity_id=inv.entity_id,
                scope="vendor" if variant == "vendor_scope" else "invoice",
                invoice_id=inv.id,
                vendor_id=uuid.UUID(vendor_id),
                base_amount=Decimal("900.00") if variant == "base" else Decimal("1037.00"),
                currency="EUR" if variant == "currency" else "USD",
                tiers=[{"days": 10, "percent": "2.00"}],
                status="accepted",
                accepted_tier=None if variant == "no_tier" else {"days": 10, "percent": "2.00"},
                valid_from=utc_today(),
            )
        )
        await s.commit()
        payable = await payable_amount(s, inv, pay_date=utc_today())
    assert payable.discount is None
    assert payable.amount == Decimal("1037.00")


async def test_a_discount_that_would_consume_the_whole_net_is_not_taken(realdb):
    """Credits already cover all but 10.00; a 20.74 deduction would leave a
    negative payment. The supplier is owed the 10.00."""
    from app.services.payment_runs import payable_amount

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id, invoice_id = await _seed_invoice(mk, org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await _accepted_offer(c, invoice_id)
    async with mk() as s:
        s.add(
            CreditMemo(
                memo_number=f"CM-{uuid.uuid4().hex[:6]}",
                vendor_id=uuid.UUID(vendor_id),
                invoice_id=uuid.UUID(invoice_id),
                amount=Decimal("1027.00"),
                currency="USD",
                status="applied",
                organization_id=org_id,
            )
        )
        await s.commit()
        inv = await s.get(Invoice, uuid.UUID(invoice_id))
        payable = await payable_amount(s, inv, pay_date=utc_today())
    assert payable.discount is None
    assert payable.amount == Decimal("10.00")


async def test_the_deadline_day_itself_still_earns_the_discount(realdb):
    """`pay_date <= deadline`: paying ON the last day is in time."""
    from app.services.discount_offers import accepted_discount_deadline
    from app.services.payment_runs import payable_amount

    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        deadline = accepted_discount_deadline(offer)
        inv = await s.get(Invoice, uuid.UUID(invoice_id))
        on_time = await payable_amount(s, inv, pay_date=deadline)
        late = await payable_amount(s, inv, pay_date=deadline + timedelta(days=1))
    assert on_time.discount_amount == Decimal("20.74")
    assert late.discount is None


# --------------------------------------------------------------------------- #
# Downstream figures read the booked amount
# --------------------------------------------------------------------------- #


async def test_1099_totals_report_the_discounted_amount_paid(realdb):
    """The 1099 box amount is what was actually paid. It sums `Payment.amount`,
    which is now the discounted figure — so the discount is never reported as
    income the vendor did not receive."""
    from app.services.tax_1099 import build_1099_report

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id, invoice_id = await _seed_invoice(mk, org_id)
    async with mk() as s:
        vendor = await s.get(Vendor, uuid.UUID(vendor_id))
        vendor.is_1099_eligible = True
        await s.commit()
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await _accepted_offer(c, invoice_id)
    run = await _create_run(realdb, invoice_id)
    await _execute(realdb, run["id"])

    async with mk() as s:
        report = await build_1099_report(
            s, org_id, utc_today().year, reporting_currency="USD", org_settings={}
        )
    (row,) = [r for r in report.rows if str(r.vendor_id) == vendor_id]
    assert row.ytd_paid == Decimal("1016.26")


async def test_deleting_either_side_of_the_link_sets_it_null(realdb):
    """Migration 0104's `ON DELETE SET NULL`, both directions. The payments that
    are ever deleted are the still-`pending` rows of a cancelled draft run (and
    test cleanup); a RESTRICT would turn that into an FK error. Deleting an
    offer a payment names must not take the payment's money record with it."""
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
    run = await _create_run(realdb, invoice_id)
    await _execute(realdb, run["id"])
    payment = await _payment(mk, invoice_id)

    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.captured_by_payment_id == payment.id
        await s.execute(Payment.__table__.delete().where(Payment.id == payment.id))
        await s.commit()
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer is not None
        assert offer.captured_by_payment_id is None

    _, invoice_id2 = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id2 = await _accepted_offer(c, invoice_id2)
    await _create_run(realdb, invoice_id2)
    async with mk() as s:
        await s.execute(
            DiscountOffer.__table__.delete().where(DiscountOffer.id == uuid.UUID(offer_id2))
        )
        await s.commit()
    payment2 = await _payment(mk, invoice_id2)
    assert payment2.discount_offer_id is None
    assert payment2.discount_amount == Decimal("20.74")
