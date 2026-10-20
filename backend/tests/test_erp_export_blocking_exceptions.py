"""An open duplicate or fraud exception refuses the ERP push (decisions §286).

Found live against QuickBooks Online: a clerk entered a second invoice with the
vendor and number of one already posted. The app raised the `duplicate`
warning and an open, payment-blocking `duplicate` exception, so our payment
runs would not pay it. But approval gates on no such exception, and "Send to
ERP" posted it as a second Bill. In QuickBooks a bookkeeper can pay that bill
directly, so the payment-run control no longer stood between the duplicate and
the money.

Pinned here, on real Postgres over real HTTP:

* the set is `duplicate` + `fraud_flag`, a subset of the payment-blocking set;
* each HTTP door (`/complete`, which is the modal's "Send to ERP",
  `/send-to-erp`, `/retry-erp`) answers 409 with the coded refusal before it
  moves the invoice, and queues nothing;
* the chokepoint `_call_erp` refuses before any adapter is built, so a flag
  raised after the push was queued still stops it, at `failed`, on the first
  attempt, with the code on the `invoice.erp_failed` row;
* an escalated flag still refuses, a resolved or dismissed one does not, and
  the four payment-timing holds never refuse;
* the end-to-end path: the push is refused, the implicated uploader cannot
  clear the flag (the `record_decision` segregation check), someone else can,
  and then the push goes through;
* `/complete`'s own `refresh_warnings` can find the duplicate mid-request: the
  refusal persists that exception, and once a person has dismissed it the next
  refresh does not re-open it (which would make the invoice unpostable);
* approval itself never pushes, whatever the `erp_export` step says.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select, update

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog, WorkflowInstance

TENANT = "a"
CODE = "exception_blocks_erp_export"

_ERP_ON = {
    "steps": [
        {"number": 2, "type": "approval", "enabled": True, "config": {"required": True}},
        {"number": 3, "type": "erp_export", "enabled": True, "config": {}},
    ]
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seed(
    mk,
    org_id,
    *,
    number: str,
    status: InvoiceStatus = InvoiceStatus.approved,
    uploaded_by_id: uuid.UUID | None = None,
    vendor_name: str = "Northwind Supplies",
    with_instance: bool = True,
) -> uuid.UUID:
    """An invoice whose frozen workflow has the ERP export step ON.

    `1234.56` with no dates, so no fraud rule fires on `refresh_warnings`.
    """
    from app.services.workflow_engine import get_or_create_workflow_definition

    inv_id = uuid.uuid4()
    async with mk() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=org_id,
            invoice_number=number,
            vendor_name=vendor_name,
            amount=Decimal("1234.56"),
            currency="USD",
            status=status,
            uploaded_by_id=uploaded_by_id,
            approved_by="Approver" if status != InvoiceStatus.ready_for_review else None,
        )
        s.add(inv)
        await s.flush()
        if with_instance:
            defn = await get_or_create_workflow_definition(s, org_id)
            s.add(
                WorkflowInstance(
                    correlation_id=inv.correlation_id,
                    definition_id=defn.id,
                    invoice_id=inv_id,
                    current_step=2,
                    state="active",
                    state_data={},
                    steps_config_snapshot=_ERP_ON,
                )
            )
        await s.commit()
    return inv_id


async def _open(mk, org_id, inv_id, exception_type: str, *, status: str = "open") -> uuid.UUID:
    from app.services.exception_service import create_exception

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        exc = await create_exception(
            s,
            exception_type=exception_type,
            severity="warning",
            description="detector output",
            organization_id=org_id,
            invoice=inv,
        )
        exc_id = exc.id
        await s.commit()
    if status != "open":
        async with mk() as s:
            await s.execute(
                update(APException).where(APException.id == exc_id).values(status=status)
            )
            await s.commit()
    return exc_id


async def _status(mk, inv_id) -> InvoiceStatus:
    async with mk() as s:
        return (await s.get(Invoice, inv_id)).status


async def _audit(mk, inv_id, action: str) -> list[AuditLog]:
    async with mk() as s:
        rows = await s.execute(
            select(AuditLog).where(AuditLog.entity_id == inv_id, AuditLog.action == action)
        )
        return list(rows.scalars())


@pytest.fixture
def dispatched(monkeypatch):
    """Record what the HTTP doors hand the dispatcher, and run nothing."""
    calls: list[uuid.UUID] = []

    async def _record(invoice_id, org_id, actor_id):
        calls.append(invoice_id)

    monkeypatch.setattr("app.api.workflow.dispatch_erp", _record)
    return calls


async def _push(mk, inv_id, *, adapter=None) -> None:
    """Run the dispatcher's send exactly as `erp_dispatch._run_local` does."""
    from app.services.erp import send_to_erp_internal

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        if adapter is None:
            await send_to_erp_internal(s, inv, actor_id=None, erp_config=None)
        else:
            with patch("app.services.erp.get_erp_adapter", return_value=adapter):
                await send_to_erp_internal(s, inv, actor_id=None, erp_config=None)


