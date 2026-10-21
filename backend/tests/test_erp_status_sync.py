"""The ERP status sweep and its on-demand route, over a real DB (decisions §303).

A pushed invoice rests at `sent_to_erp` until the ERP pays it. Pinned here:

  * the sweep picks up only invoices awaiting an ERP outcome that carry an ERP
    reference pushed through the connection the tenant has NOW — never another
    adapter's or another ERP company's id;
  * it pages through every candidate (batch size is a page, not a cap);
  * `paid` is recorded through the webhook's own path (a `completed` external
    payment, `invoice.paid_via_erp_report`), once, however many ticks run;
  * a refused record opens the BLOCKING `payment_reconciliation` once, leaves
    the invoice at `sent_to_erp`, and lands by itself once the cause is cleared;
  * `cancelled` and `settled_no_payment` are flagged once per reported status —
    resolving the flag is not answered by the same flag next tick — and never
    transition the invoice or record a payment;
  * one failing invoice and one failing tenant are counted and isolated;
  * a pushed invoice cannot be paid twice: FeohLedger will not queue, run or
    book it while the ERP holds it, and once the ERP's payment is recorded it is
    `paid` with exactly one payment;
  * the push records where the bill lives; the on-demand route applies the same
    poll for one invoice, with the push's roles.

The ERP is the `mock` adapter with its status read patched per reference — the
adapters' own mappings are pinned in `test_erp_*_adapter.py`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.workflow import AuditLog, WorkflowInstance
from app.services import erp_status_sync
from app.services.erp_adapters.base import ErpBillStatus, ErpInvoiceStatus
from app.services.erp_adapters.mock_adapter import MockAdapter
from app.services.external_payment import EXTERNAL_PAYMENT_PROVIDER

pytestmark = pytest.mark.asyncio

MOCK_ERP = {"type": "mock", "integration_method": "direct"}


@pytest.fixture(autouse=True)
def _wire(realdb, monkeypatch):
    """Point the sweep at the harness's per-loop tenant engines."""
    makers: dict[str, object] = {}

    def _maker(db_name: str):
        key = "a" if db_name == realdb.info("a").db_name else "b"
        if key not in makers:
            makers[key] = realdb.sessionmaker(key)
        return makers[key]

    monkeypatch.setattr(erp_status_sync, "tenant_sessionmaker", _maker)


class _Erp:
    """What the mock ERP answers per reference, and which references it was asked."""

    def __init__(self, monkeypatch):
        # A bare status, or an `ErpBillStatus` with the amount / date paid.
        self.statuses: dict[str, ErpInvoiceStatus | ErpBillStatus] = {}
        self.raises: set[str] = set()
        self.asked: list[str] = []
        erp = self

        async def _status(_self, erp_document_id: str) -> ErpBillStatus:
            erp.asked.append(erp_document_id)
            if erp_document_id in erp.raises:
                raise RuntimeError("ERP unreachable: secret-erp-body")
            found = erp.statuses.get(erp_document_id, ErpInvoiceStatus.open)
            return found if isinstance(found, ErpBillStatus) else ErpBillStatus(found)

        monkeypatch.setattr(MockAdapter, "get_bill_status", _status)


@pytest.fixture
def erp(monkeypatch):
    return _Erp(monkeypatch)


async def _connect(realdb, key="a", erp_config=None):
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(key).org_id)
        org.settings = {**(org.settings or {}), "erp": erp_config or MOCK_ERP}
        await s.commit()


def _pushed_state(reference, *, adapter="mock", realm=None) -> dict:
    return {
        "erp_reference": reference,
        "erp_retries": 1,
        "erp_adapter": adapter,
        "erp_realm": realm,
    }


