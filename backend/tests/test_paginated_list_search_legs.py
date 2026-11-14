"""A paginated list searches the whole set, or it lies about the empty case.

Round 13 fixed this for `/requisitions` and `/expenses`: both pages narrowed the
rows they had already loaded, so a term matching a row on page 2 rendered as
"nothing matched", and the footer's "Showing all N" — the server's whole-set
total — sat above a client-narrowed table. The fix was the backend `search` leg,
not better empty-state copy.

Two sibling surfaces shipped the same shape and were missed: `/vendor-statements`
and `/positive-pay`. Both paginate (Load-More against a server `total`), both had
a search box, and neither endpoint declared a `search` parameter — the filtering
happened entirely in the browser. This file pins their new legs, and pins them
against the property that matters rather than the plumbing: **the list, its
whole-set KPI rollup, and the paging footer describe one set.**

The tests page deliberately (`page_size=1`) because a bug that only shows past
the first page is exactly the one client-side filtering hides.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.positive_pay import PositivePayFile
from app.models.vendor import Vendor
from app.utils.dates import utc_today

pytestmark = pytest.mark.asyncio

_TODAY = utc_today()


async def _default_entity_id(session):
    from app.models.entity import Entity

    return (await session.execute(select(Entity.id).where(Entity.is_default))).scalars().first()


async def _add_vendor(mk, org_id, name: str) -> str:
    async with mk() as s:
        v = Vendor(organization_id=org_id, name=name, entity_id=await _default_entity_id(s))
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return str(v.id)


def _line(invoice_number: str, amount: str) -> dict:
    return {
        "invoice_number": invoice_number,
        "invoice_date": _TODAY.isoformat(),
        "amount": amount,
        "status": "open",
    }


# ---------------------------------------------------------------------------
# /api/vendor-statements
# ---------------------------------------------------------------------------


async def test_statement_search_matches_supplier_and_reference(realdb):
    """The two free-text columns the row renders — and the ones the page used to
    filter in the browser."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    northwind = await _add_vendor(mk, org_id, "Northwind Traders")
    contoso = await _add_vendor(mk, org_id, "Contoso Supply")

    async with realdb.client(key="a", role="ap_manager") as c:
        by_vendor = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": northwind,
                    "statement_date": _TODAY.isoformat(),
                    "currency": "USD",
                    "statement_reference": "STMT-Q2-2026",
                    "lines": [_line("NW-1", "10.00")],
                },
            )
        ).json()
        other = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": contoso,
                    "statement_date": _TODAY.isoformat(),
                    "currency": "USD",
                    "statement_reference": "REF-99",
                    "lines": [_line("CS-1", "20.00")],
                },
            )
        ).json()

        hit = (await c.get("/api/vendor-statements", params={"search": "northwind"})).json()
        assert [i["id"] for i in hit["items"]] == [by_vendor["id"]]
        assert hit["total"] == 1

        by_reference = (await c.get("/api/vendor-statements", params={"search": "Q2-2026"})).json()
        assert [i["id"] for i in by_reference["items"]] == [by_vendor["id"]]

        # And it narrows rather than replaces: the other run is still findable.
        assert [
            i["id"]
            for i in (await c.get("/api/vendor-statements", params={"search": "REF-99"})).json()[
                "items"
            ]
        ] == [other["id"]]

        none_found = (await c.get("/api/vendor-statements", params={"search": "zzz"})).json()
        assert none_found["total"] == 0
        assert none_found["items"] == []


