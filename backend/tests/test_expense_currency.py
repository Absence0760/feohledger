"""Issue #157 — expense reports must never sum mixed currencies.

Before this fix a $100.00 USD expense plus a €200.00 EUR expense attached to a
``currency="USD"`` report reported ``total_amount: 300.00, currency: "USD"``:
no conversion, no per-currency breakdown, no rejection — and that fabricated
300.00 fed the CFO-threshold gate.

The fix converts each line at a rate LOCKED onto the row (mirroring
``invoices.reporting_*`` / ``payments.fx_rate``) rather than rejecting a
mixed-currency report — an employee on one trip legitimately spends in several
currencies. Two layers, both covered here:

  line  → report currency   (``expenses.converted_*``)
  report→ org reporting ccy (``expense_reports.reporting_*``) — what the CFO
                             threshold, a bare number in the reporting currency,
                             is actually compared against.

Everything runs against the deterministic ``mock`` FX adapter (USD→EUR 0.92, so
EUR→USD = 1/0.92 = 1.086957), so the numbers below are hand-checkable and the
suite needs no cloud account.

Pure-unit coverage of the rollup/gate primitives sits alongside the realdb
end-to-end cases; the realdb idioms mirror ``tests/test_expense_approval.py``.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.expense import Expense
from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services.expense_currency import (
    ExpenseConversionError,
    lock_expense_conversion,
    report_amount_for_gate,
    rollup_report_lines,
)
from app.services.fx_adapters import get_fx_adapter

# €200 EUR at the mock EUR→USD rate (1 / 0.92, quantized to 6 dp = 1.086957).
EUR_200_IN_USD = Decimal("217.39")


class _Row:
    """Minimal duck-typed stand-in for an ORM Expense / ExpenseReport."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# Pure unit — the rollup + gate primitives
# ---------------------------------------------------------------------------


def test_rollup_excludes_unconverted_foreign_line_instead_of_summing_face_value():
    """The exact reported failure, at the primitive level: a EUR line with no
    lock must NOT contribute its face 200 to a USD total."""
    rollup = rollup_report_lines(
        [
            {"id": "a", "amount": Decimal("100.00"), "currency": "USD"},
            {"id": "b", "amount": Decimal("200.00"), "currency": "EUR"},
        ],
        report_currency="USD",
    )
    assert rollup.total == Decimal("100.00")  # NOT 300.00
    assert rollup.unconverted_count == 1
    assert rollup.unconverted_ids == ("b",)
    assert rollup.currency == "USD"


def test_rollup_counts_an_id_less_unconverted_row():
    """`unconverted_count` is the COUNT, not `len(unconverted_ids)`.

    The ids list only collects rows that carried one, so deriving the count
    from it undercounts an id-less row — and this count is what gates report
    submission, so it must never undercount. The line is still excluded from
    the total either way; the bug was that the gate stopped seeing it.
    """
    rollup = rollup_report_lines(
        [
            {"id": "a", "amount": Decimal("100.00"), "currency": "USD"},
            {"amount": Decimal("200.00"), "currency": "EUR"},  # no id
        ],
        report_currency="USD",
    )
    assert rollup.total == Decimal("100.00")
    assert rollup.unconverted_count == 1
    assert rollup.unconverted_ids == ()
    # The per-currency bucket already counted it correctly — the two must agree.
    eur = next(b for b in rollup.by_currency if b.currency == "EUR")
    assert eur.unconverted_count == 1


def test_rollup_uses_the_locked_figure_not_the_face_amount():
    rollup = rollup_report_lines(
        [
            {"id": "a", "amount": Decimal("100.00"), "currency": "USD"},
            {
                "id": "b",
                "amount": Decimal("200.00"),
                "currency": "EUR",
                "converted_amount": EUR_200_IN_USD,
                "converted_currency": "USD",
            },
        ],
        report_currency="USD",
    )
    assert rollup.total == Decimal("100.00") + EUR_200_IN_USD == Decimal("317.39")
    assert rollup.unconverted_count == 0
    by_cur = {b.currency: b for b in rollup.by_currency}
    assert by_cur["EUR"].original_amount == Decimal("200.00")
    assert by_cur["EUR"].report_amount == EUR_200_IN_USD


