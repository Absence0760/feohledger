"""Cash-flow forecasting API tests — the three CFO endpoints added under
`backend/app/api/analytics.py`:

  - GET /api/analytics/cashflow_forecast
  - GET /api/analytics/cashflow_whatif
  - GET /api/analytics/cash_position
  - GET /api/analytics/export/cashflow_forecast (CSV)

Real-Postgres harness (`realdb`): two persistent test tenants `a` / `b`.
Each test seeds its own invoices (+ optional PaymentSchedule) directly into
the tenant DB, then drives the endpoint through the ASGI client.

Covers:
  - RBAC: admin / ap_manager / CFO allowed (the copilot's audience); ap_clerk →
    403; unauthenticated → 401
  - forecast happy path buckets committed vs pending; excludes terminal
  - whatif early captures discount; granularity validation → 422
  - cash_position BYO opening balance + threshold breaches + source flag
  - CSV export content-type + header; unknown report → 404
  - tenant isolation: tenant a's invoices invisible under tenant b
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import PaymentSchedule

# The `realdb` fixture TRUNCATEs every tenant table at setup, so each test
# starts from a clean slate — no per-test cleanup fixture needed (and a
# second concurrent DELETE/TRUNCATE would deadlock against the fixture's).
_TODAY = date.today()


def _money(value) -> Decimal:
    """Read a money field off a response body, asserting it is an EXACT
    decimal string.

    Money never crosses the API boundary as a float (project invariant), and
    the `isinstance` check is the load-bearing half: `Decimal(str(1500.0))`
    would compare equal to `Decimal("1500")` just fine, so a value-only
    assertion would let this whole module regress to floats silently.
    """
    assert isinstance(value, str), f"money must serialise as a string, got {value!r}"
    return Decimal(value)


async def _add_invoice(
    realdb,
    key: str,
    *,
    amount,
    status,
    due_date,
    discount_date=None,
    discount_percent=None,
    currency=None,
) -> uuid.UUID:
    """Insert one Invoice (+ optional PaymentSchedule) into the tenant DB.

    `currency` other than the org's reporting currency, with no persisted
    `reporting_amount` lock, is what makes a row *unconvertible*."""
    mk = realdb.sessionmaker(key)
    async with mk() as s:
        inv = Invoice(
            organization_id=realdb.info(key).org_id,
            invoice_number=f"CF-{uuid.uuid4().hex[:8]}",
            vendor_name="Acme Supplies",
            amount=Decimal(str(amount)),
            status=status,
            invoice_date=_TODAY - timedelta(days=5),
            due_date=due_date,
            **({"currency": currency} if currency else {}),
        )
        s.add(inv)
        await s.flush()
        if discount_date is not None or discount_percent is not None:
            s.add(
                PaymentSchedule(
                    invoice_id=inv.id,
                    due_date=due_date,
                    discount_date=discount_date,
                    discount_percent=(
                        Decimal(str(discount_percent)) if discount_percent is not None else None
                    ),
                )
            )
        await s.commit()
        return inv.id


# ---------------------------------------------------------------------------
# cashflow_forecast
# ---------------------------------------------------------------------------


async def test_forecast_buckets_committed_and_pending(realdb):
    await _add_invoice(
        realdb,
        "a",
        amount="1000",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=10),
    )
    await _add_invoice(
        realdb,
        "a",
        amount="500",
        status=InvoiceStatus.pending.value,
        due_date=_TODAY + timedelta(days=12),
    )
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cashflow_forecast?granularity=month&horizon_days=90")
    assert resp.status_code == 200
    body = resp.json()
    assert body["granularity"] == "month"
    assert _money(body["totals"]["committed_amount"]) == Decimal("1000")
    assert _money(body["totals"]["pending_amount"]) == Decimal("500")
    assert _money(body["totals"]["scheduled_amount"]) == Decimal("1500")


async def test_forecast_reports_unconvertible_commitments(realdb):
    """A foreign invoice with no rate lock is included at FACE VALUE (dropping
    it would understate the outflow) — so the response has to say how many
    rows that applies to. Without the count, ¥10,000,000 reads as 10,000,000
    of the org's own currency and nothing on the page contradicts it."""
    await _add_invoice(
        realdb,
        "a",
        amount="10000000",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=10),
        currency="JPY",
    )
    await _add_invoice(
        realdb,
        "a",
        amount="1000",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=11),
    )
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cashflow_forecast?granularity=month&horizon_days=90")
    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"]["unconverted_count"] == 1
    assert body["totals"]["count"] == 2
    assert sum(p["unconverted_count"] for p in body["periods"]) == 1

    # The same caveat rides the running-balance curve, whose closing balance
    # carries forward — one unconvertible row poisons every later period.
    async with realdb.client(key="a", role="cfo") as c:
        pos = await c.get("/api/analytics/cash_position?opening_balance=250000&seed_balance=false")
    assert pos.status_code == 200
    pos_body = pos.json()
    assert pos_body["unconverted_count"] == 1
    assert sum(p["unconverted_count"] for p in pos_body["periods"]) == 1

    # And the what-if, whose whole purpose is comparing two outflow totals.
    async with realdb.client(key="a", role="cfo") as c:
        wi = await c.get("/api/analytics/cashflow_whatif?horizon_days=90")
    assert wi.status_code == 200
    for scenario in wi.json()["scenarios"].values():
        assert scenario["unconverted_count"] == 1


