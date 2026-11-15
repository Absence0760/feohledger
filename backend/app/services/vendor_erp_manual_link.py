"""Linking a vendor to an ERP supplier record by hand (docs/decisions.md §415).

Every link until now came from a vendor sync, which only sees the records the
adapter's walk lists. Xero's walk reads ``IsSupplier==true OR
IsCustomer==false`` plus every unbilled PO's contact (§386): a contact that is
already a CUSTOMER and has had no bill and no PO from us carries neither flag,
so no sync can see it, and its first push is refused ``vendor_not_linked``
until something else makes Xero flag it a supplier. Only a bill can, and the
bill is what is refused.

So a person on the vendor page searches the ERP's whole contact list by name or
by the vendor's tax number (:func:`search_candidates`), and links the record
they choose (:func:`link_vendor_to_erp_record`). The rules:

* **The record is re-read by its id at link time** (``ErpAdapter.get_vendor``),
  never taken from what the browser sends back: it must exist and be active
  (an archived or erased contact is no payee, the sync's own rule).
* **One link per vendor per company, one vendor per record per company.** A
  vendor that already holds a link in this company (on its row, or kept for it,
  or a row link that names no company and may be this one) is refused
  :data:`VENDOR_ERP_LINK_EXISTS`; unlink it first (§375). A record another
  vendor holds here is refused :data:`ERP_VENDOR_ALREADY_LINKED`. Both are
  checked under the vendor row lock and a transaction-scoped advisory lock on
  ``(organization, realm)`` (:func:`link_lock`), which the vendor sync takes
  too (every writer that can add a new holder of a record; unlink only clears
  and a merge only moves an existing holder's link), so neither two people
  linking one record to two vendors at once nor a sync adopting it by name in
  the same moment can both pass.
* **A row link from another named company is kept**, not dropped
  (``vendor_erp_links.keep_link``, §356), so a switch back restores it.
* **Only the link is written.** The vendor's name, tax id and other fields stay
  as the steward keyed them; the next sync that lists the record updates them
  by its normal rules (it now matches by id). A link to a record no walk lists
  survives later syncs: a sync never re-judges a same-company link.
* **The search answers no tax number.** A candidate says only whether its tax
  number matches the vendor's (``tax_id_matches``, separators and case aside);
  the number itself never leaves the server, and the vendor's own number is
  read from its row, never sent in a URL.

The caller writes the ``vendor.erp_linked`` audit row from
:class:`ErpManualLinkResult` in the same transaction.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.refusals import coded_refusal
from app.models.vendor import Vendor, VendorErpLink
from app.services.erp_adapters.base import ErpAdapter
from app.services.vendor_erp_links import keep_link

__all__ = [
    "ERP_VENDOR_ALREADY_LINKED",
    "ERP_VENDOR_INACTIVE",
    "ERP_VENDOR_NOT_FOUND",
    "VENDOR_ERP_LINK_EXISTS",
    "VENDOR_ERP_SEARCH_UNAVAILABLE",
    "ErpManualLinkError",
    "ErpManualLinkResult",
    "ErpVendorCandidate",
    "link_lock",
    "link_vendor_to_erp_record",
    "search_available",
    "search_candidates",
]

#: The refusal codes (``frontend/src/lib/api/codedRefusals.ts``). None names a
#: vendor, a record's name or a tax number: the steward is on the vendor.
VENDOR_ERP_SEARCH_UNAVAILABLE = "vendor_erp_search_unavailable"
VENDOR_ERP_LINK_EXISTS = "vendor_erp_link_exists"
ERP_VENDOR_ALREADY_LINKED = "erp_vendor_already_linked"
ERP_VENDOR_NOT_FOUND = "erp_vendor_not_found"
ERP_VENDOR_INACTIVE = "erp_vendor_inactive"

#: The longest search text taken (a business name, not a document).
SEARCH_QUERY_MAX = 100


class ErpManualLinkError(Exception):
    """The search or the link was refused; :attr:`detail` is the coded refusal."""

    def __init__(self, code: str, message: str, *, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    @property
    def detail(self) -> dict:
        return coded_refusal(self.code, self.message)

    @classmethod
    def unavailable(cls) -> ErpManualLinkError:
        return cls(
            VENDOR_ERP_SEARCH_UNAVAILABLE,
            "The connected ERP can't be searched for a supplier record from here, or no "
            "ERP company is connected.",
            status_code=409,
        )


def search_available(adapter_cls: type[ErpAdapter] | None, realm: str | None) -> bool:
    """Can a vendor be searched for and linked by hand now? Only on an adapter
    that implements it, connected to a named company (every link is stamped
    with its company, §284; a link with none is the kind §356 cannot keep)."""
    return bool(adapter_cls is not None and adapter_cls.supports_vendor_search and realm)


async def link_lock(db: AsyncSession, organization_id: uuid.UUID, realm: str) -> None:
    """Take the transaction-scoped lock every writer that can add a new
    holder of a record in ``realm`` holds: this module's link, and
    ``services/vendor_sync`` (which links records by id and by name). An
    unlink only clears a link and a merge only moves an existing holder's,
    so neither takes it. Released at commit or rollback."""
    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"vendor-erp-link:{organization_id}:{realm}", 0)
            )
        )
    )


def _norm_tax(value: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", value or "").upper()


@dataclass(frozen=True)
class ErpVendorCandidate:
    erp_vendor_id: str
    name: str
    email: str | None
    active: bool
    #: The record's tax number equals the vendor's (separators and case aside).
    tax_id_matches: bool
    #: Another vendor here already holds this record in this company. A
    #: boolean, not the vendor's id: realm uniqueness is org-wide, but the
    #: holder may sit in an entity the searching user cannot see.
    linked_elsewhere: bool


async def _holders(
    db: AsyncSession, organization_id: uuid.UUID, realm: str, erp_ids: list[str]
) -> dict[str, uuid.UUID]:
    """Which vendor holds each of ``erp_ids`` in ``realm``: on its row (named
    ``realm``, or naming no company — such a link may be this company's), or
    kept for ``realm``."""
    if not erp_ids:
        return {}
    out: dict[str, uuid.UUID] = {}
    rows = await db.execute(
        select(Vendor.erp_vendor_id, Vendor.id).where(
            Vendor.organization_id == organization_id,
            Vendor.erp_vendor_id.in_(erp_ids),
            or_(Vendor.erp_link_realm == realm, Vendor.erp_link_realm.is_(None)),
        )
    )
    for erp_id, vendor_id in rows.all():
        out.setdefault(str(erp_id), vendor_id)
    kept = await db.execute(
        select(VendorErpLink.erp_vendor_id, VendorErpLink.vendor_id).where(
            VendorErpLink.organization_id == organization_id,
            VendorErpLink.realm == realm,
            VendorErpLink.erp_vendor_id.in_(erp_ids),
        )
    )
    for erp_id, vendor_id in kept.all():
        out.setdefault(str(erp_id), vendor_id)
    return out


async def search_candidates(
    db: AsyncSession,
    adapter: ErpAdapter,
    vendor: Vendor,
    *,
    realm: str | None,
    query: str | None,
) -> tuple[list[ErpVendorCandidate], bool]:
    """``(candidates, truncated)``: the ERP records matching ``query`` (the
    vendor's own name when blank) or the vendor's tax number, each saying
    whether its tax number matches and whether another vendor holds it here.
    Tax matches first, then by name. Refused :data:`VENDOR_ERP_SEARCH_
    UNAVAILABLE` when :func:`search_available` says no. Raises what the
    adapter raises on a failed read (the caller's 502)."""
    if not search_available(type(adapter), realm):
        raise ErpManualLinkError.unavailable()
    assert realm is not None
    name = (query or "").strip()[:SEARCH_QUERY_MAX] or vendor.name
    result = await adapter.search_vendors(name=name, tax_id=vendor.tax_id)
    ours = _norm_tax(vendor.tax_id)
    holders = await _holders(
        db, vendor.organization_id, realm, [v.erp_vendor_id for v in result.vendors]
    )
    candidates = [
        ErpVendorCandidate(
            erp_vendor_id=v.erp_vendor_id,
            name=v.name,
            email=v.email,
            active=v.active,
            tax_id_matches=bool(ours) and _norm_tax(v.tax_id) == ours,
            linked_elsewhere=holders.get(v.erp_vendor_id, vendor.id) != vendor.id,
        )
        for v in result.vendors
    ]
    candidates.sort(key=lambda c: (not c.tax_id_matches, c.name.casefold()))
    return candidates, result.truncated


@dataclass
class ErpManualLinkResult:
    realm: str
    erp_vendor_id: str
    #: The ERP's name for the record at link time (the audit row's evidence of
    #: which supplier the steward chose).
    erp_name: str
    tax_id_matches: bool
    #: The row link from another company that was kept for it, if any.
    kept: dict | None


async def link_vendor_to_erp_record(
    db: AsyncSession,
    adapter: ErpAdapter,
    *,
    vendor_id: uuid.UUID,
    realm: str | None,
    erp_vendor_id: str,
) -> ErpManualLinkResult:
    """Link ``vendor_id`` to ``erp_vendor_id`` in ``realm`` (module docstring).

    Refusals, before anything is written: 409 :data:`VENDOR_ERP_SEARCH_
    UNAVAILABLE`; 409 :data:`VENDOR_ERP_LINK_EXISTS`; 409
    :data:`ERP_VENDOR_ALREADY_LINKED`; 404 :data:`ERP_VENDOR_NOT_FOUND`; 409
    :data:`ERP_VENDOR_INACTIVE`. Does not commit: the caller writes the audit
    row in the same transaction. The ERP read happens before the locks, so no
    row lock is held across a provider round trip.
    """
    if not search_available(type(adapter), realm):
        raise ErpManualLinkError.unavailable()
    assert realm is not None
    erp_vendor_id = str(erp_vendor_id or "").strip()
    record = await adapter.get_vendor(erp_vendor_id) if erp_vendor_id else None
    if record is None:
        raise ErpManualLinkError(
            ERP_VENDOR_NOT_FOUND,
            "That supplier record isn't in the connected ERP company.",
            status_code=404,
        )
    if not record.active:
        raise ErpManualLinkError(
            ERP_VENDOR_INACTIVE,
            "That supplier record is archived or inactive in the ERP, so it can't be linked.",
            status_code=409,
        )
    erp_vendor_id = record.erp_vendor_id
    # The company-wide lock first, then the vendor row: the order the vendor
    # sync takes them in (it locks, then updates vendor rows), so the two can
    # never deadlock.
    organization_id = (
        await db.execute(select(Vendor.organization_id).where(Vendor.id == vendor_id))
    ).scalar_one()
    await link_lock(db, organization_id, realm)
    vendor = (
        await db.execute(
            select(Vendor)
            .where(Vendor.id == vendor_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    kept_here = (
        await db.execute(
            select(VendorErpLink.id).where(
                VendorErpLink.vendor_id == vendor.id, VendorErpLink.realm == realm
            )
        )
    ).first()
    if (vendor.erp_vendor_id and vendor.erp_link_realm in (realm, None)) or kept_here:
        raise ErpManualLinkError(
            VENDOR_ERP_LINK_EXISTS,
            "This vendor is already linked to a supplier record in this ERP company. "
            "Clear that link first.",
            status_code=409,
        )
    holder = (await _holders(db, vendor.organization_id, realm, [erp_vendor_id])).get(erp_vendor_id)
    if holder is not None:
        raise ErpManualLinkError(
            ERP_VENDOR_ALREADY_LINKED,
            "Another vendor is already linked to that supplier record in this ERP company.",
            status_code=409,
        )
    kept = None
    if vendor.erp_vendor_id:
        # A link from another named company (the None / same-realm cases were
        # refused above): kept for it, so a switch back restores it (§356).
        kept = {"realm": vendor.erp_link_realm, "erp_vendor_id": vendor.erp_vendor_id}
        await keep_link(db, vendor)
    vendor.erp_vendor_id = erp_vendor_id
    vendor.erp_link_realm = realm
    await db.flush()
    ours = _norm_tax(vendor.tax_id)
    return ErpManualLinkResult(
        realm=realm,
        erp_vendor_id=erp_vendor_id,
        erp_name=record.name,
        tax_id_matches=bool(ours) and _norm_tax(record.tax_id) == ours,
        kept=kept,
    )
