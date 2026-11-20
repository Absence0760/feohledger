"""Pydantic v2 schemas for the adaptive-workflow endpoints.

Money / statistic Decimals serialise as **strings** (matching the project's
string-Decimal convention for adaptive stats and the JSON examples in
``backend/docs/adaptive-workflows.md``) — the API layer stringifies the
``Decimal`` values from the pure-stat service before constructing these models.
Nothing here is a wire ``float``.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Approval patterns
# ---------------------------------------------------------------------------


class ApproverPatternResponse(BaseModel):
    approver_id: str
    approver_name: str | None = None
    approved_count: int
    rejected_count: int
    approval_rate_pct: str
    median_time_to_approve_days: str
    avg_time_to_approve_days: str
    sample_size: int


class VendorPatternResponse(BaseModel):
    vendor_id: str | None = None
    vendor_name: str
    approved_count: int
    rejected_count: int
    approval_rate_pct: str
    unmodified_count: int
    consistency_pct: str
    avg_approved_amount: str
    median_approved_amount: str
    min_approved_amount: str
    max_approved_amount: str
    sample_size: int
    # Approvals whose amount could not be expressed in the org's reporting
    # currency. They are EXCLUDED from the four money fields above but INCLUDED
    # in ``sample_size`` -- so a reader handed the average beside the sample
    # count is reading an average over fewer rows than the count claims unless
    # this is disclosed. Emitted for exactly that disclosure (decisions.md
    # §79/§82); the aggregate itself is unchanged.
    unconverted_count: int = 0


class ApprovalPatternsResponse(BaseModel):
    generated_at: str
    lookback_days: int
    entity_id: str | None = None
    approvers: list[ApproverPatternResponse]
    vendors: list[VendorPatternResponse]


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


class VendorBaselineResponse(BaseModel):
    vendor_id: str | None = None
    vendor_name: str
    sample_size: int
    mean_amount: str
    median_amount: str
    stdev_amount: str
    min_amount: str
    max_amount: str
    typical_approver_ids: list[str]
    median_time_to_approve_days: str


class AnomalyFlagResponse(BaseModel):
    code: str
    severity: str
    message: str
    observed: str
    expected: str


class InvoiceAnomalyResponse(BaseModel):
    invoice_id: str
    vendor_id: str | None = None
    vendor_name: str
    amount: str
    # WHAT ``amount`` is denominated in. Normally the org's reporting currency
    # (the figure the baseline is comparable against); when the invoice carries
    # no usable rate lock, ``detect_invoice_anomaly`` falls back to the BILLED
    # figure for display and this carries the invoice's own currency instead.
    # Without it a client has no way to label the number and would stamp the
    # reporting currency onto a figure that is not in it.
    amount_currency: str = ""
    insufficient_history: bool
    baseline: VendorBaselineResponse | None = None
    flags: list[AnomalyFlagResponse]


class AnomalyBatchResponse(BaseModel):
    total_scanned: int
    flagged: list[InvoiceAnomalyResponse]


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------


class SuggestionResponse(BaseModel):
    id: uuid.UUID
    kind: str
    vendor_id: str | None = None
    vendor_name: str
    title: str
    rationale: str | None = None
    payload: dict
    confidence_pct: str
    status: str
    created_at: str | None = None
    dismissed_at: str | None = None
    # The caller's verdict on each row action — `{"dismiss"}`, a coded refusal
    # or `None` — from the endpoint's own gate (`api/adaptive_workflows.
    # dismiss_refusal`, issue #575). The page offers Dismiss by it.
    actions: dict | None = None


class SuggestionListResponse(BaseModel):
    suggestions: list[SuggestionResponse]


# ---------------------------------------------------------------------------
# Smart routing (advisory)
# ---------------------------------------------------------------------------


class RoutingCandidateResponse(BaseModel):
    approver_id: str
    approver_name: str | None = None
    score: str  # 0-100, string-Decimal (net of the outcome down-weight)
    base_score: str  # forward score BEFORE the outcome down-weight (explainability)
    outcome_penalty: str  # points subtracted for overturned decisions (>= 0)
    rank: int
    median_time_to_approve_days: str
    approval_rate_pct: str
    sample_size: int
    vendor_approved_count: int
    overturn_rate_pct: str  # share of THIS approver's decisions later overturned
    overturned_count: int  # # of this approver's decisions later overturned
    outcome_sample_size: int  # # of this approver's decisions the rate is over
    reasons: list[str]


class RoutingSuggestionResponse(BaseModel):
    invoice_id: str | None = None
    vendor_id: str | None = None
    vendor_name: str
    amount: str
    insufficient_history: bool
    candidates: list[RoutingCandidateResponse]


class ApplyRoutingRequest(BaseModel):
    """Body for the smart-routing apply path — assign the top-ranked eligible
    approver to one invoice via the audited assignment service."""

    invoice_id: uuid.UUID


class ApplyRoutingResponse(BaseModel):
    """Outcome of applying the routing recommendation.

    ``assigned`` is False on the idempotent no-op (the invoice was already
    assigned to the chosen approver — no second audit row is written)."""

    invoice_id: str
    assigned: bool
    assigned_to_id: str
    assigned_to_name: str | None = None
    rank: int  # the chosen candidate's rank (always 1 — the top recommendation)
    score: str  # the chosen candidate's routing score (string-Decimal)


# ---------------------------------------------------------------------------
# Auto-approve threshold recommendation + apply
# ---------------------------------------------------------------------------


class ThresholdEvidenceItem(BaseModel):
    vendor_id: str | None = None
    vendor_name: str
    based_on_n: int
    max_approved_amount: str  # string-Decimal
    median_approved_amount: str


class ThresholdRecommendationResponse(BaseModel):
    """Advisory recommendation to raise the org-wide ``auto_approve_below``
    threshold. Money fields are string-Decimal. ``should_raise`` /
    ``reason_code`` tell the UI whether an apply would do anything."""

    should_raise: bool
    current_threshold: str
    recommended_threshold: str
    cap_threshold: str
    qualifying_vendor_count: int
    total_clean_invoices: int
    reason_code: str  # "ok" | "insufficient_evidence" | "no_increase" | "at_cap"
    # ...plus "outcome_pullback" / "outcome_freeze" — every threshold surface now
    # returns the OUTCOME-ADJUSTED recommendation (see api/adaptive_workflows.py
    # ::_resolve_threshold_recommendation), so a raise held back by the feedback
    # loop reports why here rather than in a second, contradictory payload.
    rationale: str
    evidence: list[ThresholdEvidenceItem]
    # The active workflow definition the apply path would mutate (None when the
    # org has no active definition yet — apply 409s in that case).
    workflow_id: str | None = None
    lookback_days: int


class ApplyThresholdRequest(BaseModel):
    """Body for the threshold apply path.

    ``workflow_id`` pins which definition to update (defaults to the org's
    active definition when omitted). ``expected_recommended_threshold`` is an
    optional optimistic guard: when supplied it must equal the freshly-recomputed
    recommendation (string or number) or the apply 409s — so an admin can't apply
    a stale number the UI showed before the stats shifted underneath them."""

    workflow_id: uuid.UUID | None = None
    expected_recommended_threshold: str | None = None


class ApplyThresholdResponse(BaseModel):
    """Outcome of applying the threshold recommendation through the audited
    workflow-definition PATCH path.

    ``applied`` is False on the idempotent no-op (the recommendation does not
    raise the threshold — no version snapshot or audit row written)."""

    applied: bool
    workflow_id: str
    previous_threshold: str
    new_threshold: str
    reason_code: str
    rationale: str
    version_number: int | None = None  # the WorkflowVersion snapshot written on apply


# ---------------------------------------------------------------------------
# Feedback loop (outcome-adjusted)
# ---------------------------------------------------------------------------


class OutcomeStatsResponse(BaseModel):
    """Overturn tallies over the auto-approved invoice population — the cohort a
    raised auto-approve threshold creates."""

    auto_approved_count: int
    voided_count: int
    corrected_count: int
    rejected_count: int
    overturned_count: int
    overturn_rate_pct: str  # string-Decimal
    insufficient_data: bool


class EffectivenessMetricResponse(BaseModel):
    """One effectiveness figure. ``value_pct`` is null when ``insufficient_data``
    is true — the metric is honestly "not yet measurable", never a fabricated
    number."""

    name: str
    value_pct: str | None = None  # string-Decimal, or null when insufficient_data
    sample_size: int
    insufficient_data: bool
    label: str


class FeedbackResponse(BaseModel):
    """The feedback-loop read model under ``GET /api/adaptive/feedback``.

    ``base_recommendation`` is the forward (approval-history-only) threshold
    recommendation; ``adjusted_recommendation`` is the same recommendation after
    the realised auto-approval outcomes are folded in (it pulls back to a no-raise
    when overturns are climbing). Surfacing both makes the loop explainable —
    *why* a raise the history supported was held back."""

    lookback_days: int
    entity_id: str | None = None
    outcomes: OutcomeStatsResponse
    metrics: list[EffectivenessMetricResponse]
    base_recommendation: ThresholdRecommendationResponse
    adjusted_recommendation: ThresholdRecommendationResponse


__all__ = [
    "ApproverPatternResponse",
    "VendorPatternResponse",
    "ApprovalPatternsResponse",
    "VendorBaselineResponse",
    "AnomalyFlagResponse",
    "InvoiceAnomalyResponse",
    "AnomalyBatchResponse",
    "SuggestionResponse",
    "SuggestionListResponse",
    "RoutingCandidateResponse",
    "RoutingSuggestionResponse",
    "ApplyRoutingRequest",
    "ApplyRoutingResponse",
    "ThresholdEvidenceItem",
    "ThresholdRecommendationResponse",
    "ApplyThresholdRequest",
    "ApplyThresholdResponse",
    "OutcomeStatsResponse",
    "EffectivenessMetricResponse",
    "FeedbackResponse",
]