async def test_forecast_unconverted_count_is_zero_for_a_single_currency_book(realdb):
    await _add_invoice(
        realdb,
        "a",
        amount="1000",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=10),
    )
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cashflow_forecast?horizon_days=90")
    assert resp.json()["totals"]["unconverted_count"] == 0


async def test_forecast_excludes_terminal_and_paid(realdb):
    """paid / done / rejected / failed invoices must not appear in the
    projected-outflow total."""
    for status in (
        InvoiceStatus.paid.value,
        InvoiceStatus.done.value,
        InvoiceStatus.rejected.value,
        InvoiceStatus.failed.value,
    ):
        await _add_invoice(
            realdb,
            "a",
            amount="999",
            status=status,
            due_date=_TODAY + timedelta(days=10),
        )
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cashflow_forecast")
    assert resp.status_code == 200
    assert _money(resp.json()["totals"]["scheduled_amount"]) == Decimal("0")


async def test_forecast_include_pending_false_drops_pipeline(realdb):
    await _add_invoice(
        realdb,
        "a",
        amount="500",
        status=InvoiceStatus.pending.value,
        due_date=_TODAY + timedelta(days=10),
    )
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cashflow_forecast?include_pending=false")
    assert resp.status_code == 200
    assert _money(resp.json()["totals"]["scheduled_amount"]) == Decimal("0")


async def test_forecast_bad_granularity_422(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cashflow_forecast?granularity=fortnight")
    assert resp.status_code == 422


async def test_forecast_admin_allowed_clerk_forbidden(realdb):
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.get("/api/analytics/cashflow_forecast")).status_code == 200
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.get("/api/analytics/cashflow_forecast")).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/api/analytics/cashflow_forecast",
        "/api/analytics/cashflow_whatif",
        "/api/analytics/cash_position",
    ],
)
async def test_cash_reads_admit_ap_manager(realdb, path):
    """AP managers already see these figures through the cash-flow copilot and
    the forecast CSV export; the REST read must not be the one surface that
    refuses them."""
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.get(path)).status_code == 200


async def test_forecast_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        assert (await c.get("/api/analytics/cashflow_forecast")).status_code == 401


# ---------------------------------------------------------------------------
# cashflow_whatif
# ---------------------------------------------------------------------------


