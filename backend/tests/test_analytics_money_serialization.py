"""Money on `/api/analytics/*` is an EXACT decimal string, and nothing else is.

The project invariant is that money never crosses the API boundary as a float,
because a binary float can't hold an exact cent value — and this module is
where that invariant was violated in ~45 places at once, feeding a shipped web
dashboard and a shipped Flutter screen. `docs/decisions.md` §32 records why
`/drill/dpo` was corrected alone and the rest deferred; this is the rest.

The guard is **structural, not per-field**, because a per-field assertion only
pins the fields someone remembered to list. Instead every response is walked
recursively and every JSON *number* it contains must appear in
``NUMERIC_FIELDS`` — the declared roster of things that are genuinely not
money:

  - **day counts** (`dpo`, `cash_conversion_cycle`, `weighted_avg_pay_date_days`)
  - **percentages** (`*_share_pct`, `rate_pct`, `yield_pct`, `variance_pct`)
  - **counts** and request echoes (`period_days`, `horizon_days`, …)

So a NEW money field added as a float fails here until it is either serialised
through ``analytics._money`` or consciously declared non-money — which is the
whole point. Converting a day count or a percentage to a string would be a bug
wearing compliance's clothes, so the roster is the other half of the contract,
not an escape hatch.

Every declared money field is additionally asserted to parse as a Decimal, so
a string that isn't actually a number can't sneak through the type check.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.models.invoice import Invoice, InvoiceStatus

_TODAY = date.today()

# Every JSON number legitimately reachable in an /api/analytics/* response.
# A key NOT in here must serialise as an exact decimal string.
NUMERIC_FIELDS: frozenset[str] = frozenset(
    {
        # --- request echoes / window parameters -------------------------------
        "period_days",
        "horizon_days",
        "grace_days",
        "months",
        # --- day counts (NOT money) -------------------------------------------
        "dpo",
        "dpo_current",
        "cash_conversion_cycle",
        "weighted_avg_pay_date_days",
        # --- percentages (NOT money) ------------------------------------------
        "share_pct",
        "top_10_share_pct",
        "top_50_share_pct",
        "largest_vendor_share_pct",
        "rate_pct",
        "yield_pct",
        "variance_pct",
        # --- counts -----------------------------------------------------------
        "count",
        "total_count",
        "unconverted_count",
        "invoice_count",
        "exception_count",
        "open_exceptions",
        "reporting_avg_daily_outflow_unconverted_count",
        "reporting_outstanding_unconverted_count",
        "reporting_total_spend_unconverted_count",
        # Rebates excluded from the rebate-yield NUMERATOR for being
        # denominated in another currency (decisions §62). A count, not money —
        # it exists so a 10x-understated yield says so rather than looking
        # complete.
        "excluded_rebate_count",
    }
)


def _assert_numbers_are_declared(node, path: str = "$") -> None:
    """Walk a response body; every JSON number must be a declared non-money key.

    `bool` is excluded explicitly — it is an `int` subclass in Python, and
    `below_threshold` / `flagged` / `available` are flags, not figures.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if isinstance(value, bool):
                continue
            if isinstance(value, int | float):
                assert key in NUMERIC_FIELDS, (
                    f"{child} serialises as a JSON number ({value!r}). If it is money it must "
                    "go through `analytics._money` (exact decimal string); if it is genuinely "
                    "a day count / percentage / count, add it to NUMERIC_FIELDS."
                )
                continue
            _assert_numbers_are_declared(value, child)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _assert_numbers_are_declared(value, f"{path}[{i}]")


def _assert_money_strings(node, *, keys: set[str], path: str = "$") -> set[str]:
    """Assert every occurrence of `keys` is a Decimal-parseable string (or null).

    Returns the set of keys actually seen, so a caller can prove the fields it
    cares about were present rather than silently absent from a thin fixture.
    """
    seen: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if key in keys:
                seen.add(key)
                if value is None:
                    continue
                assert isinstance(value, str), (
                    f"{child} is money and must serialise as an exact decimal string, got {value!r}"
                )
                try:
                    Decimal(value)
                except InvalidOperation as exc:  # pragma: no cover - failure path
                    raise AssertionError(f"{child} is not a decimal string: {value!r}") from exc
            seen |= _assert_money_strings(value, keys=keys, path=child)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            seen |= _assert_money_strings(value, keys=keys, path=f"{path}[{i}]")
    return seen


