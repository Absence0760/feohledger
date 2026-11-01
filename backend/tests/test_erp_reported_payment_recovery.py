"""A payment the ERP reported is kept, and recorded once what blocked it clears.

The production shape this closes (decisions §319): an invoice paid in
QuickBooks while a stale payment-blocking exception was open. The sweep saw the
bill paid, `record_invoice_paid_outside` refused on the blocker, and a second,
`erp_reconciliation` exception told the AP user to call an API path with ids
copied out of its prose. Clearing the blocker recorded nothing, and the
payment's method, reference and date had to be re-keyed by hand.

Pinned here, over real rows:

* the ERP's payment is stored as data on the exception (`erp_report`);
* clearing the blocker records it — through the same gates, in the same
  transaction — and closes the ERP exception with an audit row;
* a blocker still open, or a FeohLedger payment that would pay twice, still
  refuses, and the row is refreshed with the new reason;
* the re-attempt is idempotent;
* the queue's one-click record resolves a BLOCKING ERP row and records in one
  transaction, under both segregation-of-duties rules;
* the payment queue names the blocking exception and carries the ERP's facts;
* any other record of the invoice closes the ERP row too.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun
from app.models.workflow import AuditLog
from app.services.erp_status_sync import retry_erp_reported_payments
from app.services.workflow_engine import get_invoice_for_update

pytestmark = pytest.mark.asyncio

TENANT = "a"
REPORTED_ON = date(2026, 10, 20)


async def _seed_invoice(mk, org_id, *, number, uploaded_by_id=None) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=number,
                vendor_name="ERP Recovery Vendor",
                amount=Decimal("410.00"),
                currency="USD",
                status=InvoiceStatus.posted_in_erp,
                uploaded_by_id=uploaded_by_id,
            )
        )
        await s.commit()
    return inv_id


async def _blocker(mk, org_id, inv_id, *, exception_type="payment_reconciliation") -> uuid.UUID:
    exc_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            APException(
                id=exc_id,
                organization_id=org_id,
                invoice_id=inv_id,
                exception_type=exception_type,
                severity="error",
                description="stale flag seeded by test",
                status="open",
            )
        )
        await s.commit()
    return exc_id


async def _report_paid(realdb, inv_id, *, method="check", paid_on=REPORTED_ON):
    """The ERP reports the bill paid, by cheque, on a stated date."""
    from app.services.erp_status_sync import record_erp_reported_payment

    async with realdb.control_sessionmaker()() as c:
        org = await c.get(Organization, realdb.info(TENANT).org_id)
    async with realdb.sessionmaker(TENANT)() as s:
        invoice = await get_invoice_for_update(s, inv_id)
        recorded = await record_erp_reported_payment(
            s,
            invoice,
            org=org,
            erp_type="quickbooks_online",
            erp_document_id="BILL-145",
            event_id="evt-145",
            paid_on=paid_on,
            method=method,
        )
        await s.commit()
    return recorded


async def _exceptions(mk, inv_id) -> list[APException]:
    async with mk() as s:
        return list(
            (
                await s.execute(
                    select(APException)
                    .where(APException.invoice_id == inv_id)
                    .order_by(APException.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _erp_row(mk, inv_id) -> APException:
    [row] = [e for e in await _exceptions(mk, inv_id) if e.erp_report]
    return row


async def _payments(mk, inv_id) -> list[Payment]:
    async with mk() as s:
        return list(
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )


async def _invoice(mk, inv_id) -> Invoice:
    async with mk() as s:
        return await s.get(Invoice, inv_id)


async def _dismiss(realdb, exc_id, *, role="admin"):
    async with realdb.client(key=TENANT, role=role) as c:
        return await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "dismiss", "resolution": "Stale flag; the bill was paid by cheque."},
        )


# ---------------------------------------------------------------------------
# The facts are kept
# ---------------------------------------------------------------------------


async def test_a_refused_report_keeps_the_erp_payment_as_data(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="ERP-KEEP-1")
    await _blocker(mk, info.org_id, inv_id)

    assert await _report_paid(realdb, inv_id) is False

    row = await _erp_row(mk, inv_id)
    assert row.exception_type == "erp_reconciliation"
    assert row.erp_report == {
        "kind": "paid_not_recorded",
        "erp_type": "quickbooks_online",
        "erp_document_id": "BILL-145",
        "event_id": "evt-145",
        "method": "check",
        "reference": "BILL-145",
        "paid_on": "2026-10-20",
        "paid_on_reported": True,
        "refusal_code": "external_payment_blocking_exception",
        "reported_at": row.erp_report["reported_at"],
    }
    assert row.description.startswith("QuickBooks Online reports this bill paid")
    assert "/api/" not in row.description
    # The product name is a param; the sentence is localized on the code.
    assert row.description_code == "exception.erp_paid_unrecorded_blocking_exception"
    assert row.description_params == {"erp": "QuickBooks Online"}


async def test_a_later_report_refreshes_the_row_instead_of_dropping_its_facts(realdb):
    """A second report while the row is open refreshes it with the latest facts
    — never a second row, never the first report's stale ones."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="ERP-KEEP-2")
    await _blocker(mk, info.org_id, inv_id)

    await _report_paid(realdb, inv_id, method=None)
    await _report_paid(realdb, inv_id, method="wire", paid_on=date(2026, 10, 21))

    row = await _erp_row(mk, inv_id)
    assert row.erp_report["method"] == "wire"
    assert row.erp_report["paid_on"] == "2026-10-21"
    first_reported_at = row.erp_report["first_reported_at"]
    assert first_reported_at


