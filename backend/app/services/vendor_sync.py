"""Vendor ERP sync service — pull vendors from ERP and sync to local database.

Every vendor row this creates or changes writes an append-only audit row
(``vendor.synced_from_erp``), the same way ``services/qms_sync`` audits each
inspection it upserts. A vendor is a PAYEE: a bulk ERP pull that silently
materialised new payee rows was the one vendor-mutating path with no record of
where the row came from or who ran the pull. Rows the sync leaves byte-identical
(``unchanged``) write nothing — an audit row for a state change that did not
happen is unbounded growth describing nothing.

**A link counts only in the ERP company it was made in** (docs/decisions.md
§284, ``services/erp_link_realm``). QuickBooks numbers vendors per company, so a
tenant reconnected from its sandbox to its production company would otherwise
match production vendor "57" to the local vendor linked to sandbox "57" and
overwrite its name. A link from another realm — or from before realms were
recorded — is kept only when the pulled record with that id is recognisably the
same supplier (same name, or the same tax id); otherwise it is dropped, audited
(``change: "unlinked"``), and the record is matched afresh like any other.
"""

import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.vendor import Vendor
from app.services.audit_dispatch import dispatch_audit
from app.services.vendor_screening import screen_best_effort
from app.services.vendor_tax_id import rekey_tax_id


