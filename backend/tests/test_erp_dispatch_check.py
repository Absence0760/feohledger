"""The live ERP-bill check at dispatch (decisions §296), over real rows.

QuickBooks status sync (§295) moves an invoice out of the payment queue once a
payment made in QuickBooks REACHES us — by webhook within seconds, or by the
next sweep. A run executing inside that window used to pay the bill a second
time. Now every payment, immediately before its rail adapter is called and
under the invoice row lock, re-reads its bill from QuickBooks; these tests
reproduce the race both ways it happens:

* the bill is paid in QuickBooks between the run's approval and its dispatch;
* the bill of the SECOND item is paid in QuickBooks while the FIRST item of
  the same run is being sent.

The ERP adapter is faked at `erp_status_sync._adapter_or_none`; the payment
rail is the tenant's default `mock`. Everything else is the real
`/runs/{id}/execute` and `/retry-failed` path.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.models.workflow import WorkflowDefinition, WorkflowInstance
from app.services import erp_status_sync
from app.services.erp_adapters.base import ErpBillReport, ErpBillState
from app.services.payment_adapters.mock_adapter import MockPaymentAdapter
from app.services.payment_runs import RETRY_SAFE, classify_payment_failure

COMPANY = "9130"
REALM = f"quickbooks_online:{COMPANY}"


@pytest.fixture(autouse=True)
def _status_sync_on(monkeypatch):
    monkeypatch.setattr(settings, "erp_status_sync_enabled", True)
    # The per-org "QuickBooks unreachable" hold is process state; every test
    # starts with QuickBooks reachable.
    erp_status_sync._unreachable_until.clear()
    yield
    erp_status_sync._unreachable_until.clear()


class FakeQbo:
    """QuickBooks as the dispatch check sees it: one state per bill id."""

    erp_type = "quickbooks_online"
    supports_status_sync = True

    def __init__(self, states: dict[str, ErpBillState] | None = None):
        self.states = dict(states or {})
        self.reads: list[str] = []
        self.fail: Exception | None = None

    async def bill_report(self, bill_id: str) -> ErpBillReport:
        self.reads.append(bill_id)
        if self.fail is not None:
            raise self.fail
        state = self.states.get(bill_id, ErpBillState.open)
        return ErpBillReport(
            bill_id=bill_id,
            state=state,
            reduced_outside=state is not ErpBillState.open,
            external_payment_ids=("901",) if state is not ErpBillState.open else (),
        )


async def _connect(realdb) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
        org.settings = {
            **(org.settings or {}),
            "erp": {
                "type": "quickbooks_online",
                "integration_method": "direct",
                "oauth": {
                    "provider": "quickbooks_online",
                    "external_tenant_id": COMPANY,
                    "org_id": str(org.id),
                    "connection_id": "conn-1",
                },
            },
        }
        await s.commit()


async def _posted_invoice(
    realdb, *, bill_id: str, realm: str | None = REALM, pending_voids: list[str] | None = None
) -> str:
    """An invoice pushed to QuickBooks as ``bill_id`` and landed payable."""
    info = realdb.info("a")
    async with realdb.sessionmaker("a")() as s:
        vendor = Vendor(organization_id=info.org_id, name=f"QBO Vendor {bill_id}")
        defn = WorkflowDefinition(organization_id=info.org_id, name="d", steps_config={"steps": []})
        s.add_all([vendor, defn])
        await s.flush()
        inv = Invoice(
            organization_id=info.org_id,
            correlation_id=uuid.uuid4(),
            invoice_number=f"QBO-DISPATCH-{bill_id}-{uuid.uuid4().hex[:4]}",
            vendor_name=vendor.name,
            vendor_id=vendor.id,
            amount=Decimal("120.00"),
            currency="USD",
            status=InvoiceStatus.posted_in_erp,
        )
        s.add(inv)
        await s.flush()
        state = {"erp_reference": bill_id}
        if pending_voids:
            state["erp_payment_voids_pending"] = pending_voids
        if realm:
            state["erp_realm"] = realm
        s.add(
            WorkflowInstance(
                correlation_id=inv.correlation_id,
                definition_id=defn.id,
                invoice_id=inv.id,
                state="completed",
                state_data=state,
            )
        )
        await s.commit()
        return str(inv.id)


async def _run(realdb, invoice_ids: list[str]) -> str:
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": i, "method": "ach"} for i in invoice_ids]},
        )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _execute(realdb, run_id: str) -> dict:
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _payment(realdb, invoice_id: str) -> Payment:
    async with realdb.sessionmaker("a")() as s:
        return (
            await s.execute(select(Payment).where(Payment.invoice_id == uuid.UUID(invoice_id)))
        ).scalar_one()


async def _blocking(realdb, invoice_id: str) -> list[str]:
    async with realdb.sessionmaker("a")() as s:
        return list(
            (
                await s.execute(
                    select(APException.exception_type).where(
                        APException.invoice_id == uuid.UUID(invoice_id),
                        APException.status == "open",
                    )
                )
            ).scalars()
        )


async def _open_row(realdb, invoice_id: str) -> APException:
    async with realdb.sessionmaker("a")() as s:
        return (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == uuid.UUID(invoice_id),
                    APException.status == "open",
                )
            )
        ).scalar_one()


async def test_a_bill_paid_in_quickbooks_after_approval_is_not_paid_again(realdb):
    await _connect(realdb)
    invoice_id = await _posted_invoice(realdb, bill_id="501")
    run_id = await _run(realdb, [invoice_id])

    # The race: a bookkeeper pays bill 501 in QuickBooks after the run was
    # approved, and no webhook or sweep has reported it yet.
    qbo = FakeQbo({"501": ErpBillState.paid})
    with patch_adapter(qbo):
        result = await _execute(realdb, run_id)

    assert result["payments_completed"] == 0
    payment = await _payment(realdb, invoice_id)
    assert payment.status == "failed"
    assert payment.failure_reason == "erp_bill_not_open:paid"
    # Stopped before the rail: no order exists at the processor.
    assert payment.provider_payment_id is None
    assert qbo.reads == ["501"]
    # Blocking, so no later run (or /retry-failed) pays it until a person looks.
    assert await _blocking(realdb, invoice_id) == ["payment_reconciliation"]
    async with realdb.sessionmaker("a")() as s:
        invoice = await s.get(Invoice, uuid.UUID(invoice_id))
    assert invoice.status == InvoiceStatus.posted_in_erp
    # A catalogued sentence for an AP user; the bill and payment are data
    # (decisions §319), not spliced into the prose.
    row = await _open_row(realdb, invoice_id)
    assert row.description_code == "exception.erp_dispatch_stopped_paid"
    assert row.description_params == {}
    assert "501" not in row.description and str(payment.id) not in row.description
    assert row.erp_report == {
        "kind": "dispatch_stopped",
        "erp_type": "quickbooks_online",
        "erp_document_id": "501",
        "bill_state": "paid",
        "payment_id": str(payment.id),
    }


async def test_a_bill_paid_while_an_earlier_item_of_the_same_run_is_sent(realdb, monkeypatch):
    """Item 2's bill is paid in QuickBooks while item 1 is at the rail. Item 1
    pays; item 2 is refused at its own dispatch; the run carries on."""
    await _connect(realdb)
    first = await _posted_invoice(realdb, bill_id="601")
    second = await _posted_invoice(realdb, bill_id="602")
    run_id = await _run(realdb, [first, second])

    qbo = FakeQbo()
    original = MockPaymentAdapter.create_payment

    async def pay_and_race(self, payload):
        # While one item is with the rail, someone pays bill 602 in QuickBooks
        # (whichever item the loop reaches first, the OTHER one's bill).
        sent = await original(self, payload)
        for bill in ("601", "602"):
            if bill not in qbo.reads[:1]:
                qbo.states[bill] = ErpBillState.paid
        return sent

    monkeypatch.setattr(MockPaymentAdapter, "create_payment", pay_and_race)
    with patch_adapter(qbo):
        result = await _execute(realdb, run_id)

    assert result["payments_completed"] == 1
    paid_first = qbo.reads[0]
    by_bill = {"601": first, "602": second}
    sent = await _payment(realdb, by_bill[paid_first])
    held = await _payment(realdb, by_bill["602" if paid_first == "601" else "601"])
    assert sent.status == "completed"
    assert held.status == "failed" and held.failure_reason == "erp_bill_not_open:paid"
    assert held.provider_payment_id is None
    # Each item read its OWN bill at its own dispatch.
    assert sorted(qbo.reads) == ["601", "602"]


async def test_an_unreadable_bill_is_held_not_paid_blind_and_retry_rechecks(realdb):
    await _connect(realdb)
    invoice_id = await _posted_invoice(realdb, bill_id="701")
    run_id = await _run(realdb, [invoice_id])

    qbo = FakeQbo()
    qbo.fail = TimeoutError()  # Intuit unreachable at the moment of dispatch
    with patch_adapter(qbo):
        result = await _execute(realdb, run_id)
    assert result["payments_completed"] == 0
    payment = await _payment(realdb, invoice_id)
    assert payment.failure_reason == "erp_bill_unverified"
    # Nothing is known to be wrong, so nothing for a person to clear...
    assert await _blocking(realdb, invoice_id) == []
    # ...and the hold is retry-safe: nothing was sent.
    assert (
        classify_payment_failure(failure_reason=payment.failure_reason, provider_payment_id=None)
        == RETRY_SAFE
    )

    # A minute later Intuit is back and the bill is still open: the retry
    # re-checks and pays. (Within the minute the org's checks hold without
    # reading — see the breaker test.)
    erp_status_sync._unreachable_until.clear()
    qbo.fail = None
    with patch_adapter(qbo):
        async with realdb.client(key="a", role="ap_manager") as c:
            resp = await c.post(f"/api/payments/runs/{run_id}/retry-failed")
        assert resp.status_code == 200, resp.text
    statuses = []
    async with realdb.sessionmaker("a")() as s:
        statuses = list(
            (
                await s.execute(
                    select(Payment.status).where(Payment.invoice_id == uuid.UUID(invoice_id))
                )
            ).scalars()
        )
    assert "completed" in statuses, statuses
    assert qbo.reads == ["701", "701"]


async def test_a_slow_read_is_bounded_and_held(realdb, monkeypatch):
    monkeypatch.setattr(settings, "erp_dispatch_check_timeout_seconds", 1)
    await _connect(realdb)
    invoice_id = await _posted_invoice(realdb, bill_id="801")
    run_id = await _run(realdb, [invoice_id])

    class Hanging(FakeQbo):
        async def bill_report(self, bill_id):
            await asyncio.sleep(30)

    with patch_adapter(Hanging()):
        await _execute(realdb, run_id)
    assert (await _payment(realdb, invoice_id)).failure_reason == "erp_bill_unverified"


async def test_a_bill_in_another_quickbooks_company_is_held_and_owned(realdb):
    """Posted while connected to a different company (a sandbox → production
    switch): the bill can never be read under this connection, so the hold
    gets a (non-blocking) exception a person resolves."""
    await _connect(realdb)
    invoice_id = await _posted_invoice(realdb, bill_id="901", realm="quickbooks_online:sandbox")
    run_id = await _run(realdb, [invoice_id])
    qbo = FakeQbo()
    with patch_adapter(qbo):
        await _execute(realdb, run_id)
    assert (await _payment(realdb, invoice_id)).failure_reason == (
        "erp_bill_unlinked:other_company"
    )
    assert qbo.reads == []
    assert await _blocking(realdb, invoice_id) == ["erp_reconciliation"]
    row = await _open_row(realdb, invoice_id)
    assert row.description_code == "exception.erp_bill_unlinked_other_company"
    assert "other_company" not in row.description
    assert row.erp_report["kind"] == "bill_unlinked"
    assert row.erp_report["unlinked"] == "other_company"
    assert row.erp_report["erp_document_id"] == "901"


async def test_without_status_sync_there_is_no_live_read(realdb, monkeypatch):
    monkeypatch.setattr(settings, "erp_status_sync_enabled", False)
    await _connect(realdb)
    invoice_id = await _posted_invoice(realdb, bill_id="991")
    run_id = await _run(realdb, [invoice_id])
    qbo = FakeQbo({"991": ErpBillState.paid})
    with patch_adapter(qbo):
        result = await _execute(realdb, run_id)
    assert result["payments_completed"] == 1
    assert qbo.reads == []


async def test_a_nacha_file_never_carries_a_bill_paid_in_quickbooks(realdb):
    """The bank's upload of the file is the money moving, so the export runs
    the same live read — and the blocking flag outlives its 409."""
    from tests.test_nacha import NACHA_CFG, _business_day, _seed_run, _settings

    info = realdb.info("a")
    await _connect(realdb)
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, inv_id = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    async with realdb.sessionmaker("a")() as s:
        invoice = await s.get(Invoice, inv_id)
        invoice.status = InvoiceStatus.posted_in_erp
        defn = WorkflowDefinition(organization_id=info.org_id, name="d", steps_config={"steps": []})
        s.add(defn)
        await s.flush()
        s.add(
            WorkflowInstance(
                definition_id=defn.id,
                invoice_id=inv_id,
                state="completed",
                state_data={"erp_reference": "551", "erp_realm": REALM},
            )
        )
        await s.commit()

    with patch_adapter(FakeQbo({"551": ErpBillState.paid})):
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.get(
                f"/api/payments/runs/{run_id}/nacha",
                params={"effective_date": _business_day(2).isoformat()},
            )
    assert resp.status_code == 409, resp.text
    assert "erp_bill_not_open:paid" in resp.text
    assert await _blocking(realdb, str(inv_id)) == ["payment_reconciliation"]


def patch_adapter(fake):
    from unittest.mock import patch

    return patch.object(erp_status_sync, "_adapter_or_none", return_value=fake)


# ---------------------------------------------------------------------------
# money-path review round (§296)
# ---------------------------------------------------------------------------


async def test_one_failed_read_holds_the_rest_of_the_run_without_reading(realdb):
    """A degraded QuickBooks costs a run ONE timeout, not one per item with
    each invoice locked."""
    await _connect(realdb)
    invoices = [await _posted_invoice(realdb, bill_id=f"77{i}") for i in range(3)]
    run_id = await _run(realdb, invoices)
    qbo = FakeQbo()
    qbo.fail = httpx.ConnectError("QuickBooks unreachable")
    with patch_adapter(qbo):
        await _execute(realdb, run_id)
    assert len(qbo.reads) == 1
    for invoice_id in invoices:
        assert (await _payment(realdb, invoice_id)).failure_reason == "erp_bill_unverified"


async def test_the_rail_is_never_called_for_a_bill_paid_in_quickbooks(realdb, monkeypatch):
    await _connect(realdb)
    invoice_id = await _posted_invoice(realdb, bill_id="511")
    run_id = await _run(realdb, [invoice_id])
    calls: list[object] = []
    original = MockPaymentAdapter.create_payment

    async def spy(self, payload):
        calls.append(payload)
        return await original(self, payload)

    monkeypatch.setattr(MockPaymentAdapter, "create_payment", spy)
    with patch_adapter(FakeQbo({"511": ErpBillState.paid})):
        await _execute(realdb, run_id)
    assert calls == []


async def test_retry_after_a_paid_bill_keeps_refusing_until_a_person_clears_it(realdb):
    await _connect(realdb)
    invoice_id = await _posted_invoice(realdb, bill_id="521")
    run_id = await _run(realdb, [invoice_id])
    qbo = FakeQbo({"521": ErpBillState.paid})
    with patch_adapter(qbo):
        await _execute(realdb, run_id)
        async with realdb.client(key="a", role="ap_manager") as c:
            resp = await c.post(f"/api/payments/runs/{run_id}/retry-failed")
        assert resp.status_code == 200, resp.text
    async with realdb.sessionmaker("a")() as s:
        statuses = list(
            (
                await s.execute(
                    select(Payment.status).where(Payment.invoice_id == uuid.UUID(invoice_id))
                )
            ).scalars()
        )
    assert "completed" not in statuses
    # The open blocking exception refuses it before the bill is even re-read.
    assert qbo.reads == ["521"]


async def test_a_virtual_card_item_for_a_paid_bill_mints_no_card(realdb):
    from app.models.virtual_card import VirtualCard
    from tests.test_card_generate_gates import _enable_cards

    await _connect(realdb)
    await _enable_cards(realdb, org_id=realdb.info("a").org_id)
    invoice_id = await _posted_invoice(realdb, bill_id="531")
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "virtual_card"}]},
        )
    assert resp.status_code == 201, resp.text
    with patch_adapter(FakeQbo({"531": ErpBillState.paid})):
        await _execute(realdb, resp.json()["id"])
    async with realdb.sessionmaker("a")() as s:
        assert (await s.execute(select(VirtualCard))).first() is None
    assert (await _payment(realdb, invoice_id)).failure_reason == "erp_bill_not_open:paid"


async def test_cards_generate_mints_nothing_for_a_bill_paid_in_quickbooks(realdb):
    from app.models.virtual_card import VirtualCard
    from tests.test_card_generate_gates import _enable_cards

    await _connect(realdb)
    await _enable_cards(realdb, org_id=realdb.info("a").org_id)
    invoice_id = await _posted_invoice(realdb, bill_id="541")
    with patch_adapter(FakeQbo({"541": ErpBillState.paid})):
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post("/api/cards/generate", json={"invoice_ids": [invoice_id]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["total"] == 0
    async with realdb.sessionmaker("a")() as s:
        assert (await s.execute(select(VirtualCard))).first() is None
    assert await _blocking(realdb, invoice_id) == ["payment_reconciliation"]


async def test_a_bill_paid_only_by_our_pending_write_back_is_held_without_a_flag(realdb):
    await _connect(realdb)
    invoice_id = await _posted_invoice(realdb, bill_id="561", pending_voids=[str(uuid.uuid4())])
    run_id = await _run(realdb, [invoice_id])

    class OursOnly(FakeQbo):
        async def bill_report(self, bill_id):
            self.reads.append(bill_id)
            return ErpBillReport(bill_id=bill_id, state=ErpBillState.paid, paid_only_by_us=True)

    with patch_adapter(OursOnly()):
        await _execute(realdb, run_id)
    assert (await _payment(realdb, invoice_id)).failure_reason == "erp_bill_void_pending"
    assert await _blocking(realdb, invoice_id) == []


async def test_paid_only_by_us_with_no_void_pending_is_unexplained_and_blocks(realdb):
    await _connect(realdb)
    invoice_id = await _posted_invoice(realdb, bill_id="562")
    run_id = await _run(realdb, [invoice_id])

    class OursOnly(FakeQbo):
        async def bill_report(self, bill_id):
            self.reads.append(bill_id)
            return ErpBillReport(bill_id=bill_id, state=ErpBillState.paid, paid_only_by_us=True)

    with patch_adapter(OursOnly()):
        await _execute(realdb, run_id)
    assert (await _payment(realdb, invoice_id)).failure_reason == "erp_bill_not_open:paid"
    assert await _blocking(realdb, invoice_id) == ["payment_reconciliation"]


async def test_a_deleted_bill_blocks(realdb):
    await _connect(realdb)
    invoice_id = await _posted_invoice(realdb, bill_id="563")
    run_id = await _run(realdb, [invoice_id])
    with patch_adapter(FakeQbo({"563": ErpBillState.deleted})):
        await _execute(realdb, run_id)
    assert (await _payment(realdb, invoice_id)).failure_reason == "erp_bill_not_open:deleted"
    assert await _blocking(realdb, invoice_id) == ["payment_reconciliation"]
    row = await _open_row(realdb, invoice_id)
    assert row.description_code == "exception.erp_dispatch_stopped_deleted"


async def test_one_bad_bill_does_not_hold_the_rest_of_the_run(realdb):
    """A 400 on one bill is about that bill, not about QuickBooks being down:
    the next item is still read and paid."""

    class Refused(Exception):
        status_code = 400

    await _connect(realdb)
    bad = await _posted_invoice(realdb, bill_id="581")
    good = await _posted_invoice(realdb, bill_id="582")
    run_id = await _run(realdb, [bad, good])

    class OneBad(FakeQbo):
        async def bill_report(self, bill_id):
            self.reads.append(bill_id)
            if bill_id == "581":
                raise Refused()
            return ErpBillReport(bill_id=bill_id, state=ErpBillState.open)

    qbo = OneBad()
    with patch_adapter(qbo):
        await _execute(realdb, run_id)
    assert sorted(qbo.reads) == ["581", "582"]
    assert (await _payment(realdb, bad)).failure_reason == "erp_bill_unverified"
    assert (await _payment(realdb, good)).status == "completed"


async def test_a_lost_connection_holds_through_the_real_adapter(realdb):
    """The real QuickBooks adapter, whose token fetch says the connection is
    gone: held (never paid blind), and the org's next checks hold unread."""
    from unittest.mock import AsyncMock
    from unittest.mock import patch as mpatch

    from app.services import erp_oauth
    from app.services.erp_adapters.quickbooks_online import QuickBooksOnlineAdapter

    await _connect(realdb)
    first = await _posted_invoice(realdb, bill_id="591")
    second = await _posted_invoice(realdb, bill_id="592")
    run_id = await _run(realdb, [first, second])
    refused = AsyncMock(side_effect=erp_oauth.ErpNotConnectedError("quickbooks_online"))
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
    adapter = QuickBooksOnlineAdapter(org.settings["erp"])
    with mpatch.object(erp_oauth, "get_access_token", refused), patch_adapter(adapter):
        await _execute(realdb, run_id)
    for invoice_id in (first, second):
        assert (await _payment(realdb, invoice_id)).failure_reason == "erp_bill_unverified"
    assert refused.await_count == 1  # the second item held without reading