async def test_whatif_early_captures_discount(realdb):
    await _add_invoice(
        realdb,
        "a",
        amount="1000",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=20),
        discount_date=_TODAY + timedelta(days=5),
        discount_percent="2",
    )
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cashflow_whatif")
    assert resp.status_code == 200
    scenarios = resp.json()["scenarios"]
    assert _money(scenarios["early"]["total_discount_captured"]) == Decimal("20")
    assert _money(scenarios["early"]["total_outflow"]) == Decimal("980")
    assert _money(scenarios["on_time"]["total_outflow"]) == Decimal("1000")
    assert _money(scenarios["late"]["total_outflow"]) == Decimal("1000")


async def test_whatif_clerk_forbidden(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.get("/api/analytics/cashflow_whatif")).status_code == 403


# ---------------------------------------------------------------------------
# cash_position
# ---------------------------------------------------------------------------


async def _set_org_settings(realdb, key: str, mutate) -> None:
    """Apply `mutate(settings_dict)` to the tenant org's control-plane settings.

    The realdb fixture truncates TENANT tables but not the control-plane
    `organizations` row, so settings-mutating tests must reset what they set —
    each such test below restores the relevant block at the end."""
    from sqlalchemy import update

    from app.models.organization import Organization

    info = realdb.info(key)
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, info.org_id)
        settings = dict(org.settings or {})
        mutate(settings)
        await s.execute(
            update(Organization).where(Organization.id == info.org_id).values(settings=settings)
        )
        await s.commit()


async def test_cash_position_auto_seeds_from_provider_balance(realdb):
    """With no opening_balance query param, the position seeds from the org's
    configured (mock) payment provider's deterministic balance."""
    await _add_invoice(
        realdb,
        "a",
        amount="50000",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=10),
    )

    def _set_mock(settings):
        settings["payments"] = {"provider": "mock"}

    await _set_org_settings(realdb, "a", _set_mock)
    try:
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.get("/api/analytics/cash_position?granularity=month")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Mock deterministic balance 250000 seeded automatically.
        assert _money(body["opening_balance"]) == Decimal("250000")
        assert body["opening_balance_source"] == "provider"
        assert body["opening_balance_currency"] == "USD"
        assert _money(body["periods"][0]["closing"]) == Decimal("200000")
    finally:
        await _set_org_settings(realdb, "a", lambda s: s.pop("payments", None))


async def test_cash_position_seed_balance_false_skips_provider(realdb):
    """`seed_balance=false` skips the provider call and falls through to the
    `none` source (no manual balance, no settings balance)."""

    def _set_mock(settings):
        settings["payments"] = {"provider": "mock"}

    await _set_org_settings(realdb, "a", _set_mock)
    try:
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.get("/api/analytics/cash_position?seed_balance=false")
        assert resp.status_code == 200
        body = resp.json()
        assert _money(body["opening_balance"]) == Decimal("0")
        assert body["opening_balance_source"] == "none"
    finally:
        await _set_org_settings(realdb, "a", lambda s: s.pop("payments", None))


async def test_cash_position_query_balance_beats_provider(realdb):
    """An explicit opening_balance query param wins over the provider auto-sync."""

    def _set_mock(settings):
        settings["payments"] = {"provider": "mock"}

    await _set_org_settings(realdb, "a", _set_mock)
    try:
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.get("/api/analytics/cash_position?opening_balance=1000")
        assert resp.status_code == 200
        body = resp.json()
        assert _money(body["opening_balance"]) == Decimal("1000")
        # `explicit` (not `query`) — this endpoint now shares the copilot's
        # resolution chain, so the four source values have one vocabulary.
        assert body["opening_balance_source"] == "explicit"
    finally:
        await _set_org_settings(realdb, "a", lambda s: s.pop("payments", None))


