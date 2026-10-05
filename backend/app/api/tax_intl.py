"""International-tax endpoints — VAT / GST / withholding / rules / report.

Worker-2 owns `/api/tax` (1099 tracking). This is the separate
international-tax surface mounted at `/api/international-tax`:

- GET  /rules                — list the country rules-engine rows (data-driven)
- GET  /rules/{country}       — one country's rules row
- GET  /rate/{country}        — resolve the rate via the pluggable adapter
- POST /vat                   — compute VAT (incl. EU reverse charge)
- POST /gst                   — compute GST (AU / IN / CA / ...)
- POST /withholding           — compute withholding tax
- GET  /report                — per-period VAT/GST/WHT report (tenant-scoped)

Every route is behind `require_roles` (auth before everything). The compute
routes are pure (no tenant DB needed) but still authenticated. The report
route resolves the tenant DB via `get_tenant_db` so isolation is enforced at
the data layer. The rate adapter is selected from `Organization.settings.tax`
(mock default — local-first, no cloud account needed).
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    require_roles,
)
from app.models.organization import Organization
from app.models.user import User
from app.schemas.international_tax import (
    CountryRuleResponse,
    CountryTaxLineResponse,
    CurrencyTaxTotalsResponse,
    GSTRequest,
    GSTResponse,
    TaxRateResponse,
    TaxReportResponse,
    VATRequest,
    VATResponse,
    WithholdingRequest,
    WithholdingResponse,
)
from app.services.international_tax.country_rules import (
    COUNTRY_RULES,
    UnknownCountry,
    get_country_rule,
)
from app.services.international_tax.gst import compute_gst
from app.services.international_tax.report import generate_tax_report
from app.services.international_tax.vat import compute_vat
from app.services.international_tax.withholding import compute_withholding
from app.services.tax_rate_adapters import (
    UnknownTaxRateProviderError,
    get_tax_rate_adapter,
    list_available_providers,
)
from app.tenant import get_tenant, get_tenant_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/international-tax", tags=["tax"])

# Tax configuration + reads are AP-team work; the report is also CFO-facing.
_READ_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_AP_CLERK, ROLE_CFO)
_REPORT_ROLES = (ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)


def _rule_to_response(rule) -> CountryRuleResponse:
    return CountryRuleResponse(
        country_code=rule.country_code,
        country_name=rule.country_name,
        regime=rule.regime,
        currency=rule.currency,
        standard_rate=rule.standard_rate,
        rate_categories=dict(rule.rate_categories),
        is_eu=rule.is_eu,
        reverse_charge_supported=rule.reverse_charge_supported,
        registration_label=rule.registration_label,
        withholding=[
            {"category": w.category, "rate": w.rate, "default": w.default} for w in rule.withholding
        ],
    )


# ---------------------------------------------------------------------------
# Rules engine (data-driven country rules)
# ---------------------------------------------------------------------------
@router.get("/rules", response_model=list[CountryRuleResponse])
async def list_country_rules(
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    return [_rule_to_response(r) for _, r in sorted(COUNTRY_RULES.items())]


@router.get("/rules/{country}", response_model=CountryRuleResponse)
async def get_country_rules(
    country: str = Path(..., min_length=2, max_length=2),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    try:
        rule = get_country_rule(country)
    except UnknownCountry as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _rule_to_response(rule)


# ---------------------------------------------------------------------------
# Rate lookup (pluggable adapter)
# ---------------------------------------------------------------------------


def _require_rate_adapter(org: Organization):
    """Resolve the org's tax-rate provider or 409 naming the bad value.

    ``get_tax_rate_adapter`` refuses a NAMED provider it has no adapter for
    rather than resolving to ``mock`` (`decisions.md` §29). The mock answers
    every country from the in-repo country-rules table — a plausible fixture,
    not a maintained rate feed — so a typo'd provider name computed VAT / GST
    off a hardcoded rate while the response's ``provider`` field named the
    provider that was asked for. These three routes are pure compute and persist
    nothing, so the refusal costs no unwinding; what it buys is that a
    jurisdiction figure is never quoted from a source nobody chose.

    409, matching the card / 1099 dispatchers: the request is well-formed; the
    org's tax configuration is in a state that cannot service it. The provider
    name is admin-supplied config, not PII, and the exception bounds it.
    """
    tax_config = (org.settings or {}).get("tax") if org.settings else None
    try:
        return get_tax_rate_adapter(tax_config)
    except UnknownTaxRateProviderError as exc:
        logger.warning(
            "[intl-tax] rate provider %r has no registered adapter for org %s",
            exc.provider,
            org.id,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{exc.provider}' is not a supported tax-rate provider "
                f"(one of: {', '.join(list_available_providers())}). "
                "Fix it in Organization Settings and retry."
            ),
        ) from None


@router.get("/rate/{country}", response_model=TaxRateResponse)
async def lookup_tax_rate(
    country: str = Path(..., min_length=2, max_length=2),
    region: str | None = Query(None),
    rate_category: str | None = Query(None),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    adapter = _require_rate_adapter(org)
    try:
        result = await adapter.get_rate(country, region=region, rate_category=rate_category)
    except UnknownCountry as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, NotImplementedError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TaxRateResponse(
        country_code=result.country_code,
        region=result.region,
        rate=result.rate,
        regime=result.regime,
        rate_category=result.rate_category,
        provider=result.provider,
    )


# ---------------------------------------------------------------------------
# VAT
# ---------------------------------------------------------------------------
@router.post("/vat", response_model=VATResponse)
async def compute_vat_endpoint(
    body: VATRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    adapter = _require_rate_adapter(org)
    try:
        rate_result = await adapter.get_rate(
            body.supplier_country, rate_category=body.rate_category
        )
        buyer_rate = None
        if body.buyer_country:
            # Reverse charge (when it applies) is self-accounted at the
            # BUYER's own domestic rate, not the supplier's — resolve it too.
            buyer_rate_result = await adapter.get_rate(
                body.buyer_country, rate_category=body.rate_category
            )
            buyer_rate = buyer_rate_result.rate
        result = compute_vat(
            net_amount=body.net_amount,
            rate=rate_result.rate,
            supplier_country=body.supplier_country,
            buyer_country=body.buyer_country,
            buyer_vat_registered=body.buyer_vat_registered,
            buyer_rate=buyer_rate,
        )
    except UnknownCountry as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, NotImplementedError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return VATResponse(**result.__dict__)


# ---------------------------------------------------------------------------
# GST
# ---------------------------------------------------------------------------
@router.post("/gst", response_model=GSTResponse)
async def compute_gst_endpoint(
    body: GSTRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    adapter = _require_rate_adapter(org)
    try:
        rate_result = await adapter.get_rate(body.country, rate_category=body.rate_category)
        result = compute_gst(
            net_amount=body.net_amount,
            rate=rate_result.rate,
            country=body.country,
            interstate=body.interstate,
            province_rate=body.province_rate,
        )
    except UnknownCountry as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, NotImplementedError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return GSTResponse(
        country_code=result.country_code,
        currency=result.currency,
        net_amount=result.net_amount,
        gst_rate=result.gst_rate,
        gst_amount=result.gst_amount,
        gross_amount=result.gross_amount,
        components=result.components,
        notes=result.notes,
    )


# ---------------------------------------------------------------------------
# Withholding
# ---------------------------------------------------------------------------
@router.post("/withholding", response_model=WithholdingResponse)
async def compute_withholding_endpoint(
    body: WithholdingRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(*_READ_ROLES)),
):
    try:
        result = compute_withholding(
            gross_amount=body.gross_amount,
            supplier_country=body.supplier_country,
            category=body.category,
            treaty_rate=body.treaty_rate,
        )
    except UnknownCountry as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WithholdingResponse(**result.__dict__)


# ---------------------------------------------------------------------------
# Per-period tax report (tenant-scoped)
# ---------------------------------------------------------------------------
@router.get("/report", response_model=TaxReportResponse)
async def tax_report(
    period_start: date = Query(...),
    period_end: date = Query(...),
    country: str | None = Query(None, min_length=2, max_length=2),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(*_REPORT_ROLES)),
):
    if period_end < period_start:
        raise HTTPException(status_code=400, detail="period_end must be >= period_start")
    report = await generate_tax_report(
        db,
        period_start=period_start,
        period_end=period_end,
        country_code=country,
    )
    return TaxReportResponse(
        period_start=report.period_start,
        period_end=report.period_end,
        countries=[
            CountryTaxLineResponse(
                country_code=line.country_code,
                currency=line.currency,
                vat_output=line.vat_output,
                vat_reverse_charge=line.vat_reverse_charge,
                gst_total=line.gst_total,
                gst_components=line.gst_components,
                withholding_total=line.withholding_total,
                record_count=line.record_count,
            )
            for line in report.countries
        ],
        totals_by_currency=[
            CurrencyTaxTotalsResponse(
                currency=t.currency,
                vat_output=t.vat_output,
                vat_reverse_charge=t.vat_reverse_charge,
                gst_total=t.gst_total,
                withholding_total=t.withholding_total,
                record_count=t.record_count,
            )
            for t in report.totals_by_currency
        ],
        currency=report.currency,
        total_vat_output=report.total_vat_output,
        total_vat_reverse_charge=report.total_vat_reverse_charge,
        total_gst=report.total_gst,
        total_withholding=report.total_withholding,
        record_count=report.record_count,
    )
