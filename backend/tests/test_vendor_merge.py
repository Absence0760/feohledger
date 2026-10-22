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
from app.models.vendor import Vendor
from app.models.vendor_priors import VendorExtractionPrior
from app.models.workflow import AuditLog
from app.services.vendor_merge import (
    VENDOR_MERGE_BLOCKED_DUPLICATE,
    VENDOR_MERGE_CANONICAL_NOT_FOUND,
    VENDOR_MERGE_CROSS_ENTITY,
    VENDOR_MERGE_DUPLICATE_NOT_FOUND,
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