async def _no_sleep(_seconds) -> None:
    return None


class _SpyAdapter:
    """An adapter that must never be reached."""

    def __init__(self):
        self.posted = 0

    async def post_invoice(self, payload):  # pragma: no cover — reaching it fails the test
        self.posted += 1
        raise AssertionError("the adapter was reached")


def _assert_refusal(res, exception_type: str) -> None:
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == CODE
    assert detail["params"] == {"exception_type": exception_type}
    assert "Resolve or dismiss it in Exceptions" in detail["message"]
    # PII guard: the sentence names a fixed type, never the vendor.
    assert "Northwind" not in res.text


# ---------------------------------------------------------------------------
# the set
# ---------------------------------------------------------------------------


def test_the_set_is_duplicate_and_fraud_and_a_subset_of_the_payment_set():
    from app.api.payments import (
        ERP_EXPORT_BLOCKING_EXCEPTION_TYPES,
        PAYMENT_BLOCKING_EXCEPTION_TYPES,
    )

    assert ERP_EXPORT_BLOCKING_EXCEPTION_TYPES == ("duplicate", "fraud_flag")
    # Clearing one is the segregated sign-off only because it is payment-blocking.
    assert set(ERP_EXPORT_BLOCKING_EXCEPTION_TYPES) <= set(PAYMENT_BLOCKING_EXCEPTION_TYPES)


def test_the_refusal_code_is_registered_for_the_browser():
    from app.services.erp_adapters.base import ERP_REFUSAL_CODES
    from app.services.erp_export_gate import EXCEPTION_BLOCKS_ERP_EXPORT

    assert EXCEPTION_BLOCKS_ERP_EXPORT == CODE
    assert CODE in ERP_REFUSAL_CODES


# ---------------------------------------------------------------------------
# the HTTP doors refuse synchronously, before the transition
# ---------------------------------------------------------------------------


async def test_send_to_erp_from_the_modal_is_refused_with_an_open_duplicate(realdb, dispatched):
    """The live incident: `/complete` on an approved invoice, ERP step on."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ERPX-001")
    await _open(mk, info.org_id, inv_id, "duplicate")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/complete")

    _assert_refusal(res, "duplicate")
    assert await _status(mk, inv_id) == InvoiceStatus.approved
    assert dispatched == []
    assert await _audit(mk, inv_id, "invoice.erp_submitted") == []


async def test_send_to_erp_endpoint_is_refused_with_an_escalated_fraud_flag(realdb, dispatched):
    """Escalating hands the decision on; it does not clear the flag."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ERPX-002")
    await _open(mk, info.org_id, inv_id, "fraud_flag", status="escalated")

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")

    _assert_refusal(res, "fraud_flag")
    assert await _status(mk, inv_id) == InvoiceStatus.approved
    assert dispatched == []


async def test_retry_erp_is_refused_and_the_invoice_stays_failed(realdb, dispatched):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ERPX-003", status=InvoiceStatus.failed)
    await _open(mk, info.org_id, inv_id, "duplicate")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/retry-erp")

    _assert_refusal(res, "duplicate")
    assert await _status(mk, inv_id) == InvoiceStatus.failed
    assert dispatched == []
    assert await _audit(mk, inv_id, "invoice.erp_retried") == []


async def test_a_duplicate_outranks_a_fraud_flag_in_the_refusal(realdb, dispatched):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ERPX-004")
    await _open(mk, info.org_id, inv_id, "fraud_flag")
    await _open(mk, info.org_id, inv_id, "duplicate")

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    _assert_refusal(res, "duplicate")


@pytest.mark.parametrize(
    "exception_type",
    ["line_total_mismatch", "payment_reconciliation", "quality_hold", "po_mismatch"],
)
async def test_a_payment_timing_hold_does_not_refuse_the_push(realdb, dispatched, exception_type):
    """The liability is real; only its payment waits. Recording it is correct."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number=f"ERPX-HOLD-{exception_type}")
    await _open(mk, info.org_id, inv_id, exception_type)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert res.status_code == 202, res.text
    assert dispatched == [inv_id]


@pytest.mark.parametrize("closed", ["resolved", "dismissed"])
async def test_a_closed_duplicate_does_not_refuse_the_push(realdb, dispatched, closed):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number=f"ERPX-CLOSED-{closed}")
    await _open(mk, info.org_id, inv_id, "duplicate", status=closed)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert res.status_code == 202, res.text


# ---------------------------------------------------------------------------
# the chokepoint: every push passes `_call_erp`
# ---------------------------------------------------------------------------


async def test_a_flag_raised_after_the_push_was_queued_still_stops_it(realdb):
    """The HTTP door passed (nothing open yet); a fraud flag lands before the
    dispatcher runs. `_call_erp` refuses before building an adapter: one
    attempt, `failed`, and the code the modal localizes on the audit row."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ERPX-005", status=InvoiceStatus.sending_to_erp)
    await _open(mk, info.org_id, inv_id, "fraud_flag")

    spy = _SpyAdapter()
    await _push(mk, inv_id, adapter=spy)

    assert spy.posted == 0
    assert await _status(mk, inv_id) == InvoiceStatus.failed
    (failed,) = await _audit(mk, inv_id, "invoice.erp_failed")
    assert failed.details["refusal"] == {"code": CODE, "params": {"exception_type": "fraud_flag"}}
    assert failed.details["error"] == f"ERP post refused: {CODE} (fraud_flag)"
    assert failed.details["retries"] == 1, "a refusal must not spend the backoff budget"


