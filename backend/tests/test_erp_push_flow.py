"""ERP push pipeline — `send_to_erp_internal` and `retry_erp`.

`send_to_erp_internal` is the load-bearing path that moves an approved
invoice to `done` via the ERP: `api/workflow.py` transitions
approved → sending_to_erp and then dispatches, and both `erp_dispatch`
and `erp_lambda` land here. A regression in retry counting double-charges
ERP IDs; a regression in the "all retries exhausted" branch wedges
invoices in `sending_to_erp` forever; a regression in the happy-path
transition chain skips audit rows the SOC 2 auditor wants.

These used to exercise a second function, `erp.py::send_to_erp`, that
nothing in production called. It held the retry loop; the reachable
function had none, so a transient ERP 503 failed the invoice on the first
attempt while these tests reported the backoff working. The retry moved to
the reachable function and the unreachable copy was deleted — so the
invoice arrives here ALREADY in `sending_to_erp`, and the
approved → sending_to_erp leg is the route's, not this function's.

These tests pin:
  - happy path walks approved → sending_to_erp → sent_to_erp → done
    with audit rows on each leg
  - the erp_reference returned by the adapter rides on the
    `invoice.erp_confirmed` audit row's details
  - workflow_instance.state_data captures `erp_retries` and
    `erp_reference` after success
  - all three retries failing transitions the invoice to `failed`
    (NOT done), persists `erp_retries=3` and `last_error` on the
    instance, and marks `instance.state="failed"`
  - retry_erp refuses to retry an invoice that was never approved
    (the `approved_by` guard) — money invariant: we don't push to
    ERP unless an actual human approved
  - retry_erp resets erp_retries to 0 and parks the invoice at
    sending_to_erp WITHOUT running the ERP call inline — the route's
    dispatch_erp owns the call (org config + FEOH_ERP_MODE); an inline
    call would double-post and always use the mock adapter

Adapter calls and `asyncio.sleep` are stubbed — we never want real
network latency or real backoff sleeps inside a unit test.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.invoice import InvoiceStatus
from app.services.erp import ErpPostRefusedError, retry_erp, send_to_erp_internal

_UNSET = object()


def _invoice(
    *,
    status=InvoiceStatus.approved,
    approved_by=_UNSET,
    vendor_id=_UNSET,
    entity_id=None,
    organization_id=None,
    gl_account=None,
):
    """Invoice fixture. `approved_by` defaults to a fresh UUID (the
    common case); pass `approved_by=None` explicitly to model an
    invoice that was never approved. `vendor_id` likewise defaults to a
    resolved vendor link; pass None for an invoice whose vendor never
    matched."""
    if approved_by is _UNSET:
        approved_by = uuid.uuid4()
    if vendor_id is _UNSET:
        vendor_id = uuid.uuid4()
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        correlation_id=uuid.uuid4(),
        organization_id=organization_id or uuid.uuid4(),
        entity_id=entity_id,
        vendor_id=vendor_id,
        approved_by=approved_by,
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("100.00"),
        currency="USD",
        vendor_tax_id=None,
        invoice_date=None,
        due_date=None,
        po_number=None,
        description=None,
        subtotal=None,
        tax_amount=None,
        tax_rate=None,
        discount_amount=None,
        shipping_amount=None,
        gl_account=gl_account,
        cost_center=None,
        payment_terms=None,
        payment_method=None,
        bill_to_address=None,
        remit_to_address=None,
        vendor_address=None,
    )


def _instance(*, state_data=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        state="active",
        state_data=state_data,
        correlation_id=uuid.uuid4(),
    )


class _AuditRecorder:
    def __init__(self):
        self.rows: list[dict] = []

    async def __call__(self, db, **kwargs):
        self.rows.append(kwargs)

    def actions(self) -> list[str]:
        return [r["action"] for r in self.rows]

    def transitions(self) -> list[tuple[str, str]]:
        return [
            (r["details"]["old_status"], r["details"]["new_status"])
            for r in self.rows
            if "old_status" in (r.get("details") or {})
        ]


# ---------------------------------------------------------------------------
# send_to_erp_internal — happy path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_to_erp_happy_path_walks_approved_to_done():
    """ERP call succeeds on the first try → invoice walks
    sending_to_erp → sent_to_erp → done. Two audit rows; the
    erp_reference rides on the first."""
    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    inst = _instance()
    recorder = _AuditRecorder()
    db = AsyncMock()

    with (
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch("app.services.erp._call_erp", AsyncMock(return_value="ERP-12345")),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=inst)),
        patch("app.services.erp.complete_workflow", AsyncMock()),
    ):
        await send_to_erp_internal(db, inv)

    assert inv.status == InvoiceStatus.done
    # Two transitions in order — the approved → sending_to_erp leg belongs to
    # `api/workflow.py`, which runs it before dispatching to this function.
    assert recorder.transitions() == [
        ("sending_to_erp", "sent_to_erp"),
        ("sent_to_erp", "done"),
    ]
    # erp_reference rides on the sent_to_erp row.
    confirm = next(r for r in recorder.rows if r["action"] == "invoice.erp_confirmed")
    assert confirm["details"]["erp_reference"] == "ERP-12345"

    # Instance state_data captures the retry count and the ERP ref.
    assert inst.state_data["erp_reference"] == "ERP-12345"
    assert inst.state_data["erp_retries"] == 1


@pytest.mark.asyncio
async def test_a_status_synced_erp_leaves_the_bill_payable_at_posted_in_erp():
    """Decisions §295: when the ERP's payment status flows back (QuickBooks
    with status sync on), a confirmed push lands at `posted_in_erp` — payable,
    so the ERP-paid path can move it to `paid` — instead of closing at `done`."""
    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    inst = _instance()
    recorder = _AuditRecorder()
    db = AsyncMock()
    config = {"type": "quickbooks_online", "integration_method": "direct"}

    with (
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch("app.services.erp._call_erp", AsyncMock(return_value="501")),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=inst)),
        patch("app.services.erp.complete_workflow", AsyncMock()),
        patch("app.services.erp.status_sync_active", return_value=True) as active,
    ):
        await send_to_erp_internal(db, inv, erp_config=config)

    active.assert_called_once_with(config)
    assert inv.status == InvoiceStatus.posted_in_erp
    assert recorder.transitions() == [
        ("sending_to_erp", "sent_to_erp"),
        ("sent_to_erp", "posted_in_erp"),
    ]


def test_status_sync_needs_the_flag_and_an_adapter_that_reports_back(monkeypatch):
    from app.config import settings
    from app.services.erp_status_sync import status_sync_active

    qbo = {"type": "quickbooks_online", "integration_method": "direct"}
    netsuite = {"type": "netsuite", "integration_method": "direct"}
    monkeypatch.setattr(settings, "erp_status_sync_enabled", False)
    assert status_sync_active(qbo) is False
    monkeypatch.setattr(settings, "erp_status_sync_enabled", True)
    assert status_sync_active(qbo) is True
    # An ERP whose payments never flow back keeps closing at `done`.
    assert status_sync_active(netsuite) is False
    assert status_sync_active(None) is False
    assert status_sync_active({"type": "no_such_erp", "integration_method": "direct"}) is False


# ---------------------------------------------------------------------------
# send_to_erp_internal — retry exhaustion.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_to_erp_retry_exhausted_transitions_to_failed():
    """All MAX_RETRIES attempts raise → invoice ends up `failed`,
    not `done`. The instance carries the final retry count + the
    last_error string. asyncio.sleep is stubbed so the backoff
    delays don't run."""
    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    inst = _instance()
    recorder = _AuditRecorder()
    db = AsyncMock()

    with (
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch(
            "app.services.erp._call_erp",
            AsyncMock(side_effect=RuntimeError("connection refused")),
        ),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=inst)),
        patch("app.services.erp.asyncio.sleep", AsyncMock()),
    ):
        await send_to_erp_internal(db, inv)

    assert inv.status == InvoiceStatus.failed
    assert recorder.transitions()[-1] == ("sending_to_erp", "failed")

    # Final audit row carries the error string and retry count.
    fail_row = next(r for r in recorder.rows if r["action"] == "invoice.erp_failed")
    assert fail_row["details"]["error"] == "connection refused"
    assert fail_row["details"]["retries"] == 3

    # Instance reflects failure for the queue-builder.
    assert inst.state == "failed"
    assert inst.state_data["erp_retries"] == 3
    assert inst.state_data["last_error"] == "connection refused"


