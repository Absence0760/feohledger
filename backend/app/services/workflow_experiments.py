"""A/B testing for workflow rules — deterministic assignment + metrics.

The "act" surface that compares the performance of two workflow-rule
configurations (an **A** control vs a **B** variant) on objective, deterministic
metrics. Like ``services/adaptive_workflows.py`` everything here is **pure** (no
async, no IO, no LLM, no cloud key): the caller (``app/api/workflow_experiments``)
pulls rows from the tenant DB and hands them in already shaped, so both the
assignment rule and the metrics math are unit-testable without a database and —
critically — **deterministic**, mirroring the local-first invariant.

Two concerns, one module:

  1. **Assignment** (``assign_variant``) — a stable, deterministic A/B split keyed
     on the *invoice id* + the experiment id (so the same invoice always lands in
     the same variant, and two experiments split independently). The split ratio
     is honoured by hashing into a [0, 100) bucket and comparing against the
     experiment's ``split_a_pct``. No randomness, no clock — the assignment is
     reproducible and auditable, and is recorded on the invoice's workflow
     instance so it survives recomputation.

  2. **Metrics** (``compute_experiment_results``) — per-variant aggregates over the
     **assigned, completed** invoices: time-to-approval, touchless rate (the ONE
     definition in ``services/touchless``, shared with the dashboard KPI),
     exception rate, rejection rate. A clear
     "not enough data yet" state guards every readout, and a winner is only
     called once both arms clear a minimum sample. The winner call is a simple,
     explainable, deterministic comparison on the configured primary metric — no
     statistical-significance test is claimed (that would over-promise on the
     small samples a single tenant produces; the rationale says so).

All amount/rate math is ``Decimal``; this module never moves money — it only
routes (which config an invoice runs under) and measures.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.services.analytics import _avg, _quantile, compute_touchless_rate

__all__ = [
    "VARIANT_A",
    "VARIANT_B",
    "VariantMetrics",
    "ExperimentResults",
    "assign_variant",
    "compute_experiment_results",
]

VARIANT_A = "A"
VARIANT_B = "B"

_CENTS = Decimal("0.01")
_TENTH = Decimal("0.1")

# Metrics where a LOWER value is better (so the winner comparison flips).
_LOWER_IS_BETTER = frozenset({"time_to_approval_days", "exception_rate_pct", "rejection_rate_pct"})
# Metrics where a HIGHER value is better.
_HIGHER_IS_BETTER = frozenset({"touchless_rate_pct"})

PRIMARY_METRICS = _LOWER_IS_BETTER | _HIGHER_IS_BETTER
DEFAULT_PRIMARY_METRIC = "time_to_approval_days"


def _q2(x: Decimal) -> Decimal:
    return x.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _q1(x: Decimal) -> Decimal:
    return x.quantize(_TENTH, rounding=ROUND_HALF_UP)


def _get(row, attr, default=None):
    """Duck-typed access: dict key or attribute."""
    if isinstance(row, dict):
        return row.get(attr, default)
    return getattr(row, attr, default)


# ---------------------------------------------------------------------------
# Assignment — deterministic, stable, ratio-honouring
# ---------------------------------------------------------------------------


def assign_variant(
    invoice_id: str,
    experiment_id: str,
    *,
    split_a_pct: int = 50,
) -> str:
    """Deterministically assign an invoice to variant ``A`` or ``B``.

    Stable: the same ``(invoice_id, experiment_id)`` pair always returns the same
    variant — there is no randomness and no clock, so the assignment is
    reproducible and auditable. Keying on the experiment id too means two
    concurrent experiments split the same invoice independently.

    The split is honoured by hashing the pair into a ``[0, 100)`` bucket
    (SHA-256 → integer → mod 100) and assigning ``A`` when the bucket is below
    ``split_a_pct``. ``split_a_pct`` is clamped to ``[0, 100]``: ``100`` forces
    every invoice to ``A`` (a no-op control), ``0`` forces every invoice to ``B``.
    """
    pct = max(0, min(100, int(split_a_pct)))
    digest = hashlib.sha256(f"{experiment_id}:{invoice_id}".encode()).hexdigest()
    bucket = int(digest, 16) % 100
    return VARIANT_A if bucket < pct else VARIANT_B


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VariantMetrics:
    variant: str  # "A" | "B"
    assigned_count: int  # invoices assigned to this variant (any status)
    completed_count: int  # of those, the ones that reached a decided outcome
    approved_count: int
    rejected_count: int
    touchless_count: int  # straight-through: see services/touchless
    exception_count: int  # invoices that raised >= 1 exception
    # Decimal metrics (the comparison surface):
    median_time_to_approval_days: Decimal
    avg_time_to_approval_days: Decimal
    touchless_rate_pct: Decimal  # touchless / completed * 100
    exception_rate_pct: Decimal  # exception / assigned * 100
    rejection_rate_pct: Decimal  # rejected / completed * 100

    def metric(self, name: str) -> Decimal:
        return {
            "time_to_approval_days": self.median_time_to_approval_days,
            "touchless_rate_pct": self.touchless_rate_pct,
            "exception_rate_pct": self.exception_rate_pct,
            "rejection_rate_pct": self.rejection_rate_pct,
        }[name]


@dataclass(frozen=True)
class ExperimentResults:
    variant_a: VariantMetrics
    variant_b: VariantMetrics
    primary_metric: str
    min_sample_per_variant: int
    enough_data: bool  # both arms have >= min_sample completed invoices
    winner: str | None  # "A" | "B" | "tie" | None (None when not enough data)
    rationale: str
    notes: list[str] = field(default_factory=list)


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal("0.0")
    return _q1(Decimal(numerator) / Decimal(denominator) * Decimal("100"))


def _variant_metrics(variant: str, rows: list) -> VariantMetrics:
    """Aggregate one variant's assigned invoice rows.

    A row is duck-typed (dict or object) with:
      * ``decision`` ("approved" | "rejected" | None) — the invoice's review
        decision (None when still in flight / undecided). The caller derives it
        from ``services/touchless``' population predicates, so "completed" here
        is exactly the dashboard touchless rate's denominator.
      * ``touchless`` (bool) — ``services/touchless.touchless_clause`` held:
        approved automatically, no human correction, no exception a person
        decided. Only counted on an approved row (a rejection is a person's
        decision by construction).
      * ``time_to_approval_days`` (Decimal | None) — None for rejections / in-flight.
      * ``had_exception`` (bool) — the invoice raised >= 1 exception.

    "Completed" = the invoice reached a terminal review decision (approved OR
    rejected); in-flight invoices count toward ``assigned_count`` (and the
    exception rate, which is over *all assigned* work) but not the
    approval/touchless/rejection rates, which are over decided outcomes.
    """
    assigned = len(rows)
    approved = rejected = touchless = exception = 0
    times: list[Decimal] = []
    for r in rows:
        decision = _get(r, "decision")
        if decision == "approved":
            approved += 1
            if _get(r, "touchless"):
                touchless += 1
            ttd = _get(r, "time_to_approval_days")
            if ttd is not None:
                times.append(Decimal(str(ttd)))
        elif decision == "rejected":
            rejected += 1
        if _get(r, "had_exception"):
            exception += 1

    completed = approved + rejected
    median_ttd = _q1(_quantile(times, 0.5)) if times else Decimal("0.0")
    avg_ttd = _q1(_avg(times)) if times else Decimal("0.0")
    return VariantMetrics(
        variant=variant,
        assigned_count=assigned,
        completed_count=completed,
        approved_count=approved,
        rejected_count=rejected,
        touchless_count=touchless,
        exception_count=exception,
        median_time_to_approval_days=median_ttd,
        avg_time_to_approval_days=avg_ttd,
        touchless_rate_pct=compute_touchless_rate(
            touchless_count=touchless, decided_count=completed
        ),
        exception_rate_pct=_rate(exception, assigned),
        rejection_rate_pct=_rate(rejected, completed),
    )


def compute_experiment_results(
    rows_a: list,
    rows_b: list,
    *,
    primary_metric: str = DEFAULT_PRIMARY_METRIC,
    min_sample_per_variant: int = 10,
) -> ExperimentResults:
    """Compute per-variant metrics and a deterministic winner call.

    ``rows_a`` / ``rows_b`` are the assigned invoice rows for each variant (shape
    documented on ``_variant_metrics``). The winner is called **only** when both
    arms have ``>= min_sample_per_variant`` *completed* (decided) invoices —
    otherwise ``enough_data=False`` and ``winner=None`` (the "not enough data
    yet" state). The comparison is a plain, explainable direction check on the
    configured ``primary_metric`` (lower-is-better for time/exception/rejection,
    higher-is-better for touchless); a tie (equal to the cent/tenth) is reported
    as ``"tie"``. No statistical-significance claim is made — see the module
    docstring.
    """
    if primary_metric not in PRIMARY_METRICS:
        primary_metric = DEFAULT_PRIMARY_METRIC

    a = _variant_metrics(VARIANT_A, rows_a)
    b = _variant_metrics(VARIANT_B, rows_b)

    enough = (
        a.completed_count >= min_sample_per_variant and b.completed_count >= min_sample_per_variant
    )

    notes: list[str] = []
    if a.completed_count < min_sample_per_variant:
        notes.append(
            f"Variant A has {a.completed_count} completed invoice(s) "
            f"(need {min_sample_per_variant})."
        )
    if b.completed_count < min_sample_per_variant:
        notes.append(
            f"Variant B has {b.completed_count} completed invoice(s) "
            f"(need {min_sample_per_variant})."
        )

    if not enough:
        return ExperimentResults(
            variant_a=a,
            variant_b=b,
            primary_metric=primary_metric,
            min_sample_per_variant=min_sample_per_variant,
            enough_data=False,
            winner=None,
            rationale=(
                "Not enough data yet to call a winner — both variants need at "
                f"least {min_sample_per_variant} completed invoices."
            ),
            notes=notes,
        )

    zero_approvals = a.approved_count == 0 or b.approved_count == 0
    if primary_metric == "time_to_approval_days" and zero_approvals:
        # `median_time_to_approval_days` / `avg_time_to_approval_days` are averaged
        # only over APPROVED invoices (see `_variant_metrics`); a variant with zero
        # approvals has no real time-to-approval sample and `_variant_metrics`
        # defaults it to Decimal("0.0"). Since this metric is lower-is-better, that
        # fabricated 0.0 would otherwise beat a variant that genuinely approved
        # things quickly — crowning a variant that approved nothing. Handle it
        # explicitly instead of falling into the generic numeric comparison below.
        if a.approved_count == 0 and b.approved_count == 0:
            # Neither side has a real sample to compare on this metric — there's
            # nothing to measure on either arm. We report `enough_data=False`
            # (even though the completed-count sample-size gate above DID pass)
            # rather than `winner="tie"`, because "tie" implies "equally good",
            # which would misrepresent "we can't tell". This also keeps the
            # existing `winner is None` <=> "not enough / not callable" contract
            # (see the `ExperimentResults` docstring/comment) that the frontend
            # (`routes/experiments/+page.svelte`) relies on: it renders any
            # non-"tie" winner string as "Variant {winner} wins", so a bare
            # `None` with `enough_data=True` would render as "Variant  wins"
            # with no variant name. The real explanation is still surfaced via
            # `rationale`, which the UI always shows regardless of `enough_data`.
            return ExperimentResults(
                variant_a=a,
                variant_b=b,
                primary_metric=primary_metric,
                min_sample_per_variant=min_sample_per_variant,
                enough_data=False,
                winner=None,
                rationale=(
                    "Neither variant approved any invoices — time-to-approval "
                    "isn't comparable when there's nothing to measure."
                ),
                notes=notes,
            )

        # Exactly one variant approved zero invoices. Its 0.0 median is a
        # fabricated "no data" default, not a real measurement, so the variant
        # that actually approved invoices wins outright regardless of the raw
        # numeric comparison.
        zero_variant, real_variant = (a, b) if a.approved_count == 0 else (b, a)
        winner = real_variant.variant
        rationale = (
            f"Variant {winner} wins by default: variant {zero_variant.variant} "
            f"approved 0 invoices, so its time-to-approval isn't a real "
            f"measurement. Variant {winner} approved {real_variant.approved_count} "
            f"invoice(s) with a median of {real_variant.median_time_to_approval_days} "
            "days."
        )
        return ExperimentResults(
            variant_a=a,
            variant_b=b,
            primary_metric=primary_metric,
            min_sample_per_variant=min_sample_per_variant,
            enough_data=True,
            winner=winner,
            rationale=rationale,
            notes=notes,
        )

    a_val = a.metric(primary_metric)
    b_val = b.metric(primary_metric)
    lower_is_better = primary_metric in _LOWER_IS_BETTER

    if a_val == b_val:
        winner = "tie"
        rationale = (
            f"Variants A and B tie on {primary_metric} ({a_val}). No measurable "
            "difference on the primary metric."
        )
    else:
        a_better = (a_val < b_val) if lower_is_better else (a_val > b_val)
        winner = VARIANT_A if a_better else VARIANT_B
        win_val, lose_val = (a_val, b_val) if a_better else (b_val, a_val)
        direction = "lower" if lower_is_better else "higher"
        rationale = (
            f"Variant {winner} wins on {primary_metric}: {win_val} vs {lose_val} "
            f"({direction} is better). Based on {a.completed_count} (A) and "
            f"{b.completed_count} (B) completed invoices."
        )

    return ExperimentResults(
        variant_a=a,
        variant_b=b,
        primary_metric=primary_metric,
        min_sample_per_variant=min_sample_per_variant,
        enough_data=True,
        winner=winner,
        rationale=rationale,
        notes=notes,
    )
