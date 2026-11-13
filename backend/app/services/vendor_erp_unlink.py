"""Clearing a vendor's ERP link in one company (docs/decisions.md §375).

A vendor holds at most one ERP link per company (``erp_link_realm``,
docs/decisions.md §284 / §356): the row's ``erp_vendor_id`` under
``erp_link_realm``, plus one kept ``vendor_erp_links`` row for every other
company it was linked in. Until this module nothing but a sync or a merge
changed them, and neither will clear a same-company link: a sync never re-judges
one, and a merge refuses two different ids in one company
(``vendor_merge_erp_link_conflict``, §373). A steward holding a stale duplicate
record had no way through that refusal but to leave the duplicate out.

:func:`unlink_vendor_erp_link` is that way through. It clears the vendor's link
in one company — the row link when it is that company's, the kept link for it,
or both should both exist — and the caller writes the ``vendor.erp_unlinked``
audit row from the :class:`ErpUnlinkResult`. What it does not do:

* **It never clears a link a push is still using.** While one of the vendor's
  invoices is in flight to that company (:func:`in_flight_invoice_count`) the
  unlink is refused 409 :data:`VENDOR_ERP_UNLINK_IN_FLIGHT`, counted, naming no
  invoice.
* **It is not a "never link" marker.** The id is free again in that company, so
  a later sync treats the record like any other unheld one: matched by id
  (nobody holds it now), restored (nothing is kept for it now), or adopted by
  name only when the name is unique among the pulled records AND among the
  unlinked local vendors (``services/vendor_sync``). A vendor still linked in
  that company is never a name candidate, so a sync can never put the cleared
  record onto the vendor a merge kept. A supplier record that should never link
  again is made inactive in the ERP; inactive records are never linked or
  created.

The consequence is visible, never silent: the vendor's next push to that
company is refused ``vendor_not_linked`` before any HTTP call, as for any
unlinked vendor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.refusals import coded_refusal
from app.models.invoice import Invoice
from app.models.invoice_status import InvoiceStatus
from app.models.vendor import Vendor, VendorErpLink
from app.models.workflow import WorkflowInstance

__all__ = [
    "IN_FLIGHT_STATUSES",
    "VENDOR_ERP_LINK_NOT_FOUND",
    "VENDOR_ERP_UNLINK_IN_FLIGHT",
    "ErpLinkView",
    "ErpUnlinkError",
    "ErpUnlinkResult",
    "in_flight_invoice_count",
    "unlink_vendor_erp_link",
    "vendor_erp_links",
]

#: The refusal codes (``frontend/src/lib/api/codedRefusals.ts``). Neither names
#: a vendor or an invoice: the steward is already on the vendor.
VENDOR_ERP_LINK_NOT_FOUND = "vendor_erp_link_not_found"
VENDOR_ERP_UNLINK_IN_FLIGHT = "vendor_erp_unlink_in_flight"

#: The statuses in which an invoice may still depend on the vendor's link:
#: a push dispatched or queued (``sending_to_erp`` — it reads the link when it
#: runs), a pushed bill awaiting the ERP's confirmation (``sent_to_erp``), and
#: a failed push whose outcome is unknown (``failed`` read through
#: ``services/erp.possibly_posted`` — its retry or reconcile pushes again).
#: From ``posted_in_erp`` on the bill carries its own id in the ERP, and
#: nothing after it (status sync-back, payment write-back) reads the vendor's
#: link, so clearing it cannot move that bill.
IN_FLIGHT_STATUSES: tuple[InvoiceStatus, ...] = (
    InvoiceStatus.sending_to_erp,
    InvoiceStatus.sent_to_erp,
    InvoiceStatus.failed,
)


@dataclass(frozen=True)
class ErpLinkView:
    """One of a vendor's ERP links. ``placement`` is ``row`` (the vendor's own
    ``erp_vendor_id``, the company it was last synced under; ``realm`` None when
    no company was recorded) or ``kept`` (a ``vendor_erp_links`` row, always
    named)."""

    realm: str | None
    erp_vendor_id: str
    placement: str


@dataclass
class ErpUnlinkResult:
    realm: str | None
    #: What was cleared, as ``{"placement", "erp_vendor_id"}`` per link — the
    #: audit row's ``details.cleared``.
    cleared: list[dict] = field(default_factory=list)


class ErpUnlinkError(Exception):
    """The unlink was refused; :attr:`detail` is the coded refusal."""

    def __init__(self, code: str, message: str, *, status_code: int, params: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.params = params or {}

    @property
    def detail(self) -> dict:
        return coded_refusal(self.code, self.message, **self.params)


async def vendor_erp_links(db: AsyncSession, vendor: Vendor) -> list[ErpLinkView]:
    """``vendor``'s links: the row link first, then the kept ones by realm."""
    links = (
        [ErpLinkView(vendor.erp_link_realm, vendor.erp_vendor_id, "row")]
        if vendor.erp_vendor_id
        else []
    )
    kept = (
        await db.execute(
            select(VendorErpLink.realm, VendorErpLink.erp_vendor_id)
            .where(VendorErpLink.vendor_id == vendor.id)
            .order_by(VendorErpLink.realm)
        )
    ).all()
    links.extend(ErpLinkView(realm, erp_id, "kept") for realm, erp_id in kept)
    return links