@pytest.mark.asyncio
async def test_send_to_erp_retry_attempts_use_exponential_backoff():
    """The retry loop must use exponential backoff (2, 4, ... seconds)
    so a transient outage doesn't get hammered. We capture the sleep
    durations and assert the doubling pattern. The final attempt
    doesn't sleep (no retry after it)."""
    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    inst = _instance()
    sleep_mock = AsyncMock()
    db = AsyncMock()

    with (
        patch("app.services.workflow_engine.dispatch_audit", new=_AuditRecorder()),
        patch(
            "app.services.erp._call_erp",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=inst)),
        patch("app.services.erp.asyncio.sleep", sleep_mock),
    ):
        await send_to_erp_internal(db, inv)

    # 3 attempts → 2 sleeps (no sleep after the last attempt).
    delays = [c.args[0] for c in sleep_mock.call_args_list]
    assert delays == [2, 4], f"expected 2s, 4s backoff, got {delays}"


@pytest.mark.asyncio
async def test_send_to_erp_fails_a_refused_payload_at_once_without_backoff():
    """A pre-flight refusal (vendor or account not linked) can't succeed on a
    re-send, so it fails the invoice on the first attempt with no backoff
    sleep, rather than spending the retry budget on the same refused bill."""
    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    inst = _instance()
    recorder = _AuditRecorder()
    call_erp = AsyncMock(
        side_effect=ErpPostRefusedError("NetSuite post refused: vendor_not_linked")
    )
    sleep_mock = AsyncMock()

    with (
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch("app.services.erp._call_erp", call_erp),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=inst)),
        patch("app.services.erp.asyncio.sleep", sleep_mock),
    ):
        await send_to_erp_internal(AsyncMock(), inv)

    assert inv.status == InvoiceStatus.failed
    assert call_erp.await_count == 1
    sleep_mock.assert_not_called()
    fail_row = next(r for r in recorder.rows if r["action"] == "invoice.erp_failed")
    assert fail_row["details"]["error"] == "NetSuite post refused: vendor_not_linked"
    assert fail_row["details"]["retries"] == 1


@pytest.mark.asyncio
async def test_send_to_erp_fails_a_missing_oauth_connection_at_once():
    """An OAuth ERP with no usable connection (never consented, revoked) raising
    out of an adapter is final: no re-send can connect it."""
    from app.services.erp_oauth import ErpNotConnectedError

    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    call_erp = AsyncMock(side_effect=ErpNotConnectedError("xero"))
    sleep_mock = AsyncMock()
    with (
        patch("app.services.workflow_engine.dispatch_audit", new=_AuditRecorder()),
        patch("app.services.erp._call_erp", call_erp),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=_instance())),
        patch("app.services.erp.asyncio.sleep", sleep_mock),
    ):
        await send_to_erp_internal(AsyncMock(), inv)
    assert inv.status == InvoiceStatus.failed
    assert call_erp.await_count == 1
    sleep_mock.assert_not_called()


@pytest.mark.asyncio
async def test_send_to_erp_retries_a_token_refresh_outage():
    """A provider outage while refreshing is transient: the backoff still runs."""
    from app.services.erp_oauth import ErpTokenRefreshError

    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    call_erp = AsyncMock(side_effect=ErpTokenRefreshError("xero", "HTTP 503"))
    with (
        patch("app.services.workflow_engine.dispatch_audit", new=_AuditRecorder()),
        patch("app.services.erp._call_erp", call_erp),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=_instance())),
        patch("app.services.erp.asyncio.sleep", AsyncMock()),
    ):
        await send_to_erp_internal(AsyncMock(), inv)
    assert call_erp.await_count == 3


# ---------------------------------------------------------------------------
# send_to_erp_internal — resumes from persisted retry count.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_to_erp_resumes_from_persisted_retry_count():
    """Instance already shows `erp_retries=2` from a prior partial
    failure. The next entry into send_to_erp_internal starts at attempt 2,
    leaving only one slot. If THIS attempt also fails, the invoice
    goes to `failed` immediately — no extra retries."""
    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    inst = _instance(state_data={"erp_retries": 2})
    recorder = _AuditRecorder()
    db = AsyncMock()

    sleep_mock = AsyncMock()
    with (
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch(
            "app.services.erp._call_erp",
            AsyncMock(side_effect=RuntimeError("still down")),
        ),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=inst)),
        patch("app.services.erp.asyncio.sleep", sleep_mock),
    ):
        await send_to_erp_internal(db, inv)

    assert inv.status == InvoiceStatus.failed
    # No sleep — there was only one slot left, and after it failed
    # we exhausted retries immediately.
    assert sleep_mock.call_count == 0


# ---------------------------------------------------------------------------
# retry_erp — guard rails.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_erp_refuses_invoice_that_was_never_approved():
    """`approved_by` is None → 409. The ERP receives nothing that
    wasn't human-approved. Without this guard, an automated retry
    job could drive any failed invoice into the ERP. The money
    invariant: money never moves on an un-approved invoice."""
    from fastapi import HTTPException

    inv = _invoice(approved_by=None, status=InvoiceStatus.failed)
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await retry_erp(db, inv)

    assert exc.value.status_code == 409
    assert "never approved" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_retry_erp_prepares_state_but_never_runs_the_erp_call_inline():
    """A human pressing "retry" should get a full fresh budget, not
    the leftover slots from the prior failure. Verify by setting
    `erp_retries=3` (exhausted) on the instance and confirming the
    retry resets it to 0 and parks the invoice at sending_to_erp.

    Regression: retry_erp used to ALSO run send_to_erp_internal inline
    — without the org's erp_config (so the retry always posted via the
    MOCK adapter), racing the route's own dispatch_erp (double-post),
    and bypassing FEOH_ERP_MODE=lambda. The actual call is the
    dispatcher's job; retry_erp must only prepare state."""
    inv = _invoice(status=InvoiceStatus.failed)
    inst = _instance(state_data={"erp_retries": 3, "last_error": "old"})
    db = AsyncMock()
    internal_mock = AsyncMock()
    recorder = _AuditRecorder()

    with (
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=inst)),
        patch("app.services.erp.send_to_erp_internal", internal_mock),
    ):
        await retry_erp(db, inv)

    # The retry counter was reset and the instance reactivated.
    assert inst.state_data["erp_retries"] == 0
    assert inst.state == "active"
    # Invoice parked at sending_to_erp; dispatch_erp (the route's next
    # call) performs the actual ERP post with the org's config.
    assert inv.status == InvoiceStatus.sending_to_erp
    internal_mock.assert_not_awaited()
    # Audit row marks this as a retry, not a fresh submission.
    assert any(r["action"] == "invoice.erp_retried" for r in recorder.rows)


# ---------------------------------------------------------------------------
# send_to_erp_internal — org ERP config plumbing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_to_erp_internal_passes_org_erp_config_to_call_erp():
    """Regression: send_to_erp_internal used to drop its erp_config on the
    floor (`_call_erp(invoice)`), so the local dispatch worker — which
    resolves the org's settings.erp and passes it in — always posted via
    the MOCK adapter no matter what ERP the tenant configured. Lock the
    pass-through."""
    from app.services.erp import send_to_erp_internal

    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    inst = _instance()
    db = AsyncMock()
    call_erp = AsyncMock(return_value="ERP-REF-1")
    cfg = {
        "type": "netsuite",
        "integration_method": "direct",
        "account_id": "ACCT",
        # Migrated while connected to this account: its NULL-realm links count.
        "legacy_link_realm": "netsuite:acct",
    }

    with (
        patch("app.services.workflow_engine.dispatch_audit", new=_AuditRecorder()),
        patch("app.services.erp._call_erp", call_erp),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=inst)),
        patch("app.services.erp.complete_workflow", AsyncMock()),
    ):
        await send_to_erp_internal(db, inv, erp_config=cfg)

    call_erp.assert_awaited_once_with(db, inv, cfg, instance=inst)


@pytest.mark.asyncio
async def test_call_erp_dispatches_via_configured_adapter_not_mock():
    """With a merge_dev config, _call_erp must post through the Merge.dev
    adapter (the returned reference is the Merge model id), not the mock."""
    import httpx as _httpx  # noqa: F401 — ensure module import for patch target

    from app.services.erp import _call_erp

    resp = AsyncMock()
    resp.status_code = 201
    resp.json = lambda: {"model": {"id": "merge-inv-77", "number": "INV-1"}}

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=resp)
        ref = await _call_erp(
            _line_items_db([]),
            _invoice(),
            {
                "integration_method": "merge_dev",
                "api_key": "k",
                "account_token": "t",
                # Migrated while on this connection: its NULL-realm links count.
                "legacy_link_realm": "merge_dev:",
            },
        )

    assert ref == "merge-inv-77"
    posted_url = client.post.await_args.args[0]
    assert posted_url.endswith("/invoices")


