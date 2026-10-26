"""An ERP link counts only in the ERP company it was made in (docs/decisions.md §284).

QuickBooks numbers accounts AND vendors per company with small integers, so a
sandbox company and a production company share most ids. Before migration 0115
a tenant that reconnected to another company kept every link: our "Office
Supplies" row, linked to sandbox "7", posted to production "7" (Advertising),
and the next sync matched the row by that id and renamed it — with no
``link_changes`` entry. Every case here uses COLLIDING numeric ids: "7" means
one account in the sandbox realm and another in production.

Also here: the chart sync's repair over the whole organization, inactive and
id-less accounts, and the person-confirmed link for a row the sync will not
guess about (the live sandbox's "Depreciation").
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update

from app.models.gl_account import GLAccount
from app.models.organization import Organization
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.services.erp_adapters.base import (
    ErpListingIncomplete,
    GLAccountPayload,
    ListingGap,
    VendorPayload,
)
from app.services.erp_adapters.catalog import merge_erp_update
from app.services.erp_link_realm import (
    LEGACY_REALM_KEY,
    connection_realm,
    legacy_links_trusted,
    link_is_live,
)
from app.services.gl_account_sync import (
    AMBIGUOUS_LEGACY,
    CODE_TAKEN,
    MISSING_ERP_ID,
    sync_chart_from_erp,
)
from app.services.gl_chart import resolve_erp_account_ids
from app.services.vendor_sync import sync_vendors_from_erp

TENANT = "a"
SANDBOX = "quickbooks_online:4620816365000000001"
PROD = "quickbooks_online:9130350000000000002"


def _qbo(realm_id: str, **extra) -> dict:
    return {
        "type": "quickbooks_online",
        "integration_method": "direct",
        "oauth": {"provider": "quickbooks_online", "external_tenant_id": realm_id},
        **extra,
    }


SANDBOX_CFG = _qbo("4620816365000000001")
PROD_CFG = _qbo("9130350000000000002")


def _acct(code, erp_id, name=None, **kw) -> GLAccountPayload:
    return GLAccountPayload(code=code, name=name or code, erp_account_id=erp_id, **kw)


async def _sync(realdb, accounts, *, realm, entity_id=None):
    async with realdb.sessionmaker(TENANT)() as s:
        result = await sync_chart_from_erp(
            s,
            organization_id=realdb.info(TENANT).org_id,
            entity_id=entity_id,
            accounts=accounts,
            realm=realm,
        )
        await s.commit()
    return result


async def _add(realdb, model=GLAccount, **kwargs):
    async with realdb.sessionmaker(TENANT)() as s:
        row = model(organization_id=realdb.info(TENANT).org_id, **kwargs)
        s.add(row)
        await s.commit()
        return row.id


async def _chart(realdb) -> dict[str, GLAccount]:
    async with realdb.sessionmaker(TENANT)() as s:
        return {r.code: r for r in (await s.execute(select(GLAccount))).scalars().all()}


async def _vendors(realdb) -> dict[str, Vendor]:
    async with realdb.sessionmaker(TENANT)() as s:
        return {v.name: v for v in (await s.execute(select(Vendor))).scalars().all()}


async def _set_erp(realdb, erp: dict | None):
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        org = (
            await s.execute(
                select(Organization).where(Organization.id == realdb.info(TENANT).org_id)
            )
        ).scalar_one()
        saved = org.settings
        settings = dict(org.settings or {})
        if erp is None:
            settings.pop("erp", None)
        else:
            settings["erp"] = erp
        org.settings = settings
        await s.commit()
        return saved


@pytest_asyncio.fixture
async def restore_settings(realdb):
    saved = await _set_erp(realdb, None)
    try:
        yield
    finally:
        mk = realdb.control_sessionmaker()
        async with mk() as s:
            org = (
                await s.execute(
                    select(Organization).where(Organization.id == realdb.info(TENANT).org_id)
                )
            ).scalar_one()
            org.settings = saved
            await s.commit()


def _adapter(*, accounts=(), vendors=()):
    class _Adapter:
        erp_type = "quickbooks_online"

        async def list_gl_accounts(self):
            return list(accounts)

        async def list_vendors(self):
            return list(vendors)

    return patch("app.services.erp_adapters.get_erp_adapter", return_value=_Adapter())


# ---------------------------------------------------------------------------
# The connection realm
# ---------------------------------------------------------------------------


def test_each_adapter_names_its_company_from_non_secret_config():
    assert connection_realm(SANDBOX_CFG) == SANDBOX
    assert connection_realm(PROD_CFG) == PROD
    # No consent yet: no company can be named (and nothing can post).
    assert connection_realm(_qbo("")) is None
    assert connection_realm({"type": "mock", "integration_method": "direct"}) is None
    assert connection_realm(None) is None
    ns = {"type": "netsuite", "integration_method": "direct", "account_id": "TSTDRV1 "}
    assert connection_realm(ns) == "netsuite:tstdrv1"
    bc = {
        "type": "dynamics_365_bc",
        "integration_method": "direct",
        "tenant_id": "T",
        "company_id": "C",
    }
    # The adapter's own default environment is the same company as naming it.
    assert connection_realm(bc) == connection_realm({**bc, "environment": "production"})
    assert connection_realm(bc) != connection_realm({**bc, "environment": "sandbox"})
    # Merge.dev ids are Merge-assigned UUIDs, unique across linked accounts.
    assert connection_realm({"integration_method": "merge_dev"}) == "merge_dev:"
    # Secrets never shape the realm (they are not stored on rows or audit rows).
    assert connection_realm({**ns, "token_secret": "s1"}) == connection_realm(
        {**ns, "token_secret": "s2"}
    )


def test_a_null_realm_link_is_trusted_only_while_the_snapshot_still_names_the_company():
    migrated_on_sandbox = {**SANDBOX_CFG, LEGACY_REALM_KEY: SANDBOX}
    assert legacy_links_trusted(migrated_on_sandbox, SANDBOX)
    # Reconnected to production since the migration: the old links are not.
    assert not legacy_links_trusted({**PROD_CFG, LEGACY_REALM_KEY: SANDBOX}, PROD)
    # No snapshot (no company was connected at migration): never trusted.
    assert not legacy_links_trusted(PROD_CFG, PROD)
    assert link_is_live(None, PROD, trust_legacy=True)
    assert not link_is_live(None, PROD, trust_legacy=False)
    assert not link_is_live(SANDBOX, PROD, trust_legacy=True)
    assert link_is_live(SANDBOX, None, trust_legacy=False)  # nothing nameable: unfiltered


def test_a_settings_save_never_sets_or_drops_the_snapshot_for_the_same_erp():
    stored = {**SANDBOX_CFG, LEGACY_REALM_KEY: SANDBOX}
    incoming = {
        "type": "quickbooks_online",
        "integration_method": "direct",
        LEGACY_REALM_KEY: PROD,  # a forged "these links are production's"
        "environment": "production",
    }
    assert merge_erp_update(stored, incoming)[LEGACY_REALM_KEY] == SANDBOX
    # Another ERP: the old links belong to neither, so the snapshot goes.
    other = {"type": "xero", "integration_method": "direct", LEGACY_REALM_KEY: "xero:x"}
    assert LEGACY_REALM_KEY not in merge_erp_update(stored, other)


# ---------------------------------------------------------------------------
# Chart sync: colliding ids across companies
# ---------------------------------------------------------------------------


async def test_a_sandbox_link_is_never_matched_by_the_colliding_production_id(realdb):
    """Sandbox "7" is Office Supplies; production "7" is Advertising. The row
    keeps its name, is re-linked by its code to production's Office Supplies,
    and the move is recorded; Advertising gets its own row."""
    row_id = await _add(
        realdb,
        code="Office Supplies",
        name="Office Supplies",
        erp_account_id="7",
        erp_link_realm=SANDBOX,
    )
    result = await _sync(
        realdb,
        [_acct("Advertising", "7"), _acct("Office Supplies", "15")],
        realm=PROD,
    )

    chart = await _chart(realdb)
    office = chart["Office Supplies"]
    assert office.id == row_id
    assert (office.name, office.erp_account_id, office.erp_link_realm) == (
        "Office Supplies",
        "15",
        PROD,
    )
    assert chart["Advertising"].erp_account_id == "7"
    assert chart["Advertising"].erp_link_realm == PROD
    assert {"code": "Office Supplies", "from": "7", "to": "15"} in result.link_changes
    assert result.code_drift == []  # it was never matched by the colliding id


async def test_a_sandbox_link_with_no_counterpart_is_unlinked_and_recorded(realdb):
    await _add(
        realdb,
        code="Office Supplies",
        name="Office Supplies",
        erp_account_id="7",
        erp_link_realm=SANDBOX,
    )
    result = await _sync(realdb, [_acct("Advertising", "7")], realm=PROD)

    office = (await _chart(realdb))["Office Supplies"]
    assert (office.erp_account_id, office.erp_link_realm) == (None, None)
    assert {"code": "Office Supplies", "from": "7", "to": None} in result.link_changes
    assert "Office Supplies" in result.unlinked


async def test_the_same_id_holding_the_same_code_in_the_new_company_is_kept(realdb):
    """A production company copied from the sandbox: "7" is Office Supplies in
    both. Evidence, not history, keeps the link — and the move is recorded."""
    await _add(
        realdb,
        code="Office Supplies",
        name="Office Supplies",
        erp_account_id="7",
        erp_link_realm=SANDBOX,
    )
    result = await _sync(realdb, [_acct("Office Supplies", "7")], realm=PROD)

    office = (await _chart(realdb))["Office Supplies"]
    assert (office.erp_account_id, office.erp_link_realm) == ("7", PROD)
    assert result.link_changes == [
        {
            "code": "Office Supplies",
            "from": "7",
            "to": "7",
            "from_realm": SANDBOX,
            "to_realm": PROD,
        }
    ]


async def test_a_move_between_companies_needs_the_current_code_not_a_legacy_name(realdb):
    """Sandbox "1000" was our "Job Materials" (keyed by its bare name before
    §284). In production "1000" is "Landscaping Services:Job Materials", whose
    legacy key is also "Job Materials" — a name QuickBooks lets two
    sub-accounts share, so a colliding id holding it proves nothing. The link
    is dropped and the account is offered for a person to confirm."""
    row_id = await _add(
        realdb,
        code="Job Materials",
        name="Job Materials",
        erp_account_id="1000",
        erp_link_realm=SANDBOX,
    )
    landscaping = _acct(
        "Landscaping Services:Job Materials", "1000", legacy_codes=("Job Materials",)
    )
    result = await _sync(realdb, [landscaping], realm=PROD)

    row = (await _chart(realdb))["Job Materials"]
    assert row.id == row_id
    assert (row.erp_account_id, row.erp_link_realm) == (None, None)
    assert row.erp_link_candidates == {
        "realm": PROD,
        "reason": AMBIGUOUS_LEGACY,
        "accounts": [
            {
                "erp_account_id": "1000",
                "code": "Landscaping Services:Job Materials",
                "name": "Landscaping Services:Job Materials",
            }
        ],
    }
    assert {"code": "Job Materials", "from": "1000", "to": None} in result.link_changes
    assert "Job Materials" in result.unlinked


async def test_a_pre_migration_link_still_takes_its_legacy_name_as_evidence(realdb):
    """A NULL-realm link is overwhelmingly into the company it is still
    connected to, where that legacy key is the row's own (§284's rule 4 then
    judges whether it is ambiguous)."""
    await _add(realdb, code="Job Materials", name="Job Materials", erp_account_id="1000")
    landscaping = _acct(
        "Landscaping Services:Job Materials", "1000", legacy_codes=("Job Materials",)
    )
    result = await _sync(realdb, [landscaping], realm=PROD)
    row = (await _chart(realdb))["Job Materials"]
    assert (row.erp_account_id, row.erp_link_realm) == ("1000", PROD)
    assert result.realm_adopted == 1


async def test_a_pre_migration_link_the_pull_confirms_is_adopted_silently(realdb):
    """A single-company tenant: its NULL-realm links are stamped, not churned."""
    await _add(realdb, code="6100", name="Office", erp_account_id="7")
    result = await _sync(realdb, [_acct("6100", "7")], realm=PROD)
    row = (await _chart(realdb))["6100"]
    assert (row.erp_account_id, row.erp_link_realm) == ("7", PROD)
    assert (result.realm_adopted, result.link_changes, result.unlinked) == (1, [], [])


async def test_a_pre_migration_link_to_a_colliding_id_is_rematched_by_code(realdb):
    """The tenant switched company before 0115 recorded anything: the NULL link
    to "7" is judged by evidence, not trusted."""
    await _add(realdb, code="Office Supplies", name="Office Supplies", erp_account_id="7")
    result = await _sync(
        realdb, [_acct("Advertising", "7"), _acct("Office Supplies", "15")], realm=PROD
    )
    chart = await _chart(realdb)
    assert chart["Office Supplies"].erp_account_id == "15"
    assert chart["Office Supplies"].name == "Office Supplies"
    assert result.realm_adopted == 0
    assert {"code": "Office Supplies", "from": "7", "to": "15"} in result.link_changes


async def test_the_push_never_resolves_a_link_from_another_company(realdb):
    await _add(
        realdb,
        code="Office Supplies",
        name="Office Supplies",
        erp_account_id="7",
        erp_link_realm=SANDBOX,
    )
    await _add(realdb, code="Legacy", name="Legacy", erp_account_id="8")
    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        codes = ["Office Supplies", "Legacy"]
        assert await resolve_erp_account_ids(s, org_id, None, codes, realm=PROD) == {}
        assert await resolve_erp_account_ids(
            s, org_id, None, codes, realm=PROD, trust_legacy=True
        ) == {"Legacy": "8"}
        assert await resolve_erp_account_ids(s, org_id, None, codes, realm=SANDBOX) == {
            "Office Supplies": "7"
        }


# ---------------------------------------------------------------------------
# The push
# ---------------------------------------------------------------------------


def _invoice(org_id, vendor_id, **kw):
    return SimpleNamespace(
        organization_id=org_id,
        vendor_id=vendor_id,
        entity_id=None,
        gl_account=kw.get("gl_account", "Office Supplies"),
    )


async def test_the_push_refuses_a_vendor_and_account_linked_in_the_sandbox(realdb):
    from app.services.erp import _resolve_erp_refs

    org_id = realdb.info(TENANT).org_id
    vendor_id = await _add(realdb, Vendor, name="Acme", erp_vendor_id="57", erp_link_realm=SANDBOX)
    await _add(
        realdb,
        code="Office Supplies",
        name="Office Supplies",
        erp_account_id="7",
        erp_link_realm=SANDBOX,
    )
    async with realdb.sessionmaker(TENANT)() as s:
        refs = await _resolve_erp_refs(s, _invoice(org_id, vendor_id), [], PROD_CFG)
        assert (refs.vendor_erp_id, refs.account_erp_ids) == (None, {})
        same = await _resolve_erp_refs(s, _invoice(org_id, vendor_id), [], SANDBOX_CFG)
        assert (same.vendor_erp_id, same.account_erp_ids) == ("57", {"Office Supplies": "7"})


NS_CFG = {"type": "netsuite", "integration_method": "direct", "account_id": "TSTDRV1"}
NS = "netsuite:tstdrv1"
NS_OTHER_CFG = {**NS_CFG, "account_id": "TSTDRV2"}


async def test_the_push_trusts_pre_migration_links_only_on_the_snapshotted_company(realdb):
    """A non-QuickBooks org: 0115 snapshotted its company, so its NULL links keep
    posting there and nowhere else."""
    from app.services.erp import _resolve_erp_refs

    org_id = realdb.info(TENANT).org_id
    vendor_id = await _add(realdb, Vendor, name="Acme", erp_vendor_id="57")
    await _add(realdb, code="Office Supplies", name="Office Supplies", erp_account_id="7")
    migrated = {**NS_CFG, LEGACY_REALM_KEY: NS}
    moved = {**NS_OTHER_CFG, LEGACY_REALM_KEY: NS}
    async with realdb.sessionmaker(TENANT)() as s:
        kept = await _resolve_erp_refs(s, _invoice(org_id, vendor_id), [], migrated)
        assert (kept.vendor_erp_id, kept.account_erp_ids) == ("57", {"Office Supplies": "7"})
        gone = await _resolve_erp_refs(s, _invoice(org_id, vendor_id), [], moved)
        assert (gone.vendor_erp_id, gone.account_erp_ids) == (None, {})


async def test_a_quickbooks_org_refuses_pre_migration_links_until_a_sync_adopts_them(
    realdb, restore_settings
):
    """QuickBooks' pre-0115 links are the ones the name-keyed sync merged, so
    0115 snapshots no company for a QuickBooks org: even on the company it never
    left, a NULL link is refused until the chart and vendor syncs re-judge it."""
    from app.services.erp import _resolve_erp_refs

    await _set_erp(realdb, SANDBOX_CFG)
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _add(realdb, Vendor, name="Acme", erp_vendor_id="57")
    await _add(realdb, code="Office Supplies", name="Office Supplies", erp_account_id="7")
    erp = await _run_migration_snapshot(realdb)
    assert LEGACY_REALM_KEY not in erp
    assert not legacy_links_trusted(erp, SANDBOX)

    async with realdb.sessionmaker(TENANT)() as s:
        refs = await _resolve_erp_refs(s, _invoice(org_id, vendor_id), [], erp)
    assert (refs.vendor_erp_id, refs.account_erp_ids) == (None, {})

    await _sync(realdb, [_acct("Office Supplies", "7")], realm=SANDBOX)
    await _vsync(realdb, [_vendor("57", "Acme")], realm=SANDBOX)
    async with realdb.sessionmaker(TENANT)() as s:
        refs = await _resolve_erp_refs(s, _invoice(org_id, vendor_id), [], erp)
    assert (refs.vendor_erp_id, refs.account_erp_ids) == ("57", {"Office Supplies": "7"})


def test_an_account_refusal_names_the_header_only_when_a_line_falls_back_to_it():
    from app.services.erp import ErpRefs, _unlinked_accounts

    inv = SimpleNamespace(gl_account="HEADER")
    coded = [SimpleNamespace(gl_account="6100"), SimpleNamespace(gl_account="6200")]
    refs = ErpRefs(account_erp_ids={"6200": "X"})
    assert _unlinked_accounts(inv, coded, refs) == ["6100"]
    uncoded = [*coded, SimpleNamespace(gl_account=None)]
    assert _unlinked_accounts(inv, uncoded, refs) == ["6100", "HEADER"]
    assert _unlinked_accounts(inv, [], refs) == ["HEADER"]


# ---------------------------------------------------------------------------
# Vendor sync: colliding ids across companies
# ---------------------------------------------------------------------------


def _vendor(erp_id, name, **kw) -> dict:
    return {"erp_vendor_id": erp_id, "name": name, **kw}


async def _vsync(realdb, records, *, realm):
    async with realdb.sessionmaker(TENANT)() as s:
        result = await sync_vendors_from_erp(s, realdb.info(TENANT).org_id, records, realm=realm)
        await s.commit()
    return result


async def test_a_sandbox_vendor_link_is_never_matched_by_the_colliding_production_id(realdb):
    """Sandbox "57" is Acme; production "57" is Squeaky Kleen. Acme keeps its
    name and tax id, is re-linked by name to production's Acme, and Squeaky
    Kleen becomes its own payee."""
    acme_id = await _add(
        realdb,
        Vendor,
        name="Acme",
        tax_id="12-3456789",
        erp_vendor_id="57",
        erp_link_realm=SANDBOX,
        source="erp_sync",
    )
    result = await _vsync(
        realdb,
        [_vendor("57", "Squeaky Kleen Car Wash"), _vendor("60", "Acme")],
        realm=PROD,
    )

    vendors = await _vendors(realdb)
    acme = vendors["Acme"]
    assert acme.id == acme_id
    assert (acme.erp_vendor_id, acme.erp_link_realm, acme.tax_id) == ("60", PROD, "12-3456789")
    assert vendors["Squeaky Kleen Car Wash"].erp_vendor_id == "57"
    assert result["unlinked"] == ["Acme"]
    assert result["created"] == 1

    async with realdb.sessionmaker(TENANT)() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "vendor.synced_from_erp")
                    .where(AuditLog.entity_id == acme_id)
                )
            )
            .scalars()
            .all()
        )
    by_change = {r.details["change"]: r.details for r in rows}
    assert {c: d["erp_vendor_id"] for c, d in by_change.items()} == {
        "unlinked": "57",
        "linked": "60",
    }
    assert by_change["unlinked"]["from_realm"] == SANDBOX


async def test_a_vendor_move_between_companies_needs_evidence_unique_in_the_pull(realdb):
    """Production has two "Acme" records, one at sandbox Acme's colliding "57".
    A name two records share does not say which one our Acme is, so the link is
    dropped and the name rule reports the ambiguity instead of guessing."""
    await _add(
        realdb, Vendor, name="Acme", erp_vendor_id="57", erp_link_realm=SANDBOX, source="erp_sync"
    )
    result = await _vsync(realdb, [_vendor("57", "Acme"), _vendor("60", "Acme")], realm=PROD)

    acme = (await _vendors(realdb))["Acme"]
    assert (acme.erp_vendor_id, acme.erp_link_realm) == (None, None)
    assert result["unlinked"] == ["Acme"]
    assert result["realm_adopted"] == 0
    assert result["conflicts"] == [
        {"name": "Acme", "reason": "ambiguous_name", "erp_vendor_ids": ["57", "60"]}
    ]
    # A tax id only this record holds is still evidence.
    await _add(
        realdb,
        Vendor,
        name="Beta",
        tax_id="98-7654321",
        erp_vendor_id="70",
        erp_link_realm=SANDBOX,
        source="erp_sync",
    )
    await _vsync(
        realdb,
        [_vendor("70", "Beta Trading", tax_id="98-7654321"), _vendor("71", "Beta Trading")],
        realm=PROD,
    )
    beta = (await _vendors(realdb))["Beta Trading"]
    assert (beta.erp_vendor_id, beta.erp_link_realm) == ("70", PROD)


async def test_a_pre_migration_vendor_link_the_pull_confirms_is_adopted(realdb):
    await _add(realdb, Vendor, name="Acme Ltd", erp_vendor_id="57", source="erp_sync")
    result = await _vsync(realdb, [_vendor("57", "ACME  ltd")], realm=PROD)
    acme = (await _vendors(realdb))["ACME  ltd"]
    assert (acme.erp_vendor_id, acme.erp_link_realm) == ("57", PROD)
    assert (result["realm_adopted"], result["unlinked"]) == (1, [])


async def test_an_inactive_erp_vendor_deactivates_its_payee_and_creates_none(realdb):
    """The live sandbox: Squeaky Kleen Car Wash (57) is inactive in QuickBooks."""
    await _add(
        realdb,
        Vendor,
        name="Squeaky Kleen Car Wash",
        erp_vendor_id="57",
        erp_link_realm=PROD,
        status="active",
        source="erp_sync",
    )
    result = await _vsync(
        realdb,
        [
            _vendor("57", "Squeaky Kleen Car Wash", active=False),
            _vendor("58", "Never Paid Co", active=False),
        ],
        realm=PROD,
    )
    vendors = await _vendors(realdb)
    assert vendors["Squeaky Kleen Car Wash"].status == "inactive"
    assert vendors["Squeaky Kleen Car Wash"].erp_vendor_id == "57"  # still the ERP's
    assert "Never Paid Co" not in vendors
    assert (result["deactivated"], result["created"]) == (1, 0)


async def test_the_old_qbo_acctnum_is_cleared_from_erp_created_vendors_only(realdb):
    """Before §284 the QuickBooks mapping wrote Vendor.AcctNum (our account
    number AT the vendor) into `code`, which CSV import resolves vendors by."""
    synced = await _add(
        realdb,
        Vendor,
        name="Acme",
        code="CUST-99812",
        erp_vendor_id="56",
        erp_link_realm=PROD,
        source="erp_sync",
    )
    await _add(
        realdb,
        Vendor,
        name="Globex",
        code="CUST-1",
        erp_vendor_id="57",
        erp_link_realm=PROD,
        source="manual",
    )
    result = await _vsync(
        realdb,
        [
            _vendor("56", "Acme", legacy_code="CUST-99812"),
            _vendor("57", "Globex", legacy_code="CUST-1"),
        ],
        realm=PROD,
    )
    vendors = await _vendors(realdb)
    assert vendors["Acme"].code is None
    assert vendors["Globex"].code == "CUST-1"  # a person may have chosen it
    assert result["codes_cleared"] == 1
    async with realdb.sessionmaker(TENANT)() as s:
        row = (
            await s.execute(
                select(AuditLog)
                .where(AuditLog.action == "vendor.synced_from_erp")
                .where(AuditLog.entity_id == synced)
            )
        ).scalar_one()
    assert row.details["code_cleared"] is True
    assert "CUST-99812" not in str(row.details)


async def test_vendor_endpoint_stamps_the_connection_realm(realdb, restore_settings):
    await _set_erp(realdb, PROD_CFG)
    await _add(
        realdb,
        Vendor,
        name="Acme",
        erp_vendor_id="57",
        erp_link_realm=SANDBOX,
        source="erp_sync",
    )
    vendors = [
        VendorPayload(erp_vendor_id="57", name="Squeaky Kleen Car Wash"),
        VendorPayload(erp_vendor_id="60", name="Acme"),
    ]
    with _adapter(vendors=vendors):
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            resp = await c.post("/api/vendors/sync-erp")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["realm"], body["unlinked"]) == (PROD, ["Acme"])
    assert "1 unlinked (linked under another ERP company)" in body["message"]
    vendors_now = await _vendors(realdb)
    assert vendors_now["Acme"].erp_vendor_id == "60"
    assert vendors_now["Squeaky Kleen Car Wash"].erp_link_realm == PROD


# ---------------------------------------------------------------------------
# An empty or incomplete pull proves nothing
# ---------------------------------------------------------------------------


async def _links_of_every_kind(realdb):
    """A pre-0115 (NULL-realm) link, a sandbox link, and a row waiting for a
    decision — every link state an absence-based pass could wrongly drop."""
    await _add(realdb, code="6100", name="Office", erp_account_id="7")
    await _add(
        realdb,
        code="Office Supplies",
        name="Office Supplies",
        erp_account_id="15",
        erp_link_realm=SANDBOX,
    )
    pending = {"realm": PROD, "reason": AMBIGUOUS_LEGACY, "accounts": [{"erp_account_id": "40"}]}
    await _add(realdb, code="Depreciation", name="Depreciation", erp_link_candidates=pending)
    return pending


async def test_an_empty_chart_pull_changes_no_link(realdb):
    """Before, rule 0 read every id's absence from an empty pull as "dead" and
    unlinked the whole chart; a transient adapter failure that degraded to []
    did exactly that on the first sync after migration 0115."""
    pending = await _links_of_every_kind(realdb)
    before = {
        c: (r.erp_account_id, r.erp_link_realm, r.erp_link_candidates)
        for c, r in (await _chart(realdb)).items()
    }

    result = await _sync(realdb, [], realm=PROD)
    # A pull of nothing but id-less accounts proves nothing either; it is
    # reported, never linked.
    id_less = await _sync(realdb, [_acct("9999", None)], realm=PROD)

    after = {
        c: (r.erp_account_id, r.erp_link_realm, r.erp_link_candidates)
        for c, r in (await _chart(realdb)).items()
    }
    assert after == before
    assert after["Depreciation"][2] == pending
    assert (result.unlinked, result.link_changes, result.conflicts) == ([], [], [])
    assert (id_less.unlinked, id_less.link_changes) == ([], [])
    assert id_less.conflicts == [{"code": "9999", "reason": MISSING_ERP_ID, "erp_account_ids": []}]


async def test_an_empty_vendor_pull_changes_no_link(realdb):
    await _add(realdb, Vendor, name="Acme", erp_vendor_id="57", source="erp_sync")
    await _add(
        realdb, Vendor, name="Beta", erp_vendor_id="58", erp_link_realm=SANDBOX, source="erp_sync"
    )
    result = await _vsync(realdb, [], realm=PROD)
    id_less = await _vsync(realdb, [_vendor(None, "Gamma")], realm=PROD)

    vendors = await _vendors(realdb)
    assert (vendors["Acme"].erp_vendor_id, vendors["Acme"].erp_link_realm) == ("57", None)
    assert (vendors["Beta"].erp_vendor_id, vendors["Beta"].erp_link_realm) == ("58", SANDBOX)
    assert "Gamma" not in vendors
    for r in (result, id_less):
        assert (r["unlinked"], r["created"], r["updated"], r["realm_adopted"]) == ([], 0, 0, 0)


async def test_a_pull_with_no_realm_leaves_every_named_company_link_alone(realdb):
    """A chart sync with the `mock` adapter (realm None) used to re-judge every
    real link against fixture data: our production "Office Supplies" (7) was
    dropped, or matched by a fixture's colliding "7" and renamed, and a
    pending decision was wiped. A pull that names no company judges none."""
    pending = {"realm": PROD, "reason": AMBIGUOUS_LEGACY, "accounts": [{"erp_account_id": "40"}]}
    await _add(
        realdb,
        code="Office Supplies",
        name="Office Supplies",
        erp_account_id="7",
        erp_link_realm=PROD,
    )
    await _add(realdb, code="Depreciation", name="Depreciation", erp_link_candidates=pending)

    result = await _sync(
        realdb,
        [
            _acct("Advertising", "7"),  # the fixture's colliding id
            _acct("Office Supplies", "99"),  # and a colliding code
            _acct("Depreciation", "40"),
            _acct("Brand New", "500"),
        ],
        realm=None,
    )

    chart = await _chart(realdb)
    office = chart["Office Supplies"]
    assert (office.name, office.erp_account_id, office.erp_link_realm) == (
        "Office Supplies",
        "7",
        PROD,
    )
    depreciation = chart["Depreciation"]
    assert (depreciation.erp_account_id, depreciation.erp_link_candidates) == (None, pending)
    # The fixture's own accounts land beside them, under no realm.
    assert (chart["Brand New"].erp_account_id, chart["Brand New"].erp_link_realm) == ("500", None)
    assert chart["Advertising"].erp_link_realm is None
    assert (result.unlinked, result.link_changes, result.code_drift) == ([], [], [])
    assert {(c["code"], c["reason"]) for c in result.conflicts} == {
        ("Office Supplies", CODE_TAKEN),
        ("Depreciation", CODE_TAKEN),
    }


async def test_a_vendor_pull_with_no_realm_leaves_named_company_links_alone(realdb):
    acme_id = await _add(
        realdb, Vendor, name="Acme", erp_vendor_id="57", erp_link_realm=PROD, source="erp_sync"
    )
    result = await _vsync(realdb, [_vendor("57", "Squeaky Kleen Car Wash")], realm=None)

    vendors = await _vendors(realdb)
    acme = vendors["Acme"]
    assert acme.id == acme_id
    assert (acme.erp_vendor_id, acme.erp_link_realm) == ("57", PROD)
    # The fixture's vendor is its own payee, not a rename of Acme.
    squeaky = vendors["Squeaky Kleen Car Wash"]
    assert squeaky.id != acme_id
    assert (squeaky.erp_vendor_id, squeaky.erp_link_realm) == ("57", None)
    assert (result["unlinked"], result["updated"], result["created"]) == ([], 0, 1)


def _failing_adapter():
    class _Adapter:
        erp_type = "quickbooks_online"

        async def list_gl_accounts(self):
            raise ErpListingIncomplete("QuickBooks Online", "Account", ListingGap.fetch_failed)

        async def list_vendors(self):
            raise ErpListingIncomplete("QuickBooks Online", "Vendor", ListingGap.truncated)

    return patch("app.services.erp_adapters.get_erp_adapter", return_value=_Adapter())


@pytest.mark.parametrize("path", ["/api/gl-accounts/sync-erp", "/api/vendors/sync-erp"])
async def test_a_listing_that_cannot_prove_itself_whole_syncs_nothing(
    realdb, restore_settings, path
):
    """The adapter raises rather than hand over part of a list; the endpoint
    answers 502 and every link stays exactly as it was."""
    await _set_erp(realdb, PROD_CFG)
    await _links_of_every_kind(realdb)
    await _add(realdb, Vendor, name="Acme", erp_vendor_id="57", source="erp_sync")
    chart_before = {
        c: (r.erp_account_id, r.erp_link_realm) for c, r in (await _chart(realdb)).items()
    }

    with _failing_adapter():
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            resp = await c.post(path)

    assert resp.status_code == 502, resp.text
    # Coded, never the exception's class name (followups: the sync screens).
    detail = resp.json()["detail"]
    assert detail["code"] == "erp_listing_incomplete"
    gap = "fetch_failed" if path.startswith("/api/gl-accounts") else "truncated"
    assert detail["params"] == {
        "provider": "QuickBooks Online",
        "gap": gap,
    }
    chart_after = {
        c: (r.erp_account_id, r.erp_link_realm) for c, r in (await _chart(realdb)).items()
    }
    assert chart_after == chart_before
    assert (await _vendors(realdb))["Acme"].erp_vendor_id == "57"


# ---------------------------------------------------------------------------
# Chart sync: whole-organization repair, inactive and id-less accounts
# ---------------------------------------------------------------------------


async def test_a_merged_row_in_another_entitys_chart_is_unlinked_too(realdb):
    """Rules 3 and 4 are about the ERP account, not the entity: a sync under
    subsidiary A still unlinks B's merged "Job Materials"."""
    from app.models.entity import Entity

    org_id = realdb.info(TENANT).org_id
    async with realdb.sessionmaker(TENANT)() as s:
        a = Entity(organization_id=org_id, name="A", slug=f"a-{uuid.uuid4().hex[:6]}")
        b = Entity(organization_id=org_id, name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
        s.add_all([a, b])
        await s.commit()
        a_id, b_id = a.id, b.id
    await _add(realdb, code="Job Materials", name="B's", erp_account_id="1100", entity_id=b_id)
    accounts = [
        _acct("Job Expenses:Job Materials", "1000", legacy_codes=("Job Materials",)),
        _acct("Landscaping:Job Materials", "1100", legacy_codes=("Job Materials",)),
    ]
    result = await _sync(realdb, accounts, realm=None, entity_id=a_id)

    assert result.unlinked == ["Job Materials"]
    async with realdb.sessionmaker(TENANT)() as s:
        b_row = (await s.execute(select(GLAccount).where(GLAccount.entity_id == b_id))).scalar_one()
    assert b_row.erp_account_id is None
    assert {a["erp_account_id"] for a in b_row.erp_link_candidates["accounts"]} == {
        "1000",
        "1100",
    }


async def test_an_inactive_account_keeps_its_link_and_retires_the_row(realdb):
    """The live sandbox: Bank Charges (8) is inactive in QuickBooks."""
    await _add(
        realdb,
        code="Bank Charges",
        name="Bank Charges",
        erp_account_id="8",
        erp_link_realm=PROD,
    )
    result = await _sync(
        realdb,
        [_acct("Bank Charges", "8", active=False), _acct("Old Retired", "9", active=False)],
        realm=PROD,
    )
    chart = await _chart(realdb)
    assert (chart["Bank Charges"].is_active, chart["Bank Charges"].erp_account_id) == (
        False,
        "8",
    )
    assert "Old Retired" not in chart  # never created
    assert result.deactivated == ["Bank Charges"]
    assert result.summary()["deactivated_count"] == 1
    again = await _sync(realdb, [_acct("Bank Charges", "8", active=False)], realm=PROD)
    assert (again.deactivated, again.unchanged) == ([], 1)


async def test_a_link_to_an_inactive_account_is_not_dead(realdb):
    """Inactive accounts are in the pull, so a row linked to one is never
    re-pointed at the active account that now shows its code."""
    await _add(realdb, code="6100", name="Office", erp_account_id="8", erp_link_realm=PROD)
    await _sync(realdb, [_acct("6100 old", "8", active=False), _acct("6100", "20")], realm=PROD)
    assert (await _chart(realdb))["6100"].erp_account_id == "8"


async def test_id_less_accounts_are_refused_never_linked_or_collapsed(realdb):
    await _add(realdb, code="6100", name="Office", erp_account_id="GONE", erp_link_realm=PROD)
    result = await _sync(
        realdb, [_acct("6100", None), _acct("6100", ""), _acct("6200", None)], realm=PROD
    )
    assert result.created == 0
    assert result.linked == 0
    assert {c["code"]: c["reason"] for c in result.conflicts} == {
        "6100": MISSING_ERP_ID,
        "6200": MISSING_ERP_ID,
    }
    assert "6200" not in await _chart(realdb)


# ---------------------------------------------------------------------------
# A person settles what the sync will not guess
# ---------------------------------------------------------------------------


def _depreciation_chart() -> list[GLAccountPayload]:
    """The live sandbox: a top-level "Depreciation" (40) and "Truck:Depreciation"
    (39) — both keyed "Depreciation" before §284."""
    return [
        _acct("Depreciation", "40", legacy_codes=("Depreciation",)),
        _acct("Truck:Depreciation", "39", legacy_codes=("Depreciation",)),
        _acct("Truck", "38", legacy_codes=("Truck",)),
    ]


async def test_an_ambiguous_row_waits_for_a_decision_with_its_candidates(realdb):
    """The old row "Depreciation" is already unlinked. Every sync used to refuse
    the top-level account forever, with no way out; now the row lists both
    accounts it could mean."""
    await _add(realdb, code="Depreciation", name="Depreciation")
    result = await _sync(realdb, _depreciation_chart(), realm=PROD)

    conflict = next(c for c in result.conflicts if c["code"] == "Depreciation")
    assert conflict["reason"] == AMBIGUOUS_LEGACY
    row = (await _chart(realdb))["Depreciation"]
    assert row.erp_account_id is None
    assert row.erp_link_candidates == {
        "realm": PROD,
        "reason": AMBIGUOUS_LEGACY,
        "accounts": [
            {"erp_account_id": "39", "code": "Truck:Depreciation", "name": "Truck:Depreciation"},
            {"erp_account_id": "40", "code": "Depreciation", "name": "Depreciation"},
        ],
    }


async def test_a_confirmed_link_is_kept_by_every_later_sync(realdb, restore_settings):
    await _set_erp(realdb, PROD_CFG)
    row_id = await _add(realdb, code="Depreciation", name="Depreciation")
    await _sync(realdb, _depreciation_chart(), realm=PROD)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        listed = (await c.get("/api/gl-accounts")).json()
        pending = next(a for a in listed if a["code"] == "Depreciation")
        assert [a["erp_account_id"] for a in pending["erp_link_candidates"]["accounts"]] == [
            "39",
            "40",
        ]
        assert "realm" not in pending["erp_link_candidates"]
        resp = await c.post(f"/api/gl-accounts/{row_id}/erp-link", json={"erp_account_id": "40"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["erp_account_id"], body["erp_link_candidates"]) == ("40", None)
    assert body["erp_link_confirmed_at"]

    async with realdb.sessionmaker(TENANT)() as s:
        audit = (
            await s.execute(
                select(AuditLog).where(AuditLog.action == "gl_account.erp_link_confirmed")
            )
        ).scalar_one()
    assert audit.entity_id == row_id
    assert audit.details["erp_account_id"] == "40"
    assert audit.details["candidates"] == ["39", "40"]

    again = await _sync(realdb, _depreciation_chart(), realm=PROD)
    assert again.unlinked == []
    assert all(c["code"] != "Depreciation" for c in again.conflicts)
    row = (await _chart(realdb))["Depreciation"]
    assert (row.erp_account_id, row.erp_link_realm) == ("40", PROD)
    assert row.erp_link_confirmed_at is not None
    # Cleared as SQL NULL, not a JSON `null`: "needs a decision" is a NULL
    # test everywhere outside the ORM (the e2e, an operator's query).
    async with realdb.sessionmaker(TENANT)() as s:
        pending = (
            await s.execute(
                text("SELECT count(*) FROM gl_accounts WHERE erp_link_candidates IS NOT NULL")
            )
        ).scalar_one()
    assert pending == 0


async def test_a_confirmed_sub_account_choice_survives_rule_4(realdb, restore_settings):
    """The row's invoices meant Truck:Depreciation. Rule 4 would unlink a row
    "Depreciation" posting to 39; a confirmed one it leaves alone."""
    await _set_erp(realdb, PROD_CFG)
    row_id = await _add(realdb, code="Depreciation", name="Depreciation")
    await _sync(realdb, _depreciation_chart(), realm=PROD)
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/gl-accounts/{row_id}/erp-link", json={"erp_account_id": "39"})
    assert resp.status_code == 200, resp.text
    again = await _sync(realdb, _depreciation_chart(), realm=PROD)
    assert again.unlinked == []
    chart = await _chart(realdb)
    assert chart["Depreciation"].erp_account_id == "39"
    assert chart["Truck:Depreciation"].erp_account_id == "39"
    # The row named after the account is the one the sync keeps current.
    assert chart["Depreciation"].name == "Depreciation"


async def test_the_confirm_endpoint_refuses_anything_but_an_offered_candidate(
    realdb, restore_settings
):
    await _set_erp(realdb, PROD_CFG)
    row_id = await _add(realdb, code="Depreciation", name="Depreciation")
    linked_id = await _add(
        realdb, code="6100", name="Office", erp_account_id="15", erp_link_realm=PROD
    )
    await _sync(realdb, [*_depreciation_chart(), _acct("6100", "15")], realm=PROD)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        guess = await c.post(f"/api/gl-accounts/{row_id}/erp-link", json={"erp_account_id": "38"})
        relink = await c.post(
            f"/api/gl-accounts/{linked_id}/erp-link", json={"erp_account_id": "15"}
        )
    assert guess.status_code == 422
    assert relink.status_code == 409

    # Candidates gathered under the sandbox are not production's.
    await _set_erp(realdb, SANDBOX_CFG)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        moved = await c.post(f"/api/gl-accounts/{row_id}/erp-link", json={"erp_account_id": "40"})
    assert moved.status_code == 409
    assert (await _chart(realdb))["Depreciation"].erp_account_id is None


async def _wait_for_lock_waiter(mk, *, timeout: float = 15.0) -> bool:
    """True once Postgres reports another backend here waiting on a lock.

    Asked of the server's own wait state, never slept for (the signal
    ``test_credit_memos.py`` waits on); the timeout only bounds a failure.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with mk() as s:
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() "
                        "AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if waiting:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_two_confirmations_at_once_link_the_row_once(realdb, restore_settings):
    """A second admin confirming another candidate while the first is mid-write
    must see the first link and be refused. Without ``FOR UPDATE`` on the read
    it passed the "not linked yet" check against the last committed row, its
    UPDATE queued behind the first, and then overwrote the first person's
    choice: last write wins, two audit rows naming different accounts."""
    await _set_erp(realdb, PROD_CFG)
    row_id = await _add(realdb, code="Depreciation", name="Depreciation")
    await _sync(realdb, _depreciation_chart(), realm=PROD)
    mk = realdb.sessionmaker(TENANT)

    async with mk() as held:
        # Stand-in for the first confirmation, holding the row mid-transaction.
        await held.execute(select(GLAccount).where(GLAccount.id == row_id).with_for_update())
        await held.execute(
            update(GLAccount)
            .where(GLAccount.id == row_id)
            .values(erp_account_id="39", erp_link_realm=PROD, erp_link_candidates=None)
        )
        async with realdb.client(key=TENANT, role="admin") as c:
            second = asyncio.create_task(
                c.post(f"/api/gl-accounts/{row_id}/erp-link", json={"erp_account_id": "40"})
            )
            assert await _wait_for_lock_waiter(mk), "the second confirmation never queued"
            await held.commit()
            resp = await second

    assert resp.status_code == 409, resp.text
    assert (await _chart(realdb))["Depreciation"].erp_account_id == "39"
    async with mk() as s:
        audits = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "gl_account.erp_link_confirmed")
            )
        ).scalar_one()
    assert audits == 0


async def test_the_confirm_endpoint_is_admin_or_ap_manager_only(realdb, restore_settings):
    await _set_erp(realdb, PROD_CFG)
    row_id = await _add(realdb, code="Depreciation", name="Depreciation")
    await _sync(realdb, _depreciation_chart(), realm=PROD)
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post(f"/api/gl-accounts/{row_id}/erp-link", json={"erp_account_id": "40"})
    assert resp.status_code == 403


async def test_gl_endpoint_stamps_the_realm_and_audits_every_move(realdb, restore_settings):
    await _set_erp(realdb, PROD_CFG)
    await _add(
        realdb,
        code="Office Supplies",
        name="Office Supplies",
        erp_account_id="7",
        erp_link_realm=SANDBOX,
    )
    with _adapter(accounts=[_acct("Advertising", "7"), _acct("Office Supplies", "15")]):
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            resp = await c.post("/api/gl-accounts/sync-erp")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["realm"] == PROD
    assert {"code": "Office Supplies", "from": "7", "to": "15"} in body["link_changes"]
    async with realdb.sessionmaker(TENANT)() as s:
        row = (
            await s.execute(select(AuditLog).where(AuditLog.action == "gl_account.synced_from_erp"))
        ).scalar_one()
    assert row.details["realm"] == PROD
    assert {"code": "Office Supplies", "from": "7", "to": "15"} in row.details["link_changes"]


# ---------------------------------------------------------------------------
# Migration 0115's control-plane step
# ---------------------------------------------------------------------------


def _migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0115_erp_link_realm.py"
    spec = importlib.util.spec_from_file_location("_m0115", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run_migration_snapshot(realdb) -> dict:
    """Run 0115's control-plane step; return the org's ``settings.erp`` after."""
    module = _migration()
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        await s.run_sync(lambda sync: module._snapshot_legacy_realms(sync.connection()))
        await s.commit()
    async with mk() as s:
        org = (
            await s.execute(
                select(Organization).where(Organization.id == realdb.info(TENANT).org_id)
            )
        ).scalar_one()
    return org.settings.get("erp") or {}


@pytest.mark.parametrize(
    ("erp", "expected"),
    [
        (NS_CFG, NS),
        ({"integration_method": "merge_dev", "type": "quickbooks_online"}, "merge_dev:"),
        # QuickBooks' NULL links are the merged ones: never trusted, on any company.
        (SANDBOX_CFG, None),
        ({"type": "mock", "integration_method": "direct"}, None),
        ({**PROD_CFG, LEGACY_REALM_KEY: "already"}, "already"),
    ],
)
async def test_the_migration_snapshots_the_company_each_org_is_connected_to(
    realdb, restore_settings, erp, expected
):
    await _set_erp(realdb, erp)
    assert (await _run_migration_snapshot(realdb)).get(LEGACY_REALM_KEY) == expected


def _disconnected_adapter(exc: Exception):
    class _Adapter:
        erp_type = "quickbooks_online"

        async def list_gl_accounts(self):
            raise exc

        async def list_vendors(self):
            raise exc

        async def list_pos(self):
            raise exc

        # The PO sync reads `list_pos_report` since decisions §323; this double
        # is not an `ErpAdapter`, so it gets no base default wrapping `list_pos`.
        async def list_pos_report(self):
            raise exc

    return patch("app.services.erp_adapters.get_erp_adapter", return_value=_Adapter())


_SYNC_PATHS = [
    "/api/gl-accounts/sync-erp",
    "/api/vendors/sync-erp",
    "/api/purchase-orders/sync-erp",
]


@pytest.mark.parametrize("path", _SYNC_PATHS)
async def test_a_sync_on_a_lost_connection_says_to_reconnect(realdb, restore_settings, path):
    """Live sandbox, 2026-10-19: a revoked QuickBooks consent answered every
    sync with the 502 "ERP request failed: ErpNotConnectedError". It is a
    coded 409 the browser states as "reconnect it under Organization"."""
    from app.services.erp_oauth import ErpNotConnectedError

    await _set_erp(realdb, PROD_CFG)
    with _disconnected_adapter(ErpNotConnectedError("quickbooks_online")):
        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post(path)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert (detail["code"], detail["params"]) == ("erp_not_connected", {})
    assert "ErpNotConnectedError" not in detail["message"]


@pytest.mark.parametrize("path", _SYNC_PATHS)
async def test_a_sync_whose_token_refresh_could_not_reach_the_provider_stays_502(
    realdb, restore_settings, path
):
    """An outage while refreshing is not a lost connection: nothing is marked
    for reconnect, and a later retry can succeed."""
    from app.services.erp_oauth import ErpTokenRefreshError

    await _set_erp(realdb, PROD_CFG)
    with _disconnected_adapter(ErpTokenRefreshError("quickbooks_online", "HTTP 503")):
        async with realdb.client(key=TENANT, role="admin") as c:
            resp = await c.post(path)

    assert resp.status_code == 502, resp.text
