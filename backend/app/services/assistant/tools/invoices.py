"""``list_invoices`` tool — a filtered, paginated, entity-scoped SELECT.

This re-builds the ``select(Invoice)`` filter shape directly rather than
importing from ``app/api/invoices.py`` (frozen this session — in-flight
multi-entity work). There is no business logic here, only a typed read. The
durable fix (a shared ``invoice_queries.py`` both the router and this tool call)
is a tracked follow-up — see ``docs/conversational-assistant.md`` § Deferred.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.services.assistant.tools.schemas import (
    InvoiceListResult,
    InvoiceSummary,
    ListInvoicesParams,
)
from app.tenant import apply_entity_scope
from app.utils.search import ilike_contains


async def list_invoices(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    current_user_id: uuid.UUID,
    params: ListInvoicesParams,
    control_db: AsyncSession | None = None,
) -> InvoiceListResult:
    stmt = select(Invoice)
    applied: dict = {}

    if params.status:
        values = [s.value for s in params.status]
        stmt = stmt.where(Invoice.status.in_(values))
        applied["status"] = values

    if params.vendor_name:
        # Resolve the name to vendor ids in-tool (never raw SQL); also match the
        # denormalised Invoice.vendor_name so manually-keyed invoices are found.
        vendor_ids = (
            (
                await db.execute(
                    apply_entity_scope(
                        select(Vendor.id).where(ilike_contains(Vendor.name, params.vendor_name)),
                        Vendor,
                        entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if vendor_ids:
            stmt = stmt.where(
                (Invoice.vendor_id.in_(vendor_ids))
                | ilike_contains(Invoice.vendor_name, params.vendor_name)
            )
        else:
            stmt = stmt.where(ilike_contains(Invoice.vendor_name, params.vendor_name))
        applied["vendor_name"] = params.vendor_name

    if params.date_from is not None:
        stmt = stmt.where(Invoice.invoice_date >= params.date_from)
        applied["date_from"] = params.date_from.isoformat()
    if params.date_to is not None:
        stmt = stmt.where(Invoice.invoice_date <= params.date_to)
        applied["date_to"] = params.date_to.isoformat()
    if params.amount_min is not None:
        stmt = stmt.where(Invoice.amount >= params.amount_min)
        applied["amount_min"] = str(params.amount_min)
    if params.amount_max is not None:
        stmt = stmt.where(Invoice.amount <= params.amount_max)
        applied["amount_max"] = str(params.amount_max)

    stmt = apply_entity_scope(stmt, Invoice, entity_id)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    rows = (
        (
            await db.execute(
                stmt.order_by(Invoice.invoice_date.desc().nullslast(), Invoice.created_at.desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )

    items = [
        InvoiceSummary(
            id=str(inv.id),
            invoice_number=inv.invoice_number,
            vendor_name=inv.vendor_name,
            amount=inv.amount,
            currency=inv.currency or None,
            status=inv.status.value if hasattr(inv.status, "value") else str(inv.status),
            invoice_date=inv.invoice_date,
            due_date=inv.due_date,
        )
        for inv in rows
    ]
    return InvoiceListResult(items=items, total=int(total), applied_filters=applied)
