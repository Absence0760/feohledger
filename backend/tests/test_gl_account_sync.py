"""The chart sync links each ERP account by the ERP's own unique key (§284).

Found in a live test against the QuickBooks Online sandbox (2026-10-19): the
adapter keyed accounts by their bare ``Name`` and the sync upserted by code, so
same-named sub-accounts ("Job Materials" under both "Job Expenses" and
"Landscaping Services") collapsed — 89 QuickBooks accounts became 81 rows with
"8 updated" on a first sync of an empty chart — and an invoice coded to one
"Job Materials" was posted as a Bill to the other.

``services/gl_account_sync.sync_chart_from_erp`` is exercised directly against a
real tenant (``realdb``) and once through ``POST /api/gl-accounts/sync-erp``
with the real QuickBooks mapping (``list_gl_accounts`` over raw ``Account``
rows), which is the regression for the live numbers.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest_asyncio
from sqlalchemy import select

from app.models.gl_account import GL_CODE_MAX_LENGTH, GLAccount
from app.services.erp_adapters.base import GLAccountPayload
from app.services.erp_adapters.quickbooks_online import QuickBooksOnlineAdapter
from app.services.gl_account_sync import (
    AMBIGUOUS_LEGACY,
    CODE_TAKEN,
    CODE_TOO_LONG,
    DUPLICATE_CODE,
    sync_chart_from_erp,
)
from app.services.gl_chart import resolve_erp_account_ids

TENANT = "a"

# Eight sub-account names QuickBooks' sandbox repeats under two parents.
_REPEATED = [
    "Job Materials",
    "Installation",
    "Equipment Rental",
    "Permits",
    "Decks and Patios",
    "Fountains and Garden Lighting",
    "Plants and Soil",
    "Sprinklers and Drip Systems",
]


def _qbo_chart() -> list[dict]:
    """89 raw QuickBooks ``Account`` rows: 71 plain accounts, two parents, and
    the eight repeated names under each parent (8 × 2 = 16 sub-accounts)."""
    rows = [
        {
            "Id": str(100 + i),
            "Name": f"Account {i:02d}",
            "FullyQualifiedName": f"Account {i:02d}",
            "Classification": "Expense",
        }
        for i in range(71)
    ]
    parents = {"1": "Job Expenses", "2": "Landscaping Services"}
    for pid, pname in parents.items():
        rows.append(
            {"Id": pid, "Name": pname, "FullyQualifiedName": pname, "Classification": "Expense"}
        )
    for p_index, (pid, pname) in enumerate(parents.items()):
        for j, sub in enumerate(_REPEATED):
            rows.append(
                {
                    "Id": str(1000 + 100 * p_index + j),
                    "Name": sub,
                    "FullyQualifiedName": f"{pname}:{sub}",
                    "Classification": "Expense",
                    "SubAccount": True,
                    "ParentRef": {"value": pid},
                }
            )
    assert len(rows) == 89
    return rows


async def _qbo_payloads(rows: list[dict]) -> list[GLAccountPayload]:
    async def query_all(self, entity, *, include_inactive=False):
        # The chart read must include inactive accounts (§284): QuickBooks'
        # plain select returns active ones only. It is always the whole chart:
        # `_query_all` reads whole or raises (§332), so there is no flag to ask.
        assert (entity, include_inactive) == ("Account", True)
        return rows

    with patch.object(QuickBooksOnlineAdapter, "_query_all", query_all):
        return await QuickBooksOnlineAdapter({"type": "quickbooks_online"}).list_gl_accounts()


async def _sync(realdb, accounts, entity_id=None):
    async with realdb.sessionmaker(TENANT)() as s:
        result = await sync_chart_from_erp(
            s,
            organization_id=realdb.info(TENANT).org_id,
            entity_id=entity_id,
            accounts=accounts,
        )
        await s.commit()
    return result


async def _rows(realdb) -> dict[str, GLAccount]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = (await s.execute(select(GLAccount))).scalars().all()
    return {r.code: r for r in rows}


async def _add(realdb, **kwargs) -> uuid.UUID:
    async with realdb.sessionmaker(TENANT)() as s:
        row = GLAccount(organization_id=realdb.info(TENANT).org_id, **kwargs)
        s.add(row)
        await s.commit()
        return row.id


def _acct(code, erp_id, name=None, **kw) -> GLAccountPayload:
    return GLAccountPayload(code=code, name=name or code, erp_account_id=erp_id, **kw)


# ---------------------------------------------------------------------------
# The live defect: 89 → 81, "8 updated"
# ---------------------------------------------------------------------------


async def test_qbo_same_named_sub_accounts_each_get_their_own_row(realdb):
    rows = _qbo_chart()
    accounts = await _qbo_payloads(rows)
    # What the pre-§284 key (the bare Name) made of this chart: 81 codes.
    assert len({c for a in accounts for c in a.legacy_codes}) == 81

    result = await _sync(realdb, accounts)

    assert (result.created, result.updated, result.conflicts, result.unlinked) == (89, 0, [], [])
    chart = await _rows(realdb)
    assert len(chart) == 89
    # Each same-named sub-account is its own row, linked to its own account.
    assert chart["Job Expenses:Job Materials"].erp_account_id == "1000"
    assert chart["Landscaping Services:Job Materials"].erp_account_id == "1100"
    assert chart["Job Expenses:Job Materials"].parent_code == "Job Expenses"
    assert chart["Landscaping Services:Job Materials"].name == (
        "Landscaping Services:Job Materials"
    )
    assert len({r.erp_account_id for r in chart.values()}) == 89


async def test_rerunning_an_unchanged_chart_changes_nothing(realdb):
    accounts = await _qbo_payloads(_qbo_chart())
    await _sync(realdb, accounts)
    again = await _sync(realdb, accounts)
    assert (again.created, again.updated, again.unchanged, again.linked) == (0, 0, 89, 0)


async def test_an_invoice_coded_to_one_job_materials_resolves_to_that_account(realdb):
    """The push path: a GL code resolves to the selected row's own ERP id, so
    the Bill's AccountRef is the account the bookkeeper picked."""
    await _sync(realdb, await _qbo_payloads(_qbo_chart()))
    async with realdb.sessionmaker(TENANT)() as s:
        ids = await resolve_erp_account_ids(
            s,
            realdb.info(TENANT).org_id,
            None,
            ["Job Expenses:Job Materials", "Landscaping Services:Job Materials"],
        )
    assert ids == {
        "Job Expenses:Job Materials": "1000",
        "Landscaping Services:Job Materials": "1100",
    }


