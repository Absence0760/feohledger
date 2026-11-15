"""The auto-send recovery sweep (decisions §411).

An approval's "send to the ERP on approval" is post-commit work (§304), so a
process that dies between the approval's commit and the push leaves the invoice
`approved` with nothing on its trail to say the automation was skipped. The
sweep finds such approvals and runs them through `erp_send.send_after_approval`.

Pinned here, on real Postgres:

* a missed approval is sent, once, marked `recovered`, with no actor;
* a refusal is recorded once and not re-recorded on the next tick;
* the window: too fresh (the approval's own job may still run) and too old (a
  pre-§304 approval nobody expected to move) are both left alone;
* "since that approval": a refusal of an EARLIER approval does not answer a
  re-approval, and a payment void back to `approved` is not an approval;
* a snapshot without the flag, or a tenant on the mock ERP, is never touched;
* two replicas at once record one refusal, and a refusal of this approval
  already on the trail is not recorded again;
* an approval made before an OAuth ERP was connected is not recovered;
* a send that raises is counted as a failure, so the health report says so;
* the per-tenant cap takes the oldest approval first;
* the sweep is off by default and registered with the health report;
* a push stranded at `sending_to_erp` (§428) is re-driven or failed, and never
  re-driven while its own job sleeps between attempts, nor when no audit row
  records its entry (round-13 amendment).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import Settings, settings
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import AuditLog, WorkflowInstance
from app.services import erp_auto_send_sweep as sweep
from app.services.erp_send import AUTO_SEND_REFUSED_ACTION, AUTO_SEND_TRIGGER

TENANT = "a"
LIVE_ERP = {"type": "netsuite", "integration_method": "direct", "account_id": "1234567"}
MOCK_ERP = {"type": "mock", "integration_method": "direct"}

pytestmark = pytest.mark.plan("growth")


def _snapshot(*, auto_send: object = True, enabled: bool = True) -> dict:
    erp_config: dict = {"export_format": "json"}
    if auto_send is not None:
        erp_config["auto_send_on_approval"] = auto_send
    return {
        "steps": [
            {"number": 2, "type": "approval", "enabled": True, "config": {"required": True}},
            {"number": 3, "type": "erp_export", "enabled": enabled, "config": erp_config},
        ]
    }


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "erp_auto_send_sweep_enabled", True)
    monkeypatch.setattr(settings, "erp_auto_send_sweep_delay_seconds", 300)
    monkeypatch.setattr(settings, "erp_auto_send_sweep_lookback_hours", 24)
    monkeypatch.setattr(settings, "erp_auto_send_sweep_batch_size", 100)


@pytest.fixture
def dispatched(monkeypatch):
    calls: list[tuple[uuid.UUID, uuid.UUID | None]] = []

    async def _record(invoice_id, org_id, actor_id, **_kwargs):
        calls.append((invoice_id, actor_id))

    monkeypatch.setattr("app.services.erp_send.dispatch_erp", _record)
    return calls


async def _write_erp(realdb, erp: dict | None) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(TENANT).org_id)
        merged = dict(org.settings or {})
        if erp is None:
            merged.pop("erp", None)
        else:
            merged["erp"] = erp
        org.settings = merged
        flag_modified(org, "settings")
        await s.commit()


def _ago(**delta) -> datetime:
    return datetime.now(UTC) - timedelta(**delta)


async def _seed_approved(
    realdb,
    *,
    approved_at: datetime | None,
    snapshot: dict | None = None,
    status: InvoiceStatus = InvoiceStatus.approved,
) -> uuid.UUID:
    """An invoice at ``status`` whose trail records an approval at
    ``approved_at`` and nothing after it — the approval's job never ran."""
    from app.services.workflow_engine import get_or_create_workflow_definition

    info = realdb.info(TENANT)
    inv_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=info.org_id,
            invoice_number=f"SWEEP-{inv_id.hex[:8]}",
            vendor_name="Northwind Supplies",
            amount=Decimal("1234.56"),
            currency="USD",
            status=status,
            approved_by="Pat Manager",
            uploaded_by_id=None,
        )
        s.add(inv)
        await s.flush()
        defn = await get_or_create_workflow_definition(s, info.org_id)
        s.add(
            WorkflowInstance(
                correlation_id=inv.correlation_id,
                definition_id=defn.id,
                invoice_id=inv_id,
                current_step=2,
                state="active",
                state_data={},
                steps_config_snapshot=snapshot if snapshot is not None else _snapshot(),
            )
        )
        await s.commit()
    if approved_at is not None:
        await _trail(realdb, inv_id, "invoice.approved", approved_at, new_status="approved")
    return inv_id


