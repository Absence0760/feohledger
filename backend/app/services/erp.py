"""ERP integration service — push approved invoices to an external ERP system."""

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceLineItem, InvoiceStatus
from app.services.erp_adapters import (
    InvoicePayload,
    LineItemPayload,
    get_erp_adapter,
)
from app.services.workflow_engine import (
    complete_workflow,
    get_workflow_instance,
    transition_invoice,
)

MAX_RETRIES = 3
BASE_DELAY_SECONDS = 2


def _build_payload(invoice: Invoice, line_items: list[InvoiceLineItem]) -> InvoicePayload:
    """Convert an Invoice ORM object (+ its line items) to a normalized ERP payload."""
    return InvoicePayload(
        correlation_id=str(invoice.correlation_id),
        invoice_number=invoice.invoice_number,
        vendor_name=invoice.vendor_name,
        amount=invoice.amount,
        currency=invoice.currency or "USD",
        vendor_tax_id=invoice.vendor_tax_id,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        po_number=invoice.po_number,
        description=invoice.description,
        subtotal=invoice.subtotal,
        tax_amount=invoice.tax_amount,
        tax_rate=invoice.tax_rate,
        discount_amount=invoice.discount_amount,
        shipping_amount=invoice.shipping_amount,
        gl_account=invoice.gl_account,
        cost_center=invoice.cost_center,
        payment_terms=invoice.payment_terms,
        payment_method=invoice.payment_method,
        bill_to_address=invoice.bill_to_address,
        remit_to_address=invoice.remit_to_address,
        vendor_address=invoice.vendor_address,
        line_items=[
            LineItemPayload(
                # A hand-keyed / legacy row can have a NULL line_number; fall
                # back to its position in the (stable) query order rather than
                # drop it from the ERP payload.
                line_number=li.line_number if li.line_number is not None else idx + 1,
                item_code=li.item_code,
                description=li.description,
                quantity=li.quantity,
                unit_price=li.unit_price,
                tax=li.tax,
                total=li.total,
                gl_account=li.gl_account,
            )
            for idx, li in enumerate(line_items)
        ],
    )


async def _fetch_line_items(db: AsyncSession, invoice_id: uuid.UUID) -> list[InvoiceLineItem]:
    """Load an invoice's line items in stable order.

    A plain query rather than the `Invoice.line_items` relationship: the
    invoice object reaching `_call_erp` may come from a session/loop the
    relationship was never eagerly loaded on (e.g. the `erp_dispatch`
    background task's own session), and a lazy load there would raise
    `MissingGreenlet` instead of silently working.
    """
    result = await db.execute(
        select(InvoiceLineItem)
        .where(InvoiceLineItem.invoice_id == invoice_id)
        .order_by(InvoiceLineItem.line_number.asc().nulls_last(), InvoiceLineItem.id.asc())
    )
    return list(result.scalars().all())


# `send_to_erp` used to sit here: it transitioned `approved → sending_to_erp`
# and then ran the retry loop that now lives in `send_to_erp_internal`. Nothing
# in production reached it — `api/workflow.py` transitions the invoice itself
# and then dispatches, so `erp_dispatch` / `erp_lambda` both call the
# `_internal` variant. Two copies of one push path, and they had already
# diverged on the thing that matters: the reachable one had no retry at all, so
# a transient ERP 503 failed the invoice on the first attempt while this copy's
# 3-attempt backoff was what the tests asserted and `retry_erp`'s counter reset
# implied. The retry moved to the live function and this copy was deleted, so
# there is no longer a second push path to drift against.


