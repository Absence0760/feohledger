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
* approval does not push while the step's `auto_send_on_approval` is off (an
  absent key reads as off); with it on, approval's auto-send meets the same
  gate and leaves the invoice `approved` (`test_erp_auto_send_on_approval.py`,
  decisions §304).
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select, text, update

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

    async def _record(invoice_id, org_id, actor_id, **_kwargs):
        calls.append(invoice_id)

    monkeypatch.setattr("app.services.erp_send.dispatch_erp", _record)
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


async def test_approval_does_not_push_with_auto_send_off(realdb, dispatched):
    """`_ERP_ON` enables the step without `auto_send_on_approval`, which reads as
    off (decisions §304): approval advances the step pointer only and the push
    is a separate, gated request. So an approved invoice with an open duplicate
    sits at `approved` until someone sends it, and that send is refused."""
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


# ---------------------------------------------------------------------------
# FeohLedger already paying the invoice refuses the push (decisions §309)
# ---------------------------------------------------------------------------
#
# Staging a run books a live `pending` payment and leaves the invoice
# `approved`; so does an `exported` NACHA run; a minted card leaves it
# `approved` too. Pushed then, the bill is payable from the ERP as well. The
# gate asks the rails' own "live" predicates, so a voided / failed / cancelled
# payment or a cancelled card no longer holds the invoice.

PAID_CODE = "payment_blocks_erp_export"


async def _stage_payment(
    mk, org_id, inv_id, *, run_status: str = "draft", payment_status: str = "pending"
) -> uuid.UUID:
    """A run holding one payment for the invoice, the invoice left as it was."""
    from app.models.payment import Payment, PaymentRun

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        run = PaymentRun(
            organization_id=org_id,
            status=run_status,
            total_amount=inv.amount,
            requires_cfo_approval=False,
        )
        s.add(run)
        await s.flush()
        payment = Payment(
            invoice_id=inv_id,
            payment_run_id=run.id,
            amount=inv.amount,
            method="ach",
            status=payment_status,
            correlation_id=inv.correlation_id,
        )
        s.add(payment)
        await s.commit()
        return payment.id


async def _mint_card(mk, org_id, inv_id, *, status: str = "active") -> uuid.UUID:
    from app.models.virtual_card import VirtualCard

    async with mk() as s:
        card = VirtualCard(
            organization_id=org_id,
            invoice_id=inv_id,
            card_provider="mock",
            provider_card_id=f"mock-{uuid.uuid4().hex[:8]}",
            amount_limit=Decimal("1234.56"),
            currency="USD",
            status=status,
            last_four="4242",
        )
        s.add(card)
        await s.commit()
        return card.id


def _assert_paid_refusal(res, claim: str) -> None:
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == PAID_CODE
    assert detail["params"] == {"claim": claim}
    assert "before sending the invoice to the ERP" in detail["message"]
    assert "Northwind" not in res.text


def test_the_paid_refusal_code_is_registered_for_the_browser():
    from app.services.erp_adapters.base import ERP_REFUSAL_CODES
    from app.services.erp_export_gate import PAYMENT_BLOCKS_ERP_EXPORT

    assert PAYMENT_BLOCKS_ERP_EXPORT == PAID_CODE
    assert PAID_CODE in ERP_REFUSAL_CODES