async def _trail(
    realdb, inv_id, action: str, at: datetime, *, new_status: str | None = None
) -> None:
    async with realdb.sessionmaker(TENANT)() as s:
        inv = await s.get(Invoice, inv_id)
        details = {"new_status": new_status} if new_status else {}
        s.add(
            AuditLog(
                correlation_id=inv.correlation_id,
                organization_id=inv.organization_id,
                actor_id=None,
                action=action,
                entity_type="invoice",
                entity_id=inv_id,
                details=details,
                created_at=at,
            )
        )
        await s.commit()


async def _status(realdb, inv_id) -> InvoiceStatus:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.get(Invoice, inv_id)).status


async def _audit(realdb, inv_id, action: str) -> list[AuditLog]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(
            select(AuditLog).where(AuditLog.entity_id == inv_id, AuditLog.action == action)
        )
        return list(rows.scalars())


async def _open_duplicate(realdb, inv_id) -> None:
    from app.services.exception_service import create_exception

    async with realdb.sessionmaker(TENANT)() as s:
        await create_exception(
            s,
            exception_type="duplicate",
            severity="warning",
            description="detector output",
            organization_id=realdb.info(TENANT).org_id,
            invoice=await s.get(Invoice, inv_id),
        )
        await s.commit()


# ---------------------------------------------------------------------------
# a missed approval is recovered, once
# ---------------------------------------------------------------------------


async def test_a_missed_auto_send_is_sent_once_and_marked_recovered(realdb, dispatched):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_approved(realdb, approved_at=_ago(minutes=10))

    result = await sweep.run_auto_send_sweep_once()

    assert result.invoices_sent == 1
    assert result.failures == 0 and result.invoice_failures == 0
    assert dispatched == [(inv_id, None)]
    assert await _status(realdb, inv_id) == InvoiceStatus.sending_to_erp
    [submitted] = await _audit(realdb, inv_id, "invoice.erp_submitted")
    assert submitted.details["trigger"] == AUTO_SEND_TRIGGER
    assert submitted.details["recovered"] is True
    # No person started it.
    assert submitted.actor_id is None

    again = await sweep.run_auto_send_sweep_once()

    assert again.invoices_sent == 0
    assert dispatched == [(inv_id, None)]
    assert len(await _audit(realdb, inv_id, "invoice.erp_submitted")) == 1


async def test_a_refusal_is_recorded_once_and_the_invoice_stays_approved(realdb, dispatched):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_approved(realdb, approved_at=_ago(minutes=10))
    await _open_duplicate(realdb, inv_id)

    result = await sweep.run_auto_send_sweep_once()

    assert result.invoices_refused == 1
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved
    [refused] = await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION)
    assert refused.details["refusal"]["code"] == "exception_blocks_erp_export"
    assert refused.details["recovered"] is True
    assert "Northwind" not in str(refused.details)

    # The refusal now answers the approval: the next tick leaves it alone.
    again = await sweep.run_auto_send_sweep_once()

    assert again.invoices_refused == 0
    assert len(await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION)) == 1


# ---------------------------------------------------------------------------
# the window, and "since that approval"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "age",
    [timedelta(minutes=1), timedelta(hours=25)],
    ids=["too-fresh-its-own-job-may-run", "older-than-the-lookback"],
)
async def test_an_approval_outside_the_window_is_left_alone(realdb, dispatched, age):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_approved(realdb, approved_at=datetime.now(UTC) - age)

    result = await sweep.run_auto_send_sweep_once()

    assert result.invoices_sent == 0
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved


async def test_a_refusal_of_an_earlier_approval_does_not_answer_a_re_approval(realdb, dispatched):
    """Approved, refused, recalled, re-approved: the second approval's job
    never ran, and the first approval's refusal is not its answer."""
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_approved(realdb, approved_at=_ago(hours=3))
    await _trail(realdb, inv_id, AUTO_SEND_REFUSED_ACTION, _ago(hours=3) + timedelta(seconds=1))
    await _trail(
        realdb, inv_id, "invoice.approval_recalled", _ago(hours=2), new_status="ready_for_review"
    )
    await _trail(realdb, inv_id, "invoice.approved", _ago(minutes=10), new_status="approved")

    result = await sweep.run_auto_send_sweep_once()

    assert result.invoices_sent == 1
    assert dispatched == [(inv_id, None)]


