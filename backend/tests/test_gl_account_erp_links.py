"""A GL account keeps its ERP link state for every company it was synced under
(docs/decisions.md §376, ``services/gl_account_erp_links``, migration 0127).

Before, a chart sync under another ERP company dropped a row's link, and a
return to the first company re-linked rows by code only: a link a person had
confirmed among candidates had to be confirmed again, and a row waiting for a
decision under a code no pulled account holds came back with no decision to
make at all — silently unlinked. Every case here uses COLLIDING numeric ids,
as QuickBooks does: "40" is one account in production and another in the
sandbox.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.gl_account import GLAccount, GLAccountErpLink
from app.models.organization import Organization
from app.services.erp_adapters.base import GLAccountPayload
from app.services.gl_account_sync import AMBIGUOUS_LEGACY, CODE_TAKEN, sync_chart_from_erp

TENANT = "a"
SANDBOX = "quickbooks_online:4620816365000000001"
PROD = "quickbooks_online:9130350000000000002"
PROD_CFG = {
    "type": "quickbooks_online",
    "integration_method": "direct",
    "oauth": {"provider": "quickbooks_online", "external_tenant_id": "9130350000000000002"},
}


def _acct(code, erp_id, name=None, **kw) -> GLAccountPayload:
    return GLAccountPayload(code=code, name=name or code, erp_account_id=erp_id, **kw)


def _depreciation_chart() -> list[GLAccountPayload]:
    """Production: a top-level "Depreciation" (40) and "Truck:Depreciation"
    (39), both keyed "Depreciation" before §284."""
    return [
        _acct("Depreciation", "40", legacy_codes=("Depreciation",)),
        _acct("Truck:Depreciation", "39", legacy_codes=("Depreciation",)),
        _acct("Truck", "38", legacy_codes=("Truck",)),
    ]


#: The sandbox: the same ids name other accounts.
SANDBOX_CHART = [_acct("Advertising", "40"), _acct("Fuel", "39"), _acct("Rent", "38")]


async def _sync(realdb, accounts, *, realm):
    async with realdb.sessionmaker(TENANT)() as s:
        result = await sync_chart_from_erp(
            s,
            organization_id=realdb.info(TENANT).org_id,
            entity_id=None,
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


async def _kept(realdb) -> set[tuple]:
    """Every kept state as (code, realm, erp id, confirmed?, candidate ids)."""
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(
            select(GLAccount.code, GLAccountErpLink).join(
                GLAccount, GLAccount.id == GLAccountErpLink.gl_account_id
            )
        )
        return {
            (
                code,
                k.realm,
                k.erp_account_id,
                k.erp_link_confirmed_at is not None,
                tuple(
                    a["erp_account_id"] for a in (k.erp_link_candidates or {}).get("accounts", [])
                ),
            )
            for code, k in rows.all()
        }


async def _org_settings(realdb, *, replace=False, settings=None) -> dict | None:
    """The org's settings as they were; with ``replace``, set to ``settings``."""
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(
                select(Organization).where(Organization.id == realdb.info(TENANT).org_id)
            )
        ).scalar_one()
        saved = org.settings
        if replace:
            org.settings = settings
            await s.commit()
        return saved


@pytest_asyncio.fixture
async def on_production(realdb):
    """The org connected to the production company, for the confirm
    endpoint (which checks the candidates' realm against the connection); the
    sync itself is driven directly, under whichever realm a test names."""
    saved = await _org_settings(realdb)
    await _org_settings(realdb, replace=True, settings={**(saved or {}), "erp": PROD_CFG})
    try:
        yield
    finally:
        await _org_settings(realdb, replace=True, settings=saved)


async def _confirm(realdb, row_id, erp_id):
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        return await c.post(f"/api/gl-accounts/{row_id}/erp-link", json={"erp_account_id": erp_id})