async def _seed(realdb, key: str = "a") -> None:
    """A minimal population that makes every CFO tile non-empty.

    An approved invoice inside the trailing window gives spend, an open AP
    balance, a concentration row and a forecast commitment; a completed
    payment against it gives the paid-in-period leg `avg_daily_outflow` and
    `working_capital_impact_5_days` are derived from.
    """
    from app.models.payment import Payment

    mk = realdb.sessionmaker(key)
    async with mk() as s:
        inv = Invoice(
            organization_id=realdb.info(key).org_id,
            invoice_number=f"MS-{uuid.uuid4().hex[:8]}",
            vendor_name="Acme Supplies",
            amount=Decimal("1234.56"),
            currency="USD",
            status=InvoiceStatus.approved.value,
            invoice_date=_TODAY - timedelta(days=5),
            due_date=_TODAY + timedelta(days=10),
        )
        s.add(inv)
        await s.flush()
        # "Yesterday", but clamped into the current calendar month — the
        # forecast-variance test buckets this payment by `_TODAY`'s month, so a
        # bare `_TODAY - 1 day` drops it into the prior month's bucket on the
        # 1st (→ `actual` reads 0.00). Still 0-1 days old, so the trailing-window
        # assertions in the sibling tests are unaffected.
        paid_on = max(_TODAY - timedelta(days=1), _TODAY.replace(day=1))
        s.add(
            Payment(
                invoice_id=inv.id,
                amount=Decimal("500.25"),
                method="ach",
                status="completed",
                completed_at=datetime.combine(paid_on, datetime.min.time()).replace(tzinfo=UTC),
            )
        )
        await s.commit()


# ---------------------------------------------------------------------------
# /api/analytics/cfo — the aggregate dashboard
# ---------------------------------------------------------------------------

# Declared so a fixture that happens not to exercise a tile can't make the
# structural walk vacuously pass for that tile's money.
_CFO_MONEY_FIELDS = {
    "total_spend",
    "total_amount",
    "original_amount",
    "reporting_amount",
    "accounts_payable_balance",
    "open_po_amount",
    "received_amount",
    "unposted_invoice_amount",
    "total_accrual",
    "working_capital_impact_5_days",
    "avg_daily_outflow",
    "rebates_total",
    "annualised_rebates",
    "total_unrealized_gain_loss",
    "open_original_amount",
    "booked_reporting_amount",
    "current_reporting_amount",
    "unrealized_gain_loss",
}


async def test_cfo_money_is_exact_strings_and_day_counts_stay_numbers(realdb):
    await _seed(realdb)
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cfo?period_days=365")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    _assert_numbers_are_declared(body)
    seen = _assert_money_strings(body, keys=_CFO_MONEY_FIELDS)

    # The tiles the seeded population guarantees are present.
    for required in (
        "total_spend",
        "accounts_payable_balance",
        "total_accrual",
        "avg_daily_outflow",
        "working_capital_impact_5_days",
        "rebates_total",
    ):
        assert required in seen, f"{required} missing from the response"

    # The seeded figures survive exactly — the whole reason for the change.
    assert Decimal(body["total_spend"]) == Decimal("1234.56")
    assert Decimal(body["accounts_payable_balance"]) == Decimal("1234.56")
    assert Decimal(body["supplier_concentration"]["total_spend"]) == Decimal("1234.56")

    # Day counts and percentages are numbers, not strings.
    assert isinstance(body["dpo_current"], int | float)
    assert body["cash_conversion_cycle"] is None or isinstance(
        body["cash_conversion_cycle"], int | float
    )
    assert isinstance(body["supplier_concentration"]["top_10_share_pct"], int | float)
    assert isinstance(body["rebate_yield"]["yield_pct"], int | float)


async def test_cfo_zero_population_still_serialises_money_as_strings(realdb):
    """An empty tenant must not fall back to numeric zeros.

    `0` and `"0"` both read as "nothing here", so a zero-population response is
    exactly where a float would go unnoticed for months.
    """
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cfo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_numbers_are_declared(body)
    _assert_money_strings(body, keys=_CFO_MONEY_FIELDS)
    assert body["total_spend"] == "0"
    # The unrealized-FX fallback zero rides the same rule.
    assert isinstance(body["unrealized_fx"]["total_unrealized_gain_loss"], str)


