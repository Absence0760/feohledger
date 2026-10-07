"""AI-read-invoice enforcement, overage billing and notices, end to end (§253).

Every test runs on the `realdb` harness as tenant "a": the gate is a SQL count
plus a control-plane plan lookup, the pause is a real state transition, the
overage report reads committed rows and writes a settings marker under a row
lock, and the notices write real in-app rows. Mocks would prove none of that.

The extraction tests drive `run_extraction` itself with the platform provider
forced to `claude_vision` (billable) and the document fetch + adapter stubbed,
so no network and no model are involved — only the billing decisions are real.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm.attributes import flag_modified

from app.models.invoice import Invoice, InvoiceStatus
from app.models.notification import EVENT_AI_INVOICE_USAGE, Notification
from app.models.organization import Organization
from app.models.usage import ExtractionUsage
from app.models.workflow import AuditLog
from app.services.billing.ai_invoice_meter import count_ai_invoices, period_of
from app.services.billing.ai_overage import (
    REPORTED_KEY,
    report_ai_overage,
    run_ai_overage_reconcile_once,
)
from app.services.billing.ai_usage_notices import NOTICES_KEY, send_due_ai_usage_notices
from app.services.billing.plan_catalog import ensure_plan_catalog, ensure_subscription
from app.services.billing_adapters import mock_adapter
from app.services.extraction_adapters.base import ExtractedField, ExtractionResult
from app.services.post_commit import drain_post_commit

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_recorded_events():
    mock_adapter.RECORDED_METER_EVENTS.clear()
    yield
    mock_adapter.RECORDED_METER_EVENTS.clear()


@pytest.fixture
def _audit_engine_on_loop(monkeypatch, realdb):
    """`dispatch_auth_audit` writes on THIS test's loop (see test_billing_webhook)."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.database import _make_tenant_url

    db_name_by_org = {info.org_id: info.db_name for info in realdb.tenants.values()}

    async def _resolve(organization_id):
        return db_name_by_org[organization_id]

    monkeypatch.setattr("app.services.audit_dispatch._resolve_tenant_db_name", _resolve)
    monkeypatch.setattr(
        "app.database.get_tenant_engine",
        lambda db_name: create_async_engine(_make_tenant_url(db_name), poolclass=NullPool),
    )


async def _seed_counted(realdb, org_id, n, *, invoice_ids=()):
    """``n`` counted AI reads this period (plus the given invoice ids)."""
    period = period_of()
    ids = [*invoice_ids, *(uuid.uuid4() for _ in range(n - len(invoice_ids)))]
    async with realdb.sessionmaker("a")() as s:
        s.add_all(
            ExtractionUsage(
                id=uuid.uuid4(),
                invoice_id=inv,
                provider="claude_vision",
                program_type="platform",
                period=period,
                success=True,
                organization_id=org_id,
            )
            for inv in ids
        )
        await s.commit()


async def _subscribe(realdb, org_id, plan_code):
    async with realdb.control_sessionmaker()() as s:
        await ensure_plan_catalog(s)
        sub = await ensure_subscription(s, organization_id=org_id, plan_code=plan_code)
        await s.commit()
    assert sub is not None


async def _set_billing(realdb, org_id, **values):
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, org_id)
        settings = dict(org.settings or {})
        billing = dict(settings.get("billing") or {})
        billing.update(values)
        settings["billing"] = billing
        org.settings = settings
        flag_modified(org, "settings")
        await s.commit()


async def _billing_settings(realdb, org_id) -> dict:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, org_id)
        await s.refresh(org)
        return dict((org.settings or {}).get("billing") or {})


async def _pending_invoice(realdb, org_id, *, warnings=None) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        s.add(
            Invoice(
                id=inv_id,
                invoice_number="",
                vendor_name="",
                description="",
                amount=Decimal("0"),
                currency="USD",
                status=InvoiceStatus.pending,
                organization_id=org_id,
                file_key=f"invoices/{inv_id}.pdf",
                warnings=warnings,
            )
        )
        await s.commit()
    return inv_id