async def sync_vendors_from_erp(
    db: AsyncSession,
    organization_id: uuid.UUID,
    erp_vendors: list[dict],
    entity_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    org_settings: dict | None = None,
    realm: str | None = None,
) -> dict:
    """Sync a list of vendor records from an ERP into the local database.

    ``entity_id`` (multi-entity Phase 2) is the entity newly-created vendors
    land under — the selected entity or the tenant default, resolved at the
    endpoint. Vendors matched/updated keep their existing entity.

    ``actor_id`` is the AP user who triggered the pull; it lands on every audit
    row this writes. ``None`` means the sync had no human behind it (a future
    background caller), which the trail records as a system action rather than
    attributing it to nobody in particular.

    ``org_settings`` selects the sanctions provider for the re-screen an
    existing vendor owes when the ERP changes its identity (``name`` or
    ``tax_id`` — the fields the adapters screen on), exactly as the same edit
    through ``PATCH /api/vendors/{id}`` re-screens. Best-effort, like every
    other identity re-screen: a provider failure never rolls the sync back.
    Newly-created rows are left to the periodic sweep (they start with a NULL
    ``last_screened_at``, so they are due on its first tick) and to the
    pre-payment screen.

    Each erp_vendor dict should have:
        - erp_vendor_id: str (required — the vendor ID in the ERP)
        - name: str
        - code: str | None
        - email: str | None
        - phone: str | None
        - address: str | None
        - tax_id: str | None
        - payment_terms: str | None

    **A name only ever links a vendor when it is unambiguous on both sides.**
    A record whose ``erp_vendor_id`` no local vendor holds may adopt a
    manually-created, still-unlinked vendor of the same name — but only when
    exactly one record in this pull has that name AND exactly one unlinked local
    vendor does. Otherwise the record is neither linked nor created and is
    reported in ``conflicts``: linking the wrong "Acme" posts its bills to
    another supplier's ERP record, and creating a fresh row beside the
    unlinked one(s) would only add another candidate for the same name
    (docs/decisions.md §284). Linking stays by hand.

    ``realm`` is the connection the pull came from
    (``erp_link_realm.connection_realm``); every link this writes is stamped
    with it, and links from any other realm are re-judged first (module
    docstring).

    **Inactive records** (``active`` False) are never created or linked as a
    payee; a vendor linked to one keeps its link and, if ``active``, becomes
    ``inactive`` — never the reverse, which is a person's call.

    **``code`` the pre-§284 QuickBooks mapping wrote** (the record's
    ``AcctNum``, the company's account number AT the vendor, sent here as
    ``legacy_code``) is cleared from vendors the sync itself created
    (``source == "erp_sync"``): CSV import resolves vendors by ``code``, and
    that value was never our identifier for the vendor.

    Returns summary: {created, updated, unchanged: int, conflicts: list of
    {"name", "reason", "erp_vendor_ids"}, unlinked: names whose link this run
    dropped, deactivated / codes_cleared / realm_adopted: int, realm} — the
    name is the vendor's business name, which the per-vendor audit row already
    records.
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
    audited: list[tuple[Vendor, str, str, dict]] = []
    # Existing vendors whose screened identity this pull changed.
    rescreen: list[Vendor] = []
    conflicts: dict[str, set[str]] = {}
    unlinked: list[str] = []
    deactivated = 0
    codes_cleared = 0
    realm_adopted = 0

    # -- links made under another connection --------------------------------
    by_id: dict[str, dict] = {}
    for erp_v in erp_vendors:
        if erp_v.get("erp_vendor_id"):
            by_id.setdefault(str(erp_v["erp_vendor_id"]), erp_v)
    linked = (
        (
            await db.execute(
                select(Vendor).where(
                    Vendor.organization_id == organization_id,
                    Vendor.erp_vendor_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for vendor in linked:
        if vendor.erp_link_realm == realm:
            continue
        record = by_id.get(vendor.erp_vendor_id)
        if record is not None and _same_vendor(vendor, record):
            if vendor.erp_link_realm is None:
                realm_adopted += 1
            else:
                audited.append(
                    (
                        vendor,
                        "realm_changed",
                        vendor.erp_vendor_id,
                        {"from_realm": vendor.erp_link_realm, "to_realm": realm},
                    )
                )
            vendor.erp_link_realm = realm
            continue
        # Another company's id, or one this pull cannot vouch for: never
        # matched by it, never posted to (the push already refuses it).
        audited.append(
            (vendor, "unlinked", vendor.erp_vendor_id, {"from_realm": vendor.erp_link_realm})
        )
        unlinked.append(vendor.name)
        vendor.erp_vendor_id = None
        vendor.erp_link_realm = None
    # Flushed so the name counts below see the dropped links as unlinked.
    await db.flush()

    # How many records in this pull, and how many unlinked local vendors, carry
    # each name — the two counts a name link must find to be exactly 1.
    pulled_names = Counter(
        v.get("name")
        for v in erp_vendors
        if v.get("erp_vendor_id") and v.get("name") and v.get("active", True)
    )
    unlinked_names = dict(
        (
            await db.execute(
                select(Vendor.name, func.count())
                .where(
                    Vendor.organization_id == organization_id,
                    Vendor.erp_vendor_id.is_(None),
                )
                .group_by(Vendor.name)
            )
        ).all()
    )

    for erp_v in erp_vendors:
        erp_id = erp_v.get("erp_vendor_id")
        if not erp_id:
            continue

        active = erp_v.get("active", True) is not False

        # Check if vendor already exists by ERP ID — in this realm only; the
        # pass above left no link from another one.
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
            identity_changed = False
            for field in ("name", "code", "email", "phone", "address", "payment_terms"):
                new_val = erp_v.get(field)
                if new_val is not None and getattr(existing, field) != new_val:
                    setattr(existing, field, new_val)
                    changed = True
                    identity_changed = identity_changed or field == "name"
            # Through the shared writer: an ERP-side TIN change voids the old
            # IRS-match stamp rather than inheriting it.
            new_tax_id = erp_v.get("tax_id")
            if new_tax_id is not None and rekey_tax_id(existing, new_tax_id):
                changed = True
                identity_changed = True
            if identity_changed:
                rescreen.append(existing)
            extra: dict = {}
            legacy_code = erp_v.get("legacy_code")
            if existing.source == "erp_sync" and legacy_code and existing.code == legacy_code:
                # The old QuickBooks mapping's AcctNum; never our code.
                existing.code = None
                codes_cleared += 1
                extra["code_cleared"] = True
                changed = True
            if not active and existing.status == "active":
                existing.status = "inactive"
                deactivated += 1
                extra["deactivated"] = True
                changed = True

            existing.erp_synced_at = now
            if changed:
                updated += 1
                audited.append((existing, "updated", erp_id, extra))
            else:
                unchanged += 1
        elif not active:
            # An inactive ERP vendor is not a payee we start paying.
            continue
        else:
            # Also check if there's a name match (might be manually created before ERP sync)
            name = erp_v.get("name", "")
            if (
                name
                and unlinked_names.get(name, 0)
                and (pulled_names[name] > 1 or unlinked_names[name] > 1)
            ):
                # Ambiguous: which ERP record is which local vendor is a guess.
                conflicts.setdefault(name, set()).add(str(erp_id))
                continue
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
                    name_match.erp_link_realm = realm
                    name_match.erp_synced_at = now
                    for field in ("code", "email", "phone", "address", "payment_terms"):
                        new_val = erp_v.get(field)
                        if new_val is not None:
                            setattr(name_match, field, new_val)
                    if erp_v.get("tax_id") is not None and rekey_tax_id(
                        name_match, erp_v["tax_id"]
                    ):
                        rescreen.append(name_match)
                    if name_match.status == "unverified":
                        name_match.status = "active"
                        name_match.source = "erp_sync"
                    updated += 1
                    audited.append((name_match, "linked", erp_id, {}))
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
                erp_link_realm=realm,
                erp_synced_at=now,
                status="active",
                source="erp_sync",
                organization_id=organization_id,
                entity_id=entity_id,
            )
            db.add(vendor)
            created += 1
            audited.append((vendor, "created", erp_id, {}))

    if audited:
        await db.flush()
        for vendor, change, erp_id, extra in audited:
            await _audit_synced(
                db,
                vendor=vendor,
                organization_id=organization_id,
                actor_id=actor_id,
                erp_vendor_id=erp_id,
                change=change,
                extra=extra,
            )

    # After the audit rows, so a screen's own `vendor.screened` row follows the
    # `vendor.synced_from_erp` row that explains why it ran.
    for vendor in rescreen:
        await screen_best_effort(
            db,
            vendor=vendor,
            org_settings=org_settings,
            org_id=organization_id,
            check_type="initial",
            actor_id=actor_id,
        )

    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "conflicts": [
            {"name": name, "reason": "ambiguous_name", "erp_vendor_ids": sorted(ids)}
            for name, ids in sorted(conflicts.items())
        ],
        "unlinked": sorted(unlinked),
        "deactivated": deactivated,
        "codes_cleared": codes_cleared,
        "realm_adopted": realm_adopted,
        "realm": realm,
    }


def _norm_name(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _same_vendor(vendor: Vendor, record: dict) -> bool:
    """Is the pulled ``record`` recognisably the supplier ``vendor`` was linked
    to? The evidence a link from another realm must show to be kept: the same
    name (case and spacing aside), or the same non-blank tax id."""
    if _norm_name(vendor.name) and _norm_name(vendor.name) == _norm_name(record.get("name")):
        return True
    tax_id = (record.get("tax_id") or "").strip()
    return bool(tax_id) and tax_id == (vendor.tax_id or "").strip()


async def _audit_synced(
    db: AsyncSession,
    *,
    vendor: Vendor,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    erp_vendor_id: str,
    change: str,
    extra: dict | None = None,
) -> None:
    """One append-only row per vendor the sync actually changed.

    ``change`` distinguishes what this sync does: ``created`` (a new payee
    row), ``updated`` (fields refreshed on a row already linked to the ERP —
    ``extra`` says when that cleared a legacy ``code`` or deactivated it),
    ``linked`` (a manually-created vendor adopted by the ERP id, which can also
    promote it out of ``unverified``), ``unlinked`` (a link from another ERP
    company dropped; ``erp_vendor_id`` is the id it held) and ``realm_changed``
    (the same supplier found under the same id in the new company). ``extra``
    carries the realms involved, which name ERP companies, not people.

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
            **(extra or {}),
        },
    )