# ---------------------------------------------------------------------------
# Predictive cash-flow trio
# ---------------------------------------------------------------------------

_CASHFLOW_MONEY_FIELDS = {
    "scheduled_amount",
    "committed_amount",
    "pending_amount",
    "discount_eligible_amount",
    "total_outflow",
    "total_discount_captured",
    "opening_balance",
    "threshold",
    "opening",
    "outflow",
    "inflow",
    "closing",
    "shortfall",
}


async def test_cashflow_endpoints_money_is_exact_strings(realdb):
    await _seed(realdb)
    async with realdb.client(key="a", role="cfo") as c:
        for url in (
            "/api/analytics/cashflow_forecast?granularity=month",
            "/api/analytics/cashflow_whatif?granularity=month",
            "/api/analytics/cash_position?granularity=month&opening_balance=5000",
        ):
            resp = await c.get(url)
            assert resp.status_code == 200, f"{url}: {resp.text}"
            body = resp.json()
            _assert_numbers_are_declared(body, path=url)
            _assert_money_strings(body, keys=_CASHFLOW_MONEY_FIELDS, path=url)


async def test_cash_position_null_threshold_stays_null(realdb):
    """No threshold set is `null`, never `"0"` — which would read as
    'alert on any positive balance'."""
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/cash_position")
    assert resp.status_code == 200, resp.text
    assert resp.json()["threshold"] is None


# ---------------------------------------------------------------------------
# Drill-throughs + forecast variance + the by-entity rollup
# ---------------------------------------------------------------------------
#
# These three had no shipped frontend or mobile consumer, which is why
# `/drill/dpo` and `/by-entity` could be corrected in isolation ahead of the
# rest. They are pinned here anyway so the whole module answers to one rule.

_DRILL_MONEY_FIELDS = {"amount", "total_spend", "accounts_payable", "cogs"}
_VARIANCE_MONEY_FIELDS = {"forecast", "actual", "variance"}
_BY_ENTITY_MONEY_FIELDS = {"total_spend", "outstanding_amount", "open_po_amount"}


async def test_drill_endpoints_money_is_exact_strings(realdb):
    await _seed(realdb)
    async with realdb.client(key="a", role="cfo") as c:
        conc = await c.get("/api/analytics/drill/spend_concentration?period_days=365")
        dpo = await c.get("/api/analytics/drill/dpo?months=3")
    assert conc.status_code == 200, conc.text
    assert dpo.status_code == 200, dpo.text

    conc_body = conc.json()
    _assert_numbers_are_declared(conc_body, path="drill/spend_concentration")
    seen = _assert_money_strings(conc_body, keys=_DRILL_MONEY_FIELDS)
    assert {"amount", "total_spend"} <= seen
    assert Decimal(conc_body["total_spend"]) == Decimal("1234.56")
    # `share_pct` is a percentage, not money.
    assert isinstance(conc_body["rows"][0]["share_pct"], int | float)

    dpo_body = dpo.json()
    _assert_numbers_are_declared(dpo_body, path="drill/dpo")
    _assert_money_strings(dpo_body, keys=_DRILL_MONEY_FIELDS)