async def test_call_erp_refuses_before_any_adapter_is_built():
    """Unit level: the gate runs ahead of `get_erp_adapter`, so no adapter (and
    so no HTTP client, token refresh or credential) is ever touched."""
    from types import SimpleNamespace

    from app.services.erp import ErpPostRefusedError, _call_erp

    inv_id = uuid.uuid4()

    class _Db:
        async def execute(self, stmt, *_a, **_kw):
            assert "FROM exceptions" in str(stmt), f"unexpected query: {stmt}"
            return SimpleNamespace(all=lambda: [(inv_id, "duplicate")])

    with patch("app.services.erp.get_erp_adapter") as factory:
        with pytest.raises(ErpPostRefusedError) as exc:
            await _call_erp(_Db(), SimpleNamespace(id=inv_id), {"type": "quickbooks_online"})
    factory.assert_not_called()
    assert exc.value.refusal == {"code": CODE, "params": {"exception_type": "duplicate"}}


# ---------------------------------------------------------------------------
# a refusal after an attempt whose outcome is unknown: the bill may be there
# ---------------------------------------------------------------------------


async def _set_state(mk, inv_id, state_data: dict) -> None:
    async with mk() as s:
        await s.execute(
            update(WorkflowInstance)
            .where(WorkflowInstance.invoice_id == inv_id)
            .values(state_data=state_data)
        )
        await s.commit()


async def test_a_flag_raised_during_a_503_refuses_the_next_attempt_as_possibly_posted(
    realdb, monkeypatch
):
    """The first attempt answers 503, which the ERP may still have applied; a
    fraud flag lands while it was in flight. The next attempt is refused by
    the gate, and the refusal and the `invoice.erp_failed` row say a bill may
    already be in the ERP, rather than reading as "nothing was posted"."""
    from app.services.erp_adapters.base import ErpPostResult

    monkeypatch.setattr("app.services.erp.asyncio.sleep", _no_sleep)
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ERPX-503", status=InvoiceStatus.sending_to_erp)

    class _TimesOutThenFlagged:
        def __init__(self):
            self.posted = 0

        async def post_invoice(self, payload):
            self.posted += 1
            await _open(mk, info.org_id, inv_id, "fraud_flag")
            return ErpPostResult(success=False, message="HTTP 503", retryable=True)

    adapter = _TimesOutThenFlagged()
    await _push(mk, inv_id, adapter=adapter)

    assert adapter.posted == 1, "the gate stops the second attempt"
    assert await _status(mk, inv_id) == InvoiceStatus.failed
    (failed,) = await _audit(mk, inv_id, "invoice.erp_failed")
    assert failed.details["refusal"] == {
        "code": CODE,
        "params": {"exception_type": "fraud_flag", "possibly_posted": True},
    }
    assert failed.details["possibly_posted"] is True
    assert failed.details["error"] == (
        f"ERP post refused: {CODE} (fraud_flag); "
        "a bill from an earlier attempt may already be in the ERP"
    )


async def test_retry_erp_after_a_timed_out_push_says_the_bill_may_be_posted(realdb, dispatched):
    """The 409 from `/retry-erp` carries the same hint as the audit row."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ERPX-RETRY-503", status=InvoiceStatus.failed)
    await _set_state(
        mk, inv_id, {"erp_retries": 3, "last_error": "HTTP 503", "erp_outcome_unknown": True}
    )
    await _open(mk, info.org_id, inv_id, "duplicate")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/retry-erp")

    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == CODE
    assert detail["params"] == {"exception_type": "duplicate", "possibly_posted": True}
    assert "may already have created the bill in the ERP" in detail["message"]
    assert await _status(mk, inv_id) == InvoiceStatus.failed
    assert dispatched == []


async def test_a_refusal_after_only_refusals_does_not_claim_a_bill(realdb):
    """No attempt reached the ERP: the gate refused the first one, so the second
    refusal must not claim a bill may exist (its own `last_error` is not a
    timeout)."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ERPX-TWICE", status=InvoiceStatus.sending_to_erp)
    await _open(mk, info.org_id, inv_id, "duplicate")

    await _push(mk, inv_id, adapter=_SpyAdapter())
    async with mk() as s:
        await s.execute(
            update(Invoice).where(Invoice.id == inv_id).values(status=InvoiceStatus.sending_to_erp)
        )
        await s.commit()
    await _push(mk, inv_id, adapter=_SpyAdapter())

    for row in await _audit(mk, inv_id, "invoice.erp_failed"):
        assert "possibly_posted" not in row.details
        assert row.details["refusal"]["params"] == {"exception_type": "duplicate"}


