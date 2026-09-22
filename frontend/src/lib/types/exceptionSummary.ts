/**
 * `GET /api/exceptions/summary` — the exception queue's chip tallies.
 *
 * Faceted: each tally honours every filter the list takes EXCEPT its own
 * dimension, so a chip reads what the table would show if it were clicked
 * (`docs/decisions.md` §188).
 *
 * Its own module, not `types/exception.ts`, because e2e stubs type against it
 * (`tests-e2e/exceptions/summary.ts`) and `tsconfig.e2e.json` cannot follow
 * that module's type import out of a `.svelte` file. A stub missing a field is
 * then a compile error rather than a silent contract drift.
 */
export interface ExceptionSummary {
	open: number;
	escalated: number;
	resolved: number;
	dismissed: number;
	by_type: Record<string, number>;
	by_severity: Record<string, number>;
}