async def test_forecast_variance_money_is_exact_strings(realdb):
    await _seed(realdb)
    month = _TODAY.strftime("%Y-%m")
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post(
            "/api/analytics/forecast_variance",
            json={"months": [{"month": month, "forecast": "1000.00"}]},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_numbers_are_declared(body, path="forecast_variance")
    seen = _assert_money_strings(body, keys=_VARIANCE_MONEY_FIELDS)
    assert seen == _VARIANCE_MONEY_FIELDS
    row = body["rows"][0]
    # The seeded completed payment is the actual; variance = actual - forecast.
    assert Decimal(row["actual"]) == Decimal("500.25")
    assert Decimal(row["variance"]) == Decimal("-499.75")
    # `variance_pct` is a percentage, not money.
    assert isinstance(row["variance_pct"], int | float)


async def test_by_entity_rollup_money_is_exact_strings(realdb):
    await _seed(realdb)
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/analytics/by-entity")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_numbers_are_declared(body, path="by-entity")
    seen = _assert_money_strings(body, keys=_BY_ENTITY_MONEY_FIELDS)
    assert seen == _BY_ENTITY_MONEY_FIELDS
    assert Decimal(body["consolidated"]["total_spend"]) == Decimal("1234.56")


# ---------------------------------------------------------------------------
# The serialiser itself
# ---------------------------------------------------------------------------


def test_money_formats_fixed_point_never_scientific():
    """`str(Decimal("1E+3"))` is `"1E+3"`.

    Both forms parse in Python, but a money field carrying `"1E+3"` is exactly
    the value a downstream consumer's own parser fumbles — and an exact-string
    contract whose figure needs interpreting isn't one. `_money` formats
    fixed-point so the wire value is always plainly a decimal.
    """
    from app.api.analytics import _money

    assert _money(Decimal("1E+3")) == "1000"
    assert _money(Decimal("1.2E-3")) == "0.0012"
    assert _money(Decimal("-1E+2")) == "-100"


def test_money_preserves_scale_and_passes_none_through():
    """Trailing zeros are the figure's precision, not noise — this is not
    `.normalize()`. And `None` stays JSON null, never the string `"None"`."""
    from app.api.analytics import _money

    assert _money(Decimal("0.00")) == "0.00"
    assert _money(Decimal("1234.50")) == "1234.50"
    assert _money(Decimal("0")) == "0"
    assert _money(None) is None


def _bare_str_calls(source: str) -> list[str]:
    """Find every bare `str(...)` in `source` that could be serialising money.

    Two exclusions, both narrow on purpose:

      - `Decimal(str(x))` — the opposite direction (parsing a DB scalar INTO a
        Decimal), and the module's established idiom.
      - `str(<name>_id)` / `str(<expr>.id)` — a UUID, not money. Matched on a
        real `_id` suffix (or the bare name `id`), NOT on the two characters
        `"id"`: `"total_paid"`, `"amount_paid"` and `"unpaid"` all end in
        `i`+`d`, and those are exactly the names a new money field in THIS
        module would carry (`paid_in_period` already exists). A loose suffix
        check would have opened a hole in the middle of the guard.

    Works on the AST, not on lines, so a docstring or comment mentioning
    `str()` can't trip it — the scan is about code, not prose.
    """
    import ast

    tree = ast.parse(source)

    # `str(...)` calls that are a direct argument of a `Decimal(...)` call.
    inside_decimal: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Decimal"
        ):
            for arg in node.args:
                if (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Name)
                    and arg.func.id == "str"
                ):
                    inside_decimal.add(id(arg))

    def _is_uuid_arg(call: ast.Call) -> bool:
        if len(call.args) != 1:
            return False
        arg = call.args[0]
        name = (
            arg.id
            if isinstance(arg, ast.Name)
            else arg.attr
            if isinstance(arg, ast.Attribute)
            else ""
        )
        return name == "id" or name.endswith("_id")

    return [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and id(node) not in inside_decimal
        and not _is_uuid_arg(node)
    ]


def test_analytics_module_has_no_second_money_serialiser():
    """`_money` is the module's ONE money serialiser.

    A source scan, because the failure it guards is a *new* response field
    hand-serialised with `str(...)` — which works, looks right in review, and
    silently reintroduces the scientific-notation and `"None"` holes `_money`
    closes.
    """
    from pathlib import Path

    import app.api.analytics as analytics_module

    offenders = _bare_str_calls(Path(analytics_module.__file__).read_text())
    assert not offenders, (
        "bare `str(...)` in api/analytics.py — if it is a money field it must go "
        f"through `_money`: {offenders}"
    )


def test_the_scan_catches_a_money_str_and_spares_the_two_idioms():
    """A test for the guard itself, because a guard with a hole is worse than
    none — it certifies the thing it stopped checking.

    The `_paid` case is the one that mattered: a bare `"id"` suffix check
    silently exempted `str(total_paid)`, which is exactly the shape a new money
    field in this module would take.
    """
    caught = _bare_str_calls(
        """
def f(total_paid, amount_paid, unpaid_total):
    return {
        "a": str(total_paid),
        "b": str(amount_paid),
        "c": str(unpaid_total),
    }
"""
    )
    assert len(caught) == 3, caught

    spared = _bare_str_calls(
        """
def f(row, inv_id, e):
    amount = Decimal(str(row.amount or 0))
    return {"amount": _money(amount), "id": str(inv_id), "entity": str(e.id)}
"""
    )
    assert spared == [], spared