def test_rollup_ignores_a_lock_into_a_stale_currency():
    """A lock into GBP is meaningless once the report is USD — the line is
    unconverted, never counted at its GBP figure."""
    rollup = rollup_report_lines(
        [
            {
                "id": "b",
                "amount": Decimal("200.00"),
                "currency": "EUR",
                "converted_amount": Decimal("171.74"),
                "converted_currency": "GBP",
            }
        ],
        report_currency="USD",
    )
    assert rollup.total == Decimal("0.00")
    assert rollup.unconverted_count == 1


def test_gate_amount_falls_back_to_total_only_for_a_same_currency_report():
    same = _Row(currency="USD", total_amount=Decimal("4900.00"), reporting_amount=None)
    assert report_amount_for_gate(same, reporting_currency="USD") == Decimal("4900.00")


def test_gate_amount_is_none_for_a_foreign_report_with_no_lock():
    """Fail-closed signal: a EUR report with no USD lock must not be compared
    against a USD threshold at face value."""
    foreign = _Row(
        currency="EUR",
        total_amount=Decimal("4900.00"),
        reporting_amount=None,
        reporting_currency=None,
    )
    assert report_amount_for_gate(foreign, reporting_currency="USD") is None


def test_gate_amount_prefers_the_locked_reporting_figure():
    locked = _Row(
        currency="EUR",
        total_amount=Decimal("4900.00"),
        reporting_amount=Decimal("5326.09"),
        reporting_currency="USD",
    )
    assert report_amount_for_gate(locked, reporting_currency="USD") == Decimal("5326.09")


async def test_lock_expense_conversion_is_exact_decimal_never_float():
    expense = _Row(amount=Decimal("200.00"), currency="EUR")
    await lock_expense_conversion(
        expense, target_currency="USD", fx_adapter=get_fx_adapter({"provider": "mock"})
    )
    assert isinstance(expense.converted_amount, Decimal)
    assert expense.converted_amount == EUR_200_IN_USD
    assert expense.converted_currency == "USD"
    assert expense.converted_fx_rate == Decimal("1.08695700")
    assert expense.converted_fx_locked_at is not None


async def test_lock_expense_conversion_raises_on_an_unknown_currency():
    expense = _Row(amount=Decimal("10.00"), currency="XYZ")
    with pytest.raises(ExpenseConversionError) as exc:
        await lock_expense_conversion(
            expense, target_currency="USD", fx_adapter=get_fx_adapter({"provider": "mock"})
        )
    # PII-free: currency codes only.
    assert "XYZ" in str(exc.value) and "USD" in str(exc.value)


# ---------------------------------------------------------------------------
# realdb — the reproduction from the issue, end to end
# ---------------------------------------------------------------------------