# ---------------------------------------------------------------------------
# Matching: ERP id first, then code
# ---------------------------------------------------------------------------


async def test_a_rename_in_the_erp_updates_the_linked_row_and_keeps_its_code(realdb):
    """Matched by ERP id: no second row, the name follows the ERP, and the code
    — which invoices record as a string — is kept and reported as drift."""
    row_id = await _add(realdb, code="Travel", name="Travel", erp_account_id="7")
    result = await _sync(realdb, [_acct("Travel & Meals", "7")])

    assert (result.created, result.updated) == (0, 1)
    assert result.code_drift == [{"code": "Travel", "erp_code": "Travel & Meals"}]
    chart = await _rows(realdb)
    assert list(chart) == ["Travel"]
    assert (chart["Travel"].id, chart["Travel"].name) == (row_id, "Travel & Meals")


async def test_two_accounts_with_one_code_are_refused_and_neither_is_linked(realdb):
    """Never "the later one wins": the code is refused for both, nothing is
    created, and an existing row showing that code loses its link."""
    await _add(realdb, code="6100", name="Office", erp_account_id="A")
    result = await _sync(realdb, [_acct("6100", "A"), _acct("6100", "B"), _acct("6200", "C")])

    assert result.created == 1  # 6200 only
    assert result.updated == 0
    assert result.conflicts == [
        {"code": "6100", "reason": DUPLICATE_CODE, "erp_account_ids": ["A", "B"]}
    ]
    assert result.unlinked == ["6100"]
    chart = await _rows(realdb)
    assert chart["6100"].erp_account_id is None
    assert chart["6200"].erp_account_id == "C"


async def test_the_same_account_listed_twice_is_one_account(realdb):
    result = await _sync(realdb, [_acct("6100", "A"), _acct("6100", "A")])
    assert (result.created, result.conflicts) == (1, [])


async def test_a_code_held_by_a_row_linked_to_another_live_account_is_refused(realdb):
    """Account Y was renamed away from "Ops" and a new account Z took the name:
    our "Ops" row's invoices meant Y, so it stays on Y and Z is refused."""
    await _add(realdb, code="Ops", name="Ops", erp_account_id="Y")
    result = await _sync(realdb, [_acct("Ops 2", "Y"), _acct("Ops", "Z")])

    assert result.conflicts == [{"code": "Ops", "reason": CODE_TAKEN, "erp_account_ids": ["Z"]}]
    assert (await _rows(realdb))["Ops"].erp_account_id == "Y"


async def test_an_unlinked_row_is_linked_by_code(realdb):
    await _add(realdb, code="6100", name="Office (manual)")
    result = await _sync(realdb, [_acct("6100", "A", name="Office")])
    assert (result.created, result.linked) == (0, 1)
    assert (await _rows(realdb))["6100"].erp_account_id == "A"


