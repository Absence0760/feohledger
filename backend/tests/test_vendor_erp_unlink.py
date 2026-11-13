"""Clearing a vendor's ERP link in one company (docs/decisions.md §375).

``POST /api/vendors/{id}/erp-links/unlink`` clears the vendor's row link and
its kept ``vendor_erp_links`` link for one realm, `vendor.manage` only, refused
while one of its invoices is in flight to that company, with a
``vendor.erp_unlinked`` audit row naming the realm and every id cleared. It is
the way through a merge refused ``vendor_merge_erp_link_conflict`` (§373), and a
later sync may re-link the record only by the ordinary rules (§284 / §356).

Real-Postgres end-to-end (``realdb``): the locks, the kept-link rows, the audit
row and the sync all run against the real schema.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor, VendorErpLink
from app.models.workflow import AuditLog, WorkflowDefinition, WorkflowInstance
from app.services.vendor_erp_unlink import (
    VENDOR_ERP_LINK_NOT_FOUND,
    VENDOR_ERP_UNLINK_IN_FLIGHT,
)
from app.services.vendor_merge import VENDOR_MERGE_ERP_LINK_CONFLICT

_MERGE_URL = "/api/enrichment/vendors/consolidation/merge"


def _unlink_url(vendor_id) -> str:
    return f"/api/vendors/{vendor_id}/erp-links/unlink"


def _realm() -> str:
    # Unique per test: tenant "a" is shared, and a sync judges every link the
    # tenant holds under the realm it runs in.
    return f"quickbooks:{uuid.uuid4().hex[:12]}"


async def _seed_linked(mk, org_id, *, name, erp_vendor_id=None, realm=None, kept=None):
    """A vendor with a row link (``erp_vendor_id`` under ``realm``) and one kept
    link per ``kept`` entry ``{realm: erp_vendor_id}``."""
    async with mk() as s:
        v = Vendor(
            organization_id=org_id,
            name=name,
            status="active",
            erp_vendor_id=erp_vendor_id,
            erp_link_realm=realm,
        )
        s.add(v)
        await s.flush()
        for kept_realm, kept_id in (kept or {}).items():
            s.add(
                VendorErpLink(
                    organization_id=org_id,
                    vendor_id=v.id,
                    realm=kept_realm,
                    erp_vendor_id=kept_id,
                )
            )
        await s.commit()
        return v.id


async def _links_of(mk, vendor_id):
    """(row link as (realm, id) or None, {realm: id} of kept links)."""
    async with mk() as s:
        v = await s.get(Vendor, vendor_id)
        row = (v.erp_link_realm, v.erp_vendor_id) if v.erp_vendor_id else None
        kept = (
            await s.execute(
                select(VendorErpLink.realm, VendorErpLink.erp_vendor_id).where(
                    VendorErpLink.vendor_id == vendor_id
                )
            )
        ).all()
    return row, {r: i for r, i in kept}


async def _unlink_audits(mk, vendor_id) -> list[dict]:
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_type == "vendor",
                        AuditLog.entity_id == vendor_id,
                        AuditLog.action == "vendor.erp_unlinked",
                    )
                    .order_by(AuditLog.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return [{"actor_id": r.actor_id, **dict(r.details or {})} for r in rows]


async def _seed_invoice(mk, org_id, vendor_id, *, status, state_data=None):
    """An invoice to ``vendor_id`` at ``status`` with a workflow instance
    carrying ``state_data`` (the push's ``erp_realm`` / outcome keys)."""
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default.is_(True)))).scalar_one()
        inv = Invoice(
            organization_id=org_id,
            entity_id=ent,
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="x",
            amount=Decimal("100.00"),
            currency="USD",
            vendor_id=vendor_id,
            status=status,
        )
        s.add(inv)
        defn = WorkflowDefinition(
            organization_id=org_id,
            name=f"unlink-{uuid.uuid4().hex[:6]}",
            steps_config={"steps": [{"type": "erp_export"}]},
            is_active=True,
            is_default=False,
        )
        s.add(defn)
        await s.flush()
        s.add(
            WorkflowInstance(
                definition_id=defn.id,
                invoice_id=inv.id,
                current_step=1,
                state="active",
                state_data=state_data,
                steps_config_snapshot={"steps": [{"type": "erp_export"}]},
            )
        )
        await s.commit()
        return inv.id


