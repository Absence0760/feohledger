"""A same-currency expense report must record its reporting-currency total even
when the tenant has no usable FX provider.

Round 15 taught the LINE-level lock (``api/expenses._lock_line_conversion``) to
ask which currency PAIR is being converted before demanding an FX adapter, so a
same-currency line locks at rate 1 for a tenant whose ``settings.fx.provider``
names no registered adapter (``get_fx_adapter`` refuses such a name rather than
resolving it to ``mock`` — `decisions §29`). The REPORT-level lock kept the
old shape: adapter first, pair never asked. So the same tenant submitted a
report already denominated in the org's reporting currency with
``expense_reports.reporting_*`` all NULL and ``"reporting_total": null`` in its
own ``expense_report.submitted`` audit row.

This is a completeness gap in the stored snapshot, **not** a control failure:
``expense_currency.report_amount_for_gate`` falls back to ``total_amount`` when
the report currency already equals the reporting currency, so the CFO gate
evaluated the right figure either way. The gate assertions below pin that it
stays right — in both directions — while the recorded snapshot stops being
empty.

The fix shares one pair-first predicate
(``expense_currency.conversion_requires_rate_source``) between the two levels so
they cannot drift again.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.expense import ExpenseReport
from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services.expense_currency import conversion_requires_rate_source

_UNREGISTERED_FX = "definitely-not-a-registered-provider"


# ---------------------------------------------------------------------------
# The shared predicate itself (pure)
# ---------------------------------------------------------------------------


def test_pair_is_checked_before_the_provider():
    # No adapter + same pair → no rate is needed, so nothing is required.
    assert not conversion_requires_rate_source(
        source_currency="USD", target_currency="USD", fx_adapter=None
    )
    # Case / blank normalisation rides the same helper the locks use.
    assert not conversion_requires_rate_source(
        source_currency="usd", target_currency="USD", fx_adapter=None
    )
    assert not conversion_requires_rate_source(
        source_currency=None, target_currency="EUR", fx_adapter=None
    )
    # No adapter + a genuinely different pair → fail closed.
    assert conversion_requires_rate_source(
        source_currency="EUR", target_currency="USD", fx_adapter=None
    )
    # An adapter is present → never blocked, whatever the pair.
    assert not conversion_requires_rate_source(
        source_currency="EUR", target_currency="USD", fx_adapter=object()
    )


# ---------------------------------------------------------------------------
# Real-DB end-to-end
# ---------------------------------------------------------------------------


async def _set_org_settings(realdb, **entries):
    """Merge ``entries`` into the tenant org's settings; returns a restorer."""
    org_id = realdb.info("a").org_id
    ctrl = realdb.control_sessionmaker()
    async with ctrl() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        cfg = dict(org.settings or {})
        cfg.update(entries)
        org.settings = cfg
        flag_modified(org, "settings")
        await s.commit()

    async def restore():
        async with ctrl() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            cfg = dict(org.settings or {})
            for key in entries:
                cfg.pop(key, None)
            org.settings = cfg
            flag_modified(org, "settings")
            await s.commit()

    return restore