@pytest.mark.asyncio
async def test_call_erp_carries_an_unconfirmed_erp_job_to_the_next_attempt():
    """An ERP whose create is a background job (Blackbaud FE NXT) reports a job
    it queued but could not see finish. `_call_erp` keeps it on the workflow
    instance and hands it back on the next attempt — a manual retry after a
    non-retryable `job_unconfirmed` included — so the adapter checks that job
    instead of queueing a second bill. A later result without one clears it."""
    from app.services.erp import (
        OUTCOME_UNKNOWN_KEY,
        PENDING_JOB_KEY,
        ErpPostRefusedError,
        _call_erp,
    )
    from app.services.erp_adapters.base import ErpPostResult

    seen: list[str | None] = []
    results = [
        ErpPostResult(
            success=False, message="unconfirmed", retryable=False, pending_job_id="job-41"
        ),
        ErpPostResult(success=True, erp_document_id="FE-9"),
    ]

    class _Adapter:
        async def post_invoice(self, payload):
            seen.append(payload.pending_job_id)
            return results.pop(0)

    inst = _instance(state_data={"erp_retries": 0})
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        with pytest.raises(ErpPostRefusedError):
            await _call_erp(_line_items_db([]), _invoice(), {"type": "x"}, instance=inst)
        assert inst.state_data == {
            "erp_retries": 0,
            PENDING_JOB_KEY: "job-41",
            OUTCOME_UNKNOWN_KEY: True,
        }

        ref = await _call_erp(_line_items_db([]), _invoice(), {"type": "x"}, instance=inst)

    assert ref == "FE-9"
    assert seen == [None, "job-41"]
    assert inst.state_data == {
        "erp_retries": 0,
        PENDING_JOB_KEY: None,
        OUTCOME_UNKNOWN_KEY: False,
        "erp_reference": "FE-9",
        # The connection realm the bill lives in (§295) — `merge_dev:` for the
        # default config these tests send with.
        "erp_realm": "merge_dev:",
    }


@pytest.mark.asyncio
async def test_call_erp_hands_each_earlier_attempts_invoice_date_to_the_next():
    """An ERP whose duplicate lookup narrows by date (Blackbaud FE NXT) has to
    search the date an earlier attempt was sent with, not only the invoice's
    current one: an edit between a lost attempt and the retry would otherwise
    hide the bill it made, and the retry would post a second (decisions §322).
    The date is kept before the call and cleared by a success."""
    from datetime import date

    from app.services.erp import ATTEMPT_INVOICE_DATES_KEY, ErpPostFailedError, _call_erp
    from app.services.erp_adapters.base import ErpPostResult

    seen: list[tuple[date, list[date]]] = []
    results = [
        ErpPostResult(success=False, message="timed out"),
        ErpPostResult(success=True, erp_document_id="FE-9"),
    ]

    class _Adapter:
        async def post_invoice(self, payload):
            seen.append((payload.invoice_date, list(payload.prior_invoice_dates)))
            return results.pop(0)

    invoice = _invoice()
    invoice.invoice_date, invoice.due_date = date(2026, 1, 1), date(2026, 1, 31)
    inst = _instance(state_data={"erp_retries": 0})
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        with pytest.raises(ErpPostFailedError):
            await _call_erp(_line_items_db([]), invoice, {"type": "x"}, instance=inst)
        assert inst.state_data[ATTEMPT_INVOICE_DATES_KEY] == ["2026-01-01"]

        invoice.invoice_date = date(2026, 1, 11)  # edited before the retry
        await _call_erp(_line_items_db([]), invoice, {"type": "x"}, instance=inst)

    assert seen == [
        (date(2026, 1, 1), []),
        (date(2026, 1, 11), [date(2026, 1, 1)]),
    ]
    assert ATTEMPT_INVOICE_DATES_KEY not in inst.state_data


@pytest.mark.asyncio
async def test_an_attempt_that_left_nothing_gives_its_date_slot_back():
    """Only an attempt that may have reached the ERP keeps its date. A refusal
    made before any call (`not_posted`: a budget refusal, a pre-flight one)
    names no bill, so editing the date and being refused again and again never
    crowds out the date of a bill that may exist (decisions §322)."""
    from datetime import date

    from app.services.erp import (
        ATTEMPT_INVOICE_DATES_KEY,
        ErpPostFailedError,
        ErpPostRefusedError,
        _call_erp,
    )
    from app.services.erp_adapters.base import ErpPostResult, erp_preflight_refusal

    results = [
        ErpPostResult(success=False, message="timed out"),
        *[erp_preflight_refusal("X", "erp_call_budget_exhausted") for _ in range(7)],
    ]

    class _Adapter:
        async def post_invoice(self, payload):
            return results.pop(0)

    invoice = _invoice()
    invoice.invoice_date, invoice.due_date = date(2026, 1, 1), date(2026, 3, 31)
    inst = _instance(state_data={"erp_retries": 0})
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        with pytest.raises(ErpPostFailedError):
            await _call_erp(_line_items_db([]), invoice, {"type": "x"}, instance=inst)
        for day in range(2, 9):
            invoice.invoice_date = date(2026, 1, day)
            with pytest.raises(ErpPostRefusedError):
                await _call_erp(_line_items_db([]), invoice, {"type": "x"}, instance=inst)

    assert inst.state_data[ATTEMPT_INVOICE_DATES_KEY] == ["2026-01-01"]


# ---------------------------------------------------------------------------
# _call_erp — line items reach the adapter payload.
# ---------------------------------------------------------------------------


def _line_items_db(rows, *, vendor_erp_id="ERP-V-1", accounts=None, open_exceptions=()):
    """A fake AsyncSession answering the four reads `_call_erp` makes, routed
    by the table each statement selects from:

    * the export gate (`erp_export_gate`, decisions §286) — `open_exceptions`,
      `(invoice_id, exception_type)` rows; none by default, so the push goes on,
      and then its live payment / card reads (§309) and its vendor read,
      always empty here (the
      gate's own SQL is proven against a real tenant in
      `test_erp_export_blocking_exceptions.py`);
    * `_fetch_line_items` — the given rows, in the order given (mirrors the
      real query's ORDER BY);
    * the vendor link — `vendor_erp_id` (None models an unlinked vendor);
    * the chart — `accounts`, `(code, entity_id, erp_account_id[,
      erp_link_realm])` rows, which `gl_chart.resolve_erp_account_ids` folds
      (its SQL filter is proven against a real tenant in the realdb tests
      below). A row without a realm is a pre-0115 (NULL) link. Default: every
      line's code, shared, with an `ERP-<code>` id.

    `db.calls` records the table of each statement, in order.
    """
    if accounts is None:
        accounts = [(r.gl_account, None, f"ERP-{r.gl_account}") for r in rows if r.gl_account]
    db = AsyncMock()
    db.calls = []

    async def execute(stmt, *_a, **_kw):
        sql = str(stmt)
        if "FROM exceptions" in sql:
            db.calls.append("exceptions")
            return SimpleNamespace(all=lambda: list(open_exceptions))
        # The gate's live-claim reads (decisions §309): no live payment, no
        # live card, so the push goes on.
        if "JOIN payments" in sql:
            db.calls.append("payments")
            return SimpleNamespace(all=lambda: [])
        if "FROM virtual_cards" in sql:
            db.calls.append("virtual_cards")
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        # The gate's vendor read (`payment_runs.vendor_payability`): no row,
        # so nothing holds the push (its SQL and every status are proven
        # against a real tenant in `test_erp_export_blocking_exceptions.py`).
        if "FROM vendors" in sql and "payments_blocked" in sql:
            db.calls.append("vendor_payability")
            return SimpleNamespace(all=lambda: [])
        if "FROM invoice_line_items" in sql:
            db.calls.append("invoice_line_items")
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
        if "FROM vendors" in sql:
            db.calls.append("vendors")
            return SimpleNamespace(scalar_one_or_none=lambda: vendor_erp_id)
        if "FROM gl_accounts" in sql:
            db.calls.append("gl_accounts")
            return SimpleNamespace(
                all=lambda: [tuple(a) + (None,) * (4 - len(a)) for a in accounts]
            )
        raise AssertionError(f"unexpected query: {sql}")

    db.execute = execute
    return db