async def test_a_void_back_to_approved_is_not_an_approval(realdb, dispatched):
    """A payment void returns the invoice to `approved`; it may already have a
    bill in the ERP, and nothing was approved, so nothing is auto-sent."""
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_approved(realdb, approved_at=_ago(hours=2))
    await _trail(
        realdb, inv_id, "invoice.payment_scheduled", _ago(hours=1), new_status="payment_scheduled"
    )
    await _trail(
        realdb,
        inv_id,
        "invoice.voided_return_to_approved",
        _ago(minutes=30),
        new_status="approved",
    )

    result = await sweep.run_auto_send_sweep_once()

    assert result.invoices_sent == 0
    assert dispatched == []


@pytest.mark.parametrize(
    "snapshot",
    [_snapshot(auto_send=False), _snapshot(auto_send=None), _snapshot(enabled=False)],
    ids=["flag-off", "flag-absent", "step-disabled"],
)
async def test_a_snapshot_that_does_not_say_so_is_never_a_candidate(realdb, dispatched, snapshot):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_approved(realdb, approved_at=_ago(minutes=10), snapshot=snapshot)

    result = await sweep.run_auto_send_sweep_once()

    assert result.invoices_sent == result.invoices_skipped == 0
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved


async def test_a_tenant_on_the_mock_erp_is_not_swept(realdb, dispatched):
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed_approved(realdb, approved_at=_ago(minutes=10))

    result = await sweep.run_auto_send_sweep_once()

    assert result.invoices_sent == 0
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved


# ---------------------------------------------------------------------------
# bounded, idempotent across replicas, and never silent
# ---------------------------------------------------------------------------


async def test_two_replicas_at_once_record_one_refusal(realdb, dispatched):
    """No cross-request lock: the invoice row lock serialises the two runs, and
    the second finds the first's refusal of this approval and does nothing."""
    import asyncio

    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_approved(realdb, approved_at=_ago(minutes=10))
    await _open_duplicate(realdb, inv_id)

    first, second = await asyncio.gather(
        sweep.run_auto_send_sweep_once(), sweep.run_auto_send_sweep_once()
    )

    assert first.invoices_refused + second.invoices_refused == 1
    assert first.invoice_failures == second.invoice_failures == 0
    assert dispatched == []
    assert len(await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION)) == 1


async def test_a_refusal_already_recorded_for_this_approval_is_not_recorded_again(
    realdb, dispatched
):
    """The approval's own slow job (a long bulk batch) and the sweep can both
    reach one approval; whichever is second writes nothing."""
    from app.services.erp_send import AUTO_SEND_SKIPPED, send_after_approval

    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_approved(realdb, approved_at=_ago(minutes=10))
    await _open_duplicate(realdb, inv_id)
    await _trail(realdb, inv_id, AUTO_SEND_REFUSED_ACTION, _ago(minutes=9))

    outcome = await send_after_approval(
        realdb.sessionmaker(TENANT)().bind,
        invoice_id=inv_id,
        org_id=realdb.info(TENANT).org_id,
        org_settings={"erp": LIVE_ERP},
        actor_id=None,
        recovered=True,
    )

    assert outcome == AUTO_SEND_SKIPPED
    assert len(await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION)) == 1


@pytest.mark.parametrize(
    ("connected_ago", "expect_sent"),
    [(timedelta(minutes=5), False), (timedelta(hours=2), True)],
    ids=["approved-before-the-erp-was-connected", "approved-after-it"],
)
async def test_an_approval_before_the_oauth_erp_was_connected_is_not_recovered(
    realdb, dispatched, connected_ago, expect_sent
):
    """With no live ERP the approval's job writes nothing, so its trail looks
    like a crash's; the connection's moment tells them apart."""
    connected_at = (datetime.now(UTC) - connected_ago).isoformat()
    await _write_erp(realdb, {**LIVE_ERP, "oauth": {"connected_at": connected_at}})
    inv_id = await _seed_approved(realdb, approved_at=_ago(minutes=10))

    result = await sweep.run_auto_send_sweep_once()

    assert result.invoices_sent == (1 if expect_sent else 0)
    assert dispatched == ([(inv_id, None)] if expect_sent else [])


