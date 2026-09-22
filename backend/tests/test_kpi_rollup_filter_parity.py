"""Behavioural parity: a KPI rollup must describe EXACTLY the filtered set.

`tests/test_whole_set_kpi_rollups.py` is the *structural* guard — it discovers
every `GET .../summary` with a list parent, asserts the summary accepts every
filter its list offers, and asserts both endpoints route through the one shared
`_*_list_filters` builder. That catches a surface that cannot express a filter.
It cannot catch a surface that accepts the filter and then computes the wrong
number.

This file is that second half, against real Postgres. For each of the eight
surfaces the #321 KPI-parity family touched it:

* seeds a population **deliberately larger than one page**
  (`DEFAULT_PAGE_SIZE` == 20) — several of the pre-fix KPIs reduced over the
  rows already LOADED, so a page-scoped bug is invisible under 20 rows;
* applies each filter the list accepts and compares the rollup against an
  expectation **recomputed in the test** from the seed spec. The expectation is
  never produced by calling the helper the endpoint calls — that would only
  prove the endpoint calls itself;
* pins the documented dimension exceptions (`/api/invoices/counts` honours every
  population filter but ignores `status`; `/api/exceptions/summary` honours every
  filter but each tally's own dimension; `/api/payments/summary` is deliberately
  wider than any filtered table);
* pins the money invariant on every money-bearing rollup: exact decimal
  **strings**, split per currency, never one cross-currency sum;
* pins entity scoping — `X-Entity-ID` must narrow the list and its rollup
  identically.

Surfaces: `/api/budgets`, `/api/intake`, `/api/positive-pay`, `/api/recurring`,
`/api/requisitions`, `/api/vendor-statements`, `/api/invoices/counts`,
`/api/expenses` (list + summary + export), `/api/exceptions` (list + ids + summary).
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import select

from app.api.pagination import DEFAULT_PAGE_SIZE
from app.models.entity import Entity
from app.models.exception import Exception as APException
from app.models.expense import Expense, ExpenseReport
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.positive_pay import PositivePayFile
from app.models.procurement import (
    Budget,
    IntakeRequest,
    PurchaseRequisition,
)
from app.models.recurring_invoice import RecurringInvoiceTemplate
from app.models.vendor import Vendor
from app.models.vendor_statement_recon import (
    CLASS_AMOUNT_MISMATCH,
    CLASS_MISSING_OUR_SIDE,
    CLASS_MISSING_THEIR_SIDE,
    RESOLUTION_UNRESOLVED,
    VendorStatementReconciliation,
    VendorStatementReconLine,
)
from app.utils.dates import utc_today

pytestmark = pytest.mark.asyncio

# The window every pre-fix KPI silently reduced over. Every population below is
# seeded past it so a page-scoped rollup cannot pass by coincidence.
assert DEFAULT_PAGE_SIZE == 20
_OVER_A_PAGE = DEFAULT_PAGE_SIZE + 5

# `utc_today()`, not `date.today()`: the backend resolves "today" in UTC
# everywhere, and a local-date fixture disagrees with it for hours a day
# west of UTC (backend/CLAUDE.md § Date-sensitive tests).
_TODAY = utc_today()
_CENTS = Decimal("0.01")


# ---------------------------------------------------------------------------
# Test-side expectation helpers — pure Python over the seed spec.
#
# These deliberately re-implement "what the filter means" instead of importing
# `_*_list_filters`. A test that asked the endpoint's own builder for the
# expected set would pass no matter what that builder did.
# ---------------------------------------------------------------------------


def _contains(haystack, needle: str) -> bool:
    """The ILIKE-substring semantics every list `search` leg uses, spelled out
    here rather than borrowed from `app.utils.search`."""
    return needle.lower() in (haystack or "").lower()


def _tally(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[str(row[key])] = out.get(str(row[key]), 0) + 1
    return out


def _money_by_currency(rows: list[dict], amount_key: str = "amount") -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for row in rows:
        code = str(row["currency"]).upper()
        out[code] = out.get(code, Decimal("0")) + Decimal(row[amount_key])
    return out


def _by_currency(body: dict, field: str = "by_currency") -> dict[str, dict]:
    return {row["currency"]: row for row in body[field]}


async def _default_entity_id(session) -> uuid.UUID:
    return (
        await session.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
    ).scalar_one()


async def _extra_entity(realdb, key: str = "a", *, slug: str = "sub") -> uuid.UUID:
    """A second, non-default entity in the tenant — the `X-Entity-ID` target."""
    mk = realdb.sessionmaker(key)
    eid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Entity(
                id=eid,
                name="Subsidiary Two",
                slug=slug,
                organization_id=realdb.info(key).org_id,
            )
        )
        await s.commit()
    return eid


def _csv_rows(body: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(body)))


# ===========================================================================
# /api/budgets  — whole-set count + per-currency allocation (PR #349)
# ===========================================================================

# 25 rows: two dimensions, two periods, two currencies, and a searchable token
# on a known subset. Amounts are distinct so a dropped or double-counted row
# moves the total, not just the count.
_BUDGET_SEED: list[dict] = (
    [
        {
            "name": f"Engineering cloud {i}",
            "dimension": "department",
            "dimension_value": "Engineering",
            "period": "2026",
            "amount": f"{100 + i}.11",
            "currency": "USD",
        }
        for i in range(12)
    ]
    + [
        {
            "name": f"Atlas rollout {i}",
            "dimension": "project",
            "dimension_value": "Atlas",
            "period": "2026",
            "amount": f"{200 + i}.07",
            "currency": "EUR",
        }
        for i in range(8)
    ]
    + [
        {
            "name": f"Legacy facilities {i}",
            "dimension": "department",
            "dimension_value": "Facilities",
            "period": "2025",
            "amount": f"{300 + i}.03",
            "currency": "USD",
        }
        for i in range(5)
    ]
)


def _budget_expect(
    rows: list[dict],
    *,
    dimension: str | None = None,
    period: str | None = None,
    search: str | None = None,
) -> list[dict]:
    kept = rows
    if dimension:
        kept = [r for r in kept if r["dimension"] == dimension]
    if period:
        kept = [r for r in kept if r["period"] == period]
    if search and search.strip():
        term = search.strip()
        kept = [
            r for r in kept if _contains(r["name"], term) or _contains(r["dimension_value"], term)
        ]
    return kept


async def _seed_budgets(realdb, key="a", *, entity_id=None, rows=None) -> None:
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    async with mk() as s:
        eid = entity_id if entity_id is not None else await _default_entity_id(s)
        for spec in rows if rows is not None else _BUDGET_SEED:
            s.add(
                Budget(
                    name=spec["name"],
                    dimension=spec["dimension"],
                    dimension_value=spec["dimension_value"],
                    period=spec["period"],
                    amount=Decimal(spec["amount"]),
                    currency=spec["currency"],
                    organization_id=org_id,
                    entity_id=eid,
                )
            )
        await s.commit()


async def test_budget_summary_covers_the_whole_set_not_the_loaded_page(realdb):
    """The headline defect: `totalAllocated` reduced over the 20 rows the page
    had, beside a whole-set `total` count. With 25 rows seeded, a page-scoped
    sum is 5 rows and several hundred short."""
    await _seed_budgets(realdb)

    async with realdb.client(key="a", role="cfo") as c:
        listed = (await c.get("/api/budgets")).json()
        body = (await c.get("/api/budgets/summary")).json()

    # The list really is paginated past one window — otherwise this proves nothing.
    assert len(listed["items"]) == DEFAULT_PAGE_SIZE
    assert listed["total"] == len(_BUDGET_SEED) == _OVER_A_PAGE

    assert body["total"] == _OVER_A_PAGE
    expected = _money_by_currency(_BUDGET_SEED)
    got = _by_currency(body)
    assert set(got) == set(expected)
    for code, total in expected.items():
        assert got[code]["total"] == str(total), code
        assert got[code]["count"] == sum(1 for r in _BUDGET_SEED if r["currency"] == code), code
    # The page-scoped figure this replaced, computed the old way, is NOT what
    # the endpoint returns — the assertion has teeth.
    page_only = _money_by_currency(_BUDGET_SEED[:DEFAULT_PAGE_SIZE])
    assert got["USD"]["total"] != str(page_only.get("USD"))


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"dimension": "department"},
        {"dimension": "project"},
        {"period": "2026"},
        {"period": "2025"},
        {"search": "atlas"},
        {"search": "Engineering"},  # matches dimension_value, not just name
        {"dimension": "department", "period": "2025"},
        {"dimension": "project", "search": "rollout"},
        {"search": "nothing-matches-this"},
    ],
)
async def test_budget_summary_equals_an_independent_recount_under_every_filter(realdb, params):
    await _seed_budgets(realdb)
    expected_rows = _budget_expect(_BUDGET_SEED, **params)
    expected_money = _money_by_currency(expected_rows)

    async with realdb.client(key="a", role="cfo") as c:
        listed = (await c.get("/api/budgets", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/budgets/summary", params=params)).json()

    assert listed["total"] == len(expected_rows), params
    assert body["total"] == len(expected_rows), params
    got = _by_currency(body)
    assert set(got) == set(expected_money), params
    for code, total in expected_money.items():
        assert got[code]["total"] == str(total), (params, code)


async def test_budget_summary_never_blends_two_currencies_into_one_figure(realdb):
    await _seed_budgets(realdb)
    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/budgets/summary")).json()

    got = _by_currency(body)
    assert set(got) == {"USD", "EUR"}
    blended = sum(Decimal(r["total"]) for r in body["by_currency"])
    assert all(Decimal(r["total"]) != blended for r in body["by_currency"])
    # Money crosses the JSON boundary as an exact string, never a float.
    for row in body["by_currency"]:
        assert isinstance(row["total"], str)
        assert Decimal(row["total"]) == Decimal(row["total"]).quantize(_CENTS)


async def test_budget_summary_single_currency_org_reports_one_group(realdb):
    """The one-currency tenant is the common case and must not grow a second,
    empty, or platform-default group."""
    single = [dict(r, currency="USD") for r in _BUDGET_SEED]
    await _seed_budgets(realdb, key="b", rows=single)
    async with realdb.client(key="b", role="cfo") as c:
        body = (await c.get("/api/budgets/summary")).json()

    assert [r["currency"] for r in body["by_currency"]] == ["USD"]
    assert body["by_currency"][0]["total"] == str(_money_by_currency(single)["USD"])
    assert body["total"] == len(single)


async def test_budget_summary_and_list_narrow_identically_on_x_entity_id(realdb):
    sub = await _extra_entity(realdb, slug="budget-sub")
    await _seed_budgets(realdb)  # default entity
    sub_rows = _BUDGET_SEED[:3]
    await _seed_budgets(realdb, entity_id=sub, rows=sub_rows)

    async with realdb.client(key="a", role="cfo") as c:
        scoped_list = (
            await c.get(
                "/api/budgets", params={"page_size": 100}, headers={"X-Entity-ID": str(sub)}
            )
        ).json()
        scoped_sum = (await c.get("/api/budgets/summary", headers={"X-Entity-ID": str(sub)})).json()
        consolidated = (await c.get("/api/budgets/summary")).json()

    assert scoped_list["total"] == len(sub_rows)
    assert scoped_sum["total"] == len(sub_rows)
    assert _by_currency(scoped_sum)["USD"]["total"] == str(_money_by_currency(sub_rows)["USD"])
    # No header = consolidated across entities.
    assert consolidated["total"] == len(_BUDGET_SEED) + len(sub_rows)


async def test_budget_summary_is_a_well_formed_zero_on_an_empty_set(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/budgets/summary")).json()
    assert body == {"total": 0, "by_currency": []}


# ===========================================================================
# /api/intake  — whole-set status counts (PR #349)
# ===========================================================================

_INTAKE_SEED: list[dict] = (
    [
        {
            "request_number": f"INTK-OPEN-{i}",
            "title": f"Figma seats {i}",
            "request_type": "software",
            "status": "open",
            "vendor_name": "Figma Inc",
        }
        for i in range(11)
    ]
    + [
        {
            "request_number": f"INTK-REV-{i}",
            "title": f"Datacentre racks {i}",
            "request_type": "hardware",
            "status": "in_review",
            "vendor_name": "Rackspace Ltd",
        }
        for i in range(9)
    ]
    + [
        {
            "request_number": f"INTK-APP-{i}",
            "title": f"Legal retainer {i}",
            "request_type": "services",
            "status": "approved",
            "vendor_name": "Figma Inc",
        }
        for i in range(5)
    ]
)


def _intake_expect(
    rows: list[dict],
    *,
    status: str | None = None,
    type: str | None = None,  # noqa: A002 — matches the query-param name
    search: str | None = None,
) -> list[dict]:
    kept = rows
    if status:
        kept = [r for r in kept if r["status"] == status]
    if type:
        kept = [r for r in kept if r["request_type"] == type]
    if search and search.strip():
        term = search.strip()
        kept = [
            r
            for r in kept
            if _contains(r["request_number"], term)
            or _contains(r["title"], term)
            or _contains(r["vendor_name"], term)
        ]
    return kept


async def _seed_intake(realdb, key="a", *, entity_id=None, rows=None) -> None:
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    async with mk() as s:
        eid = entity_id if entity_id is not None else await _default_entity_id(s)
        for spec in rows if rows is not None else _INTAKE_SEED:
            s.add(
                IntakeRequest(
                    request_number=f"{spec['request_number']}-{uuid.uuid4().hex[:6]}",
                    title=spec["title"],
                    request_type=spec["request_type"],
                    requester_user_id=uuid.uuid4(),
                    status=spec["status"],
                    vendor_name=spec["vendor_name"],
                    estimated_amount=Decimal("100.00"),
                    currency="USD",
                    organization_id=org_id,
                    entity_id=eid,
                )
            )
        await s.commit()


async def test_intake_summary_counts_the_whole_set_not_the_loaded_page(realdb):
    """`openCount` / `reviewCount` filtered the LOADED page by status while the
    "Requests" card beside them showed the server's whole-set total. 25 rows
    means the page holds 20 and the two cards must still agree."""
    await _seed_intake(realdb)

    async with realdb.client(key="a", role="ap_clerk") as c:
        listed = (await c.get("/api/intake")).json()
        body = (await c.get("/api/intake/summary")).json()

    assert len(listed["items"]) == DEFAULT_PAGE_SIZE
    assert listed["total"] == _OVER_A_PAGE
    assert body["total"] == _OVER_A_PAGE
    assert body["by_status"] == _tally(_INTAKE_SEED, "status")
    # A page-scoped tally would be short on whichever status trails the ordering.
    assert sum(body["by_status"].values()) > DEFAULT_PAGE_SIZE


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"status": "open"},
        {"status": "in_review"},
        {"type": "software"},
        {"type": "hardware"},
        {"search": "figma"},
        {"search": "racks"},
        {"status": "approved", "type": "services"},
        {"status": "open", "search": "figma"},
        # A term that matches rows the status filter excludes — the pairing that
        # is empty of meaning unless both legs really compose.
        {"status": "approved", "search": "racks"},
        {"search": "zzz-no-match"},
    ],
)
async def test_intake_summary_equals_an_independent_recount_under_every_filter(realdb, params):
    await _seed_intake(realdb)
    expected = _intake_expect(_INTAKE_SEED, **params)

    async with realdb.client(key="a", role="ap_clerk") as c:
        listed = (await c.get("/api/intake", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/intake/summary", params=params)).json()

    assert listed["total"] == len(expected), params
    assert body["total"] == len(expected), params
    assert body["by_status"] == _tally(expected, "status"), params


async def test_intake_summary_and_list_narrow_identically_on_x_entity_id(realdb):
    sub = await _extra_entity(realdb, slug="intake-sub")
    await _seed_intake(realdb)
    sub_rows = _INTAKE_SEED[:4]
    await _seed_intake(realdb, entity_id=sub, rows=sub_rows)

    async with realdb.client(key="a", role="ap_clerk") as c:
        scoped_list = (
            await c.get("/api/intake", params={"page_size": 100}, headers={"X-Entity-ID": str(sub)})
        ).json()
        scoped_sum = (await c.get("/api/intake/summary", headers={"X-Entity-ID": str(sub)})).json()

    assert scoped_list["total"] == len(sub_rows)
    assert scoped_sum["total"] == len(sub_rows)
    assert scoped_sum["by_status"] == _tally(sub_rows, "status")


async def test_intake_summary_is_a_well_formed_zero_on_an_empty_set(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        body = (await c.get("/api/intake/summary")).json()
    assert body == {"total": 0, "by_status": {}}


# ===========================================================================
# /api/positive-pay  — status counts + items exported + returns flagged (#349)
# ===========================================================================

_PP_SEED: list[dict] = (
    [
        {
            "file_type": "check_issue",
            "bank_format": "csv",
            "status": "generated",
            "item_count": 3 + i,
            "amount_mismatches": 0,
            "not_on_file": 0,
        }
        for i in range(10)
    ]
    + [
        {
            "file_type": "check_issue",
            "bank_format": "fixed_width",
            "status": "returned_processed",
            "item_count": 5,
            "amount_mismatches": 1,
            "not_on_file": 2,
        }
        for _ in range(7)
    ]
    + [
        {
            "file_type": "ach_authorization",
            "bank_format": "csv",
            "status": "generated",
            "item_count": 11,
            "amount_mismatches": 0,
            "not_on_file": 0,
        }
        for _ in range(8)
    ]
)


def _pp_expect(
    rows: list[dict],
    *,
    file_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
) -> list[dict]:
    kept = rows
    if file_type:
        kept = [r for r in kept if r["file_type"] == file_type]
    if status:
        kept = [r for r in kept if r["status"] == status]
    if search and search.strip():
        term = search.strip()
        kept = [
            r
            for r in kept
            if _contains(r["bank_format"], term)
            or _contains(r["file_type"], term)
            or _contains(str(r["id"]), term)
        ]
    return kept


async def _seed_positive_pay(realdb, key="a", *, entity_id=None, rows=None) -> list[dict]:
    """Insert `PositivePayFile` rows straight into the tenant DB.

    Deliberately not through `POST .../check-issue`: that needs a payment run,
    an executed cheque payment and a MinIO upload each, and this file needs 25
    rows to get past one page. The rollup reads the row's own columns + `meta`
    JSONB, which is exactly what is seeded here.
    """
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    specs = [dict(r) for r in (rows if rows is not None else _PP_SEED)]
    async with mk() as s:
        eid = entity_id if entity_id is not None else await _default_entity_id(s)
        for spec in specs:
            fid = uuid.uuid4()
            spec["id"] = fid
            s.add(
                PositivePayFile(
                    id=fid,
                    organization_id=org_id,
                    entity_id=eid,
                    file_type=spec["file_type"],
                    bank_format=spec["bank_format"],
                    status=spec["status"],
                    item_count=spec["item_count"],
                    total_amount=Decimal("1000.00"),
                    currency="USD",
                    content_hash="0" * 64,
                    meta={
                        "return_summary": {
                            "amount_mismatches": spec["amount_mismatches"],
                            "not_on_file": spec["not_on_file"],
                        }
                    }
                    if (spec["amount_mismatches"] or spec["not_on_file"])
                    else None,
                )
            )
        await s.commit()
    return specs


def _pp_assert(body: dict, expected: list[dict], label) -> None:
    assert body["total"] == len(expected), label
    assert body["by_status"] == _tally(expected, "status"), label
    assert body["items_exported"] == sum(r["item_count"] for r in expected), label
    assert body["returns_flagged"] == sum(
        r["amount_mismatches"] + r["not_on_file"] for r in expected
    ), label


async def test_positive_pay_summary_sums_the_whole_set_not_the_loaded_page(realdb):
    """`itemsExported` / `returnsFlagged` reduced over the LOADED page. With 25
    files the page holds 20, so a page-scoped `items_exported` is short by the
    tail's item counts."""
    seeded = await _seed_positive_pay(realdb)

    async with realdb.client(key="a", role="ap_manager") as c:
        listed = (await c.get("/api/positive-pay")).json()
        body = (await c.get("/api/positive-pay/summary")).json()

    assert len(listed["items"]) == DEFAULT_PAGE_SIZE
    assert listed["total"] == _OVER_A_PAGE
    _pp_assert(body, seeded, "unfiltered")
    # And it is genuinely more than one page's worth of items.
    assert body["items_exported"] > sum(r["item_count"] for r in seeded[:5])


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"file_type": "check_issue"},
        {"file_type": "ach_authorization"},
        {"status": "generated"},
        {"status": "returned_processed"},
        {"search": "fixed"},  # bank_format
        {"search": "ach"},  # file_type
        {"file_type": "check_issue", "status": "generated"},
        {"file_type": "ach_authorization", "search": "fixed"},  # composes to empty
    ],
)
async def test_positive_pay_summary_equals_an_independent_recount_under_every_filter(
    realdb, params
):
    seeded = await _seed_positive_pay(realdb)
    expected = _pp_expect(seeded, **params)

    async with realdb.client(key="a", role="ap_manager") as c:
        listed = (await c.get("/api/positive-pay", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/positive-pay/summary", params=params)).json()

    assert listed["total"] == len(expected), params
    _pp_assert(body, expected, params)


async def test_positive_pay_summary_search_matches_the_pasted_row_id(realdb):
    """The row's on-screen label is an 8-character prefix of the file id, so
    pasting it must find the row in the rollup as well as the table."""
    seeded = await _seed_positive_pay(realdb)
    target = seeded[0]
    prefix = str(target["id"])[:8]

    async with realdb.client(key="a", role="ap_manager") as c:
        listed = (await c.get("/api/positive-pay", params={"search": prefix})).json()
        body = (await c.get("/api/positive-pay/summary", params={"search": prefix})).json()

    expected = _pp_expect(seeded, search=prefix)
    assert [i["id"] for i in listed["items"]] == [str(r["id"]) for r in expected]
    _pp_assert(body, expected, prefix)


async def test_positive_pay_summary_and_list_narrow_identically_on_x_entity_id(realdb):
    sub = await _extra_entity(realdb, slug="pp-sub")
    await _seed_positive_pay(realdb)
    sub_rows = await _seed_positive_pay(realdb, entity_id=sub, rows=_PP_SEED[:3])

    async with realdb.client(key="a", role="ap_manager") as c:
        scoped_list = (
            await c.get(
                "/api/positive-pay",
                params={"page_size": 100},
                headers={"X-Entity-ID": str(sub)},
            )
        ).json()
        scoped_sum = (
            await c.get("/api/positive-pay/summary", headers={"X-Entity-ID": str(sub)})
        ).json()

    assert scoped_list["total"] == len(sub_rows)
    _pp_assert(scoped_sum, sub_rows, "entity-scoped")


async def test_positive_pay_summary_is_a_well_formed_zero_on_an_empty_set(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        body = (await c.get("/api/positive-pay/summary")).json()
    assert body == {
        "total": 0,
        "by_status": {},
        "items_exported": 0,
        "returns_flagged": 0,
    }


# ===========================================================================
# /api/recurring  — status counts + exact monthly-equivalent spend (#349)
# ===========================================================================

# Cadence divisors are 1 / 3 / 12. Amounts here divide exactly at those
# divisors, so the expectation is unambiguous; the exactness of the division
# itself gets its own dedicated tests below.
_REC_SEED: list[dict] = (
    [
        {
            "name": f"Acme Towers rent {i}",
            "vendor": "acme",
            "status": "active",
            "cadence": "monthly",
            "amount": "1200.00",
            "currency": "USD",
            "next_run_on": _TODAY + timedelta(days=10 + i),
        }
        for i in range(9)
    ]
    + [
        {
            "name": f"Globex support {i}",
            "vendor": "globex",
            "status": "active",
            "cadence": "quarterly",
            "amount": "1200.00",
            "currency": "EUR",
            "next_run_on": _TODAY + timedelta(days=3 + i),
        }
        for i in range(6)
    ]
    + [
        {
            "name": f"Globex licence {i}",
            "vendor": "globex",
            "status": "active",
            "cadence": "annual",
            "amount": "1200.00",
            "currency": "USD",
            "next_run_on": _TODAY + timedelta(days=40 + i),
        }
        for i in range(5)
    ]
    + [
        {
            "name": f"Paused acme feed {i}",
            "vendor": "acme",
            "status": "paused",
            "cadence": "monthly",
            "amount": "9999.00",
            "currency": "USD",
            "next_run_on": _TODAY + timedelta(days=1),
        }
        for i in range(3)
    ]
    + [
        {
            "name": f"Ended globex trial {i}",
            "vendor": "globex",
            "status": "ended",
            "cadence": "monthly",
            "amount": "5000.00",
            "currency": "EUR",
            "next_run_on": None,
        }
        for i in range(2)
    ]
)

_DIVISOR = {"monthly": 1, "quarterly": 3, "annual": 12}


def _rec_expect(
    rows: list[dict],
    *,
    status: str | None = None,
    vendor: str | None = None,
    search: str | None = None,
) -> list[dict]:
    kept = rows
    if status:
        kept = [r for r in kept if r["status"] == status]
    if vendor:
        kept = [r for r in kept if r["vendor"] == vendor]
    if search and search.strip():
        term = search.strip()
        kept = [r for r in kept if _contains(r["name"], term) or _contains(r["vendor_name"], term)]
    return kept


def _rec_monthly_expected(rows: list[dict]) -> dict[str, str]:
    """Per-currency monthly-equivalent, recomputed with exact Decimal math.

    Mirrors the endpoint's SEMANTICS, not its code: active templates with an
    amount only, `amount / divisor` summed exactly per currency, then a single
    ROUND_HALF_UP to 2dp. (`amount / divisor` here is exact by construction of
    the seed; the non-terminating case is covered separately.)
    """
    per: dict[str, Decimal] = {}
    for row in rows:
        if row["status"] != "active" or row["amount"] is None:
            continue
        code = row["currency"].upper()
        per[code] = per.get(code, Decimal("0")) + (
            Decimal(row["amount"]) / _DIVISOR[row["cadence"]]
        )
    return {k: str(v.quantize(_CENTS, rounding=ROUND_HALF_UP)) for k, v in per.items()}


async def _seed_recurring(realdb, key="a", *, entity_id=None, rows=None) -> list[dict]:
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    specs = [dict(r) for r in (rows if rows is not None else _REC_SEED)]
    vendor_names = {"acme": "Acme Towers LLC", "globex": "Globex Industrial"}
    async with mk() as s:
        eid = entity_id if entity_id is not None else await _default_entity_id(s)
        vendor_ids: dict[str, uuid.UUID] = {}
        for slug, name in vendor_names.items():
            vid = uuid.uuid4()
            vendor_ids[slug] = vid
            s.add(
                Vendor(
                    id=vid,
                    organization_id=org_id,
                    entity_id=eid,
                    name=f"{name} {uuid.uuid4().hex[:6]}",
                    status="active",
                )
            )
        await s.flush()
        for spec in specs:
            spec["vendor_id"] = vendor_ids[spec["vendor"]]
            spec["vendor_name"] = vendor_names[spec["vendor"]]
            s.add(
                RecurringInvoiceTemplate(
                    organization_id=org_id,
                    entity_id=eid,
                    name=spec["name"],
                    vendor_id=spec["vendor_id"],
                    vendor_name=spec["vendor_name"],
                    amount=Decimal(spec["amount"]) if spec["amount"] is not None else None,
                    currency=spec["currency"],
                    cadence=spec["cadence"],
                    day_of_period=1,
                    start_date=_TODAY,
                    next_run_on=spec["next_run_on"],
                    status=spec["status"],
                )
            )
        await s.commit()
    return specs


def _rec_soonest(rows: list[dict]) -> str | None:
    dates = [r["next_run_on"] for r in rows if r["status"] == "active" and r["next_run_on"]]
    return min(dates).isoformat() if dates else None


async def test_recurring_summary_covers_the_whole_set_not_the_loaded_page(realdb):
    """`activeCount`, `soonestNextRun` and `monthlyRecurringTotal` were all
    derived from the LOADED page, so they contradicted the whole-set footer."""
    seeded = await _seed_recurring(realdb)

    async with realdb.client(key="a", role="ap_clerk") as c:
        listed = (await c.get("/api/recurring")).json()
        body = (await c.get("/api/recurring/summary")).json()

    assert len(listed["items"]) == DEFAULT_PAGE_SIZE
    assert listed["total"] == _OVER_A_PAGE
    assert body["total"] == _OVER_A_PAGE
    assert body["by_status"] == _tally(seeded, "status")
    assert body["soonest_next_run"] == _rec_soonest(seeded)

    expected = _rec_monthly_expected(seeded)
    got = {r["currency"]: r["total"] for r in body["monthly_equivalent"]}
    assert got == expected
    # Grouped by currency code, ordered by code (load-bearing: the page
    # headlines the first group and puts the rest on a sub-line).
    assert [r["currency"] for r in body["monthly_equivalent"]] == sorted(expected)


@pytest.mark.parametrize(
    "params, expect_kwargs",
    [
        ({}, {}),
        ({"status": "active"}, {"status": "active"}),
        ({"status": "paused"}, {"status": "paused"}),
        ({"status": "ended"}, {"status": "ended"}),
        ({"search": "acme"}, {"search": "acme"}),
        ({"search": "globex"}, {"search": "globex"}),
        ({"search": "licence"}, {"search": "licence"}),
        ({"status": "active", "search": "globex"}, {"status": "active", "search": "globex"}),
        ({"search": "zzz-no-match"}, {"search": "zzz-no-match"}),
    ],
)
async def test_recurring_summary_equals_an_independent_recount_under_every_filter(
    realdb, params, expect_kwargs
):
    seeded = await _seed_recurring(realdb)
    expected = _rec_expect(seeded, **expect_kwargs)

    async with realdb.client(key="a", role="ap_clerk") as c:
        listed = (await c.get("/api/recurring", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/recurring/summary", params=params)).json()

    assert listed["total"] == len(expected), params
    assert body["total"] == len(expected), params
    assert body["by_status"] == _tally(expected, "status"), params
    assert {r["currency"]: r["total"] for r in body["monthly_equivalent"]} == (
        _rec_monthly_expected(expected)
    ), params
    assert body["soonest_next_run"] == _rec_soonest(expected), params


async def test_recurring_summary_honours_the_vendor_id_filter(realdb):
    """`vendor_id` is declared as a plain default (`vendor_id: uuid|None = None`),
    not a `Query(...)` — the exact shape that made the previous structural guard
    pass vacuously on this surface. So it gets a behavioural assertion."""
    seeded = await _seed_recurring(realdb)
    globex_id = next(r["vendor_id"] for r in seeded if r["vendor"] == "globex")
    expected = _rec_expect(seeded, vendor="globex")

    async with realdb.client(key="a", role="ap_clerk") as c:
        params = {"vendor_id": str(globex_id)}
        listed = (await c.get("/api/recurring", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/recurring/summary", params=params)).json()

    assert listed["total"] == len(expected)
    assert body["total"] == len(expected)
    assert body["by_status"] == _tally(expected, "status")
    assert {r["currency"]: r["total"] for r in body["monthly_equivalent"]} == (
        _rec_monthly_expected(expected)
    )
    # And it really narrowed — the other vendor's templates are excluded.
    assert body["total"] < len(seeded)


async def test_recurring_monthly_equivalent_normalises_each_cadence_exactly(realdb):
    """1200.00 at monthly / quarterly / annual is 1200 / 400 / 100 a month.

    One template per cadence, one currency each, so a wrong divisor cannot be
    hidden by a coincidental sum.
    """
    rows = [
        {
            "name": "Monthly only",
            "vendor": "acme",
            "status": "active",
            "cadence": "monthly",
            "amount": "1200.00",
            "currency": "USD",
            "next_run_on": _TODAY,
        },
        {
            "name": "Quarterly only",
            "vendor": "acme",
            "status": "active",
            "cadence": "quarterly",
            "amount": "1200.00",
            "currency": "EUR",
            "next_run_on": _TODAY,
        },
        {
            "name": "Annual only",
            "vendor": "acme",
            "status": "active",
            "cadence": "annual",
            "amount": "1200.00",
            "currency": "GBP",
            "next_run_on": _TODAY,
        },
    ]
    await _seed_recurring(realdb, rows=rows)

    async with realdb.client(key="a", role="ap_clerk") as c:
        body = (await c.get("/api/recurring/summary")).json()

    got = {r["currency"]: r["total"] for r in body["monthly_equivalent"]}
    assert got == {"USD": "1200.00", "EUR": "400.00", "GBP": "100.00"}


async def test_recurring_monthly_equivalent_rounds_half_up_not_to_even(realdb):
    """96.30 / 12 == 8.025 exactly. ROUND_HALF_UP gives 8.03; Python's default
    round() (half-to-even) gives 8.02 and a truncating divide gives 8.02. The
    two are distinguishable only on a boundary like this one."""
    await _seed_recurring(
        realdb,
        rows=[
            {
                "name": "Boundary annual",
                "vendor": "acme",
                "status": "active",
                "cadence": "annual",
                "amount": "96.30",
                "currency": "USD",
                "next_run_on": _TODAY,
            }
        ],
    )

    async with realdb.client(key="a", role="ap_clerk") as c:
        body = (await c.get("/api/recurring/summary")).json()

    assert [r["total"] for r in body["monthly_equivalent"]] == ["8.03"]


async def test_recurring_monthly_equivalent_keeps_a_repeating_quotient_exact(realdb):
    """100.00 / 12 is non-terminating. The division stays in Postgres numeric
    and the SUM is rounded ONCE at the end, so three such templates report
    25.00 (3 x 8.333... == 25 exactly), not 24.99 (3 x 8.33, a per-template
    round) and not a float artefact.

    NOTE: `app/api/recurring.py::template_summary`'s docstring and
    `backend/docs/recurring-invoices.md` both describe the quantisation as
    happening "per template"; the implementation quantises the per-currency SUM.
    This test pins the implemented (and more accurate) behaviour — the prose is
    what is out of date.
    """
    await _seed_recurring(
        realdb,
        rows=[
            {
                "name": f"Repeating annual {i}",
                "vendor": "acme",
                "status": "active",
                "cadence": "annual",
                "amount": "100.00",
                "currency": "USD",
                "next_run_on": _TODAY,
            }
            for i in range(3)
        ],
    )

    async with realdb.client(key="a", role="ap_clerk") as c:
        body = (await c.get("/api/recurring/summary")).json()

    totals = [r["total"] for r in body["monthly_equivalent"]]
    assert totals == ["25.00"]
    assert totals != ["24.99"]  # per-template rounding
    assert isinstance(body["monthly_equivalent"][0]["total"], str)


async def test_recurring_monthly_equivalent_excludes_inactive_and_amountless(realdb):
    """Only ACTIVE templates carrying an amount contribute to the monthly
    figure — a paused template is not committed spend, and an amountless one
    has no figure to normalise. The status COUNTS still include them."""
    rows = [
        {
            "name": "Active with amount",
            "vendor": "acme",
            "status": "active",
            "cadence": "monthly",
            "amount": "100.00",
            "currency": "USD",
            "next_run_on": _TODAY,
        },
        {
            "name": "Paused with amount",
            "vendor": "acme",
            "status": "paused",
            "cadence": "monthly",
            "amount": "500.00",
            "currency": "USD",
            "next_run_on": _TODAY,
        },
        {
            "name": "Active without amount",
            "vendor": "acme",
            "status": "active",
            "cadence": "monthly",
            "amount": None,
            "currency": "USD",
            "next_run_on": _TODAY,
        },
    ]
    await _seed_recurring(realdb, rows=rows)

    async with realdb.client(key="a", role="ap_clerk") as c:
        body = (await c.get("/api/recurring/summary")).json()

    assert body["total"] == 3
    assert body["by_status"] == {"active": 2, "paused": 1}
    assert [r["total"] for r in body["monthly_equivalent"]] == ["100.00"]
    assert body["monthly_equivalent"][0]["count"] == 1


async def test_recurring_summary_and_list_narrow_identically_on_x_entity_id(realdb):
    sub = await _extra_entity(realdb, slug="rec-sub")
    await _seed_recurring(realdb)
    sub_rows = await _seed_recurring(realdb, entity_id=sub, rows=_REC_SEED[:2])

    async with realdb.client(key="a", role="ap_clerk") as c:
        scoped_list = (
            await c.get(
                "/api/recurring", params={"page_size": 100}, headers={"X-Entity-ID": str(sub)}
            )
        ).json()
        scoped_sum = (
            await c.get("/api/recurring/summary", headers={"X-Entity-ID": str(sub)})
        ).json()

    assert scoped_list["total"] == len(sub_rows)
    assert scoped_sum["total"] == len(sub_rows)
    assert {r["currency"]: r["total"] for r in scoped_sum["monthly_equivalent"]} == (
        _rec_monthly_expected(sub_rows)
    )


async def test_recurring_summary_is_a_well_formed_zero_on_an_empty_set(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        body = (await c.get("/api/recurring/summary")).json()
    assert body == {
        "total": 0,
        "by_status": {},
        "monthly_equivalent": [],
        "soonest_next_run": None,
    }


# ===========================================================================
# /api/requisitions  — status counts + per-currency value totals (#349)
# ===========================================================================

_REQ_SEED: list[dict] = (
    [
        {
            "requisition_number": f"REQ-D{i}",
            "title": f"Laptop refresh {i}",
            "department": "Engineering",
            "status": "draft",
            "total": f"{500 + i}.25",
            "currency": "USD",
        }
        for i in range(10)
    ]
    + [
        {
            "requisition_number": f"REQ-P{i}",
            "title": f"Warehouse shelving {i}",
            "department": "Operations",
            "status": "pending_approval",
            "total": f"{700 + i}.75",
            "currency": "EUR",
        }
        for i in range(8)
    ]
    + [
        {
            "requisition_number": f"REQ-A{i}",
            "title": f"Consulting retainer {i}",
            "department": "Finance",
            "status": "approved",
            "total": f"{900 + i}.50",
            "currency": "USD",
        }
        for i in range(7)
    ]
)


def _req_expect(
    rows: list[dict], *, status: str | None = None, search: str | None = None
) -> list[dict]:
    kept = rows
    if status:
        kept = [r for r in kept if r["status"] == status]
    if search and search.strip():
        term = search.strip()
        kept = [
            r
            for r in kept
            if _contains(r["requisition_number"], term)
            or _contains(r["title"], term)
            or _contains(r["department"], term)
        ]
    return kept


async def _seed_requisitions(realdb, key="a", *, entity_id=None, rows=None) -> list[dict]:
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    specs = [dict(r) for r in (rows if rows is not None else _REQ_SEED)]
    suffix = uuid.uuid4().hex[:6]
    async with mk() as s:
        eid = entity_id if entity_id is not None else await _default_entity_id(s)
        for spec in specs:
            spec["requisition_number"] = f"{spec['requisition_number']}-{suffix}"
            s.add(
                PurchaseRequisition(
                    requisition_number=spec["requisition_number"],
                    title=spec["title"],
                    department=spec["department"],
                    requester_user_id=uuid.uuid4(),
                    status=spec["status"],
                    total=Decimal(spec["total"]),
                    currency=spec["currency"],
                    organization_id=org_id,
                    entity_id=eid,
                )
            )
        await s.commit()
    return specs


async def test_requisition_summary_covers_the_whole_set_not_the_loaded_page(realdb):
    """`pendingCount` filtered the LOADED page for `pending_approval` and
    `periodTotal` summed it (across currencies), beside a whole-set count."""
    seeded = await _seed_requisitions(realdb)

    async with realdb.client(key="a", role="ap_clerk") as c:
        listed = (await c.get("/api/requisitions")).json()
        body = (await c.get("/api/requisitions/summary")).json()

    assert len(listed["items"]) == DEFAULT_PAGE_SIZE
    assert listed["total"] == _OVER_A_PAGE
    assert body["total"] == _OVER_A_PAGE
    assert body["by_status"] == _tally(seeded, "status")

    expected = _money_by_currency(seeded, amount_key="total")
    got = _by_currency(body)
    assert set(got) == set(expected)
    for code, total in expected.items():
        assert got[code]["total"] == str(total), code
    # `pending_approval` really does run past the page window in this seed.
    assert body["by_status"]["pending_approval"] == 8


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"status": "draft"},
        {"status": "pending_approval"},
        {"status": "approved"},
        {"search": "shelving"},
        {"search": "engineering"},  # department, not the title
        {"search": "REQ-A"},  # requisition number prefix
        {"status": "approved", "search": "finance"},
        {"status": "draft", "search": "shelving"},  # composes to empty
        {"search": "zzz-no-match"},
    ],
)
async def test_requisition_summary_equals_an_independent_recount_under_every_filter(realdb, params):
    seeded = await _seed_requisitions(realdb)
    expected = _req_expect(seeded, **params)
    expected_money = _money_by_currency(expected, amount_key="total")

    async with realdb.client(key="a", role="ap_clerk") as c:
        listed = (await c.get("/api/requisitions", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/requisitions/summary", params=params)).json()

    assert listed["total"] == len(expected), params
    assert body["total"] == len(expected), params
    assert body["by_status"] == _tally(expected, "status"), params
    got = _by_currency(body)
    assert set(got) == set(expected_money), params
    for code, total in expected_money.items():
        assert got[code]["total"] == str(total), (params, code)


async def test_requisition_summary_never_blends_two_currencies(realdb):
    seeded = await _seed_requisitions(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        body = (await c.get("/api/requisitions/summary")).json()

    got = _by_currency(body)
    assert set(got) == {"USD", "EUR"}
    # `periodTotal` used to add every row's value together regardless of
    # currency and stamp the org default on it. That figure appears nowhere.
    blended = sum(Decimal(r["total"]) for r in seeded)
    assert all(Decimal(r["total"]) != blended for r in body["by_currency"])
    for row in body["by_currency"]:
        assert isinstance(row["total"], str)


async def test_requisition_summary_and_list_narrow_identically_on_x_entity_id(realdb):
    sub = await _extra_entity(realdb, slug="req-sub")
    await _seed_requisitions(realdb)
    sub_rows = await _seed_requisitions(realdb, entity_id=sub, rows=_REQ_SEED[:5])

    async with realdb.client(key="a", role="ap_clerk") as c:
        scoped_list = (
            await c.get(
                "/api/requisitions", params={"page_size": 100}, headers={"X-Entity-ID": str(sub)}
            )
        ).json()
        scoped_sum = (
            await c.get("/api/requisitions/summary", headers={"X-Entity-ID": str(sub)})
        ).json()

    assert scoped_list["total"] == len(sub_rows)
    assert scoped_sum["total"] == len(sub_rows)
    assert _by_currency(scoped_sum)["USD"]["total"] == str(
        _money_by_currency(sub_rows, amount_key="total")["USD"]
    )


async def test_requisition_summary_is_a_well_formed_zero_on_an_empty_set(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        body = (await c.get("/api/requisitions/summary")).json()
    assert body == {"total": 0, "by_status": {}, "by_currency": []}


# ===========================================================================
# /api/vendor-statements  — status counts + open discrepancies (#349)
# ===========================================================================

_VSR_SEED: list[dict] = (
    [
        {
            "vendor": "acme",
            "vendor_name": "Acme Supplies",
            "statement_reference": f"ACME-STMT-{i}",
            "status": "open",
            "amount_mismatch_count": 1,
            "missing_our_side_count": 2,
            "missing_their_side_count": 0,
        }
        for i in range(12)
    ]
    + [
        {
            "vendor": "globex",
            "vendor_name": "Globex Industrial",
            "statement_reference": f"GLBX-STMT-{i}",
            "status": "open",
            "amount_mismatch_count": 0,
            "missing_our_side_count": 1,
            "missing_their_side_count": 3,
        }
        for i in range(8)
    ]
    + [
        {
            "vendor": "globex",
            "vendor_name": "Globex Industrial",
            "statement_reference": f"GLBX-DONE-{i}",
            "status": "resolved",
            "amount_mismatch_count": 0,
            "missing_our_side_count": 0,
            "missing_their_side_count": 0,
        }
        for i in range(5)
    ]
)


def _vsr_expect(
    rows: list[dict],
    *,
    vendor: str | None = None,
    status: str | None = None,
    search: str | None = None,
) -> list[dict]:
    kept = rows
    if vendor:
        kept = [r for r in kept if r["vendor"] == vendor]
    if status:
        kept = [r for r in kept if r["status"] == status]
    if search and search.strip():
        term = search.strip()
        kept = [
            r
            for r in kept
            if _contains(r["vendor_name"], term) or _contains(r["statement_reference"], term)
        ]
    return kept


def _vsr_discrepancies(rows: list[dict]) -> int:
    """The discrepancy count the summary endpoint is expected to report.

    `missing_their_side_count` is deliberately NOT summed. The endpoint counts
    live line rows whose classification is in `_ACTIONABLE_CLASSES`
    (`amount_mismatch`, `missing_on_our_side`) and which are still unresolved —
    the same population `_recompute_run_status` and `close-readiness` use. An
    invoice on our ledger the supplier has not billed is not something a clerk
    chases on this screen, so counting it here would make this KPI the only
    reader of a third population.
    """
    return sum(r["amount_mismatch_count"] + r["missing_our_side_count"] for r in rows)


async def _seed_vendor_statements(realdb, key="a", *, entity_id=None, rows=None) -> list[dict]:
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    specs = [dict(r) for r in (rows if rows is not None else _VSR_SEED)]
    async with mk() as s:
        eid = entity_id if entity_id is not None else await _default_entity_id(s)
        vendor_ids: dict[str, uuid.UUID] = {}
        for slug in {r["vendor"] for r in specs}:
            vid = uuid.uuid4()
            vendor_ids[slug] = vid
            s.add(
                Vendor(
                    id=vid,
                    organization_id=org_id,
                    entity_id=eid,
                    name=f"{slug}-{uuid.uuid4().hex[:6]}",
                    status="active",
                )
            )
        await s.flush()
        for spec in specs:
            spec["vendor_id"] = vendor_ids[spec["vendor"]]
            run = VendorStatementReconciliation(
                organization_id=org_id,
                entity_id=eid,
                vendor_id=spec["vendor_id"],
                vendor_name=spec["vendor_name"],
                statement_date=_TODAY,
                statement_reference=spec["statement_reference"],
                currency="USD",
                status=spec["status"],
                line_count=5,
                matched_count=2,
                amount_mismatch_count=spec["amount_mismatch_count"],
                missing_our_side_count=spec["missing_our_side_count"],
                missing_their_side_count=spec["missing_their_side_count"],
            )
            s.add(run)
            await s.flush()
            # The per-run counters above are stamped once at import and are
            # never updated when a line is resolved, which is exactly why
            # `GET /summary` stopped reading them. It counts the LIVE lines, so
            # a fixture that sets only the counters describes a run with no
            # lines at all and the endpoint correctly answers 0. Seed the rows
            # the counters claim, one per counted discrepancy, so the two halves
            # of the fixture agree and the parity this file exists to assert is
            # about filtering rather than about a stale column.
            for classification, count in (
                (CLASS_AMOUNT_MISMATCH, spec["amount_mismatch_count"]),
                (CLASS_MISSING_OUR_SIDE, spec["missing_our_side_count"]),
                (CLASS_MISSING_THEIR_SIDE, spec["missing_their_side_count"]),
            ):
                for _ in range(count):
                    s.add(
                        VendorStatementReconLine(
                            reconciliation_id=run.id,
                            organization_id=org_id,
                            entity_id=eid,
                            classification=classification,
                            resolution_status=RESOLUTION_UNRESOLVED,
                        )
                    )
        await s.commit()
    return specs


async def test_vendor_statement_summary_covers_the_whole_set_not_the_loaded_page(realdb):
    """`openCount` filtered the LOADED page and `totalDiscrepancies` reduced the
    per-run counts over it, under a "Showing all N" footer."""
    seeded = await _seed_vendor_statements(realdb)

    async with realdb.client(key="a", role="ap_manager") as c:
        listed = (await c.get("/api/vendor-statements")).json()
        body = (await c.get("/api/vendor-statements/summary")).json()

    assert len(listed["items"]) == DEFAULT_PAGE_SIZE
    assert listed["total"] == _OVER_A_PAGE
    assert body["total"] == _OVER_A_PAGE
    assert body["by_status"] == _tally(seeded, "status")
    assert body["open_discrepancies"] == _vsr_discrepancies(seeded)
    # A page-scoped reduce would have stopped at 20 rows' worth.
    assert body["open_discrepancies"] > _vsr_discrepancies(seeded[: DEFAULT_PAGE_SIZE - 5])


@pytest.mark.parametrize(
    "params, expect_kwargs",
    [
        ({}, {}),
        ({"status": "open"}, {"status": "open"}),
        ({"status": "resolved"}, {"status": "resolved"}),
        ({"search": "acme"}, {"search": "acme"}),
        ({"search": "GLBX-DONE"}, {"search": "GLBX-DONE"}),
        ({"status": "open", "search": "globex"}, {"status": "open", "search": "globex"}),
        ({"status": "resolved", "search": "acme"}, {"status": "resolved", "search": "acme"}),
        ({"search": "zzz-no-match"}, {"search": "zzz-no-match"}),
    ],
)
async def test_vendor_statement_summary_equals_an_independent_recount(
    realdb, params, expect_kwargs
):
    seeded = await _seed_vendor_statements(realdb)
    expected = _vsr_expect(seeded, **expect_kwargs)

    async with realdb.client(key="a", role="ap_manager") as c:
        listed = (await c.get("/api/vendor-statements", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/vendor-statements/summary", params=params)).json()

    assert listed["total"] == len(expected), params
    assert body["total"] == len(expected), params
    assert body["by_status"] == _tally(expected, "status"), params
    assert body["open_discrepancies"] == _vsr_discrepancies(expected), params


async def test_vendor_statement_summary_honours_the_vendor_id_filter(realdb):
    seeded = await _seed_vendor_statements(realdb)
    acme_id = next(r["vendor_id"] for r in seeded if r["vendor"] == "acme")
    expected = _vsr_expect(seeded, vendor="acme")

    async with realdb.client(key="a", role="ap_manager") as c:
        params = {"vendor_id": str(acme_id)}
        listed = (await c.get("/api/vendor-statements", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/vendor-statements/summary", params=params)).json()

    assert listed["total"] == len(expected)
    assert body["total"] == len(expected)
    assert body["by_status"] == _tally(expected, "status")
    assert body["open_discrepancies"] == _vsr_discrepancies(expected)
    assert body["total"] < len(seeded)


async def test_vendor_statement_summary_and_list_narrow_identically_on_x_entity_id(realdb):
    sub = await _extra_entity(realdb, slug="vsr-sub")
    await _seed_vendor_statements(realdb)
    sub_rows = await _seed_vendor_statements(realdb, entity_id=sub, rows=_VSR_SEED[:3])

    async with realdb.client(key="a", role="ap_manager") as c:
        scoped_list = (
            await c.get(
                "/api/vendor-statements",
                params={"page_size": 100},
                headers={"X-Entity-ID": str(sub)},
            )
        ).json()
        scoped_sum = (
            await c.get("/api/vendor-statements/summary", headers={"X-Entity-ID": str(sub)})
        ).json()

    assert scoped_list["total"] == len(sub_rows)
    assert scoped_sum["total"] == len(sub_rows)
    assert scoped_sum["open_discrepancies"] == _vsr_discrepancies(sub_rows)


async def test_vendor_statement_summary_is_a_well_formed_zero_on_an_empty_set(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        body = (await c.get("/api/vendor-statements/summary")).json()
    assert body == {"total": 0, "by_status": {}, "open_discrepancies": 0}


# ===========================================================================
# /api/invoices/counts  — every population filter, never `status` (PR #352)
# ===========================================================================

_INV_SEED: list[dict] = (
    [
        {
            "invoice_number": f"GLB-{i:03d}",
            "vendor_name": "Globex Corp",
            "po_number": f"PO-GLB-{i}",
            "description": "Quarterly hosting",
            "amount": f"{100 + i}.00",
            "status": "new",
            "due_offset": 10 + i,
        }
        for i in range(12)
    ]
    + [
        {
            "invoice_number": f"INI-{i:03d}",
            "vendor_name": "Initech LLC",
            "po_number": f"PO-INI-{i}",
            "description": "Consulting hours",
            "amount": f"{1000 + i}.00",
            "status": "approved",
            "due_offset": 40 + i,
        }
        for i in range(11)
    ]
    + [
        {
            "invoice_number": f"UMB-{i:03d}",
            "vendor_name": "Umbrella Inc",
            "po_number": None,
            "description": "Lab supplies",
            "amount": f"{5000 + i}.00",
            "status": "paid",
            "due_offset": 80 + i,
        }
        for i in range(7)
    ]
)


def _inv_expect(rows: list[dict], **f) -> list[dict]:
    """The list's population predicates, re-implemented. `status` is
    deliberately NOT accepted here — it is the tallied dimension."""
    kept = rows
    if f.get("vendor"):
        kept = [r for r in kept if _contains(r["vendor_name"], f["vendor"])]
    if f.get("invoice_number"):
        kept = [r for r in kept if _contains(r["invoice_number"], f["invoice_number"])]
    if f.get("po_number"):
        kept = [r for r in kept if _contains(r["po_number"], f["po_number"])]
    if f.get("description"):
        kept = [r for r in kept if _contains(r["description"], f["description"])]
    if f.get("amount_min") is not None:
        kept = [r for r in kept if Decimal(r["amount"]) >= Decimal(str(f["amount_min"]))]
    if f.get("amount_max") is not None:
        kept = [r for r in kept if Decimal(r["amount"]) <= Decimal(str(f["amount_max"]))]
    if f.get("due_date_from"):
        kept = [r for r in kept if r["due_date"] >= f["due_date_from"]]
    if f.get("due_date_to"):
        kept = [r for r in kept if r["due_date"] <= f["due_date_to"]]
    if f.get("search"):
        term = f["search"]
        kept = [
            r
            for r in kept
            if _contains(r["vendor_name"], term)
            or _contains(r["invoice_number"], term)
            or _contains(r["po_number"], term)
            or _contains(r["description"], term)
        ]
    if f.get("assigned_to_id"):
        kept = [r for r in kept if r.get("assigned_to_id") == f["assigned_to_id"]]
    return kept


async def _seed_invoices(realdb, key="a", *, entity_id=None, rows=None, assignee=None):
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    specs = [dict(r) for r in (rows if rows is not None else _INV_SEED)]
    suffix = uuid.uuid4().hex[:6]
    async with mk() as s:
        eid = entity_id if entity_id is not None else await _default_entity_id(s)
        for idx, spec in enumerate(specs):
            spec["invoice_number"] = f"{spec['invoice_number']}-{suffix}"
            spec["due_date"] = _TODAY + timedelta(days=spec["due_offset"])
            # Assign a known slice so `assigned_to_id` is a real discriminator.
            spec["assigned_to_id"] = str(assignee) if (assignee and idx % 5 == 0) else None
            s.add(
                Invoice(
                    organization_id=org_id,
                    entity_id=eid,
                    invoice_number=spec["invoice_number"],
                    vendor_name=spec["vendor_name"],
                    po_number=spec["po_number"],
                    description=spec["description"],
                    amount=Decimal(spec["amount"]),
                    currency="USD",
                    invoice_date=_TODAY,
                    due_date=spec["due_date"],
                    status=InvoiceStatus(spec["status"]),
                    assigned_to_id=uuid.UUID(spec["assigned_to_id"])
                    if spec["assigned_to_id"]
                    else None,
                )
            )
        await s.commit()
    return specs


async def test_invoice_counts_tally_the_whole_tenant_not_one_page(realdb):
    seeded = await _seed_invoices(realdb)

    async with realdb.client(key="a") as c:
        listed = (await c.get("/api/invoices")).json()
        body = (await c.get("/api/invoices/counts")).json()

    assert len(listed["items"]) == DEFAULT_PAGE_SIZE
    assert listed["total"] == len(seeded) == 30
    assert body["counts"] == _tally(seeded, "status")
    assert body["total"] == len(seeded)
    # Every chip is bigger than a page-1 client-side tally could have produced
    # for at least one status.
    assert body["total"] > DEFAULT_PAGE_SIZE


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"search": "globex"},
        {"search": "consulting"},  # description leg
        {"search": "PO-INI"},  # po_number leg
        {"vendor": "initech"},
        {"invoice_number": "UMB-"},
        {"po_number": "PO-GLB"},
        {"description": "lab supplies"},
        {"amount_min": 900},
        {"amount_max": 900},
        {"amount_min": 900, "amount_max": 2000},
        {"vendor": "globex", "amount_min": 105},
        {"search": "zzz-no-match"},
    ],
)
async def test_invoice_counts_honour_every_population_filter(realdb, params):
    seeded = await _seed_invoices(realdb)
    expected = _inv_expect(seeded, **params)

    async with realdb.client(key="a") as c:
        listed = (await c.get("/api/invoices", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/invoices/counts", params=params)).json()

    assert listed["total"] == len(expected), params
    assert body["total"] == len(expected), params
    assert body["counts"] == _tally(expected, "status"), params


async def test_invoice_counts_honour_the_due_date_window(realdb):
    seeded = await _seed_invoices(realdb)
    frm = (_TODAY + timedelta(days=40)).isoformat()
    to = (_TODAY + timedelta(days=60)).isoformat()
    expected = _inv_expect(
        seeded,
        due_date_from=date.fromisoformat(frm),
        due_date_to=date.fromisoformat(to),
    )

    async with realdb.client(key="a") as c:
        params = {"due_date_from": frm, "due_date_to": to}
        listed = (await c.get("/api/invoices", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/invoices/counts", params=params)).json()

    assert listed["total"] == len(expected)
    assert body["counts"] == _tally(expected, "status")
    assert body["total"] == len(expected)
    assert 0 < len(expected) < len(seeded)  # the window really bit


async def test_invoice_counts_honour_assigned_to_id(realdb):
    """The "My Approvals" quick view is `assigned_to_id=<me>`; the chips have to
    follow it or the badge counts the whole tenant's queue."""
    assignee = uuid.uuid4()
    seeded = await _seed_invoices(realdb, assignee=assignee)
    expected = _inv_expect(seeded, assigned_to_id=str(assignee))
    assert 0 < len(expected) < len(seeded)

    async with realdb.client(key="a") as c:
        params = {"assigned_to_id": str(assignee)}
        listed = (await c.get("/api/invoices", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/invoices/counts", params=params)).json()

    assert listed["total"] == len(expected)
    assert body["total"] == len(expected)
    assert body["counts"] == _tally(expected, "status")


@pytest.mark.parametrize("status", ["new", "approved", "paid", "new,approved"])
async def test_invoice_counts_ignore_a_status_param_but_keep_the_rest(realdb, status):
    """`status` is the dimension being tallied: applying it would zero every
    other chip. It must be ignored EVEN WHEN it rides alongside a real
    population filter — the inline-chip toggle reuses the page's param builder,
    so both arrive together."""
    seeded = await _seed_invoices(realdb)
    unfiltered = _tally(seeded, "status")
    searched = _tally(_inv_expect(seeded, search="globex"), "status")

    async with realdb.client(key="a") as c:
        with_status = (await c.get("/api/invoices/counts", params={"status": status})).json()
        both = (
            await c.get("/api/invoices/counts", params={"status": status, "search": "globex"})
        ).json()

    assert with_status["counts"] == unfiltered
    assert with_status["total"] == len(seeded)
    # `search` still applies; `status` still does not.
    assert both["counts"] == searched


async def test_invoice_counts_narrow_with_x_entity_id_exactly_like_the_list(realdb):
    sub = await _extra_entity(realdb, slug="inv-sub")
    await _seed_invoices(realdb)
    sub_rows = await _seed_invoices(realdb, entity_id=sub, rows=_INV_SEED[:6])

    async with realdb.client(key="a") as c:
        scoped_list = (
            await c.get(
                "/api/invoices", params={"page_size": 100}, headers={"X-Entity-ID": str(sub)}
            )
        ).json()
        scoped_counts = (
            await c.get("/api/invoices/counts", headers={"X-Entity-ID": str(sub)})
        ).json()

    assert scoped_list["total"] == len(sub_rows)
    assert scoped_counts["total"] == len(sub_rows)
    assert scoped_counts["counts"] == _tally(sub_rows, "status")


async def test_invoice_counts_search_is_literal_not_a_wildcard(realdb):
    """The chips inherit the shared builder's LIKE-metacharacter escaping, so a
    `%` in the box is text. A widened chip count over a narrow table is the
    same lie the filter parity fix removed."""
    await _seed_invoices(
        realdb,
        rows=[
            {
                "invoice_number": "PCT-1",
                "vendor_name": "Acme Freight",
                "po_number": None,
                "description": "Fuel surcharge 50% of base",
                "amount": "120.00",
                "status": "new",
                "due_offset": 5,
            },
            {
                "invoice_number": "PCT-2",
                "vendor_name": "Beta Haulage",
                "po_number": None,
                "description": "Flat rate",
                "amount": "130.00",
                "status": "new",
                "due_offset": 6,
            },
        ],
    )

    async with realdb.client(key="a") as c:
        body = (await c.get("/api/invoices/counts", params={"search": "%"})).json()
        listed = (await c.get("/api/invoices", params={"search": "%"})).json()

    assert body["total"] == 1
    assert listed["total"] == 1


# ===========================================================================
# /api/expenses  — list + summary + export all one filtered set (#349, #355)
# ===========================================================================

_EXP_SEED: list[dict] = (
    [
        {
            "merchant": f"Skyline Hotels {i}",
            "category": "lodging",
            "description": "Client visit",
            "amount": f"{300 + i}.10",
            "currency": "USD",
            "status": "draft",
            "on_report": False,
        }
        for i in range(10)
    ]
    + [
        {
            "merchant": f"Deutsche Bahn {i}",
            "category": "travel",
            "description": "Rail transfer",
            "amount": f"{40 + i}.55",
            "currency": "EUR",
            "status": "submitted",
            "on_report": False,
        }
        for i in range(9)
    ]
    + [
        {
            "merchant": f"Corner Deli {i}",
            "category": "meals",
            "description": "Team lunch",
            "amount": f"{20 + i}.99",
            "currency": "USD",
            "status": "approved",
            "on_report": True,
        }
        for i in range(6)
    ]
)


def _exp_expect(
    rows: list[dict],
    *,
    status: str | None = None,
    search: str | None = None,
    on_report: bool | None = None,
) -> list[dict]:
    kept = rows
    if status:
        kept = [r for r in kept if r["status"] == status]
    if on_report is not None:
        kept = [r for r in kept if r["on_report"] is on_report]
    if search and search.strip():
        term = search.strip()
        kept = [
            r
            for r in kept
            if _contains(r["merchant"], term)
            or _contains(r["description"], term)
            or _contains(r["category"], term)
        ]
    return kept


async def _seed_expenses(realdb, key="a", *, entity_id=None, rows=None):
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    specs = [dict(r) for r in (rows if rows is not None else _EXP_SEED)]
    suffix = uuid.uuid4().hex[:6]
    report_id = uuid.uuid4()
    async with mk() as s:
        eid = entity_id if entity_id is not None else await _default_entity_id(s)
        s.add(
            ExpenseReport(
                id=report_id,
                report_number=f"EXR-{suffix}",
                title="Q2 travel",
                employee_user_id=uuid.uuid4(),
                status="draft",
                total_amount=Decimal("0"),
                currency="USD",
                organization_id=org_id,
                entity_id=eid,
            )
        )
        await s.flush()
        for spec in specs:
            spec["merchant"] = f"{spec['merchant']} {suffix}"
            spec["report_id"] = str(report_id) if spec["on_report"] else None
            s.add(
                Expense(
                    organization_id=org_id,
                    entity_id=eid,
                    report_id=report_id if spec["on_report"] else None,
                    expense_date=_TODAY,
                    merchant=spec["merchant"],
                    category=spec["category"],
                    description=spec["description"],
                    amount=Decimal(spec["amount"]),
                    currency=spec["currency"],
                    status=spec["status"],
                )
            )
        await s.commit()
    return specs, str(report_id)


async def test_expense_summary_covers_the_whole_set_not_the_loaded_page(realdb):
    """The KPI row reduced over the loaded 20-row page AND summed EUR into USD,
    so "Period total" contradicted the whole-set "Expenses" card beside it."""
    seeded, _ = await _seed_expenses(realdb)

    async with realdb.client(key="a", role="ap_clerk") as c:
        listed = (await c.get("/api/expenses")).json()
        body = (await c.get("/api/expenses/summary")).json()

    assert len(listed["items"]) == DEFAULT_PAGE_SIZE
    assert listed["total"] == _OVER_A_PAGE
    assert body["total"] == _OVER_A_PAGE
    assert body["by_status"] == _tally(seeded, "status")

    expected = _money_by_currency(seeded)
    got = _by_currency(body)
    assert set(got) == {"USD", "EUR"}
    for code, total in expected.items():
        assert got[code]["total"] == str(total), code
        assert isinstance(got[code]["total"], str)
    # Never one blended figure: the two groups' sum is not reported anywhere.
    blended = sum(expected.values())
    assert all(Decimal(r["total"]) != blended for r in body["by_currency"])


@pytest.mark.parametrize(
    "params, expect_kwargs",
    [
        ({}, {}),
        ({"status": "draft"}, {"status": "draft"}),
        ({"status": "submitted"}, {"status": "submitted"}),
        ({"status": "approved"}, {"status": "approved"}),
        ({"search": "skyline"}, {"search": "skyline"}),
        ({"search": "rail transfer"}, {"search": "rail transfer"}),  # description
        ({"search": "MEALS"}, {"search": "MEALS"}),  # category, case-insensitive
        ({"status": "draft", "search": "skyline"}, {"status": "draft", "search": "skyline"}),
        # A term matching rows the status filter excludes.
        ({"status": "approved", "search": "skyline"}, {"status": "approved", "search": "skyline"}),
        ({"search": "zzz-no-match"}, {"search": "zzz-no-match"}),
    ],
)
async def test_expense_list_summary_and_export_describe_one_set(realdb, params, expect_kwargs):
    """The #355 property, asserted across all three surfaces at once: for any
    filter set the page can produce, the table, the KPI row and the CSV agree.

    `GET /api/expenses/export` declared no `search` leg at all, and FastAPI
    drops an undeclared query param silently — so a CSV taken mid-search
    covered the whole status-filtered register with nothing to say it had.
    """
    seeded, _ = await _seed_expenses(realdb)
    expected = _exp_expect(seeded, **expect_kwargs)
    expected_money = _money_by_currency(expected)

    async with realdb.client(key="a", role="ap_clerk") as c:
        listed = (await c.get("/api/expenses", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/expenses/summary", params=params)).json()
        exported = await c.get("/api/expenses/export", params=params)

    assert exported.status_code == 200, params
    csv_rows = _csv_rows(exported.text)

    assert listed["total"] == len(expected), params
    assert body["total"] == len(expected), params
    assert len(csv_rows) == len(expected), params
    assert {r["merchant"] for r in csv_rows} == {r["merchant"] for r in expected}, params
    assert {i["merchant"] for i in listed["items"]} == {r["merchant"] for r in expected}, params

    assert body["by_status"] == _tally(expected, "status"), params
    got = _by_currency(body)
    assert set(got) == set(expected_money), params
    for code, total in expected_money.items():
        assert got[code]["total"] == str(total), (params, code)
        assert got[code]["count"] == sum(1 for r in expected if r["currency"] == code)


async def test_expense_report_id_filter_narrows_all_three_surfaces(realdb):
    seeded, report_id = await _seed_expenses(realdb)
    expected = _exp_expect(seeded, on_report=True)
    assert 0 < len(expected) < len(seeded)

    async with realdb.client(key="a", role="ap_clerk") as c:
        params = {"report_id": report_id}
        listed = (await c.get("/api/expenses", params={**params, "page_size": 100})).json()
        body = (await c.get("/api/expenses/summary", params=params)).json()
        exported = await c.get("/api/expenses/export", params=params)

    assert listed["total"] == len(expected)
    assert body["total"] == len(expected)
    assert len(_csv_rows(exported.text)) == len(expected)
    assert body["by_status"] == _tally(expected, "status")


async def test_expense_export_row_count_matches_the_whole_filtered_set_past_a_page(realdb):
    """The CSV has no page — 25 rows in, 25 data rows out, not the list
    window's 20."""
    seeded, _ = await _seed_expenses(realdb)

    async with realdb.client(key="a", role="ap_clerk") as c:
        first_page = (await c.get("/api/expenses")).json()
        exported = await c.get("/api/expenses/export")

    assert len(first_page["items"]) == DEFAULT_PAGE_SIZE
    assert len(_csv_rows(exported.text)) == len(seeded) == _OVER_A_PAGE


async def test_expense_summary_and_export_narrow_identically_on_x_entity_id(realdb):
    sub = await _extra_entity(realdb, slug="exp-sub")
    await _seed_expenses(realdb)
    sub_rows, _ = await _seed_expenses(realdb, entity_id=sub, rows=_EXP_SEED[:4])

    async with realdb.client(key="a", role="ap_clerk") as c:
        headers = {"X-Entity-ID": str(sub)}
        listed = (await c.get("/api/expenses", params={"page_size": 100}, headers=headers)).json()
        body = (await c.get("/api/expenses/summary", headers=headers)).json()
        exported = await c.get("/api/expenses/export", headers=headers)

    assert listed["total"] == len(sub_rows)
    assert body["total"] == len(sub_rows)
    assert len(_csv_rows(exported.text)) == len(sub_rows)
    assert body["by_status"] == _tally(sub_rows, "status")


async def test_expense_summary_single_currency_org_reports_one_group(realdb):
    single = [dict(r, currency="USD") for r in _EXP_SEED]
    await _seed_expenses(realdb, key="b", rows=single)
    async with realdb.client(key="b", role="ap_clerk") as c:
        body = (await c.get("/api/expenses/summary")).json()

    assert [r["currency"] for r in body["by_currency"]] == ["USD"]
    assert body["by_currency"][0]["total"] == str(_money_by_currency(single)["USD"])


async def test_expense_summary_is_a_well_formed_zero_on_an_empty_set(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        body = (await c.get("/api/expenses/summary")).json()
        exported = await c.get("/api/expenses/export")
    assert body == {"total": 0, "by_status": {}, "by_currency": []}
    assert _csv_rows(exported.text) == []


# ===========================================================================
# The documented whole-set exemption
# ===========================================================================


async def _seed_payments(realdb, key="a"):
    """Two completed + two pending payments on USD invoices, plus one invoice
    with no payment (the queue leg)."""
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    completed = [Decimal("100.00"), Decimal("250.50")]
    pending = [Decimal("75.25"), Decimal("10.00")]
    async with mk() as s:
        eid = await _default_entity_id(s)
        idx = 0
        for status, amounts in (("completed", completed), ("submitted", pending)):
            for amount in amounts:
                idx += 1
                inv = Invoice(
                    organization_id=org_id,
                    entity_id=eid,
                    invoice_number=f"PAY-{idx}-{uuid.uuid4().hex[:6]}",
                    vendor_name="Globex Corp",
                    amount=amount,
                    currency="USD",
                    invoice_date=_TODAY,
                    due_date=_TODAY + timedelta(days=30),
                    status=InvoiceStatus.approved,
                )
                s.add(inv)
                await s.flush()
                s.add(
                    Payment(
                        entity_id=eid,
                        invoice_id=inv.id,
                        amount=amount,
                        method="ach",
                        status=status,
                    )
                )
        await s.commit()
    return sum(completed), sum(pending)


async def test_payments_summary_is_a_whole_entity_treasury_figure_by_design(realdb):
    """`/api/payments/summary` is exempted in
    `tests/test_whole_set_kpi_rollups.py::_DELIBERATELY_WHOLE_SET`. This asserts
    the exemption's CLAIM holds: it takes none of the list's filters, so
    narrowing the payments table cannot move it, and its money is the reporting
    currency as exact strings.
    """
    paid, pending = await _seed_payments(realdb)

    async with realdb.client(key="a", role="ap_manager") as c:
        baseline = (await c.get("/api/payments/summary")).json()
        # Every filter the LIST offers, sent at the summary. FastAPI drops each
        # (they are not declared), which is exactly what "whole set" means here.
        with_filters = (
            await c.get(
                "/api/payments/summary",
                params={
                    "status": "completed",
                    "method": "check",
                    "search": "no-such-vendor",
                    "amount_min": 999999,
                },
            )
        ).json()
        narrowed_list = (
            await c.get("/api/payments", params={"status": "completed", "page_size": 100})
        ).json()

    assert baseline["total_paid"] == str(paid)
    assert baseline["total_pending"] == str(pending)
    assert isinstance(baseline["total_paid"], str)
    assert baseline["currency"]
    # The list really does narrow …
    assert narrowed_list["total"] == 2
    # … and the KPI bar deliberately does not follow it.
    assert with_filters == baseline
    assert baseline["payment_count"] == 4


async def test_payments_summary_is_still_entity_scoped(realdb):
    """Whole-SET is not whole-tenant: the exemption is about the list's
    filters, and `X-Entity-ID` must still narrow it or the treasury figure
    would span subsidiaries the page is not showing."""
    sub = await _extra_entity(realdb, slug="pay-sub")
    await _seed_payments(realdb)

    async with realdb.client(key="a", role="ap_manager") as c:
        scoped = (await c.get("/api/payments/summary", headers={"X-Entity-ID": str(sub)})).json()

    assert scoped["total_paid"] == "0"
    assert scoped["total_pending"] == "0"
    assert scoped["payment_count"] == 0


# ===========================================================================
# /api/exceptions/summary — faceted chip tallies
# ===========================================================================
#
# Not an exemption any more. The summary takes every filter the queue takes and
# applies them through the queue's own builder, and each of its three tallies
# ignores exactly one filter — its own dimension — so a chip reads what the
# table would show if it were clicked. The expectations below are recomputed
# from the seed spec, never from the endpoint's own builder.

#: Vendors the seeded invoices carry. `None` = an invoice-less exception (a
#: Positive Pay `not_on_file` fraud flag has no invoice), which has no invoice
#: number or vendor and so can never match a search.
_EXC_VENDORS = ("Acme Supplies", "Globex Corp", "Initech 50%_Ltd", None)
_EXC_TYPES = ("duplicate", "po_mismatch", "fraud_flag", "missing_data")
_EXC_STATUSES = ("open", "open", "escalated", "resolved", "dismissed")
_EXC_SEVERITIES = ("error", "warning", "info")


async def _seed_exceptions(realdb, key="a") -> list[dict]:
    """A population larger than one page, crossing every axis the queue
    filters on. Returns the spec with each row's `invoice_number` filled in."""
    mk = realdb.sessionmaker(key)
    org_id = realdb.info(key).org_id
    suffix = uuid.uuid4().hex[:6]
    spec = [
        {
            "exception_type": _EXC_TYPES[i % len(_EXC_TYPES)],
            "status": _EXC_STATUSES[i % len(_EXC_STATUSES)],
            "severity": _EXC_SEVERITIES[i % len(_EXC_SEVERITIES)],
            "vendor_name": _EXC_VENDORS[i % len(_EXC_VENDORS)],
            "invoice_number": None,
        }
        for i in range(_OVER_A_PAGE + 2)
    ]
    async with mk() as s:
        eid = await _default_entity_id(s)
        for idx, row in enumerate(spec):
            invoice_id = None
            if row["vendor_name"] is not None:
                row["invoice_number"] = f"EXC-{idx:03d}-{suffix}"
                inv = Invoice(
                    organization_id=org_id,
                    entity_id=eid,
                    invoice_number=row["invoice_number"],
                    vendor_name=row["vendor_name"],
                    amount=Decimal("10.00"),
                    currency="USD",
                    status=InvoiceStatus.new,
                )
                s.add(inv)
                await s.flush()
                invoice_id = inv.id
            s.add(
                APException(
                    organization_id=org_id,
                    entity_id=eid,
                    invoice_id=invoice_id,
                    exception_type=row["exception_type"],
                    severity=row["severity"],
                    status=row["status"],
                )
            )
        await s.commit()
    return spec


def _exc_expect(rows: list[dict], **f) -> list[dict]:
    """The queue's filter predicates, re-implemented over the seed spec.
    `status` takes the list's own meaning: absent or `all` is no filter."""
    kept = rows
    if f.get("status") not in (None, "all"):
        wanted = set(f["status"].split(","))
        kept = [r for r in kept if r["status"] in wanted]
    if f.get("type"):
        kept = [r for r in kept if r["exception_type"] == f["type"]]
    if f.get("severity"):
        kept = [r for r in kept if r["severity"] == f["severity"]]
    if f.get("search"):
        term = f["search"]
        kept = [
            r
            for r in kept
            if _contains(r["invoice_number"], term) or _contains(r["vendor_name"], term)
        ]
    return kept


def _without(filters: dict, dimension: str) -> dict:
    return {k: v for k, v in filters.items() if k != dimension}


def _statuses(rows: list[dict]) -> dict[str, int]:
    tally = _tally(rows, "status")
    return {s: tally.get(s, 0) for s in ("open", "escalated", "resolved", "dismissed")}


_EXC_FILTER_CASES = [
    {},
    {"status": "open"},
    {"status": "all"},
    {"status": "open,escalated"},
    {"type": "fraud_flag"},
    {"severity": "error"},
    {"search": "acme"},
    # LIKE metacharacters are literal: `50%_` matches the one vendor that
    # carries them, not every row (`%`) or any single character (`_`).
    {"search": "50%_"},
    {"search": "EXC-00"},
    {"status": "open", "type": "duplicate", "severity": "warning", "search": "globex"},
    {"search": "zzz-no-match"},
]


@pytest.mark.parametrize("filters", _EXC_FILTER_CASES, ids=repr)
async def test_exceptions_summary_tallies_follow_every_filter_but_their_own(realdb, filters):
    """Each chip row counts the set every OTHER selected filter describes.

    The status counts ignore `status` (a selected status chip must not zero its
    siblings), `by_type` ignores `type`, `by_severity` ignores `severity` — and
    every one of them follows the search box, so a search for one vendor no
    longer leaves the chips counting the whole tenant above a one-row table.
    """
    spec = await _seed_exceptions(realdb)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/exceptions/summary", params=filters)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    expected_status = _statuses(_exc_expect(spec, **_without(filters, "status")))
    for status, count in expected_status.items():
        assert body[status] == count, (filters, status)
    assert body["by_type"] == _tally(
        _exc_expect(spec, **_without(filters, "type")), "exception_type"
    )
    assert body["by_severity"] == _tally(
        _exc_expect(spec, **_without(filters, "severity")), "severity"
    )


@pytest.mark.parametrize("filters", _EXC_FILTER_CASES, ids=repr)
async def test_exceptions_list_ids_and_active_chips_describe_one_set(realdb, filters):
    """The table's total, the "select all N matching" resolver and the chip
    that is switched on must all be the same number — the whole point of
    routing the three endpoints through one filter builder.
    """
    spec = await _seed_exceptions(realdb)
    expected = _exc_expect(spec, **filters)

    async with realdb.client(key="a", role="ap_manager") as c:
        listed = (await c.get("/api/exceptions", params={**filters, "page_size": 100})).json()
        ids = (await c.get("/api/exceptions/ids", params=filters)).json()
        summary = (await c.get("/api/exceptions/summary", params=filters)).json()

    assert listed["total"] == len(expected), filters
    assert len(listed["items"]) == len(expected), filters
    assert ids["total"] == len(expected), filters
    assert set(ids["ids"]) == {row["id"] for row in listed["items"]}, filters

    # Summing any one chip row over its whole roster reproduces the table when
    # that row's own filter is off; with it on, the selected chip does.
    if filters.get("type"):
        assert summary["by_type"].get(filters["type"], 0) == len(expected)
    else:
        assert sum(summary["by_type"].values()) == len(expected)
    if filters.get("severity"):
        assert summary["by_severity"].get(filters["severity"], 0) == len(expected)
    else:
        assert sum(summary["by_severity"].values()) == len(expected)
    selected = [s for s in (filters.get("status") or "all").split(",") if s != "all"]
    statuses = selected or ["open", "escalated", "resolved", "dismissed"]
    assert sum(summary[s] for s in statuses) == len(expected), filters


async def test_exceptions_search_never_matches_an_invoice_less_exception(realdb):
    """An invoice-less exception has no number and no vendor. A search must
    neither error on it nor match it, and clearing the search must bring it
    back — it is excluded by the term, not lost by the join."""
    spec = await _seed_exceptions(realdb)
    orphans = [r for r in spec if r["vendor_name"] is None]
    assert orphans, "the seed must include invoice-less exceptions"

    async with realdb.client(key="a", role="ap_manager") as c:
        searched = await c.get("/api/exceptions", params={"search": "e", "page_size": 100})
        unsearched = await c.get("/api/exceptions", params={"page_size": 100})
        blank = await c.get("/api/exceptions", params={"search": "   ", "page_size": 100})

    assert searched.status_code == 200, searched.text
    assert all(item["invoice_id"] is not None for item in searched.json()["items"])
    assert sum(1 for i in unsearched.json()["items"] if i["invoice_id"] is None) == len(orphans)
    # A whitespace-only term is no filter, as on every other list.
    assert blank.json()["total"] == unsearched.json()["total"] == len(spec)


async def test_exceptions_summary_is_entity_scoped(realdb):
    """`X-Entity-ID` narrows every tally, exactly as it narrows the list."""
    sub = await _extra_entity(realdb, slug="exc-sub")
    await _seed_exceptions(realdb)

    async with realdb.client(key="a", role="ap_manager") as c:
        scoped = (await c.get("/api/exceptions/summary", headers={"X-Entity-ID": str(sub)})).json()

    assert {k: scoped[k] for k in ("open", "escalated", "resolved", "dismissed")} == {
        "open": 0,
        "escalated": 0,
        "resolved": 0,
        "dismissed": 0,
    }
    assert scoped["by_type"] == {}
    assert scoped["by_severity"] == {}


# ===========================================================================
# Cross-surface: an empty tenant yields a well-formed zero everywhere
# ===========================================================================


@pytest.mark.parametrize(
    "path, role",
    [
        ("/api/budgets/summary", "cfo"),
        ("/api/expenses/summary", "ap_clerk"),
        ("/api/intake/summary", "ap_clerk"),
        ("/api/positive-pay/summary", "ap_manager"),
        ("/api/recurring/summary", "ap_clerk"),
        ("/api/requisitions/summary", "ap_clerk"),
        ("/api/vendor-statements/summary", "ap_manager"),
        ("/api/invoices/counts", "ap_clerk"),
        ("/api/payments/summary", "ap_manager"),
        ("/api/exceptions/summary", "ap_manager"),
    ],
)
async def test_every_rollup_returns_a_zero_not_a_null_or_a_500(realdb, path, role):
    """An empty (or freshly filtered-to-nothing) set is the state a new tenant
    opens every page in. A rollup that 500s or returns `null` there is a broken
    KPI row on day one — and it is the case a hand-rolled aggregate most often
    misses, because `SUM()` over no rows is NULL, not 0."""
    async with realdb.client(key="a", role=role) as c:
        resp = await c.get(path)
        empty_filtered = await c.get(path, params={"search": "zzz-no-match"})

    assert resp.status_code == 200, path
    body = resp.json()
    assert isinstance(body, dict) and body, path

    checked = 0
    for field, value in body.items():
        # `currency` is a label, `soonest_next_run` is an absent date — neither
        # is a figure that should read zero.
        if field in {"currency", "soonest_next_run"}:
            assert value is None or isinstance(value, str), (path, field)
            continue
        checked += 1
        if isinstance(value, str):
            # Money fields serialise as exact decimal strings, never null.
            assert Decimal(value) == Decimal("0"), (path, field)
        elif isinstance(value, bool):
            raise AssertionError(f"{path}:{field} unexpected bool in a rollup")
        elif isinstance(value, int | float):
            assert value == 0, (path, field)
        else:
            assert value == [] or value == {}, (path, field)
    # Guards the loop: a response whose every field was skipped would have made
    # the assertions above unreachable and this test a no-op.
    assert checked >= 2, (path, sorted(body))

    assert empty_filtered.status_code == 200, path
    assert empty_filtered.json() == body, path