async def _seed(
    realdb,
    *,
    number,
    status=InvoiceStatus.sent_to_erp,
    state_data=None,
    key="a",
    amount="410.00",
    invoice_id=None,
) -> uuid.UUID:
    from app.services.workflow_engine import get_or_create_workflow_definition

    info = realdb.info(key)
    inv_id = invoice_id or uuid.uuid4()
    async with realdb.sessionmaker(key)() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=info.org_id,
            invoice_number=number,
            vendor_name="ERP Status Vendor",
            amount=Decimal(amount),
            currency="USD",
            status=status,
            approved_by="someone",
        )
        s.add(inv)
        await s.flush()
        defn = await get_or_create_workflow_definition(s, info.org_id)
        s.add(
            WorkflowInstance(
                correlation_id=inv.correlation_id,
                definition_id=defn.id,
                invoice_id=inv_id,
                current_step=3,
                state="completed",
                state_data=state_data,
            )
        )
        await s.commit()
    return inv_id


async def _invoice(realdb, inv_id, key="a") -> Invoice:
    async with realdb.sessionmaker(key)() as s:
        return await s.get(Invoice, inv_id)


async def _payments(realdb, inv_id, key="a") -> list[Payment]:
    async with realdb.sessionmaker(key)() as s:
        return list(
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )


async def _exceptions(realdb, inv_id, key="a") -> list[APException]:
    async with realdb.sessionmaker(key)() as s:
        return list(
            (await s.execute(select(APException).where(APException.invoice_id == inv_id)))
            .scalars()
            .all()
        )


async def _actions(realdb, inv_id, key="a") -> list[str]:
    async with realdb.sessionmaker(key)() as s:
        return list(
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_id == inv_id)))
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# paid
# ---------------------------------------------------------------------------


async def test_paid_in_the_erp_is_recorded_once_through_the_webhook_path(realdb, erp):
    await _connect(realdb)
    inv_id = await _seed(realdb, number="SYNC-PAID", state_data=_pushed_state("MOCK-PAID-1"))
    erp.statuses["MOCK-PAID-1"] = ErpInvoiceStatus.paid

    first = await erp_status_sync.run_erp_status_sync_once()
    second = await erp_status_sync.run_erp_status_sync_once()

    assert (first.recorded_paid, first.failures, first.invoice_failures) == (1, 0, 0)
    # Paid left the candidate set: the second tick does not even ask.
    assert second.invoices_polled == 0
    assert erp.asked == ["MOCK-PAID-1"]

    invoice = await _invoice(realdb, inv_id)
    [payment] = await _payments(realdb, inv_id)
    assert invoice.status == InvoiceStatus.paid
    assert payment.status == "completed"
    assert payment.provider == EXTERNAL_PAYMENT_PROVIDER
    assert payment.reference == "MOCK-PAID-1"
    assert payment.amount == Decimal("410.00")
    actions = await _actions(realdb, inv_id)
    # The trail reads in the order things happened: posted, scheduled, paid.
    assert "invoice.erp_status_posted_in_erp" in actions
    assert "invoice.paid_via_erp_report" in actions


async def test_only_invoices_awaiting_the_erp_on_this_connection_are_polled(realdb, erp):
    await _connect(realdb)
    await _seed(realdb, number="E-OK", state_data=_pushed_state("REF-OK"))
    await _seed(
        realdb,
        number="E-POSTED",
        status=InvoiceStatus.posted_in_erp,
        state_data=_pushed_state("REF-POSTED"),
    )
    # Not awaiting the ERP.
    await _seed(
        realdb, number="E-APPROVED", status=InvoiceStatus.approved, state_data=_pushed_state("X1")
    )
    await _seed(realdb, number="E-DONE", status=InvoiceStatus.done, state_data=_pushed_state("X2"))
    await _seed(realdb, number="E-PAID", status=InvoiceStatus.paid, state_data=_pushed_state("X3"))
    # No usable reference.
    await _seed(realdb, number="E-NOREF", state_data={"erp_retries": 1})
    await _seed(realdb, number="E-UNKNOWN", state_data=_pushed_state("UNKNOWN"))
    # Pushed through another adapter, or another ERP company, or before the
    # push recorded where the bill lives: its id means nothing to this one.
    await _seed(
        realdb,
        number="E-QBO",
        state_data=_pushed_state("X4", adapter="quickbooks_online", realm="quickbooks_online:9"),
    )
    await _seed(realdb, number="E-REALM", state_data=_pushed_state("X5", realm="mock:other"))
    await _seed(realdb, number="E-LEGACY", state_data={"erp_reference": "X6", "erp_retries": 1})

    result = await erp_status_sync.run_erp_status_sync_once()

    assert sorted(erp.asked) == ["REF-OK", "REF-POSTED"]
    assert result.invoices_polled == 2