async def _mk_expense(c, amount, currency="USD", **kw):
    body = {"expense_date": "2026-06-01", "amount": amount, "currency": currency}
    body.update(kw)
    resp = await c.post("/api/expenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _mk_report(c, currency="USD"):
    resp = await c.post(
        "/api/expense-reports",
        json={"report_number": f"R-{uuid.uuid4().hex[:8]}", "currency": currency},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_usd_and_eur_lines_do_not_sum_to_300(realdb):
    """The issue's exact reproduction: $100.00 USD + €200.00 EUR on a USD
    report must NOT report 300.00."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        usd = await _mk_expense(c, "100.00", "USD")
        eur = await _mk_expense(c, "200.00", "EUR")
        rid = await _mk_report(c, "USD")
        attached = await c.post(
            f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [usd, eur]}
        )
        assert attached.status_code == 200, attached.text
        body = attached.json()

    assert body["currency"] == "USD"
    assert body["total_amount"] != 300.0, "mixed currencies were summed at face value"
    assert body["total_amount_exact"] == "317.39"

    # The EUR line carries its own locked evidence; the USD line locks 1:1.
    lines = {e["currency"]: e for e in body["expenses"]}
    assert lines["EUR"]["converted_currency"] == "USD"
    assert lines["EUR"]["converted_amount"] == "217.39"
    assert lines["EUR"]["converted_fx_rate"] == "1.08695700"
    assert lines["EUR"]["converted_fx_locked_at"]
    assert lines["USD"]["converted_amount"] == "100.00"
    assert lines["USD"]["converted_fx_rate"] == "1.00000000"


async def test_summary_reports_the_converted_total_and_a_currency_breakdown(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        usd = await _mk_expense(c, "100.00", "USD", category="travel")
        eur = await _mk_expense(c, "200.00", "EUR", category="travel")
        rid = await _mk_report(c, "USD")
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [usd, eur]})
        summary = (await c.get(f"/api/expense-reports/{rid}/summary")).json()

    assert summary["currency"] == "USD"
    assert summary["total"] != 300.0
    assert summary["total_exact"] == "317.39"
    assert summary["count"] == 2
    assert summary["unconverted_count"] == 0
    by_cur = {b["currency"]: b for b in summary["by_currency"]}
    assert by_cur["EUR"]["original_amount"] == "200.00"
    assert by_cur["EUR"]["report_amount"] == "217.39"
    assert by_cur["USD"]["report_amount"] == "100.00"
    # The per-category rollup is in report currency too, not a naive sum.
    travel = next(b for b in summary["by_category"] if b["category"] == "travel")
    assert travel["total_exact"] == "317.39"


async def test_locked_rate_does_not_drift_when_the_market_moves(realdb):
    """A report's total must be reproducible: re-reading it never re-fetches a
    rate, even after the org's FX config is re-pointed at a different rate."""
    org_id = realdb.info("a").org_id
    ctrl = realdb.control_sessionmaker()
    async with realdb.client(key="a", role="ap_clerk") as c:
        eur = await _mk_expense(c, "200.00", "EUR")
        rid = await _mk_report(c, "USD")
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eur]})

    async with ctrl() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        cfg = dict(org.settings or {})
        cfg["fx"] = {"provider": "mock", "mock_rates": {"EUR": "0.5"}}
        org.settings = cfg
        flag_modified(org, "settings")
        await s.commit()
    try:
        async with realdb.client(key="a", role="ap_clerk") as c:
            after = (await c.get(f"/api/expense-reports/{rid}")).json()
        # A EUR→USD move from 1.0870 to 2.0 would have taken 217.39 → 400.00.
        assert after["total_amount_exact"] == "217.39"
        assert after["expenses"][0]["converted_fx_rate"] == "1.08695700"
    finally:
        async with ctrl() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            cfg = dict(org.settings or {})
            cfg.pop("fx", None)
            org.settings = cfg
            flag_modified(org, "settings")
            await s.commit()


async def test_unconvertible_line_is_refused_rather_than_attached(realdb):
    """An unknown currency can't be expressed in the report currency; the attach
    is refused (422) instead of quietly landing at face value."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        # Bypass the API to plant a line the FX provider has no rate for.
        bad = await _mk_expense(c, "50.00", "USD")
        rid = await _mk_report(c, "USD")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        row = (await s.execute(select(Expense).where(Expense.id == uuid.UUID(bad)))).scalar_one()
        row.currency = "XYZ"
        await s.commit()

    async with realdb.client(key="a", role="ap_clerk") as c:
        refused = await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [bad]})
    assert refused.status_code == 422, refused.text
    assert "XYZ" in refused.json()["detail"]

    async with mk() as s:
        still = (
            await s.execute(select(Expense.report_id).where(Expense.id == uuid.UUID(bad)))
        ).scalar_one()
    assert still is None, "the unconvertible line must not have been attached"


async def test_submit_blocks_a_legacy_unconverted_line(realdb):
    """A row predating the locked-FX columns (foreign currency, no lock) must
    block submission rather than submit an understated total."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        eur = await _mk_expense(c, "200.00", "EUR")
        rid = await _mk_report(c, "USD")
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eur]})
    mk = realdb.sessionmaker("a")
    async with mk() as s:  # simulate the pre-migration state
        row = (await s.execute(select(Expense).where(Expense.id == uuid.UUID(eur)))).scalar_one()
        row.converted_amount = None
        row.converted_currency = None
        row.converted_fx_rate = None
        row.converted_fx_locked_at = None
        await s.commit()

    async with realdb.client(key="a", role="ap_clerk") as c:
        blocked = await c.post(f"/api/expense-reports/{rid}/submit")
    assert blocked.status_code == 422, blocked.text
    assert eur in blocked.json()["detail"]["expense_ids"]