async def test_a_send_that_raises_is_counted_so_the_health_report_says_so(
    realdb, dispatched, monkeypatch
):
    from app.services.sweep_health import extract_counts, failure_count

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("vendor Northwind leaked into an error string")

    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_approved(realdb, approved_at=_ago(minutes=10))
    monkeypatch.setattr("app.services.erp_send.start_erp_send", _boom)

    result = await sweep.run_auto_send_sweep_once()

    assert result.invoice_failures == 1
    assert failure_count(extract_counts(result)) == 1
    assert await _status(realdb, inv_id) == InvoiceStatus.approved
    assert await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION) == []


async def test_the_cap_takes_the_oldest_approval_first(realdb, dispatched, monkeypatch):
    await _write_erp(realdb, LIVE_ERP)
    newer = await _seed_approved(realdb, approved_at=_ago(minutes=20))
    older = await _seed_approved(realdb, approved_at=_ago(hours=2))
    monkeypatch.setattr(settings, "erp_auto_send_sweep_batch_size", 1)

    first = await sweep.run_auto_send_sweep_once()

    assert first.invoices_sent == 1
    assert dispatched == [(older, None)]
    assert await _status(realdb, newer) == InvoiceStatus.approved

    second = await sweep.run_auto_send_sweep_once()

    assert second.invoices_sent == 1
    assert dispatched == [(older, None), (newer, None)]


# ---------------------------------------------------------------------------
# off by default, and registered
# ---------------------------------------------------------------------------


async def test_the_sweep_does_nothing_when_off(monkeypatch):
    monkeypatch.setattr(settings, "erp_auto_send_sweep_enabled", False)

    result = await sweep.run_auto_send_sweep_once()

    assert result == sweep.AutoSendSweepResult()


def test_the_sweep_defaults_off_and_is_registered_with_the_health_report():
    from app.services.sweep_health import SWEEP_ENABLED_FLAGS, SWEEP_ERP_AUTO_SEND

    assert Settings.model_fields["erp_auto_send_sweep_enabled"].default is False
    assert SWEEP_ENABLED_FLAGS[SWEEP_ERP_AUTO_SEND] == "erp_auto_send_sweep_enabled"


# ---------------------------------------------------------------------------
# a directly configured ERP's `configured_at` fences the recovery (§428)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured_ago", "expect_sent"),
    [(timedelta(minutes=5), False), (timedelta(hours=2), True)],
    ids=["approved-before-the-erp-was-saved", "approved-after-it"],
)
async def test_an_approval_before_a_direct_erp_was_configured_is_not_recovered(
    realdb, dispatched, configured_ago, expect_sent
):
    """NetSuite and its kind record no OAuth connection; `PATCH /api/organization`
    stamps `configured_at` instead, and the sweep fences on it the same way."""
    configured_at = (datetime.now(UTC) - configured_ago).isoformat()
    await _write_erp(realdb, {**LIVE_ERP, "configured_at": configured_at})
    inv_id = await _seed_approved(realdb, approved_at=_ago(minutes=10))

    result = await sweep.run_auto_send_sweep_once()

    assert result.invoices_sent == (1 if expect_sent else 0)
    assert dispatched == ([(inv_id, None)] if expect_sent else [])


def test_the_fence_is_the_later_of_the_two_moments():
    early, late = _ago(hours=3), _ago(hours=1)
    both = {
        "erp": {"configured_at": late.isoformat(), "oauth": {"connected_at": early.isoformat()}}
    }
    assert sweep._connected_at(both) == late
    assert sweep._connected_at({"erp": {"configured_at": "not a time"}}) is None
    assert sweep._connected_at({"erp": "netsuite"}) is None
    assert sweep._connected_at(None) is None


# ---------------------------------------------------------------------------
# a push stranded at `sending_to_erp` (§428)
# ---------------------------------------------------------------------------


@pytest.fixture
def redriven(monkeypatch):
    calls: list[tuple[uuid.UUID, uuid.UUID | None, bool]] = []

    async def _record(invoice_id, org_id, actor_id, *, inline=False):
        calls.append((invoice_id, actor_id, inline))

    monkeypatch.setattr("app.services.erp_auto_send_sweep.dispatch_erp", _record)
    return calls