class _ReadingAdapter:
    """Stands in for the paid vision model; records that it was called."""

    provider_name = "claude_vision"

    def __init__(self):
        self.calls = 0

    async def extract(self, **_kwargs):
        self.calls += 1
        return ExtractionResult(
            success=True,
            overall_confidence=0.95,
            vendor_name=ExtractedField("Meter Vendor", 0.95),
            invoice_number=ExtractedField(f"AI-{uuid.uuid4().hex[:8]}", 0.95),
            amount=ExtractedField("120.00", 0.95),
            line_items=[],
            provider="claude_vision",
        )


async def _extract(realdb, inv_id, adapter, *, org_settings=None):
    """Run the real `run_extraction` with a billable platform provider."""
    from app.config import settings
    from app.services.extraction import run_extraction

    with (
        patch.object(settings, "extraction_provider", "claude_vision"),
        patch(
            "app.services.storage._get_object",
            AsyncMock(return_value=(b"%PDF-1.4 scanned, no text layer", "application/pdf")),
        ),
        patch(
            "app.services.extraction_adapters.get_extraction_adapter",
            lambda _config: adapter,
        ),
    ):
        async with realdb.sessionmaker("a")() as s:
            inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
            await run_extraction(s, inv, org_settings=org_settings or {})
    await drain_post_commit()
    async with realdb.sessionmaker("a")() as s:
        return (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()


async def _count(realdb, org_id) -> int:
    async with realdb.sessionmaker("a")() as s:
        return await count_ai_invoices(s, organization_id=org_id, period=period_of())


async def _ai_notices(realdb) -> list[Notification]:
    async with realdb.sessionmaker("a")() as s:
        return list(
            (
                await s.execute(
                    select(Notification).where(Notification.event_type == EVENT_AI_INVOICE_USAGE)
                )
            )
            .scalars()
            .all()
        )


def _paused_warning(inv: Invoice) -> dict | None:
    return next((w for w in inv.warnings or [] if w.get("type") == "ai_reading_paused"), None)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


async def test_free_org_at_its_limit_lands_the_invoice_for_manual_entry(realdb):
    """No plan reads as Free: 100 used → the model is NOT called, the invoice
    goes back to `new` (not `failed`) with a coded, localizable reason."""
    org_id = realdb.info("a").org_id
    await _seed_counted(realdb, org_id, 100)
    inv_id = await _pending_invoice(realdb, org_id)
    adapter = _ReadingAdapter()

    inv = await _extract(realdb, inv_id, adapter)

    assert adapter.calls == 0, "a paused org must not spend a model read"
    assert inv.status == InvoiceStatus.new
    finding = _paused_warning(inv)
    assert finding is not None
    assert finding["code"] == "ai_allowance_reached"
    assert finding["params"] == {"included": 100}
    assert await _count(realdb, org_id) == 100  # nothing new counted
    async with realdb.sessionmaker("a")() as s:
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_id == inv_id)))
            .scalars()
            .all()
        )
    assert "invoice.ai_reading_paused" in actions


async def test_a_reread_of_an_invoice_counted_this_month_is_allowed(realdb):
    org_id = realdb.info("a").org_id
    inv_id = await _pending_invoice(
        realdb,
        org_id,
        warnings=[{"type": "ai_reading_paused", "severity": "warning", "code": "x"}],
    )
    await _seed_counted(realdb, org_id, 100, invoice_ids=[inv_id])
    adapter = _ReadingAdapter()

    inv = await _extract(realdb, inv_id, adapter)

    assert adapter.calls == 1
    assert inv.status == InvoiceStatus.ready_for_review
    assert _paused_warning(inv) is None, "a read that ran supersedes the pause notice"
    assert await _count(realdb, org_id) == 100, "a re-read counts once"