async def test_changing_the_report_currency_relocks_every_line(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        eur = await _mk_expense(c, "200.00", "EUR")
        rid = await _mk_report(c, "USD")
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eur]})
        moved = await c.patch(f"/api/expense-reports/{rid}", json={"currency": "EUR"})
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["currency"] == "EUR"
    # Now the report is in the line's own currency — 1:1, exactly 200.00.
    assert body["total_amount_exact"] == "200.00"
    assert body["expenses"][0]["converted_currency"] == "EUR"


async def test_detach_clears_the_lock(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        eur = await _mk_expense(c, "200.00", "EUR")
        rid = await _mk_report(c, "USD")
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eur]})
        after = await c.post(
            f"/api/expense-reports/{rid}/expenses",
            json={"expense_ids": [eur], "detach": True},
        )
        assert after.status_code == 200
        assert after.json()["total_amount_exact"] == "0.00"
        line = (await c.get(f"/api/expenses/{eur}")).json()
    assert line["converted_amount"] is None
    assert line["converted_currency"] is None


async def test_editing_a_line_currency_relocks_it(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        line = await _mk_expense(c, "200.00", "USD")
        rid = await _mk_report(c, "USD")
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [line]})
        patched = await c.patch(f"/api/expenses/{line}", json={"currency": "EUR"})
        assert patched.status_code == 200, patched.text
        report = (await c.get(f"/api/expense-reports/{rid}")).json()
    # 200 EUR is worth MORE than 200 USD — the total must move, not stay 200.
    assert report["total_amount_exact"] == "217.39"
    assert patched.json()["converted_amount"] == "217.39"


# ---------------------------------------------------------------------------
# The CFO gate cannot be dodged with currencies
# ---------------------------------------------------------------------------


async def _set_org_settings(realdb, **patch):
    org_id = realdb.info("a").org_id
    ctrl = realdb.control_sessionmaker()
    async with ctrl() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        cfg = dict(org.settings or {})
        cfg.update(patch)
        org.settings = cfg
        flag_modified(org, "settings")
        await s.commit()


async def _clear_org_settings(realdb, *keys):
    org_id = realdb.info("a").org_id
    ctrl = realdb.control_sessionmaker()
    async with ctrl() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        cfg = dict(org.settings or {})
        for k in keys:
            cfg.pop(k, None)
        org.settings = cfg
        flag_modified(org, "settings")
        await s.commit()


async def test_cfo_gate_cannot_be_dodged_by_splitting_across_currencies(realdb):
    """Two 3 000-unit lines in USD and EUR are each under the 5 000 threshold,
    and their naive sum (6 000) would also have been over it — but the pre-fix
    bug is the *other* direction too: what matters is that the gate sees the
    real converted total (3 000 + 3 260.87 = 6 260.87 USD) and holds.

    The dodge this closes: file the foreign leg so the total *looks* small.
    Here EUR 3 000 is worth USD 3 260.87, and a manager must not be able to
    approve on the strength of an under-converted figure."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        usd = await _mk_expense(c, "3000.00", "USD")
        eur = await _mk_expense(c, "3000.00", "EUR")
        rid = await _mk_report(c, "USD")
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [usd, eur]})
        submitted = await c.post(f"/api/expense-reports/{rid}/submit")
        assert submitted.status_code == 200, submitted.text
    assert submitted.json()["total_amount_exact"] == "6260.87"

    async with realdb.client(key="a", role="ap_manager") as c:
        denied = await c.post(f"/api/expense-reports/{rid}/approve")
    assert denied.status_code == 403
    assert "cfo" in denied.json()["detail"]["message"].lower()
    assert denied.json()["detail"]["code"] == "expense_cfo_required"

    async with realdb.client(key="a", role="cfo") as c:
        ok = await c.post(f"/api/expense-reports/{rid}/approve")
    assert ok.status_code == 200, ok.text


async def test_cfo_gate_uses_the_reporting_currency_not_the_report_currency(realdb):
    """A EUR 4 900 report is under a bare `5000` threshold at face value but is
    USD 5 326.09 — over it. The gate must convert before comparing, otherwise
    filing in a weaker currency dodges CFO review."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        eur = await _mk_expense(c, "4900.00", "EUR")
        rid = await _mk_report(c, "EUR")
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eur]})
        submitted = await c.post(f"/api/expense-reports/{rid}/submit")
        assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["total_amount_exact"] == "4900.00"  # in the report's own currency
    assert body["reporting_currency"] == "USD"
    assert body["reporting_amount"] == "5326.09"  # what the threshold sees

    async with realdb.client(key="a", role="ap_manager") as c:
        denied = await c.post(f"/api/expense-reports/{rid}/approve")
    assert denied.status_code == 403, denied.text
    assert "cfo" in denied.json()["detail"]["message"].lower()
    assert denied.json()["detail"]["code"] == "expense_cfo_required"
    # Every figure the sentence names, as an exact string beside its currency.
    assert denied.json()["detail"]["params"] == {
        "amount": "5326.09",
        "currency": "USD",
        "limit": "5000",
        "report_currency": "EUR",
    }

    async with realdb.client(key="a", role="cfo") as c:
        ok = await c.post(f"/api/expense-reports/{rid}/approve")
    assert ok.status_code == 200, ok.text

    # The decision records the exact figure it compared (SOX replayability).
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        row = (
            await s.execute(
                select(AuditLog.details).where(
                    AuditLog.action == "expense_report.approved",
                    AuditLog.entity_id == uuid.UUID(rid),
                )
            )
        ).scalar_one()
    assert row["gate_total"] == "5326.09"
    assert row["gate_currency"] == "USD"


