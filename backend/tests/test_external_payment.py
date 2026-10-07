"""Recording a payment made OUTSIDE FeohLedger — the no-rail pilot's path to `paid`.

`POST /api/payments/record-outside` and `POST /api/payments/runs/{id}/record-outside`,
over a real DB (issue #517, `docs/decisions.md` §251). What is pinned:

* the record is a `completed` payment with `provider = "external"`, the invoice
  reaches `paid` through `payment_scheduled`, and append-only audit rows land;
* it is **idempotent** — a replay returns the existing payment and writes nothing;
* the controls: the granular permission, segregation of duties against the
  invoice's implicated actors (and its per-org opt-out), the payment-blocking
  exception gate, the amount binding, a future date, the card rail;
* a pending standalone booking FeohLedger never dispatched is completed in place
  rather than left holding the live-payment slot;
* the downstream readers treat it as a rail-settled payment: the 1099 YTD
  aggregate counts it and bank reconciliation matches it by reference;
* the run-level record: maker-checker, all-or-nothing, run rolls up `completed`.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.services.external_payment import EXTERNAL_PAYMENT_PROVIDER
from app.utils.dates import utc_today

pytestmark = pytest.mark.asyncio


async def _seed_invoice(
    mk,
    org_id,
    *,
    number: str,
    amount: str = "250.00",
    status: InvoiceStatus = InvoiceStatus.approved,
    uploaded_by_id: uuid.UUID | None = None,
    vendor_id: uuid.UUID | None = None,
) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=number,
                vendor_name="Outside Pay Vendor",
                vendor_id=vendor_id,
                amount=Decimal(amount),
                currency="USD",
                status=status,
                uploaded_by_id=uploaded_by_id,
            )
        )
        await s.commit()
    return inv_id


async def _set_payments_settings(realdb, *, org_id, **values) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, org_id)
        settings = dict(org.settings or {})
        payments = dict(settings.get("payments") or {})
        payments.update(values)
        settings["payments"] = payments
        org.settings = settings
        await s.commit()


async def _payments_for(mk, invoice_id) -> list[Payment]:
    async with mk() as s:
        return list(
            (await s.execute(select(Payment).where(Payment.invoice_id == invoice_id)))
            .scalars()
            .all()
        )


async def _invoice(mk, invoice_id) -> Invoice:
    async with mk() as s:
        return await s.get(Invoice, invoice_id)


def _body(invoice_id, **over) -> dict:
    body = {
        "invoice_id": str(invoice_id),
        "method": "check",
        "reference": "CHK-10042",
        "paid_on": utc_today().isoformat(),
    }
    body.update(over)
    return body


async def test_records_a_completed_external_payment_and_walks_the_invoice_to_paid(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, number="EXT-HAPPY-1")
    paid_on = utc_today() - timedelta(days=2)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/payments/record-outside", json=_body(inv_id, paid_on=paid_on.isoformat())
        )
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["status"] == "completed"
    assert out["provider"] == EXTERNAL_PAYMENT_PROVIDER

    [payment] = await _payments_for(mk, inv_id)
    assert payment.status == "completed"
    assert payment.provider == EXTERNAL_PAYMENT_PROVIDER
    assert payment.amount == Decimal("250.00")
    assert payment.method == "check"
    assert payment.reference == "CHK-10042"
    assert payment.settled_amount == Decimal("250.00")
    assert payment.settled_currency == "USD"
    # Booked on the paid-on date, at midnight UTC — the date the 1099 reads.
    assert payment.completed_at.date() == paid_on
    assert payment.submitted_at == payment.completed_at
    assert (await _invoice(mk, inv_id)).status == InvoiceStatus.paid

    async with mk() as s:
        actions = set(
            (
                await s.execute(
                    select(AuditLog.action).where(AuditLog.entity_id.in_([payment.id, inv_id]))
                )
            )
            .scalars()
            .all()
        )
    assert "payment.recorded_outside" in actions
    assert "invoice.payment_scheduled" in actions
    assert "invoice.paid_outside" in actions


async def test_replaying_the_same_record_is_idempotent(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, number="EXT-IDEM-1")

    async with realdb.client(key="a", role="admin") as c:
        first = await c.post("/api/payments/record-outside", json=_body(inv_id))
        second = await c.post("/api/payments/record-outside", json=_body(inv_id))
    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert len(await _payments_for(mk, inv_id)) == 1

    # A DIFFERENT record against the now-paid invoice is a refusal, not a replay.
    async with realdb.client(key="a", role="admin") as c:
        other = await c.post(
            "/api/payments/record-outside", json=_body(inv_id, reference="CHK-OTHER")
        )
    assert other.status_code == 409, other.text
    assert other.json()["detail"]["code"] == "external_payment_not_payable"


async def test_a_clerk_without_the_permission_is_refused(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, number="EXT-PERM-1")

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post("/api/payments/record-outside", json=_body(inv_id))
    assert resp.status_code == 403, resp.text
    assert await _payments_for(mk, inv_id) == []


async def test_the_invoices_creator_may_not_record_its_payment(realdb):
    """Segregation of duties, the approval rule: the uploader is implicated."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(
        mk, info.org_id, number="EXT-SOD-1", uploaded_by_id=info.users["ap_manager"]
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/payments/record-outside", json=_body(inv_id))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "external_payment_segregation"
    assert await _payments_for(mk, inv_id) == []
    assert (await _invoice(mk, inv_id)).status == InvoiceStatus.approved

    # Another user records it.
    async with realdb.client(key="a", role="admin") as c:
        ok = await c.post("/api/payments/record-outside", json=_body(inv_id))
    assert ok.status_code == 201, ok.text


