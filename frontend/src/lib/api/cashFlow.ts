// Typed helpers for the AI Cash-Flow Copilot's Phase 3 enact endpoints
// (`POST /api/cash-flow/plans/{plan_id}/{draft-run,capture-discounts}`). Both
// re-derive the plan server-side from the SAME defining parameters the
// `propose_payment_plan` tool result already carries (granularity,
// horizon_days, min_balance_threshold, cash_budget, cost_of_capital_pct) —
// callers must echo those back verbatim, never invent their own. See
// docs/cash-flow-copilot.md §6.
import { api } from '#lib/api.ts';
import type {
	PaymentPlanResult,
	PlanVarianceResult,
	SaveCashPlanResult,
	SavedPlanDetail,
	SavedPlanSummary
} from '#lib/types/cashFlow.ts';

export interface PlanReplayParams {
	granularity: string;
	horizon_days: number;
	min_balance_threshold: string | null;
	cash_budget: string | null;
	cost_of_capital_pct: string;
}

/** The save body — the replay params plus two fields that are deliberately NOT
 *  part of the plan id: the opening balance the curve was rendered with (it
 *  changes what the chart shows, never which commitments are due) and an
 *  optional human label. */
export interface PlanSaveParams extends PlanReplayParams {
	opening_balance: string | null;
	label?: string;
}

export interface DraftRunResult {
	plan_id: string;
	/** False when this call returned an ALREADY-EXISTING draft run for this
	 *  plan_id (idempotent replay) rather than staging a new one. */
	created: boolean;
	run_id: string;
	status: string;
	total_amount: string;
	payment_count: number;
	requires_cfo_approval: boolean;
}

export interface CaptureDiscountsResult {
	plan_id: string;
	accepted_offer_ids: string[];
	accepted_count: number;
	skipped_count: number;
	total_savings_selected: string;
}

/** Build the replay body from a `propose_payment_plan` tool result — the
 *  single place that decides which fields round-trip back to the enact
 *  endpoints, so a future field added to the plan can't accidentally get
 *  echoed somewhere it shouldn't (or forgotten somewhere it should). */
export function planReplayParams(plan: PaymentPlanResult): PlanReplayParams {
	return {
		granularity: plan.granularity,
		horizon_days: plan.horizon_days,
		min_balance_threshold: plan.min_balance_threshold,
		cash_budget: plan.cash_budget,
		cost_of_capital_pct: plan.cost_of_capital_pct
	};
}

/** Tier 1 — stage a DRAFT payment run over the plan's currently-payable
 *  commitments. Never executes; the CFO-gated `/execute` path is unchanged.
 *  Idempotent on `plan.plan_id`. */
export function createDraftRunFromPlan(plan: PaymentPlanResult): Promise<DraftRunResult> {
	return api.post<DraftRunResult>(
		`/api/cash-flow/plans/${plan.plan_id}/draft-run`,
		planReplayParams(plan)
	);
}

/** Tier 2 — accept every discount offer the SAME optimizer pass selected.
 *  Status-only; never moves money. A repeat call is a clean no-op. */
export function captureDiscountsFromPlan(
	plan: PaymentPlanResult
): Promise<CaptureDiscountsResult> {
	return api.post<CaptureDiscountsResult>(
		`/api/cash-flow/plans/${plan.plan_id}/capture-discounts`,
		planReplayParams(plan)
	);
}

/** Freeze this proposal as a saved plan so it can later be compared to what
 *  actually got paid. Read-only over the money path. Idempotent: saving an
 *  already-saved plan returns the ORIGINAL snapshot (`created: false`) rather
 *  than restating it against newer data. */
export function saveCashFlowPlan(
	plan: PaymentPlanResult,
	label?: string
): Promise<SaveCashPlanResult> {
	const body: PlanSaveParams = {
		...planReplayParams(plan),
		opening_balance: plan.opening_balance
	};
	if (label?.trim()) body.label = label.trim();
	return api.post<SaveCashPlanResult>(`/api/cash-flow/plans/${plan.plan_id}/save`, body);
}

/** Saved plans, newest first. Scoped to the selected entity unless
 *  `consolidated` is set, which lists every plan in the tenant (a consolidated
 *  snapshot belongs to no single entity). */
export function listSavedPlans(opts?: {
	limit?: number;
	consolidated?: boolean;
}): Promise<SavedPlanSummary[]> {
	const params = new URLSearchParams();
	if (opts?.limit) params.set('limit', String(opts.limit));
	if (opts?.consolidated) params.set('consolidated', 'true');
	const qs = params.toString();
	return api.get<SavedPlanSummary[]>(`/api/cash-flow/plans${qs ? `?${qs}` : ''}`);
}

/** One saved snapshot, including its frozen cash curve. */
export function getSavedPlan(planId: string): Promise<SavedPlanDetail> {
	return api.get<SavedPlanDetail>(`/api/cash-flow/plans/${planId}`);
}

/** Plan vs actual for a saved snapshot — compute-on-read, so re-running it
 *  later simply scores more elapsed periods. */
export function getSavedPlanVariance(planId: string): Promise<PlanVarianceResult> {
	return api.get<PlanVarianceResult>(`/api/cash-flow/plans/${planId}/variance`);
}

/** Discard a saved snapshot. Removes the baseline only — any draft run staged
 *  from the same plan, and every payment and offer, are untouched. */
export function deleteSavedPlan(planId: string): Promise<void> {
	return api.delete(`/api/cash-flow/plans/${planId}`);
}
