"""Response schemas for sanctions screening + vendor risk.

Single-authored shared contract so the screening endpoints (api/vendors.py)
and the risk endpoints (api/vendor_risk.py) agree on shapes. None of these
expose raw provider match details — only the list NAME, the verdict, scores,
the PII-free category taxonomy, and timestamps (invariant #7).
"""

from __future__ import annotations

from pydantic import BaseModel

from app.services.sanctions_categories import (
    categories_from_raw_response,
    has_adverse_media,
)


class SanctionsCheckResponse(BaseModel):
    """One row of a vendor's screening trail (`sanctions_checks`).

    `categories` is the fixed-vocabulary taxonomy of what was hit — `sanctions`
    / `pep` / `adverse_media` / `high_risk_country`. It is safe to return
    because the labels are ours, not the provider's free text; the raw payload
    they were derived from stays confined to the JSONB column and is never
    serialized here.
    """

    id: str
    vendor_id: str
    provider: str
    check_type: str  # initial | periodic | manual | pre_payment | bank_change | tax_id_change
    result: str  # clear | match | review_required
    risk_score: str | None = None
    matched_list: str | None = None
    categories: list[str] = []
    adverse_media: bool = False
    checked_at: str

    @classmethod
    def from_db(cls, c) -> SanctionsCheckResponse:
        categories = categories_from_raw_response(c.raw_response)
        return cls(
            id=str(c.id),
            vendor_id=str(c.vendor_id),
            provider=c.provider,
            check_type=c.check_type,
            result=c.result,
            risk_score=str(c.risk_score) if c.risk_score is not None else None,
            matched_list=c.matched_list,
            categories=list(categories),
            adverse_media=has_adverse_media(categories),
            checked_at=c.checked_at.isoformat() if c.checked_at else "",
        )


class ScreeningReviewItem(BaseModel):
    """A vendor needing screening attention (match / review_required), for the
    review-queue surface."""

    vendor_id: str
    vendor_name: str
    screening_status: str
    last_screened_at: str | None = None
    payments_blocked: bool = False
    risk_level: str = "unknown"
    risk_score: str | None = None
    latest_matched_list: str | None = None
    latest_provider: str | None = None
    # PII-free taxonomy from the same latest row — lets the queue distinguish a
    # negative-news hit from a watchlist match at a glance, which is a different
    # instruction to the reviewer.
    latest_categories: list[str] = []
    adverse_media: bool = False


class VendorBlockRequest(BaseModel):
    """Manual block / unblock payload."""

    reason: str | None = None


class VendorRiskResponse(BaseModel):
    """Composite risk for one vendor."""

    vendor_id: str
    risk_score: str | None = None
    risk_level: str = "unknown"
    risk_factors: dict | None = None
    risk_scored_at: str | None = None


class VendorRiskSummaryItem(BaseModel):
    """One bucket of the org-wide risk distribution."""

    risk_level: str
    count: int