async def test_single_operator_opt_out_lifts_the_segregation_refusal(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    await _set_payments_settings(realdb, org_id=info.org_id, require_run_segregation=False)
    inv_id = await _seed_invoice(
        mk, info.org_id, number="EXT-SOD-OPTOUT", uploaded_by_id=info.users["admin"]
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments/record-outside", json=_body(inv_id))
    assert resp.status_code == 201, resp.text


async def test_an_open_payment_blocking_exception_refuses(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, number="EXT-BLOCK-1")
    async with mk() as s:
        s.add(
            APException(
                id=uuid.uuid4(),
                organization_id=info.org_id,
                invoice_id=inv_id,
                exception_type="duplicate",
                severity="error",
                description="seeded by test",
                status="open",
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments/record-outside", json=_body(inv_id))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "external_payment_blocking_exception"
    assert detail["params"]["exception_type"] == "duplicate"
    assert await _payments_for(mk, inv_id) == []


@pytest.mark.parametrize(
    ("over", "status_code"),
    [
        ({"amount": "249.99"}, 422),  # not what the invoice owes
        ({"paid_on": (utc_today() + timedelta(days=3)).isoformat()}, 422),
        ({"method": "virtual_card"}, 422),
        ({"reference": "   "}, 422),
    ],
)
async def test_bad_figures_are_refused(realdb, over, status_code):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, number=f"EXT-BAD-{uuid.uuid4().hex[:6]}")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments/record-outside", json=_body(inv_id, **over))
    assert resp.status_code == status_code, resp.text
    assert await _payments_for(mk, inv_id) == []


async def test_the_matching_amount_is_accepted(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, number="EXT-AMOUNT-OK")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments/record-outside", json=_body(inv_id, amount="250.00"))
    assert resp.status_code == 201, resp.text


async def test_an_undispatched_standalone_booking_is_completed_in_place(realdb):
    """A `pending` payment FeohLedger booked but never sent would otherwise hold
    the invoice's live-payment slot forever."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, number="EXT-CONVERT-1")
    pay_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                amount=Decimal("250.00"),
                method="ach",
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments/record-outside", json=_body(inv_id))
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == str(pay_id)
    [payment] = await _payments_for(mk, inv_id)
    assert payment.status == "completed"
    assert payment.provider == EXTERNAL_PAYMENT_PROVIDER
    assert payment.method == "check"


async def test_a_dispatched_payment_refuses(realdb):
    """Money FeohLedger already handed to a rail — recording a second payment
    would be a double pay on the books."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(
        mk, info.org_id, number="EXT-LIVE-1", status=InvoiceStatus.payment_scheduled
    )
    async with mk() as s:
        s.add(
            Payment(
                invoice_id=inv_id,
                amount=Decimal("250.00"),
                status="submitted",
                provider="mock",
                provider_payment_id="mock_123",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments/record-outside", json=_body(inv_id))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "external_payment_payment_live"


async def test_feeds_the_1099_aggregate_and_bank_reconciliation(realdb):
    from app.services.bank_reconciliation import match_statement_transactions
    from app.services.tax_1099 import build_1099_report

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    vendor_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=info.org_id,
                name="Outside Pay 1099 Vendor",
                status="active",
                is_1099_eligible=True,
            )
        )
        await s.commit()
    inv_id = await _seed_invoice(
        mk, info.org_id, number="EXT-1099-1", amount="1200.00", vendor_id=vendor_id
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/record-outside",
            json=_body(inv_id, method="ach", reference="BANK-REF-7781"),
        )
    assert resp.status_code == 201, resp.text

    async with mk() as s:
        report = await build_1099_report(s, info.org_id, utc_today().year)
    [row] = [r for r in report.rows if r.vendor_id == vendor_id]
    assert row.ytd_paid == Decimal("1200.00")

    tx = SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=info.org_id,
        transaction_date=utc_today(),
        amount=Decimal("1200.00"),
        currency="USD",
        direction="debit",
        reference="BANK-REF-7781",
        counterparty_name=None,
        description=None,
        matched_payment_id=None,
        match_method=None,
        match_confidence=None,
        matched_at=None,
    )
    async with mk() as s:
        counts = await match_statement_transactions(s, [tx])
    assert counts["matched"] == 1, counts
    assert str(tx.matched_payment_id) == resp.json()["id"]


# ── Run-level record ────────────────────────────────────────────────────


async def _draft_run(realdb, *, creator_role: str, numbers: list[str]) -> tuple[str, list]:
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    ids = [await _seed_invoice(mk, info.org_id, number=n) for n in numbers]
    async with realdb.client(key="a", role=creator_role) as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(i), "method": "ach"} for i in ids]},
        )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], ids