async def test_a_dead_link_is_repointed_at_the_account_that_now_holds_the_code(realdb):
    """A chart moved to another ERP company (sandbox → production): the old id
    is gone from the pull, the code is unique in the new company."""
    await _add(realdb, code="6100", name="Office", erp_account_id="SANDBOX-7")
    result = await _sync(realdb, [_acct("6100", "PROD-42")])
    assert result.linked == 1
    assert (await _rows(realdb))["6100"].erp_account_id == "PROD-42"


async def test_a_sub_account_path_past_the_old_50_character_limit_is_stored(realdb):
    """Migration 0114 widened the column to the width invoices record a code in."""
    code = "Landscaping Services:Job Materials:Decks and Patios"
    assert 50 < len(code) <= GL_CODE_MAX_LENGTH
    result = await _sync(realdb, [_acct(code, "D")])
    assert result.created == 1
    assert (await _rows(realdb))[code].erp_account_id == "D"


async def test_a_link_to_an_over_long_account_is_live_not_dead(realdb):
    """The account the row is linked to is in the pull, just refused for its
    length — so the row's link is not "dead" and is never re-pointed at the
    other account that now shows the row's code."""
    await _add(realdb, code="6100", name="Office", erp_account_id="L")
    result = await _sync(
        realdb, [_acct("x" * (GL_CODE_MAX_LENGTH + 1), "L"), _acct("6100", "OTHER")]
    )
    assert result.linked == 0
    assert any(c["code"] == "6100" and c["reason"] == CODE_TAKEN for c in result.conflicts)
    assert (await _rows(realdb))["6100"].erp_account_id == "L"


async def test_an_over_long_code_is_refused_not_truncated(realdb):
    code = "x" * (GL_CODE_MAX_LENGTH + 1)
    result = await _sync(realdb, [_acct(code, "L")])
    assert result.created == 0
    assert result.conflicts == [{"code": code, "reason": CODE_TOO_LONG, "erp_account_ids": ["L"]}]


async def test_an_over_long_parent_code_is_dropped_never_aborts_the_sync(realdb):
    """`parent_code` is as wide as `code` and was written unchecked: one
    account whose parent's code ran past the column failed the flush and rolled
    back the whole sync. The account syncs, its parent is not recorded (never
    truncated, which would name another account), and the run says which."""
    long_parent = "p" * (GL_CODE_MAX_LENGTH + 1)
    await _add(realdb, code="6200", name="Rent", parent_code="Occupancy", erp_account_id="62")
    result = await _sync(
        realdb,
        [
            _acct("6100", "61", parent_code=long_parent),
            _acct("6200", "62", parent_code=long_parent),
            _acct("6300", "63", parent_code="Overheads"),
        ],
    )

    chart = await _rows(realdb)
    assert (chart["6100"].erp_account_id, chart["6100"].parent_code) == ("61", None)
    assert chart["6200"].parent_code == "Occupancy"  # kept, not overwritten
    assert chart["6300"].parent_code == "Overheads"
    assert result.created == 2
    assert result.parent_code_dropped == ["6100", "6200"]
    assert result.summary()["parent_code_dropped_count"] == 2


async def test_a_reported_parent_is_named_by_our_code_and_a_top_level_one_clears_it(realdb):
    """Seen live against QuickBooks: a parent was recorded under the ERP's code
    ("6020") while our row for that account kept its own ("Office Expenses"),
    and an account the ERP files at top level kept the parent a merged row had
    carried. An adapter that reports parentage settles both; one that does not
    leaves our parent alone."""
    await _add(realdb, code="Office Expenses", name="Office Expenses", erp_account_id="15")
    await _add(
        realdb, code="Rental", name="Rental", parent_code="Job Expenses", erp_account_id="29"
    )
    await _add(realdb, code="Fees", name="Fees", parent_code="Overheads", erp_account_id="30")
    await _sync(
        realdb,
        [
            _acct("6020", "15"),
            _acct(
                "Office Expenses:Software",
                "16",
                parent_code="6020",
                parent_erp_account_id="15",
                reports_parent=True,
            ),
            _acct("Rental", "29", reports_parent=True),
            _acct("Fees", "30"),  # parentage not reported
            # A parent we hold no row for is named by the ERP's code.
            _acct(
                "Child", "40", parent_code="Unheld", parent_erp_account_id="99", reports_parent=True
            ),
        ],
    )

    chart = await _rows(realdb)
    assert chart["Office Expenses:Software"].parent_code == "Office Expenses"
    assert chart["Rental"].parent_code is None
    assert chart["Fees"].parent_code == "Overheads"
    assert chart["Child"].parent_code == "Unheld"