async def _seed_stranded(
    realdb, *, started_at: datetime, state_data: dict | None = None
) -> uuid.UUID:
    """An invoice committed at `sending_to_erp` at ``started_at`` whose job never
    ran: the transition's audit row, and nothing after it."""
    inv_id = await _seed_approved(realdb, approved_at=None, status=InvoiceStatus.sending_to_erp)
    if state_data is not None:
        async with realdb.sessionmaker(TENANT)() as s:
            inst = (
                await s.execute(
                    select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id)
                )
            ).scalar_one()
            inst.state_data = state_data
            await s.commit()
    await _trail(realdb, inv_id, "invoice.erp_submitted", started_at, new_status="sending_to_erp")
    return inv_id


def _attempt_stamp(at: datetime) -> dict:
    from app.services.erp import (
        OUTCOME_UNKNOWN_AT_FINAL_KEY,
        OUTCOME_UNKNOWN_AT_KEY,
        OUTCOME_UNKNOWN_KEY,
    )

    return {
        "erp_retries": 0,
        OUTCOME_UNKNOWN_KEY: True,
        OUTCOME_UNKNOWN_AT_KEY: at.isoformat(),
        OUTCOME_UNKNOWN_AT_FINAL_KEY: False,
    }


async def test_a_push_stranded_before_its_dispatch_is_re_driven(realdb, redriven):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_stranded(realdb, started_at=_ago(minutes=10))

    result = await sweep.run_auto_send_sweep_once()

    assert result.pushes_redriven == 1
    assert redriven == [(inv_id, None, True)]
    # The re-drive itself writes nothing: the push's own outcome rows do.
    assert await _status(realdb, inv_id) == InvoiceStatus.sending_to_erp
    assert await _audit(realdb, inv_id, "invoice.erp_failed") == []


async def test_a_stamp_from_an_earlier_push_is_not_an_attempt_of_this_one(realdb, redriven):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_stranded(
        realdb, started_at=_ago(minutes=10), state_data=_attempt_stamp(_ago(days=3))
    )

    result = await sweep.run_auto_send_sweep_once()

    assert result.pushes_redriven == 1
    assert redriven == [(inv_id, None, True)]


@pytest.mark.parametrize("trail", ["too-fresh", "answered"])
async def test_a_push_still_in_its_own_hands_is_left_alone(realdb, redriven, trail):
    await _write_erp(realdb, LIVE_ERP)
    if trail == "too-fresh":
        inv_id = await _seed_stranded(realdb, started_at=_ago(minutes=2))
    else:
        inv_id = await _seed_stranded(realdb, started_at=_ago(minutes=10))
        await _trail(realdb, inv_id, "invoice.erp_confirmed", _ago(minutes=9))

    result = await sweep.run_auto_send_sweep_once()

    assert (result.pushes_redriven, result.pushes_interrupted) == (0, 0)
    assert redriven == []
    assert await _status(realdb, inv_id) == InvoiceStatus.sending_to_erp


async def test_an_attempt_inside_the_lookback_is_left_to_finish(realdb, redriven):
    """An attempt began (its stamp is committed before the adapter call) and
    nothing ended it: it may still be running, or be redelivered. Never
    re-driven beside it."""
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_stranded(
        realdb, started_at=_ago(minutes=30), state_data=_attempt_stamp(_ago(minutes=29))
    )

    result = await sweep.run_auto_send_sweep_once()

    assert (result.pushes_redriven, result.pushes_interrupted) == (0, 0)
    assert redriven == []
    assert await _status(realdb, inv_id) == InvoiceStatus.sending_to_erp


@pytest.mark.parametrize(
    "case",
    ["no-attempt-past-the-lookback", "attempt-past-the-lookback", "before-the-erp-was-saved"],
)
async def test_a_stranded_push_outside_the_window_fails_as_interrupted(realdb, redriven, case):
    """Never pushed on nobody's say-so: landed at `failed`, coded, where a
    person can retry it, and flagged when an attempt may have left the bill."""
    from app.services.erp import ERP_PUSH_INTERRUPTED

    if case == "before-the-erp-was-saved":
        await _write_erp(realdb, {**LIVE_ERP, "configured_at": _ago(minutes=5).isoformat()})
        inv_id = await _seed_stranded(realdb, started_at=_ago(minutes=10))
    elif case == "no-attempt-past-the-lookback":
        await _write_erp(realdb, LIVE_ERP)
        inv_id = await _seed_stranded(realdb, started_at=_ago(hours=30))
    else:
        await _write_erp(realdb, LIVE_ERP)
        inv_id = await _seed_stranded(
            realdb, started_at=_ago(hours=30), state_data=_attempt_stamp(_ago(hours=29))
        )

    result = await sweep.run_auto_send_sweep_once()

    assert result.pushes_interrupted == 1
    assert redriven == []
    assert await _status(realdb, inv_id) == InvoiceStatus.failed
    (row,) = await _audit(realdb, inv_id, "invoice.erp_failed")
    assert row.actor_id is None
    assert row.details["refusal"]["code"] == ERP_PUSH_INTERRUPTED
    attempted = case == "attempt-past-the-lookback"
    assert row.details["refusal"]["params"] == ({"possibly_posted": True} if attempted else {})

    # Answered: the next tick leaves it.
    again = await sweep.run_auto_send_sweep_once()
    assert (again.pushes_redriven, again.pushes_interrupted) == (0, 0)


