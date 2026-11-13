import { describe, expect, it } from 'vitest';
import { COMMON_CURRENCIES } from './money.ts';

/**
 * No form enters a currency through a `<select>` over a shortlist.
 *
 * The backend accepts any ISO 4217 code, so a picker that can only offer the
 * common sixteen (or a hardcoded six) makes a PLN, ZAR or HKD record
 * impossible to enter from the web: the statement uploads had that defect
 * (decisions §377), then the credit-memo form and the organization's invoice
 * default currency (decisions §381). `ui/CurrencyInput.svelte` is the field;
 * the shortlist is its suggestions, never its limit.
 *
 * This scans every component and route for the two shapes those pickers took:
 * options built from `currencyOptions(` / `COMMON_CURRENCIES`, and hardcoded
 * `<option value="USD">` lists. A select that only DISPLAYS a currency (the
 * credit-memo dialog's locked, linked-invoice currency is one disabled option
 * holding the invoice's code) matches neither.
 */
const sources = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const SHORTLIST_IN_MARKUP = /\bcurrencyOptions\(|\bCOMMON_CURRENCIES\b/;
// Any common currency code as a literal option value. Built from the shortlist
// rather than `[A-Z]{3}`, which also matches an HTTP method picker's "GET".
const HARDCODED_CURRENCY_OPTION = new RegExp(
	`<option\\s+value="(${COMMON_CURRENCIES.join('|')})"`
);

describe('currency entry fields', () => {
	it('scans the Svelte sources (guard against a glob that matches nothing)', () => {
		expect(Object.keys(sources).length).toBeGreaterThan(50);
		expect(Object.keys(sources)).toContain('/src/routes/credit-memos/+page.svelte');
	});

	it('no Svelte file builds a currency picker from the shortlist', () => {
		const offenders = Object.entries(sources)
			.filter(([, src]) => SHORTLIST_IN_MARKUP.test(src))
			.map(([path]) => path);
		expect(offenders).toEqual([]);
	});

	it('no Svelte file hardcodes a list of currency options', () => {
		const offenders = Object.entries(sources)
			.filter(([, src]) => HARDCODED_CURRENCY_OPTION.test(src))
			.map(([path]) => path);
		expect(offenders).toEqual([]);
	});

	it('the credit-memo form and the invoice default currency use CurrencyInput', () => {
		for (const path of [
			'/src/routes/credit-memos/+page.svelte',
			'/src/routes/organization/+page.svelte',
			'/src/routes/bank-reconciliation/ImportStatementModal.svelte',
			'/src/lib/components/modals/VendorStatementReconModal.svelte'
		]) {
			expect(sources[path], path).toMatch(/<CurrencyInput\b/);
		}
	});
});
