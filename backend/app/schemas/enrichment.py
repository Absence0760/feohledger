"""Pydantic v2 schemas for the data-enrichment endpoints.

Money / statistic Decimals serialise as **strings** (matching the project's
string-Decimal convention — never a wire ``float``). The API layer stringifies
the ``Decimal`` values from the pure-stat service before constructing these
models. N/A scores are typed ``str | None``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.vendor import VendorCardRevocationResponse, VendorResponse

# ---------------------------------------------------------------------------
# Suggestions (auto-fill + price variance)
# ---------------------------------------------------------------------------


class FieldSuggestionOut(BaseModel):
    field: str
    value: str
    confidence: str
    sample_size: int
    occurrences: int
    evidence: str
    runner_up: str | None = None


class PriceVarianceOut(BaseModel):
    line_index: int
    item_key: str
    description: str | None = None
    current_unit_price: str
    baseline_unit_price: str
    delta: str
    delta_pct: str
    sample_size: int
    direction: str
    severity: str


class EnrichmentSuggestionsResponse(BaseModel):
    invoice_id: str
    vendor_id: str | None = None
    field_suggestions: list[FieldSuggestionOut]
    price_variances: list[PriceVarianceOut]
    generated_at: str


# ---------------------------------------------------------------------------
# Vendor score
# ---------------------------------------------------------------------------


class SubScoreOut(BaseModel):
    name: str
    score: str | None = None
    sample_size: int
    detail: str


class VendorScoreResponse(BaseModel):
    vendor_id: str
    vendor_name: str
    composite: str | None = None
    sub_scores: list[SubScoreOut]
    computed_at: str


# ---------------------------------------------------------------------------
# Vendor consolidation (duplicate / similar vendor clusters)
# ---------------------------------------------------------------------------


class VendorClusterMemberOut(BaseModel):
    vendor_id: str
    name: str
    code: str | None = None
    tax_id_masked: str | None = None  # ***6789 — never the full tax id
    status: str | None = None
    invoice_count: int
    is_canonical: bool


class VendorClusterOut(BaseModel):
    cluster_id: int
    members: list[VendorClusterMemberOut]
    canonical_vendor_id: str
    score: str  # 0..1 strongest pairwise evidence, string-Decimal
    reasons: list[str]


class VendorConsolidationResponse(BaseModel):
    clusters: list[VendorClusterOut]
    vendor_count: int
    cluster_count: int
    truncated: bool  # tenant exceeded the bound, or clusters were capped
    generated_at: str


# ---------------------------------------------------------------------------
# Vendor consolidation — execute (merge duplicates into one canonical vendor)
# ---------------------------------------------------------------------------


class VendorMergeRequest(BaseModel):
    """The steward's explicit merge: fold ``duplicate_vendor_ids`` into
    ``canonical_vendor_id``. The advisory ``consolidation-suggestions`` endpoint
    proposes the cluster + canonical pick; this is the deliberate execute."""

    canonical_vendor_id: str
    duplicate_vendor_ids: list[str] = Field(default_factory=list)


class VendorMergeResponse(BaseModel):
    canonical_vendor_id: str
    duplicate_vendor_ids: list[str]
    # Per-table reassigned row counts (PII-free — table name → rows moved).
    reassigned: dict[str, int]
    total_reassigned: int
    # Duplicate ids THIS call flipped active → inactive (empty on an idempotent
    # re-run where they were already retired).
    deactivated_vendor_ids: list[str]
    merged_at: str
    # Live virtual cards the merge left on an un-payable vendor, and what
    # happened to them (`services/vendor_card_revocation.py`). Empty when every
    # involved vendor is still payable or holds no live card.
    card_revocations: list[VendorCardRevocationResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# External vendor enrichment (firmographics from D&B / Clearbit / ...)
# ---------------------------------------------------------------------------


class VendorFirmographicsOut(BaseModel):
    """Normalised firmographics returned by an external enrichment provider.

    Advisory / suggestion-only — the API surfaces this for a steward to review;
    the enrichment path NEVER writes it back onto the ``Vendor`` row. ``annual_revenue``
    is a string (never a wire float). No raw ``tax_id`` ever appears — only the
    masked ``tax_id_masked`` (``***<last4>``)."""

    provider: str
    matched: bool
    legal_name: str | None = None
    address: str | None = None
    country: str | None = None
    industry: str | None = None
    sic_code: str | None = None
    naics_code: str | None = None
    employee_count: int | None = None
    annual_revenue: str | None = None
    website: str | None = None
    duns_number: str | None = None
    year_founded: int | None = None
    tax_id_masked: str | None = None
    confidence: int | None = None
    extra: dict = {}


class EnrichmentFieldSuggestionOut(BaseModel):
    """A single advisory change a steward may choose to apply to the vendor."""

    field: str  # vendor column the value maps to (address, website, ...)
    current_value: str | None = None
    suggested_value: str | None = None


class VendorEnrichmentResponse(BaseModel):
    vendor_id: str
    vendor_name: str
    firmographics: VendorFirmographicsOut
    # Per-field advisory suggestions: where the provider's value differs from
    # what we hold today, the steward can choose to apply it. Never auto-applied.
    suggestions: list[EnrichmentFieldSuggestionOut]
    generated_at: str


# ---------------------------------------------------------------------------
# Apply an enrichment suggestion onto the vendor (audited write)
# ---------------------------------------------------------------------------


class EnrichmentApplyField(BaseModel):
    """One field the steward has explicitly accepted from the enrich diff.

    ``field`` must be one of the applyable vendor columns (``name`` / ``address``
    / ``website``); ``tax_id`` is intentionally NOT applyable here — a tax-id
    change is a fraud surface and must go through the bank/tax change-request
    gate, never an enrichment auto-apply."""

    field: str
    value: str | None = Field(default=None, max_length=500)


class VendorEnrichmentApplyRequest(BaseModel):
    """The steward's selection of enrichment fields to write onto the vendor.

    Never auto-derived — the caller lists exactly which fields to apply, so the
    apply is non-destructive (only the named fields change)."""

    fields: list[EnrichmentApplyField] = Field(default_factory=list)


class VendorEnrichmentApplyResponse(BaseModel):
    vendor_id: str
    # Field-level before/after diff actually written (PII-free — applyable
    # fields are non-sensitive). Empty when the apply was a no-op (idempotent).
    applied: dict[str, dict[str, str | None]]
    vendor: VendorResponse
    applied_at: str


__all__ = [
    "FieldSuggestionOut",
    "PriceVarianceOut",
    "EnrichmentSuggestionsResponse",
    "SubScoreOut",
    "VendorScoreResponse",
    "VendorClusterMemberOut",
    "VendorClusterOut",
    "VendorConsolidationResponse",
    "VendorMergeRequest",
    "VendorMergeResponse",
    "VendorFirmographicsOut",
    "VendorEnrichmentResponse",
    "EnrichmentFieldSuggestionOut",
    "EnrichmentApplyField",
    "VendorEnrichmentApplyRequest",
    "VendorEnrichmentApplyResponse",
]
