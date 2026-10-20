"""The real QuickBooks Online sandbox company, replayed through the adapter and
both syncs (#598, decisions §284).

FeohLedger was tested against Intuit's sample company on 2026-10-19 and the
live sync found what no hand-written fixture had: same-named sub-accounts
collapsing into one row, a numbered chart drifting, inactive records the query
never returned. CI cannot reach Intuit, so that company is recorded in
``tools/fake-erp/fixtures/qbo_sandbox_company.json`` and served two ways from
the one file, so the layers cannot drift:

* by the fake ERP (``tools/fake-erp/qbo_sandbox.py``) to the e2e
  ``erp/quickbooks-online-sandbox.spec.ts``;
* here, in-process: the REAL adapter's HTTP goes through an
  ``httpx.MockTransport`` whose handler answers with the fake's own
  ``run_query`` — so Intuit's query semantics (active records only unless the
  query asks, paging) are the fake's, not a copy of them.

The replay seeds our chart and vendors as they stood BEFORE the live sync
(``legacy_chart`` / ``legacy_vendors``, keyed the pre-§284 way) and checks the
syncs answer exactly what they answered live (``live_sync``).
"""

from __future__ import annotations

import dataclasses
import importlib.util
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.gl_account import GLAccount
from app.models.vendor import Vendor
from app.services import erp_oauth
from app.services.erp_adapters import quickbooks_online
from app.services.erp_adapters.quickbooks_online import QuickBooksOnlineAdapter
from app.services.erp_link_realm import connection_realm
from app.services.gl_account_sync import AMBIGUOUS_LEGACY, sync_chart_from_erp
from app.services.vendor_sync import sync_vendors_from_erp

TENANT = "a"
_FAKE_ERP = Path(__file__).resolve().parents[2] / "tools" / "fake-erp"


