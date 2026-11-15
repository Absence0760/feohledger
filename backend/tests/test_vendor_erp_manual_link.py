"""Linking a vendor to an ERP supplier record by hand (docs/decisions.md §415).

``POST /api/vendors/{id}/erp-links/search`` finds records in the ERP's whole
contact list (by name and by the vendor's own tax number, which never leaves
the server), and ``POST …/erp-links/link`` links one, re-read from the ERP by
its id first, ``vendor.manage`` only, audited ``vendor.erp_linked``. For the
supplier no sync can see: a Xero customer with no bill or PO from us.

Real-Postgres end-to-end (``realdb``) for the locks, the kept links and the
audit row; the ERP is a small in-memory adapter patched in where the endpoint
resolves one, so nothing here depends on a provider double's HTTP shape (the
Xero calls themselves are pinned in ``test_erp_xero_adapter.py``).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from sqlalchemy import select

from app.models.organization import Organization
from app.models.vendor import Vendor, VendorErpLink
from app.models.workflow import AuditLog
from app.services.erp_adapters.base import ErpAdapter, VendorPayload, VendorSearchResult
from app.services.vendor_erp_manual_link import (
    ERP_VENDOR_ALREADY_LINKED,
    ERP_VENDOR_INACTIVE,
    ERP_VENDOR_NOT_FOUND,
    VENDOR_ERP_LINK_EXISTS,
    VENDOR_ERP_SEARCH_UNAVAILABLE,
)
from app.services.vendor_sync import sync_vendors_from_erp


class _Erp(ErpAdapter):
    """A connected ERP holding ``records`` (id → payload), searchable."""

    erp_type = "xero"
    supports_vendor_search = True

    def __init__(self, records: list[VendorPayload], *, truncated: bool = False):
        super().__init__({})
        self.records = {r.erp_vendor_id: r for r in records}
        self.truncated = truncated
        self.searches: list[dict] = []

    async def search_vendors(self, *, name, tax_id):
        self.searches.append({"name": name, "tax_id": tax_id})
        hits = [
            r
            for r in self.records.values()
            if (name and name.lower() in r.name.lower()) or (tax_id and r.tax_id == tax_id)
        ]
        return VendorSearchResult(vendors=hits, truncated=self.truncated)

    async def get_vendor(self, erp_vendor_id):
        return self.records.get(erp_vendor_id)


class _NoSearch(_Erp):
    supports_vendor_search = False


def _realm() -> str:
    return f"xero:{uuid.uuid4().hex[:12]}"


def _patched(adapter, realm):
    async def resolve(org, control_db):
        return adapter, realm

    return patch("app.api.vendors._erp_adapter_for_link", resolve)


async def _vendor(mk, org_id, *, name, tax_id=None, erp_vendor_id=None, realm=None, kept=None):
    async with mk() as s:
        v = Vendor(
            organization_id=org_id,
            name=name,
            status="active",
            tax_id=tax_id,
            erp_vendor_id=erp_vendor_id,
            erp_link_realm=realm,
        )
        s.add(v)
        await s.flush()
        for kept_realm, kept_id in (kept or {}).items():
            s.add(
                VendorErpLink(
                    organization_id=org_id, vendor_id=v.id, realm=kept_realm, erp_vendor_id=kept_id
                )
            )
        await s.commit()
        return v.id


async def _row(mk, vendor_id):
    async with mk() as s:
        v = await s.get(Vendor, vendor_id)
        kept = (
            await s.execute(
                select(VendorErpLink.realm, VendorErpLink.erp_vendor_id).where(
                    VendorErpLink.vendor_id == vendor_id
                )
            )
        ).all()
        return (v.erp_link_realm, v.erp_vendor_id), {r: i for r, i in kept}


async def _audits(mk, vendor_id) -> list[dict]:
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == vendor_id, AuditLog.action == "vendor.erp_linked"
                    )
                )
            )
            .scalars()
            .all()
        )
        return [{"actor_id": r.actor_id, **dict(r.details or {})} for r in rows]


async def _post(realdb, path, body, *, role="ap_manager"):
    async with realdb.client(key="a", role=role) as c:
        return await c.post(path, json=body)


CUSTOMER = VendorPayload(
    erp_vendor_id="c0ffee00-0000-4000-8000-000000000001",
    name="Bloom Florists Ltd",
    email="accounts@bloom.example",
    tax_id="GB 123 4567 89",
)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def test_search_finds_a_customer_by_name_and_says_its_tax_number_matches(realdb):
    mk = realdb.sessionmaker("a")
    vid = await _vendor(mk, realdb.info("a").org_id, name="Bloom Florists", tax_id="gb123456789")
    erp = _Erp([CUSTOMER])
    with _patched(erp, _realm()):
        r = await _post(realdb, f"/api/vendors/{vid}/erp-links/search", {"query": None})
    assert r.status_code == 200, r.text
    [c] = r.json()["candidates"]
    assert c["erp_vendor_id"] == CUSTOMER.erp_vendor_id
    assert c["tax_id_matches"] is True  # separators and case aside
    assert c["linked_elsewhere"] is False and c["active"] is True
    # The number itself never leaves the server.
    assert "tax_id" not in c
    assert CUSTOMER.tax_id not in r.text and "gb123456789" not in r.text.lower()
    # A blank query searches the vendor's own name, and its stored tax number.
    assert erp.searches == [{"name": "Bloom Florists", "tax_id": "gb123456789"}]


async def test_search_marks_a_record_another_vendor_holds_here(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    realm = _realm()
    other = await _vendor(
        mk, org_id, name="Bloom Old", erp_vendor_id=CUSTOMER.erp_vendor_id, realm=realm
    )
    vid = await _vendor(mk, org_id, name="Bloom Florists")
    with _patched(_Erp([CUSTOMER]), realm):
        r = await _post(realdb, f"/api/vendors/{vid}/erp-links/search", {"query": "bloom"})
    [c] = r.json()["candidates"]
    assert c["linked_elsewhere"] is True
    # A flag, never the holder's id (it may sit in another entity).
    assert str(other) not in r.text


async def test_search_on_an_erp_that_cannot_search_is_refused_with_a_code(realdb):
    mk = realdb.sessionmaker("a")
    vid = await _vendor(mk, realdb.info("a").org_id, name="Nowhere Co")
    with _patched(_NoSearch([]), _realm()):
        r = await _post(realdb, f"/api/vendors/{vid}/erp-links/search", {})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == VENDOR_ERP_SEARCH_UNAVAILABLE
    with _patched(_Erp([]), None):  # no company connected
        r = await _post(realdb, f"/api/vendors/{vid}/erp-links/search", {})
    assert r.json()["detail"]["code"] == VENDOR_ERP_SEARCH_UNAVAILABLE


async def test_a_failed_erp_search_is_a_502(realdb):
    mk = realdb.sessionmaker("a")
    vid = await _vendor(mk, realdb.info("a").org_id, name="Down Co")

    class _Down(_Erp):
        async def search_vendors(self, *, name, tax_id):
            raise RuntimeError("provider down")

    with _patched(_Down([]), _realm()):
        r = await _post(realdb, f"/api/vendors/{vid}/erp-links/search", {"query": "x"})
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


async def test_link_writes_the_link_in_this_company_and_audits_it(realdb):
    mk = realdb.sessionmaker("a")
    realm = _realm()
    vid = await _vendor(mk, realdb.info("a").org_id, name="Bloom Florists", tax_id="GB123456789")
    with _patched(_Erp([CUSTOMER]), realm):
        r = await _post(
            realdb,
            f"/api/vendors/{vid}/erp-links/link",
            {"erp_vendor_id": CUSTOMER.erp_vendor_id},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["realm"] == realm and body["vendor"]["erp_vendor_id"] == CUSTOMER.erp_vendor_id
    assert body["links"] == [
        {"realm": realm, "erp_vendor_id": CUSTOMER.erp_vendor_id, "placement": "row"}
    ]
    assert await _row(mk, vid) == ((realm, CUSTOMER.erp_vendor_id), {})
    # Only the link: the steward's own name and tax id stay as keyed.
    async with mk() as s:
        v = await s.get(Vendor, vid)
        assert v.name == "Bloom Florists" and v.tax_id == "GB123456789"
    [audit] = await _audits(mk, vid)
    assert audit["realm"] == realm
    assert audit["erp_vendor_id"] == CUSTOMER.erp_vendor_id
    assert audit["erp_name"] == "Bloom Florists Ltd"
    assert audit["tax_id_matches"] is True
    assert audit["actor_id"] is not None


async def test_link_keeps_a_row_link_from_another_company(realdb):
    mk = realdb.sessionmaker("a")
    old, realm = _realm(), _realm()
    vid = await _vendor(mk, realdb.info("a").org_id, name="Mover Co", erp_vendor_id="57", realm=old)
    with _patched(_Erp([CUSTOMER]), realm):
        r = await _post(
            realdb, f"/api/vendors/{vid}/erp-links/link", {"erp_vendor_id": CUSTOMER.erp_vendor_id}
        )
    assert r.status_code == 200, r.text
    assert await _row(mk, vid) == ((realm, CUSTOMER.erp_vendor_id), {old: "57"})
    [audit] = await _audits(mk, vid)
    assert audit["kept"] == {"realm": old, "erp_vendor_id": "57"}


async def test_link_refuses_a_vendor_already_linked_in_this_company(realdb):
    """On its row, kept for this company, or a row link naming no company (which
    may be this one): unlink first (§375). Nothing written."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    realm = _realm()
    on_row = await _vendor(mk, org_id, name="Row Co", erp_vendor_id="1", realm=realm)
    kept = await _vendor(
        mk, org_id, name="Kept Co", erp_vendor_id="2", realm=_realm(), kept={realm: "3"}
    )
    unnamed = await _vendor(mk, org_id, name="Unnamed Co", erp_vendor_id="4")
    for vid in (on_row, kept, unnamed):
        before = await _row(mk, vid)
        with _patched(_Erp([CUSTOMER]), realm):
            r = await _post(
                realdb,
                f"/api/vendors/{vid}/erp-links/link",
                {"erp_vendor_id": CUSTOMER.erp_vendor_id},
            )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == VENDOR_ERP_LINK_EXISTS
        assert await _row(mk, vid) == before
        assert await _audits(mk, vid) == []