@pytest.mark.parametrize("door", ["send-to-erp", "complete"])
async def test_a_pending_payment_in_a_draft_run_refuses_the_push(realdb, dispatched, door):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number=f"PAYX-DRAFT-{door}")
    await _stage_payment(mk, info.org_id, inv_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/{door}")

    _assert_paid_refusal(res, "payment")
    assert await _status(mk, inv_id) == InvoiceStatus.approved
    assert dispatched == []
    assert await _audit(mk, inv_id, "invoice.erp_submitted") == []


async def test_an_exported_nacha_run_refuses_the_push(realdb, dispatched):
    """The bank holds the file: the payment is still `pending`, and live."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="PAYX-EXPORTED")
    await _stage_payment(mk, info.org_id, inv_id, run_status="exported")

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")

    _assert_paid_refusal(res, "payment")
    assert dispatched == []


async def test_a_live_virtual_card_refuses_the_push(realdb, dispatched):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="PAYX-CARD")
    await _mint_card(mk, info.org_id, inv_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")

    _assert_paid_refusal(res, "virtual_card")
    assert await _status(mk, inv_id) == InvoiceStatus.approved
    assert dispatched == []


@pytest.mark.parametrize("dead", ["voided", "failed", "cancelled"])
async def test_a_payment_no_longer_live_does_not_refuse_the_push(realdb, dispatched, dead):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number=f"PAYX-DEAD-{dead}")
    await _stage_payment(mk, info.org_id, inv_id, payment_status=dead)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert res.status_code == 202, res.text
    assert dispatched == [inv_id]


async def test_a_cancelled_card_does_not_refuse_the_push(realdb, dispatched):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="PAYX-CARD-CANCELLED")
    await _mint_card(mk, info.org_id, inv_id, status="cancelled")

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert res.status_code == 202, res.text


async def test_no_payment_and_no_card_does_not_refuse_the_push(realdb, dispatched):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="PAYX-NONE")

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert res.status_code == 202, res.text
    assert dispatched == [inv_id]


async def test_an_open_duplicate_is_named_before_a_live_payment(realdb, dispatched):
    """Both hold the invoice; the §286 refusal comes first, as it always did."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="PAYX-BOTH")
    await _stage_payment(mk, info.org_id, inv_id)
    await _open(mk, info.org_id, inv_id, "duplicate")

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    _assert_refusal(res, "duplicate")


async def test_staging_a_run_then_cancelling_it_refuses_then_allows_the_push(realdb, dispatched):
    """End to end through the real run builder: `POST /api/payments/runs`
    leaves the invoice `approved` with a live payment, so the push is refused;
    cancelling the run releases it, and the push goes through."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="PAYX-E2E")

    async with realdb.client(key=TENANT, role="admin") as c:
        staged = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": str(inv_id), "method": "ach"}]},
        )
        assert staged.status_code == 201, staged.text
        assert await _status(mk, inv_id) == InvoiceStatus.approved

        _assert_paid_refusal(await c.post(f"/api/invoices/{inv_id}/send-to-erp"), "payment")
        assert dispatched == []

        run_id = staged.json()["id"]
        cancelled = await c.post(f"/api/payments/runs/{run_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text

        sent = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert sent.status_code == 202, sent.text
    assert dispatched == [inv_id]


async def test_retry_erp_with_a_live_payment_says_the_bill_may_be_posted(realdb, dispatched):
    """A timed-out push left the invoice `failed` with a live payment beside
    it (rows from before this gate): the retry is refused and says the ERP may
    already hold a bill FeohLedger is also paying."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="PAYX-RETRY", status=InvoiceStatus.failed)
    await _set_state(
        mk, inv_id, {"erp_retries": 3, "last_error": "HTTP 503", "erp_outcome_unknown": True}
    )
    await _stage_payment(mk, info.org_id, inv_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/retry-erp")

    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == PAID_CODE
    assert detail["params"] == {"claim": "payment", "possibly_posted": True}
    assert "may already have created the bill in the ERP" in detail["message"]
    assert await _status(mk, inv_id) == InvoiceStatus.failed
    assert dispatched == []


@pytest.mark.parametrize("claim", ["payment", "virtual_card"])
async def test_a_claim_made_after_the_push_was_queued_stops_it_at_call_erp(realdb, claim):
    """The door passed; a payment or card is live by the time the dispatcher
    runs. `_call_erp` refuses before building an adapter, at `failed`, on the
    first attempt, with the code on the `invoice.erp_failed` row."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(
        mk, info.org_id, number=f"PAYX-QUEUED-{claim}", status=InvoiceStatus.sending_to_erp
    )
    if claim == "payment":
        await _stage_payment(mk, info.org_id, inv_id)
    else:
        await _mint_card(mk, info.org_id, inv_id)

    spy = _SpyAdapter()
    await _push(mk, inv_id, adapter=spy)

    assert spy.posted == 0
    assert await _status(mk, inv_id) == InvoiceStatus.failed
    (failed,) = await _audit(mk, inv_id, "invoice.erp_failed")
    assert failed.details["refusal"] == {"code": PAID_CODE, "params": {"claim": claim}}
    assert failed.details["error"] == f"ERP post refused: {PAID_CODE} ({claim})"
    assert failed.details["retries"] == 1, "a refusal must not spend the backoff budget"


async def test_call_erp_refuses_a_live_payment_before_any_adapter_is_built():
    """Unit level: no open exception, a live payment; no adapter is built."""
    from types import SimpleNamespace

    from app.services.erp import ErpPostRefusedError, _call_erp

    inv_id = uuid.uuid4()

    class _Db:
        async def execute(self, stmt, *_a, **_kw):
            sql = str(stmt)
            if "FROM exceptions" in sql:
                return SimpleNamespace(all=lambda: [])
            assert "JOIN payments" in sql, f"unexpected query: {sql}"
            return SimpleNamespace(all=lambda: [(inv_id, "INV-1")])

    with patch("app.services.erp.get_erp_adapter") as factory:
        with pytest.raises(ErpPostRefusedError) as exc:
            await _call_erp(_Db(), SimpleNamespace(id=inv_id), {"type": "quickbooks_online"})
    factory.assert_not_called()
    assert exc.value.refusal == {"code": PAID_CODE, "params": {"claim": "payment"}}


# ---------------------------------------------------------------------------
# The run builder and the send serialise on the invoice row
# ---------------------------------------------------------------------------
#
# `/send-to-erp` holds the invoice `FOR UPDATE` across its export gate and its
# `approved → sending_to_erp` transition. The run builder used to read its
# invoices with a plain SELECT: a run staged inside that window saw `approved`
# and no live payment, and its payment insert's `FOR KEY SHARE` only waited for
# the send's commit before booking a `pending` payment beside the push. It now
# locks the invoices `FOR NO KEY UPDATE` before any check
# (`payment_runs.lock_run_invoices`), so the two serialise in either order.


async def _a_backend_waits_on_a_lock(mk, *, timeout_s: float = 10.0) -> bool:
    """Poll `pg_stat_activity` until a backend on this tenant DB is queued on a
    lock: the real signal that the second request reached it, not a sleep."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        async with mk() as s:
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if waiting:
            return True
        await asyncio.sleep(0.02)
    return False


async def _payments_for(mk, inv_id) -> list:
    from app.models.payment import Payment

    async with mk() as s:
        rows = await s.execute(select(Payment).where(Payment.invoice_id == inv_id))
        return list(rows.scalars())


def _run_body(inv_id) -> dict:
    return {"items": [{"invoice_id": str(inv_id), "method": "ach"}]}


async def test_a_run_staged_while_the_send_holds_the_invoice_waits_and_is_refused(
    realdb, dispatched, monkeypatch
):
    """The send has passed its gate (no live payment) and still holds the
    invoice. The run queues on the lock, then sees `sending_to_erp` and 409s:
    no payment is booked beside the push."""
    import app.services.erp_export_gate as gate

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="RACE-SEND-FIRST")

    async with realdb.client(key=TENANT, role="admin") as c:
        run: dict = {}
        original = gate.erp_export_refusal

        async def gate_then_start_the_run(db, invoice_id, *, possibly_posted):
            refused = await original(db, invoice_id, possibly_posted=possibly_posted)
            run["task"] = asyncio.create_task(c.post("/api/payments/runs", json=_run_body(inv_id)))
            run["queued"] = await _a_backend_waits_on_a_lock(mk)
            return refused

        monkeypatch.setattr(gate, "erp_export_refusal", gate_then_start_the_run)
        sent = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
        staged = await run["task"]

    assert sent.status_code == 202, sent.text
    assert dispatched == [inv_id]
    assert run["queued"], "the run must queue on the invoice lock, not read past it"
    assert staged.status_code == 409, staged.text
    assert "not approved for payment" in staged.json()["detail"]
    assert await _payments_for(mk, inv_id) == []
    assert await _status(mk, inv_id) == InvoiceStatus.sending_to_erp


async def test_a_send_made_while_the_run_holds_the_invoice_waits_and_is_refused(
    realdb, dispatched, monkeypatch
):
    """The other order: the run holds the invoice and has booked its payment.
    The send queues on the lock, then its gate sees the live payment and 409s
    `payment_blocks_erp_export`; the invoice stays `approved`."""
    import app.services.audit_dispatch as audit_dispatch

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="RACE-RUN-FIRST")

    async with realdb.client(key=TENANT, role="admin") as c:
        send: dict = {}
        original = audit_dispatch.dispatch_audit

        async def audit_then_start_the_send(db, **kwargs):
            result = await original(db, **kwargs)
            if kwargs.get("action") == "payment_run.created" and "task" not in send:
                send["task"] = asyncio.create_task(c.post(f"/api/invoices/{inv_id}/send-to-erp"))
                send["queued"] = await _a_backend_waits_on_a_lock(mk)
            return result

        monkeypatch.setattr(audit_dispatch, "dispatch_audit", audit_then_start_the_send)
        staged = await c.post("/api/payments/runs", json=_run_body(inv_id))
        sent = await send["task"]

    assert staged.status_code == 201, staged.text
    assert send["queued"], "the send must queue on the invoice lock"
    _assert_paid_refusal(sent, "payment")
    assert dispatched == []
    assert await _status(mk, inv_id) == InvoiceStatus.approved
    assert [p.status for p in await _payments_for(mk, inv_id)] == ["pending"]