def _load_fake():
    spec = importlib.util.spec_from_file_location("qbo_sandbox", _FAKE_ERP / "qbo_sandbox.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = _load_fake()
COMPANY = FAKE.COMPANY
REALM = FAKE.REALM
TOKEN = "sandbox-replay-token"
API_BASE = "http://fake-erp.test/qbo"
CONFIG = {
    "type": "quickbooks_online",
    "integration_method": "direct",
    "oauth": {"provider": "quickbooks_online", "external_tenant_id": REALM},
}
#: The connection realm the endpoints stamp links with (§284).
LINK_REALM = connection_realm(CONFIG)
#: Every pre-0115 link in the seeded state is confirmed by this company's own
#: pull (the same id holds the row's code, or the legacy key it was synced
#: under; the same vendor name), so rule 0 adopts them all and moves none —
#: the repair below is rules 3 and 4 alone, exactly as live.
ADOPTED_CHART_LINKS = sum(1 for r in COMPANY["legacy_chart"] if r.get("erp_account_id"))
ADOPTED_VENDOR_LINKS = sum(1 for v in COMPANY["legacy_vendors"] if v.get("erp_vendor_id"))


# ---------------------------------------------------------------------------
# The sandbox over an in-process transport
# ---------------------------------------------------------------------------


class _Sandbox:
    """The company's ``/query`` route, answered by the fake's own parser."""

    def __init__(self):
        self.statements: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        prefix = f"/qbo/v3/company/{REALM}/"
        assert request.url.path.startswith(prefix), request.url
        assert request.url.params["minorversion"] == quickbooks_online.MINOR_VERSION
        path = request.url.path.removeprefix(prefix)
        if path != "query":
            raise AssertionError(f"the replay serves only queries, not {path}")
        statement = request.url.params["query"]
        self.statements.append(statement)
        try:
            body = {"QueryResponse": FAKE.run_query(FAKE.list_entities(), statement)}
        except FAKE.QboFault as fault:
            return httpx.Response(fault.status, json=fault.body())
        return httpx.Response(200, json=body)


@pytest.fixture
def sandbox(monkeypatch):
    """Point the adapter at the sandbox; tokens come from a patched
    ``erp_oauth`` (the token lifecycle is the e2e's, against the fake)."""
    monkeypatch.setattr(settings, "erp_qbo_api_base", API_BASE)
    company = _Sandbox()
    transport = httpx.MockTransport(company.handle)
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    with (
        patch.object(erp_oauth, "get_access_token", new=AsyncMock(return_value=TOKEN)),
        patch.object(quickbooks_online.httpx, "AsyncClient", client),
    ):
        yield company


def _adapter() -> QuickBooksOnlineAdapter:
    return QuickBooksOnlineAdapter(CONFIG)


def _account(account_id: str) -> dict:
    return next(a for a in COMPANY["accounts"] if a["Id"] == account_id)


# ---------------------------------------------------------------------------
# Intuit's query semantics, as the fake serves them
# ---------------------------------------------------------------------------


def test_the_recorded_company_is_the_one_tested_live():
    accounts, vendors = COMPANY["accounts"], COMPANY["vendors"]
    assert (len(accounts), len(vendors)) == (91, 29)
    assert len({a["Id"] for a in accounts}) == 91
    assert len({a["FullyQualifiedName"] for a in accounts}) == 91
    assert {a["Id"]: a["AcctNum"] for a in accounts if "AcctNum" in a} == {
        "7": "6010",
        "15": "6020",
    }
    assert [a["Id"] for a in accounts if not a["Active"]] == ["8"]
    assert [v["Id"] for v in vendors if not v["Active"]] == ["57"]
    # Every ParentRef names a real account, and the path agrees with it.
    by_id = {a["Id"]: a for a in accounts}
    for a in accounts:
        if a["SubAccount"]:
            parent = by_id[a["ParentRef"]["value"]]
            assert a["FullyQualifiedName"] == f"{parent['FullyQualifiedName']}:{a['Name']}"
        else:
            assert "ParentRef" not in a and a["FullyQualifiedName"] == a["Name"]
    # The repeats the live sync tripped on: a Name QuickBooks keeps unique only
    # among siblings.
    repeated = {n for n, c in Counter(a["Name"] for a in accounts).items() if c > 1}
    assert repeated == {
        "Job Materials",
        "Decks and Patios",
        "Plants and Soil",
        "Sprinklers and Drip Systems",
        "Installation",
        "Equipment Rental",
        "Maintenance and Repair",
        "Depreciation",
        "Software",
    }


def test_a_query_returns_active_records_unless_it_asks_for_inactive_ones():
    entities = FAKE.list_entities()

    def ids(statement, entity):
        return [r["Id"] for r in FAKE.run_query(entities, statement).get(entity, [])]

    assert "8" not in ids("select * from Account MAXRESULTS 1000", "Account")
    assert len(ids("select * from Account MAXRESULTS 1000", "Account")) == 90
    every = ids("select * from Account where Active IN (true,false) MAXRESULTS 1000", "Account")
    assert len(every) == 91 and "8" in every
    assert ids("select * from Vendor where Active = false", "Vendor") == ["57"]
    assert len(ids("select * from Vendor", "Vendor")) == 28
    # QuickBooks' default page is 100; paging walks the filtered list.
    assert (
        ids("select * from Account STARTPOSITION 81 MAXRESULTS 20", "Account")
        == [a["Id"] for a in COMPANY["accounts"] if a["Active"]][80:]
    )
    assert FAKE.run_query(entities, "select * from Account STARTPOSITION 200") == {}
    with pytest.raises(FAKE.QboFault):
        FAKE.run_query(entities, "select * from Account where Name = 'Software'")
    # A condition after the paging terms is refused, as Intuit's parser does.
    with pytest.raises(FAKE.QboFault):
        FAKE.run_query(entities, "select * from Account MAXRESULTS 5 where Active = false")
    # Blanks are blanks, and a quoted value is matched exactly as sent.
    assert ids("select  *\tfrom Vendor\n where  Active IN ( false )", "Vendor") == ["57"]
    bills = {"Bill": [{"Id": "1", "DocNumber": "A  and where B"}, {"Id": "2", "DocNumber": "A"}]}
    found = FAKE.run_query(bills, "select * from Bill where DocNumber = 'A  and where B'")
    assert [b["Id"] for b in found["Bill"]] == ["1"]


# ---------------------------------------------------------------------------
# The adapter over the sandbox
# ---------------------------------------------------------------------------


async def test_the_chart_is_keyed_by_what_quickbooks_keeps_unique(sandbox):
    accounts = await _adapter().list_gl_accounts()
    by_id = {a.erp_account_id: a for a in accounts}
    codes = [a.code for a in accounts]
    assert len(codes) == len(set(codes)), "two accounts share a code"

    # Numbering is on for two accounts only.
    assert by_id["7"].code == "6010" and by_id["15"].code == "6020"
    # The two "Software" accounts are two codes, and the sub-account's parent
    # is its parent's code — the number.
    assert by_id["1150040000"].code == "Software"
    assert by_id["1150040001"].code == "Office Expenses:Software"
    assert by_id["1150040001"].parent_code == "6020"
    # ...and its id, which the sync uses to name the parent by OUR code.
    assert by_id["1150040001"].parent_erp_account_id == "15"
    assert all(a.reports_parent for a in accounts)
    assert by_id["29"].parent_erp_account_id is None and by_id["29"].parent_code is None
    assert "Software" in by_id["1150040001"].legacy_codes
    # Each repeated leaf is one code per path.
    assert (by_id["40"].code, by_id["39"].code) == ("Depreciation", "Truck:Depreciation")
    assert by_id["72"].code == "Maintenance and Repair"
    assert by_id["53"].code == "Landscaping Services:Labor:Maintenance and Repair"
    assert by_id["46"].code == "Landscaping Services:Job Materials"
    assert by_id["63"].code == "Job Expenses:Job Materials"
    # Classification maps onto our lowercase type.
    assert by_id["33"].account_type == "liability"
    assert by_id["45"].account_type == "revenue"
    assert by_id["2"].account_type == "equity"

    # What the query asked for decides whether the inactive Bank Charges (8)
    # reaches the sync: the adapter's statement is pinned with it. Live, the
    # plain select dropped it, so our row stayed active and linked; the query
    # now asks for inactive records too and the account arrives inactive.
    assert sandbox.statements == [
        "select * from Account where Active IN (true, false) STARTPOSITION 1 MAXRESULTS 1000"
    ]
    assert len(accounts) == 91
    assert by_id["8"].active is False
    # QuickBooks renames an inactive record "Bank Charges (deleted)"; the
    # adapter reads it back under the name it is linked by.
    assert (by_id["8"].code, by_id["8"].name) == ("Bank Charges", "Bank Charges")
    assert "Bank Charges" in by_id["8"].legacy_codes
    assert all(a.active for a in accounts if a.erp_account_id != "8")


def test_only_an_inactive_records_deleted_suffix_is_removed():
    """QuickBooks appends " (deleted)" to every name of a record it makes
    inactive, a sub-account's path included. An ACTIVE record's name is the
    company's own, whatever it ends in."""
    undeleted = quickbooks_online._undeleted
    inactive = {
        "Id": "9",
        "Active": False,
        "Name": "Fuel (deleted)",
        "FullyQualifiedName": "Automobile:Fuel (deleted)",
    }
    assert undeleted(inactive) == {
        **inactive,
        "Name": "Fuel",
        "FullyQualifiedName": "Automobile:Fuel",
    }
    assert inactive["Name"] == "Fuel (deleted)"  # the raw record is not mutated
    vendor = {"Id": "5", "Active": "false", "DisplayName": "Acme: West (deleted)"}
    assert undeleted(vendor)["DisplayName"] == "Acme: West"
    active = {"Id": "7", "Active": True, "DisplayName": "Salvage (deleted)"}
    assert undeleted(active) is active


def test_a_deleted_name_a_new_active_record_reuses_keeps_its_suffix():
    """QuickBooks frees the name when it renames an inactive record, so a new
    active "Bank Charges" can follow the old one. Both stripped would share a
    code, which the sync refuses for both; the active one keeps the name."""
    old = {"Id": "8", "Active": False, "Name": "Bank Charges (deleted)"}
    old["FullyQualifiedName"] = old["Name"]
    new = {"Id": "99", "Active": True, "Name": "Bank Charges", "FullyQualifiedName": "Bank Charges"}
    gone = {
        "Id": "9",
        "Active": False,
        "Name": "Fuel (deleted)",
        "FullyQualifiedName": "Fuel (deleted)",
    }
    rows = quickbooks_online._undeleted_all([old, new, gone], quickbooks_online._account_code)
    assert [quickbooks_online._account_code(r) for r in rows] == [
        "Bank Charges (deleted)",
        "Bank Charges",
        "Fuel",
    ]
    vendors = quickbooks_online._undeleted_all(
        [
            {"Id": "57", "Active": False, "DisplayName": "Squeaky Kleen (deleted)"},
            {"Id": "70", "Active": True, "DisplayName": "squeaky  kleen"},
        ],
        quickbooks_online._vendor_name_key,
    )
    assert vendors[0]["DisplayName"] == "Squeaky Kleen (deleted)"


async def test_paging_walks_the_whole_chart(sandbox, monkeypatch):
    monkeypatch.setattr(quickbooks_online, "_PAGE_SIZE", 25)
    accounts = await _adapter().list_gl_accounts()
    assert len(accounts) == 91
    assert sandbox.statements == [
        f"select * from Account where Active IN (true, false) STARTPOSITION {start} MAXRESULTS 25"
        for start in (1, 26, 51, 76)
    ]


async def test_vendors_keep_their_names_terms_and_never_take_acctnum_as_code(sandbox):
    vendors = {v.erp_vendor_id: v for v in await _adapter().list_vendors()}
    aero = vendors["59"]
    assert aero.name == "Ærøskøbing Café & Søn ApS"
    assert aero.payment_terms == "Net 15"
    northwind = vendors["60"]
    assert northwind.name == (
        "Northwind Industrial Fasteners and Precision Components International Holdings Corporation"
    )
    assert len(northwind.name) == 90
    assert northwind.payment_terms == "Net 60"
    # "NW-778812" is OUR account number at Northwind, not Northwind's code.
    assert northwind.code is None
    assert vendors["32"].payment_terms == "Due on receipt"
    # Squeaky Kleen is inactive: read, and marked so (live it was never read).
    assert len(vendors) == 29 and vendors["57"].active is False
    assert vendors["57"].name == "Squeaky Kleen Car Wash"  # not "... (deleted)"
    # The AcctNum rides as legacy_code, for the old mapping's cleanup only.
    assert northwind.legacy_code == "NW-778812"


# ---------------------------------------------------------------------------
# The live sync, replayed against a real tenant
# ---------------------------------------------------------------------------


async def _seed_legacy_chart(realdb) -> None:
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        for row in COMPANY["legacy_chart"]:
            s.add(GLAccount(organization_id=org_id, entity_id=None, **row))
        await s.commit()


async def _sync_chart(realdb):
    accounts = await _adapter().list_gl_accounts()
    async with realdb.sessionmaker(TENANT)() as s:
        result = await sync_chart_from_erp(
            s,
            organization_id=realdb.info(TENANT).org_id,
            entity_id=None,
            accounts=accounts,
            realm=LINK_REALM,
        )
        await s.commit()
    return result


async def _chart(realdb) -> dict[str, GLAccount]:
    async with realdb.sessionmaker(TENANT)() as s:
        return {r.code: r for r in (await s.execute(select(GLAccount))).scalars().all()}


def _comparable(summary: dict) -> dict:
    """The live response, with the one list whose order is the DB's row order
    (not a contract) sorted."""
    return {
        **summary,
        "link_changes": sorted(summary["link_changes"], key=lambda c: c["code"]),
    }


async def test_the_live_sync_of_the_pre_284_chart_replays_exactly(realdb, sandbox):
    """The live answer, plus exactly what two later fixes add, one "updated"
    each: Bank Charges (8), inactive in QuickBooks, is retired in our chart
    (live it was never read and stayed active), and Equipment Rental (29),
    top-level in QuickBooks, loses the "Job Expenses" parent the merged row
    carried (live a top-level account never cleared a stale parent)."""
    await _seed_legacy_chart(realdb)
    assert len(await _chart(realdb)) == 81  # "89 accounts became 81 rows"

    result = await _sync_chart(realdb)

    live = COMPANY["live_sync"]["gl_accounts"]
    assert _comparable(result.summary(capped=False)) == _comparable(
        {
            **live,
            "updated": live["updated"] + 2,
            "unchanged": live["unchanged"] - 1,
            "deactivated": ["Bank Charges"],
            "deactivated_count": 1,
            "realm": LINK_REALM,
            "realm_adopted": ADOPTED_CHART_LINKS,
            # Reported since the live run; every sandbox parent path fits.
            "parent_code_dropped": [],
            "parent_code_dropped_count": 0,
        }
    )
    assert (await _chart(realdb))["Bank Charges"].is_active is False


async def test_after_the_repair_every_path_has_its_own_correctly_linked_row(realdb, sandbox):
    await _seed_legacy_chart(realdb)
    await _sync_chart(realdb)
    chart = await _chart(realdb)

    linked = [r for r in chart.values() if r.erp_account_id]
    assert len(linked) == 89
    assert len({r.erp_account_id for r in linked}) == 89

    # Each repeated leaf is one row per path, on that path's id.
    for path, account_id in [
        ("Job Expenses:Job Materials", "63"),
        ("Landscaping Services:Job Materials", "46"),
        ("Job Expenses:Job Materials:Decks and Patios", "64"),
        ("Landscaping Services:Job Materials:Decks and Patios", "47"),
        ("Truck:Depreciation", "39"),
        ("Landscaping Services:Labor:Maintenance and Repair", "53"),
        ("Office Expenses:Software", "1150040001"),
        ("Software", "1150040000"),
    ]:
        assert chart[path].erp_account_id == account_id, path
        assert chart[path].name == path

    # The merged bare-name rows stay, unlinked: their invoices are refused
    # account_not_linked and recoded, never posted on a guess.
    for code in [
        "Job Materials",
        "Decks and Patios",
        "Plants and Soil",
        "Sprinklers and Drip Systems",
        "Installation",
        "Maintenance and Repair",
        "Depreciation",
    ]:
        assert chart[code].erp_account_id is None, code

    # A numbered account keeps its code and its link; the number is drift.
    assert (chart["Advertising"].erp_account_id, chart["Office Expenses"].erp_account_id) == (
        "7",
        "15",
    )
    assert "6010" not in chart and "6020" not in chart

    # Parents name OUR row for the parent account: the numbered Office
    # Expenses keeps its code, so its Software is under "Office Expenses",
    # not QuickBooks' "6020"; and a top-level account has no parent, however
    # the merged row it came from was filed.
    assert chart["Office Expenses:Software"].parent_code == "Office Expenses"
    assert chart["Equipment Rental"].parent_code is None
    assert chart["Job Expenses:Equipment Rental"].parent_code == "Job Expenses"

    # Inactive in QuickBooks, so retired here and still on its own id: the
    # " (deleted)" QuickBooks puts on its name does not cost it its link.
    assert (chart["Bank Charges"].erp_account_id, chart["Bank Charges"].is_active) == ("8", False)
    assert chart["Bank Charges"].name == "Bank Charges"


async def test_a_second_sync_changes_nothing_and_still_refuses_the_same_codes(realdb, sandbox):
    await _seed_legacy_chart(realdb)
    first = await _sync_chart(realdb)
    again = (await _sync_chart(realdb)).summary(capped=False)

    assert (again["created"], again["updated"], again["linked"]) == (0, 0, 0)
    assert again["unlinked"] == [] and again["link_changes"] == []
    assert again["conflicts"] == first.conflicts
    assert {c["code"]: c["reason"] for c in again["conflicts"]} == {
        "Depreciation": AMBIGUOUS_LEGACY,
        "Maintenance and Repair": AMBIGUOUS_LEGACY,
    }


async def test_a_confirmed_choice_settles_a_locked_out_account_for_good(realdb, sandbox):
    """Depreciation (40) was refused live because our bare-name row could mean
    it or Truck:Depreciation (39). Once a person confirms 40 (what
    ``POST /api/gl-accounts/{id}/erp-link`` writes), later syncs keep it."""
    from datetime import UTC, datetime

    await _seed_legacy_chart(realdb)
    await _sync_chart(realdb)
    async with realdb.sessionmaker(TENANT)() as s:
        row = (
            await s.execute(select(GLAccount).where(GLAccount.code == "Depreciation"))
        ).scalar_one()
        offered = sorted(a["erp_account_id"] for a in row.erp_link_candidates["accounts"])
        assert (row.erp_account_id, offered) == (None, ["39", "40"])
        row.erp_account_id = "40"
        row.erp_link_realm = LINK_REALM
        row.erp_link_confirmed_at = datetime.now(UTC)
        row.erp_link_candidates = None
        await s.commit()

    again = (await _sync_chart(realdb)).summary(capped=False)

    assert [c["code"] for c in again["conflicts"]] == ["Maintenance and Repair"]
    assert again["link_changes"] == [] and again["unlinked"] == []
    chart = await _chart(realdb)
    assert chart["Depreciation"].erp_account_id == "40"
    assert chart["Truck:Depreciation"].erp_account_id == "39"


async def test_the_live_vendor_sync_replays_exactly(realdb, sandbox):
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        for row in COMPANY["legacy_vendors"]:
            s.add(Vendor(organization_id=org_id, status="active", source="erp_sync", **row))
        await s.commit()

    # As the endpoint hands them over (`dataclasses.asdict`).
    pulled = [dataclasses.asdict(v) for v in await _adapter().list_vendors()]
    async with realdb.sessionmaker(TENANT)() as s:
        result = await sync_vendors_from_erp(s, org_id, pulled, realm=LINK_REALM)
        await s.commit()

    # The live answer, plus what reading inactive records adds: Squeaky Kleen
    # (57), inactive in QuickBooks, is deactivated here instead of staying an
    # active payee.
    live = COMPANY["live_sync"]["vendors"]
    assert result == {
        **live,
        "updated": live["updated"] + 1,
        "unchanged": live["unchanged"],
        "unlinked": [],
        "deactivated": 1,
        "codes_cleared": 0,
        "realm_adopted": ADOPTED_VENDOR_LINKS,
        # Reported since the live run; every sandbox value fits its column.
        "values_too_long": [],
        "realm": LINK_REALM,
    }
    async with realdb.sessionmaker(TENANT)() as s:
        vendors = {v.erp_vendor_id: v for v in (await s.execute(select(Vendor))).scalars().all()}
    assert vendors["57"].status == "inactive"
    assert vendors["60"].code is None
    assert vendors["60"].payment_terms == "Net 60"
    assert vendors["59"].name == "Ærøskøbing Café & Søn ApS"