# ---------------------------------------------------------------------------
# end to end: refused, cleared by someone else, then posted
# ---------------------------------------------------------------------------


async def test_refused_then_cleared_by_a_non_implicated_user_then_posted(realdb, dispatched):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    # The admin entered this invoice; someone else approved it.
    inv_id = await _seed(mk, info.org_id, number="ERPX-006", uploaded_by_id=info.users["admin"])
    exc_id = await _open(mk, info.org_id, inv_id, "duplicate")

    async with realdb.client(key=TENANT, role="admin") as c:
        refused = await c.post(f"/api/invoices/{inv_id}/complete")
        _assert_refusal(refused, "duplicate")

        # The uploader is implicated, so they cannot clear the flag themselves.
        self_clear = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "dismiss", "resolution": "Not a duplicate."},
        )
        assert self_clear.status_code == 403, self_clear.text
        assert self_clear.json()["detail"]["code"] == "segregation_implicated"

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        cleared = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "Confirmed with the vendor: a new bill."},
        )
        assert cleared.status_code == 200, cleared.text

    async with realdb.client(key=TENANT, role="admin") as c:
        sent = await c.post(f"/api/invoices/{inv_id}/complete")
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "sending_to_erp"
    assert dispatched == [inv_id]

    # And the dispatcher's own run posts it (mock ERP) to `done`.
    await _push(mk, inv_id)
    assert await _status(mk, inv_id) == InvoiceStatus.done
    assert len(await _audit(mk, inv_id, "invoice.erp_confirmed")) == 1


# ---------------------------------------------------------------------------
# `/complete` finds the duplicate itself
# ---------------------------------------------------------------------------


async def test_a_duplicate_found_by_complete_is_persisted_and_stays_dismissed(realdb, dispatched):
    """The bill already in the ERP and its re-entry share vendor and number.
    `/complete`'s `refresh_warnings` opens the `duplicate` in the same request;
    the refusal must keep it (a 409 otherwise rolls it back, leaving nothing in
    the queue to resolve). Once a person dismisses it, the next "Send to ERP"
    must not re-open it, or the invoice could never be posted."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    await _seed(mk, info.org_id, number="QB-7001", status=InvoiceStatus.done, with_instance=False)
    inv_id = await _seed(mk, info.org_id, number="QB-7001")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        refused = await c.post(f"/api/invoices/{inv_id}/complete")
    _assert_refusal(refused, "duplicate")
    async with mk() as s:
        (row,) = (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == inv_id, APException.exception_type == "duplicate"
                )
            )
        ).scalars()
    assert row.status == "open"
    assert row.description_code == "duplicate_invoice_number"

    async with realdb.client(key=TENANT, role="admin") as c:
        dismissed = await c.post(
            f"/api/exceptions/{row.id}/resolve",
            json={"action": "dismiss", "resolution": "Vendor reuses numbers yearly."},
        )
        assert dismissed.status_code == 200, dismissed.text
        sent = await c.post(f"/api/invoices/{inv_id}/complete")
    assert sent.status_code == 200, sent.text
    assert dispatched == [inv_id]
    async with mk() as s:
        statuses = (
            await s.execute(
                select(APException.status).where(
                    APException.invoice_id == inv_id, APException.exception_type == "duplicate"
                )
            )
        ).scalars()
        assert list(statuses) == ["dismissed"], "the dismissed duplicate was re-opened"


async def test_a_new_fraud_finding_is_still_raised_after_another_was_dismissed(realdb):
    """The suppression is per finding (code + params), not per type."""
    from app.services.invoice_warning_catalog import warning
    from app.services.invoice_warnings import _ensure_exception

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ERPX-007", with_instance=False)
    round_flag = warning("round_amount", "info", amount=Decimal("5000.00"), currency="USD")
    rush_flag = warning("rush_payment", "warning", days=1)

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        await _ensure_exception(s, inv, "fraud_flag", "info", round_flag)
        await s.commit()
    async with mk() as s:
        await s.execute(
            update(APException)
            .where(APException.invoice_id == inv_id)
            .values(status="dismissed", resolved_by="Reviewer")
        )
        await s.commit()

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        await _ensure_exception(s, inv, "fraud_flag", "info", round_flag)  # same finding
        await _ensure_exception(s, inv, "fraud_flag", "warning", rush_flag)  # a new one
        await s.commit()
        rows = (
            await s.execute(
                select(APException.description_code, APException.status).where(
                    APException.invoice_id == inv_id
                )
            )
        ).all()
    assert sorted(rows) == [("round_amount", "dismissed"), ("rush_payment", "open")]


# ---------------------------------------------------------------------------
# nothing pushes automatically after approval
# ---------------------------------------------------------------------------


async def test_approval_never_pushes_even_with_the_erp_step_on(realdb, dispatched):
    """Approval advances the workflow's step pointer only; the push is a
    separate, gated request. So an approved invoice with an open duplicate sits
    at `approved` until someone sends it, and that send is refused."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ERPX-008", status=InvoiceStatus.ready_for_review)
    await _open(mk, info.org_id, inv_id, "duplicate")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        approved = await c.post(f"/api/invoices/{inv_id}/approve", json={})
        assert approved.status_code == 200, approved.text
        assert await _status(mk, inv_id) == InvoiceStatus.approved
        assert dispatched == []

        sent = await c.post(f"/api/invoices/{inv_id}/complete")
    _assert_refusal(sent, "duplicate")
    assert dispatched == []