async def test_a_run_refuses_invoice_locked_past_the_bound_and_books_nothing(realdb, monkeypatch):
    """A holder of the invoice lock (a dispatch across its processor call)
    makes the run wait at most `payment_invoice_lock_timeout_ms`, then refuse by
    name with nothing booked. Retry-safe: once the holder is gone it stages."""
    from app.api.payments import INVOICE_LOCKED_DETAIL
    from app.config import settings

    monkeypatch.setattr(settings, "payment_invoice_lock_timeout_ms", 200)
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="RACE-HELD")

    async with realdb.client(key=TENANT, role="admin") as c:
        async with mk() as holder:
            await holder.execute(
                text("SELECT id FROM invoices WHERE id = :id FOR NO KEY UPDATE"), {"id": inv_id}
            )
            refused = await c.post("/api/payments/runs", json=_run_body(inv_id))
            await holder.rollback()
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"] == INVOICE_LOCKED_DETAIL
        assert await _payments_for(mk, inv_id) == []

        staged = await c.post("/api/payments/runs", json=_run_body(inv_id))
    assert staged.status_code == 201, staged.text


async def test_a_plan_replay_that_waited_on_the_lock_returns_the_winners_run(realdb):
    """The copilot's enact is idempotent on `plan_id`. A replay that queued on
    the invoice lock behind the first request must return that run, not 409 on
    the payment the first request just made live."""
    from app.models.entity import Entity
    from app.services.payment_runs import PaymentRunItemInput, create_payment_run_for_invoices

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="RACE-PLAN")
    async with mk() as s:
        entity_id = (
            await s.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
        ).scalar_one()
    org = SimpleNamespace(id=info.org_id, name="PyTest", slug=info.slug, settings={})
    user = SimpleNamespace(id=info.users["admin"], full_name="Plan Tester")
    plan_id = f"plan-{uuid.uuid4().hex}"

    async def stage(db):
        return await create_payment_run_for_invoices(
            db,
            org=org,
            org_id=info.org_id,
            entity_id=entity_id,
            scope_entity_id=None,
            user=user,
            items=[PaymentRunItemInput(invoice_id=inv_id, method="ach")],
            plan_id=plan_id,
        )

    async with mk() as first_db, mk() as replay_db:
        first = await stage(first_db)
        assert first.created is True
        first_run_id = first.run.id
        replay_task = asyncio.create_task(stage(replay_db))
        assert await _a_backend_waits_on_a_lock(mk), "the replay must queue on the lock"
        await first_db.commit()
        replay = await replay_task
        replay_run_id = replay.run.id
        await replay_db.rollback()

    assert replay.created is False
    assert replay_run_id == first_run_id
    assert len(await _payments_for(mk, inv_id)) == 1