async def test_a_tenant_without_an_erp_is_not_swept(realdb, erp):
    await _seed(realdb, number="NO-ERP", state_data=_pushed_state("REF-NO-ERP"))
    result = await erp_status_sync.run_erp_status_sync_once()
    assert erp.asked == []
    assert result.tenants_scanned == 0


async def test_every_candidate_is_reached_whatever_the_page_size(realdb, erp, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "erp_status_sync_batch_size", 2)
    await _connect(realdb)
    refs = [f"PAGE-{n}" for n in range(5)]
    for ref in refs:
        await _seed(realdb, number=ref, state_data=_pushed_state(ref))
        erp.statuses[ref] = ErpInvoiceStatus.paid

    result = await erp_status_sync.run_erp_status_sync_once()

    assert sorted(erp.asked) == sorted(refs)
    assert result.recorded_paid == 5


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


async def test_a_refused_record_blocks_once_and_lands_after_the_cause_is_cleared(realdb, erp):
    info = realdb.info("a")
    await _connect(realdb)
    inv_id = await _seed(realdb, number="SYNC-DUP", state_data=_pushed_state("REF-DUP"))
    erp.statuses["REF-DUP"] = ErpInvoiceStatus.paid
    async with realdb.sessionmaker("a")() as s:
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

    first = await erp_status_sync.run_erp_status_sync_once()
    await erp_status_sync.run_erp_status_sync_once()

    assert first.refused == 1
    invoice = await _invoice(realdb, inv_id)
    assert invoice.status == InvoiceStatus.sent_to_erp  # never the payable posted_in_erp
    assert await _payments(realdb, inv_id) == []
    flags = [e for e in await _exceptions(realdb, inv_id) if e.exception_type != "duplicate"]
    assert [e.exception_type for e in flags] == ["payment_reconciliation"]
    assert "external_payment_blocking_exception" in flags[0].description

    # A person clears both flags; the next tick records the ERP's payment.
    async with realdb.sessionmaker("a")() as s:
        await s.execute(
            update(APException).where(APException.invoice_id == inv_id).values(status="resolved")
        )
        await s.commit()
    third = await erp_status_sync.run_erp_status_sync_once()

    assert third.recorded_paid == 1
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.paid
    assert len(await _payments(realdb, inv_id)) == 1


@pytest.mark.parametrize(
    ("erp_status", "needle"),
    [
        (ErpInvoiceStatus.cancelled, "void/cancel"),
        (ErpInvoiceStatus.settled_no_payment, "could not confirm a cash payment"),
    ],
)
async def test_cancelled_and_settled_without_payment_are_flagged_once(
    realdb, erp, erp_status, needle
):
    await _connect(realdb)
    inv_id = await _seed(realdb, number=f"SYNC-{erp_status.value}", state_data=_pushed_state("R"))
    erp.statuses["R"] = erp_status

    first = await erp_status_sync.run_erp_status_sync_once()
    [flag] = await _exceptions(realdb, inv_id)
    assert first.flagged == 1
    assert flag.exception_type == "erp_reconciliation"
    assert needle in flag.description
    assert flag.raised_by_user_id is None

    # Resolving the flag is not answered by the same flag on the next tick.
    async with realdb.sessionmaker("a")() as s:
        await s.execute(
            update(APException).where(APException.id == flag.id).values(status="resolved")
        )
        await s.commit()
    second = await erp_status_sync.run_erp_status_sync_once()

    assert second.flagged == 0
    assert len(await _exceptions(realdb, inv_id)) == 1
    invoice = await _invoice(realdb, inv_id)
    assert invoice.status == InvoiceStatus.sent_to_erp
    assert await _payments(realdb, inv_id) == []


