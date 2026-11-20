"""Pydantic v2 schemas for the workflow A/B-testing (experiments) endpoints.

Statistic Decimals serialise as **strings** (matching the adaptive-workflow
convention) — the API layer stringifies the ``Decimal`` values from the pure
``services/workflow_experiments`` metrics before constructing these models.
Nothing here is a wire ``float``.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator


def _validate_variant_config(v: dict | None) -> dict | None:
    """A variant config must be a full ``steps_config`` (same shape the workflow
    definition stores) — ``{"steps": [ {step}, ... ]}``.

    It is frozen verbatim onto the invoice's ``steps_config_snapshot`` and read
    back through ``workflow_engine.get_step_config``, which keys off ``steps``.
    A config without a ``steps`` list (or with non-dict step entries) would make
    every step lookup return ``{}`` for assigned invoices — silently disabling
    auto-approve, the approval thresholds (max-amount / CFO gate), and
    segregation. Reject it at the boundary rather than freeze an unreadable
    snapshot.

    The same reasoning extends one level down, to each step's ``type``: a typo'd
    ``"aproval"`` is structurally fine but is not a type the engine recognises,
    so ``get_step_config(snapshot, "approval")`` returns ``{}`` and
    ``review._enforce_approval_thresholds`` returns early — dropping the
    max-amount cap, the CFO gate and the approver-strategy check for roughly half
    the tenant's invoices. That is exactly the failure ``decisions §32`` closed
    for ``POST /api/workflows/import``, so this runs the SAME gate
    (``workflow_builder.validate_builder_steps``) — which also checks the builder
    step-config shapes and ``condition`` goto targets.
    """
    if v is None:
        return v
    steps = v.get("steps")
    if not isinstance(steps, list):
        raise ValueError("variant config must contain a 'steps' list (a full steps_config)")
    for step in steps:
        if not isinstance(step, dict) or "type" not in step:
            raise ValueError("each step in a variant config must be an object with a 'type'")

    from app.services.workflow_builder import validate_builder_steps

    errors = validate_builder_steps(steps)
    if errors:
        raise ValueError("invalid variant config: " + "; ".join(errors))
    return v


class ExperimentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    workflow_definition_id: uuid.UUID
    config_a: dict
    config_b: dict
    split_a_pct: int = Field(default=50, ge=0, le=100)
    primary_metric: str = "time_to_approval_days"
    min_sample_per_variant: int = Field(default=10, ge=1, le=10000)

    @field_validator("config_a", "config_b")
    @classmethod
    def _check_config(cls, v: dict) -> dict:
        return _validate_variant_config(v)


class ExperimentUpdate(BaseModel):
    """Partial update — only allowed while the experiment is ``draft``."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    config_a: dict | None = None
    config_b: dict | None = None
    split_a_pct: int | None = Field(default=None, ge=0, le=100)
    primary_metric: str | None = None
    min_sample_per_variant: int | None = Field(default=None, ge=1, le=10000)

    @field_validator("config_a", "config_b")
    @classmethod
    def _check_config(cls, v: dict | None) -> dict | None:
        return _validate_variant_config(v)


class ExperimentResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    workflow_definition_id: str
    workflow_definition_name: str | None = None
    config_a: dict
    config_b: dict
    split_a_pct: int
    primary_metric: str
    min_sample_per_variant: int
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    assigned_count: int
    entity_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # The caller's verdict on each row action — `{"start"}`, a coded refusal or
    # `None` — from the endpoint's own gate (`api/workflow_experiments.
    # start_refusal`, issue #575). The page offers Start by it.
    actions: dict | None = None


class ExperimentListResponse(BaseModel):
    experiments: list[ExperimentResponse]


class VariantMetricsResponse(BaseModel):
    variant: str
    assigned_count: int
    completed_count: int
    approved_count: int
    rejected_count: int
    touchless_count: int
    exception_count: int
    median_time_to_approval_days: str
    avg_time_to_approval_days: str
    touchless_rate_pct: str
    exception_rate_pct: str
    rejection_rate_pct: str


class ExperimentResultsResponse(BaseModel):
    experiment_id: str
    experiment_name: str
    status: str
    primary_metric: str
    min_sample_per_variant: int
    enough_data: bool
    winner: str | None = None
    rationale: str
    notes: list[str]
    variant_a: VariantMetricsResponse
    variant_b: VariantMetricsResponse
    generated_at: str
