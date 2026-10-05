"""Vendor risk endpoints — composite risk score + org-wide distribution.

Mounted under `/api/vendors` alongside the main vendors router. Three
routes:

  * `GET  /{vendor_id}/risk`           — read the persisted composite.
  * `POST /{vendor_id}/risk/recompute` — recompute + persist (mutate).
  * `GET  /risk/summary`               — org-wide risk-level distribution.

The literal `/risk/summary` route is declared *before* the
`/{vendor_id}/risk` parameterised route so a request to it can never be
captured by the path param (vendor_id is typed `uuid.UUID`, which would
already 422 on "summary", but declaring literals first is the robust
pattern).

Every response uses the shared `app/schemas/sanctions.py` contract and
never exposes raw provider match detail — only list NAMES, scores, and
counts (invariant #7).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_roles,
)
from app.models.organization import Organization
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.sanctions import VendorRiskResponse, VendorRiskSummaryItem
from app.services.audit_dispatch import dispatch_audit
from app.services.vendor_risk_scoring import recompute_and_persist
from app.tenant import (
    ensure_in_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
)

router = APIRouter(prefix="/vendors", tags=["vendor-risk"])


def _to_response(vendor: Vendor) -> VendorRiskResponse:
    return VendorRiskResponse(
        vendor_id=str(vendor.id),
        risk_score=str(vendor.risk_score) if vendor.risk_score is not None else None,
        risk_level=vendor.risk_level,
        risk_factors=vendor.risk_factors,
        risk_scored_at=vendor.risk_scored_at.isoformat() if vendor.risk_scored_at else None,
    )


@router.get("/risk/summary", response_model=list[VendorRiskSummaryItem])
async def get_risk_summary(
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
):
    """Org-wide vendor-risk distribution — one bucket per `risk_level`
    with its count. Drives the screening dashboard's risk breakdown."""
    rows = (
        await db.execute(
            select(Vendor.risk_level, func.count())
            .group_by(Vendor.risk_level)
            .order_by(Vendor.risk_level)
        )
    ).all()
    return [
        VendorRiskSummaryItem(risk_level=level or "unknown", count=int(count))
        for level, count in rows
    ]


@router.get("/{vendor_id}/risk", response_model=VendorRiskResponse)
async def get_vendor_risk(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Return the vendor's persisted composite risk (reads denormalised
    columns; recompute is a separate POST)."""
    await ensure_in_entity_scope(
        db, Vendor, vendor_id, entity_id, detail="Vendor not found", include_shared=True
    )
    vendor = (await db.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return _to_response(vendor)


@router.post("/{vendor_id}/risk/recompute", response_model=VendorRiskResponse)
async def recompute_vendor_risk(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org_id: uuid.UUID = Depends(get_org_id),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Recompute the vendor's composite risk from current signals
    (latest sanctions check + open fraud flags + payment history) and
    persist it onto the vendor row. Returns the fresh assessment.

    `org.settings` is what resolves the org's REPORTING currency — the payment
    -history signal expresses its trailing volume in that currency and compares
    it against a threshold denominated in it, so without the settings the
    scorer would fall back to the platform default and judge a EUR book against
    a USD ramp."""
    await ensure_in_entity_scope(
        db, Vendor, vendor_id, entity_id, detail="Vendor not found", include_shared=True
    )
    vendor = (await db.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one_or_none()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    assessment = await recompute_and_persist(
        db,
        vendor=vendor,
        organization_id=org_id,
        org_settings=org.settings,
    )

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="vendor.risk_recomputed",
        entity_type="vendor",
        entity_id=vendor.id,
        details={
            "risk_level": assessment.risk_level,
            "risk_score": str(assessment.risk_score) if assessment.risk_score is not None else None,
        },
    )
    await db.commit()
    await db.refresh(vendor)
    return _to_response(vendor)