@pytest.mark.parametrize(
    "erp_status",
    [ErpInvoiceStatus.open, ErpInvoiceStatus.partially_paid, ErpInvoiceStatus.unknown],
)
async def test_an_unpaid_bill_changes_nothing(realdb, erp, erp_status):
    await _connect(realdb)
    inv_id = await _seed(realdb, number=f"SYNC-{erp_status.value}", state_data=_pushed_state("U"))
    erp.statuses["U"] = erp_status

    result = await erp_status_sync.run_erp_status_sync_once()

    assert (result.invoices_polled, result.recorded_paid, result.flagged) == (1, 0, 0)
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.sent_to_erp
    assert await _exceptions(realdb, inv_id) == []


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


async def test_a_failing_invoice_does_not_stop_the_rest(realdb, erp):
    await _connect(realdb)
    # Ordered by id: the failing invoice comes first.
    bad = await _seed(
        realdb,
        number="SYNC-BAD",
        state_data=_pushed_state("REF-BAD"),
        invoice_id=uuid.UUID(int=1),
    )
    good = await _seed(realdb, number="SYNC-GOOD", state_data=_pushed_state("REF-GOOD"))
    erp.raises.add("REF-BAD")
    erp.statuses["REF-GOOD"] = ErpInvoiceStatus.paid

    result = await erp_status_sync.run_erp_status_sync_once()

    assert (result.invoice_failures, result.recorded_paid) == (1, 1)
    assert (await _invoice(realdb, bad)).status == InvoiceStatus.sent_to_erp
    assert (await _invoice(realdb, good)).status == InvoiceStatus.paid


async def test_a_failing_tenant_does_not_stop_the_others(realdb, erp):
    from app.services.sweep_health import extract_counts, failure_count

    await _connect(realdb, "a")
    # Tenant b names an adapter we don't have: its sweep cannot run.
    await _connect(realdb, "b", {"type": "no_such_erp", "integration_method": "direct"})
    inv_id = await _seed(realdb, number="SYNC-A", state_data=_pushed_state("REF-A"))
    erp.statuses["REF-A"] = ErpInvoiceStatus.paid

    result = await erp_status_sync.run_erp_status_sync_once()

    assert result.failures == 1
    assert result.recorded_paid == 1
    assert failure_count(extract_counts(result)) == 1  # visible in sweep health
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.paid


# ---------------------------------------------------------------------------
# the double-payment safeguard
# ---------------------------------------------------------------------------


async def test_a_pushed_invoice_cannot_be_paid_twice(realdb, erp):
    """FeohLedger will not queue, run or book a bill the ERP holds; once the
    ERP's payment is recorded the invoice is `paid` with exactly one payment,
    and a second report or a later run changes nothing."""
    await _connect(realdb)
    inv_id = await _seed(realdb, number="SYNC-TWICE", state_data=_pushed_state("REF-TWICE"))

    async with realdb.client(key="a", role="admin") as c:
        queue = await c.get("/api/payments/queue")
        run = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]}
        )
        single = await c.post("/api/payments", json={"invoice_id": str(inv_id)})
    assert queue.status_code == 200, queue.text
    assert str(inv_id) not in queue.text
    assert run.status_code != 201, run.text
    assert single.status_code == 409, single.text
    assert await _payments(realdb, inv_id) == []

    erp.statuses["REF-TWICE"] = ErpInvoiceStatus.paid
    await erp_status_sync.run_erp_status_sync_once()
    async with realdb.client(key="a", role="admin") as c:
        again = await c.post(f"/api/invoices/{inv_id}/sync-erp-status")
        run = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]}
        )

    assert again.status_code == 409  # no longer waiting on the ERP
    assert run.status_code != 201, run.text
    [payment] = await _payments(realdb, inv_id)
    assert payment.provider == EXTERNAL_PAYMENT_PROVIDER
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.paid


# ---------------------------------------------------------------------------
# the push records where the bill lives
# ---------------------------------------------------------------------------