async def test_switching_company_and_back_restores_a_confirmed_link(realdb, on_production):
    """A person chose production "40" for the old row "Depreciation". The
    sandbox sync drops it ("40" is Advertising there) and keeps it for
    production; back on production it is restored, still confirmed, and the
    sync does not ask again."""
    row_id = await _add(realdb, code="Depreciation", name="Depreciation")
    await _sync(realdb, _depreciation_chart(), realm=PROD)
    resp = await _confirm(realdb, row_id, "40")
    assert resp.status_code == 200, resp.text

    away = await _sync(realdb, SANDBOX_CHART, realm=SANDBOX)
    row = (await _chart(realdb))["Depreciation"]
    assert (row.erp_account_id, row.erp_link_confirmed_at) == (None, None)
    assert "Depreciation" in away.unlinked
    assert ("Depreciation", PROD, "40", True, ()) in await _kept(realdb)

    back = await _sync(realdb, _depreciation_chart(), realm=PROD)
    row = (await _chart(realdb))["Depreciation"]
    assert row.id == row_id
    assert (row.erp_account_id, row.erp_link_realm) == ("40", PROD)
    assert row.erp_link_confirmed_at is not None
    assert row.erp_link_candidates is None
    assert all(c["code"] != "Depreciation" for c in back.conflicts)
    assert "Depreciation" not in back.unlinked
    assert back.restored >= 1
    assert {
        "code": "Depreciation",
        "from": None,
        "to": "40",
        "from_realm": None,
        "to_realm": PROD,
        "restored": True,
    } in back.link_changes
    kept = await _kept(realdb)
    # Nothing is left kept for production; the sandbox's own links are.
    assert all(realm != PROD for _, realm, *_ in kept)
    assert ("Advertising", SANDBOX, "40", False, ()) in kept


async def test_a_confirmed_share_survives_a_switch_and_back(realdb, on_production):
    """A person chose "Truck:Depreciation" (39) for the old row, which then
    shares 39 with the row named after it — the one share the rules allow.
    Both come back from the round trip."""
    row_id = await _add(realdb, code="Depreciation", name="Depreciation")
    await _sync(realdb, _depreciation_chart(), realm=PROD)
    assert (await _confirm(realdb, row_id, "39")).status_code == 200
    await _sync(realdb, _depreciation_chart(), realm=PROD)

    await _sync(realdb, SANDBOX_CHART, realm=SANDBOX)
    back = await _sync(realdb, _depreciation_chart(), realm=PROD)

    chart = await _chart(realdb)
    assert (chart["Depreciation"].erp_account_id, chart["Depreciation"].erp_link_realm) == (
        "39",
        PROD,
    )
    assert chart["Depreciation"].erp_link_confirmed_at is not None
    assert (
        chart["Truck:Depreciation"].erp_account_id,
        chart["Truck:Depreciation"].erp_link_realm,
    ) == (
        "39",
        PROD,
    )
    assert back.restore_dropped == []
    # As before the switch: the top-level account cannot own the code.
    assert ("Depreciation", CODE_TAKEN) in {(c["code"], c["reason"]) for c in back.conflicts}


async def test_a_row_waiting_with_candidates_gets_its_decision_back(realdb, on_production):
    """The old row "Job Materials" waits for a person to say which of two
    production sub-accounts it meant. No pulled account holds the code "Job
    Materials", so without the kept decision a return to production never
    visits the row again: it would sit unlinked with nothing to decide."""
    pending = {
        "realm": PROD,
        "reason": AMBIGUOUS_LEGACY,
        "accounts": [
            {"erp_account_id": "46", "code": "x", "name": "x"},
            {"erp_account_id": "63", "code": "y", "name": "y"},
        ],
    }
    row_id = await _add(
        realdb, code="Job Materials", name="Job Materials", erp_link_candidates=pending
    )

    await _sync(realdb, SANDBOX_CHART, realm=SANDBOX)
    assert (await _chart(realdb))["Job Materials"].erp_link_candidates is None
    assert ("Job Materials", PROD, None, False, ("46", "63")) in await _kept(realdb)

    production = [
        _acct("Landscaping Services:Job Materials", "46", legacy_codes=("Job Materials",)),
        _acct("Job Expenses:Job Materials", "63", legacy_codes=("Job Materials",)),
    ]
    back = await _sync(realdb, production, realm=PROD)
    row = (await _chart(realdb))["Job Materials"]
    assert row.erp_account_id is None
    # Restored, then refreshed against the pull like any open decision.
    assert row.erp_link_candidates == {
        "realm": PROD,
        "reason": AMBIGUOUS_LEGACY,
        "accounts": [
            {
                "erp_account_id": "46",
                "code": "Landscaping Services:Job Materials",
                "name": "Landscaping Services:Job Materials",
            },
            {
                "erp_account_id": "63",
                "code": "Job Expenses:Job Materials",
                "name": "Job Expenses:Job Materials",
            },
        ],
    }
    assert back.restored == 1

    # And the person can still make it.
    resp = await _confirm(realdb, row_id, "63")
    assert resp.status_code == 200, resp.text


async def test_a_switch_keeps_the_old_companys_link_even_when_evidence_moves_it(realdb):
    """A production company copied from the sandbox: "7" is Office Supplies in
    both, so rule 0 moves the link (§284). The sandbox's copy is kept too, so
    a return there restores it rather than re-judging it."""
    await _add(
        realdb,
        code="Office Supplies",
        name="Office Supplies",
        erp_account_id="7",
        erp_link_realm=SANDBOX,
    )
    await _sync(realdb, [_acct("Office Supplies", "7")], realm=PROD)
    row = (await _chart(realdb))["Office Supplies"]
    assert (row.erp_account_id, row.erp_link_realm) == ("7", PROD)
    assert await _kept(realdb) == {("Office Supplies", SANDBOX, "7", False, ())}