def _line_item(
    *,
    line_number=1,
    item_code="SKU-1",
    description="Widget",
    quantity=Decimal("2"),
    unit_price=Decimal("10.00"),
    tax=Decimal("1.00"),
    total=Decimal("21.00"),
    gl_account="6000",
):
    return SimpleNamespace(
        line_number=line_number,
        item_code=item_code,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        tax=tax,
        total=total,
        gl_account=gl_account,
    )


@pytest.mark.asyncio
async def test_call_erp_includes_line_items_in_payload():
    """Regression: `_build_payload` used to never populate
    `InvoicePayload.line_items`, so every ERP push — regardless of
    adapter — collapsed a per-line-GL-coded invoice into a single line
    at the header GL code. Merge.dev's `post_invoice` renders
    `payload.line_items` straight into the request body, so asserting
    on the posted JSON proves the line items made it all the way from
    the DB fixture through `_build_payload` to the wire."""
    from app.services.erp import _call_erp

    rows = [
        _line_item(line_number=1, item_code="SKU-1", gl_account="6000", total=Decimal("21.00")),
        # A hand-keyed row with no line_number — must fall back to its
        # position in the query's stable order (2nd → line_number 2),
        # not be dropped from the payload.
        _line_item(line_number=None, item_code="SKU-2", gl_account="6100", total=Decimal("50.00")),
    ]
    db = _line_items_db(rows)

    resp = AsyncMock()
    resp.status_code = 201
    resp.json = lambda: {"model": {"id": "merge-inv-88", "number": "INV-1"}}

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=resp)
        await _call_erp(
            db,
            _invoice(),
            {
                "integration_method": "merge_dev",
                "api_key": "k",
                "account_token": "t",
                "legacy_link_realm": "merge_dev:",
            },
        )

    # The adapter serialises the body itself (`content=`, not `json=`) so the
    # Decimal amounts reach the wire exactly — see `utils/json_money`.
    posted_body = json.loads(client.post.await_args.kwargs["content"])
    posted_lines = posted_body["model"]["line_items"]
    assert len(posted_lines) == 2
    # Merge references accounts by its own object id — the chart's
    # `erp_account_id`, never the GL code.
    assert posted_lines[0]["account"] == "ERP-6000"
    assert posted_lines[0]["total_line_amount"] == 21.00
    assert posted_lines[1]["account"] == "ERP-6100"
    assert posted_lines[1]["total_line_amount"] == 50.00
    assert posted_body["model"]["contact"] == "ERP-V-1"


def test_build_payload_falls_back_to_query_order_for_null_line_number():
    """`_build_payload`'s `LineItemPayload.line_number` isn't nullable
    (unlike the DB column) — a legacy/hand-keyed row with no
    `line_number` must still get a positive one derived from its
    position in the (already-ordered) query result."""
    from app.services.erp import _build_payload

    rows = [_line_item(line_number=None), _line_item(line_number=None)]
    payload = _build_payload(_invoice(), rows)

    assert [li.line_number for li in payload.line_items] == [1, 2]


# ---------------------------------------------------------------------------
# ERP references — the vendor's and the accounts' ERP ids reach the payload
# (docs/known-issues.md § "The direct ERP adapters post bills without the
# ERP's vendor and account ids").
# ---------------------------------------------------------------------------


def test_build_payload_carries_the_resolved_erp_ids():
    """The header and every line carry the ERP id of their own GL code; a code
    the chart could not resolve stays None (unlinked), never the code."""
    from app.services.erp import ErpRefs, _build_payload

    rows = [
        _line_item(gl_account="6000"),
        _line_item(gl_account="6100"),
        _line_item(gl_account=None),
    ]
    refs = ErpRefs(vendor_erp_id="V-9", account_erp_ids={"6000": "A-6000", "7000": "A-7000"})
    payload = _build_payload(_invoice(gl_account="7000"), rows, refs)

    assert payload.vendor_erp_id == "V-9"
    assert payload.gl_account_erp_id == "A-7000"
    assert [li.gl_account_erp_id for li in payload.line_items] == ["A-6000", None, None]


def test_build_payload_without_refs_is_unlinked():
    from app.services.erp import _build_payload

    payload = _build_payload(_invoice(gl_account="7000"), [_line_item(gl_account="6000")])
    assert payload.vendor_erp_id is None
    assert payload.gl_account_erp_id is None
    assert payload.line_items[0].gl_account_erp_id is None


@pytest.mark.asyncio
async def test_resolving_erp_refs_costs_two_queries_whatever_the_line_count():
    """One vendor read and one chart read for the header and every line
    together — never a query per line."""
    from app.services.erp import _resolve_erp_refs

    rows = [_line_item(line_number=i, gl_account=f"6{i:03d}") for i in range(1, 51)]
    db = _line_items_db(rows)
    refs = await _resolve_erp_refs(db, _invoice(gl_account="7000"), rows)

    assert db.calls == ["vendors", "gl_accounts"]
    assert refs.vendor_erp_id == "ERP-V-1"
    assert refs.account_erp_ids["6050"] == "ERP-6050"


@pytest.mark.asyncio
async def test_resolving_erp_refs_skips_the_vendor_read_for_an_unmatched_vendor():
    """No `vendor_id` link → no ERP vendor, and no lookup by name either."""
    from app.services.erp import _resolve_erp_refs

    db = _line_items_db([])
    refs = await _resolve_erp_refs(db, _invoice(vendor_id=None), [])

    assert refs.vendor_erp_id is None
    assert db.calls == []  # no GL code either, so the chart is not read


@pytest.mark.asyncio
async def test_resolving_erp_refs_prefers_the_entity_override_over_the_shared_account():
    """A code in both the shared chart and the invoice entity's own resolves
    to the entity's account, even when that row has no ERP id — the shared
    account is not the one this invoice is coded to."""
    from app.services.erp import _resolve_erp_refs

    ent = uuid.uuid4()
    rows = [_line_item(gl_account="6000"), _line_item(gl_account="6100")]
    db = _line_items_db(
        rows,
        accounts=[
            ("6000", None, "SHARED-6000"),
            ("6000", ent, "OWN-6000"),
            ("6100", None, "SHARED-6100"),
            ("6100", ent, None),
        ],
    )
    refs = await _resolve_erp_refs(db, _invoice(entity_id=ent), rows)
    assert refs.account_erp_ids == {"6000": "OWN-6000"}


_NETSUITE_CFG = {
    "type": "netsuite",
    "integration_method": "direct",
    "account_id": "FAKE123",
    # Migrated while connected to this account (migration 0115), so the
    # fakes' NULL-realm links are trusted — see the realm tests in
    # test_erp_link_realm.py for the other cases.
    "legacy_link_realm": "netsuite:fake123",
    "consumer_key": "ck",
    "consumer_secret": "cs",
    "token_id": "ti",
    "token_secret": "ts",
}


@pytest.mark.asyncio
async def test_call_erp_refuses_an_unlinked_vendor_before_any_http_call():
    """An invoice whose vendor has no ERP id fails with the stable, PII-free
    reason code and never reaches NetSuite — no name fallback."""
    from app.services.erp import _call_erp

    rows = [_line_item(gl_account="6000")]
    with patch("httpx.AsyncClient") as cm:
        with pytest.raises(RuntimeError) as exc:
            await _call_erp(_line_items_db(rows, vendor_erp_id=None), _invoice(), _NETSUITE_CFG)
    assert str(exc.value) == "NetSuite post refused: vendor_not_linked"
    assert isinstance(exc.value, ErpPostRefusedError)
    cm.assert_not_called()


@pytest.mark.asyncio
async def test_call_erp_posts_netsuite_by_vendor_and_account_id():
    """End to end through `_call_erp`: the resolved ids are what NetSuite
    receives — `entity: {id}` and expense lines on `account: {id}`."""
    from app.services.erp import _call_erp

    # The line makes the approved 100.00, so it posts as its own expense line
    # (lines that do not add up collapse onto the header account instead).
    rows = [_line_item(gl_account="6000", total=Decimal("100.00"))]
    lookup = AsyncMock()
    lookup.status_code = 200
    lookup.json = lambda: {"items": []}
    created = AsyncMock()
    created.status_code = 204
    created.headers = {"Location": "https://x/vendorBill/1001"}
    created.content = b""
    # The read-back: NetSuite booked exactly the approved 100.00.
    readback = AsyncMock()
    readback.status_code = 200
    readback.content = b'{"id": "1001", "total": 100.00}'

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=[lookup, readback])
        client.post = AsyncMock(return_value=created)
        ref = await _call_erp(_line_items_db(rows), _invoice(), _NETSUITE_CFG)

    assert ref == "1001"
    body = json.loads(client.post.await_args.kwargs["content"])
    assert body["entity"] == {"id": "ERP-V-1"}
    assert body["expense"]["items"] == [
        {"account": {"id": "ERP-6000"}, "amount": 100.00, "memo": "Widget"}
    ]
    assert "item" not in body


