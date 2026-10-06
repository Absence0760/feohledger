/**
 * Localizing an exception's description — the queue's half of
 * `backend/app/services/invoice_warning_catalog.py`.
 *
 * Since migration 0103 a detector-raised exception carries
 * `description_code` + `description_params` beside its English `description`.
 * The code is a catalogue code — usually the very warning the invoice carries
 * (so the queue and the invoice state one finding in one wording), otherwise an
 * `exception.*` sentence no warning states — and one reader,
 * `localizeInvoiceWarning`, renders all of them.
 *
 * A COMPOSITE description (several flagged price-variance lines, several
 * breached contract terms) is a frame code whose params carry the findings
 * themselves as a list under `findings`. The backend used to join them into
 * one English string; now each is localized here and the caller lists them.
 *
 * Fallback rules, in order:
 *
 * - **No code** (a human-authored rejection reason, or any row raised before
 *   0103 — nothing backfills them) → the `description` as written.
 * - **A code this build cannot render** (unknown, or a parameter missing) →
 *   the whole `description`, with NO separate findings: the server's fallback
 *   for a composite already contains every finding, so listing them as well
 *   would say each one twice.
 * - **A finding inside a renderable composite that this build cannot render**
 *   → that finding's own English `message`, beside its localized siblings.
 *
 * Pure (the caller passes `m` in), so vitest can drive it under node — the
 * same arrangement as `invoiceWarnings.ts`.
 */
import {
	type LocalizableWarning,
	type Translate,
	invoiceWarningText,
	localizeInvoiceWarning
} from '#lib/api/invoiceWarnings.ts';

/** The reserved params key a composite description lists its findings under. */
export const FINDINGS_PARAM = 'findings';

/** The fields this module reads off an exception row. */
export interface ExceptionDescriptionSource {
	description: string | null;
	description_code?: string | null;
	description_params?: Record<string, unknown> | null;
}

/** What to render: the sentence, plus any findings to list beneath it. */
export interface ExceptionDescriptionText {
	summary: string;
	findings: string[];
}

function scalarParams(raw: Record<string, unknown>): Record<string, string | number> {
	const out: Record<string, string | number> = {};
	for (const [name, value] of Object.entries(raw)) {
		if (typeof value === 'string' || typeof value === 'number') out[name] = value;
	}
	return out;
}

function asFinding(raw: unknown): LocalizableWarning | null {
	if (!raw || typeof raw !== 'object') return null;
	const f = raw as Record<string, unknown>;
	if (typeof f.message !== 'string') return null;
	return {
		message: f.message,
		code: typeof f.code === 'string' ? f.code : null,
		params:
			f.params && typeof f.params === 'object'
				? scalarParams(f.params as Record<string, unknown>)
				: null
	};
}

/**
 * The description to show for one exception, or `null` when it has none.
 * See the module note for the fallback order.
 */
export function exceptionDescriptionText(
	exc: ExceptionDescriptionSource,
	translate: Translate
): ExceptionDescriptionText | null {
	const fallback = exc.description ?? '';
	if (!exc.description_code) {
		return fallback ? { summary: fallback, findings: [] } : null;
	}
	const raw = exc.description_params ?? {};
	const localized = localizeInvoiceWarning({
		message: fallback,
		code: exc.description_code,
		params: scalarParams(raw)
	});
	if (!localized) {
		return fallback ? { summary: fallback, findings: [] } : null;
	}
	const list = raw[FINDINGS_PARAM];
	const findings = Array.isArray(list)
		? list
				.map(asFinding)
				.filter((f): f is LocalizableWarning => f !== null)
				.map((f) => invoiceWarningText(f, translate))
		: [];
	return { summary: translate(localized.key, localized.params), findings };
}

/**
 * One string for a surface that can hold only one (a `title` tooltip): the
 * summary and each finding on its own line. A newline is a separator no
 * language owns, which is why it beats joining localized sentences with `; `.
 */
export function exceptionDescriptionTitle(text: ExceptionDescriptionText): string {
	return [text.summary, ...text.findings].join('\n');
}