# ---------------------------------------------------------------------------
# what a person cleared is the invoice as it stood (the fingerprint)
# ---------------------------------------------------------------------------


async def _refresh(mk, inv_id) -> None:
    from app.services.invoice_warnings import refresh_warnings

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        await refresh_warnings(s, inv, org_settings={})
        await s.commit()


async def _rows(mk, inv_id, exception_type: str = "duplicate") -> list[APException]:
    async with mk() as s:
        rows = await s.execute(
            select(APException)
            .where(APException.invoice_id == inv_id, APException.exception_type == exception_type)
            .order_by(APException.created_at)
        )
        return list(rows.scalars())


async def _duplicates(mk, inv_id) -> list[str]:
    return [r.status for r in await _rows(mk, inv_id)]


async def test_a_dismissed_duplicate_re_raises_once_the_invoice_is_re_pointed(realdb):
    """The attack a params-only match allowed: `duplicate_invoice_number` has
    no params, so one dismissal exempted the invoice from the finding. Here a
    person dismisses a legitimate collision, then the invoice is edited to
    collide with a DIFFERENT, already-paid bill. That is a finding nobody has
    judged, and it must open again and block."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    await _seed(mk, info.org_id, number="RP-100", status=InvoiceStatus.done, with_instance=False)
    await _seed(
        mk,
        info.org_id,
        number="RP-555",
        status=InvoiceStatus.paid,
        vendor_name="Contoso Paid Ltd",
        with_instance=False,
    )
    inv_id = await _seed(mk, info.org_id, number="RP-100", status=InvoiceStatus.new)
    await _refresh(mk, inv_id)
    (row,) = await _rows(mk, inv_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(
            f"/api/exceptions/{row.id}/resolve",
            json={"action": "dismiss", "resolution": "Vendor reuses numbers yearly."},
        )
        assert res.status_code == 200, res.text

    await _refresh(mk, inv_id)
    assert await _duplicates(mk, inv_id) == ["dismissed"], "an unchanged invoice stays cleared"

    async with mk() as s:
        await s.execute(
            update(Invoice)
            .where(Invoice.id == inv_id)
            .values(vendor_name="Contoso Paid Ltd", invoice_number="RP-555")
        )
        await s.commit()
    await _refresh(mk, inv_id)
    assert await _duplicates(mk, inv_id) == ["dismissed", "open"]


async def test_a_close_no_person_made_never_suppresses(realdb):
    """`record_decision` with no actor (a detector) or with `via` (an agent)
    clears the fingerprint, so the finding is raised again on the next refresh."""
    from app.services.exception_lifecycle import record_decision

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    await _seed(mk, info.org_id, number="NP-1", status=InvoiceStatus.done, with_instance=False)
    inv_id = await _seed(mk, info.org_id, number="NP-1", status=InvoiceStatus.new)
    await _refresh(mk, inv_id)

    async with mk() as s:
        (row,) = (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == inv_id, APException.exception_type == "duplicate"
                )
            )
        ).scalars()
        assert row.finding_fingerprint, "a detector-raised duplicate records what it is about"
        await record_decision(
            s,
            exception=row,
            action="resolve",
            resolution="Closed by a sweep.",
            actor_id=None,
            actor_name="system",
        )
        assert row.finding_fingerprint is None
        await s.commit()

    await _refresh(mk, inv_id)
    assert await _duplicates(mk, inv_id) == ["resolved", "open"]


async def test_params_that_drift_without_an_edit_do_not_re_raise(realdb):
    """The other half of matching on params: an LLM's wording, or a vendor's
    age in days, changes with no edit. The person judged THIS invoice, which
    has not changed, so the dismissal holds."""
    from app.services.invoice_warning_catalog import warning
    from app.services.invoice_warnings import _ensure_exception

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="DRIFT-1", with_instance=False)
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        first = warning("llm_anomaly", "warning", reason="Odd.")
        await _ensure_exception(s, inv, "fraud_flag", "warning", first)
        await s.commit()
    await _person_dismisses(mk, inv_id, "fraud_flag", info.users["ap_manager"])
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        reworded = warning("llm_anomaly", "warning", reason="Unusual, on reflection.")
        await _ensure_exception(s, inv, "fraud_flag", "warning", reworded)
        await s.commit()
    assert [r.status for r in await _rows(mk, inv_id, "fraud_flag")] == ["dismissed"]


async def _person_dismisses(mk, inv_id, exception_type: str, actor_id: uuid.UUID) -> None:
    """The queue's dismiss, through `record_decision`, as a named person."""
    from app.services.exception_lifecycle import record_decision

    async with mk() as s:
        (row,) = (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == inv_id,
                    APException.exception_type == exception_type,
                    APException.status.in_(["open", "escalated"]),
                )
            )
        ).scalars()
        await record_decision(
            s,
            exception=row,
            action="dismiss",
            resolution="Checked.",
            actor_id=actor_id,
            actor_name="Reviewer",
        )
        await s.commit()