async def test_link_refuses_a_record_another_vendor_holds_here(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    realm = _realm()
    await _vendor(mk, org_id, name="Holder", erp_vendor_id=CUSTOMER.erp_vendor_id, realm=realm)
    keeper = await _vendor(
        mk, org_id, name="Keeper", erp_vendor_id="9", realm=_realm(), kept={realm: "k-1"}
    )
    assert keeper
    vid = await _vendor(mk, org_id, name="Bloom Florists")
    kept_record = VendorPayload(erp_vendor_id="k-1", name="Kept Ltd")
    for record in (CUSTOMER, kept_record):
        with _patched(_Erp([CUSTOMER, kept_record]), realm):
            r = await _post(
                realdb,
                f"/api/vendors/{vid}/erp-links/link",
                {"erp_vendor_id": record.erp_vendor_id},
            )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == ERP_VENDOR_ALREADY_LINKED
    assert await _row(mk, vid) == ((None, None), {})


async def test_link_rereads_the_record_and_refuses_one_gone_or_archived(realdb):
    mk = realdb.sessionmaker("a")
    vid = await _vendor(mk, realdb.info("a").org_id, name="Gone Co")
    archived = VendorPayload(erp_vendor_id="arch-1", name="Archived Ltd", active=False)
    with _patched(_Erp([archived]), _realm()):
        missing = await _post(realdb, f"/api/vendors/{vid}/erp-links/link", {"erp_vendor_id": "x"})
        inactive = await _post(
            realdb, f"/api/vendors/{vid}/erp-links/link", {"erp_vendor_id": "arch-1"}
        )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == ERP_VENDOR_NOT_FOUND
    assert inactive.status_code == 409
    assert inactive.json()["detail"]["code"] == ERP_VENDOR_INACTIVE
    assert await _row(mk, vid) == ((None, None), {})


async def test_search_and_link_are_vendor_manage_only(realdb):
    mk = realdb.sessionmaker("a")
    vid = await _vendor(mk, realdb.info("a").org_id, name="Gate Co")
    with _patched(_Erp([CUSTOMER]), _realm()):
        for path, body in (
            (f"/api/vendors/{vid}/erp-links/search", {}),
            (f"/api/vendors/{vid}/erp-links/link", {"erp_vendor_id": CUSTOMER.erp_vendor_id}),
        ):
            r = await _post(realdb, path, body, role="ap_clerk")
            assert r.status_code == 403, (path, r.text)
    assert await _row(mk, vid) == ((None, None), {})


async def test_a_hand_made_link_survives_a_sync_that_does_not_list_the_record(realdb):
    """The customer is not in the vendor walk until its first bill; a later sync
    must not read its absence as "not this company's vendor"."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    realm = _realm()
    vid = await _vendor(mk, org_id, name="Bloom Florists")
    with _patched(_Erp([CUSTOMER]), realm):
        r = await _post(
            realdb, f"/api/vendors/{vid}/erp-links/link", {"erp_vendor_id": CUSTOMER.erp_vendor_id}
        )
    assert r.status_code == 200, r.text
    async with mk() as s:
        await sync_vendors_from_erp(
            s,
            org_id,
            [{"erp_vendor_id": "sup-1", "name": f"Other Supplier {uuid.uuid4().hex[:6]}"}],
            realm=realm,
        )
        await s.commit()
    assert await _row(mk, vid) == ((realm, CUSTOMER.erp_vendor_id), {})


async def _set_erp(realdb, erp):
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
        before = (org.settings or {}).get("erp")
        org.settings = {**(org.settings or {}), "erp": erp}
        await s.commit()
        return before


async def _verdict(realdb, vid):
    async with realdb.client(key="a", role="ap_manager") as c:
        r = await c.get(f"/api/vendors/{vid}/erp-links")
    assert r.status_code == 200, r.text
    refusal = r.json()["search_refusal"]
    return None if refusal is None else refusal["code"]


XERO = {
    "type": "xero",
    "integration_method": "direct",
    "oauth": {"provider": "xero", "external_tenant_id": "xt-1"},
}
XERO_REALM = "xero:xt-1"


async def test_the_links_read_states_the_search_verdict_from_the_settings(realdb):
    """From the stored settings alone: a searchable adapter connected to a named
    company, not one needing a reconnect, never the mock."""
    mk = realdb.sessionmaker("a")
    vid = await _vendor(mk, realdb.info("a").org_id, name="Read Co")
    before = await _set_erp(realdb, XERO)
    try:
        assert await _verdict(realdb, vid) is None
        await _set_erp(realdb, {**XERO, "oauth": {**XERO["oauth"], "needs_reconnect": True}})
        assert await _verdict(realdb, vid) == VENDOR_ERP_SEARCH_UNAVAILABLE
        await _set_erp(realdb, {**XERO, "oauth": {"provider": "xero"}})  # no company yet
        assert await _verdict(realdb, vid) == VENDOR_ERP_SEARCH_UNAVAILABLE
        await _set_erp(realdb, {"type": "mock", "integration_method": "direct"})
        assert await _verdict(realdb, vid) == VENDOR_ERP_SEARCH_UNAVAILABLE
    finally:
        await _set_erp(realdb, before)


async def test_the_search_verdict_agrees_with_both_doors_per_vendor(realdb):
    """The verdict is per vendor (issue #575): a vendor already linked in the
    connected company is refused `vendor_erp_link_exists` by the read, by the
    search and by the link alike — on its row, kept for this company, or a row
    link naming no company. One linked only in another company, or not at all,
    is offered both."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    states = {
        "unlinked": await _vendor(mk, org_id, name="Free Co"),
        "other_company": await _vendor(
            mk, org_id, name="Away Co", erp_vendor_id="a-1", realm=_realm()
        ),
        "on_row": await _vendor(mk, org_id, name="Row Co", erp_vendor_id="r-1", realm=XERO_REALM),
        "kept": await _vendor(
            mk,
            org_id,
            name="Kept Co",
            erp_vendor_id="k-1",
            realm=_realm(),
            kept={XERO_REALM: "k-2"},
        ),
        "unnamed": await _vendor(mk, org_id, name="Unnamed Co", erp_vendor_id="u-1"),
    }
    expected = {
        "unlinked": None,
        "other_company": None,
        "on_row": VENDOR_ERP_LINK_EXISTS,
        "kept": VENDOR_ERP_LINK_EXISTS,
        "unnamed": VENDOR_ERP_LINK_EXISTS,
    }
    before = await _set_erp(realdb, XERO)
    try:
        for state, vid in states.items():
            verdict = await _verdict(realdb, vid)
            assert verdict == expected[state], state
            with _patched(_Erp([]), XERO_REALM):
                search = await _post(realdb, f"/api/vendors/{vid}/erp-links/search", {})
            if verdict is None:
                assert search.status_code == 200, (state, search.text)
            else:
                assert search.status_code == 409, (state, search.text)
                assert search.json()["detail"]["code"] == verdict, state
            record = VendorPayload(erp_vendor_id=f"rec-{state}", name=f"Record {state}")
            with _patched(_Erp([record]), XERO_REALM):
                link = await _post(
                    realdb,
                    f"/api/vendors/{vid}/erp-links/link",
                    {"erp_vendor_id": record.erp_vendor_id},
                )
            if verdict is None:
                assert link.status_code == 200, (state, link.text)
                # Now linked here: the response says so, and so does the read.
                assert link.json()["search_refusal"]["code"] == VENDOR_ERP_LINK_EXISTS
                assert await _verdict(realdb, vid) == VENDOR_ERP_LINK_EXISTS
            else:
                assert link.status_code == 409, (state, link.text)
                assert link.json()["detail"]["code"] == verdict, state
    finally:
        await _set_erp(realdb, before)


async def test_a_refused_link_reads_nothing_from_the_erp(realdb):
    """A vendor already linked here is refused before the record is re-read:
    no provider round trip for an answer the vendor row already gives."""
    mk = realdb.sessionmaker("a")
    realm = _realm()
    vid = await _vendor(mk, realdb.info("a").org_id, name="Row Co", erp_vendor_id="1", realm=realm)

    class _Counting(_Erp):
        reads = 0

        async def get_vendor(self, erp_vendor_id):
            type(self).reads += 1
            return await super().get_vendor(erp_vendor_id)

    erp = _Counting([CUSTOMER])
    with _patched(erp, realm):
        r = await _post(
            realdb, f"/api/vendors/{vid}/erp-links/link", {"erp_vendor_id": CUSTOMER.erp_vendor_id}
        )
        s = await _post(realdb, f"/api/vendors/{vid}/erp-links/search", {})
    assert r.json()["detail"]["code"] == VENDOR_ERP_LINK_EXISTS
    assert s.json()["detail"]["code"] == VENDOR_ERP_LINK_EXISTS
    assert _Counting.reads == 0 and erp.searches == []


async def test_unlinking_the_connected_company_reopens_the_search(realdb):
    mk = realdb.sessionmaker("a")
    vid = await _vendor(
        mk, realdb.info("a").org_id, name="Undo Co", erp_vendor_id="1", realm=XERO_REALM
    )
    before = await _set_erp(realdb, XERO)
    try:
        assert await _verdict(realdb, vid) == VENDOR_ERP_LINK_EXISTS
        r = await _post(realdb, f"/api/vendors/{vid}/erp-links/unlink", {"realm": XERO_REALM})
        assert r.status_code == 200, r.text
        assert r.json()["search_refusal"] is None
        assert await _verdict(realdb, vid) is None
    finally:
        await _set_erp(realdb, before)
