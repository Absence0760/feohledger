import { describe, it, expect } from 'vitest';
import { resolveOrgCurrency, resolveReportingCurrency } from './reportingCurrency';
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

describe('resolveOrgCurrency', () => {
	it('prefers the server-resolved field over the client-side rungs', () => {
		// The follow-up this closes: the backend KNOWS the answer (all four
		// rungs, including the operator default no client can read) and used to
		// serve only the settings a client could partially re-derive from.
		expect(
			resolveOrgCurrency({
				resolved_reporting_currency: 'JPY',
				settings: {
					reporting_currency: 'GBP',
					payments: { home_currency: 'EUR' },
					invoice_defaults: { currency: 'USD' }
				}
			})
		).toBe('JPY');
	});

	it('normalises the server field the same way as the client rungs', () => {
		expect(resolveOrgCurrency({ resolved_reporting_currency: ' chf ' })).toBe('CHF');
	});

	it('falls back to the three-rung client resolution when the server field is absent', () => {
		// An older / cached backend response that predates this field.
		expect(
			resolveOrgCurrency({
				settings: { payments: { home_currency: 'EUR' } }
			})
		).toBe('EUR');
	});

	it('falls back when the server field is present but malformed', () => {
		expect(
			resolveOrgCurrency({
				resolved_reporting_currency: '',
				settings: { invoice_defaults: { currency: 'ZAR' } }
			})
		).toBe('ZAR');
	});

	it('returns null only when the server field AND every fallback rung miss', () => {
		expect(resolveOrgCurrency(null)).toBeNull();
		expect(resolveOrgCurrency(undefined)).toBeNull();
		expect(resolveOrgCurrency({})).toBeNull();
		expect(resolveOrgCurrency({ resolved_reporting_currency: null, settings: {} })).toBeNull();
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
		expect(code).toContain('this.currency = resolveOrgCurrency(');
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