# --- against a real tenant: the chart query itself -------------------------


async def test_erp_refs_resolve_against_the_invoice_entitys_own_chart(realdb):
    """`_resolve_erp_refs` over a real tenant: an entity's own account wins
    over the shared one with the same code; a shared-only code falls back to
    the shared account; another entity's account never resolves; an account
    with no ERP id resolves to nothing; the vendor id comes from the linked
    vendor row."""
    from sqlalchemy import select

    from app.models.entity import Entity
    from app.models.gl_account import GLAccount
    from app.models.vendor import Vendor
    from app.services.erp import _resolve_erp_refs

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        default_id = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        entity_b = Entity(
            organization_id=org_id,
            name="B Co",
            slug=f"b-co-{uuid.uuid4().hex[:6]}",
            is_default=False,
            is_active=True,
        )
        vendor = Vendor(organization_id=org_id, name="Linked Co", erp_vendor_id="NS-25")
        s.add_all([entity_b, vendor])
        await s.flush()
        s.add_all(
            [
                GLAccount(
                    organization_id=org_id,
                    code="6100",
                    name="Shared supplies",
                    erp_account_id="S-6100",
                ),
                GLAccount(
                    organization_id=org_id,
                    code="6100",
                    name="B supplies",
                    entity_id=entity_b.id,
                    erp_account_id="B-6100",
                ),
                GLAccount(
                    organization_id=org_id,
                    code="6200",
                    name="Shared software",
                    erp_account_id="S-6200",
                ),
                GLAccount(
                    organization_id=org_id,
                    code="6300",
                    name="Default only",
                    entity_id=default_id,
                    erp_account_id="D-6300",
                ),
                GLAccount(organization_id=org_id, code="6400", name="Never synced"),
            ]
        )
        await s.commit()
        b_id, vendor_id = entity_b.id, vendor.id

    codes = ["6100", "6200", "6300", "6400"]
    lines = [SimpleNamespace(gl_account=c) for c in codes]
    async with mk() as s:
        on_b = await _resolve_erp_refs(
            s, _invoice(organization_id=org_id, entity_id=b_id, vendor_id=vendor_id), lines
        )
        on_default = await _resolve_erp_refs(
            s, _invoice(organization_id=org_id, entity_id=default_id, vendor_id=None), lines
        )

    assert on_b.vendor_erp_id == "NS-25"
    assert on_b.account_erp_ids == {"6100": "B-6100", "6200": "S-6200"}
    assert on_default.vendor_erp_id is None
    assert on_default.account_erp_ids == {
        "6100": "S-6100",
        "6200": "S-6200",
        "6300": "D-6300",
    }


# ---------------------------------------------------------------------------
# A refusal that leaves a bill in the ERP names it.
#
# `posted_total_mismatch` whose void failed, and `posted_total_unconfirmed`,
# are non-retryable failures that come back WITH the created bill's id.
# `_call_erp` used to raise with the message alone, so the invoice went to
# `failed` and the live bill — booked at a total nobody approved — could not
# be found from our side. The ids now ride the error, the workflow instance
# (`ORPHAN_DOCUMENT_*_KEY`) and the `invoice.erp_failed` audit row; the
# provider's response body never does.
# ---------------------------------------------------------------------------

_PROVIDER_BODY_MARKER = "SECRET-PROVIDER-BODY"


class _NoVoidAdapter:
    """An ERP that cannot void the bill it just created."""

    async def void_invoice(self, document_id):
        return False


async def _posted_total_failure(kind: str):
    """The real `check_posted_total` result for a mismatch-not-voided or an
    unconfirmed total, carrying a provider body that must never be persisted."""
    from app.services.erp_adapters.base import InvoicePayload
    from app.services.erp_adapters.posted_total import check_posted_total

    payload = InvoicePayload(
        correlation_id="c", invoice_number="INV-1", vendor_name="Acme", amount=Decimal("100.00")
    )
    result = await check_posted_total(
        _NoVoidAdapter(),
        "Xero",
        payload,
        posted_total=Decimal("115.00") if kind == "mismatch" else None,
        document_id="XERO-BILL-7",
        document_number="BILL-0007",
        raw_response={"body": _PROVIDER_BODY_MARKER},
    )
    assert result is not None and not result.success and not result.retryable
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mismatch", "unconfirmed"])
async def test_call_erp_keeps_the_id_of_a_bill_a_refusal_left_in_the_erp(kind):
    from app.services.erp import (
        ORPHAN_DOCUMENT_ID_KEY,
        ORPHAN_DOCUMENT_NUMBER_KEY,
        OUTCOME_UNKNOWN_KEY,
        _call_erp,
    )

    result = await _posted_total_failure(kind)
    if kind == "mismatch":
        assert "could not be voided" in result.message

    class _Adapter:
        async def post_invoice(self, payload):
            return result

    inst = _instance(state_data={"erp_retries": 0})
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        with pytest.raises(ErpPostRefusedError) as exc:
            await _call_erp(_line_items_db([]), _invoice(), {"type": "x"}, instance=inst)

    assert exc.value.erp_document_id == "XERO-BILL-7"
    assert exc.value.erp_document_number == "BILL-0007"
    assert str(exc.value) == result.message
    assert inst.state_data == {
        "erp_retries": 0,
        ORPHAN_DOCUMENT_ID_KEY: "XERO-BILL-7",
        ORPHAN_DOCUMENT_NUMBER_KEY: "BILL-0007",
        OUTCOME_UNKNOWN_KEY: True,
    }


@pytest.mark.asyncio
async def test_call_erp_retryable_failure_carries_the_bill_ids_too():
    """The retryable path raises the same carrier, so a transient failure that
    reports a created document is not dropped either."""
    from app.services.erp import ORPHAN_DOCUMENT_ID_KEY, ErpPostFailedError, _call_erp
    from app.services.erp_adapters.base import ErpPostResult

    class _Adapter:
        async def post_invoice(self, payload):
            return ErpPostResult(success=False, message="X post failed", erp_document_id="D-1")

    inst = _instance(state_data={})
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        with pytest.raises(ErpPostFailedError) as exc:
            await _call_erp(_line_items_db([]), _invoice(), {"type": "x"}, instance=inst)
    assert not isinstance(exc.value, ErpPostRefusedError)
    assert exc.value.erp_document_id == "D-1"
    assert inst.state_data[ORPHAN_DOCUMENT_ID_KEY] == "D-1"


@pytest.mark.asyncio
async def test_call_erp_failure_without_a_document_keeps_the_earlier_orphan():
    """A later attempt that reports no document does not erase the bill an
    earlier attempt left in the ERP — it is still there."""
    from app.services.erp import ORPHAN_DOCUMENT_ID_KEY, OUTCOME_UNKNOWN_KEY, _call_erp
    from app.services.erp_adapters.base import erp_refusal

    class _Adapter:
        async def post_invoice(self, payload):
            return erp_refusal("Xero", "vendor_not_linked")

    state = {"erp_retries": 0, ORPHAN_DOCUMENT_ID_KEY: "XERO-BILL-7"}
    inst = _instance(state_data=dict(state))
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        with pytest.raises(ErpPostRefusedError) as exc:
            await _call_erp(_line_items_db([]), _invoice(), {"type": "x"}, instance=inst)
    assert exc.value.erp_document_id is None
    assert inst.state_data == {**state, OUTCOME_UNKNOWN_KEY: True}