# ---------------------------------------------------------------------------
# Repair of a chart a pre-§284 sync collapsed
# ---------------------------------------------------------------------------


async def test_a_collapsed_legacy_row_is_unlinked_and_each_account_gets_its_own(realdb):
    """The live state: one "Job Materials" row, shown under Job Expenses but
    linked to Landscaping's account (the later one won). Its id cannot say
    which account its invoices meant, so the link goes — those invoices are
    then refused `account_not_linked` and recoded — and both real accounts get
    rows under their real codes. The row itself stays, for its invoices."""
    legacy_id = await _add(
        realdb,
        code="Job Materials",
        name="Job Materials",
        parent_code="Job Expenses",
        erp_account_id="1100",
    )
    result = await _sync(realdb, await _qbo_payloads(_qbo_chart()))

    assert result.unlinked == ["Job Materials"]
    assert result.conflicts == []
    chart = await _rows(realdb)
    assert chart["Job Materials"].id == legacy_id
    assert chart["Job Materials"].erp_account_id is None
    assert chart["Job Expenses:Job Materials"].erp_account_id == "1000"
    assert chart["Landscaping Services:Job Materials"].erp_account_id == "1100"
    async with realdb.sessionmaker(TENANT)() as s:
        ids = await resolve_erp_account_ids(s, realdb.info(TENANT).org_id, None, ["Job Materials"])
    assert ids == {}

    # And it stays unlinked: a later pull never re-attaches it on a guess.
    again = await _sync(realdb, await _qbo_payloads(_qbo_chart()))
    assert (again.unlinked, again.linked) == ([], 0)
    assert (await _rows(realdb))["Job Materials"].erp_account_id is None


async def test_a_legacy_row_only_one_account_answers_to_keeps_its_link(realdb):
    """A sub-account whose name no other account shares was never merged: it
    stays linked, keeps its code, and its drift is reported."""
    rows = _qbo_chart()
    rows.append(
        {
            "Id": "77",
            "Name": "Bookkeeper",
            "FullyQualifiedName": "Account 00:Bookkeeper",
            "ParentRef": {"value": "100"},
            "Classification": "Expense",
        }
    )
    await _add(realdb, code="Bookkeeper", name="Bookkeeper", erp_account_id="77")
    result = await _sync(realdb, await _qbo_payloads(rows))
    assert result.unlinked == []
    assert {"code": "Bookkeeper", "erp_code": "Account 00:Bookkeeper"} in result.code_drift
    assert (await _rows(realdb))["Bookkeeper"].erp_account_id == "77"


async def test_a_merged_row_linked_to_the_sub_account_is_unlinked(realdb):
    """A top-level "Job Materials" and one sub-account of that name were both
    keyed "Job Materials" before §284, and the row ended up on the
    sub-account. The top-level account's name is still a claim on the row, so
    it is unlinked rather than left posting to the sub-account."""
    rows = [
        {"Id": "5", "Name": "Job Materials", "FullyQualifiedName": "Job Materials"},
        {"Id": "60", "Name": "Job Expenses", "FullyQualifiedName": "Job Expenses"},
        {
            "Id": "1000",
            "Name": "Job Materials",
            "FullyQualifiedName": "Job Expenses:Job Materials",
            "ParentRef": {"value": "60"},
        },
    ]
    await _add(realdb, code="Job Materials", name="Job Materials", erp_account_id="1000")
    result = await _sync(realdb, await _qbo_payloads(rows))

    assert result.unlinked == ["Job Materials"]
    assert result.link_changes == [{"code": "Job Materials", "from": "1000", "to": None}]
    conflict = next(c for c in result.conflicts if c["code"] == "Job Materials")
    assert conflict["reason"] == AMBIGUOUS_LEGACY
    chart = await _rows(realdb)
    assert chart["Job Materials"].erp_account_id is None
    assert chart["Job Expenses:Job Materials"].erp_account_id == "1000"


async def test_an_unlinked_row_a_legacy_code_names_twice_is_not_relinked(realdb):
    """A manual "Job Materials" row and a top-level QuickBooks account of that
    name: two sub-accounts were ALSO once keyed "Job Materials", so which one
    the row's invoices meant is unknowable — refused, not guessed."""
    rows = _qbo_chart()
    rows.append(
        {
            "Id": "5",
            "Name": "Job Materials",
            "FullyQualifiedName": "Job Materials",
            "Classification": "Expense",
        }
    )
    await _add(realdb, code="Job Materials", name="Job Materials")
    result = await _sync(realdb, await _qbo_payloads(rows))
    conflict = next(c for c in result.conflicts if c["code"] == "Job Materials")
    assert conflict["reason"] == AMBIGUOUS_LEGACY
    assert conflict["erp_account_ids"] == ["1000", "1100", "5"]
    assert (await _rows(realdb))["Job Materials"].erp_account_id is None


