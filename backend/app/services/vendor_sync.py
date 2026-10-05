"""Vendor ERP sync service — pull vendors from ERP and sync to local database.

Every vendor row this creates or changes writes an append-only audit row
(``vendor.synced_from_erp``), the same way ``services/qms_sync`` audits each
inspection it upserts. A vendor is a PAYEE: a bulk ERP pull that silently
materialised new payee rows was the one vendor-mutating path with no record of
where the row came from or who ran the pull. Rows the sync leaves byte-identical
(``unchanged``) write nothing — an audit row for a state change that did not
happen is unbounded growth describing nothing.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.vendor import Vendor
from app.services.audit_dispatch import dispatch_audit
from app.services.vendor_tax_id import rekey_tax_id


async def sync_vendors_from_erp(
    db: AsyncSession,
    organization_id: uuid.UUID,
    erp_vendors: list[dict],
    entity_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
) -> dict:
    """Sync a list of vendor records from an ERP into the local database.

    ``entity_id`` (multi-entity Phase 2) is the entity newly-created vendors
    land under — the selected entity or the tenant default, resolved at the
    endpoint. Vendors matched/updated keep their existing entity.

    ``actor_id`` is the AP user who triggered the pull; it lands on every audit
    row this writes. ``None`` means the sync had no human behind it (a future
    background caller), which the trail records as a system action rather than
    attributing it to nobody in particular.

    Each erp_vendor dict should have:
        - erp_vendor_id: str (required — the vendor ID in the ERP)
        - name: str
        - code: str | None
        - email: str | None
        - phone: str | None
        - address: str | None
        - tax_id: str | None
        - payment_terms: str | None

    Returns summary: {created: int, updated: int, unchanged: int}
    """
    now = datetime.now(UTC)
    created = 0
    updated = 0
    unchanged = 0
    # (vendor, change, erp_vendor_id) for every row this pull actually touched.
    # Audited after the loop's single flush rather than inline: `Vendor.id` is a
    # column default SQLAlchemy evaluates at INSERT time, so a newly-created
    # vendor audited before the flush would produce a row whose `entity_id` is
    # NULL — evidence pointing at nothing.
    audited: list[tuple[Vendor, str, str]] = []

    for erp_v in erp_vendors:
        erp_id = erp_v.get("erp_vendor_id")
        if not erp_id:
            continue

        # Check if vendor already exists by ERP ID
        result = await db.execute(
            select(Vendor).where(
                Vendor.erp_vendor_id == erp_id,
                Vendor.organization_id == organization_id,
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            # Update fields if changed
            changed = False
            for field in ("name", "code", "email", "phone", "address", "payment_terms"):
                new_val = erp_v.get(field)
                if new_val is not None and getattr(existing, field) != new_val:
                    setattr(existing, field, new_val)
                    changed = True
            # Through the shared writer: an ERP-side TIN change voids the old
            # IRS-match stamp rather than inheriting it.
            new_tax_id = erp_v.get("tax_id")
            if new_tax_id is not None and rekey_tax_id(existing, new_tax_id):
                changed = True

            existing.erp_synced_at = now
            if changed:
                updated += 1
                audited.append((existing, "updated", erp_id))
            else:
                unchanged += 1
        else:
            # Also check if there's a name match (might be manually created before ERP sync)
            name = erp_v.get("name", "")
            if name:
                result = await db.execute(
                    select(Vendor).where(
                        Vendor.name == name,
                        Vendor.organization_id == organization_id,
                        Vendor.erp_vendor_id.is_(None),
                    )
                )
                name_match = result.scalar_one_or_none()
                if name_match:
                    # Link existing vendor to ERP
                    name_match.erp_vendor_id = erp_id
                    name_match.erp_synced_at = now
                    for field in ("code", "email", "phone", "address", "payment_terms"):
                        new_val = erp_v.get(field)
                        if new_val is not None:
                            setattr(name_match, field, new_val)
                    if erp_v.get("tax_id") is not None:
                        rekey_tax_id(name_match, erp_v["tax_id"])
                    if name_match.status == "unverified":
                        name_match.status = "active"
                        name_match.source = "erp_sync"
                    updated += 1
                    audited.append((name_match, "linked", erp_id))
                    continue

            # Create new vendor
            vendor = Vendor(
                name=name or f"ERP Vendor {erp_id}",
                code=erp_v.get("code"),
                email=erp_v.get("email"),
                phone=erp_v.get("phone"),
                address=erp_v.get("address"),
                tax_id=erp_v.get("tax_id"),
                payment_terms=erp_v.get("payment_terms"),
                erp_vendor_id=erp_id,
                erp_synced_at=now,
                status="active",
                source="erp_sync",
                organization_id=organization_id,
                entity_id=entity_id,
            )
            db.add(vendor)
            created += 1
            audited.append((vendor, "created", erp_id))

    if audited:
        await db.flush()
        for vendor, change, erp_id in audited:
            await _audit_synced(
                db,
                vendor=vendor,
                organization_id=organization_id,
                actor_id=actor_id,
                erp_vendor_id=erp_id,
                change=change,
            )

    return {"created": created, "updated": updated, "unchanged": unchanged}


async def _audit_synced(
    db: AsyncSession,
    *,
    vendor: Vendor,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    erp_vendor_id: str,
    change: str,
) -> None:
    """One append-only row per vendor the sync actually changed.

    ``change`` distinguishes the three things this sync does: ``created`` (a new
    payee row), ``updated`` (fields refreshed on a row already linked to the
    ERP) and ``linked`` (a manually-created vendor adopted by the ERP id, which
    can also promote it out of ``unverified``).

    PII-free: the vendor's name and code — the same two fields the
    ``vendor.created`` row already records — plus the ERP's own identifier.
    Never ``tax_id``, ``address``, ``email`` or ``phone``, all of which the ERP
    payload carries and this sync writes onto the row.
    """
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=organization_id,
        actor_id=actor_id,
        action="vendor.synced_from_erp",
        entity_type="vendor",
        entity_id=vendor.id,
        details={
            "change": change,
            "erp_vendor_id": erp_vendor_id,
            "name": vendor.name,
            "code": vendor.code,
        },
    )