# ---------------------------------------------------------------------------
# A vendor the payment path will not pay refuses the push
# ---------------------------------------------------------------------------
#
# A payment run refuses a vendor that is not `active`; dispatch refuses one
# that is `payments_blocked` and refuses or holds one whose last screen is a
# sanctions `match` / `review`. A bill in the ERP is paid past all of that, so
# the push refuses a blocked, sanctions-held, rejected or unknown-status
# vendor. An `unverified` or `inactive` vendor's approved bill is a real debt
# and still books: verification gates the PAYMENT, which the run gate still
# holds here.

VENDOR_CODE = "vendor_blocks_erp_export"

#: case → (status, payments_blocked, screening_status, expected hold or None)
_VENDOR_CASES = {
    "blocked": ("active", True, "clear", "payments_blocked"),
    # A sanctions match sets the block; a match left after an unblock still
    # refuses, as the pre-payment screen would refuse it again.
    "sanctions_match": ("active", False, "match", "sanctions_match"),
    "sanctions_match_and_blocked": ("active", True, "match", "payments_blocked"),
    "sanctions_review": ("active", False, "review", "sanctions_review"),
    "rejected": ("rejected", False, "clear", "vendor_rejected"),
    "unknown_status": ("archived", False, "clear", "vendor_status_unknown"),
    # The sticky block outranks a status the push would otherwise book.
    "unverified_but_blocked": ("unverified", True, "unscreened", "payments_blocked"),
    "unverified": ("unverified", False, "unscreened", None),
    "inactive": ("inactive", False, "clear", None),
    "active": ("active", False, "clear", None),
    "active_unscreened": ("active", False, "unscreened", None),
}
_REFUSED = [k for k, v in _VENDOR_CASES.items() if v[3] is not None]
_BOOKED = [k for k, v in _VENDOR_CASES.items() if v[3] is None]


