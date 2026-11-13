"""Coverage for the vendor-consolidation EXECUTE path.

``POST /api/enrichment/vendors/consolidation/merge`` folds a set of duplicate
vendors into one canonical vendor: it reassigns every ``vendor_id`` FK across
the tenant child tables (so nothing orphans), soft-retires the duplicates
(``status="inactive"`` — never hard-deleted), is idempotent on a re-run, writes
a PII-free ``vendor.merged`` audit row, and refuses self-merge / cross-entity /
unknown vendors. RBAC: ``vendor.manage`` (admin / ap_manager).

Real-Postgres end-to-end (``realdb``) — exercises the SQL UPDATEs, the audit
row, the idempotency, the refusals, RBAC, and tenant isolation.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.contract import Contract, ContractStatus, ContractType
from app.models.credit_memo import CreditMemo
from app.models.invoice import Invoice, InvoiceStatus
from app.models.procurement import PurchaseOrder
from app.models.vendor import Vendor, VendorErpLink
from app.models.vendor_priors import VendorExtractionPrior
from app.models.workflow import AuditLog
from app.services.vendor_merge import (
    VENDOR_MERGE_BLOCKED_DUPLICATE,
    VENDOR_MERGE_CANONICAL_NOT_FOUND,
    VENDOR_MERGE_CROSS_ENTITY,
    VENDOR_MERGE_DUPLICATE_NOT_FOUND,
    VENDOR_MERGE_ERP_LINK_CONFLICT,
    VENDOR_MERGE_INVALID_ID,
    VENDOR_MERGE_NO_DUPLICATES,
    VENDOR_MERGE_SELF,
)

_MERGE_URL = "/api/enrichment/vendors/consolidation/merge"


def _assert_coded_refusal(r, status: int, code: str, message: str) -> None:
    # Empty params: no vendor name, tax id or bank detail rides on a refusal.
    assert r.status_code == status, r.text
    assert r.json()["detail"] == {"code": code, "message": message, "params": {}}


async def _seed_vendor(
    mk, org_id, *, name="Acme", status="active", entity_id=None, payments_blocked=False
):
    async with mk() as s:
        v = Vendor(
            organization_id=org_id,
            name=name,
            status=status,
            entity_id=entity_id,
            payments_blocked=payments_blocked,
            payments_blocked_reason="sanctions hit" if payments_blocked else None,
        )
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return v.id


async def _default_entity_id(mk):
    from app.models.entity import Entity

    async with mk() as s:
        return (await s.execute(select(Entity.id).where(Entity.is_default.is_(True)))).scalar_one()


async def _seed_invoice(mk, org_id, vendor_id, *, number, entity_id):
    from decimal import Decimal

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=entity_id,
            invoice_number=number,
            vendor_name="x",
            amount=Decimal("100.00"),
            vendor_id=vendor_id,
            status=InvoiceStatus.new,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return inv.id


async def _seed_po(mk, org_id, vendor_id, *, number, entity_id):
    from decimal import Decimal

    async with mk() as s:
        po = PurchaseOrder(
            organization_id=org_id,
            entity_id=entity_id,
            po_number=number,
            vendor_id=vendor_id,
            total=Decimal("500.00"),
            status="open",
        )
        s.add(po)
        await s.commit()
        await s.refresh(po)
        return po.id


async def _seed_credit_memo(mk, org_id, vendor_id, *, entity_id):
    from decimal import Decimal

    async with mk() as s:
        cm = CreditMemo(
            organization_id=org_id,
            entity_id=entity_id,
            vendor_id=vendor_id,
            memo_number=f"CM-{uuid.uuid4().hex[:8]}",
            amount=Decimal("10.00"),
            status="open",
        )
        s.add(cm)
        await s.commit()
        await s.refresh(cm)
        return cm.id


async def _seed_prior(mk, vendor_id, *, field_name, value, correction_count=1):
    async with mk() as s:
        p = VendorExtractionPrior(
            vendor_id=vendor_id,
            field_name=field_name,
            value=value,
            correction_count=correction_count,
        )
        s.add(p)
        await s.commit()
        await s.refresh(p)
        return p.id


async def _priors_of(mk, vendor_id):
    """{field_name: value} for one vendor's surviving extraction priors."""
    async with mk() as s:
        rows = (
            await s.execute(
                select(VendorExtractionPrior.field_name, VendorExtractionPrior.value).where(
                    VendorExtractionPrior.vendor_id == vendor_id
                )
            )
        ).all()
    return {r.field_name: r.value for r in rows}