async def test_a_draft_run_is_recorded_as_paid_outside(realdb):
    mk = realdb.sessionmaker("a")
    run_id, ids = await _draft_run(
        realdb, creator_role="ap_manager", numbers=["EXT-RUN-1A", "EXT-RUN-1B"]
    )

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/payments/runs/{run_id}/record-outside",
            json={"reference": "NACHA-BATCH-1", "paid_on": utc_today().isoformat()},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"
    assert resp.json()["payment_count"] == 2

    for inv_id in ids:
        [payment] = await _payments_for(mk, inv_id)
        assert payment.status == "completed"
        assert payment.provider == EXTERNAL_PAYMENT_PROVIDER
        assert payment.method == "ach"  # the run's own rail is kept
        assert (await _invoice(mk, inv_id)).status == InvoiceStatus.paid
    async with mk() as s:
        run = await s.get(PaymentRun, uuid.UUID(run_id))
    assert run.status == "completed"


async def test_the_runs_creator_may_not_record_it(realdb):
    run_id, _ = await _draft_run(realdb, creator_role="admin", numbers=["EXT-RUN-MC"])

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/payments/runs/{run_id}/record-outside",
            json={"reference": "NACHA-BATCH-2", "paid_on": utc_today().isoformat()},
        )
    assert resp.status_code == 403, resp.text