async def retry_erp(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Prepare a failed ERP push for retry. Only valid if the invoice was
    previously approved.

    Resets the retry counter and transitions to sending_to_erp; the actual
    ERP call is the caller's job via `dispatch_erp` (which resolves the
    org's settings.erp and honours FEOH_ERP_MODE). Running it inline here
    would double-post — the route already dispatches after this returns —
    and would bypass both the org's adapter config and the lambda mode.
    """
    if not invoice.approved_by:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409,
            detail="Cannot retry ERP push — invoice was never approved",
        )

    # Reset retry count
    instance = await get_workflow_instance(db, invoice.id)
    if instance:
        state_data = instance.state_data or {}
        state_data["erp_retries"] = 0
        instance.state_data = state_data
        instance.state = "active"

    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.sending_to_erp,
        actor_id=actor_id,
        action_name="invoice.erp_retried",
    )
    await db.commit()


async def send_to_erp_internal(
    db: AsyncSession,
    invoice: Invoice,
    *,
    actor_id: uuid.UUID | None = None,
    erp_config: dict | None = None,
) -> None:
    """Run the ERP call, with bounded retries (invoice is already in
    `sending_to_erp`).

    This is the ONLY path that pushes an invoice to an ERP —
    `api/workflow.py` → `dispatch_erp` → `erp_dispatch` / `erp_lambda` all land
    here. It used to be a single `try/except` that sent a transient 503 or
    timeout straight to `InvoiceStatus.failed`, while a second, unreachable
    copy of this function carried the 3-attempt exponential backoff that the
    tests, `retry_erp`'s counter reset, and the docs all describe. The retry
    semantics were tested but not shipped, and the divergence was invisible
    precisely because nothing in production reached the copy that had them.

    Retrying is safe to do here rather than only by hand: `_call_erp` sends the
    invoice's `correlation_id` as the adapter's idempotency key, so a retry
    after a timeout that the ERP actually applied returns the existing
    document rather than posting a second one.

    The transaction is closed before each sleep, so the wait does not hold a
    pooled connection open — `erp_dispatch` builds a `pool_size=1` engine per
    send, and sleeping with the connection checked out would pin a worker slot
    for the whole backoff instead of just the call.
    """
    instance = await get_workflow_instance(db, invoice.id)
    state_data = (instance.state_data if instance else None) or {}
    # Resume from whatever the last attempt recorded, so a manual
    # `POST /{id}/retry-erp` (which resets the counter to 0) gets a full budget
    # while an in-flight sequence is not restarted from scratch.
    retry_count = state_data.get("erp_retries", 0)

    for attempt in range(retry_count, MAX_RETRIES):
        try:
            erp_ref = await _call_erp(db, invoice, erp_config)

            await transition_invoice(
                db,
                invoice,
                InvoiceStatus.sent_to_erp,
                actor_id=actor_id,
                action_name="invoice.erp_confirmed",
                details={"erp_reference": erp_ref},
            )
            await transition_invoice(
                db,
                invoice,
                InvoiceStatus.done,
                actor_id=actor_id,
                action_name="invoice.completed",
            )

            if instance:
                instance.state_data = {
                    **(instance.state_data or {}),
                    "erp_reference": erp_ref,
                    "erp_retries": attempt + 1,
                }
                await complete_workflow(db, instance, action="erp_confirmed")

            await db.commit()
            return

        except Exception as exc:
            if attempt + 1 < MAX_RETRIES:
                if instance:
                    instance.state_data = {
                        **(instance.state_data or {}),
                        "erp_retries": attempt + 1,
                        "last_error": str(exc),
                    }
                # Commit unconditionally, even with no instance to write: it is
                # what returns this session's connection to the pool for the
                # duration of the backoff.
                await db.commit()
                await asyncio.sleep(BASE_DELAY_SECONDS * (2**attempt))
            else:
                await transition_invoice(
                    db,
                    invoice,
                    InvoiceStatus.failed,
                    actor_id=actor_id,
                    action_name="invoice.erp_failed",
                    details={"error": str(exc), "retries": attempt + 1},
                )
                if instance:
                    instance.state = "failed"
                    instance.state_data = {
                        **(instance.state_data or {}),
                        "erp_retries": attempt + 1,
                        "last_error": str(exc),
                    }
                await db.commit()
                return


async def _call_erp(db: AsyncSession, invoice: Invoice, erp_config: dict | None = None) -> str:
    """Send invoice to the configured ERP via the adapter pattern.

    Uses the invoice's correlation_id as an idempotency key.
    Returns an ERP reference ID on success, raises on failure.
    """
    config = erp_config or {"type": "mock", "integration_method": "direct"}

    adapter = get_erp_adapter(config)
    line_items = await _fetch_line_items(db, invoice.id)
    payload = _build_payload(invoice, line_items)
    result = await adapter.post_invoice(payload)

    if not result.success:
        raise RuntimeError(result.message or "ERP post failed")

    return result.erp_document_id or result.erp_document_number or "UNKNOWN"
