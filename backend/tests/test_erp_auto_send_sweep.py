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
* another replica holding the tenant's lock makes this one skip the tenant;
* a send that raises is counted as a failure, so the health report says so;
* the per-tenant cap takes the oldest approval first;
* the sweep is off by default and registered with the health report.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

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


async def test_another_replica_holding_the_tenant_lock_makes_this_one_skip_it(realdb, dispatched):
    from app.database import get_tenant_engine

    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_approved(realdb, approved_at=_ago(minutes=10))
    async with realdb.control_sessionmaker()() as s:
        db_name = (await s.get(Organization, realdb.info(TENANT).org_id)).db_name

    async with get_tenant_engine(db_name).connect() as other:
        other = await other.execution_options(isolation_level="AUTOCOMMIT")
        await other.execute(
            text("SELECT pg_advisory_lock(CAST(:k AS BIGINT))"), {"k": sweep.ADVISORY_LOCK_KEY}
        )
        try:
            busy = await sweep.run_auto_send_sweep_once()
        finally:
            await other.execute(
                text("SELECT pg_advisory_unlock(CAST(:k AS BIGINT))"),
                {"k": sweep.ADVISORY_LOCK_KEY},
            )

    assert busy.tenants_busy >= 1
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved

    # Released: the next tick takes it, and gives its own lock back.
    assert (await sweep.run_auto_send_sweep_once()).invoices_sent == 1
    async with get_tenant_engine(db_name).connect() as probe:
        probe = await probe.execution_options(isolation_level="AUTOCOMMIT")
        got = await probe.scalar(
            text("SELECT pg_try_advisory_lock(CAST(:k AS BIGINT))"), {"k": sweep.ADVISORY_LOCK_KEY}
        )
        assert got is True
        await probe.execute(
            text("SELECT pg_advisory_unlock(CAST(:k AS BIGINT))"), {"k": sweep.ADVISORY_LOCK_KEY}
        )


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