async def test_a_push_rests_at_sent_to_erp_and_records_its_connection(realdb, erp):
    from app.services.erp import send_to_erp_internal
    from app.services.workflow_engine import get_workflow_instance

    inv_id = await _seed(
        realdb, number="PUSH-1", status=InvoiceStatus.sending_to_erp, state_data={}
    )
    async with realdb.sessionmaker("a")() as s:
        invoice = await s.get(Invoice, inv_id)
        await send_to_erp_internal(s, invoice, erp_config=MOCK_ERP)
    async with realdb.sessionmaker("a")() as s:
        invoice = await s.get(Invoice, inv_id)
        instance = await get_workflow_instance(s, inv_id)
    assert invoice.status == InvoiceStatus.sent_to_erp
    assert instance.state_data["erp_reference"].startswith("MOCK-")
    assert instance.state_data["erp_adapter"] == "mock"
    assert instance.state_data["erp_realm"] is None

    # ...and the sweep then finds it under the same connection.
    await _connect(realdb)
    erp.statuses[instance.state_data["erp_reference"]] = ErpInvoiceStatus.paid
    result = await erp_status_sync.run_erp_status_sync_once()
    assert result.recorded_paid == 1


# ---------------------------------------------------------------------------
# POST /api/invoices/{id}/sync-erp-status
# ---------------------------------------------------------------------------


async def test_the_route_applies_one_invoices_status_now(realdb, erp):
    await _connect(realdb)
    inv_id = await _seed(realdb, number="ROUTE-1", state_data=_pushed_state("REF-ROUTE"))
    other = await _seed(realdb, number="ROUTE-2", state_data=_pushed_state("REF-OTHER"))
    erp.statuses["REF-ROUTE"] = ErpInvoiceStatus.paid
    erp.statuses["REF-OTHER"] = ErpInvoiceStatus.paid

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/sync-erp-status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "id": str(inv_id),
        "status": "paid",
        "erp_status": "paid",
        "outcome": "recorded_paid",
    }
    assert erp.asked == ["REF-ROUTE"]  # one invoice, not the tenant
    assert (await _invoice(realdb, other)).status == InvoiceStatus.sent_to_erp


async def test_the_route_refuses_what_it_cannot_ask_about(realdb, erp):
    approved = await _seed(realdb, number="ROUTE-APPROVED", status=InvoiceStatus.approved)
    elsewhere = await _seed(
        realdb,
        number="ROUTE-QBO",
        state_data=_pushed_state("X", adapter="quickbooks_online", realm="quickbooks_online:9"),
    )

    async with realdb.client(key="a", role="admin") as c:
        no_erp = await c.post(f"/api/invoices/{elsewhere}/sync-erp-status")
    assert no_erp.status_code == 400
    assert no_erp.json()["detail"]["code"] == "erp_status_not_configured"

    await _connect(realdb)
    async with realdb.client(key="a", role="admin") as c:
        not_awaiting = await c.post(f"/api/invoices/{approved}/sync-erp-status")
        other_connection = await c.post(f"/api/invoices/{elsewhere}/sync-erp-status")
        missing = await c.post(f"/api/invoices/{uuid.uuid4()}/sync-erp-status")
    assert not_awaiting.status_code == 409
    assert not_awaiting.json()["detail"]["code"] == "erp_status_not_awaiting"
    assert other_connection.status_code == 409
    assert other_connection.json()["detail"]["code"] == "erp_status_not_pushed_here"
    assert missing.status_code == 404
    assert erp.asked == []


async def test_the_route_is_refused_to_a_clerk_and_across_tenants(realdb, erp):
    await _connect(realdb)
    await _connect(realdb, "b")
    inv_id = await _seed(realdb, number="ROUTE-RBAC", state_data=_pushed_state("REF-RBAC"))

    async with realdb.client(key="a", role="ap_clerk") as c:
        clerk = await c.post(f"/api/invoices/{inv_id}/sync-erp-status")
    async with realdb.client(key="b", role="admin") as c:
        other_tenant = await c.post(f"/api/invoices/{inv_id}/sync-erp-status")

    assert clerk.status_code == 403
    assert other_tenant.status_code == 404
    assert erp.asked == []


# ---------------------------------------------------------------------------
# what the ERP says was paid, and when (money-path review)
# ---------------------------------------------------------------------------