async def test_an_edit_made_while_a_person_decides_does_not_ride_their_verdict(realdb):
    """The race the second review found. The finding is raised against vendor X
    / RP2-100 (a legitimate collision). While the manager reads the queue, the
    clerk re-points the invoice at a different, already-paid bill; the refresh
    finds the open row and leaves its stamp alone. The manager's dismissal
    therefore does not match the invoice as it now stands, binds nothing, and
    the next refresh raises the finding again for a person to see."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    await _seed(mk, info.org_id, number="RP2-100", status=InvoiceStatus.done, with_instance=False)
    await _seed(
        mk,
        info.org_id,
        number="RP2-555",
        status=InvoiceStatus.paid,
        vendor_name="Fabrikam Paid Ltd",
        with_instance=False,
    )
    inv_id = await _seed(mk, info.org_id, number="RP2-100", status=InvoiceStatus.new)
    await _refresh(mk, inv_id)

    async with mk() as s:  # the clerk's edit, mid-decision
        await s.execute(
            update(Invoice)
            .where(Invoice.id == inv_id)
            .values(vendor_name="Fabrikam Paid Ltd", invoice_number="RP2-555")
        )
        await s.commit()
    await _refresh(mk, inv_id)
    assert await _duplicates(mk, inv_id) == ["open"], "the open row is not duplicated"

    await _person_dismisses(mk, inv_id, "duplicate", info.users["ap_manager"])
    (row,) = await _rows(mk, inv_id)
    assert row.finding_fingerprint is None, "a verdict on another state binds nothing"
    await _refresh(mk, inv_id)
    assert await _duplicates(mk, inv_id) == ["dismissed", "open"]


async def test_an_edit_reverted_before_the_dismissal_still_binds_nothing(realdb):
    """Raised at S1 (a real duplicate); the clerk edits to a clean-looking S2
    while the manager reads the queue, then reverts to S1 just before the
    click. The invoice hashes as raised again, but the manager may have judged
    S2, so the refresh that saw S2 voided the stamp for good."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    await _seed(mk, info.org_id, number="RV-100", status=InvoiceStatus.paid, with_instance=False)
    inv_id = await _seed(mk, info.org_id, number="RV-100", status=InvoiceStatus.new)
    await _refresh(mk, inv_id)

    for number in ("RV-CLEAN-1", "RV-100"):  # S2, then back to S1
        async with mk() as s:
            await s.execute(
                update(Invoice).where(Invoice.id == inv_id).values(invoice_number=number)
            )
            await s.commit()
        await _refresh(mk, inv_id)

    await _person_dismisses(mk, inv_id, "duplicate", info.users["ap_manager"])
    (row,) = await _rows(mk, inv_id)
    assert row.finding_fingerprint is None
    await _refresh(mk, inv_id)
    assert await _duplicates(mk, inv_id) == ["dismissed", "open"]


async def test_an_agent_escalation_keeps_the_stamp_so_a_later_dismissal_holds(realdb, dispatched):
    """Agents escalate `duplicate` rows rather than resolve them. That is not a
    clearing decision, so it must not drop the stamp: a person dismissing the
    escalated row afterwards still lets the invoice be sent."""
    from app.services.exception_lifecycle import record_decision

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    await _seed(mk, info.org_id, number="AG-7001", status=InvoiceStatus.done, with_instance=False)
    inv_id = await _seed(mk, info.org_id, number="AG-7001")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        _assert_refusal(await c.post(f"/api/invoices/{inv_id}/complete"), "duplicate")

    async with mk() as s:
        (row,) = (
            await s.execute(select(APException).where(APException.invoice_id == inv_id))
        ).scalars()
        await record_decision(
            s,
            exception=row,
            action="escalate",
            resolution="Needs a human.",
            actor_id=info.users["admin"],
            actor_name="agent",
            via="agent",
        )
        await s.commit()
        assert row.finding_fingerprint

    async with realdb.client(key=TENANT, role="admin") as c:
        dismissed = await c.post(
            f"/api/exceptions/{row.id}/resolve",
            json={"action": "dismiss", "resolution": "Vendor reuses numbers yearly."},
        )
        assert dismissed.status_code == 200, dismissed.text
        sent = await c.post(f"/api/invoices/{inv_id}/complete")
    assert sent.status_code == 200, sent.text
    assert dispatched == [inv_id]