async def test_a_re_driven_push_lands_through_send_to_erp_internal(realdb, monkeypatch):
    """The re-drive reaches the one push path, which gates on `sending_to_erp`
    and confirms the bill once; the next tick finds nothing left to do.

    The dispatcher's hand-off is replaced by what `erp_dispatch._run_local`
    does once it has a tenant session (its own engines are built from the
    global settings, which the harness's per-slot databases are not), as
    `test_erp_export_blocking_exceptions._push` does."""
    from unittest.mock import AsyncMock

    from app.services.erp import send_to_erp_internal

    call = AsyncMock(return_value="NS-BILL-1")
    monkeypatch.setattr("app.services.erp._call_erp", call)
    handed: list[tuple] = []

    async def _run(invoice_id, org_id, actor_id, *, inline=False):
        handed.append((invoice_id, actor_id, inline))
        async with realdb.sessionmaker(TENANT)() as s:
            inv = await s.get(Invoice, invoice_id)
            await send_to_erp_internal(s, inv, actor_id=actor_id, erp_config=LIVE_ERP)

    monkeypatch.setattr("app.services.erp_auto_send_sweep.dispatch_erp", _run)
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_stranded(realdb, started_at=_ago(minutes=10))

    first = await sweep.run_auto_send_sweep_once()
    second = await sweep.run_auto_send_sweep_once()

    assert first.pushes_redriven == 1
    assert second.pushes_redriven == 0
    assert handed == [(inv_id, None, True)]
    call.assert_awaited_once()
    assert await _status(realdb, inv_id) == InvoiceStatus.done
    assert len(await _audit(realdb, inv_id, "invoice.erp_confirmed")) == 1


async def test_a_push_sleeping_between_attempts_is_not_re_driven(realdb, redriven, monkeypatch):
    """The first attempt ends `not_posted` with nothing earlier unknown, so
    `_call_erp` drops the outcome-unknown stamp; the job then sleeps before its
    second attempt. Run the sweep inside that sleep: the push must read as
    attempted (the never-cleared attempt stamp, and `erp_retries` = 1) and be
    left to its own job, never re-driven beside it (round-13 amendment, §428)."""
    from app.services import erp as erp_service
    from app.services.erp import (
        ATTEMPT_STARTED_AT_KEY,
        OUTCOME_UNKNOWN_AT_KEY,
        send_to_erp_internal,
    )
    from app.services.erp_adapters.base import ErpPostResult

    class _Adapter:
        calls = 0

        async def post_invoice(self, payload):
            _Adapter.calls += 1
            if _Adapter.calls == 1:
                result = ErpPostResult(success=False, message="HTTP 503", retryable=True)
                result.not_posted = True
                return result
            return ErpPostResult(success=True, erp_document_id="NS-BILL-9")

    monkeypatch.setattr(erp_service, "get_erp_adapter", lambda config: _Adapter())

    mid_backoff: list = []

    async def _sweep_during_backoff(_seconds):
        async with realdb.sessionmaker(TENANT)() as s:
            inst = (
                await s.execute(
                    select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id)
                )
            ).scalar_one()
            mid_backoff.append(dict(inst.state_data))
        mid_backoff.append(await sweep.run_auto_send_sweep_once())

    monkeypatch.setattr(erp_service.asyncio, "sleep", _sweep_during_backoff)
    await _write_erp(realdb, LIVE_ERP)
    # The job runs late: its push entered `sending_to_erp` past the delay.
    inv_id = await _seed_stranded(realdb, started_at=_ago(minutes=10))

    async with realdb.sessionmaker(TENANT)() as s:
        inv = await s.get(Invoice, inv_id)
        await send_to_erp_internal(s, inv, actor_id=None, erp_config=MOCK_ERP)

    state, result = mid_backoff
    # The condition the old reading mistook for "no attempt".
    assert OUTCOME_UNKNOWN_AT_KEY not in state
    assert state["erp_retries"] == 1
    assert ATTEMPT_STARTED_AT_KEY in state
    assert (result.pushes_redriven, result.pushes_interrupted) == (0, 0)
    assert redriven == []
    # The job's own second attempt lands the push, once.
    assert _Adapter.calls == 2
    assert await _status(realdb, inv_id) == InvoiceStatus.done
    assert len(await _audit(realdb, inv_id, "invoice.erp_confirmed")) == 1


