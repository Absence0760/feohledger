/**
 * Reading a `workspace_close_refused` refusal's blocker codes
 * (`backend/app/api/refusals.py`, decisions §303).
 *
 * Its own module, and deliberately free of catalogue keys: `api/codedRefusals.ts`
 * needs it, and that registry is reachable from every route, while the
 * blockers' sentences live in the settings slice only /organization loads
 * (`types/workspaceClosure.ts`; `i18n/slices.test.ts` enforces the split).
 */

/** The blocker codes from a `workspace_close_refused` refusal's params, or
 *  `null` when they are missing or malformed. */
export function refusalBlockers(params: Record<string, unknown>): string[] | null {
	const raw = params.blockers;
	if (!Array.isArray(raw) || raw.length === 0) return null;
	if (!raw.every((b) => typeof b === 'string' && b)) return null;
	return raw as string[];
}