async def test_an_undated_later_report_never_replaces_a_stated_date(realdb):
    """The payment is the one the ERP first reported unless it now says more:
    an undated report keeps the stated date (it would otherwise become today)."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="ERP-KEEP-3")
    await _blocker(mk, info.org_id, inv_id)

    await _report_paid(realdb, inv_id)  # dated 2026-10-20 by the ERP
    await _report_paid(realdb, inv_id, paid_on=None)

    row = await _erp_row(mk, inv_id)
    assert row.erp_report["paid_on"] == "2026-10-20"
    assert row.erp_report["paid_on_reported"] is True
    assert row.erp_report["reference"] == "BILL-145"


# ---------------------------------------------------------------------------
# Clearing the blocker records the payment
# ---------------------------------------------------------------------------


async def test_dismissing_the_blocker_records_the_erp_payment(realdb):
    """The production case, end to end over HTTP: dismiss the stale blocker and
    the payment the ERP reported is recorded with ITS rail, reference and date,
    the invoice is paid, and the ERP exception is closed with an audit row."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="ERP-RETRY-1")
    blocker_id = await _blocker(mk, info.org_id, inv_id)
    await _report_paid(realdb, inv_id)
    erp_row = await _erp_row(mk, inv_id)

    res = await _dismiss(realdb, blocker_id)
    assert res.status_code == 200, res.text

    invoice = await _invoice(mk, inv_id)
    [payment] = await _payments(mk, inv_id)
    assert invoice.status == InvoiceStatus.paid
    assert payment.status == "completed"
    assert payment.provider == "external"
    assert payment.method == "check"
    assert payment.reference == "BILL-145"
    assert payment.completed_at.date() == REPORTED_ON
    assert payment.amount == Decimal("410.00")

    closed = await _erp_row(mk, inv_id)
    assert closed.id == erp_row.id
    assert closed.status == "resolved"
    async with mk() as s:
        actions = (
            (
                await s.execute(
                    select(AuditLog.action, AuditLog.details).where(
                        AuditLog.entity_id == erp_row.id
                    )
                )
            )
            .tuples()
            .all()
        )
        invoice_actions = set(
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_id == inv_id)))
            .scalars()
            .all()
        )
    resolved = [d for a, d in actions if a == "exception.resolved"]
    assert len(resolved) == 1 and resolved[0]["via"] == "payment_recorded"
    # Recorded as the ERP's report, not as the person who cleared the blocker.
    assert "invoice.paid_via_erp_report" in invoice_actions