async def _audit_merge_rows(mk, canonical_id):
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_type == "vendor",
                        AuditLog.entity_id == canonical_id,
                        AuditLog.action == "vendor.merged",
                    )
                    .order_by(AuditLog.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        return [dict(r.details or {}) for r in rows]


async def test_merge_reassigns_fks_and_deactivates_duplicates(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    ent = await _default_entity_id(mk)

    canonical = await _seed_vendor(mk, org_id, name="Acme Inc")
    dup1 = await _seed_vendor(mk, org_id, name="Acme Incorporated")
    dup2 = await _seed_vendor(mk, org_id, name="ACME, LLC")

    # Children spread across the duplicates.
    inv1 = await _seed_invoice(mk, org_id, dup1, number="INV-1", entity_id=ent)
    inv2 = await _seed_invoice(mk, org_id, dup2, number="INV-2", entity_id=ent)
    po1 = await _seed_po(mk, org_id, dup1, number="PO-1", entity_id=ent)
    cm1 = await _seed_credit_memo(mk, org_id, dup2, entity_id=ent)

    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup1), str(dup2)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
    assert r.status_code == 200, r.text
    data = r.json()

    # Every child FK moved to the canonical vendor.
    assert data["reassigned"]["invoices"] == 2
    assert data["reassigned"]["purchase_orders"] == 1
    assert data["reassigned"]["credit_memos"] == 1
    assert data["total_reassigned"] == 4
    assert set(data["deactivated_vendor_ids"]) == {str(dup1), str(dup2)}

    async with mk() as s:
        assert (await s.get(Invoice, inv1)).vendor_id == canonical
        assert (await s.get(Invoice, inv2)).vendor_id == canonical
        assert (await s.get(PurchaseOrder, po1)).vendor_id == canonical
        assert (await s.get(CreditMemo, cm1)).vendor_id == canonical
        # Duplicates soft-retired, canonical untouched & active. Not deleted.
        assert (await s.get(Vendor, dup1)).status == "inactive"
        assert (await s.get(Vendor, dup2)).status == "inactive"
        assert (await s.get(Vendor, canonical)).status == "active"

    # Exactly one PII-free vendor.merged audit row.
    details = await _audit_merge_rows(mk, canonical)
    assert len(details) == 1
    d = details[0]
    assert d["canonical_vendor_id"] == str(canonical)
    assert set(d["duplicate_vendor_ids"]) == {str(dup1), str(dup2)}
    assert d["total_reassigned"] == 4
    assert d["reassigned"]["invoices"] == 2


async def test_merge_is_idempotent(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    ent = await _default_entity_id(mk)

    canonical = await _seed_vendor(mk, org_id, name="Globex")
    dup = await _seed_vendor(mk, org_id, name="Globex Corp")
    await _seed_invoice(mk, org_id, dup, number="INV-G", entity_id=ent)

    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r1 = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
        assert r1.status_code == 200, r1.text
        assert r1.json()["total_reassigned"] == 1
        assert r1.json()["deactivated_vendor_ids"] == [str(dup)]

        # Re-run: FKs already moved, duplicate already inactive → no-op (not error).
        r2 = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
        assert r2.status_code == 200, r2.text
        assert r2.json()["total_reassigned"] == 0
        assert r2.json()["deactivated_vendor_ids"] == []

    # The re-run still wrote its own audit row (append-only) but moved nothing.
    details = await _audit_merge_rows(mk, canonical)
    assert len(details) == 2
    assert details[1]["total_reassigned"] == 0


async def test_merge_refuses_self_merge(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    v = await _seed_vendor(mk, org_id, name="SelfCo")
    body = {"canonical_vendor_id": str(v), "duplicate_vendor_ids": [str(v)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
    _assert_coded_refusal(
        r,
        422,
        VENDOR_MERGE_SELF,
        "The canonical vendor cannot also be a duplicate (self-merge).",
    )
    async with mk() as s:
        assert (await s.get(Vendor, v)).status == "active"


async def test_merge_refuses_empty_duplicates(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    v = await _seed_vendor(mk, org_id, name="EmptyCo")
    body = {"canonical_vendor_id": str(v), "duplicate_vendor_ids": []}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
    _assert_coded_refusal(
        r, 422, VENDOR_MERGE_NO_DUPLICATES, "At least one duplicate vendor is required."
    )
    async with mk() as s:
        assert (await s.get(Vendor, v)).status == "active"


async def test_merge_refuses_cross_entity(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    # A second, non-default entity.
    from app.models.entity import Entity

    async with mk() as s:
        other = Entity(
            organization_id=org_id, name="Sub B", slug="sub-b", currency="USD", is_default=False
        )
        s.add(other)
        await s.commit()
        await s.refresh(other)
        other_ent = other.id
    default_ent = await _default_entity_id(mk)

    canonical = await _seed_vendor(mk, org_id, name="CrossCo", entity_id=default_ent)
    dup = await _seed_vendor(mk, org_id, name="CrossCo B", entity_id=other_ent)

    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
    _assert_coded_refusal(
        r,
        422,
        VENDOR_MERGE_CROSS_ENTITY,
        "Cannot merge vendors that belong to different entities.",
    )

    # Nothing changed — the duplicate stays active.
    async with mk() as s:
        assert (await s.get(Vendor, dup)).status == "active"


async def test_merge_refuses_blocked_duplicate_into_unblocked_canonical(realdb):
    """Issue #177: folding a payments-blocked (sanctioned) duplicate into an
    unblocked canonical would silently make its held invoices payable once
    reassigned — a sanctions-block bypass. Must refuse, not merge."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    canonical = await _seed_vendor(mk, org_id, name="CleanCo")
    dup = await _seed_vendor(mk, org_id, name="SanctionedCo", payments_blocked=True)
    held_invoice = await _seed_invoice(
        mk, org_id, dup, number="HELD-1", entity_id=await _default_entity_id(mk)
    )

    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
    _assert_coded_refusal(
        r,
        422,
        VENDOR_MERGE_BLOCKED_DUPLICATE,
        "Cannot merge a payments-blocked vendor into an unblocked canonical — resolve the "
        "block first (see the vendor's screening review) or exclude it from this merge.",
    )
    # The sanctions reason seeded on the duplicate never reaches the refusal.
    assert "sanctions hit" not in r.text

    # Nothing changed: the duplicate is still blocked/active, the canonical
    # is still unblocked, and the held invoice was NOT reassigned to the
    # (unblocked) canonical.
    async with mk() as s:
        dup_row = await s.get(Vendor, dup)
        assert dup_row.status == "active"
        assert dup_row.payments_blocked is True
        canonical_row = await s.get(Vendor, canonical)
        assert canonical_row.payments_blocked is False
        inv = await s.get(Invoice, held_invoice)
        assert inv.vendor_id == dup


async def test_merge_allows_clean_vendor_into_blocked_canonical(realdb):
    """The reverse direction is fine and needs no refusal: merging a clean
    vendor INTO an already-blocked canonical only tightens the block (the
    duplicate's invoices join an already-blocked vendor), never bypasses it."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    canonical = await _seed_vendor(mk, org_id, name="SanctionedCanonical", payments_blocked=True)
    dup = await _seed_vendor(mk, org_id, name="CleanDup")

    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
    assert r.status_code == 200, r.text

    async with mk() as s:
        assert (await s.get(Vendor, dup)).status == "inactive"
        assert (await s.get(Vendor, canonical)).payments_blocked is True


async def test_merge_unknown_vendor_404(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    canonical = await _seed_vendor(mk, org_id, name="KnownCo")
    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(uuid.uuid4())]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
    _assert_coded_refusal(
        r, 404, VENDOR_MERGE_DUPLICATE_NOT_FOUND, "A duplicate vendor was not found."
    )
    async with mk() as s:
        assert (await s.get(Vendor, canonical)).status == "active"


async def test_merge_unknown_canonical_404(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    dup = await _seed_vendor(mk, org_id, name="OrphanDupCo")
    body = {"canonical_vendor_id": str(uuid.uuid4()), "duplicate_vendor_ids": [str(dup)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(_MERGE_URL, json=body)
    _assert_coded_refusal(r, 404, VENDOR_MERGE_CANONICAL_NOT_FOUND, "Canonical vendor not found.")
    async with mk() as s:
        assert (await s.get(Vendor, dup)).status == "active"


async def test_merge_malformed_vendor_id_422(realdb):
    body = {"canonical_vendor_id": "not-a-uuid", "duplicate_vendor_ids": [str(uuid.uuid4())]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(_MERGE_URL, json=body)
    _assert_coded_refusal(r, 422, VENDOR_MERGE_INVALID_ID, "Invalid vendor id.")


async def test_merge_clerk_forbidden(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    canonical = await _seed_vendor(mk, org_id, name="ClerkCo")
    dup = await _seed_vendor(mk, org_id, name="ClerkCo Dup")
    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup)]}
    async with realdb.client(key="a", role="ap_clerk") as clerk:
        r = await clerk.post("/api/enrichment/vendors/consolidation/merge", json=body)
    assert r.status_code == 403
    # Nothing retired.
    async with mk() as s:
        assert (await s.get(Vendor, dup)).status == "active"


async def test_merge_auth_required(realdb):
    async with realdb.client(key="a", role=None) as client:
        r = await client.post(
            "/api/enrichment/vendors/consolidation/merge",
            json={
                "canonical_vendor_id": str(uuid.uuid4()),
                "duplicate_vendor_ids": [str(uuid.uuid4())],
            },
        )
    assert r.status_code == 401


async def test_merge_tenant_isolation(realdb):
    """A vendor from tenant A is unknown to tenant B → 404, no cross-tenant reach."""
    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    canonical_a = await _seed_vendor(mk_a, org_a, name="IsoCo")
    dup_a = await _seed_vendor(mk_a, org_a, name="IsoCo Dup")

    body = {"canonical_vendor_id": str(canonical_a), "duplicate_vendor_ids": [str(dup_a)]}
    async with realdb.client(key="b", role="ap_manager") as client_b:
        r = await client_b.post("/api/enrichment/vendors/consolidation/merge", json=body)
    assert r.status_code == 404
    # A's vendors untouched.
    async with mk_a() as s:
        assert (await s.get(Vendor, dup_a)).status == "active"


async def test_merge_contract_fk_reassigned(realdb):
    """A non-nullable vendor_id FK table (contracts) also reassigns cleanly."""
    from datetime import date

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    ent = await _default_entity_id(mk)
    canonical = await _seed_vendor(mk, org_id, name="ContractCo")
    dup = await _seed_vendor(mk, org_id, name="ContractCo Dup")

    async with mk() as s:
        c = Contract(
            organization_id=org_id,
            entity_id=ent,
            vendor_id=dup,
            contract_number=f"C-{uuid.uuid4().hex[:8]}",
            title="Service",
            contract_type=ContractType.service,
            status=ContractStatus.active,
            start_date=date(2026, 1, 1),
        )
        s.add(c)
        await s.commit()
        await s.refresh(c)
        cid = c.id

    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["reassigned"]["contracts"] == 1
    async with mk() as s:
        assert (await s.get(Contract, cid)).vendor_id == canonical


async def test_merge_collapses_priors_colliding_across_duplicates(realdb):
    """Two duplicates each holding a prior for the SAME field the canonical
    lacks must NOT blow the merge up on ``uq_vendor_priors_vendor_field``.

    The pre-fix code only deduped duplicates against the *canonical's* fields,
    so the second `terms` row reassigned collided with the first → IntegrityError
    → the whole merge 500'd and rolled back.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    canonical = await _seed_vendor(mk, org_id, name="PriorCo")
    dup1 = await _seed_vendor(mk, org_id, name="PriorCo Ltd")
    dup2 = await _seed_vendor(mk, org_id, name="Prior Co.")

    # Canonical holds NO prior for `terms` — the natural "pick the cleanest
    # record" choice. Both duplicates do; dup2's is the better-evidenced one.
    await _seed_prior(mk, dup1, field_name="terms", value="net_15", correction_count=2)
    await _seed_prior(mk, dup2, field_name="terms", value="net_30", correction_count=7)
    # A field only one duplicate has still moves across untouched.
    await _seed_prior(mk, dup1, field_name="cost_center", value="CC-100")

    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup1), str(dup2)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
    assert r.status_code == 200, r.text
    data = r.json()

    # Exactly one `terms` prior survives — the most-corrected one — plus the
    # uncontested `cost_center`.
    assert await _priors_of(mk, canonical) == {"terms": "net_30", "cost_center": "CC-100"}
    assert await _priors_of(mk, dup1) == {}
    assert await _priors_of(mk, dup2) == {}
    assert data["reassigned"]["vendor_extraction_priors:dropped"] == 1
    assert data["reassigned"]["vendor_extraction_priors"] == 2


async def test_merge_canonical_prior_wins_over_duplicate(realdb):
    """Where the canonical already holds the field, its own value survives."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    canonical = await _seed_vendor(mk, org_id, name="KeepMineCo")
    dup = await _seed_vendor(mk, org_id, name="KeepMineCo Dup")
    await _seed_prior(mk, canonical, field_name="currency", value="USD", correction_count=1)
    # Better-evidenced, but the canonical is the surviving vendor — it still loses.
    await _seed_prior(mk, dup, field_name="currency", value="EUR", correction_count=9)

    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
    assert r.status_code == 200, r.text

    assert await _priors_of(mk, canonical) == {"currency": "USD"}
    assert await _priors_of(mk, dup) == {}
    assert r.json()["reassigned"]["vendor_extraction_priors:dropped"] == 1
    assert "vendor_extraction_priors" not in r.json()["reassigned"]


async def test_merge_prior_collapse_is_idempotent(realdb):
    """Re-running a completed merge collapses nothing further and keeps the
    same winner — the tie-break is deterministic, not arbitrary."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    canonical = await _seed_vendor(mk, org_id, name="IdemPriorCo")
    dup1 = await _seed_vendor(mk, org_id, name="IdemPriorCo A")
    dup2 = await _seed_vendor(mk, org_id, name="IdemPriorCo B")
    await _seed_prior(mk, dup1, field_name="tax_rate", value="0.20", correction_count=3)
    await _seed_prior(mk, dup2, field_name="tax_rate", value="0.15", correction_count=1)

    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(dup1), str(dup2)]}
    async with realdb.client(key="a", role="ap_manager") as client:
        r1 = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
        assert r1.status_code == 200, r1.text
        assert await _priors_of(mk, canonical) == {"tax_rate": "0.20"}

        r2 = await client.post("/api/enrichment/vendors/consolidation/merge", json=body)
    assert r2.status_code == 200, r2.text
    # Nothing left to drop or move, and the surviving prior is unchanged.
    assert r2.json()["reassigned"] == {}
    assert r2.json()["total_reassigned"] == 0
    assert await _priors_of(mk, canonical) == {"tax_rate": "0.20"}


# ---------------------------------------------------------------------------
# ERP links move with the merge (docs/decisions.md §373)
# ---------------------------------------------------------------------------
#
# A vendor's ERP link lives in two places since §356: the row's
# `erp_vendor_id` + `erp_link_realm`, and one `vendor_erp_links` row per other
# company it was linked in. A merge used to move neither, so the canonical
# vendor's invoices were refused `vendor_not_linked` while the id sat on the
# retired duplicate, and a later sync restored a kept link onto it.


def _realm() -> str:
    # Unique per test: tenant "a" is shared, and a sync judges every link the
    # tenant holds under the realm it runs in.
    return f"quickbooks:{uuid.uuid4().hex[:12]}"


async def _seed_linked(
    mk, org_id, *, name, erp_vendor_id=None, realm=None, kept=None, bank_details=None
):
    """A vendor with a row link (``erp_vendor_id`` under ``realm``) and a kept
    link per ``kept`` entry ``{realm: erp_vendor_id}``."""
    async with mk() as s:
        v = Vendor(
            organization_id=org_id,
            name=name,
            status="active",
            erp_vendor_id=erp_vendor_id,
            erp_link_realm=realm,
            bank_details=bank_details,
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


async def _holders(mk, org_id, realm, erp_id) -> set[uuid.UUID]:
    """Every vendor holding ``erp_id`` in ``realm``, on its row or kept."""
    async with mk() as s:
        on_row = (
            await s.execute(
                select(Vendor.id).where(
                    Vendor.organization_id == org_id,
                    Vendor.erp_link_realm == realm,
                    Vendor.erp_vendor_id == erp_id,
                )
            )
        ).scalars()
        kept = (
            await s.execute(
                select(VendorErpLink.vendor_id).where(
                    VendorErpLink.organization_id == org_id,
                    VendorErpLink.realm == realm,
                    VendorErpLink.erp_vendor_id == erp_id,
                )
            )
        ).scalars()
        return {*on_row, *kept}


async def _merge(realdb, canonical, *dups):
    body = {"canonical_vendor_id": str(canonical), "duplicate_vendor_ids": [str(d) for d in dups]}
    async with realdb.client(key="a", role="ap_manager") as client:
        return await client.post(_MERGE_URL, json=body)


async def test_merge_moves_the_duplicates_live_link_onto_an_unlinked_canonical(realdb):
    """The case the follow-up named: the duplicate held the ERP link, so the
    canonical's invoices were refused `vendor_not_linked`. Now the link lands on
    the canonical's row, the push resolves it there, and the duplicate holds
    none. The canonical's bank details are untouched."""
    from app.services.vendor_erp_links import vendor_erp_ids

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    realm = _realm()
    bank = {"account_number": "000123456789", "routing_number": "021000021"}
    canonical = await _seed_linked(mk, org_id, name="LinkMove Co", bank_details=bank)
    dup = await _seed_linked(mk, org_id, name="LinkMove Company", erp_vendor_id="57", realm=realm)

    r = await _merge(realdb, canonical, dup)
    assert r.status_code == 200, r.text

    assert await _links_of(mk, canonical) == ((realm, "57"), {})
    assert await _links_of(mk, dup) == (None, {})
    async with mk() as s:
        c = await s.get(Vendor, canonical)
        assert c.bank_details == bank
        assert await vendor_erp_ids(s, org_id, [c], realm=realm, trust_legacy=False) == {
            canonical: "57"
        }
    assert await _holders(mk, org_id, realm, "57") == {canonical}

    (details,) = await _audit_merge_rows(mk, canonical)
    assert details["erp_links"] == [
        {"realm": realm, "erp_vendor_id": "57", "from_vendor_id": str(dup), "placed": "row"}
    ]


async def test_merge_moves_kept_links_to_realms_the_canonical_has_none_in(realdb):
    """The canonical keeps its own row link; the duplicate's row link (another
    company) and its kept link are filed as the canonical's kept links, so a
    return to either company posts them at once."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1, r2, r3 = _realm(), _realm(), _realm()
    canonical = await _seed_linked(mk, org_id, name="KeptMove Co", erp_vendor_id="10", realm=r1)
    dup = await _seed_linked(
        mk, org_id, name="KeptMove Ltd", erp_vendor_id="57", realm=r2, kept={r3: "88"}
    )

    r = await _merge(realdb, canonical, dup)
    assert r.status_code == 200, r.text

    assert await _links_of(mk, canonical) == ((r1, "10"), {r2: "57", r3: "88"})
    assert await _links_of(mk, dup) == (None, {})
    (details,) = await _audit_merge_rows(mk, canonical)
    assert sorted((e["realm"], e["placed"]) for e in details["erp_links"]) == sorted(
        [(r2, "kept"), (r3, "kept")]
    )


async def test_merge_releases_a_duplicate_link_the_canonical_already_holds(realdb):
    """Same id in the same company on both sides: nothing to choose. The
    duplicate drops its copy and the canonical gains no second row for it."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1, r2 = _realm(), _realm()
    canonical = await _seed_linked(
        mk, org_id, name="Release Co", erp_vendor_id="57", realm=r1, kept={r2: "9"}
    )
    dup = await _seed_linked(
        mk, org_id, name="Release Inc", erp_vendor_id="9", realm=r2, kept={r1: "57"}
    )

    r = await _merge(realdb, canonical, dup)
    assert r.status_code == 200, r.text

    assert await _links_of(mk, canonical) == ((r1, "57"), {r2: "9"})
    assert await _links_of(mk, dup) == (None, {})
    (details,) = await _audit_merge_rows(mk, canonical)
    assert {e["placed"] for e in details["erp_links"]} == {"released"}


async def test_merge_refuses_a_realm_where_both_hold_different_ids(realdb):
    """Two ERP supplier records claim the merged vendor in one company. The
    merge refuses, names the company, and moves nothing at all: no link, no
    invoice, no retirement, no audit row."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    ent = await _default_entity_id(mk)
    r1, r2 = _realm(), _realm()
    canonical = await _seed_linked(
        mk, org_id, name="Conflict Co", erp_vendor_id="10", realm=r1, kept={r2: "5"}
    )
    dup = await _seed_linked(
        mk, org_id, name="Conflict Corp", erp_vendor_id="57", realm=r1, kept={r2: "6"}
    )
    inv = await _seed_invoice(mk, org_id, dup, number="INV-CONFLICT", entity_id=ent)

    r = await _merge(realdb, canonical, dup)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == VENDOR_MERGE_ERP_LINK_CONFLICT
    assert detail["params"] == {"realms": sorted([r1, r2])}

    assert await _links_of(mk, canonical) == ((r1, "10"), {r2: "5"})
    assert await _links_of(mk, dup) == ((r1, "57"), {r2: "6"})
    async with mk() as s:
        assert (await s.get(Invoice, inv)).vendor_id == dup
        assert (await s.get(Vendor, dup)).status == "active"
    assert await _audit_merge_rows(mk, canonical) == []


async def test_merge_refuses_two_duplicates_linked_differently_in_one_realm(realdb):
    """The conflict is not only canonical-vs-duplicate: two duplicates moving
    different ids into a company the canonical has none in is the same choice."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    realm = _realm()
    canonical = await _seed_linked(mk, org_id, name="TwoDup Co")
    dup1 = await _seed_linked(mk, org_id, name="TwoDup A", kept={realm: "57"})
    dup2 = await _seed_linked(mk, org_id, name="TwoDup B", erp_vendor_id="58", realm=realm)

    r = await _merge(realdb, canonical, dup1, dup2)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["params"] == {"realms": [realm]}
    assert await _links_of(mk, dup1) == (None, {realm: "57"})
    assert await _links_of(mk, dup2) == ((realm, "58"), {})
    assert await _links_of(mk, canonical) == (None, {})


async def test_merge_refuses_an_unnamed_link_against_a_different_named_one(realdb):
    """A NULL-realm link names no company, so it may be the same company as the
    duplicate's named link: a different id there is reported as realm null."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    realm = _realm()
    canonical = await _seed_linked(mk, org_id, name="Legacy Co", erp_vendor_id="10")
    dup = await _seed_linked(mk, org_id, name="Legacy Inc", erp_vendor_id="57", realm=realm)

    r = await _merge(realdb, canonical, dup)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["params"] == {"realms": [None]}
    assert await _links_of(mk, canonical) == ((None, "10"), {})


async def test_after_a_merge_a_sync_restores_nothing_onto_the_retired_duplicate(realdb):
    """The duplicate's kept link went to the canonical, so the next sync under
    that company restores it onto the canonical — and the duplicate stays
    unlinked, with nothing kept for any company."""
    from app.services.vendor_sync import sync_vendors_from_erp

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1, r2 = _realm(), _realm()
    tag = uuid.uuid4().hex[:8]
    canonical = await _seed_linked(mk, org_id, name=f"Restore Co {tag}")
    dup = await _seed_linked(
        mk, org_id, name=f"Restore Ltd {tag}", erp_vendor_id="57", realm=r1, kept={r2: "88"}
    )

    r = await _merge(realdb, canonical, dup)
    assert r.status_code == 200, r.text
    assert await _links_of(mk, canonical) == ((r1, "57"), {r2: "88"})

    async with mk() as s:
        result = await sync_vendors_from_erp(
            s, org_id, [{"erp_vendor_id": "88", "name": f"Restore Co {tag}"}], realm=r2
        )
        await s.commit()
    assert result["restored"] == 1

    # Restored onto the canonical, which files its r1 link in turn (§356).
    assert await _links_of(mk, canonical) == ((r2, "88"), {r1: "57"})
    assert await _links_of(mk, dup) == (None, {})


async def test_merge_never_leaves_two_holders_of_one_id_in_one_realm(realdb):
    """Two duplicates holding the same id in one company (one on its row, one
    kept) collapse to one link on the canonical: one holder per (realm, id),
    one kept row per (vendor, realm) — the unique constraint is never reached."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    r1, r2 = _realm(), _realm()
    canonical = await _seed_linked(mk, org_id, name="OneHolder Co", erp_vendor_id="1", realm=r1)
    dup1 = await _seed_linked(mk, org_id, name="OneHolder A", kept={r2: "88"})
    dup2 = await _seed_linked(mk, org_id, name="OneHolder B", erp_vendor_id="88", realm=r2)

    r = await _merge(realdb, canonical, dup1, dup2)
    assert r.status_code == 200, r.text

    assert await _links_of(mk, canonical) == ((r1, "1"), {r2: "88"})
    assert await _holders(mk, org_id, r2, "88") == {canonical}
    assert await _links_of(mk, dup1) == (None, {})
    assert await _links_of(mk, dup2) == (None, {})

    # A re-run has nothing left to move.
    r2_ = await _merge(realdb, canonical, dup1, dup2)
    assert r2_.status_code == 200, r2_.text
    rows = await _audit_merge_rows(mk, canonical)
    assert rows[-1]["erp_links"] == []
    assert await _links_of(mk, canonical) == ((r1, "1"), {r2: "88"})


# ---------------------------------------------------------------------------
# VENDOR_FK_CHILDREN drift guard
# ---------------------------------------------------------------------------
#
# `VENDOR_FK_CHILDREN` is the single source of truth for "what points at a
# vendor", and its own docstring says a new table with a `vendor_id` FK MUST be
# added there or its rows are left dangling on a merge. Nothing enforced that —
# the list happened to be complete, so these guard the future. Shaped like
# `tests/test_payment_methods.py`'s drift guards: walk the live metadata rather
# than restate the list, so the guard can't itself go stale.

# Tenant tables carrying a `vendor_id` column that a merge deliberately does
# NOT plain-reassign. Each needs a reason; a bare entry is a hole, not an
# exemption.
_VENDOR_FK_NOT_PLAIN_REASSIGNED = {
    # Unique on (vendor_id, field_name) — a blind UPDATE collides when both the
    # duplicate and the canonical vendor hold a prior for the same field.
    # `merge_vendors` handles it specially (keep canonical, drop the dup's).
    "vendor_extraction_priors": (
        "unique (vendor_id, field_name) — merged specially, not plain-reassigned"
    ),
    # Unique on (vendor_id, realm), and a realm the canonical is already linked
    # in must not gain a second link: `_plan_erp_links` moves each kept link
    # realm by realm with the row link, or refuses the merge (decisions §373).
    "vendor_erp_links": (
        "unique (vendor_id, realm) — moved realm by realm with the row link, not plain-reassigned"
    ),
}


def _tenant_tables_with_vendor_fk() -> set[str]:
    """Every tenant-DB table declaring a `vendor_id` column, from live metadata.

    Importing `vendor_merge` pulls in every child model, which is what puts
    them on `Base.metadata`.
    """
    import app.services.vendor_merge  # noqa: F401
    from app.models.base import Base
    from app.services.tenant_provisioning import CONTROL_TABLES

    return {
        name
        for name, table in Base.metadata.tables.items()
        if name not in CONTROL_TABLES and table.columns.get("vendor_id") is not None
    }


def test_every_tenant_table_with_a_vendor_fk_is_in_vendor_fk_children():
    """A new table with a `vendor_id` FK that nobody adds to
    `VENDOR_FK_CHILDREN` leaves its rows dangling on a merge — pointing at a
    vendor that has just been soft-retired, with no path to the canonical one.
    """
    from app.services.vendor_merge import VENDOR_FK_CHILDREN

    discovered = _tenant_tables_with_vendor_fk()
    assert discovered, "expected to discover tenant tables carrying a vendor_id FK"

    covered = {model.__tablename__ for model in VENDOR_FK_CHILDREN}
    missing = discovered - covered - set(_VENDOR_FK_NOT_PLAIN_REASSIGNED)
    assert not missing, (
        "these tenant tables carry a vendor_id FK but are not in "
        f"VENDOR_FK_CHILDREN, so a vendor merge would orphan their rows: {sorted(missing)}. "
        "Add the model to VENDOR_FK_CHILDREN, or add the table to "
        "_VENDOR_FK_NOT_PLAIN_REASSIGNED with a reason."
    )


def test_vendor_fk_children_has_no_stale_entries():
    """The reverse direction: a model in the list that no longer declares a
    `vendor_id` column would make the merge issue a doomed UPDATE."""
    from app.services.vendor_merge import VENDOR_FK_CHILDREN

    discovered = _tenant_tables_with_vendor_fk()
    stale = [m.__tablename__ for m in VENDOR_FK_CHILDREN if m.__tablename__ not in discovered]
    assert not stale, f"VENDOR_FK_CHILDREN names tables with no vendor_id column: {stale}"


def test_vendor_fk_exemptions_are_current_and_justified():
    """An exemption that stops being true silently excuses a table the merge
    should by then be reassigning."""
    discovered = _tenant_tables_with_vendor_fk()
    for table, reason in _VENDOR_FK_NOT_PLAIN_REASSIGNED.items():
        assert reason, f"{table} needs a reason, not a bare exemption"
        assert table in discovered, (
            f"exempted table {table!r} no longer carries a vendor_id column — drop the exemption"
        )
