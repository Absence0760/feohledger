"""Request / response schemas for the AI Cash-Flow Copilot's Phase 3 enact
routes (`app/api/cash_flow.py`). See docs/cash-flow-copilot.md §5/§6."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.money import OptionalExactMoneyInput


class CashFlowPlanReplay(BaseModel):
    """The plan-defining parameters read off the `propose_payment_plan` tool
    result that produced the plan being enacted (`PaymentPlanResult`'s own
    `granularity` / `horizon_days` / `min_balance_threshold` / `cash_budget` /
    `cost_of_capital_pct` fields — already RESOLVED values, not the original
    possibly-`None` request overrides).

    NEVER trusted for WHAT to act on — the server re-derives its own
    commitment rows / discount-offer selection from these inputs. It is used
    ONLY to recompute the plan's `plan_id` and confirm it still matches the
    id in the URL (the stale-plan guard — see
    `services.cash_flow_plan.compute_plan_id`). Mirrors
    `ProposePaymentPlanParams` minus `opening_balance`, which only affects the
    displayed cash-position curve — never which invoices are due or which
    discounts are worth capturing — so it plays no part in either enact
    action.
    """

    granularity: Literal["day", "week", "month"] = "week"
    horizon_days: int | None = Field(default=None, ge=7, le=730)
    # Exact decimal STRINGs, parsed the same way `ProposePaymentPlanParams`
    # parses them (`schemas/money.py::parse_exact_money`). The two sides MUST
    # agree: `compute_plan_id` hashes `str()` of each, so a replay that parsed
    # a budget differently from the propose that issued the id would fail the
    # stale-plan check on a plan that had not actually changed. The parser
    # normalises nothing for the same reason — `"8.00"` stays `Decimal("8.00")`
    # (see `test_the_decimal_scale_is_part_of_the_preimage`). A shipped client
    # already sends these as strings (`frontend/src/lib/api/cashFlow.ts`
    # `PlanReplayParams`), so refusing a JSON number closes a door nothing
    # walks through.
    min_balance_threshold: OptionalExactMoneyInput = None
    cash_budget: OptionalExactMoneyInput = Field(default=None, ge=0)
    cost_of_capital_pct: Decimal | None = Field(default=None, ge=0, le=100)


class DraftRunSkip(BaseModel):
    invoice_id: uuid.UUID
    #: `currency_unknown` — the invoice records no currency, so no run can
    #: name what it would pay; `currency_not_run_currency` — it is in another
    #: currency than the run's (`params.currency`).
    code: Literal["currency_unknown", "currency_not_run_currency"]
    params: dict[str, str] = Field(default_factory=dict)


class DraftRunResponse(BaseModel):
    plan_id: str
    # False when this call returned an ALREADY-EXISTING draft run for this
    # plan_id (idempotent replay) rather than staging a new one.
    created: bool
    run_id: uuid.UUID
    status: str
    total_amount: Decimal
    payment_count: int
    requires_cfo_approval: bool
    # A payment run is single-currency (`PaymentRun.total_amount` is a bare
    # Numeric and the CFO threshold is compared against it as a bare number),
    # so a plan spanning several currencies stages the ORG's reporting-currency
    # slice and says what it left behind. `excluded_currency_count` is 0 for
    # every single-currency tenant.
    run_currency: str = "USD"
    excluded_currency_count: int = 0
    # Payable invoices in the plan's horizon that were NOT staged, each with
    # the coded reason the client states (`DRAFT_RUN_SKIP_*` in
    # `api/cash_flow.py`). An invoice whose currency is not recorded is never
    # read as the run's currency — it is listed here instead (decisions §160 /
    # §196 / §200: a missing currency never becomes the default).
    skipped: list[DraftRunSkip] = Field(default_factory=list)


class CaptureDiscountsResponse(BaseModel):
    plan_id: str
    accepted_offer_ids: list[str]
    accepted_count: int
    # Offers the SAME optimizer pass selected but that were skipped because
    # they were no longer `offered` (already accepted/captured/declined/
    # expired — e.g. a prior call, or a manual accept via /discounts, already
    # handled them) or their tier window closed between the optimizer pass
    # and now. Never an error — status-only idempotency.
    skipped_count: int
    total_savings_selected: Decimal


# ---------------------------------------------------------------------------
# Saved plans + plan-vs-actual (`models.cash_plan.CashPlan`).
#
# Money is a `Decimal` on every schema below, so it serializes as an exact JSON
# string — the same contract the copilot tools hold. No `float` in this path.
# ---------------------------------------------------------------------------


class CashFlowPlanSaveRequest(CashFlowPlanReplay):
    """Replay body for `POST /api/cash-flow/plans/{plan_id}/save`.

    Same stale-plan guard as the enact routes — the server recomputes
    `plan_id` from these RESOLVED parameters and refuses a mismatch — plus two
    fields that are deliberately NOT part of that id:

    * `opening_balance` — the explicit override the proposal was rendered
      with, if any. It seeds the displayed curve but changes neither which
      commitments are due nor which discounts are worth capturing, which is
      why `compute_plan_id` excludes it (see `CashFlowPlanReplay`). Replaying
      it keeps the SAVED curve identical to the one on screen; omitting it
      re-resolves through the normal opening-balance chain.
    * `label` — an optional human name for the snapshot. Never used for
      lookup, so two plans may share one.
    """

    opening_balance: OptionalExactMoneyInput = Field(default=None, max_digits=15, decimal_places=2)
    label: str | None = Field(default=None, max_length=200)

    # The parent's three plan-defining params are unbounded there because a
    # replay only hashes them into `plan_id` and compares them — no column can
    # overflow. Saving PERSISTS them onto `cash_plans`, so on this subclass they
    # are bounded to those columns: without it an out-of-range value reaches
    # Postgres and raises NumericValueOutOfRangeError — a 500 for what is really
    # a 422.
    # Re-declared, so the parent's exact-string parsing has to be re-declared
    # WITH them: a subclass field replaces the parent's annotation outright, and
    # a save that parsed a budget differently from the replay it extends would
    # hash to a different `plan_id` than the snapshot it is filing under.
    min_balance_threshold: OptionalExactMoneyInput = Field(
        default=None, max_digits=15, decimal_places=2
    )
    cash_budget: OptionalExactMoneyInput = Field(
        default=None, ge=0, max_digits=15, decimal_places=2
    )
    cost_of_capital_pct: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=5, decimal_places=2
    )


class SavedPlanPeriod(BaseModel):
    period: str
    period_start: date
    period_end: date
    opening: Decimal
    outflow: Decimal
    closing: Decimal
    below_threshold: bool
    unconverted_count: int = 0


class SavedPlanSummary(BaseModel):
    """List-row shape — what a picker needs, without the frozen curve."""

    plan_id: str
    plan_date: date
    label: str | None
    currency: str
    granularity: str
    horizon_days: int
    # NULL means the snapshot is CONSOLIDATED — a whole-group plan spanning
    # every entity — not an unstamped row. See `models/cash_plan.py`.
    entity_id: uuid.UUID | None
    consolidated: bool
    opening_balance: Decimal
    min_balance_threshold: Decimal | None
    first_shortfall_period: str | None
    total_savings_selected: Decimal
    period_count: int
    unconverted_count: int
    created_at: datetime


class SavedPlanDetail(SavedPlanSummary):
    cash_budget: Decimal | None
    cost_of_capital_pct: Decimal
    total_outlay_selected: Decimal
    periods: list[SavedPlanPeriod]
    selected_offer_ids: list[str]
    unretimed_offer_ids: list[str]
    # True once this plan has been enacted into a draft payment run — the SAME
    # deterministic key lives on `payment_runs.plan_id`.
    has_draft_run: bool = False


class SaveCashPlanResponse(BaseModel):
    # False when this call returned an ALREADY-SAVED snapshot for this plan_id
    # rather than storing a new one. A saved plan is a frozen baseline: a
    # repeat save must not restate it against newer data, which would rewrite
    # the very thing a variance is measured against.
    created: bool
    plan: SavedPlanDetail


class PlanVariancePeriod(BaseModel):
    period: str
    period_start: date
    period_end: date
    planned_outflow: Decimal
    actual_outflow: Decimal
    # actual - planned. Positive = more cash left than the plan projected.
    variance: Decimal
    # `elapsed` | `in_progress` | `future`. Only `elapsed` periods are scored
    # into the totals (see `services.cash_flow_plan.compare_plan_to_actual`).
    status: str


class PlanVarianceResponse(BaseModel):
    plan_id: str
    plan_date: date
    label: str | None
    currency: str
    granularity: str
    consolidated: bool
    as_of: date
    periods: list[PlanVariancePeriod]
    planned_total: Decimal
    actual_total: Decimal
    variance_total: Decimal
    elapsed_period_count: int
    open_period_count: int
    unmatched_actual_periods: list[str]
    unmatched_actual_total: Decimal
    # Completed payments the comparison could NOT place. `undated` = no
    # `completed_at`, so there is no period to bucket it into; `unconvertible`
    # = its outflow is not establishable in the plan's currency
    # (`currency_conversion.payment_reporting_amount_sql` deliberately refuses
    # a face-value fallback for regulated totals). Both are excluded from every
    # figure above and counted here rather than dropped silently.
    undated_payment_count: int = 0
    unconvertible_payment_count: int = 0
    # How the plan's own discount recommendations actually played out.
    selected_offer_count: int = 0
    captured_offer_count: int = 0
    # Non-zero means the PLANNED curve itself mixed currencies (see the plan's
    # own `unconverted_count`) — the variance inherits that caveat.
    planned_unconverted_count: int = 0