async def test_a_push_that_named_no_document_is_held(realdb):
    await _connect(realdb)
    invoice_id = await _posted_invoice(realdb, bill_id="UNKNOWN")
    run_id = await _run(realdb, [invoice_id])
    qbo = FakeQbo()
    with patch_adapter(qbo):
        await _execute(realdb, run_id)
    assert (await _payment(realdb, invoice_id)).failure_reason == "erp_bill_unlinked:no_document"
    assert qbo.reads == []
    row = await _open_row(realdb, invoice_id)
    assert row.description_code == "exception.erp_bill_unlinked_no_document"


async def test_a_nacha_export_with_an_unreadable_bill_says_retry_and_commits_nothing(realdb):
    from tests.test_nacha import NACHA_CFG, _business_day, _seed_run, _settings

    info = realdb.info("a")
    await _connect(realdb)
    await _settings(realdb, info.org_id, {"mode": "record_only", "nacha": NACHA_CFG})
    run_id, inv_id = await _seed_run(realdb, initiated_by=info.users["ap_manager"])
    async with realdb.sessionmaker("a")() as s:
        invoice = await s.get(Invoice, inv_id)
        invoice.status = InvoiceStatus.posted_in_erp
        defn = WorkflowDefinition(organization_id=info.org_id, name="d", steps_config={"steps": []})
        s.add(defn)
        await s.flush()
        s.add(
            WorkflowInstance(
                definition_id=defn.id,
                invoice_id=inv_id,
                state="completed",
                state_data={"erp_reference": "552", "erp_realm": REALM},
            )
        )
        await s.commit()
    qbo = FakeQbo()
    qbo.fail = httpx.ConnectError("QuickBooks unreachable")
    with patch_adapter(qbo):
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.get(
                f"/api/payments/runs/{run_id}/nacha",
                params={"effective_date": _business_day(2).isoformat()},
            )
    assert resp.status_code == 409, resp.text
    assert "try the export again" in resp.text
    assert await _blocking(realdb, str(inv_id)) == []
    async with realdb.sessionmaker("a")() as s:
        run_status = (
            await s.execute(select(PaymentRun.status).where(PaymentRun.id == run_id))
        ).scalar_one()
    assert run_status == "draft"  # nothing exported, nothing committed