@pytest.mark.asyncio
async def test_call_erp_success_clears_an_earlier_orphan():
    """Once a push succeeds its `erp_reference` names the bill of record; the
    orphan keys from an earlier failed attempt are cleared."""
    from app.services.erp import (
        ORPHAN_DOCUMENT_ID_KEY,
        ORPHAN_DOCUMENT_NUMBER_KEY,
        OUTCOME_UNKNOWN_KEY,
        _call_erp,
    )
    from app.services.erp_adapters.base import ErpPostResult

    class _Adapter:
        async def post_invoice(self, payload):
            return ErpPostResult(success=True, erp_document_id="XERO-BILL-7")

    inst = _instance(
        state_data={
            "erp_retries": 0,
            ORPHAN_DOCUMENT_ID_KEY: "XERO-BILL-7",
            ORPHAN_DOCUMENT_NUMBER_KEY: "BILL-0007",
        }
    )
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        ref = await _call_erp(_line_items_db([]), _invoice(), {"type": "x"}, instance=inst)
    assert ref == "XERO-BILL-7"
    assert inst.state_data == {
        "erp_retries": 0,
        OUTCOME_UNKNOWN_KEY: False,
        "erp_reference": "XERO-BILL-7",
        "erp_realm": "merge_dev:",
    }


@pytest.mark.asyncio
async def test_send_to_erp_failed_audit_row_names_the_bill_left_in_the_erp():
    """Through the whole push: one attempt (non-retryable), the invoice at
    `failed`, and the append-only `invoice.erp_failed` row carrying the bill's
    ERP ids — never the provider's response body."""
    from app.services.erp import ORPHAN_DOCUMENT_ID_KEY

    result = await _posted_total_failure("mismatch")
    calls = []

    class _Adapter:
        async def post_invoice(self, payload):
            calls.append(payload)
            return result

    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    inst = _instance(state_data={})
    recorder = _AuditRecorder()
    with (
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch("app.services.erp.get_erp_adapter", return_value=_Adapter()),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=inst)),
        patch("app.services.erp.asyncio.sleep", AsyncMock()),
    ):
        await send_to_erp_internal(_line_items_db([]), inv, erp_config={"type": "x"})

    assert len(calls) == 1
    assert inv.status == InvoiceStatus.failed
    fail_row = next(r for r in recorder.rows if r["action"] == "invoice.erp_failed")
    assert fail_row["details"]["erp_document_id"] == "XERO-BILL-7"
    assert fail_row["details"]["erp_document_number"] == "BILL-0007"
    assert fail_row["details"]["error"] == result.message
    assert _PROVIDER_BODY_MARKER not in json.dumps(recorder.rows, default=str)
    assert inst.state == "failed"
    assert inst.state_data[ORPHAN_DOCUMENT_ID_KEY] == "XERO-BILL-7"
    assert _PROVIDER_BODY_MARKER not in json.dumps(inst.state_data)


@pytest.mark.asyncio
async def test_send_to_erp_failed_audit_row_falls_back_to_an_earlier_orphan():
    """The last attempt reports no document, but an earlier one left a bill:
    the failure row still names it."""
    from app.services.erp import ORPHAN_DOCUMENT_ID_KEY

    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    inst = _instance(state_data={"erp_retries": 2, ORPHAN_DOCUMENT_ID_KEY: "D-OLD"})
    recorder = _AuditRecorder()
    with (
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch("app.services.erp._call_erp", AsyncMock(side_effect=RuntimeError("down"))),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=inst)),
    ):
        await send_to_erp_internal(AsyncMock(), inv)

    fail_row = next(r for r in recorder.rows if r["action"] == "invoice.erp_failed")
    assert fail_row["details"]["erp_document_id"] == "D-OLD"
    assert "erp_document_number" not in fail_row["details"]


# ---------------------------------------------------------------------------
# Against a real tenant: what these paths write is actually COMMITTED.
#
# `WorkflowInstance.state_data` is a plain JSONB column — no MutableDict — so
# only a NEW dict assigned to it is seen as a change. `retry_erp` used to edit
# the loaded dict in place and assign the same object back: SQLAlchemy saw no
# change, the `erp_retries` reset was never written, and the push the route
# dispatched next resumed from the exhausted counter and failed the invoice
# again without ever calling the ERP. A mock session cannot see that; a commit
# and a fresh read can.
# ---------------------------------------------------------------------------


async def _seed_erp_failed_invoice(mk, org_id, *, state_data):
    from app.models.invoice import Invoice
    from app.models.workflow import WorkflowDefinition, WorkflowInstance

    async with mk() as s:
        definition = WorkflowDefinition(
            organization_id=org_id, name="ERP push", steps_config={"steps": []}
        )
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"ERP-{uuid.uuid4().hex[:8]}",
            vendor_name="Acme",
            amount=Decimal("100.00"),
            currency="USD",
            status=InvoiceStatus.failed,
            approved_by=str(uuid.uuid4()),
        )
        s.add_all([definition, inv])
        await s.flush()
        s.add(
            WorkflowInstance(
                correlation_id=inv.correlation_id,
                definition_id=definition.id,
                invoice_id=inv.id,
                current_step=3,
                state="failed",
                state_data=state_data,
            )
        )
        await s.commit()
        return inv.id


async def _reload(mk, invoice_id):
    from sqlalchemy import select

    from app.models.invoice import Invoice
    from app.models.workflow import AuditLog, WorkflowInstance

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        inst = (
            await s.execute(
                select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice_id)
            )
        ).scalar_one()
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.entity_id == invoice_id)))
            .scalars()
            .all()
        )
        return inv, inst, rows


@pytest.mark.asyncio
async def test_retry_erp_reset_is_committed_and_keeps_the_pending_job(realdb):
    """The reset survives a commit and a fresh read. The pending ERP job and the
    orphan bill are carried over: Blackbaud must poll the job an earlier attempt
    queued before it queues another (else the retry posts a second bill), and
    the orphan stays in the ERP until a successful push supersedes it."""
    from sqlalchemy import select

    from app.models.invoice import Invoice
    from app.services.erp import ORPHAN_DOCUMENT_ID_KEY, PENDING_JOB_KEY

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_erp_failed_invoice(
        mk,
        info.org_id,
        state_data={
            "erp_retries": 3,
            "last_error": "old",
            PENDING_JOB_KEY: "job-41",
            ORPHAN_DOCUMENT_ID_KEY: "XERO-BILL-7",
        },
    )

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await retry_erp(s, inv)

    inv, inst, rows = await _reload(mk, inv_id)
    assert inv.status == InvoiceStatus.sending_to_erp
    assert inst.state == "active"
    assert inst.state_data == {
        "erp_retries": 0,
        "last_error": "old",
        PENDING_JOB_KEY: "job-41",
        ORPHAN_DOCUMENT_ID_KEY: "XERO-BILL-7",
    }
    assert any(r.action == "invoice.erp_retried" for r in rows)


@pytest.mark.asyncio
async def test_send_to_erp_commits_the_orphan_bill_and_its_audit_row(realdb):
    """End to end on a real tenant: retry, then a mismatch the ERP could not
    void. The invoice is `failed`, the bill's ids are committed on the
    instance, and the committed `invoice.erp_failed` row names the bill."""
    from sqlalchemy import select

    from app.models.invoice import Invoice
    from app.services.erp import ORPHAN_DOCUMENT_ID_KEY, ORPHAN_DOCUMENT_NUMBER_KEY

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_erp_failed_invoice(mk, info.org_id, state_data={"erp_retries": 3})
    result = await _posted_total_failure("mismatch")

    class _Adapter:
        async def post_invoice(self, payload):
            return result

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await retry_erp(s, inv)
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
            await send_to_erp_internal(s, inv, erp_config={"type": "x"})

    inv, inst, rows = await _reload(mk, inv_id)
    assert inv.status == InvoiceStatus.failed
    assert inst.state == "failed"
    assert inst.state_data[ORPHAN_DOCUMENT_ID_KEY] == "XERO-BILL-7"
    assert inst.state_data[ORPHAN_DOCUMENT_NUMBER_KEY] == "BILL-0007"
    # One attempt from a reset counter: the reset reached the database.
    assert inst.state_data["erp_retries"] == 1
    fail_row = next(r for r in rows if r.action == "invoice.erp_failed")
    assert fail_row.details["erp_document_id"] == "XERO-BILL-7"
    assert fail_row.details["erp_document_number"] == "BILL-0007"
    assert _PROVIDER_BODY_MARKER not in json.dumps(fail_row.details)


# ---------------------------------------------------------------------------
# A refusal is recorded as a code + params the browser localizes (§284)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_erp_account_refusal_names_the_unlinked_codes():
    """`account_not_linked` carries WHICH codes resolved to no ERP account —
    what the reader must sync or recode — beside the unchanged English."""
    from app.services.erp import _call_erp

    rows = [_line_item(gl_account="Job Materials"), _line_item(line_number=2, gl_account="6100")]
    db = _line_items_db(rows, accounts=[("Job Materials", None, None), ("6100", None, "ERP-6100")])
    with patch("httpx.AsyncClient") as cm:
        with pytest.raises(ErpPostRefusedError) as exc:
            await _call_erp(db, _invoice(), _NETSUITE_CFG)
    cm.assert_not_called()
    assert str(exc.value) == "NetSuite post refused: account_not_linked"
    assert exc.value.refusal == {
        "code": "account_not_linked",
        "params": {"provider": "NetSuite", "accounts": ["Job Materials"]},
    }