async def test_a_byok_org_is_never_paused(realdb):
    org_id = realdb.info("a").org_id
    await _seed_counted(realdb, org_id, 100)
    inv_id = await _pending_invoice(realdb, org_id)
    adapter = _ReadingAdapter()

    byok = {"extraction": {"program_type": "byok", "provider": "claude_vision"}}
    inv = await _extract(realdb, inv_id, adapter, org_settings=byok)

    assert adapter.calls == 1
    assert inv.status == InvoiceStatus.ready_for_review
    assert await _count(realdb, org_id) == 100


async def test_paid_overage_reads_bills_one_event_and_sends_the_100_percent_notice(realdb):
    org_id = realdb.info("a").org_id
    await _subscribe(realdb, org_id, "growth")
    await _seed_counted(realdb, org_id, 500)
    inv_id = await _pending_invoice(realdb, org_id)
    adapter = _ReadingAdapter()

    inv = await _extract(realdb, inv_id, adapter)

    assert adapter.calls == 1 and inv.status == InvoiceStatus.ready_for_review
    assert await _count(realdb, org_id) == 501
    events = mock_adapter.RECORDED_METER_EVENTS
    assert [e.identifier for e in events] == [f"ai-overage:{org_id}:{period_of()}:1"]
    assert events[0].value == "1"
    billing = await _billing_settings(realdb, org_id)
    assert billing[REPORTED_KEY][period_of()] == 1
    # 80% and 100% are both due; ONE notice goes out, for the more severe.
    assert billing[NOTICES_KEY][period_of()] == ["80", "100"]
    notices = await _ai_notices(realdb)
    assert notices and all("All 500" in n.title for n in notices)


async def test_the_spending_cap_pauses_a_paid_org(realdb):
    org_id = realdb.info("a").org_id
    await _subscribe(realdb, org_id, "growth")
    await _set_billing(realdb, org_id, monthly_spend_cap="0.00")
    await _seed_counted(realdb, org_id, 500)
    inv_id = await _pending_invoice(realdb, org_id)
    adapter = _ReadingAdapter()

    inv = await _extract(realdb, inv_id, adapter)

    assert adapter.calls == 0
    assert inv.status == InvoiceStatus.new
    assert _paused_warning(inv)["code"] == "ai_spend_cap_reached"
    assert mock_adapter.RECORDED_METER_EVENTS == []
    billing = await _billing_settings(realdb, org_id)
    assert "cap" in billing[NOTICES_KEY][period_of()]


# ---------------------------------------------------------------------------
# Overage reporting
# ---------------------------------------------------------------------------


async def test_overage_report_is_idempotent_and_clamped_to_the_cap(realdb):
    org_id = realdb.info("a").org_id
    await _subscribe(realdb, org_id, "growth")
    await _set_billing(realdb, org_id, monthly_spend_cap="0.20")
    await _seed_counted(realdb, org_id, 503)  # 3 over; the cap pays for 2

    async with realdb.sessionmaker("a")() as s:
        first = await report_ai_overage(s, organization_id=org_id)
        again = await report_ai_overage(s, organization_id=org_id)
    assert first.newly_reported == 2 and again.newly_reported == 0
    assert len(mock_adapter.RECORDED_METER_EVENTS) == 2

    # Lift the cap: the third unit becomes billable and is reported once.
    await _set_billing(realdb, org_id, monthly_spend_cap=None)
    async with realdb.sessionmaker("a")() as s:
        third = await report_ai_overage(s, organization_id=org_id)
    assert third.newly_reported == 1
    assert [e.identifier.rsplit(":", 1)[1] for e in mock_adapter.RECORDED_METER_EVENTS] == [
        "1",
        "2",
        "3",
    ]


async def test_free_reports_no_overage(realdb):
    org_id = realdb.info("a").org_id
    await _seed_counted(realdb, org_id, 150)  # race overshoot past Free's 100
    async with realdb.sessionmaker("a")() as s:
        report = await report_ai_overage(s, organization_id=org_id)
    assert report.newly_reported == 0 and mock_adapter.RECORDED_METER_EVENTS == []