async def test_gate_fails_closed_when_the_reporting_figure_is_unavailable(realdb):
    """No usable conversion → the gate escalates to CFO rather than comparing a
    foreign-currency number against a threshold in another currency."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        eur = await _mk_expense(c, "10.00", "EUR")
        rid = await _mk_report(c, "EUR")
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eur]})
        await c.post(f"/api/expense-reports/{rid}/submit")

    # Wipe the report-level lock to simulate an FX outage at submit time.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        from app.models.expense import ExpenseReport

        row = (
            await s.execute(select(ExpenseReport).where(ExpenseReport.id == uuid.UUID(rid)))
        ).scalar_one()
        row.reporting_amount = None
        row.reporting_currency = None
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        denied = await c.post(f"/api/expense-reports/{rid}/approve")
    # A 10 EUR report is nowhere near 5000 — it is held purely because the
    # figure could not be established. Fail closed.
    assert denied.status_code == 403, denied.text
    assert "cfo" in denied.json()["detail"]["message"].lower()
    assert denied.json()["detail"]["code"] == "expense_cfo_required"


async def test_same_currency_report_still_compares_at_face_value(realdb):
    """No regression for the single-currency majority: a USD report under a USD
    threshold is still approvable by a manager."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        usd = await _mk_expense(c, "100.00", "USD")
        rid = await _mk_report(c, "USD")
        await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [usd]})
        await c.post(f"/api/expense-reports/{rid}/submit")
    async with realdb.client(key="a", role="ap_manager") as c:
        ok = await c.post(f"/api/expense-reports/{rid}/approve")
    assert ok.status_code == 200, ok.text


async def test_reporting_currency_setting_drives_the_gate(realdb):
    """An org that reports in EUR compares against a EUR threshold: the same
    EUR 4 900 report is now UNDER 5 000 and a manager may approve it."""
    await _set_org_settings(realdb, reporting_currency="EUR")
    try:
        async with realdb.client(key="a", role="ap_clerk") as c:
            eur = await _mk_expense(c, "4900.00", "EUR")
            rid = await _mk_report(c, "EUR")
            await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eur]})
            submitted = await c.post(f"/api/expense-reports/{rid}/submit")
        assert submitted.json()["reporting_amount"] == "4900.00"
        async with realdb.client(key="a", role="ap_manager") as c:
            ok = await c.post(f"/api/expense-reports/{rid}/approve")
        assert ok.status_code == 200, ok.text
    finally:
        await _clear_org_settings(realdb, "reporting_currency")


# ---------------------------------------------------------------------------
# Pre-approval cover check is currency-aware
# ---------------------------------------------------------------------------