async def test_statement_search_finds_a_run_past_the_first_page(realdb):
    """The defect in one assertion. With client-side filtering the term could
    only ever see the loaded page, so this run was invisible until the user
    paged to it — and the footer said "Showing all"."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="ap_manager") as c:
        # Created FIRST, so the `created_at DESC` list order pushes it off the
        # first page — which is the whole point of the assertion below.
        needle_vendor = await _add_vendor(mk, org_id, "Zzz Last Supplier")
        needle = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": needle_vendor,
                    "statement_date": _TODAY.isoformat(),
                    "currency": "USD",
                    "lines": [_line("Z-1", "10.00")],
                },
            )
        ).json()
        for n in range(3):
            vendor = await _add_vendor(mk, org_id, f"Filler Vendor {n}")
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": vendor,
                    "statement_date": _TODAY.isoformat(),
                    "currency": "USD",
                    "lines": [_line(f"F-{n}", "10.00")],
                },
            )

        # It is genuinely not on page 1 of the unfiltered list…
        page_one = (await c.get("/api/vendor-statements", params={"page_size": 1})).json()
        assert page_one["items"][0]["id"] != needle["id"]
        assert page_one["total"] > 1

        # …and the search finds it anyway, on page 1 of the filtered list.
        found = (
            await c.get("/api/vendor-statements", params={"search": "Zzz Last", "page_size": 1})
        ).json()
        assert [i["id"] for i in found["items"]] == [needle["id"]]
        assert found["total"] == 1


async def test_statement_summary_describes_the_searched_set(realdb):
    """The KPI row shares `_recon_list_filters` with the list, so it cannot
    describe a wider set than the table beneath it."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="ap_manager") as c:
        for name, ref in (("Alpha Metals", "A-1"), ("Beta Plastics", "B-1")):
            vendor = await _add_vendor(mk, org_id, name)
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": vendor,
                    "statement_date": _TODAY.isoformat(),
                    "currency": "USD",
                    "statement_reference": ref,
                    "lines": [_line(ref, "10.00")],
                },
            )

        for params in ({}, {"search": "alpha"}, {"search": "beta"}, {"search": "no-such-thing"}):
            listed = (
                await c.get("/api/vendor-statements", params={**params, "page_size": 50})
            ).json()
            rolled = (await c.get("/api/vendor-statements/summary", params=params)).json()
            assert sum(rolled["by_status"].values()) == listed["total"], params


