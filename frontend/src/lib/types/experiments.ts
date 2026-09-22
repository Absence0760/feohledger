// Types for A/B testing of workflow rules (workflow experiments).
// Money / statistic Decimals arrive as string-Decimal from the backend —
// display via `formatMoney` / render directly, never `parseFloat`.

import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';

export type ExperimentStatus = 'draft' | 'running' | 'concluded';

export type PrimaryMetric =
	| 'time_to_approval_days'
	| 'touchless_rate_pct'
	| 'exception_rate_pct'
	| 'rejection_rate_pct';

// Every status the backend's `WorkflowExperiment.status` can hold — the array
// the filter chips are built from, so a new status joins them for free rather
// than being a fourth hand-written chip nobody adds.
export const EXPERIMENT_STATUSES: ExperimentStatus[] = ['draft', 'running', 'concluded'];

// The primary metric is a table cell AND a `<select>` option beside the
// translated status badge, so an English literal here reads as a gap.
export const PRIMARY_METRIC_LABEL_KEYS: Record<PrimaryMetric, MessageKey> = {
	time_to_approval_days: 'experiments.metric.timeToApprovalDays',
	touchless_rate_pct: 'experiments.metric.touchlessRatePct',
	exception_rate_pct: 'experiments.metric.exceptionRatePct',
	rejection_rate_pct: 'experiments.metric.rejectionRatePct'
};

/**
 * The i18n key carrying each status label — never the English string itself.
 *
 * `/experiments` already rendered translated filter chips (from its own
 * hand-written `experiments.chip.*` keys) directly beside an untranslated
 * `Running` badge fed by this map. One keyed map now feeds both, so the two
 * can't drift either. `Record<ExperimentStatus, MessageKey>` makes a new
 * status a compile error rather than a blank badge, and `experiments.test.ts`
 * proves every key exists in the catalogue.
 */
export const STATUS_LABEL_KEYS: Record<ExperimentStatus, MessageKey> = {
	draft: 'experiments.status.draft',
	running: 'experiments.status.running',
	concluded: 'experiments.status.concluded'
};

/**
 * The message key for a status, or `null` for one this frontend doesn't know
 * (the caller renders the raw value — visible and searchable — rather than a
 * blank badge).
 */
export function experimentStatusLabelKey(status: string): MessageKey | null {
	return STATUS_LABEL_KEYS[status as ExperimentStatus] ?? null;
}

// Badge tone per status. It replaces a page-local helper that mapped these to
// the colour names `green` / `amber` / `grey` — naming the paint rather than
// the meaning is how a tone ends up spelled four ways.
export const STATUS_TONES: Record<ExperimentStatus, BadgeTone> = {
	// A draft experiment is measuring nothing yet — amber, the same "waiting on
	// a human" register the rest of the app uses.
	draft: 'warning',
	running: 'success',
	concluded: 'muted'
};

export interface Experiment {
	id: string;
	name: string;
	description: string | null;
	workflow_definition_id: string;
	workflow_definition_name: string | null;
	config_a: Record<string, unknown>;
	config_b: Record<string, unknown>;
	split_a_pct: number;
	primary_metric: PrimaryMetric;
	min_sample_per_variant: number;
	status: ExperimentStatus;
	started_at: string | null;
	ended_at: string | null;
	assigned_count: number;
	entity_id: string | null;
	created_at: string | null;
	updated_at: string | null;
}

export interface ExperimentListResponse {
	experiments: Experiment[];
}

export interface ExperimentCreate {
	name: string;
	description?: string | null;
	workflow_definition_id: string;
	config_a: Record<string, unknown>;
	config_b: Record<string, unknown>;
	split_a_pct?: number;
	primary_metric?: PrimaryMetric;
	min_sample_per_variant?: number;
}

export interface VariantMetrics {
	variant: 'A' | 'B';
	assigned_count: number;
	completed_count: number;
	approved_count: number;
	rejected_count: number;
	touchless_count: number;
	exception_count: number;
	median_time_to_approval_days: string;
	avg_time_to_approval_days: string;
	touchless_rate_pct: string;
	exception_rate_pct: string;
	rejection_rate_pct: string;
}

export interface ExperimentResults {
	experiment_id: string;
	experiment_name: string;
	status: ExperimentStatus;
	primary_metric: PrimaryMetric;
	min_sample_per_variant: number;
	enough_data: boolean;
	winner: 'A' | 'B' | 'tie' | null;
	rationale: string;
	notes: string[];
	variant_a: VariantMetrics;
	variant_b: VariantMetrics;
	generated_at: string;
}