async def test_foreign_currency_preapproval_does_not_cover_an_expense(realdb):
    """A €500 EUR pre-approval must not silently satisfy the pre-approval
    requirement for a $500 USD expense (issue #157, fourth site)."""
    async with realdb.client(key="a", role="ap_manager") as c:
        await c.post(
            "/api/expense-policies",
            json={
                "name": "PreapprovalReq",
                "category": "software",
                "requires_preapproval_above": "100.00",
            },
        )
    # Raised by the clerk, decided by the manager — `check_segregation` blocks a
    # user deciding their own request.
    async with realdb.client(key="a", role="ap_clerk") as c:
        pre = await c.post(
            "/api/expense-preapprovals",
            json={
                "title": "Tooling",
                "estimated_amount": "500.00",
                "currency": "EUR",
                "category": "software",
            },
        )
        assert pre.status_code == 201, pre.text
    async with realdb.client(key="a", role="ap_manager") as c:
        approved = await c.post(f"/api/expense-preapprovals/{pre.json()['id']}/approve")
        assert approved.status_code == 200, approved.text

    async with realdb.client(key="a", role="ap_clerk") as c:
        usd = await _mk_expense(c, "500.00", "USD", category="software")
        line = (await c.get(f"/api/expenses/{usd}")).json()
    codes = {v["code"] for v in (line["policy_violations"] or [])}
    assert "preapproval_required" in codes, "a EUR pre-approval covered a USD expense"


async def test_same_currency_preapproval_still_covers(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        await c.post(
            "/api/expense-policies",
            json={
                "name": "PreapprovalReq2",
                "category": "hardware",
                "requires_preapproval_above": "100.00",
            },
        )
    async with realdb.client(key="a", role="ap_clerk") as c:
        pre = await c.post(
            "/api/expense-preapprovals",
            json={
                "title": "Laptop",
                "estimated_amount": "500.00",
                "currency": "USD",
                "category": "hardware",
            },
        )
        assert pre.status_code == 201, pre.text
    async with realdb.client(key="a", role="ap_manager") as c:
        approved = await c.post(f"/api/expense-preapprovals/{pre.json()['id']}/approve")
        assert approved.status_code == 200, approved.text

    # Cover belongs to the requester, so the line has to be on the requester's
    # own report — an unattached line has no owner yet and nothing covers it
    # (backend/docs/expense-management.md § Pre-approval cover is the
    # requester's own).
    async with realdb.client(key="a", role="ap_clerk") as c:
        usd = await _mk_expense(c, "500.00", "USD", category="hardware")
        rid = await _mk_report(c, "USD")
        attached = await c.post(f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [usd]})
        assert attached.status_code == 200, attached.text
        line = (await c.get(f"/api/expenses/{usd}")).json()
    codes = {v["code"] for v in (line["policy_violations"] or [])}
    assert "preapproval_required" not in codes


async def test_same_currency_line_attaches_without_a_usable_fx_provider(realdb):
    """A single-currency report must not need an FX provider.

    ``_lock_line_conversion`` demanded an adapter BEFORE asking whether a
    conversion was even needed, so one bad ``settings.fx.provider`` value —
    which ``get_fx_adapter`` refuses rather than silently resolving to ``mock``
    (`decisions §29`) — 422'd every attach in the tenant, including an entirely
    domestic USD line on a USD report. That line has no FX question to fail
    closed ON: ``lock_expense_conversion`` locks it at rate 1 with no adapter
    call at all.

    The cross-currency line, which genuinely could understate the total the CFO
    gate reads, is still refused.
    """
    org_id = realdb.info("a").org_id
    ctrl = realdb.control_sessionmaker()
    async with ctrl() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        cfg = dict(org.settings or {})
        cfg["fx"] = {"provider": "definitely-not-a-registered-provider"}
        org.settings = cfg
        flag_modified(org, "settings")
        await s.commit()
    try:
        async with realdb.client(key="a", role="ap_clerk") as c:
            rid = await _mk_report(c, "USD")
            usd = await _mk_expense(c, "40.00", "USD")
            eur = await _mk_expense(c, "40.00", "EUR")

            attached = await c.post(
                f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [usd]}
            )
            assert attached.status_code == 200, attached.text
            assert attached.json()["total_amount_exact"] == "40.00"
            line = next(e for e in attached.json()["expenses"] if e["id"] == usd)
            assert line["converted_currency"] == "USD"
            assert line["converted_fx_rate"] == "1.00000000"

            # The foreign line still fails closed.
            refused = await c.post(
                f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [eur]}
            )
            assert refused.status_code == 422, refused.text
    finally:
        async with ctrl() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            cfg = dict(org.settings or {})
            cfg.pop("fx", None)
            org.settings = cfg
            flag_modified(org, "settings")
            await s.commit()