async def test_reconcile_sweep_reports_what_the_post_read_leg_missed(realdb):
    org_id = realdb.info("a").org_id
    await _subscribe(realdb, org_id, "growth")
    await _seed_counted(realdb, org_id, 502)

    result = await run_ai_overage_reconcile_once()

    assert result.units_reported >= 2
    mine = [e for e in mock_adapter.RECORDED_METER_EVENTS if str(org_id) in e.identifier]
    assert len(mine) == 2
    second = await run_ai_overage_reconcile_once()
    assert [e for e in mock_adapter.RECORDED_METER_EVENTS if str(org_id) in e.identifier] == mine
    assert second.failures == 0


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------


async def test_each_threshold_is_announced_once(realdb):
    org_id = realdb.info("a").org_id
    await _seed_counted(realdb, org_id, 80)  # Free: 80%

    async with realdb.sessionmaker("a")() as s:
        assert await send_due_ai_usage_notices(s, organization_id=org_id) == ["80"]
        assert await send_due_ai_usage_notices(s, organization_id=org_id) == []
    first = await _ai_notices(realdb)
    assert first

    await _seed_counted(realdb, org_id, 20)  # → 100
    async with realdb.sessionmaker("a")() as s:
        assert await send_due_ai_usage_notices(s, organization_id=org_id) == ["100"]
    await drain_post_commit()
    after = await _ai_notices(realdb)
    assert len(after) == 2 * len(first)
    assert any("paused" in n.body for n in after), "Free's 100% notice says reading pauses"


async def test_a_notice_prices_in_the_plans_own_currency(realdb):
    """A negotiated non-USD plan must not read "USD" in its overage notice."""
    from app.models.billing import Plan

    org_id = realdb.info("a").org_id
    await _subscribe(realdb, org_id, "growth")
    async with realdb.control_sessionmaker()() as s:
        plan = (await s.execute(select(Plan).where(Plan.code == "growth"))).scalar_one()
        plan.currency = "EUR"
        await s.commit()
    try:
        await _seed_counted(realdb, org_id, 500)  # Growth: 100%
        async with realdb.sessionmaker("a")() as s:
            assert await send_due_ai_usage_notices(s, organization_id=org_id) == ["80", "100"]
        bodies = [n.body for n in await _ai_notices(realdb)]
        assert bodies and all("EUR 0.10" in b and "USD" not in b for b in bodies)
    finally:
        async with realdb.control_sessionmaker()() as s:
            plan = (await s.execute(select(Plan).where(Plan.code == "growth"))).scalar_one()
            plan.currency = "USD"
            await s.commit()


async def test_a_notice_that_reached_nobody_is_not_marked_sent(realdb):
    org_id = realdb.info("a").org_id
    await _seed_counted(realdb, org_id, 80)
    with patch(
        "app.services.notification_dispatch.resolve_role_user_ids", AsyncMock(return_value=[])
    ):
        async with realdb.sessionmaker("a")() as s:
            assert await send_due_ai_usage_notices(s, organization_id=org_id) == []
    billing = await _billing_settings(realdb, org_id)
    assert not (billing.get(NOTICES_KEY) or {}).get(period_of())


# ---------------------------------------------------------------------------
# The billing API
# ---------------------------------------------------------------------------


async def test_subscription_endpoint_reports_ai_usage_and_the_pause(realdb):
    org_id = realdb.info("a").org_id
    await _seed_counted(realdb, org_id, 100)
    async with realdb.client(key="a", role="admin") as c:
        body = (await c.get("/api/billing/subscription")).json()
    ai = body["ai_usage"]
    assert ai["used"] == 100 and ai["included"] == 100
    assert ai["overage_unit_price"] is None
    assert ai["paused"] is True and ai["pause_reason"] == "allowance_reached"
    assert body["usage"]["ai_invoices"] == "100"