async def test_a_blocker_still_open_keeps_refusing_and_the_reason_is_refreshed(realdb):
    """Two blockers: clearing one re-attempts and is refused by the other —
    nothing is bypassed — and clearing the second records it."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="ERP-RETRY-2")
    first = await _blocker(mk, info.org_id, inv_id)
    second = await _blocker(mk, info.org_id, inv_id, exception_type="duplicate")
    await _report_paid(realdb, inv_id)

    assert (await _dismiss(realdb, first)).status_code == 200
    assert await _payments(mk, inv_id) == []
    row = await _erp_row(mk, inv_id)
    assert row.status == "open"
    assert row.erp_report["refusal_code"] == "external_payment_blocking_exception"

    assert (await _dismiss(realdb, second)).status_code == 200
    assert (await _invoice(mk, inv_id)).status == InvoiceStatus.paid
    assert len(await _payments(mk, inv_id)) == 1


async def test_a_feohledger_payment_in_a_run_still_refuses_the_retry(realdb):
    """The double-pay guard is re-run, not remembered: a run booked for the
    invoice after the first refusal makes the re-attempt a double payment, so
    it is refused and the BLOCKING reconciliation opens to stop the run."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="ERP-RETRY-3")
    blocker_id = await _blocker(mk, info.org_id, inv_id, exception_type="duplicate")
    await _report_paid(realdb, inv_id)
    async with mk() as s:
        run = PaymentRun(
            id=uuid.uuid4(),
            organization_id=info.org_id,
            status="draft",
            total_amount=Decimal("410.00"),
            initiated_by=info.users["ap_manager"],
        )
        s.add(run)
        await s.flush()
        s.add(
            Payment(
                invoice_id=inv_id,
                payment_run_id=run.id,
                amount=Decimal("410.00"),
                method="ach",
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()

    assert (await _dismiss(realdb, blocker_id)).status_code == 200

    assert (await _invoice(mk, inv_id)).status == InvoiceStatus.posted_in_erp
    [payment] = await _payments(mk, inv_id)
    assert payment.status == "pending" and payment.provider != "external"
    # ONE live row holds the ERP's payment: the blocking one that stops the run.
    # The non-blocking row it replaced is closed as superseded, so nothing is
    # left to be re-attempted against the decision someone makes on this one.
    rows = [e for e in await _exceptions(mk, inv_id) if e.erp_report]
    live = [e for e in rows if e.status == "open"]
    assert [e.exception_type for e in live] == ["payment_reconciliation"]
    assert live[0].erp_report["refusal_code"] == "external_payment_in_run"
    assert live[0].erp_report["method"] == "check"
    [superseded] = [e for e in rows if e.status == "resolved"]
    assert superseded.exception_type == "erp_reconciliation"


async def test_the_retry_is_idempotent(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="ERP-RETRY-4")
    blocker_id = await _blocker(mk, info.org_id, inv_id)
    await _report_paid(realdb, inv_id)
    assert (await _dismiss(realdb, blocker_id)).status_code == 200

    # A second dismiss of the same row is refused at the door; a direct
    # re-attempt finds nothing live and records nothing.
    assert (await _dismiss(realdb, blocker_id)).status_code == 409
    async with mk() as s:
        assert (
            await retry_erp_reported_payments(s, invoice_id=inv_id, organization_id=info.org_id)
            is False
        )
        await s.commit()
    assert len(await _payments(mk, inv_id)) == 1


async def test_a_failed_re_attempt_never_undoes_the_clear(realdb, monkeypatch):
    """Our own failure in the re-attempt rolls back only the attempt: the
    person's dismissal stands, nothing is booked, and the ERP row stays open
    with its facts for the one-click record."""
    import app.services.erp_status_sync as sync

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="ERP-RETRY-6")
    blocker_id = await _blocker(mk, info.org_id, inv_id)
    await _report_paid(realdb, inv_id)

    async def _boom(*_a, **_k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(sync, "record_erp_reported_payment", _boom)
    assert (await _dismiss(realdb, blocker_id)).status_code == 200

    async with mk() as s:
        assert (await s.get(APException, blocker_id)).status == "dismissed"
    assert await _payments(mk, inv_id) == []
    row = await _erp_row(mk, inv_id)
    assert row.status == "open" and row.erp_report["method"] == "check"
    # It no longer promises an automatic record that will not come.
    assert row.erp_report["retry_failed_at"]
    assert "record the ERP's payment from this exception" in row.description
    # The refreshed sentence moves its code with it, never English alone.
    assert row.description_code == "exception.erp_paid_retry_failed"
    assert row.description_params == {"erp": "QuickBooks Online"}


async def test_resolving_a_withdrawn_row_records_nothing(realdb):
    """A blocking row whose payment the ERP withdrew is a person's to clear;
    clearing it re-runs the re-attempt, which must find nothing to record."""
    from app.services.erp_status_sync import withdraw_erp_paid_reports

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id, row_id = await _blocking_erp_row(realdb, mk, info, number="ERP-WD-1")
    async with mk() as s:
        invoice = await get_invoice_for_update(s, inv_id)
        assert await withdraw_erp_paid_reports(
            s, invoice, erp_type="quickbooks_online", erp_state="open"
        )
        await s.commit()

    assert (await _dismiss(realdb, row_id)).status_code == 200
    assert (await _invoice(mk, inv_id)).status == InvoiceStatus.posted_in_erp
    assert [p for p in await _payments(mk, inv_id) if p.status == "completed"] == []


async def test_escalating_the_blocker_records_nothing(realdb):
    """Escalation is not a clear — the blocker still stops payment."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="ERP-RETRY-5")
    blocker_id = await _blocker(mk, info.org_id, inv_id)
    await _report_paid(realdb, inv_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{blocker_id}/resolve",
            json={"action": "escalate", "resolution": "Needs the controller."},
        )
    assert res.status_code == 200, res.text
    assert await _payments(mk, inv_id) == []
    assert (await _erp_row(mk, inv_id)).status == "open"


# ---------------------------------------------------------------------------
# Any record of the invoice answers the ERP row
# ---------------------------------------------------------------------------


async def test_a_users_own_record_closes_the_erp_row(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="ERP-CLOSE-1")
    # The blocker is closed behind the lifecycle's back (no re-attempt runs),
    # so the ERP row is still open when a person records the payment by hand
    # from the payment queue, with their own reference.
    blocker_id = await _blocker(mk, info.org_id, inv_id)
    await _report_paid(realdb, inv_id)
    async with mk() as s:
        row = await s.get(APException, blocker_id)
        row.status = "dismissed"
        await s.commit()

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(
            "/api/payments/record-outside",
            json={
                "invoice_id": str(inv_id),
                "method": "check",
                "reference": "CHQ-5521",
                "paid_on": "2026-10-20",
            },
        )
    assert res.status_code == 201, res.text
    erp_row = await _erp_row(mk, inv_id)
    assert erp_row.status == "resolved"
    assert erp_row.resolved_by == "FeohLedger"


# ---------------------------------------------------------------------------
# The one-click record from the exception
# ---------------------------------------------------------------------------


async def _blocking_erp_row(realdb, mk, info, *, number, uploaded_by_id=None):
    """A BLOCKING `paid_not_recorded` row (the ERP paid an invoice FeohLedger
    held in a draft run), then the run's payment cancelled — the state in which
    a person decides the ERP's payment is the one that stands."""
    inv_id = await _seed_invoice(mk, info.org_id, number=number, uploaded_by_id=uploaded_by_id)
    async with mk() as s:
        run = PaymentRun(
            id=uuid.uuid4(),
            organization_id=info.org_id,
            status="draft",
            total_amount=Decimal("410.00"),
            initiated_by=info.users["ap_manager"],
        )
        s.add(run)
        await s.flush()
        payment = Payment(
            invoice_id=inv_id,
            payment_run_id=run.id,
            amount=Decimal("410.00"),
            method="ach",
            status="pending",
            correlation_id=uuid.uuid4(),
        )
        s.add(payment)
        await s.commit()
        payment_id = payment.id
    await _report_paid(realdb, inv_id)
    async with mk() as s:
        (await s.get(Payment, payment_id)).status = "cancelled"
        await s.commit()
    row = await _erp_row(mk, inv_id)
    assert row.exception_type == "payment_reconciliation"
    return inv_id, row.id


async def test_a_second_one_click_is_refused_and_books_nothing(realdb):
    """A double click: the second request finds the row already resolved (it
    is locked and re-read before it is judged) and records nothing more."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id, row_id = await _blocking_erp_row(realdb, mk, info, number="ERP-ONE-5")
    body = {"method": "check", "reference": "BILL-145", "paid_on": "2026-10-20"}

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post(f"/api/exceptions/{row_id}/record-erp-payment", json=body)
        second = await c.post(f"/api/exceptions/{row_id}/record-erp-payment", json=body)

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "exception_not_actionable"
    assert len([p for p in await _payments(mk, inv_id) if p.status == "completed"]) == 1


async def test_one_click_records_the_erp_payment_and_resolves_a_blocking_row(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id, row_id = await _blocking_erp_row(realdb, mk, info, number="ERP-ONE-1")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        listed = await c.get("/api/exceptions", params={"search": "ERP-ONE-1"})
        detail = await c.get(f"/api/exceptions/{row_id}")
        # `POST /api/payments/record-outside` refuses on this very row.
        refused = await c.post(
            "/api/payments/record-outside",
            json={
                "invoice_id": str(inv_id),
                "method": "check",
                "reference": "BILL-145",
                "paid_on": "2026-10-20",
            },
        )
        res = await c.post(
            f"/api/exceptions/{row_id}/record-erp-payment",
            json={"method": "check", "reference": "BILL-145", "paid_on": "2026-10-20"},
        )

    [item] = [i for i in listed.json()["items"] if i["id"] == str(row_id)]
    assert item["record_erp_payment_refusal"] is None
    assert item["erp_report"]["method"] == "check"
    assert item["erp_name"] == "QuickBooks Online"
    assert detail.json()["record_erp_payment_refusal"] is None
    assert detail.json()["decision_refusals"]["resolve"] is None
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "external_payment_blocking_exception"

    assert res.status_code == 201, res.text
    assert (await _invoice(mk, inv_id)).status == InvoiceStatus.paid
    row = await _erp_row(mk, inv_id)
    assert row.status == "resolved"
    # Resolved as the person who recorded it, not as the system.
    assert row.resolved_by and row.resolved_by != "FeohLedger"
    async with mk() as s:
        actors = (
            (
                await s.execute(
                    select(AuditLog.actor_id).where(
                        AuditLog.entity_id == row_id, AuditLog.action == "exception.resolved"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert actors == [info.users["ap_manager"]]
    recorded = [p for p in await _payments(mk, inv_id) if p.status == "completed"]
    assert len(recorded) == 1 and recorded[0].method == "check"


async def test_a_refused_one_click_ships_no_audit_row(realdb, monkeypatch):
    """In `lambda` audit mode a dispatched audit row leaves for SQS at once, so
    a rollback cannot take it back: every refusal must come before anything is
    audited, or the WORM trail would say a still-open blocker was resolved."""
    import app.services.audit_dispatch as audit_dispatch
    from app.config import settings

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id, row_id = await _blocking_erp_row(realdb, mk, info, number="ERP-ONE-6")
    await _blocker(mk, info.org_id, inv_id, exception_type="fraud_flag")

    shipped: list[dict] = []
    monkeypatch.setattr(settings, "audit_mode", "lambda")
    monkeypatch.setattr(
        audit_dispatch, "_send_to_sqs", lambda *a, **k: shipped.append({"args": a, "kw": k})
    )
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(
            f"/api/exceptions/{row_id}/record-erp-payment",
            json={"method": "check", "reference": "BILL-145", "paid_on": "2026-10-20"},
        )

    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "external_payment_blocking_exception"
    assert shipped == []
    assert (await _erp_row(mk, inv_id)).status == "open"


async def test_one_click_is_refused_to_the_invoices_uploader_and_rolls_back(realdb):
    """Both SoD rules hold: the uploader may neither clear a blocking row nor
    record the payment. Nothing is resolved, nothing is booked."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id, row_id = await _blocking_erp_row(
        realdb, mk, info, number="ERP-ONE-2", uploaded_by_id=info.users["admin"]
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        detail = await c.get(f"/api/exceptions/{row_id}")
        res = await c.post(
            f"/api/exceptions/{row_id}/record-erp-payment",
            json={"method": "check", "reference": "BILL-145", "paid_on": "2026-10-20"},
        )

    assert detail.json()["record_erp_payment_refusal"]["code"] == "segregation_implicated"
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == "segregation_implicated"
    assert (await _erp_row(mk, inv_id)).status == "open"
    assert (await _invoice(mk, inv_id)).status == InvoiceStatus.posted_in_erp


async def test_one_click_refusal_from_the_record_leaves_the_row_open(realdb):
    """A record the money gates refuse (another open blocker) rolls the
    resolve back with it — the question is never closed with nothing booked."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id, row_id = await _blocking_erp_row(realdb, mk, info, number="ERP-ONE-3")
    await _blocker(mk, info.org_id, inv_id, exception_type="fraud_flag")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(
            f"/api/exceptions/{row_id}/record-erp-payment",
            json={"method": "check", "reference": "BILL-145", "paid_on": "2026-10-20"},
        )

    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "external_payment_blocking_exception"
    assert (await _erp_row(mk, inv_id)).status == "open"
    assert [p for p in await _payments(mk, inv_id) if p.status == "completed"] == []


async def test_one_click_on_a_row_without_an_erp_payment_is_refused(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id, number="ERP-ONE-4")
    blocker_id = await _blocker(mk, info.org_id, inv_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(
            f"/api/exceptions/{blocker_id}/record-erp-payment",
            json={"method": "check", "reference": "X", "paid_on": "2026-10-20"},
        )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "exception_not_erp_paid_report"


# ---------------------------------------------------------------------------
# The payment queue names the blocker and carries the ERP's payment
# ---------------------------------------------------------------------------


async def test_the_queue_names_the_blocking_exception_and_the_erp_payment(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(
        mk, info.org_id, number="ERP-QUEUE-1", uploaded_by_id=info.users["admin"]
    )
    blocker_id = await _blocker(mk, info.org_id, inv_id)
    await _report_paid(realdb, inv_id)
    erp_row = await _erp_row(mk, inv_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.get("/api/payments/queue", params={"page_size": 100})
    assert res.status_code == 200, res.text
    [row] = [i for i in res.json()["items"] if i["id"] == str(inv_id)]

    assert row["blocked_reason"] == "payment_reconciliation"
    assert row["record_outside_refusal"] == "external_payment_blocking_exception"
    assert row["blocking_exception"] == {
        "id": str(blocker_id),
        "exception_type": "payment_reconciliation",
        # The reader uploaded the invoice: a colleague must clear it.
        "clear_refusal": "segregation_implicated",
    }
    assert row["erp_reported_payment"] == {
        "exception_id": str(erp_row.id),
        "erp_name": "QuickBooks Online",
        "method": "check",
        "reference": "BILL-145",
        "paid_on": "2026-10-20",
    }