async def in_flight_invoice_count(db: AsyncSession, vendor: Vendor, realm: str | None) -> int:
    """How many of ``vendor``'s invoices may still be using its link in
    ``realm`` (:data:`IN_FLIGHT_STATUSES`). Fail-closed wherever the company is
    not on record:

    * ``sending_to_erp`` counts in every company: the push resolves the link
      under whatever connection it runs against, which no row records ahead;
    * ``failed`` counts in every company when its outcome is unknown, since
      the uncertain attempt's company is not recorded either (an
      ``erp_realm`` in its state may name an earlier push); a failure that
      certainly posted nothing does not count — its retry meets
      ``vendor_not_linked``, the unlink's visible consequence;
    * ``sent_to_erp`` counts where its recorded ``erp_realm`` is ``realm``, or
      is missing, or ``realm`` is None — a link that names no company may be
      any company's.
    """
    from app.services.erp import possibly_posted

    rows = (
        await db.execute(
            select(Invoice.id, Invoice.status, WorkflowInstance.state_data)
            .join(WorkflowInstance, WorkflowInstance.invoice_id == Invoice.id, isouter=True)
            .where(
                Invoice.organization_id == vendor.organization_id,
                Invoice.vendor_id == vendor.id,
                Invoice.status.in_(IN_FLIGHT_STATUSES),
            )
        )
    ).all()
    counted: set[uuid.UUID] = set()
    for invoice_id, status, state in rows:
        if status == InvoiceStatus.sending_to_erp:
            counted.add(invoice_id)
        elif status == InvoiceStatus.failed:
            if possibly_posted(state):
                counted.add(invoice_id)
        else:
            recorded = (state or {}).get("erp_realm")
            if realm is None or recorded is None or recorded == realm:
                counted.add(invoice_id)
    return len(counted)


async def unlink_vendor_erp_link(
    db: AsyncSession, *, vendor_id: uuid.UUID, realm: str | None
) -> ErpUnlinkResult:
    """Clear ``vendor_id``'s link in ``realm`` (None = the row's link that names
    no company). Locks the vendor and its kept links first — the same order a
    merge takes them, so the two cannot deadlock — and refuses before writing:

    * 404 :data:`VENDOR_ERP_LINK_NOT_FOUND` — the vendor holds no link there
      (an unknown vendor is the caller's 404, checked in entity scope first);
    * 409 :data:`VENDOR_ERP_UNLINK_IN_FLIGHT` — :func:`in_flight_invoice_count`
      is not zero (``params.invoices``).

    Does not commit: the caller writes the audit row in the same transaction.
    A push that moves to ``sending_to_erp`` after the check reads the link in
    its own transaction: before this one commits it still sees the link and
    posts with it (the bill is then the in-flight one the next unlink is
    refused over); after, it sees none and is refused ``vendor_not_linked``.
    Neither outcome posts to a supplier the vendor is not linked to.
    """
    vendor = (
        await db.execute(
            select(Vendor)
            .where(Vendor.id == vendor_id)
            .with_for_update()
            # The caller may have loaded the row already; read it under the lock.
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    kept = (
        (
            await db.execute(
                select(VendorErpLink)
                .where(VendorErpLink.vendor_id == vendor_id)
                .order_by(VendorErpLink.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    on_row = bool(vendor.erp_vendor_id) and vendor.erp_link_realm == realm
    # Never NULL-realm (§356), so a None realm matches only the row.
    kept_here = [link for link in kept if realm is not None and link.realm == realm]
    if not on_row and not kept_here:
        raise ErpUnlinkError(
            VENDOR_ERP_LINK_NOT_FOUND,
            "This vendor isn't linked to a supplier record in that ERP company.",
            status_code=404,
        )
    in_flight = await in_flight_invoice_count(db, vendor, realm)
    if in_flight:
        raise ErpUnlinkError(
            VENDOR_ERP_UNLINK_IN_FLIGHT,
            "An invoice to this vendor is being sent to that ERP company, or may "
            "already be there with its outcome unknown, so this link can't be "
            "cleared yet. Clear it once that invoice has posted or its push is resolved.",
            status_code=409,
            params={"invoices": in_flight},
        )
    result = ErpUnlinkResult(realm=realm)
    if on_row:
        result.cleared.append({"placement": "row", "erp_vendor_id": vendor.erp_vendor_id})
        vendor.erp_vendor_id = None
        vendor.erp_link_realm = None
    for link in kept_here:
        result.cleared.append({"placement": "kept", "erp_vendor_id": link.erp_vendor_id})
        await db.delete(link)
    await db.flush()
    return result