async def test_cash_position_refuses_a_foreign_currency_provider_balance(realdb):
    """A funding account denominated in something other than the org's reporting
    currency must NOT seed the curve — every outflow subtracted from it here is
    in the reporting currency, so mixing them makes the running balance a silent
    two-currency figure. The chain falls through and says why."""

    def _set(settings):
        settings["payments"] = {
            "provider": "mock",
            "balance": "9999.99",
            "balance_currency": "EUR",
        }
        settings["reporting_currency"] = "USD"
        settings["cashflow"] = {"opening_balance": "4321"}

    await _set_org_settings(realdb, "a", _set)
    try:
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.get("/api/analytics/cash_position")
        assert resp.status_code == 200
        body = resp.json()
        assert _money(body["opening_balance"]) == Decimal("4321")
        assert body["opening_balance_source"] == "settings"
        # The refusal is visible — otherwise indistinguishable from "no bank".
        assert body["opening_balance_provider_skipped"] == "currency_mismatch"
        # The curve is always denominated in the reporting currency.
        assert body["opening_balance_currency"] == "USD"
    finally:

        def _clear(settings):
            settings.pop("payments", None)
            settings.pop("cashflow", None)
            settings.pop("reporting_currency", None)

        await _set_org_settings(realdb, "a", _clear)


async def test_cash_position_falls_back_to_settings_when_provider_unsupported(realdb):
    """Provider can't report a balance → fall through to the persisted
    settings.cashflow.opening_balance (source `settings`)."""

    def _set(settings):
        settings["payments"] = {"provider": "mock", "balance_available": False}
        settings["cashflow"] = {"opening_balance": "7777"}

    await _set_org_settings(realdb, "a", _set)
    try:
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.get("/api/analytics/cash_position")
        assert resp.status_code == 200
        body = resp.json()
        assert _money(body["opening_balance"]) == Decimal("7777")
        assert body["opening_balance_source"] == "settings"
    finally:

        def _clear(settings):
            settings.pop("payments", None)
            settings.pop("cashflow", None)

        await _set_org_settings(realdb, "a", _clear)


async def test_cash_position_rejects_a_malformed_opening_balance_param(realdb):
    """A bad value the CLIENT sent is a client error — still the 400
    `_parse_decimal_param` has always raised, unchanged by the move to the
    shared resolver (the query param is parsed at the call site precisely so
    this boundary survives)."""
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cash_position?opening_balance=not-a-number")
    assert resp.status_code == 400


async def test_cash_position_degrades_on_a_corrupt_persisted_balance(realdb):
    """A corrupt value in the org's OWN stored settings is not the caller's
    fault, so it must not 422 the dashboard — the chain falls through to the
    next link and the UI prompts for a balance, matching how
    `resolve_cash_thresholds` already treats a corrupt stored threshold."""

    def _set(settings):
        settings["cashflow"] = {"opening_balance": "not-a-number"}

    await _set_org_settings(realdb, "a", _set)
    try:
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.get("/api/analytics/cash_position")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert _money(body["opening_balance"]) == Decimal("0")
        assert body["opening_balance_source"] == "none"
    finally:
        await _set_org_settings(realdb, "a", lambda s: s.pop("cashflow", None))


