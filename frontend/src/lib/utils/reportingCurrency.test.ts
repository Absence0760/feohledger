import { describe, it, expect } from 'vitest';
import { resolveReportingCurrency } from './reportingCurrency';
// The store is a `.svelte.ts` rune module (and reaches `$env` through
// `$lib/api`), so it cannot be imported under this plain-Node config. Its
// contract is small enough to pin from source instead.
import storeSource from '../stores/orgSettings.svelte.ts?raw';

describe('resolveReportingCurrency', () => {
	it('prefers the explicit reporting currency over every other key', () => {
		// The bug: the store read only `invoice_defaults.currency`, so this org's
		// GBP-denominated rollups were rendered with a `$`.
		expect(
			resolveReportingCurrency({
				reporting_currency: 'GBP',
				payments: { home_currency: 'EUR' },
				invoice_defaults: { currency: 'USD' }
			})
		).toBe('GBP');
	});

	it('falls to the payments home currency when no reporting currency is set', () => {
		expect(
			resolveReportingCurrency({
				payments: { home_currency: 'EUR' },
				invoice_defaults: { currency: 'USD' }
			})
		).toBe('EUR');
	});

	it('falls to the invoice default last — the key the store used to read first', () => {
		expect(resolveReportingCurrency({ invoice_defaults: { currency: 'ZAR' } })).toBe('ZAR');
	});

	it('returns null when the org declares nothing usable — the store keeps that null', () => {
		expect(resolveReportingCurrency(null)).toBeNull();
		expect(resolveReportingCurrency(undefined)).toBeNull();
		expect(resolveReportingCurrency({})).toBeNull();
		expect(resolveReportingCurrency({ reporting_currency: null, payments: null })).toBeNull();
	});

	it('skips a malformed code rather than letting it win the resolution', () => {
		// A blank or wrong-length value must not shadow the next candidate —
		// otherwise a half-saved settings blob silently downgrades the label.
		expect(
			resolveReportingCurrency({
				reporting_currency: '  ',
				payments: { home_currency: 'US' },
				invoice_defaults: { currency: 'gbp' }
			})
		).toBe('GBP');
	});

	it('normalises to upper case and trims', () => {
		expect(resolveReportingCurrency({ reporting_currency: ' eur ' })).toBe('EUR');
	});
});

describe('orgCurrency store — an unresolved currency stays null', () => {
	// Code only: the store's docstring names the old `USD` fallback on purpose,
	// to say why it is gone.
	const code = storeSource
		.replace(/\/\*[\s\S]*?\*\//g, ' ')
		.replace(/(^|[^:])\/\/.*$/gm, '$1');

	it('starts at null, never at a platform default', () => {
		// The bug (decisions §119, §200): it started at `DEFAULT_CURRENCY`, so
		// every figure labelled from it wore a `$` before the store had loaded,
		// and for any org whose settings resolve nothing.
		expect(code).toMatch(/currency = \$state<string \| null>\(null\)/);
	});

	it('assigns the resolver result as-is, null included', () => {
		// `if (ccy) this.currency = ccy` kept whatever was there before when the
		// resolver abstained — the same guess, just older.
		expect(code).toContain('this.currency = resolveReportingCurrency(');
		expect(code).not.toMatch(/if\s*\(\s*ccy\s*\)/);
	});

	it('resets to null', () => {
		expect(code).toMatch(/reset\(\): void \{\s*this\.currency = null;/);
	});

	it('never names a platform default or a currency literal', () => {
		// The store's job is to report what resolved. A FORM that needs a value
		// takes `orgCurrency.currency ?? DEFAULT_CURRENCY` itself.
		expect(code).not.toContain('DEFAULT_CURRENCY');
		expect(code).not.toMatch(/'[A-Z]{3}'/);
	});
});
