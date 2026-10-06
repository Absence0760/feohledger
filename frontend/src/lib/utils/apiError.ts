/**
 * Rendering a FastAPI error body as human-readable text.
 *
 * `{"detail": …}` is the backend's error envelope, but `detail` is **not always
 * a string**: a 422 raised by Pydantic carries a LIST of `{loc, msg, type}`
 * objects, and a few routes return a single `{message, …}` object. Passing any
 * of those into `new Error(...)` stringifies them as the notorious
 * `"[object Object]"` — which is exactly what every validation failure surfaced
 * as in a toast (a blank required field on the expense modal being the case
 * that found it).
 *
 * Lives in `utils/` rather than inside `api.ts` because it is pure — no
 * `$app/env/public`, no `fetch`, no browser globals — so it is unit-testable
 * under the plain-Node vitest config. `api.ts` is its only production caller
 * (plus `api/expenses.ts`, which hand-rolls one `fetch`).
 */

interface ValidationErrorItem {
	loc?: unknown;
	msg?: unknown;
}

/** Segments of a Pydantic `loc` that name the request part, not the field.
 *  Useless to a user reading a toast — `expense_date` is the actionable half,
 *  `body.expense_date` is noise. */
const LOC_SOURCE_SEGMENTS = new Set(['body', 'query', 'path', 'header', 'cookie']);

function fieldFromLoc(loc: unknown): string {
	if (!Array.isArray(loc)) return '';
	return loc
		.filter((part): part is string => typeof part === 'string' && !LOC_SOURCE_SEGMENTS.has(part))
		.join('.');
}

function renderItem(item: unknown): string {
	if (typeof item === 'string') return item.trim();
	if (!item || typeof item !== 'object') return '';
	const { loc, msg } = item as ValidationErrorItem;
	const message = typeof msg === 'string' ? msg.trim() : '';
	if (!message) return '';
	const field = fieldFromLoc(loc);
	return field ? `${field}: ${message}` : message;
}

/**
 * Turn whatever FastAPI put in `detail` into one readable sentence.
 *
 * - `string` → itself
 * - validation-error list → `"field: msg; field: msg"`
 * - `{msg}` / `{message}` → that text
 * - anything else (missing, empty, unrecognised shape) → `fallback`
 *
 * Deliberately lossy: it flattens to a string because that is what an `Error`
 * message is. A caller that needs the STRUCTURE (e.g. the expense-report submit
 * flow, which renders a policy-violation panel) reads the raw body itself.
 */
export function formatApiDetail(detail: unknown, fallback: string): string {
	if (typeof detail === 'string') {
		const trimmed = detail.trim();
		if (trimmed) return trimmed;
		return fallback;
	}

	if (Array.isArray(detail)) {
		const parts = detail.map(renderItem).filter(Boolean);
		if (parts.length) return parts.join('; ');
		return fallback;
	}

	if (detail && typeof detail === 'object') {
		const obj = detail as { msg?: unknown; message?: unknown };
		const single = typeof obj.msg === 'string' ? obj.msg : obj.message;
		if (typeof single === 'string' && single.trim()) return single.trim();
	}

	return fallback;
}

/** The machine-readable half of a coded refusal. */
export interface ApiErrorCode {
	/** Stable identifier the client keys a translated sentence on, or `null`
	 *  when the detail carried none (every plain-string detail). */
	code: string | null;
	/** Typed values the translated sentence needs. Empty when there are none. */
	params: Record<string, unknown>;
}

/**
 * Read the `code` + `params` out of a coded refusal's `detail`.
 *
 * The backend's coded shape is `{code, message, params}` (`api/auth.py::
 * coded_refusal`): `message` is the English fallback `formatApiDetail` already
 * renders, and `code` is what lets a page say the same thing in the reader's
 * language instead (`frontend/CLAUDE.md` § Internationalization). Anything
 * else — a string, a validation list, an object with no string `code` — yields
 * `{code: null, params: {}}`, so a caller can always fall back to the message.
 * A non-object `params` is dropped rather than trusted.
 */
export function apiErrorCode(detail: unknown): ApiErrorCode {
	if (!detail || typeof detail !== 'object' || Array.isArray(detail)) {
		return { code: null, params: {} };
	}
	const { code, params } = detail as { code?: unknown; params?: unknown };
	if (typeof code !== 'string' || !code) return { code: null, params: {} };
	const safeParams =
		params && typeof params === 'object' && !Array.isArray(params)
			? (params as Record<string, unknown>)
			: {};
	return { code, params: safeParams };
}
