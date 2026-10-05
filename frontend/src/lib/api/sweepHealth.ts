// Typed helper for the operator sweep-health report
// (`GET /api/health/sweeps`, `backend/app/api/health.py`, admin-gated).
//
// The public `GET /api/health` deliberately stays a static `ok` and says
// nothing about sweeps — folding a degraded sweep into the liveness probe
// would turn a misconfigured audit sink into a rolling restart loop
// (`backend/docs/background-sweeps.md`). This admin read is therefore the only
// surface on which a dead or stalled sweep is visible at all.
import { api } from '#lib/api.ts';

/** Per-sweep state. `not_started` (enabled but never registered) and `died`
 *  are the real defects; `disabled` is an expected, benign state. */
export type SweepState =
	| 'not_started'
	| 'disabled'
	| 'starting'
	| 'running'
	| 'idle'
	| 'stalled'
	| 'stopped'
	| 'died';

/** `partial` = the tick completed but the sweep reported failures of its own. */
export type SweepOutcome = 'ok' | 'partial' | 'error';

/** One sweep's health. PII-free by construction: only an exception CLASS name
 *  is ever carried, and the raw per-sweep counters (which would leak
 *  cross-tenant cardinality to an ordinary tenant admin) stay server-side. */
export interface SweepHealth {
	name: string;
	state: SweepState;
	enabled: boolean;
	started_at: string | null;
	last_run_started_at: string | null;
	last_run_finished_at: string | null;
	last_outcome: SweepOutcome | null;
	last_error_class: string | null;
	last_failure_count: number;
	consecutive_failures: number;
	total_runs: number;
	total_failed_runs: number;
	exit_error_class: string | null;
}

/** Aggregate + per-sweep report for ONE backend process (not the cluster). */
export interface SweepHealthReport {
	/** ok | degraded | failing — worst sweep wins. */
	state: 'ok' | 'degraded' | 'failing';
	/** Consecutive failed runs at which a sweep is called degraded. */
	failure_alert_streak: number;
	sweeps: SweepHealth[];
}

export function getSweepHealth(): Promise<SweepHealthReport> {
	return api.get<SweepHealthReport>('/api/health/sweeps');
}