async def _mk_expense(c, amount, currency="USD"):
    resp = await c.post(
        "/api/expenses",
        json={"expense_date": "2026-06-01", "amount": amount, "currency": currency},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _mk_submitted_report(realdb, *, report_currency, amount):
    """Create a report in ``report_currency`` holding one line of the same
    currency, and submit it. Returns the report id."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        created = await c.post(
            "/api/expense-reports",
            json={"report_number": f"R-{uuid.uuid4().hex[:8]}", "currency": report_currency},
        )
        assert created.status_code == 201, created.text
        rid = created.json()["id"]
        eid = await _mk_expense(c, amount, report_currency)
        attached = await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eid]})
        assert attached.status_code == 200, attached.text
        submitted = await c.post(f"/api/expense-reports/{rid}/submit")
        assert submitted.status_code == 200, submitted.text
    return rid


async def _load_report(realdb, rid: str) -> ExpenseReport:
    async with realdb.sessionmaker("a")() as s:
        return (
            await s.execute(select(ExpenseReport).where(ExpenseReport.id == uuid.UUID(rid)))
        ).scalar_one()


async def _submitted_audit_details(realdb, rid: str) -> dict:
    async with realdb.sessionmaker("a")() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog.details).where(
                        AuditLog.action == "expense_report.submitted",
                        AuditLog.entity_id == uuid.UUID(rid),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    return rows[0]


async def test_same_currency_report_locks_its_reporting_total_without_an_fx_provider(realdb):
    """The gap: a USD report on a USD-reporting tenant needs no rate at all, and
    must not be left with an empty snapshot because the provider name is bad.

    Pre-fix ``reporting_amount`` was NULL and the audit row carried
    ``"reporting_total": null``."""
    restore = await _set_org_settings(
        realdb, fx={"provider": _UNREGISTERED_FX}, reporting_currency="USD"
    )
    try:
        rid = await _mk_submitted_report(realdb, report_currency="USD", amount="120.00")

        report = await _load_report(realdb, rid)
        assert report.total_amount == Decimal("120.00")
        # Pre-fix: all four NULL.
        assert report.reporting_currency == "USD"
        assert report.reporting_amount == Decimal("120.00")
        assert report.reporting_fx_rate == Decimal("1.00000000")
        assert report.reporting_fx_locked_at is not None

        details = await _submitted_audit_details(realdb, rid)
        assert details["reporting_total"] == "120.00"  # pre-fix: None
        assert details["reporting_currency"] == "USD"
    finally:
        await restore()


async def test_cross_currency_report_still_refuses_to_lock_without_an_fx_provider(realdb):
    """The fail-closed direction is unchanged: an EUR report on a USD-reporting
    tenant genuinely needs a rate, so with no usable provider the figure stays
    NULL rather than being invented."""
    restore = await _set_org_settings(
        realdb, fx={"provider": _UNREGISTERED_FX}, reporting_currency="USD"
    )
    try:
        rid = await _mk_submitted_report(realdb, report_currency="EUR", amount="120.00")

        report = await _load_report(realdb, rid)
        assert report.total_amount == Decimal("120.00")
        assert report.currency == "EUR"
        assert report.reporting_currency is None
        assert report.reporting_amount is None
        assert report.reporting_fx_rate is None

        details = await _submitted_audit_details(realdb, rid)
        assert details["reporting_total"] is None
    finally:
        await restore()


async def test_cfo_gate_still_evaluates_the_same_currency_report_correctly(realdb):
    """The recorded snapshot changed; the gate's verdict must not.

    Under the threshold an ap_manager may approve; over it, only CFO/admin —
    both read from the now-populated reporting figure."""
    restore = await _set_org_settings(
        realdb,
        fx={"provider": _UNREGISTERED_FX},
        reporting_currency="USD",
        expense_approval={"cfo_threshold": "500"},
    )
    try:
        under = await _mk_submitted_report(realdb, report_currency="USD", amount="120.00")
        async with realdb.client(key="a", role="ap_manager") as c:
            ok = await c.post(f"/api/expense-reports/{under}/approve")
        assert ok.status_code == 200, ok.text

        over = await _mk_submitted_report(realdb, report_currency="USD", amount="900.00")
        async with realdb.client(key="a", role="ap_manager") as c:
            denied = await c.post(f"/api/expense-reports/{over}/approve")
        assert denied.status_code == 403, denied.text
        assert "cfo" in denied.json()["detail"]["message"].lower()
        assert denied.json()["detail"]["code"] == "expense_cfo_required"
        async with realdb.client(key="a", role="cfo") as c:
            escalated = await c.post(f"/api/expense-reports/{over}/approve")
        assert escalated.status_code == 200, escalated.text
    finally:
        await restore()


async def test_cfo_gate_fails_closed_on_the_cross_currency_report(realdb):
    """No reporting figure means the gate cannot compare, so it escalates — even
    for a total far under the threshold."""
    restore = await _set_org_settings(
        realdb,
        fx={"provider": _UNREGISTERED_FX},
        reporting_currency="USD",
        expense_approval={"cfo_threshold": "500"},
    )
    try:
        rid = await _mk_submitted_report(realdb, report_currency="EUR", amount="10.00")
        async with realdb.client(key="a", role="ap_manager") as c:
            denied = await c.post(f"/api/expense-reports/{rid}/approve")
        assert denied.status_code == 403, denied.text
        assert "cfo" in denied.json()["detail"]["message"].lower()
        assert denied.json()["detail"]["code"] == "expense_cfo_required"
        async with realdb.client(key="a", role="cfo") as c:
            ok = await c.post(f"/api/expense-reports/{rid}/approve")
        assert ok.status_code == 200, ok.text
    finally:
        await restore()