async def test_a_vendor_email_domain_change_re_raises_a_dismissed_flag(realdb):
    """`personal_email_domain` reads the VENDOR's email, which no invoice field
    records, so its `domain` param is part of what the person judged."""
    from app.services.invoice_warning_catalog import warning
    from app.services.invoice_warnings import _ensure_exception

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="MAIL-1", with_instance=False)

    async def _raise(domain: str) -> None:
        async with mk() as s:
            inv = await s.get(Invoice, inv_id)
            flag = warning("personal_email_domain", "warning", domain=domain)
            await _ensure_exception(s, inv, "fraud_flag", "warning", flag)
            await s.commit()

    await _raise("gmail.com")
    await _person_dismisses(mk, inv_id, "fraud_flag", info.users["ap_manager"])
    await _raise("gmail.com")
    assert [r.status for r in await _rows(mk, inv_id, "fraud_flag")] == ["dismissed"]
    await _raise("yahoo.com")
    assert [r.status for r in await _rows(mk, inv_id, "fraud_flag")] == ["dismissed", "open"]


# ---------------------------------------------------------------------------
# the vendor bank-change flag refuses the push too
# ---------------------------------------------------------------------------


async def test_a_vendor_bank_change_flag_refuses_the_push_of_its_approved_invoices(
    realdb, dispatched
):
    """Approving a vendor's bank change opens a `fraud_flag` on each of its
    approved invoices, and that refuses the push as well (decisions §286): the
    change may be the BEC itself, and once the bill is in the ERP it can be
    paid there against the ERP's own vendor record."""
    from app.api.vendors import _flag_payable_invoices_for_bank_change
    from app.models.vendor import Vendor

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="BANK-1")
    async with mk() as s:
        vendor = Vendor(id=uuid.uuid4(), organization_id=info.org_id, name="Bank Change Co")
        s.add(vendor)
        await s.flush()
        await s.execute(update(Invoice).where(Invoice.id == inv_id).values(vendor_id=vendor.id))
        await _flag_payable_invoices_for_bank_change(s, vendor=vendor, actor_id=info.users["admin"])
        await s.commit()

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    _assert_refusal(res, "fraud_flag")
    assert dispatched == []


async def _bank_change(mk, org_id, inv_ids, *, actor_id) -> None:
    """Point the invoices at one vendor and approve that vendor's bank change."""
    from app.api.vendors import _flag_payable_invoices_for_bank_change
    from app.models.vendor import Vendor

    async with mk() as s:
        vendor = Vendor(id=uuid.uuid4(), organization_id=org_id, name="Bank Change Co")
        s.add(vendor)
        await s.flush()
        await s.execute(update(Invoice).where(Invoice.id.in_(inv_ids)).values(vendor_id=vendor.id))
        await _flag_payable_invoices_for_bank_change(s, vendor=vendor, actor_id=actor_id)
        await s.commit()


def test_the_bank_change_flag_covers_every_status_still_short_of_payment():
    """Derived from `VALID_TRANSITIONS`, so it is every status an approved
    invoice passes through on the way to `paid` — the ERP push's states
    included, not only the payment queue's."""
    from app.api.vendors import _BANK_CHANGE_FLAG_STATUSES

    assert _BANK_CHANGE_FLAG_STATUSES == {
        InvoiceStatus.approved,
        InvoiceStatus.sending_to_erp,
        InvoiceStatus.sent_to_erp,
        InvoiceStatus.failed,
        InvoiceStatus.posted_in_erp,
        InvoiceStatus.payment_scheduled,
    }


async def test_a_bank_change_flags_a_failed_push_and_retry_erp_is_refused(realdb, dispatched):
    """The gap the hand-listed set left: the push hit 503s and landed at
    `failed`, the vendor's bank change was then approved, and `/retry-erp`
    posted the bill, payable from the ERP to the new bank details."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="BANK-FAILED", status=InvoiceStatus.failed)
    await _bank_change(mk, info.org_id, [inv_id], actor_id=info.users["admin"])

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/retry-erp")

    _assert_refusal(res, "fraud_flag")
    assert await _status(mk, inv_id) == InvoiceStatus.failed
    assert dispatched == []
    assert await _audit(mk, inv_id, "invoice.erp_retried") == []


async def test_a_bank_change_flags_an_invoice_mid_push_and_its_next_attempt_is_refused(realdb):
    """`sending_to_erp`: the queued attempt re-reads the gate, so the flag
    raised while it waited stops it before any adapter is built."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(
        mk, info.org_id, number="BANK-SENDING", status=InvoiceStatus.sending_to_erp
    )
    await _bank_change(mk, info.org_id, [inv_id], actor_id=info.users["admin"])

    spy = _SpyAdapter()
    await _push(mk, inv_id, adapter=spy)

    assert spy.posted == 0
    assert await _status(mk, inv_id) == InvoiceStatus.failed
    [row] = await _audit(mk, inv_id, "invoice.erp_failed")
    assert row.details["refusal"]["code"] == CODE