def test_a_retry_count_is_an_attempt_even_with_no_stamp():
    """A row from before the attempt stamp existed, mid-backoff: `erp_retries`
    alone proves an attempt of this push ended (every push resets it to 0), and
    it is dated at the push's entry so the lookback still releases it."""
    started = _ago(minutes=10)
    assert sweep._attempt_started_at({"erp_retries": 1}, started) == started
    assert sweep._attempt_started_at({"erp_retries": "garbled"}, started) == started
    assert sweep._attempt_started_at({"erp_retries": 0}, started) is None
    assert sweep._attempt_started_at({}, started) is None


async def _seed_untrailed(realdb, *, state_data: dict | None = None) -> uuid.UUID:
    """An invoice at `sending_to_erp` with NO audit row recording that entry
    (`audit_log` is append-only, so the trail is never written rather than
    deleted: a Lambda-shipped row that never landed)."""
    inv_id = await _seed_approved(realdb, approved_at=None, status=InvoiceStatus.sending_to_erp)
    if state_data is not None:
        async with realdb.sessionmaker(TENANT)() as s:
            inst = (
                await s.execute(
                    select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id)
                )
            ).scalar_one()
            inst.state_data = state_data
            await s.commit()
    return inv_id


async def _age_invoice_row(realdb, inv_id, at: datetime) -> None:
    from sqlalchemy import update

    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(update(Invoice).where(Invoice.id == inv_id).values(updated_at=at))
        await s.commit()


async def test_a_stranded_push_with_no_entry_row_fails_closed(realdb, redriven):
    """No audit row records the entry into `sending_to_erp`: the sweep cannot
    tell this push's attempts from an earlier one's, so it never re-drives it.
    It lands at `failed` coded `erp_push_interrupted` for a person (round-13
    amendment, §428), where it used to be dropped from the sweep for ever."""
    from app.services.erp import ERP_PUSH_INTERRUPTED

    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_untrailed(realdb)
    await _age_invoice_row(realdb, inv_id, _ago(minutes=10))

    result = await sweep.run_auto_send_sweep_once()

    assert (result.pushes_redriven, result.pushes_interrupted) == (0, 1)
    assert redriven == []
    assert await _status(realdb, inv_id) == InvoiceStatus.failed
    (row,) = await _audit(realdb, inv_id, "invoice.erp_failed")
    assert row.details["refusal"]["code"] == ERP_PUSH_INTERRUPTED


@pytest.mark.parametrize("case", ["row-touched-inside-the-delay", "attempt-stamped-recently"])
async def test_a_stranded_push_with_no_entry_row_is_left_while_it_may_be_live(
    realdb, redriven, case
):
    """Not failed under a push that may still be running: the invoice row was
    written inside the delay (a Lambda-shipped audit row may not have landed
    yet), or an attempt stamp inside the lookback says one began."""
    from app.services.erp import ATTEMPT_STARTED_AT_KEY

    await _write_erp(realdb, LIVE_ERP)
    if case == "row-touched-inside-the-delay":
        inv_id = await _seed_untrailed(realdb)
        await _age_invoice_row(realdb, inv_id, _ago(seconds=30))
    else:
        inv_id = await _seed_untrailed(
            realdb,
            state_data={"erp_retries": 1, ATTEMPT_STARTED_AT_KEY: _ago(minutes=9).isoformat()},
        )
        await _age_invoice_row(realdb, inv_id, _ago(minutes=10))

    result = await sweep.run_auto_send_sweep_once()

    assert (result.pushes_redriven, result.pushes_interrupted) == (0, 0)
    assert redriven == []
    assert await _status(realdb, inv_id) == InvoiceStatus.sending_to_erp