async def test_spending_cap_endpoint_sets_audits_and_clears(realdb, _audit_engine_on_loop):
    org_id = realdb.info("a").org_id
    await _subscribe(realdb, org_id, "growth")
    await _seed_counted(realdb, org_id, 530)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put("/api/billing/spending-cap", json={"monthly_spend_cap": "2.5"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["monthly_spend_cap"] == "2.50"
        ai = body["ai_usage"]
        # 30 over at $0.10 = $3.00, clamped to the $2.50 cap (25 units).
        assert ai["overage_units"] == 25 and ai["overage_amount"] == "2.50"
        assert ai["paused"] is True and ai["pause_reason"] == "spend_cap_reached"

        cleared = await c.put("/api/billing/spending-cap", json={"monthly_spend_cap": None})
        assert cleared.json()["monthly_spend_cap"] is None
        assert cleared.json()["ai_usage"]["overage_amount"] == "3.00"

    assert "monthly_spend_cap" not in await _billing_settings(realdb, org_id)
    async with realdb.sessionmaker("a")() as s:
        rows = (
            await s.execute(
                select(AuditLog.details).where(AuditLog.action == "billing.spending_cap_updated")
            )
        ).all()
    details = [r[0] for r in rows]
    assert {"previous_cap": None, "new_cap": "2.50"} in details
    assert {"previous_cap": "2.50", "new_cap": None} in details


@pytest.mark.parametrize("role", ["cfo", "ap_manager", "ap_clerk"])
async def test_only_an_admin_sets_the_spending_cap(realdb, role):
    async with realdb.client(key="a", role=role) as c:
        resp = await c.put("/api/billing/spending-cap", json={"monthly_spend_cap": "5.00"})
    assert resp.status_code == 403


@pytest.mark.parametrize("bad", [5.0, "-1", "1.005", "1000000.01", "abc"])
async def test_spending_cap_refuses_inexact_or_out_of_range_values(realdb, bad):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put("/api/billing/spending-cap", json={"monthly_spend_cap": bad})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# The `pending → new` edge is the worker's, not a human's
# ---------------------------------------------------------------------------


async def test_bulk_status_refuses_to_send_a_mid_read_invoice_back_to_draft(realdb):
    org_id = realdb.info("a").org_id
    inv_id = await _pending_invoice(realdb, org_id)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/invoices/bulk/status", json={"ids": [str(inv_id)], "status": "new"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 0
    assert body["skipped"][0]["id"] == str(inv_id)


# ---------------------------------------------------------------------------
# The local worker must not hold its one control connection across the read
# ---------------------------------------------------------------------------


async def test_local_worker_frees_its_control_connection_before_extracting(realdb, monkeypatch):
    """`_run_local`'s control engine is a ONE-connection pool. Holding it for
    the whole extraction made every control-plane session underneath — the
    allowance gate, the notification + audit hooks — wait out the pool timeout
    and fail. The fake extraction opens one, the way the gate does."""
    from app.config import settings
    from app.database import control_session_factory
    from app.services import extraction_dispatch

    org_id = realdb.info("a").org_id
    inv_id = await _pending_invoice(realdb, org_id)
    monkeypatch.setattr(settings, "database_url", realdb.control_db_url())
    reached: list[int] = []

    async def _fake_run_extraction(db, invoice, **_kwargs):
        async with control_session_factory() as ctrl:
            reached.append((await ctrl.execute(text("select 1"))).scalar_one())

    with patch("app.services.extraction.run_extraction", _fake_run_extraction):
        await asyncio.wait_for(extraction_dispatch._run_local(inv_id, org_id, None), timeout=20)
    assert reached == [1]


@pytest.fixture(autouse=True)
async def _clean_usage(realdb):
    yield
    org_id = realdb.info("a").org_id
    async with realdb.sessionmaker("a")() as s:
        await s.execute(delete(ExtractionUsage).where(ExtractionUsage.organization_id == org_id))
        await s.commit()