async def test_every_merged_row_in_scope_is_unlinked_not_just_the_preferred(realdb):
    """Multi-entity: a shared "Job Materials" and an entity's own override can
    both hold the merged link. Unlinking only the preferred one would leave the
    other posting to Landscaping for every invoice that resolves to it."""
    from app.models.entity import Entity

    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        sub = Entity(organization_id=org_id, name="Sub", slug=f"sub-{uuid.uuid4().hex[:6]}")
        s.add(sub)
        await s.commit()
        sub_id = sub.id
    await _add(realdb, code="Job Materials", name="Shared", erp_account_id="1100")
    await _add(realdb, code="Job Materials", name="Own", erp_account_id="1100", entity_id=sub_id)

    result = await _sync(realdb, await _qbo_payloads(_qbo_chart()), entity_id=sub_id)

    assert result.unlinked == ["Job Materials", "Job Materials"]
    async with realdb.sessionmaker(TENANT)() as s:
        legacy = (
            (await s.execute(select(GLAccount).where(GLAccount.code == "Job Materials")))
            .scalars()
            .all()
        )
    assert [r.erp_account_id for r in legacy] == [None, None]


async def test_the_audit_row_records_every_link_change_uncapped(realdb):
    """Where each row's bills posted before and where they post now, for every
    row — the response caps its lists at 50, the audit row does not."""
    for i in range(60):
        await _add(realdb, code=f"7{i:03d}", name=f"A{i}", erp_account_id=f"SANDBOX-{i}")
    result = await _sync(realdb, [_acct(f"7{i:03d}", f"PROD-{i}") for i in range(60)])
    assert result.linked == 60
    assert len(result.summary()["link_changes"]) == 50
    full = result.summary(capped=False)["link_changes"]
    assert len(full) == 60
    assert {"code": "7000", "from": "SANDBOX-0", "to": "PROD-0"} in full


# ---------------------------------------------------------------------------
# Through the endpoint
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def qbo_configured(realdb):
    from app.models.organization import Organization

    mk = realdb.control_sessionmaker()
    org_id = realdb.info(TENANT).org_id

    async def _set(value):
        async with mk() as s:
            org = (
                await s.execute(select(Organization).where(Organization.id == org_id))
            ).scalar_one()
            saved = org.settings
            org.settings = value
            await s.commit()
            return saved

    saved = await _set({"erp": {"type": "mock", "integration_method": "direct"}})
    try:
        yield
    finally:
        await _set(saved)


async def test_endpoint_reports_the_live_chart_as_89_new_none_updated(realdb, qbo_configured):
    accounts = await _qbo_payloads(_qbo_chart())

    class _Adapter:
        erp_type = "quickbooks_online"

        async def list_gl_accounts(self):
            return accounts

    with patch("app.services.erp_adapters.get_erp_adapter", return_value=_Adapter()):
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            resp = await c.post("/api/gl-accounts/sync-erp")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["created"], body["updated"], body["conflict_count"]) == (89, 0, 0)
    assert body["message"] == "Synced 89 new, 0 updated GL accounts"


async def test_endpoint_reports_conflicts_by_code_never_as_updated(realdb, qbo_configured):
    class _Adapter:
        erp_type = "merge_dev"

        async def list_gl_accounts(self):
            return [_acct("6100", "A"), _acct("6100", "B")]

    with patch("app.services.erp_adapters.get_erp_adapter", return_value=_Adapter()):
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            resp = await c.post("/api/gl-accounts/sync-erp")
    body = resp.json()
    assert (body["created"], body["updated"], body["conflict_count"]) == (0, 0, 1)
    assert body["conflicts"] == [
        {"code": "6100", "reason": DUPLICATE_CODE, "erp_account_ids": ["A", "B"]}
    ]
    assert body["message"] == "Synced 0 new, 0 updated GL accounts; 1 not linked, 0 unlinked"

    from app.models.workflow import AuditLog

    async with realdb.sessionmaker(TENANT)() as s:
        row = (
            await s.execute(select(AuditLog).where(AuditLog.action == "gl_account.synced_from_erp"))
        ).scalar_one()
    assert row.details["conflicts"] == body["conflicts"]
    assert row.details["adapter"] == "merge_dev"