@pytest.mark.asyncio
async def test_call_erp_vendor_refusal_carries_its_code_and_provider():
    from app.services.erp import _call_erp

    with pytest.raises(ErpPostRefusedError) as exc:
        await _call_erp(
            _line_items_db([_line_item()], vendor_erp_id=None), _invoice(), _NETSUITE_CFG
        )
    assert exc.value.refusal == {"code": "vendor_not_linked", "params": {"provider": "NetSuite"}}


@pytest.mark.asyncio
async def test_failed_audit_row_carries_the_coded_refusal():
    from app.services.erp import erp_refusal_record

    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    recorder = _AuditRecorder()
    refusal = erp_refusal_record("vendor_not_linked", provider="QuickBooks Online")
    call_erp = AsyncMock(
        side_effect=ErpPostRefusedError(
            "QuickBooks Online post refused: vendor_not_linked", refusal=refusal
        )
    )
    with (
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch("app.services.erp._call_erp", call_erp),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=_instance())),
        patch("app.services.erp.asyncio.sleep", AsyncMock()),
    ):
        await send_to_erp_internal(AsyncMock(), inv)
    fail_row = next(r for r in recorder.rows if r["action"] == "invoice.erp_failed")
    assert fail_row["details"]["error"] == "QuickBooks Online post refused: vendor_not_linked"
    assert fail_row["details"]["refusal"] == {
        "code": "vendor_not_linked",
        "params": {"provider": "QuickBooks Online"},
    }


@pytest.mark.asyncio
async def test_a_missing_oauth_connection_is_recorded_as_not_connected():
    from app.services.erp_oauth import ErpNotConnectedError

    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    recorder = _AuditRecorder()
    with (
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch("app.services.erp._call_erp", AsyncMock(side_effect=ErpNotConnectedError("x"))),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=_instance())),
        patch("app.services.erp.asyncio.sleep", AsyncMock()),
    ):
        await send_to_erp_internal(AsyncMock(), inv)
    fail_row = next(r for r in recorder.rows if r["action"] == "invoice.erp_failed")
    assert fail_row["details"]["refusal"] == {"code": "not_connected", "params": {}}


@pytest.mark.asyncio
async def test_a_transient_failure_records_no_refusal():
    """An HTTP failure has no stable code to state; the modal shows its text."""
    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    recorder = _AuditRecorder()
    with (
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch("app.services.erp._call_erp", AsyncMock(side_effect=RuntimeError("boom"))),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=_instance())),
        patch("app.services.erp.asyncio.sleep", AsyncMock()),
    ):
        await send_to_erp_internal(AsyncMock(), inv)
    fail_row = next(r for r in recorder.rows if r["action"] == "invoice.erp_failed")
    assert "refusal" not in fail_row["details"]


# ---------------------------------------------------------------------------
# A deterministic 4xx is never retried; an answer that can change still is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (400, False),
        (403, False),
        (404, False),
        (413, False),
        (422, False),
        (401, True),
        (408, True),
        (409, True),
        (423, True),
        (425, True),
        (429, True),
        (500, True),
        (502, True),
        (503, True),
        (200, True),
    ],
)
def test_erp_status_retryable(status, retryable):
    from app.services.erp_adapters.base import erp_http_failure, erp_status_retryable

    assert erp_status_retryable(status) is retryable
    result = erp_http_failure("Xero", status)
    assert (result.success, result.retryable) == (False, retryable)
    assert result.message.startswith(f"Xero post failed: HTTP {status} (")


def _merge_send(status: int):
    """Run a whole send through the real `_call_erp` and the Merge.dev adapter,
    whose create answers ``status`` every time: the invoice, the audit
    recorder, the config and the response."""
    resp = AsyncMock()
    resp.status_code = status
    resp.headers = {"content-type": "application/json"}
    resp.json = lambda: {"errors": [{"detail": "Vendor Acme 12-3456789"}]}
    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    recorder = _AuditRecorder()
    cfg = {"integration_method": "merge_dev", "api_key": "k", "account_token": "t"}
    return inv, recorder, cfg, resp


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "attempts"), [(400, 1), (422, 1), (503, 3), (429, 3)])
async def test_send_to_erp_retries_only_an_answer_that_can_change(status, attempts):
    inv, recorder, cfg, resp = _merge_send(status)
    sleep_mock = AsyncMock()
    with (
        patch("httpx.AsyncClient") as cm,
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=_instance())),
        patch("app.services.erp.asyncio.sleep", sleep_mock),
    ):
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=resp)
        await send_to_erp_internal(_line_items_db([]), inv, erp_config=cfg)

    assert inv.status == InvoiceStatus.failed
    assert client.post.await_count == attempts
    assert sleep_mock.await_count == attempts - 1
    fail_row = next(r for r in recorder.rows if r["action"] == "invoice.erp_failed")
    assert fail_row["details"]["retries"] == attempts
    assert fail_row["details"]["error"].startswith(f"Merge.dev post failed: HTTP {status}")


@pytest.mark.asyncio
async def test_a_coded_erp_refusal_records_its_params_on_the_audit_row():
    """A QuickBooks Fault refusal (`refusal_params`: the Intuit code, the GL
    codes) reaches `invoice.erp_failed.refusal` beside the provider."""
    from app.services.erp_adapters.base import ErpPostResult

    result = ErpPostResult(
        success=False,
        message="QuickBooks Online post refused: account_inactive (Intuit fault 2500)",
        retryable=False,
        refusal_code="account_inactive",
        refusal_provider="QuickBooks Online",
        refusal_params={"intuit_code": "2500", "accounts": ["6100"]},
    )
    adapter = SimpleNamespace(post_invoice=AsyncMock(return_value=result))
    inv = _invoice(status=InvoiceStatus.sending_to_erp)
    recorder = _AuditRecorder()
    with (
        patch("app.services.erp.get_erp_adapter", return_value=adapter),
        patch("app.services.workflow_engine.dispatch_audit", new=recorder),
        patch("app.services.erp.get_workflow_instance", AsyncMock(return_value=_instance())),
        patch("app.services.erp.asyncio.sleep", AsyncMock()),
    ):
        await send_to_erp_internal(_line_items_db([]), inv, erp_config={"type": "x"})

    assert adapter.post_invoice.await_count == 1
    fail_row = next(r for r in recorder.rows if r["action"] == "invoice.erp_failed")
    assert fail_row["details"]["retries"] == 1
    assert fail_row["details"]["refusal"] == {
        "code": "account_inactive",
        "params": {"intuit_code": "2500", "accounts": ["6100"], "provider": "QuickBooks Online"},
    }


# ---------------------------------------------------------------------------
# _call_erp — whether the ERP may already hold the bill (decisions §286)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "unknown"),
    [
        ("retryable", True),
        ("raised", True),
        # Fail-closed: from here a refusal made before anything was sent cannot
        # be told from one made after the create went through (a QuickBooks
        # read-back that lost its connection answers `not_connected`; a Xero
        # read-back raises it), so every non-success counts.
        ("refused", True),
        ("not_connected", True),
        # ...except an attempt the adapter states left nothing: the ERP's
        # create refused it outright (QuickBooks' closed books, 2026-10-20).
        ("refused_not_posted", False),
        ("success", False),
    ],
)
async def test_call_erp_records_whether_the_attempt_outcome_is_unknown(answer, unknown):
    """Only a success says definitely where the bill stands."""
    from app.services.erp import OUTCOME_UNKNOWN_KEY, _call_erp
    from app.services.erp_adapters.base import ErpPostResult, erp_refusal
    from app.services.erp_oauth import ErpNotConnectedError

    class _Adapter:
        async def post_invoice(self, payload):
            if answer == "raised":
                raise TimeoutError("read timed out")
            if answer == "not_connected":
                raise ErpNotConnectedError("quickbooks_online")
            if answer == "retryable":
                return ErpPostResult(success=False, message="HTTP 503", retryable=True)
            if answer == "refused":
                return erp_refusal("Xero", "vendor_not_linked")
            if answer == "refused_not_posted":
                result = erp_refusal("QuickBooks Online", "period_closed")
                result.not_posted = True
                return result
            return ErpPostResult(success=True, erp_document_id="B-1")

    inst = _instance(state_data={})
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        if answer == "success":
            await _call_erp(_line_items_db([]), _invoice(), {"type": "x"}, instance=inst)
        else:
            with pytest.raises((RuntimeError, TimeoutError)):
                await _call_erp(_line_items_db([]), _invoice(), {"type": "x"}, instance=inst)
    assert inst.state_data[OUTCOME_UNKNOWN_KEY] is unknown