async def _link_vendor(mk, org_id, inv_id, case: str) -> uuid.UUID:
    """A vendor in `case`'s state, created a year ago (so no new-vendor fraud
    rule fires on `/complete`), linked to the invoice."""
    from datetime import UTC, datetime, timedelta

    from app.models.vendor import Vendor

    status, blocked, screening, _ = _VENDOR_CASES[case]
    async with mk() as s:
        vendor = Vendor(
            organization_id=org_id,
            name="Northwind Supplies",
            status=status,
            payments_blocked=blocked,
            payments_blocked_reason="manually blocked by AP" if blocked else None,
            screening_status=screening,
        )
        s.add(vendor)
        await s.flush()
        vendor.created_at = datetime.now(UTC) - timedelta(days=400)
        (await s.get(Invoice, inv_id)).vendor_id = vendor.id
        await s.commit()
        return vendor.id


def _assert_vendor_refusal(res, hold: str, **extra) -> None:
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == VENDOR_CODE
    assert detail["params"] == {"hold": hold, **extra}
    assert "send" in detail["message"] and "ERP" in detail["message"]
    # PII guard: the sentence and params name a fixed hold, never the vendor.
    assert "Northwind" not in res.text


def test_the_vendor_refusal_code_is_registered_for_the_browser():
    from app.services.erp_adapters.base import ERP_REFUSAL_CODES
    from app.services.erp_export_gate import VENDOR_BLOCKS_ERP_EXPORT

    assert VENDOR_BLOCKS_ERP_EXPORT == VENDOR_CODE
    assert VENDOR_CODE in ERP_REFUSAL_CODES


def test_every_hold_has_a_409_sentence():
    """The screening gate's holds are the payment path's constants; the 409
    has a sentence for each, and for the gate's own two."""
    from app.services import erp_export_gate as gate
    from app.services import payment_runs as pr

    holds = {
        pr.HOLD_PAYMENTS_BLOCKED,
        pr.HOLD_SANCTIONS_MATCH,
        pr.HOLD_SANCTIONS_REVIEW,
        gate.HOLD_VENDOR_REJECTED,
        gate.HOLD_VENDOR_STATUS_UNKNOWN,
    }
    assert set(gate._HOLD_SENTENCE) == holds
    assert {v[3] for v in _VENDOR_CASES.values()} - {None} == holds


@pytest.mark.parametrize("door", ["send-to-erp", "complete", "retry-erp"])
@pytest.mark.parametrize("case", _REFUSED)
async def test_a_vendor_the_payment_path_will_not_pay_refuses_every_door(
    realdb, dispatched, case, door
):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    start = InvoiceStatus.failed if door == "retry-erp" else InvoiceStatus.approved
    inv_id = await _seed(mk, info.org_id, number=f"VNDX-{case}-{door}"[:50], status=start)
    await _link_vendor(mk, info.org_id, inv_id, case)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/{door}")

    _assert_vendor_refusal(res, _VENDOR_CASES[case][3])
    assert await _status(mk, inv_id) == start
    assert dispatched == []
    assert await _audit(mk, inv_id, "invoice.erp_submitted") == []
    assert await _audit(mk, inv_id, "invoice.erp_retried") == []


