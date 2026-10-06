/**
 * Localizing the GL-chart refusal — the client half of
 * `backend/app/services/gl_chart.py::ChartRefusal.body`.
 *
 * Every invoice GL write (create, PATCH, line items, approve-with-corrections,
 * a recurring template, a CSV import row) refuses a code that is not an active
 * account of the invoice's own chart (decisions §194 / §199). The backend sends
 * that refusal STRUCTURED — a stable `code`, the refused codes by reason
 * (`foreign` / `retired` / `unknown`), whether they were line-item codes, and
 * the English `message` — so the sentence can be stated in the reader's
 * language instead of rendering server English inside a translated frame
 * (`frontend/CLAUDE.md` § Internationalization).
 *
 * The same object arrives two ways: as the 422 `detail` (handled at the
 * transport by `localizeApiDetail`, so every toast on every write path gets it
 * without a per-site change) and as a CSV import's per-row error, which is that
 * body plus `row`.
 *
 * Safe to render on skew, in both directions: a body this build does not
 * recognise — another code, a missing or non-string-array bucket, every bucket
 * empty — parses to `null`, and the caller renders the server's own `message`.
 *
 * Deliberately imports NOTHING from the Svelte runtime: the caller passes `m`
 * in, so this stays unit-testable under vitest's node environment. Same
 * arrangement as `invoiceWarnings.ts`.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';

/** The backend's `gl_chart.GL_CODES_OUTSIDE_CHART`. */
export const GL_CODES_OUTSIDE_CHART = 'gl_codes_outside_chart';

export interface GlChartRefusal {
	code: typeof GL_CODES_OUTSIDE_CHART;
	/** The codes were line items' codes, not the invoice's header field. */
	on_lines: boolean;
	/** Codes that belong to another entity's chart (§194). */
	foreign: string[];
	/** Codes of a retired account in the invoice's own chart (§199). */
	retired: string[];
	/** Codes in no chart this invoice can see (§199). */
	unknown: string[];
	/** The server's English sentence — the fallback. */
	message: string;
}

/** `m` from `#lib/i18n/store.svelte`, passed in so this module stays pure. */
export type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

function stringArray(v: unknown): string[] | null {
	return Array.isArray(v) && v.every((c) => typeof c === 'string') ? (v as string[]) : null;
}

/**
 * The refusal, if `detail` is one this build can state — otherwise `null`.
 * Never throws: anything else (a string detail, a validation list, a body a
 * newer backend reshaped) is simply not this refusal.
 */
export function parseGlChartRefusal(detail: unknown): GlChartRefusal | null {
	if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null;
	const d = detail as Record<string, unknown>;
	if (d.code !== GL_CODES_OUTSIDE_CHART) return null;
	const foreign = stringArray(d.foreign);
	const retired = stringArray(d.retired);
	const unknown = stringArray(d.unknown);
	if (!foreign || !retired || !unknown) return null;
	if (foreign.length + retired.length + unknown.length === 0) return null;
	return {
		code: GL_CODES_OUTSIDE_CHART,
		on_lines: d.on_lines === true,
		foreign,
		retired,
		unknown,
		message: typeof d.message === 'string' ? d.message : ''
	};
}

/**
 * The codes as one list, each quoted as the server quotes them
 * (`'6800'`) — a code is chart configuration the reader has to find, so it is
 * shown verbatim inside the translated sentence.
 */
function codeList(codes: string[]): string {
	// A literal separator on purpose: these are identifiers to find in the
	// chart, not prose — the case `utils/list.ts` says keeps its `', '`. The
	// mobile half joins the same way.
	return codes.map((c) => `'${c}'`).join(', ');
}

/** The refusal stated in the reader's language. */
export function localizeGlChartRefusal(refusal: GlChartRefusal, m: Translate): string {
	const parts: string[] = [];
	const reasons: [string[], MessageKey][] = [
		[refusal.foreign, 'glChartRefusal.foreign'],
		[refusal.retired, 'glChartRefusal.retired'],
		[refusal.unknown, 'glChartRefusal.unknown']
	];
	for (const [codes, key] of reasons) {
		if (codes.length) parts.push(m(key, { n: codes.length, codes: codeList(codes) }));
	}
	// Only a non-empty active chart refuses a retired or unknown code, so
	// "an active code" is always something the reader can actually pick — the
	// backend's own rule for the same sentence.
	parts.push(
		m(
			refusal.retired.length || refusal.unknown.length
				? 'glChartRefusal.pickActive'
				: 'glChartRefusal.pick'
		)
	);
	const sentence = parts.join(' ');
	return refusal.on_lines ? m('glChartRefusal.onLines', { reasons: sentence }) : sentence;
}

/**
 * The localized sentence for a structured error body this build recognises —
 * a 422 `detail` or a CSV row error — or `null`, in which case the caller
 * renders the server's text. The one entry point `api.ts` and the CSV import
 * modal call, so a second localized refusal joins here rather than at every
 * call site.
 */
export function localizeApiDetail(detail: unknown, m: Translate): string | null {
	const refusal = parseGlChartRefusal(detail);
	return refusal ? localizeGlChartRefusal(refusal, m) : null;
}
