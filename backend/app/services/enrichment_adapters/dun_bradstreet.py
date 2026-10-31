"""Dun & Bradstreet (D&B) Direct+ adapter — skeleton.

D&B exposes a Direct+ REST API: OAuth2 client-credentials → a bearer token →
``GET /v1/match/cleanseMatch`` (resolve a name/address to a DUNS) and
``GET /v1/data/duns/{duns}`` (firmographics block). The match step maps the
vendor we know to a DUNS; the data step returns the registered legal name,
primary address, industry (SIC / NAICS), employee count, revenue, and founding
year.

API: https://directplus.documentation.dnb.com/

This ships as a working skeleton — the request shape and response parsing match
the published API, but the live credential lives in
``Organization.settings.enrichment.api_key`` (a Direct+ bearer token, or a
client-id/secret pair the operator pre-exchanges). Without it the adapter
**fails closed** by raising ``EnrichmentNotConfigured`` on the first call (same
pattern as the OXR FX + ComplyAdvantage sanctions adapters). NO hardcoded
fallback secret.
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

_BASE_URL = "https://plus.dnb.com"


@register_enrichment_adapter("dun_bradstreet")
class DunBradstreetAdapter:
    provider_name = "dun_bradstreet"

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        # A pre-exchanged Direct+ bearer token. No env fallback — the operator
        # sets it per-org via sops-backed settings. Fails closed when empty.
        self.api_key: str = cfg.get("api_key", "")
        self.base_url: str = cfg.get("base_url") or _BASE_URL
        self.timeout = float(cfg.get("timeout_seconds", 10.0))

    async def enrich_vendor(self, query: VendorEnrichmentQuery) -> VendorFirmographics:
        if not self.api_key:
            # Fail closed — never silently degrade to a fabricated record.
            raise EnrichmentNotConfigured(
                "dun_bradstreet adapter requires `api_key` in enrichment config"
            )

        headers = {"Authorization": f"Bearer {self.api_key}"}
        match_params = {"name": query.vendor_name}
        if query.vendor_country:
            match_params["countryISOAlpha2Code"] = query.vendor_country.upper()

        # SSRF guard: base_url is admin-overridable — refuse an internal host.
        # The client is pinned too, so httpx's own lookup cannot be rebound.
        from app.utils.url_safety import assert_public_url_async, pinned_async_client

        await assert_public_url_async(self.base_url)
        async with pinned_async_client(timeout=self.timeout, follow_redirects=False) as client:
            # 1. Resolve the vendor to a DUNS via cleanseMatch.
            match_resp = await client.get(
                f"{self.base_url}/v1/match/cleanseMatch",
                params=match_params,
                headers=headers,
            )
            match_resp.raise_for_status()
            candidates = (match_resp.json() or {}).get("matchCandidates") or []
            if not candidates:
                return VendorFirmographics(provider=self.provider_name, matched=False)

            top = candidates[0]
            org = top.get("organization") or {}
            duns = org.get("duns")
            confidence = top.get("matchQualityInformation", {}).get("confidenceCode")

            # 2. Pull the firmographics block for the resolved DUNS.
            if duns:
                data_resp = await client.get(
                    f"{self.base_url}/v1/data/duns/{duns}",
                    params={"blockIDs": "companyinfo_L2_v1"},
                    headers=headers,
                )
                data_resp.raise_for_status()
                org = (data_resp.json() or {}).get("organization") or org

        primary_addr = org.get("primaryAddress") or {}
        industry_codes = org.get("primaryIndustryCodes") or []
        sic = next((c.get("usSicV4") for c in industry_codes if c.get("usSicV4")), None)
        naics = next((c.get("code") for c in industry_codes if c.get("typeDnBCode") == 24664), None)
        emp = (org.get("numberOfEmployees") or [{}])[0].get("value")

        return VendorFirmographics(
            provider=self.provider_name,
            matched=True,
            legal_name=org.get("primaryName"),
            address=self._format_address(primary_addr),
            country=(primary_addr.get("addressCountry") or {}).get("isoAlpha2Code"),
            industry=next(
                (c.get("description") for c in industry_codes if c.get("description")), None
            ),
            sic_code=str(sic) if sic is not None else None,
            naics_code=str(naics) if naics is not None else None,
            employee_count=int(emp) if isinstance(emp, int | str) and str(emp).isdigit() else None,
            annual_revenue=self._yearly_revenue(org),
            website=(org.get("websiteAddress") or [{}])[0].get("url"),
            duns_number=str(duns) if duns else None,
            year_founded=org.get("startDate", {}).get("year"),
            tax_id_masked=mask_tax_id(query.vendor_tax_id),
            confidence=int(confidence) * 10 if isinstance(confidence, int) else None,
        )

    @staticmethod
    def _format_address(addr: dict) -> str | None:
        parts = [
            (addr.get("streetAddress") or {}).get("line1"),
            (addr.get("addressLocality") or {}).get("name"),
            (addr.get("addressRegion") or {}).get("abbreviatedName"),
            (addr.get("postalCode")),
        ]
        joined = ", ".join(p for p in parts if p)
        return joined or None

    @staticmethod
    def _yearly_revenue(org: dict) -> str | None:
        rev = (org.get("financials") or [{}])[0].get("yearlyRevenue") or []
        if not rev:
            return None
        amount = rev[0].get("value")
        # String — never a float on the wire (money invariant).
        return str(amount) if amount is not None else None

    async def test_connection(self) -> bool:
        if not self.api_key:
            return False
        try:
            await self.enrich_vendor(VendorEnrichmentQuery(vendor_name="connection_test_payload"))
        except Exception:  # noqa: BLE001
            return False
        return True
