"""``/api/v1/invoices`` — public read-only invoice surface.

Every route here is gated by ``require_api_scope("read")`` (which itself depends
on ``get_api_key_principal``) AND ``require_api_entitlement(FEATURE_PUBLIC_API)`` (the
public API is a paid-plan feature → 402 when the plan doesn't include it) AND
reads through ``get_api_key_db`` — so the tenant is resolved from the API key at
the data layer, never from a header the caller controls. Auth-before-everything
holds: there is no unauthenticated path into this router.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ApiKeyPrincipal,
    get_api_key_db,
    require_api_entitlement,
    require_api_scope,
)
from app.models.invoice import Invoice
from app.schemas.public_v1 import V1Invoice, V1InvoiceList
from app.services.billing.plan_catalog import FEATURE_PUBLIC_API

router = APIRouter(prefix="/v1", tags=["public-v1"])


@router.get("/invoices", response_model=V1InvoiceList)
async def list_invoices(
    db: AsyncSession = Depends(get_api_key_db),
    _principal: ApiKeyPrincipal = Depends(require_api_scope("read")),
    # Plan gate: the public API is a paid-plan feature. Composes WITH the scope
    # check above — scope is who-can, entitlement is does-your-plan-include.
    _entitled: ApiKeyPrincipal = Depends(require_api_entitlement(FEATURE_PUBLIC_API)),
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> V1InvoiceList:
    """List invoices for the API key's tenant, newest first.

    Optional ``status`` filter; paginated (max 200/page). The session is
    already tenant-scoped, so no extra tenant predicate is needed.
    """
    query = select(Invoice)
    if status_filter is not None:
        query = query.where(Invoice.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()

    rows = (
        (
            await db.execute(
                query.order_by(Invoice.created_at.desc(), Invoice.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    return V1InvoiceList(
        data=[V1Invoice.model_validate(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/invoices/{invoice_id}", response_model=V1Invoice)
async def get_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_api_key_db),
    _principal: ApiKeyPrincipal = Depends(require_api_scope("read")),
    _entitled: ApiKeyPrincipal = Depends(require_api_entitlement(FEATURE_PUBLIC_API)),
) -> V1Invoice:
    """Fetch a single invoice by id, scoped to the API key's tenant.

    A 404 is returned for both a missing invoice and one in another tenant
    (the latter is unreachable — the session is bound to this key's tenant DB —
    so a foreign id simply isn't found, never leaking its existence)."""
    invoice = (
        await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    ).scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return V1Invoice.model_validate(invoice)