@pytest.mark.parametrize("door", ["send-to-erp", "complete", "retry-erp"])
@pytest.mark.parametrize("case", _BOOKED)
async def test_an_unverified_inactive_or_active_vendor_still_books(realdb, dispatched, case, door):
    """An unverified vendor's approved bill is a real debt: it books, while
    the run gate still refuses to PAY it (see the parity test below)."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    start = InvoiceStatus.failed if door == "retry-erp" else InvoiceStatus.approved
    inv_id = await _seed(mk, info.org_id, number=f"VNDOK-{case}-{door}"[:50], status=start)
    await _link_vendor(mk, info.org_id, inv_id, case)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/{door}")

    assert res.status_code in (200, 202), res.text
    assert dispatched == [inv_id]
    assert await _status(mk, inv_id) == InvoiceStatus.sending_to_erp


async def test_an_invoice_without_a_linked_vendor_does_not_refuse_the_push(realdb, dispatched):
    """No vendor row to judge: the run gate does not refuse it either, and the
    adapter's own vendor resolution answers it."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="VNDX-NONE")

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert res.status_code == 202, res.text


async def test_a_live_payment_is_named_before_a_vendor_hold(realdb, dispatched):
    """§286 and §309 read as before: the vendor check runs last."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="VNDX-BOTH")
    await _link_vendor(mk, info.org_id, inv_id, "blocked")
    await _stage_payment(mk, info.org_id, inv_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    _assert_paid_refusal(res, "payment")


async def test_retry_erp_for_a_held_vendor_says_the_bill_may_be_posted(realdb, dispatched):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="VNDX-RETRY", status=InvoiceStatus.failed)
    await _set_state(
        mk, inv_id, {"erp_retries": 3, "last_error": "HTTP 503", "erp_outcome_unknown": True}
    )
    await _link_vendor(mk, info.org_id, inv_id, "sanctions_review")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/retry-erp")

    _assert_vendor_refusal(res, "sanctions_review", possibly_posted=True)
    assert "may already have created the bill in the ERP" in res.json()["detail"]["message"]
    assert await _status(mk, inv_id) == InvoiceStatus.failed
    assert dispatched == []


@pytest.mark.parametrize("case", ["blocked", "sanctions_match", "rejected"])
async def test_a_vendor_blocked_after_the_push_was_queued_stops_it_at_call_erp(realdb, case):
    """The door passed; the vendor is blocked (a re-screen match, a manual
    block, a rejection) by the time the dispatcher runs. `_call_erp` refuses
    before building an adapter, at `failed`, on the first attempt."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(
        mk, info.org_id, number=f"VNDX-QUEUED-{case}", status=InvoiceStatus.sending_to_erp
    )
    await _link_vendor(mk, info.org_id, inv_id, case)
    hold = _VENDOR_CASES[case][3]

    spy = _SpyAdapter()
    await _push(mk, inv_id, adapter=spy)

    assert spy.posted == 0
    assert await _status(mk, inv_id) == InvoiceStatus.failed
    (failed,) = await _audit(mk, inv_id, "invoice.erp_failed")
    assert failed.details["refusal"] == {"code": VENDOR_CODE, "params": {"hold": hold}}
    assert failed.details["error"] == f"ERP post refused: {VENDOR_CODE} ({hold})"
    assert failed.details["retries"] == 1, "a refusal must not spend the backoff budget"
    assert "Northwind" not in str(failed.details)


async def test_unblocking_the_vendor_releases_the_push(realdb, dispatched):
    """End to end: refused while blocked; `POST /vendors/{id}/unblock` (the
    payment path's own release) lets the next send through."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="VNDX-UNBLOCK")
    vendor_id = await _link_vendor(mk, info.org_id, inv_id, "blocked")

    async with realdb.client(key=TENANT, role="admin") as c:
        _assert_vendor_refusal(
            await c.post(f"/api/invoices/{inv_id}/send-to-erp"), "payments_blocked"
        )
        unblocked = await c.post(f"/api/vendors/{vendor_id}/unblock")
        assert unblocked.status_code == 200, unblocked.text
        sent = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert sent.status_code == 202, sent.text
    assert dispatched == [inv_id]


async def test_re_screening_the_vendor_releases_a_review_hold(realdb, dispatched):
    """The gate reads the STORED screen, so a stale `review` (a provider that
    could not run, since fixed) is not a dead end: re-screening the vendor
    (`POST /vendors/{id}/screen`, the exit the 409 names) rewrites it, and the
    next send goes through. The local `mock` screen clears an ordinary name."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="VNDX-RESCREEN")
    vendor_id = await _link_vendor(mk, info.org_id, inv_id, "sanctions_review")

    async with realdb.client(key=TENANT, role="admin") as c:
        _assert_vendor_refusal(
            await c.post(f"/api/invoices/{inv_id}/send-to-erp"), "sanctions_review"
        )
        screened = await c.post(f"/api/vendors/{vendor_id}/screen")
        assert screened.status_code == 200, screened.text
        assert screened.json()["screening_status"] == "clear"
        sent = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert sent.status_code == 202, sent.text
    assert dispatched == [inv_id]