async def test_one_refused_invoice_refuses_the_whole_run(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    run_id, ids = await _draft_run(
        realdb, creator_role="ap_manager", numbers=["EXT-RUN-AON-A", "EXT-RUN-AON-B"]
    )
    # A flag raised AFTER the run was staged.
    async with mk() as s:
        s.add(
            APException(
                id=uuid.uuid4(),
                organization_id=info.org_id,
                invoice_id=ids[1],
                exception_type="fraud_flag",
                severity="error",
                description="seeded by test",
                status="open",
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/payments/runs/{run_id}/record-outside",
            json={"reference": "NACHA-BATCH-3", "paid_on": utc_today().isoformat()},
        )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "external_payment_blocking_exception"
    assert detail["params"]["invoice_number"] == "EXT-RUN-AON-B"

    # Nothing was recorded — not even the clean first invoice.
    for inv_id in ids:
        [payment] = await _payments_for(mk, inv_id)
        assert payment.status == "pending"
        assert (await _invoice(mk, inv_id)).status == InvoiceStatus.approved
    async with mk() as s:
        assert (await s.get(PaymentRun, uuid.UUID(run_id))).status == "draft"


async def test_an_invoice_inside_a_draft_run_points_at_the_run(realdb):
    mk = realdb.sessionmaker("a")
    run_id, ids = await _draft_run(realdb, creator_role="ap_manager", numbers=["EXT-IN-RUN"])

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments/record-outside", json=_body(ids[0]))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "external_payment_in_run"
    assert detail["params"]["payment_run_id"] == run_id
    [payment] = await _payments_for(mk, ids[0])
    assert payment.status == "pending"


async def test_an_exported_run_is_recorded_and_a_changed_amount_refuses(realdb):
    """The NACHA flow's close-out: `exported` is recordable. And a run whose
    invoice now owes a different figure than was staged refuses rather than
    book the staged amount over a credit."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    run_id, ids = await _draft_run(
        realdb, creator_role="ap_manager", numbers=["EXT-EXP-A", "EXT-EXP-B"]
    )
    async with mk() as s:
        (await s.get(PaymentRun, uuid.UUID(run_id))).status = "exported"
        (await s.get(Invoice, ids[1])).amount = Decimal("200.00")  # re-priced
        await s.commit()

    body = {"reference": "NACHA-EXP-1", "paid_on": utc_today().isoformat()}
    async with realdb.client(key="a", role="admin") as c:
        refused = await c.post(f"/api/payments/runs/{run_id}/record-outside", json=body)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "external_payment_amount_changed"
    assert detail["params"]["invoice_number"] == "EXT-EXP-B"
    assert detail["params"]["staged_amount"] == "250.00"
    for inv_id in ids:
        assert (await _invoice(mk, inv_id)).status == InvoiceStatus.approved

    async with mk() as s:
        (await s.get(Invoice, ids[1])).amount = Decimal("250.00")
        await s.commit()
    async with realdb.client(key="a", role="admin") as c:
        ok = await c.post(f"/api/payments/runs/{run_id}/record-outside", json=body)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "completed"
    _ = info


async def test_a_backdated_record_does_not_take_a_discount_accepted_later(realdb):
    """A discount is the price of paying early AFTER accepting the offer; a
    paid-on date before the acceptance owes the full amount, even though the
    offer's window covers it."""
    from datetime import UTC, datetime

    from app.models.discount import DiscountOffer
    from app.services.payment_runs import payable_amount

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, number="EXT-DISC-1", amount="1000.00")
    paid_on = utc_today() - timedelta(days=3)
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        s.add(
            DiscountOffer(
                organization_id=info.org_id,
                entity_id=inv.entity_id,
                scope="invoice",
                invoice_id=inv_id,
                base_amount=Decimal("1000.00"),
                currency="USD",
                tiers=[{"days": 10, "percent": "2.00"}],
                status="accepted",
                accepted_tier={"days": 10, "percent": "2.00"},
                valid_from=utc_today() - timedelta(days=5),
                accepted_at=datetime.now(UTC),  # accepted today, after `paid_on`
            )
        )
        await s.commit()
        # Control: the window alone WOULD discount a payment on `paid_on`.
        assert (await payable_amount(s, inv, pay_date=paid_on)).discount is not None

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/record-outside", json=_body(inv_id, paid_on=paid_on.isoformat())
        )
    assert resp.status_code == 201, resp.text
    [payment] = await _payments_for(mk, inv_id)
    assert payment.amount == Decimal("1000.00")
    assert payment.discount_offer_id is None