async def _unlink(realdb, vendor_id, realm, *, role="ap_manager"):
    async with realdb.client(key="a", role=role) as client:
        return await client.post(_unlink_url(vendor_id), json={"realm": realm})


# ---------------------------------------------------------------------------
# Each link kind
# ---------------------------------------------------------------------------


async def test_unlink_clears_the_row_link_and_keeps_the_other_companies(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1, r2 = _realm(), _realm()
    vid = await _seed_linked(
        mk, org_id, name="RowLink Co", erp_vendor_id="57", realm=r1, kept={r2: "9"}
    )

    r = await _unlink(realdb, vid, r1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["realm"] == r1
    assert body["cleared"] == [{"placement": "row", "erp_vendor_id": "57"}]
    assert body["vendor"]["erp_vendor_id"] is None
    assert body["links"] == [{"realm": r2, "erp_vendor_id": "9", "placement": "kept"}]

    assert await _links_of(mk, vid) == (None, {r2: "9"})


async def test_unlink_clears_a_kept_link_and_leaves_the_row(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1, r2 = _realm(), _realm()
    vid = await _seed_linked(
        mk, org_id, name="KeptLink Co", erp_vendor_id="57", realm=r1, kept={r2: "9"}
    )

    r = await _unlink(realdb, vid, r2)
    assert r.status_code == 200, r.text
    assert r.json()["cleared"] == [{"placement": "kept", "erp_vendor_id": "9"}]
    assert r.json()["vendor"]["erp_vendor_id"] == "57"

    assert await _links_of(mk, vid) == ((r1, "57"), {})


async def test_unlink_null_realm_clears_only_the_unnamed_row_link(realdb):
    """A NULL-realm row link names no company (pre-0115, or a no-realm sync).
    `realm: null` is how a steward asks for it, and only for it."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r2 = _realm()
    vid = await _seed_linked(mk, org_id, name="Legacy Link Co", erp_vendor_id="10", kept={r2: "9"})

    r = await _unlink(realdb, vid, None)
    assert r.status_code == 200, r.text
    assert r.json()["realm"] is None
    assert r.json()["cleared"] == [{"placement": "row", "erp_vendor_id": "10"}]
    assert await _links_of(mk, vid) == (None, {r2: "9"})


async def test_unlink_clears_both_when_row_and_kept_name_one_company(realdb):
    """§356 never files the row's own realm as kept, but if both exist the unlink
    clears the company completely rather than leaving a link the push reads."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1 = _realm()
    vid = await _seed_linked(
        mk, org_id, name="Both Co", erp_vendor_id="57", realm=r1, kept={r1: "58"}
    )

    r = await _unlink(realdb, vid, r1)
    assert r.status_code == 200, r.text
    assert sorted(r.json()["cleared"], key=lambda c: c["placement"]) == [
        {"placement": "kept", "erp_vendor_id": "58"},
        {"placement": "row", "erp_vendor_id": "57"},
    ]
    assert await _links_of(mk, vid) == (None, {})


async def test_unlink_a_company_the_vendor_has_no_link_in_is_404_and_changes_nothing(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1 = _realm()
    vid = await _seed_linked(mk, org_id, name="NoLink Co", erp_vendor_id="57", realm=r1)

    for realm in (_realm(), None):
        r = await _unlink(realdb, vid, realm)
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == VENDOR_ERP_LINK_NOT_FOUND
        assert r.json()["detail"]["params"] == {}
    assert await _links_of(mk, vid) == ((r1, "57"), {})
    assert await _unlink_audits(mk, vid) == []


async def test_unlink_requires_the_realm_field(realdb):
    """`realm` is required: an empty body never defaults to the unnamed link."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _seed_linked(mk, org_id, name="NoBody Co", erp_vendor_id="10")
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(_unlink_url(vid), json={})
    assert r.status_code == 422, r.text
    assert await _links_of(mk, vid) == ((None, "10"), {})


async def test_list_erp_links_reports_row_then_kept(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1, r2, r3 = sorted([_realm(), _realm(), _realm()])
    vid = await _seed_linked(
        mk, org_id, name="List Co", erp_vendor_id="1", realm=r2, kept={r3: "3", r1: "2"}
    )
    async with realdb.client(key="a", role="cfo") as client:
        r = await client.get(f"/api/vendors/{vid}/erp-links")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "vendor_id": str(vid),
        "links": [
            {"realm": r2, "erp_vendor_id": "1", "placement": "row"},
            {"realm": r1, "erp_vendor_id": "2", "placement": "kept"},
            {"realm": r3, "erp_vendor_id": "3", "placement": "kept"},
        ],
    }


# ---------------------------------------------------------------------------
# In flight to that company
# ---------------------------------------------------------------------------


async def test_unlink_refused_while_a_push_is_sending_in_any_company(realdb):
    """`sending_to_erp` resolves the link when the push runs, under whatever
    connection it runs against — so it blocks every company's unlink."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1, r2 = _realm(), _realm()
    vid = await _seed_linked(
        mk, org_id, name="Sending Co", erp_vendor_id="57", realm=r1, kept={r2: "9"}
    )
    await _seed_invoice(mk, org_id, vid, status=InvoiceStatus.sending_to_erp)

    for realm in (r1, r2):
        r = await _unlink(realdb, vid, realm)
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["code"] == VENDOR_ERP_UNLINK_IN_FLIGHT
        assert detail["params"] == {"invoices": 1}
    assert await _links_of(mk, vid) == ((r1, "57"), {r2: "9"})
    assert await _unlink_audits(mk, vid) == []


async def test_unlink_refused_for_a_pushed_bill_awaiting_status_in_that_company(realdb):
    """`sent_to_erp` records the company it was pushed to: it blocks that
    company's unlink, not another's."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1, r2 = _realm(), _realm()
    vid = await _seed_linked(
        mk, org_id, name="Sent Co", erp_vendor_id="57", realm=r1, kept={r2: "9"}
    )
    await _seed_invoice(
        mk,
        org_id,
        vid,
        status=InvoiceStatus.sent_to_erp,
        state_data={"erp_reference": "B-1", "erp_realm": r1},
    )

    r = await _unlink(realdb, vid, r1)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == VENDOR_ERP_UNLINK_IN_FLIGHT

    r = await _unlink(realdb, vid, r2)
    assert r.status_code == 200, r.text
    assert await _links_of(mk, vid) == ((r1, "57"), {})


async def test_a_pushed_bill_with_no_recorded_company_blocks_every_unlink(realdb):
    """Fail closed: a `sent_to_erp` bill whose company is not on record may be
    any company's."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1, r2 = _realm(), _realm()
    vid = await _seed_linked(
        mk, org_id, name="NoRealm Co", erp_vendor_id="57", realm=r1, kept={r2: "9"}
    )
    await _seed_invoice(mk, org_id, vid, status=InvoiceStatus.sent_to_erp, state_data={})

    for realm in (r1, r2):
        r = await _unlink(realdb, vid, realm)
        assert r.status_code == 409, r.text


async def test_a_failed_push_counts_only_when_it_may_have_posted(realdb):
    """A failure that may already be in the ERP blocks the unlink; one that
    certainly posted nothing does not — its retry meets `vendor_not_linked`."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1 = _realm()
    unsure = await _seed_linked(mk, org_id, name="Unsure Co", erp_vendor_id="57", realm=r1)
    await _seed_invoice(
        mk, org_id, unsure, status=InvoiceStatus.failed, state_data={"erp_outcome_unknown": True}
    )
    clean = await _seed_linked(mk, org_id, name="Clean Co", erp_vendor_id="58", realm=r1)
    await _seed_invoice(
        mk, org_id, clean, status=InvoiceStatus.failed, state_data={"erp_outcome_unknown": False}
    )

    r = await _unlink(realdb, unsure, r1)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == VENDOR_ERP_UNLINK_IN_FLIGHT

    r = await _unlink(realdb, clean, r1)
    assert r.status_code == 200, r.text


async def test_a_posted_bill_does_not_block_the_unlink(realdb):
    """From `posted_in_erp` the bill carries its own id and nothing reads the
    vendor's link for it again."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1 = _realm()
    vid = await _seed_linked(mk, org_id, name="Posted Co", erp_vendor_id="57", realm=r1)
    await _seed_invoice(
        mk,
        org_id,
        vid,
        status=InvoiceStatus.posted_in_erp,
        state_data={"erp_reference": "B-2", "erp_realm": r1},
    )
    r = await _unlink(realdb, vid, r1)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_unlink_is_vendor_manage_only(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1 = _realm()
    vid = await _seed_linked(mk, org_id, name="Auth Co", erp_vendor_id="57", realm=r1)

    for role in ("ap_clerk", "cfo"):
        r = await _unlink(realdb, vid, r1, role=role)
        assert r.status_code == 403, (role, r.text)
    async with realdb.client(key="a", role=None) as client:
        r = await client.post(_unlink_url(vid), json={"realm": r1})
    assert r.status_code == 401
    assert await _links_of(mk, vid) == ((r1, "57"), {})

    r = await _unlink(realdb, vid, r1, role="admin")
    assert r.status_code == 200, r.text


async def test_unlink_another_tenants_vendor_is_404(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1 = _realm()
    vid = await _seed_linked(mk, org_id, name="Iso Co", erp_vendor_id="57", realm=r1)
    async with realdb.client(key="b", role="ap_manager") as client:
        r = await client.post(_unlink_url(vid), json={"realm": r1})
        assert r.status_code == 404
        r = await client.get(f"/api/vendors/{vid}/erp-links")
        assert r.status_code == 404
    assert await _links_of(mk, vid) == ((r1, "57"), {})


# ---------------------------------------------------------------------------
# The audit row
# ---------------------------------------------------------------------------


async def test_unlink_writes_one_audit_row_naming_the_realm_and_the_old_id(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1 = _realm()
    vid = await _seed_linked(mk, org_id, name="Audit Co", erp_vendor_id="57", realm=r1)

    r = await _unlink(realdb, vid, r1)
    assert r.status_code == 200, r.text

    (row,) = await _unlink_audits(mk, vid)
    assert row["actor_id"] is not None
    assert row["realm"] == r1
    assert row["cleared"] == [{"placement": "row", "erp_vendor_id": "57"}]
    # Ids only: no name, tax id, bank or address on the row.
    assert set(row) == {"actor_id", "realm", "cleared"}


# ---------------------------------------------------------------------------
# The way through a refused merge
# ---------------------------------------------------------------------------


async def test_a_merge_refused_for_a_link_conflict_goes_through_after_an_unlink(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1 = _realm()
    canonical = await _seed_linked(
        mk, org_id, name="Conflict Unlink Co", erp_vendor_id="10", realm=r1
    )
    dup = await _seed_linked(mk, org_id, name="Conflict Unlink Corp", erp_vendor_id="57", realm=r1)
    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup)]}

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(_MERGE_URL, json=body)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == VENDOR_MERGE_ERP_LINK_CONFLICT
        # The refusal points at the unlink.
        assert "unlink" in r.json()["detail"]["message"].lower()

        r = await client.post(_unlink_url(dup), json={"realm": r1})
        assert r.status_code == 200, r.text

        r = await client.post(_MERGE_URL, json=body)
        assert r.status_code == 200, r.text

    assert await _links_of(mk, canonical) == ((r1, "10"), {})
    assert await _links_of(mk, dup) == (None, {})
    async with mk() as s:
        assert (await s.get(Vendor, dup)).status == "inactive"


# ---------------------------------------------------------------------------
# A later sync follows the ordinary rules
# ---------------------------------------------------------------------------


async def test_after_an_unlink_a_unique_name_relinks_by_the_normal_rule(realdb):
    """The unlink is not a "never link" marker: the record is unheld, so a sync
    adopts it by name only when the name is unique on both sides — and says so
    (`change: linked`)."""
    from app.services.vendor_sync import sync_vendors_from_erp

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1 = _realm()
    name = f"Relink Co {uuid.uuid4().hex[:8]}"
    vid = await _seed_linked(mk, org_id, name=name, erp_vendor_id="57", realm=r1)
    assert (await _unlink(realdb, vid, r1)).status_code == 200

    async with mk() as s:
        result = await sync_vendors_from_erp(
            s, org_id, [{"erp_vendor_id": "57", "name": name}], realm=r1
        )
        await s.commit()
    assert result["created"] == 0
    assert await _links_of(mk, vid) == ((r1, "57"), {})
    async with mk() as s:
        changes = (
            (
                await s.execute(
                    select(AuditLog.details).where(
                        AuditLog.entity_id == vid, AuditLog.action == "vendor.synced_from_erp"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [c["change"] for c in changes] == ["linked"]


async def test_after_an_unlink_an_ambiguous_name_links_nothing(realdb):
    """Two pulled records share the vendor's name: which is which is a guess,
    so the sync reports the conflict and neither links nor creates."""
    from app.services.vendor_sync import sync_vendors_from_erp

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1 = _realm()
    name = f"Ambiguous Co {uuid.uuid4().hex[:8]}"
    vid = await _seed_linked(mk, org_id, name=name, erp_vendor_id="57", realm=r1)
    assert (await _unlink(realdb, vid, r1)).status_code == 200

    async with mk() as s:
        result = await sync_vendors_from_erp(
            s,
            org_id,
            [{"erp_vendor_id": "57", "name": name}, {"erp_vendor_id": "58", "name": name}],
            realm=r1,
        )
        await s.commit()
    assert result["created"] == 0
    assert [c["erp_vendor_ids"] for c in result["conflicts"]] == [["57", "58"]]
    assert await _links_of(mk, vid) == (None, {})


async def test_after_unlinking_a_kept_link_a_sync_there_restores_nothing(realdb):
    from app.services.vendor_sync import sync_vendors_from_erp

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1, r2 = _realm(), _realm()
    tag = uuid.uuid4().hex[:8]
    vid = await _seed_linked(
        mk, org_id, name=f"Kept Gone {tag}", erp_vendor_id="57", realm=r1, kept={r2: "9"}
    )
    assert (await _unlink(realdb, vid, r2)).status_code == 200

    async with mk() as s:
        result = await sync_vendors_from_erp(
            s, org_id, [{"erp_vendor_id": "9", "name": f"Someone Else {tag}"}], realm=r2
        )
        await s.commit()
    assert result["restored"] == 0
    row, kept = await _links_of(mk, vid)
    # The sync under r2 files the r1 row link as kept (§356) — and nothing for r2.
    assert row is None
    assert kept == {r1: "57"}
    async with mk() as s:
        holder = (
            await s.execute(
                select(Vendor.id).where(
                    Vendor.organization_id == org_id,
                    Vendor.erp_link_realm == r2,
                    Vendor.erp_vendor_id == "9",
                )
            )
        ).scalar_one()
    assert holder != vid


async def test_after_unlink_and_merge_a_sync_never_puts_the_stale_record_on_the_canonical(
    realdb,
):
    """The canonical still holds its own id in that company, so it is no name
    candidate: the stale record can never be attached to the merged vendor."""
    from app.services.vendor_sync import sync_vendors_from_erp

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1 = _realm()
    name = f"Stale Co {uuid.uuid4().hex[:8]}"
    canonical = await _seed_linked(mk, org_id, name=name, erp_vendor_id="10", realm=r1)
    dup = await _seed_linked(mk, org_id, name=f"{name} Ltd", erp_vendor_id="57", realm=r1)
    assert (await _unlink(realdb, dup, r1)).status_code == 200
    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        assert (await client.post(_MERGE_URL, json=body)).status_code == 200

    async with mk() as s:
        await sync_vendors_from_erp(
            s,
            org_id,
            [{"erp_vendor_id": "10", "name": name}, {"erp_vendor_id": "57", "name": name}],
            realm=r1,
        )
        await s.commit()
    assert await _links_of(mk, canonical) == ((r1, "10"), {})
