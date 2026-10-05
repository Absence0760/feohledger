// Typed helpers for the adaptive-workflow endpoints (`/api/adaptive`).
//
// The whole surface is DETERMINISTIC (no LLM), COMPUTE-ON-READ and — with two
// exceptions — READ-ONLY. The two writes are `dismissSuggestion` and
// `applyThresholdRecommendation` / `applyRoutingSuggestion`; everything else is
// a read model over the tenant's own approval history. Nothing here changes a
// workflow by being displayed: a suggestion is a recommendation a human
// accepts, never a change that already happened.
//
// Types live here rather than in `#lib/types/` because this is the only
// consumer, and because every money field on the wire is an exact decimal
// STRING (`MoneyString`) — the backend stringifies its `Decimal`s — so nothing
// in this module may be typed `number` for currency (frontend/CLAUDE.md
// § Money formatting).
//
// One rule the caller must honour: `getFeedback` writes an
// `adaptive_feedback.viewed` ACCESS-AUDIT row on the server. Never poll it and
// never fetch it speculatively — call it on an explicit user action, or once
// per page visit.
import { api } from '#lib/api.ts';
import type { MoneyString } from '#lib/utils/money.ts';

// ---------------------------------------------------------------------------
// Approval patterns — GET /api/adaptive/approval-patterns
// ---------------------------------------------------------------------------

export interface ApproverPattern {
	approver_id: string;
	approver_name: string | null;
	approved_count: number;
	rejected_count: number;
	approval_rate_pct: string;
	median_time_to_approve_days: string;
	avg_time_to_approve_days: string;
	sample_size: number;
}

export interface VendorPattern {
	vendor_id: string | null;
	vendor_name: string;
	approved_count: number;
	rejected_count: number;
	approval_rate_pct: string;
	unmodified_count: number;
	consistency_pct: string;
	avg_approved_amount: MoneyString;
	median_approved_amount: MoneyString;
	min_approved_amount: MoneyString;
	max_approved_amount: MoneyString;
	sample_size: number;
	/**
	 * Approvals EXCLUDED from the four money fields above because their amount
	 * could not be expressed in the org's reporting currency — but still counted
	 * in `sample_size`. Non-zero means the average is over `approved_count`
	 * minus this, so rendering it beside the sample count without disclosing it
	 * is a wrong number (decisions §79/§82). Never move the denominator to make
	 * them agree — say what was left out.
	 */
	unconverted_count: number;
}

export interface ApprovalPatterns {
	generated_at: string;
	lookback_days: number;
	entity_id: string | null;
	approvers: ApproverPattern[];
	vendors: VendorPattern[];
}

export function getApprovalPatterns(days?: number): Promise<ApprovalPatterns> {
	const qs = days ? `?days=${days}` : '';
	return api.get<ApprovalPatterns>(`/api/adaptive/approval-patterns${qs}`);
}

// ---------------------------------------------------------------------------
// Anomalies — GET /api/adaptive/anomalies
// ---------------------------------------------------------------------------

export interface VendorBaseline {
	vendor_id: string | null;
	vendor_name: string;
	sample_size: number;
	mean_amount: MoneyString;
	median_amount: MoneyString;
	stdev_amount: MoneyString;
	min_amount: MoneyString;
	max_amount: MoneyString;
	typical_approver_ids: string[];
	median_time_to_approve_days: string;
}

/**
 * The backend emits `info` and `warning` only (`services/adaptive_workflows.py`
 * → `AnomalyFlag.severity`). `error` is accepted here so an added severity maps
 * to a louder tone rather than being silently absorbed into `warning`, which is
 * what the old `severity === 'error' ? 'danger' : 'warning'` ternary did to
 * every `info` flag on the page.
 */
export type AnomalySeverity = 'info' | 'warning' | 'error';

export interface AnomalyFlag {
	code: string;
	severity: AnomalySeverity | (string & {});
	message: string;
	observed: string;
	expected: string;
}

export interface InvoiceAnomaly {
	invoice_id: string;
	vendor_id: string | null;
	vendor_name: string;
	amount: MoneyString;
	/**
	 * What `amount` is DENOMINATED IN — normally the org's reporting currency,
	 * but the invoice's own billed currency when it carries no usable rate lock
	 * and the backend fell back to the billed figure "for DISPLAY only". Always
	 * label `amount` with this, never with the org's reporting currency: the
	 * fallback figure is not in it, and stamping it on is a wrong number rather
	 * than a missing one.
	 */
	amount_currency: string;
	insufficient_history: boolean;
	baseline: VendorBaseline | null;
	flags: AnomalyFlag[];
}

export interface AnomalyBatch {
	/** Invoices SCANNED, not money — see the `_scanned` suffix. */
	total_scanned: number;
	flagged: InvoiceAnomaly[];
}

export function getAnomalies(): Promise<AnomalyBatch> {
	return api.get<AnomalyBatch>('/api/adaptive/anomalies');
}

// ---------------------------------------------------------------------------
// Advisory suggestions — GET /suggestions, POST /suggestions/{id}/dismiss
// ---------------------------------------------------------------------------

export type SuggestionStatus = 'open' | 'dismissed' | 'applied' | 'stale';

export interface WorkflowSuggestion {
	id: string;
	kind: string;
	vendor_id: string | null;
	vendor_name: string;
	title: string;
	rationale: string | null;
	payload: Record<string, unknown>;
	confidence_pct: string;
	status: SuggestionStatus;
	created_at: string | null;
	dismissed_at: string | null;
}

export interface SuggestionList {
	suggestions: WorkflowSuggestion[];
}