async def test_the_erps_paid_amount_and_date_are_what_get_booked(realdb, erp):
    from datetime import date

    await _connect(realdb)
    inv_id = await _seed(realdb, number="SYNC-DATED", state_data=_pushed_state("REF-DATED"))
    erp.statuses["REF-DATED"] = ErpBillStatus(
        ErpInvoiceStatus.paid, amount_paid=Decimal("410.00"), paid_on=date(2026, 1, 5)
    )

    result = await erp_status_sync.run_erp_status_sync_once()

    assert result.recorded_paid == 1
    [payment] = await _payments(realdb, inv_id)
    assert payment.amount == Decimal("410.00")
    # Dated the day the ERP paid, not the day of the poll (1099 year, aging).
    assert payment.completed_at.date() == date(2026, 1, 5)


async def test_a_bill_part_settled_by_erp_credit_is_refused_not_booked_in_full(realdb, erp):
    """1,000 owed; the ERP applied a 400 credit and paid 600 cash. FeohLedger
    knows nothing of the credit, so booking the net owed would record 1,000 that
    never left the bank. The stated 600 does not match, so the record is
    refused and the invoice blocked for a person."""
    await _connect(realdb)
    inv_id = await _seed(
        realdb, number="SYNC-PART", state_data=_pushed_state("REF-PART"), amount="1000.00"
    )
    erp.statuses["REF-PART"] = ErpBillStatus(ErpInvoiceStatus.paid, amount_paid=Decimal("600.00"))

    result = await erp_status_sync.run_erp_status_sync_once()

    assert result.refused == 1
    assert await _payments(realdb, inv_id) == []
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.sent_to_erp
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "payment_reconciliation"
    assert "external_payment_amount_mismatch" in flag.description


@pytest.mark.parametrize(
    "erp_status", [ErpInvoiceStatus.cancelled, ErpInvoiceStatus.settled_no_payment]
)
async def test_a_payable_invoice_the_erp_no_longer_claims_is_blocked(realdb, erp, erp_status):
    """At `posted_in_erp` FeohLedger could pay the invoice, so a bill the ERP
    shows cancelled or settled without payment blocks payment."""
    await _connect(realdb)
    inv_id = await _seed(
        realdb,
        number=f"SYNC-PAYABLE-{erp_status.value}",
        status=InvoiceStatus.posted_in_erp,
        state_data=_pushed_state("REF-PAYABLE"),
    )
    erp.statuses["REF-PAYABLE"] = erp_status

    await erp_status_sync.run_erp_status_sync_once()

    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "payment_reconciliation"


async def test_voiding_the_erps_payment_does_not_reopen_the_bill_for_payment(realdb, erp):
    """The sweep records the ERP's payment; someone voids it. The invoice goes
    back to `approved`, but the ERP still holds (and paid) the bill, so a run
    must not be able to pay it again."""
    await _connect(realdb)
    inv_id = await _seed(realdb, number="SYNC-VOID", state_data=_pushed_state("REF-VOID"))
    erp.statuses["REF-VOID"] = ErpInvoiceStatus.paid
    await erp_status_sync.run_erp_status_sync_once()
    [payment] = await _payments(realdb, inv_id)

    async with realdb.client(key="a", role="admin") as c:
        voided = await c.post(
            f"/api/payments/{payment.id}/void", json={"reason": "booked against the wrong bill"}
        )
        run = await c.post(
            "/api/payments/runs", json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]}
        )

    assert voided.status_code == 200, voided.text
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.approved
    assert run.status_code != 201, run.text
    flags = [e.exception_type for e in await _exceptions(realdb, inv_id)]
    assert "payment_reconciliation" in flags


async def test_a_pushed_invoice_cannot_be_closed_to_done(realdb, erp):
    """`sent_to_erp → done` is gone: `/complete` used to fall through to it and
    strand a pushed bill in the terminal `done`, where no ERP payment lands."""
    inv_id = await _seed(realdb, number="SYNC-COMPLETE", state_data=_pushed_state("R-C"))
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/complete")
    assert resp.status_code == 409, resp.text
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.sent_to_erp


async def test_the_route_reports_an_unreachable_erp_without_its_body(realdb, erp):
    await _connect(realdb)
    inv_id = await _seed(realdb, number="ROUTE-502", state_data=_pushed_state("REF-502"))
    erp.raises.add("REF-502")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/sync-erp-status")

    assert resp.status_code == 502
    assert "secret-erp-body" not in resp.text
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.sent_to_erp
    assert await _exceptions(realdb, inv_id) == []
