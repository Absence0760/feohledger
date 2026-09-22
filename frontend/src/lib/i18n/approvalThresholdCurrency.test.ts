import { describe, test, expect } from 'vitest';
import { interpolate } from './interpolate';
import { SUPPORTED_LOCALES } from './locale';
import { CATALOGUE_LOADERS } from './catalogues';
import builderSource from '../../routes/workflows/[id]/+page.svelte?raw';
import organizationSource from '../../routes/organization/+page.svelte?raw';
import matrixEditorSource from '../components/modals/ApprovalMatrixEditor.svelte?raw';

/**
 * The approval money thresholds are denominated in the org's REPORTING
 * currency, and the UI has to say which one.
 *
 * `auto_approve_below`, `require_cfo_above`, `max_invoice_amount`, the approval
 * matrix's per-level `min_amount` / `max_amount` bands and
 * `payments.cfo_approval_above` are bare numbers on a JSONB config. The backend
 * converts each invoice into the org's reporting currency before comparing them
 * (`approval_chain.reporting_gate_amount` / `payment_controls.cfo_approval_decision`).
 *
 * Every one of these labels used to read `($)`, hardcoded, in all six locales —
 * so an admin on a GBP-reporting tenant typing `10000` into "Require CFO
 * approval above ($)" had no way to know whether that meant pounds or dollars.
 * Before the backend change the number was merely ambiguous; now it has a
 * definite meaning the operator could not see, which is worse.
 *
 * The code is resolved at runtime from `orgCurrency` and interpolated into the
 * LABEL as `{currency}`, via `orgCurrency.label`: the resolved code, or — when
 * nothing the client can read resolves it — the localized noun "reporting
 * currency". Hardcoding a default here would be the same defect one layer up,
 * and so would the store's own old `USD` fallback: the backend's last rung
 * (`settings.reporting_currency_default`) is invisible to a client, so a
 * guessed code is indistinguishable from a configured one (decisions §119,
 * §200).
 *
 * The HINTS beneath three of those fields, and the matrix hint, name the
 * CONCEPT instead ("your organisation's reporting currency") and carry no
 * `{currency}`: the code is on the label directly above them, and a sentence
 * built as "converted to {currency}, your organisation's reporting currency"
 * has no honest reading once the code is the noun itself.
 *
 * Fails pre-fix: every asserted message contained `($)` and no `{currency}`.
 */

/** Labels whose money field is in the reporting currency — each names it. */
const DENOMINATED_KEYS = [
	'approvalMatrix.maxAmount',
	'approvalMatrix.minAmount',
	'org.payments.cfoThreshold',
	'workflows.builder.approval.autoApproveBelow',
	'workflows.builder.approval.maxInvoiceAmount',
	'workflows.builder.approval.requireCfoAbove',
] as const;

/** The hints beneath those fields: they name the concept, never a code. */
const CONCEPT_HINT_KEYS = [
	'workflows.builder.approval.autoApproveBelowHint',
	'workflows.builder.approval.matrixHint',
	'workflows.builder.approval.maxInvoiceAmountHint',
	'workflows.builder.approval.requireCfoAboveHint',
] as const;

/** A currency sign — `＄` / `（$）` full-width forms count too (ja). */
const HARDCODED_SIGN = /[$＄€£¥]/;

/**
 * Every place one of those messages is rendered, as raw source. `?raw` (rather
 * than `node:fs`) keeps this runnable under the plain-Node vitest config
 * without pulling `@types/node` into `svelte-check`'s program.
 */
const RENDER_SITES: Record<string, string> = {
	'workflows/[id]/+page.svelte': builderSource,
	'organization/+page.svelte': organizationSource,
	'ApprovalMatrixEditor.svelte': matrixEditorSource,
};

describe('approval thresholds name their currency', () => {
	for (const loc of SUPPORTED_LOCALES) {
		test(`${loc}: every denominated label carries {currency} and no hardcoded sign`, async () => {
			const dict = (await CATALOGUE_LOADERS[loc]()) as Record<string, string>;
			for (const key of DENOMINATED_KEYS) {
				const value = dict[key];
				expect(value, `${loc} is missing ${key}`).toBeTruthy();
				expect(value, `${loc}/${key} does not name its currency`).toContain('{currency}');
				// A hardcoded sign is exactly the bug: it asserts a denomination
				// nobody resolved.
				expect(value, `${loc}/${key} hardcodes a currency sign`).not.toMatch(HARDCODED_SIGN);
			}
		});

		test(`${loc}: every hint names the concept, not a code or a sign`, async () => {
			const dict = (await CATALOGUE_LOADERS[loc]()) as Record<string, string>;
			for (const key of CONCEPT_HINT_KEYS) {
				const value = dict[key];
				expect(value, `${loc} is missing ${key}`).toBeTruthy();
				expect(value, `${loc}/${key} still interpolates a code`).not.toContain('{currency}');
				expect(value, `${loc}/${key} hardcodes a currency sign`).not.toMatch(HARDCODED_SIGN);
			}
		});

		test(`${loc}: the unresolved-currency noun is a word, not a code`, async () => {
			const dict = (await CATALOGUE_LOADERS[loc]()) as Record<string, string>;
			const noun = dict['common.reportingCurrencyUnresolved'];
			expect(noun, `${loc} is missing the noun`).toBeTruthy();
			// Not a three-letter code, and no sign — it names the concept so the
			// label never claims a denomination nobody resolved.
			expect(noun).not.toMatch(/^[A-Z]{3}$/);
			expect(noun).not.toMatch(HARDCODED_SIGN);
		});
	}

	test('the code is substituted, not left as a literal placeholder', () => {
		expect(interpolate('Require CFO approval above ({currency})', { currency: 'GBP' })).toBe(
			'Require CFO approval above (GBP)',
		);
		// A GBP-reporting tenant never sees a dollar sign on these fields.
		expect(interpolate('Min amount ({currency})', { currency: 'JPY' })).toBe('Min amount (JPY)');
		// An unresolved org sees the concept, never `(USD)`.
		expect(interpolate('Min amount ({currency})', { currency: 'reporting currency' })).toBe(
			'Min amount (reporting currency)',
		);
	});

	for (const [rel, src] of Object.entries(RENDER_SITES)) {
		test(`${rel}: resolves the label from orgCurrency.label, never a code or a default`, () => {
			// Every m('<denominated key>' … call in this file passes `currency`,
			// and passes the LABEL form: `orgCurrency.currency` is `string | null`
			// (the type refuses it here anyway), and a `?? 'USD'` beside it would
			// be the guessed code this test exists to keep out.
			for (const key of DENOMINATED_KEYS) {
				const calls = [...src.matchAll(new RegExp(`m\\('${key.replace('.', '\\.')}'([^)]*)\\)`, 'g'))];
				for (const call of calls) {
					expect(call[1], `${rel} renders ${key} without the label param`).toContain(
						'currency: orgCurrency.label',
					);
				}
			}
			for (const key of CONCEPT_HINT_KEYS) {
				const calls = [...src.matchAll(new RegExp(`m\\('${key.replace('.', '\\.')}'([^)]*)\\)`, 'g'))];
				for (const call of calls) {
					expect(call[1], `${rel} passes ${key} a currency it no longer reads`).not.toContain(
						'currency',
					);
				}
			}
			// …and the store is actually loaded, so the label isn't stuck on the
			// unresolved noun for the life of the page.
			if (src.includes('orgCurrency.label')) {
				expect(src, `${rel} never calls orgCurrency.ensureLoaded()`).toContain(
					'orgCurrency.ensureLoaded()',
				);
			}
		});
	}
});