async def test_statement_search_is_tenant_scoped(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor = await _add_vendor(mk, org_id, "Tenant A Only Supplier")
    async with realdb.client(key="a", role="ap_manager") as c:
        await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": vendor,
                "statement_date": _TODAY.isoformat(),
                "currency": "USD",
                "lines": [_line("TA-1", "10.00")],
            },
        )

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get("/api/vendor-statements", params={"search": "Tenant A Only"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


# ---------------------------------------------------------------------------
# /api/positive-pay
# ---------------------------------------------------------------------------


async def _add_positive_pay_file(mk, org_id, *, bank_format: str, file_type: str) -> str:
    """Insert a `PositivePayFile` directly.

    The generate routes need a payment run with cheques on it; this file is
    about the list's filters, and the row's own columns are what those read.
    """
    async with mk() as s:
        row = PositivePayFile(
            organization_id=org_id,
            entity_id=await _default_entity_id(s),
            file_type=file_type,
            bank_format=bank_format,
            status="generated",
            item_count=1,
            total_amount=100,
            currency="USD",
            content_hash="0" * 64,
            created_at=datetime.now(UTC) - timedelta(seconds=len(bank_format)),
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return str(row.id)


async def test_positive_pay_search_matches_bank_format_and_file_type(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    csv_file = await _add_positive_pay_file(mk, org_id, bank_format="csv", file_type="check_issue")
    fixed = await _add_positive_pay_file(
        mk, org_id, bank_format="fixed_width", file_type="ach_authorization"
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        by_format = (await c.get("/api/positive-pay", params={"search": "fixed"})).json()
        assert [i["id"] for i in by_format["items"]] == [fixed]

        by_type = (await c.get("/api/positive-pay", params={"search": "ach"})).json()
        assert [i["id"] for i in by_type["items"]] == [fixed]

        assert (await c.get("/api/positive-pay", params={"search": "check_issue"})).json()["items"][
            0
        ]["id"] == csv_file


async def test_positive_pay_search_matches_the_id_prefix_the_row_shows(realdb):
    """The row's label renders an 8-character prefix of the file id, so pasting
    what is on screen has to find the row — that is what the browser-side filter
    did, and the only part of it that could move to SQL."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    target = await _add_positive_pay_file(mk, org_id, bank_format="csv", file_type="check_issue")
    await _add_positive_pay_file(mk, org_id, bank_format="csv", file_type="check_issue")

    async with realdb.client(key="a", role="ap_manager") as c:
        found = (await c.get("/api/positive-pay", params={"search": target[:8]})).json()
        assert [i["id"] for i in found["items"]] == [target]


async def test_positive_pay_search_finds_a_file_past_the_first_page(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    for _ in range(3):
        await _add_positive_pay_file(mk, org_id, bank_format="csv", file_type="check_issue")
    needle = await _add_positive_pay_file(
        mk, org_id, bank_format="fixed_width", file_type="ach_authorization"
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        page_one = (await c.get("/api/positive-pay", params={"page_size": 1})).json()
        assert page_one["total"] > 1

        found = (
            await c.get("/api/positive-pay", params={"search": "fixed_width", "page_size": 1})
        ).json()
        assert [i["id"] for i in found["items"]] == [needle]
        assert found["total"] == 1


async def test_positive_pay_summary_describes_the_searched_set(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_positive_pay_file(mk, org_id, bank_format="csv", file_type="check_issue")
    await _add_positive_pay_file(
        mk, org_id, bank_format="fixed_width", file_type="ach_authorization"
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        for params in ({}, {"search": "csv"}, {"search": "fixed"}, {"search": "nope"}):
            listed = (await c.get("/api/positive-pay", params={**params, "page_size": 50})).json()
            rolled = (await c.get("/api/positive-pay/summary", params=params)).json()
            assert sum(rolled["by_status"].values()) == listed["total"], params


async def test_positive_pay_search_is_tenant_scoped(realdb):
    mk = realdb.sessionmaker("a")
    await _add_positive_pay_file(
        mk, realdb.info("a").org_id, bank_format="csv", file_type="check_issue"
    )

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get("/api/positive-pay", params={"search": "csv"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------


# The list/rollup filter-parity guard that used to live here now lives in
# `test_whole_set_kpi_rollups.py`, which supersedes it on every axis: it
# discovers the surfaces instead of hardcoding four, checks the FULL filter set
# rather than `search` alone, reads the mounted OpenAPI schema (a parameter
# declared as a plain default is a real query param but not a
# `fastapi.params.Query` instance, so the signature check here passed
# vacuously), and additionally asserts both endpoints route through the shared
# filter builder. Keeping a weaker second copy would be the duplicated-guard
# debt `docs/followups.md` already tracks elsewhere.


def test_no_route_page_filters_loaded_rows_with_a_search_term():
    """Source scan over the SvelteKit routes: a paginated list must not narrow
    the rows it already loaded.

    `/requisitions` and `/expenses` shipped that way and needed an honest
    "searched only the N rows loaded so far" empty state to avoid lying;
    `/vendor-statements` and `/positive-pay` shipped it too and had no such
    caveat. The idiom is unmistakable — a `.filter()` over the loaded array
    testing `.toLowerCase().includes(` against the search box's term — so a scan
    is cheap and precise. The exemptions are the surfaces that fetch their whole
    set in one request, where filtering in the browser is correct.
    """
    import pathlib

    routes = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "routes"
    exempt = {
        # A bounded attention queue: the fetch pulls the whole set, and the
        # search covers a DERIVED field (formatted hit categories) that has no
        # column to ILIKE.
        "vendors/screening/+page.svelte",
        # `GET /api/tax/1099-report` returns every vendor in one payload — there
        # is no pagination to out-run.
        "tax/+page.svelte",
        # Filters a fully-loaded option list (the GL / step pickers), not a
        # paginated result set.
        "workflows/[id]/+page.svelte",
        # `/budgets` sends `search` to the server; its client pass only narrows
        # the already-filtered page during the debounce window.
        "budgets/+page.svelte",
    }
    offenders: list[str] = []
    for path in sorted(routes.rglob("+page.svelte")):
        relative = str(path.relative_to(routes))
        if relative in exempt:
            continue
        text = path.read_text(encoding="utf-8")
        if "toLowerCase().includes(" in text and ".filter(" in text:
            offenders.append(relative)

    assert not offenders, (
        f"{offenders} filter loaded rows by a search term. Add a `search` leg to "
        "the endpoint (and to its /summary sibling, through the SAME filter "
        "builder) and pass the term through — a client-side filter cannot see a "
        "row on page 2, so the user is told nothing matched while the footer "
        "counts the whole set."
    )