async def test_cash_position_reads_persisted_threshold(realdb):
    """When the request passes no min_balance_threshold, the endpoint reads the
    org's persisted threshold and flags / collects breaches accordingly."""
    await _add_invoice(
        realdb,
        "a",
        amount="800",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=10),
    )

    def _set(settings):
        settings["cashflow"] = {"min_balance_threshold": "500"}

    await _set_org_settings(realdb, "a", _set)
    try:
        async with realdb.client(key="a", role="cfo") as c:
            resp = await c.get(
                "/api/analytics/cash_position?opening_balance=1000&granularity=month"
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Persisted threshold 500 applied (no query override): 1000-800=200 < 500.
        assert _money(body["threshold"]) == Decimal("500")
        assert body["periods"][0]["below_threshold"] is True
        assert len(body["breaches"]) == 1
        assert _money(body["breaches"][0]["shortfall"]) == Decimal("300")
    finally:
        await _set_org_settings(realdb, "a", lambda s: s.pop("cashflow", None))


# ---------------------------------------------------------------------------
# cash-position-settings (persisted thresholds GET/PUT)
# ---------------------------------------------------------------------------


async def test_cash_position_settings_round_trip(realdb):
    """PUT persists the threshold; GET reads it back; cash_position then applies
    it without a per-request override."""
    try:
        async with realdb.client(key="a", role="cfo") as c:
            # Default: nothing persisted.
            get0 = await c.get("/api/analytics/cash-position-settings")
            assert get0.status_code == 200
            assert get0.json()["min_balance_threshold"] is None

            put = await c.put(
                "/api/analytics/cash-position-settings",
                json={"min_balance_threshold": "2500.00"},
            )
            assert put.status_code == 200, put.text
            assert put.json()["min_balance_threshold"] == "2500.00"

            get1 = await c.get("/api/analytics/cash-position-settings")
            assert get1.json()["min_balance_threshold"] == "2500.00"
    finally:
        await _set_org_settings(realdb, "a", lambda s: s.pop("cashflow", None))


async def test_cash_position_settings_negative_rejected(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.put(
            "/api/analytics/cash-position-settings",
            json={"min_balance_threshold": "-1"},
        )
    assert resp.status_code == 422


async def test_cash_position_settings_clerk_forbidden(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.get("/api/analytics/cash-position-settings")).status_code == 403
        assert (
            await c.put(
                "/api/analytics/cash-position-settings",
                json={"min_balance_threshold": "100"},
            )
        ).status_code == 403


async def test_cash_position_settings_admin_allowed(realdb):
    try:
        async with realdb.client(key="a", role="admin") as c:
            assert (await c.get("/api/analytics/cash-position-settings")).status_code == 200
            assert (
                await c.put(
                    "/api/analytics/cash-position-settings",
                    json={"min_balance_threshold": "100"},
                )
            ).status_code == 200
    finally:
        await _set_org_settings(realdb, "a", lambda s: s.pop("cashflow", None))


async def test_cash_position_with_opening_balance_and_breach(realdb):
    await _add_invoice(
        realdb,
        "a",
        amount="800",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=10),
    )
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get(
            "/api/analytics/cash_position"
            "?opening_balance=1000&min_balance_threshold=500&granularity=month"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert _money(body["opening_balance"]) == Decimal("1000")
    assert body["opening_balance_source"] == "explicit"
    assert _money(body["periods"][0]["closing"]) == Decimal("200")
    assert body["periods"][0]["below_threshold"] is True
    assert len(body["breaches"]) == 1
    assert _money(body["breaches"][0]["shortfall"]) == Decimal("300")


async def test_cash_position_defaults_to_zero_with_source_none(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cash_position")
    assert resp.status_code == 200
    body = resp.json()
    assert _money(body["opening_balance"]) == Decimal("0")
    assert body["opening_balance_source"] == "none"


async def test_cash_position_bad_opening_balance_400(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cash_position?opening_balance=notamoney")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


async def test_export_cashflow_forecast_csv(realdb):
    await _add_invoice(
        realdb,
        "a",
        amount="1000",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=10),
    )
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/export/cashflow_forecast?granularity=month")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert "scheduled_amount" in resp.text
    assert "1000.00" in resp.text


async def test_export_unknown_report_404(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/export/does_not_exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# White-label branding on the analytics exports (CSV provenance + PDF)
# ---------------------------------------------------------------------------


async def test_export_csv_carries_brand_provenance_block(realdb):
    """The default CSV is prefixed with a `#`-comment provenance block (product
    name + report + generated-at); the data grid below still parses."""
    await _add_invoice(
        realdb,
        "a",
        amount="250",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=10),
    )
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/export/invoice_register")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.splitlines()
    assert lines[0].startswith("# ")
    assert lines[0].endswith("Analytics Export")
    assert any(ln.startswith("# Report: invoice_register") for ln in lines)
    assert any(ln.startswith("# Generated:") for ln in lines)
    # The data grid is intact below the comment block.
    assert "invoice_id" in resp.text


async def test_export_invoice_register_pdf(realdb):
    """`format=pdf` returns application/pdf with a .pdf attachment filename."""
    await _add_invoice(
        realdb,
        "a",
        amount="250",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=10),
    )
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/export/invoice_register?format=pdf")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.pdf"')
    assert resp.content.startswith(b"%PDF")


async def test_export_aging_snapshot_pdf(realdb):
    """A second report renders as PDF too (no rows needed — cover + table)."""
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/export/aging_snapshot?format=pdf")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content.startswith(b"%PDF")


async def test_export_bad_format_422(realdb):
    """An unsupported `format` is rejected by the route validator."""
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/export/invoice_register?format=xlsx")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_forecast_tenant_isolation(realdb):
    """An invoice seeded under tenant `a` must not surface when the
    forecast is queried under tenant `b`."""
    await _add_invoice(
        realdb,
        "a",
        amount="1234",
        status=InvoiceStatus.approved.value,
        due_date=_TODAY + timedelta(days=10),
    )
    async with realdb.client(key="b", role="cfo") as c:
        resp = await c.get("/api/analytics/cashflow_forecast")
    assert resp.status_code == 200
    assert _money(resp.json()["totals"]["scheduled_amount"]) == Decimal("0")


# ---------------------------------------------------------------------------
# multi-currency
# ---------------------------------------------------------------------------


async def test_forecast_outflows_are_in_the_reporting_currency(realdb):
    """A foreign-currency invoice must contribute its REPORTING amount, not
    its raw face value.

    `_commitment_rows` used to select `Invoice.amount` — the invoice's own
    currency — with no conversion, while every consumer subtracts those rows
    from an opening balance that `cashflow.resolve_opening_balance` guarantees
    is in the reporting currency (it REFUSES a provider balance in any other,
    on exactly this ground). A ¥10,000,000 invoice against a $250,000 opening
    balance projected a −$9.75M shortfall that does not exist — and the
    shortfall sweep emails finance leaders about it, the copilot re-times
    payments around it, and a draft payment run gets staged off that plan.
    """
    key = "a"
    mk = realdb.sessionmaker(key)
    due = _TODAY + timedelta(days=10)

    async with mk() as s:
        # Domestic: 1,000 USD, no lock needed (same currency, exact 1:1).
        s.add(
            Invoice(
                organization_id=realdb.info(key).org_id,
                invoice_number=f"CFFX-USD-{uuid.uuid4().hex[:6]}",
                vendor_name="Domestic Supplies",
                amount=Decimal("1000.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                invoice_date=_TODAY - timedelta(days=5),
                due_date=due,
            )
        )
        # Foreign: 10,000,000 JPY carrying a locked reporting amount of
        # 65,000 USD. Raw-summed it would swamp the curve by ~150x.
        s.add(
            Invoice(
                organization_id=realdb.info(key).org_id,
                invoice_number=f"CFFX-JPY-{uuid.uuid4().hex[:6]}",
                vendor_name="Foreign Supplies",
                amount=Decimal("10000000.00"),
                currency="JPY",
                reporting_amount=Decimal("65000.00"),
                reporting_currency="USD",
                status=InvoiceStatus.approved,
                invoice_date=_TODAY - timedelta(days=5),
                due_date=due,
            )
        )
        await s.commit()

    async with realdb.client(key=key, role="cfo") as c:
        resp = await c.get("/api/analytics/cashflow_forecast?granularity=month&horizon_days=90")
    assert resp.status_code == 200, resp.text
    totals = resp.json()["totals"]

    # 1,000 USD + 65,000 USD — NOT 1,000 + 10,000,000.
    assert _money(totals["scheduled_amount"]) == Decimal("66000.00"), totals
    assert _money(totals["committed_amount"]) == Decimal("66000.00"), totals


# ---------------------------------------------------------------------------
# One commitment row per invoice, whatever its schedule count
# ---------------------------------------------------------------------------


async def test_two_payment_schedules_do_not_double_count_the_invoice(realdb):
    """`_commitment_rows` fanned an invoice out to one row per `PaymentSchedule`.

    The bare `outerjoin(PaymentSchedule)` returned a row per schedule and the
    loop appended a commitment row for each, so a two-schedule invoice was
    counted TWICE at its FULL amount — in the forecast, the cash-position curve,
    the what-if, every copilot planning tool and the `plan_id` hash at once,
    with nothing on any surface to show it. Nothing but `scripts/seed.py`
    constructs a `PaymentSchedule` today, which is an accident of the current
    feature set, not a guarantee.

    Dedup takes the LATEST schedule, matching
    `discount_auto_trigger._resolve_due_date`'s existing precaution on the same
    table — so the discount engine and the cash forecast can't disagree about an
    invoice's due date. The two schedules are written in SEPARATE transactions,
    because `created_at` is `transaction_timestamp()`: rows sharing a commit
    share a timestamp, and "latest" is then genuinely undefined between them.
    """
    key = "a"
    mk = realdb.sessionmaker(key)
    superseded_due = _TODAY + timedelta(days=5)
    current_due = _TODAY + timedelta(days=20)

    async with mk() as s:
        invoice = Invoice(
            organization_id=realdb.info(key).org_id,
            invoice_number=f"CFDUP-{uuid.uuid4().hex[:6]}",
            vendor_name="Rescheduled Supplies",
            amount=Decimal("1200.00"),
            currency="USD",
            status=InvoiceStatus.approved,
            invoice_date=_TODAY - timedelta(days=5),
            # Deliberately different from BOTH schedules, so the assertions
            # below can tell a schedule date from the invoice fallback.
            due_date=_TODAY + timedelta(days=60),
        )
        s.add(invoice)
        await s.flush()
        s.add(
            PaymentSchedule(
                invoice_id=invoice.id,
                due_date=superseded_due,
                discount_date=_TODAY + timedelta(days=1),
                discount_percent=Decimal("2.00"),
            )
        )
        await s.commit()
        invoice_id = invoice.id

    # The reschedule is its OWN transaction, exactly as a real reschedule is.
    # `created_at` defaults to `func.now()`, which Postgres resolves to
    # `transaction_timestamp()` — constant within a transaction — so two rows
    # written in one commit carry the SAME timestamp and "latest" is genuinely
    # undefined between them. `_resolve_due_date` has the same property; the
    # `id` tiebreak in the query only makes that case deterministic (so the
    # copilot's `plan_id` can't flap between reads), it does not invent an order.
    async with mk() as s:
        s.add(
            PaymentSchedule(
                invoice_id=invoice_id,
                due_date=current_due,
                discount_date=_TODAY + timedelta(days=8),
                discount_percent=Decimal("1.00"),
            )
        )
        await s.commit()

    async with realdb.client(key=key, role="cfo") as c:
        resp = await c.get("/api/analytics/cashflow_forecast?granularity=week&horizon_days=90")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Counted ONCE, at face value — 1,200.00, not 2,400.00.
    assert _money(body["totals"]["scheduled_amount"]) == Decimal("1200.00"), body["totals"]
    assert _money(body["totals"]["committed_amount"]) == Decimal("1200.00"), body["totals"]

    # `count` is the sharpest signal — two rows for one invoice IS the bug.
    assert body["totals"]["count"] == 1, body["totals"]

    # …and it lands on the LATEST schedule's date, not the superseded one and
    # not the invoice's own due date.
    non_empty = [p for p in body["periods"] if _money(p["scheduled_amount"]) != Decimal("0")]
    assert len(non_empty) == 1, non_empty
    period = non_empty[0]
    assert _money(period["scheduled_amount"]) == Decimal("1200.00")
    assert period["count"] == 1
    assert period["period_start"] <= current_due.isoformat() <= period["period_end"], period
    assert not (period["period_start"] <= superseded_due.isoformat() <= period["period_end"]), (
        "the superseded schedule's date must not drive the bucket"
    )