# -- parity: the ERP gate and the payment path read the vendor the same way --


async def test_the_erp_gate_and_the_run_gate_read_one_vendor_predicate(realdb, monkeypatch):
    """Both gates go through `payment_runs.vendor_payability`, so neither can
    read the vendor's columns differently from the other."""
    from app.services import payment_runs
    from app.services.erp_export_gate import erp_export_vendor_hold

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="VNDX-PARITY-SPY")
    await _link_vendor(mk, info.org_id, inv_id, "unverified")

    real = payment_runs.vendor_payability
    calls: list[str] = []

    async def _spy(db, invoices):
        calls.append("read")
        return await real(db, invoices)

    monkeypatch.setattr(payment_runs, "vendor_payability", _spy)
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert await payment_runs.inactive_vendor_statuses(s, [inv]) == {inv_id: "unverified"}
        assert await erp_export_vendor_hold(s, inv) is None
    assert calls == ["read", "read"]


@pytest.mark.parametrize("case", list(_VENDOR_CASES))
async def test_the_erp_gate_never_refuses_a_vendor_the_payment_path_would_pay(realdb, case):
    """Per case, against a real tenant:

    * the run gate refuses exactly the non-`active` statuses (unchanged by the
      shared read);
    * the ERP gate refuses exactly the documented holds;
    * a status refusal from the ERP gate (`rejected`, unknown) is always one
      the run gate makes too; the push never refuses on status alone where
      the run would pay. Its screening holds are the stored counterparts of
      dispatch's live screen, pinned separately (the block by
      `test_dispatch_refuses_the_blocked_vendor_the_erp_gate_refuses`, the
      stored screen's exit by `test_re_screening_the_vendor_releases_a_review_hold`).
    """
    from app.services.erp_export_gate import erp_export_vendor_hold
    from app.services.payment_runs import (
        REFUSAL_VENDOR_NOT_ACTIVE,
        run_refusal_reasons,
        vendor_payability,
    )

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number=f"VNDX-PARITY-{case}"[:50])
    await _link_vendor(mk, info.org_id, inv_id, case)
    status, _blocked, _screening, expected_hold = _VENDOR_CASES[case]

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        payability = (await vendor_payability(s, [inv]))[inv_id]
        run = (await run_refusal_reasons(s, [inv])).get(inv_id)
        hold = await erp_export_vendor_hold(s, inv)

    run_refuses = run is not None and run.reason == REFUSAL_VENDOR_NOT_ACTIVE
    assert run_refuses == (status != "active")
    assert hold == expected_hold
    if hold is not None and payability.sanctions_hold is None:
        assert run_refuses, "a status refusal the run gate does not make"


async def test_dispatch_refuses_the_blocked_vendor_the_erp_gate_refuses():
    """The sticky block the ERP gate reads is the one the pre-payment
    compliance check refuses first, before any screen or FX lock."""
    from app.services.compliance import check_payment_compliance
    from app.services.payment_runs import HOLD_PAYMENTS_BLOCKED, VendorPayability

    vendor = SimpleNamespace(payments_blocked=True, payments_blocked_reason="manual")
    decision = await check_payment_compliance(
        None,
        vendor=vendor,
        payment_amount=Decimal("1"),
        payment_currency="USD",
        payment_method="ach",
        org_settings=None,
        organization_id=uuid.uuid4(),
    )
    assert decision.verdict == "refuse"
    assert VendorPayability("active", True, "clear").sanctions_hold == HOLD_PAYMENTS_BLOCKED