async def test_a_bank_change_flags_sent_to_erp_but_not_paid_or_a_failed_extraction(realdb):
    """`sent_to_erp` walks on to `posted_in_erp` and into the payment queue, so
    it is flagged. `paid` has nothing left to stop, and a `failed` invoice that
    was never approved is a failed extraction, which reaches money only through
    an approval made after the change."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    sent = await _seed(mk, info.org_id, number="BANK-SENT", status=InvoiceStatus.sent_to_erp)
    paid = await _seed(mk, info.org_id, number="BANK-PAID", status=InvoiceStatus.paid)
    extraction_failed = await _seed(
        mk, info.org_id, number="BANK-XFAIL", status=InvoiceStatus.failed
    )
    async with mk() as s:
        await s.execute(
            update(Invoice)
            .where(Invoice.id == extraction_failed)
            .values(approved_by=None, approval_date=None)
        )
        await s.commit()

    await _bank_change(
        mk, info.org_id, [sent, paid, extraction_failed], actor_id=info.users["admin"]
    )

    assert [r.status for r in await _rows(mk, sent, "fraud_flag")] == ["open"]
    assert await _rows(mk, paid, "fraud_flag") == []
    assert await _rows(mk, extraction_failed, "fraud_flag") == []


async def _paid_invoice_with_payment(mk, org_id, *, creator_id):
    """A `paid` invoice for its own vendor, and the completed payment that paid it."""
    from datetime import UTC, datetime, timedelta

    from app.models.payment import Payment, PaymentRun
    from app.models.vendor import Vendor

    inv_id = await _seed(
        mk, org_id, number=f"VOID-{uuid.uuid4().hex[:6]}", status=InvoiceStatus.paid
    )
    pay_id = uuid.uuid4()
    async with mk() as s:
        vendor = Vendor(id=uuid.uuid4(), organization_id=org_id, name="Void Bank Co")
        s.add(vendor)
        await s.flush()
        inv = await s.get(Invoice, inv_id)
        inv.vendor_id = vendor.id
        run = PaymentRun(
            organization_id=org_id,
            status="completed",
            total_amount=inv.amount,
            initiated_by=creator_id,
            requires_cfo_approval=False,
        )
        s.add(run)
        await s.flush()
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                payment_run_id=run.id,
                amount=inv.amount,
                method="ach",
                status="completed",
                correlation_id=inv.correlation_id,
                created_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
        await s.commit()
    return inv_id, pay_id, vendor.id


async def _approved_bank_change(mk, org_id, vendor_id, *, reviewer_id, days_ago: int) -> None:
    from datetime import UTC, datetime, timedelta

    from app.models.vendor_change_request import VendorChangeRequest

    async with mk() as s:
        s.add(
            VendorChangeRequest(
                vendor_id=vendor_id,
                organization_id=org_id,
                requested_by_user_id=uuid.uuid4(),
                change_type="bank_details",
                status="approved",
                proposed_value={"bank_details": {}},
                reviewed_by_user_id=reviewer_id,
                reviewed_at=datetime.now(UTC) - timedelta(days=days_ago),
            )
        )
        await s.commit()


async def _void_payment(realdb, pay_id, org_id, actor_id):
    from types import SimpleNamespace

    from app.api.payments import VoidPaymentRequest, void_payment

    async with realdb.sessionmaker(TENANT)() as db:
        await void_payment(
            payment_id=pay_id,
            body=VoidPaymentRequest(reason="sent twice"),
            db=db,
            org=SimpleNamespace(
                id=org_id,
                name="PyTest",
                slug="pytesta",
                settings={"payments": {"provider": "mock"}},
            ),
            user=SimpleNamespace(id=actor_id, full_name="Void Tester", roles=["admin"]),
            entity_id=None,
        )


async def test_voiding_a_payment_made_before_a_bank_change_reflags_the_invoice(realdb, dispatched):
    """The change skipped the invoice while it was `paid` (nothing left to stop).
    The void sends it back to `approved`, and the re-payment would go to the
    NEW account — so the void raises the flag the change would have, stamped
    with the change's approver so that person cannot clear it themselves."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id, pay_id, vendor_id = await _paid_invoice_with_payment(
        mk, info.org_id, creator_id=info.users["admin"]
    )
    await _approved_bank_change(
        mk, info.org_id, vendor_id, reviewer_id=info.users["ap_manager"], days_ago=1
    )

    await _void_payment(realdb, pay_id, info.org_id, info.users["admin"])

    async with mk() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.approved
    flags = await _rows(mk, inv_id, "fraud_flag")
    assert [(f.status, f.raised_by_user_id) for f in flags] == [("open", info.users["ap_manager"])]
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    _assert_refusal(res, "fraud_flag")
    assert dispatched == []


async def test_voiding_a_payment_made_after_the_bank_change_raises_nothing(realdb):
    """A change approved BEFORE the payment was already the destination that
    payment used; voiding it re-pays the same account, so no flag."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id, pay_id, vendor_id = await _paid_invoice_with_payment(
        mk, info.org_id, creator_id=info.users["admin"]
    )
    await _approved_bank_change(
        mk, info.org_id, vendor_id, reviewer_id=info.users["ap_manager"], days_ago=5
    )

    await _void_payment(realdb, pay_id, info.org_id, info.users["admin"])

    assert await _rows(mk, inv_id, "fraud_flag") == []
