/**
 * Parsing + localizing the per-field reasons an e-invoice dialect refused an
 * invoice (the HTTP 422 body of `GET /api/invoices/{id}/einvoice` and of
 * `POST .../peppol-send`).
 *
 * Deliberately imports NOTHING beyond the generated catalogue and a type: the
 * feature module `einvoice.ts` pulls `#lib/api` → `$app/env/public`, which
 * the node-environment `vitest.config.ts` does not alias, so the logic worth
 * unit-testing lives here instead. Same arrangement as `hostRouting.ts` under
 * `tenant.ts`.
 */
import { E_INVOICE_RULE_MESSAGE_KEYS } from '#lib/api/einvoiceRuleMessages.generated.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

export interface EInvoiceValidationIssue {
	/** Dotted field path, e.g. `seller.tax_id`, `lines`, `taxes[0].rate`. */
	field: string;
	/** The backend's own PII-free sentence for this field. Where the failure is
	 *  an EN 16931 / PEPPOL rule the message leads with the rule id
	 *  (`BR-CO-25: …`) — the identifier a receiving Access Point's validator
	 *  names. This is the fallback rendering; prefer the localized sentence
	 *  `einvoiceRuleMessageKey` resolves when it can. */
	message: string;
	/** The EN 16931 / PEPPOL rule id, when the message led with one. `null` for
	 *  a generic kind (`missing` / `malformed` / …), which the 422 body does not
	 *  fold into `msg` and no client can therefore identify — see
	 *  `E_INVOICE_OPAQUE_CODES`. */
	code: string | null;
}

// The client half of `validate.is_rule_id`: upper-case and carrying a letter.
// `error_payload` folds exactly those codes into `msg` as a `"<CODE>: "` prefix
// (decisions.md §95 — the rule id rides on two channels because a client that
// flattens the payload keeps only `loc` and `msg`), so this is how the code
// gets back out.
const RULE_ID = /^[A-Z0-9][A-Z0-9-]*$/;

function ruleIdFrom(message: string): string | null {
	const at = message.indexOf(': ');
	if (at < 0) return null;
	const candidate = message.slice(0, at);
	if (!RULE_ID.test(candidate) || !/[A-Z]/.test(candidate)) return null;
	return candidate;
}

/**
 * The message key that states this issue in the reader's language, or `null`
 * when there isn't one — then the caller renders `issue.message`, the server's
 * own sentence, which already leads with the rule id.
 *
 * The map is GENERATED from the backend's rule set
 * (`einvoiceRuleMessages.generated.ts`), so it cannot fall behind the
 * validator the way the hand-written table §95 deleted did. Lookup stays
 * tolerant on purpose: a code the running backend emits but this build's
 * catalogue predates degrades to the raw sentence, never to a blank row.
 */
export function einvoiceRuleMessageKey(issue: EInvoiceValidationIssue): MessageKey | null {
	if (!issue.code) return null;
	const keys: Record<string, MessageKey> = E_INVOICE_RULE_MESSAGE_KEYS;
	return keys[issue.code] ?? null;
}

// A field path is a dotted/indexed identifier — anything else means the detail
// is not the validation rendering (a different 4xx, a proxy's HTML error page),
// and the caller must render it verbatim instead of pretending it parsed.
const FIELD_PATH = /^[A-Za-z0-9_]+(?:\[\d+\])?(?:\.[A-Za-z0-9_]+(?:\[\d+\])?)*$/;

/**
 * Split the rendered 422 detail into its per-field parts so the UI can list
 * *why* the dialect refused the invoice, one row per field.
 *
 * The backend returns the errors STRUCTURED — `[{loc, type, msg}]` — and the
 * shared `formatApiDetail` (in `utils/apiError.ts`, which every response in the
 * app already goes through) flattens exactly that shape to
 * `"issue_date: Issue date is required; lines: At least one invoice line is
 * required"`. `ApiError` carries only a message, so this re-splits that one
 * rendering rather than duplicating the transport — including the rule id the
 * backend folds into `msg` for exactly this reason, which `einvoiceRuleMessageKey`
 * then turns into a localized sentence.
 *
 * Returns `[]` when the string is not that shape — the caller then shows the
 * raw message. Never throws, never guesses: a partially-parseable detail is
 * treated as unparseable, because dropping half the reasons would be worse
 * than showing the backend's own wording.
 */
export function parseEInvoiceIssues(detail: string): EInvoiceValidationIssue[] {
	// `formatApiDetail` joins with exactly `'; '`. Splitting on the bare `;`
	// would cut a message that legitimately contains one.
	const parts = detail
		.split('; ')
		.map((p) => p.trim())
		.filter(Boolean);
	if (parts.length === 0) return [];
	const issues: EInvoiceValidationIssue[] = [];
	for (const part of parts) {
		// First `': '` only: a rule-id message carries its own colon
		// (`due_date: BR-CO-25: an invoice needs a due date`).
		const at = part.indexOf(': ');
		if (at < 0) return [];
		const field = part.slice(0, at).trim();
		const message = part.slice(at + 2).trim();
		if (!FIELD_PATH.test(field) || !message) return [];
		issues.push({ field, message, code: ruleIdFrom(message) });
	}
	return issues;
}
