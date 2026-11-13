"""A vendor's ERP link in each company it has been linked under (docs/decisions.md §356).

An ERP vendor id names a supplier only inside the company that issued it
(§284, ``services/erp_link_realm``). ``vendors.erp_vendor_id`` +
``erp_link_realm`` hold the link in the company the vendor was last synced
under; ``vendor_erp_links`` (:class:`~app.models.vendor.VendorErpLink`) keeps the
vendor's link in every OTHER named company, so a tenant that switches ERP — or
QuickBooks sandbox to production — and back gets its links back instead of
re-deriving them from names.

Two rules keep that safe, and both live here:

* **The push and the PO sync read a link only under the current realm**
  (:func:`vendor_erp_ids`): the row's link when it is live there, else the kept
  link for exactly that realm. A link ERP A set is never handed to ERP B — the
  vendor is unlinked in B and the push refuses it ``vendor_not_linked``.
* **Only a link with a named realm is kept** (:func:`keep_link`). A NULL-realm
  link (made before migration 0115, or by a sync with no realm) records nothing
  about which company issued it; filing it under a guessed company is the
  manufactured evidence §141 refuses. It is dropped exactly as before.

``services/vendor_sync`` is the only writer: it keeps the row's link before a
sync under another company moves or drops it, and restores a kept link when a
sync runs under that link's company again.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.vendor import Vendor, VendorErpLink
from app.services.erp_link_realm import link_is_live

__all__ = ["keep_link", "vendor_erp_ids"]


class _VendorLink(Protocol):
    id: uuid.UUID
    erp_vendor_id: str | None
    erp_link_realm: str | None


async def vendor_erp_ids(
    db: AsyncSession,
    organization_id: uuid.UUID,
    vendors: Iterable[_VendorLink],
    *,
    realm: str | None,
    trust_legacy: bool,
) -> dict[uuid.UUID, str]:
    """Each vendor's ERP id in ``realm``, for the vendors linked there.

    ``vendors`` are Vendor rows or rows of ``(id, erp_vendor_id,
    erp_link_realm)``. A vendor's row link counts when it is live in ``realm``
    (``erp_link_realm.link_is_live``, the §284 rule, legacy trust included);
    otherwise its kept link for ``realm``, if any. A vendor with neither is
    absent — "not linked". ``realm`` None (``mock``, or an OAuth ERP with no
    consent) filters nothing, as before, and reads no kept link: none is filed
    under no company.
    """
    rows = list(vendors)
    found = {
        v.id: v.erp_vendor_id
        for v in rows
        if v.erp_vendor_id and link_is_live(v.erp_link_realm, realm, trust_legacy=trust_legacy)
    }
    if realm is None:
        return found
    wanted = {v.id for v in rows} - found.keys()
    if not wanted:
        return found
    # Every link kept for this realm in the org, filtered here: bounded by the
    # vendors that were linked in this company and since left it, where an IN
    # list would be as long as the caller's vendor set.
    kept = await db.execute(
        select(VendorErpLink.vendor_id, VendorErpLink.erp_vendor_id).where(
            VendorErpLink.organization_id == organization_id,
            VendorErpLink.realm == realm,
        )
    )
    found.update({vid: erp_id for vid, erp_id in kept.all() if vid in wanted})
    return found


async def keep_link(db: AsyncSession, vendor: Vendor) -> bool:
    """Keep ``vendor``'s current row link under its realm, before the caller
    moves or drops it. Returns whether anything was kept: a NULL-realm link, or
    no link, keeps nothing (module docstring).

    One kept link per ``(vendor, realm)``: a later one for the same company
    replaces the earlier, since the row only ever held one at a time.
    """
    if not vendor.erp_vendor_id or vendor.erp_link_realm is None:
        return False
    stmt = pg_insert(VendorErpLink).values(
        id=uuid.uuid4(),
        organization_id=vendor.organization_id,
        vendor_id=vendor.id,
        realm=vendor.erp_link_realm,
        erp_vendor_id=vendor.erp_vendor_id,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_vendor_erp_links_vendor_realm",
            set_={"erp_vendor_id": stmt.excluded.erp_vendor_id, "updated_at": func.now()},
        )
    )
    return True