@pytest.mark.asyncio
async def test_a_refusal_after_an_unknown_outcome_keeps_it_unknown():
    """The refusal posted nothing, but the earlier 503 may have."""
    from app.services.erp import OUTCOME_UNKNOWN_KEY, _call_erp, possibly_posted
    from app.services.erp_adapters.base import erp_refusal

    class _Adapter:
        async def post_invoice(self, payload):
            return erp_refusal("Xero", "vendor_not_linked")

    inst = _instance(state_data={"erp_retries": 1, OUTCOME_UNKNOWN_KEY: True})
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        with pytest.raises(ErpPostRefusedError):
            await _call_erp(_line_items_db([]), _invoice(), {"type": "x"}, instance=inst)
    assert inst.state_data[OUTCOME_UNKNOWN_KEY] is True
    assert possibly_posted(inst.state_data)


@pytest.mark.asyncio
async def test_a_refusal_that_posted_nothing_still_keeps_an_earlier_unknown():
    """``not_posted`` speaks for this attempt only: the earlier 503 may still
    have left a bill."""
    from app.services.erp import OUTCOME_UNKNOWN_KEY, _call_erp, possibly_posted
    from app.services.erp_adapters.base import erp_refusal

    class _Adapter:
        async def post_invoice(self, payload):
            result = erp_refusal("QuickBooks Online", "period_closed")
            result.not_posted = True
            return result

    inst = _instance(state_data={"erp_retries": 1, OUTCOME_UNKNOWN_KEY: True})
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        with pytest.raises(ErpPostRefusedError):
            await _call_erp(_line_items_db([]), _invoice(), {"type": "x"}, instance=inst)
    assert possibly_posted(inst.state_data)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, False),
        ({"erp_retries": 0}, False),
        # A row written before the key existed is read fail-closed.
        ({"erp_retries": 3, "last_error": "HTTP 503"}, True),
        ({"erp_retries": 0, "last_error": "HTTP 503"}, True),
        # Once pinned, the recorded value wins over the legacy reading.
        ({"last_error": "refused", "erp_outcome_unknown": False}, False),
        ({"erp_outcome_unknown": True}, True),
        ({"erp_outcome_unknown": False, "erp_pending_job_id": "job-41"}, True),
        ({"erp_outcome_unknown": False, "erp_orphan_document_id": "B-7"}, True),
        ({"erp_outcome_unknown": False, "erp_reference": "B-1"}, True),
    ],
)
def test_possibly_posted(state, expected):
    from app.services.erp import possibly_posted

    assert possibly_posted(state) is expected


@pytest.mark.asyncio
async def test_call_erp_commits_the_unknown_outcome_before_the_adapter_call():
    """A worker that dies mid-call (a Lambda timeout SQS redelivers) leaves the
    committed row saying the bill may exist, not the state before the call."""
    from app.services.erp import OUTCOME_UNKNOWN_KEY, _call_erp
    from app.services.erp_adapters.base import ErpPostResult

    inst = _instance(state_data={})
    db = _line_items_db([])
    seen_at_call: list[tuple[int, object]] = []

    class _Adapter:
        async def post_invoice(self, payload):
            seen_at_call.append((db.commit.await_count, inst.state_data[OUTCOME_UNKNOWN_KEY]))
            return ErpPostResult(success=True, erp_document_id="B-1")

    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        await _call_erp(db, _invoice(), {"type": "x"}, instance=inst)
    assert seen_at_call == [(1, True)]
    assert inst.state_data[OUTCOME_UNKNOWN_KEY] is False


@pytest.mark.asyncio
async def test_a_success_clears_an_earlier_unknown_and_names_the_bill():
    """The success is the bill of record, recorded with the result itself so a
    failure before the caller's transitions commit still reads as posted."""
    from app.services.erp import OUTCOME_UNKNOWN_KEY, _call_erp, possibly_posted
    from app.services.erp_adapters.base import ErpPostResult

    class _Adapter:
        async def post_invoice(self, payload):
            return ErpPostResult(success=True, erp_document_id="B-1")

    inst = _instance(state_data={"erp_retries": 1, OUTCOME_UNKNOWN_KEY: True})
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        await _call_erp(_line_items_db([]), _invoice(), {"type": "x"}, instance=inst)
    assert inst.state_data[OUTCOME_UNKNOWN_KEY] is False
    assert inst.state_data["erp_reference"] == "B-1"
    assert possibly_posted(inst.state_data)


@pytest.mark.asyncio
async def test_a_success_stamps_the_bill_and_its_deep_link_on_the_invoice():
    """The invoice row carries the bill of record, so the invoice can link to
    it: found live, a Xero push left the bill id only in state_data and the
    audit row, where no screen showed it."""
    from app.services.erp import _call_erp
    from app.services.erp_adapters.base import ErpPostResult

    bill = "8f1c2a3b-0000-4000-8000-1234567890ab"

    class _Adapter:
        async def post_invoice(self, payload):
            return ErpPostResult(success=True, erp_document_id=bill, erp_document_number="7")

    inv = _invoice()
    config = {"type": "xero", "integration_method": "direct"}
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        await _call_erp(_line_items_db([]), inv, config, instance=_instance(state_data={}))
    assert inv.erp_document_id == bill
    assert inv.erp_document_url == (
        f"https://go.xero.com/AccountsPayable/View.aspx?InvoiceID={bill}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "kept"),
    [
        # A failure that left a bill names an orphan, not the bill of record,
        # and leaves the earlier bill of record alone.
        ({"success": False, "erp_document_id": "ORPHAN-1", "retryable": False}, "EARLIER"),
        # A number is not an id, and "UNKNOWN" is what the reference falls back
        # to. A confirmed push with no id clears the pair: the earlier id may
        # name another ERP's bill if the org has switched.
        ({"success": True, "erp_document_number": "7"}, None),
        ({"success": True}, None),
    ],
)
async def test_only_a_confirmed_id_is_stamped_on_the_invoice(result, kept):
    """Only the ERP's id is stamped, and only by a confirmed push."""
    from app.services.erp import _call_erp
    from app.services.erp_adapters.base import ErpPostResult

    class _Adapter:
        async def post_invoice(self, payload):
            return ErpPostResult(**result)

    inv = _invoice()
    inv.erp_document_id = "EARLIER"
    inv.erp_document_url = None
    with patch("app.services.erp.get_erp_adapter", return_value=_Adapter()):
        call = _call_erp(_line_items_db([]), inv, {"type": "x"}, instance=_instance())
        if result["success"]:
            await call
        else:
            with pytest.raises(ErpPostRefusedError):
                await call
    assert inv.erp_document_id == kept
    assert inv.erp_document_url is None


@pytest.mark.asyncio
async def test_call_erp_refuses_a_bill_due_before_it_is_dated():
    """Live QuickBooks sandbox, 2026-10-20: a bill dated after its due date
    posted, and QuickBooks' own UI then flagged it invalid. Whatever path made
    the invoice, the push refuses it before any adapter is built."""
    from datetime import date

    from app.services.erp import INVOICE_DUE_BEFORE_DATE, _call_erp
    from app.services.erp_adapters.base import ErpPostResult

    inv = _invoice()
    inv.invoice_date, inv.due_date = date(2026, 10, 20), date(2026, 10, 15)
    with patch("app.services.erp.get_erp_adapter") as get_adapter:
        with pytest.raises(ErpPostRefusedError) as exc:
            await _call_erp(_line_items_db([]), inv, {"type": "x"}, instance=_instance())
    get_adapter.assert_not_called()
    assert exc.value.refusal == {"code": INVOICE_DUE_BEFORE_DATE, "params": {}}

    # Due the day it is dated is fine.
    inv.due_date = inv.invoice_date
    adapter = AsyncMock()
    adapter.post_invoice.return_value = ErpPostResult(success=True, erp_document_id="B-1")
    with patch("app.services.erp.get_erp_adapter", return_value=adapter):
        assert await _call_erp(_line_items_db([]), inv, {"type": "x"}) == "B-1"
