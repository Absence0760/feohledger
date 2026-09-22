import type { ExceptionSummary } from '$lib/types/exceptionSummary';

/**
 * A stubbed `GET /api/exceptions/summary` body. Every exceptions spec that
 * fakes the chip tallies builds them here, and the return type is the app's own
 * `ExceptionSummary`, so a field the page starts reading is a compile error in
 * `pnpm check:e2e` rather than a stub that silently stops matching the wire —
 * the severity chip row arrived with `by_severity`, and four hand-written stubs
 * lacked it (`frontend/CLAUDE.md` § `pnpm check` does not cover `tests-e2e/`).
 */
export function exceptionSummary(overrides: Partial<ExceptionSummary> = {}): ExceptionSummary {
	return {
		open: 0,
		escalated: 0,
		resolved: 0,
		dismissed: 0,
		by_type: {},
		by_severity: {},
		...overrides
	};
}
