"""Clearbit Enrichment adapter — skeleton.

Clearbit's Company API resolves a company by its primary **domain**:
``GET https://company.clearbit.com/v2/companies/find?domain=<domain>`` with
``Authorization: Bearer <key>``. It returns name, legal name, category
(industry + SIC + NAICS), employee count, estimated annual revenue, founding
year, and the canonical site URL.

API: https://dashboard.clearbit.com/docs#enrichment-api-company-api

This ships as a working skeleton — request shape + response parsing match the
published API, but the live key lives in
``Organization.settings.enrichment.api_key``. Without it the adapter **fails
closed** by raising ``EnrichmentNotConfigured`` on the first call (no hardcoded
fallback secret). Clearbit keys off a domain, so a vendor with no
``domain``/email-derived domain and no website is a no-match rather than an error.
"""

from __future__ import annotations

import logging

from app.services.enrichment_adapters.base import (
    EnrichmentNotConfigured,
    VendorEnrichmentQuery,
    VendorFirmographics,
)
from app.services.enrichment_adapters.dispatcher import register_enrichment_adapter
from app.services.vendor_consolidation import mask_tax_id

logger = logging.getLogger(__name__)

_BASE_URL = "https://company.clearbit.com"


@register_enrichment_adapter("clearbit")
class ClearbitAdapter:
    provider_name = "clearbit"

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.api_key: str = cfg.get("api_key", "")
        self.base_url: str = cfg.get("base_url") or _BASE_URL
        self.timeout = float(cfg.get("timeout_seconds", 10.0))

    async def enrich_vendor(self, query: VendorEnrichmentQuery) -> VendorFirmographics:
        if not self.api_key:
            raise EnrichmentNotConfigured(
                "clearbit adapter requires `api_key` in enrichment config"
            )

        # Clearbit keys off a domain. No domain → nothing to look up (no-match,
        # not an error — the steward can add a website and retry).
        if not query.domain:
            return VendorFirmographics(provider=self.provider_name, matched=False)

        # SSRF guard: base_url is a per-org override (settings.enrichment), so
        # `org_host_client` refuses an internal host up front and pins the
        # connect (decisions §339). No dev loopback allowance: no local Clearbit.
        from app.utils.url_safety import org_host_client

        client = await org_host_client(
            self.base_url, allow_private=False, timeout=self.timeout, follow_redirects=False
        )
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with client:
            resp = await client.get(
                f"{self.base_url}/v2/companies/find",
                params={"domain": query.domain},
                headers=headers,
            )
            # 404 = no company on that domain → clean no-match.
            if resp.status_code == 404:
                return VendorFirmographics(provider=self.provider_name, matched=False)
            resp.raise_for_status()
            data = resp.json() or {}

        category = data.get("category") or {}
        metrics = data.get("metrics") or {}
        geo = data.get("geo") or {}
        revenue = metrics.get("estimatedAnnualRevenue") or metrics.get("annualRevenue")

        return VendorFirmographics(
            provider=self.provider_name,
            matched=bool(data.get("name") or data.get("legalName")),
            legal_name=data.get("legalName") or data.get("name"),
            address=self._format_address(geo),
            country=geo.get("countryCode"),
            industry=category.get("industry"),
            sic_code=category.get("sicCode"),
            naics_code=category.get("naicsCode"),
            employee_count=metrics.get("employees"),
            # String, never a float — Clearbit returns a free-text bucket here.
            annual_revenue=str(revenue) if revenue is not None else None,
            website=f"https://{data['domain']}" if data.get("domain") else None,
            year_founded=data.get("foundedYear"),
            tax_id_masked=mask_tax_id(query.vendor_tax_id),
        )

    @staticmethod
    def _format_address(geo: dict) -> str | None:
        parts = [
            geo.get("streetNumber"),
            geo.get("streetName"),
            geo.get("city"),
            geo.get("state"),
            geo.get("postalCode"),
        ]
        joined = " ".join(str(p) for p in parts if p)
        return joined or None

    async def test_connection(self) -> bool:
        if not self.api_key:
            return False
        try:
            await self.enrich_vendor(
                VendorEnrichmentQuery(vendor_name="connection_test", domain="clearbit.com")
            )
        except Exception:  # noqa: BLE001
            return False
        return True