async def test_two_kept_links_claiming_one_id_are_neither_restored(realdb):
    """No two rows hold one account in one company on the rules' own
    judgement: which of two rows a kept "7" belongs to is a guess, so neither
    gets it back, and the rules link the row whose code the account holds."""
    a = await _add(realdb, code="6100", name="Office")
    b = await _add(realdb, code="6150", name="Office (old)")
    for gid in (a, b):
        await _add(realdb, GLAccountErpLink, gl_account_id=gid, realm=PROD, erp_account_id="7")

    result = await _sync(realdb, [_acct("6100", "7", name="Office")], realm=PROD)

    chart = await _chart(realdb)
    assert (chart["6100"].erp_account_id, chart["6100"].erp_link_realm) == ("7", PROD)
    assert chart["6150"].erp_account_id is None
    assert (result.restored, result.restore_dropped) == (0, ["6100", "6150"])
    assert await _kept(realdb) == set()
    await _assert_one_row_per_id_per_realm(realdb)


async def test_a_kept_link_another_row_holds_here_is_dropped(realdb):
    await _add(realdb, code="6100", name="Office", erp_account_id="7", erp_link_realm=PROD)
    other = await _add(realdb, code="6150", name="Office (old)")
    await _add(realdb, GLAccountErpLink, gl_account_id=other, realm=PROD, erp_account_id="7")

    result = await _sync(realdb, [_acct("6100", "7", name="Office")], realm=PROD)

    chart = await _chart(realdb)
    assert chart["6100"].erp_account_id == "7"
    assert chart["6150"].erp_account_id is None
    assert result.restore_dropped == ["6150"]
    await _assert_one_row_per_id_per_realm(realdb)


async def _assert_one_row_per_id_per_realm(realdb):
    async with realdb.sessionmaker(TENANT)() as s:
        shared = (
            await s.execute(
                text(
                    "SELECT erp_link_realm, erp_account_id FROM gl_accounts "
                    "WHERE erp_account_id IS NOT NULL "
                    "GROUP BY erp_link_realm, erp_account_id HAVING count(*) > 1"
                )
            )
        ).all()
    assert shared == []


async def test_a_link_with_no_realm_is_never_kept(realdb):
    """A pre-0115 link names no company; filing it under one would be a guess
    (§141). It is judged by rule 0 exactly as before."""
    await _add(realdb, code="Office Supplies", name="Office Supplies", erp_account_id="7")
    await _sync(realdb, [_acct("Advertising", "7")], realm=PROD)
    assert await _kept(realdb) == set()


async def test_a_pull_with_no_realm_neither_keeps_nor_restores(realdb):
    gid = await _add(realdb, code="6100", name="Office", erp_account_id="7", erp_link_realm=SANDBOX)
    await _add(realdb, GLAccountErpLink, gl_account_id=gid, realm=PROD, erp_account_id="15")
    result = await _sync(realdb, [_acct("Fixture", "ERP-1")], realm=None)
    assert (result.kept, result.restored) == (0, 0)
    assert await _kept(realdb) == {("6100", PROD, "15", False, ())}
    row = (await _chart(realdb))["6100"]
    assert (row.erp_account_id, row.erp_link_realm) == ("7", SANDBOX)


async def test_a_deleted_account_takes_its_kept_states_with_it(realdb):
    gid = await _add(realdb, code="6100", name="Office")
    await _add(realdb, GLAccountErpLink, gl_account_id=gid, realm=PROD, erp_account_id="15")
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(text("DELETE FROM gl_accounts WHERE id = :id"), {"id": gid})
        await s.commit()
    assert await _kept(realdb) == set()


async def test_a_kept_state_must_hold_a_link_or_a_decision(realdb):
    """The CHECK: an empty kept row, or a confirmation with no link, is
    refused by the database."""
    gid = await _add(realdb, code="6100", name="Office")
    with pytest.raises(IntegrityError):
        await _add(realdb, GLAccountErpLink, gl_account_id=gid, realm=PROD)
    with pytest.raises(IntegrityError):
        await _add(
            realdb,
            GLAccountErpLink,
            gl_account_id=gid,
            realm=SANDBOX,
            erp_link_candidates={"realm": SANDBOX, "reason": AMBIGUOUS_LEGACY, "accounts": []},
            erp_link_confirmed_at=datetime.now(UTC),
        )
