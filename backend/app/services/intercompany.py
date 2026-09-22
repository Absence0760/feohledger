"""Inter-company invoice routing (multi-entity).

An inter-company charge is an invoice owed between two legal entities /
subsidiaries of the SAME tenant: entity A bills entity B. When such an invoice
names a counterparty entity (``invoice.counterparty_entity_id``), this service
generates the mirror **payable** under the counterparty entity, links the two
invoices bidirectionally, and audits both — so each subsidiary's books reflect
the transaction.

Design notes:
- **Idempotent, at three layers.** ``intercompany_mirror_id`` on the origin is
  the dedupe guard: if it's already set, the mirror exists and the existing one
  is returned — a second call never creates a duplicate payable. That in-memory
  check is only safe because the caller holds a row lock on the origin
  (``workflow_engine.get_invoice_for_update``) and because the partial unique
  index ``uq_invoice_intercompany_mirror`` (migration 0075) makes a second row
  pointing at the same origin impossible to persist. No money moves here (the
  mirror enters the normal approval queue at ``new``), but a duplicate payable
  is a live double liability — it can be approved and paid on its own — so all
  three layers are mandatory. **Callers must lock the origin first**; without
  the lock two concurrent callers both read NULL and both insert.
- **Money is exact.** The mirror copies the origin ``amount`` (``Decimal``)
  verbatim — never a float round-trip.
- The mirror enters via the normal workflow entry point
  (``workflow_engine.create_workflow_instance``) at status ``new``, exactly like
  any other freshly-created invoice — it is NOT slipped past the state machine.
- **Segregation of duties crosses the entity boundary.** The mirror's terms are
  the source's terms, copied verbatim, so everyone implicated in the source
  payable (``approval_chain.implicated_actors``: its uploader plus its
  ``segregation_actor_ids``) is implicated in the mirror, and the routing actor
  — who created the mirror — is its uploader. Entities subdivide a tenant's
  books; they are not a boundary a payable's authors stop being its authors at.
  See docs/decisions.md §192.

See backend/docs/inter-company.md.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceStatus
from app.services.approval_chain import implicated_actors
from app.services.audit_dispatch import dispatch_audit
from app.services.workflow_engine import create_workflow_instance


async def route_intercompany_invoice(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID | None = None,
) -> Invoice:
    """Generate (or return the existing) mirror payable for an inter-company charge.

    Precondition: ``invoice.counterparty_entity_id`` is set and differs from
    ``invoice.entity_id`` — a subsidiary cannot bill itself. Violations raise
    ``ValueError`` (the caller maps this to a 4xx).

    Idempotent: if ``invoice.intercompany_mirror_id`` is already set, the mirror
    exists; it is loaded and returned without creating a second one. **The caller
    must hold a row lock on ``invoice``** (``get_invoice_for_update``) — this
    check reads in-memory state, so without the lock two concurrent callers both
    see NULL and both create a mirror. The ``uq_invoice_intercompany_mirror``
    partial unique index is the DB-level backstop for that.

    Returns the mirror Invoice.
    """
    counterparty_id = invoice.counterparty_entity_id
    if counterparty_id is None:
        raise ValueError("Invoice has no counterparty_entity_id to route to.")
    if counterparty_id == invoice.entity_id:
        raise ValueError("An entity cannot bill itself (counterparty equals own entity).")

    # Idempotency: the mirror already exists — return it, never create a second.
    if invoice.intercompany_mirror_id is not None:
        existing = await db.get(Invoice, invoice.intercompany_mirror_id)
        if existing is not None:
            return existing

    # Everyone implicated in the SOURCE payable is implicated in its mirror: the
    # mirror's vendor, amount and currency are the source's, copied verbatim, so
    # whoever shaped those terms shaped these. Both of the source's columns
    # travel together — its uploader AND its `segregation_actor_ids` — through
    # the same `implicated_actors` the approval gate refuses on, because
    # carrying one without the other would bar a source *editor* from the mirror
    # while leaving the source *uploader* free. Snapshotted now, like every
    # other implicated set (§152): the source's terms are what they are at
    # routing time. The routing actor lands in `uploaded_by_id` below and is
    # dropped from the set, so no one is named twice (docs/decisions.md §192).
    inherited = implicated_actors(invoice)
    if actor_id is not None:
        inherited.discard(str(actor_id))

    # Create the mirror payable under the counterparty entity. Amount stays an
    # exact Decimal (copied straight off the origin column). The invoice_number
    # is prefixed so the mirror is recognisable as the inter-company side.
    mirror = Invoice(
        organization_id=invoice.organization_id,
        entity_id=counterparty_id,
        # The routing actor CREATED this payable, so they are its uploader for
        # segregation-of-duties purposes — exactly as on the manual-create and
        # upload paths. Without it the mirror is a NULL-uploader row, and the
        # one person who caused a live liability to exist under another entity
        # could also sign it off.
        uploaded_by_id=actor_id,
        # NULL rather than `[]` when nobody is left, the shape every other
        # creation path writes for "nobody beyond the uploader".
        segregation_actor_ids=sorted(inherited) or None,
        invoice_number=f"IC-{invoice.invoice_number}",
        vendor_name=invoice.vendor_name,
        amount=invoice.amount,
        currency=invoice.currency,
        status=InvoiceStatus.new,
        # Point the mirror's counterparty back at the origin's entity.
        counterparty_entity_id=invoice.entity_id,
        intercompany_mirror_id=invoice.id,
    )
    db.add(mirror)
    await db.flush()

    # Link the origin back to the mirror.
    invoice.intercompany_mirror_id = mirror.id

    # The mirror enters the workflow exactly like any other new invoice.
    await create_workflow_instance(db, mirror)

    # Audit both sides. Details are PII-free (ids + entity ids only).
    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=actor_id,
        action="invoice.intercompany_routed",
        entity_type="invoice",
        entity_id=invoice.id,
        details={
            "role": "origin",
            "counterparty_entity_id": str(counterparty_id),
            "mirror_invoice_id": str(mirror.id),
        },
    )
    await dispatch_audit(
        db,
        correlation_id=mirror.correlation_id,
        organization_id=mirror.organization_id,
        actor_id=actor_id,
        action="invoice.intercompany_routed",
        entity_type="invoice",
        entity_id=mirror.id,
        details={
            "role": "mirror",
            "counterparty_entity_id": str(invoice.entity_id),
            "origin_invoice_id": str(invoice.id),
        },
    )
    await db.flush()
    return mirror