export function getSuggestions(
	status: 'open' | 'dismissed' | 'applied' | 'stale' | 'all' = 'open'
): Promise<SuggestionList> {
	return api.get<SuggestionList>(`/api/adaptive/suggestions?status=${status}`);
}

/** Advisory-only: flips the suggestion row's status. Changes no workflow. */
export function dismissSuggestion(id: string): Promise<SuggestionList> {
	return api.post<SuggestionList>(`/api/adaptive/suggestions/${id}/dismiss`, {});
}

// ---------------------------------------------------------------------------
// Smart routing — GET /routing-suggestion, POST /routing-suggestion/apply
// ---------------------------------------------------------------------------

export interface RoutingCandidate {
	approver_id: string;
	approver_name: string | null;
	/** 0–100 routing fit, NET of the outcome down-weight. */
	score: string;
	/** The forward score BEFORE the down-weight — the explainability half. */
	base_score: string;
	/** Points subtracted for decisions a human later overturned (>= 0). */
	outcome_penalty: string;
	rank: number;
	median_time_to_approve_days: string;
	approval_rate_pct: string;
	sample_size: number;
	vendor_approved_count: number;
	overturn_rate_pct: string;
	overturned_count: number;
	outcome_sample_size: number;
	reasons: string[];
}

export interface RoutingSuggestion {
	invoice_id: string | null;
	vendor_id: string | null;
	vendor_name: string;
	amount: MoneyString;
	insufficient_history: boolean;
	candidates: RoutingCandidate[];
}

export interface ApplyRoutingResult {
	invoice_id: string;
	/** False on the idempotent no-op — already assigned to the top pick. */
	assigned: boolean;
	assigned_to_id: string;
	assigned_to_name: string | null;
	rank: number;
	score: string;
}

export function getRoutingSuggestion(invoiceId: string): Promise<RoutingSuggestion> {
	return api.get<RoutingSuggestion>(
		`/api/adaptive/routing-suggestion?invoice_id=${encodeURIComponent(invoiceId)}`
	);
}

export function applyRoutingSuggestion(invoiceId: string): Promise<ApplyRoutingResult> {
	return api.post<ApplyRoutingResult>('/api/adaptive/routing-suggestion/apply', {
		invoice_id: invoiceId
	});
}

// ---------------------------------------------------------------------------
// Auto-approve threshold — GET /threshold-recommendation, POST .../apply
// ---------------------------------------------------------------------------

export interface ThresholdEvidenceItem {
	vendor_id: string | null;
	vendor_name: string;
	based_on_n: number;
	max_approved_amount: MoneyString;
	median_approved_amount: MoneyString;
}

export interface ThresholdRecommendation {
	should_raise: boolean;
	current_threshold: MoneyString;
	recommended_threshold: MoneyString;
	cap_threshold: MoneyString;
	qualifying_vendor_count: number;
	/** Clean invoices behind the recommendation — a COUNT, not money. */
	total_clean_invoices: number;
	/** "ok" | "insufficient_evidence" | "no_increase" | "at_cap" |
	 *  "outcome_pullback" | "outcome_freeze" */
	reason_code: string;
	rationale: string;
	evidence: ThresholdEvidenceItem[];
	workflow_id: string | null;
	lookback_days: number;
}

export interface ApplyThresholdResult {
	/** False on the idempotent no-op (the recommendation does not raise). */
	applied: boolean;
	workflow_id: string;
	previous_threshold: MoneyString;
	new_threshold: MoneyString;
	reason_code: string;
	rationale: string;
	version_number: number | null;
}

export function getThresholdRecommendation(): Promise<ThresholdRecommendation> {
	return api.get<ThresholdRecommendation>('/api/adaptive/threshold-recommendation');
}

/**
 * Apply the auto-approve threshold raise.
 *
 * `expected_recommended_threshold` is the backend's optimistic-concurrency
 * guard and exists FOR THIS UI: the number rendered on screen is sent back, and
 * the apply 409s if the deterministic stats have moved underneath it. Always
 * send what was rendered — omitting it is how a stale number gets applied.
 */
export function applyThresholdRecommendation(
	expectedRecommendedThreshold: MoneyString,
	workflowId?: string | null
): Promise<ApplyThresholdResult> {
	const body: Record<string, unknown> = {
		expected_recommended_threshold: expectedRecommendedThreshold
	};
	if (workflowId) body.workflow_id = workflowId;
	return api.post<ApplyThresholdResult>('/api/adaptive/threshold-recommendation/apply', body);
}

// ---------------------------------------------------------------------------
// Feedback loop — GET /feedback  (WRITES AN ACCESS-AUDIT ROW — see header)
// ---------------------------------------------------------------------------

export interface OutcomeStats {
	auto_approved_count: number;
	voided_count: number;
	corrected_count: number;
	rejected_count: number;
	overturned_count: number;
	overturn_rate_pct: string;
	/** True below the minimum sample — there is no rate to show, only this. */
	insufficient_data: boolean;
}

export interface EffectivenessMetric {
	name: string;
	/** Null whenever `insufficient_data` — never render a substitute figure. */
	value_pct: string | null;
	sample_size: number;
	insufficient_data: boolean;
	/** A complete, PII-free explanatory sentence from the backend. */
	label: string;
}

export interface AdaptiveFeedback {
	lookback_days: number;
	entity_id: string | null;
	outcomes: OutcomeStats;
	metrics: EffectivenessMetric[];
	base_recommendation: ThresholdRecommendation;
	adjusted_recommendation: ThresholdRecommendation;
}

export function getFeedback(): Promise<AdaptiveFeedback> {
	return api.get<AdaptiveFeedback>('/api/adaptive/feedback');
}
