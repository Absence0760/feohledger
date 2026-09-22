import { describe, it, expect } from 'vitest';
import {
	formatCurrencyTotals,
	groupAmountsByCurrency,
	spansMultipleCurrencies
} from './currencyGroups';

describe('groupAmountsByCurrency', () => {
	it('returns [] for an empty selection', () => {
		expect(groupAmountsByCurrency([])).toEqual([]);
	});

	it('collapses a single-currency selection into one exact subtotal', () => {
		expect(
			groupAmountsByCurrency([
				{ amount: '250.00', currency: 'USD' },
				{ amount: '250.00', currency: 'USD' }
			])
		).toEqual([{ currency: 'USD', total: 500, count: 2 }]);
	});

	it('NEVER adds across currencies — one bucket per currency', () => {
		// The bug this helper exists to prevent: EUR 100 + USD 100 rendering
		// as a single "200".
		const groups = groupAmountsByCurrency([
			{ amount: '100.00', currency: 'USD' },
			{ amount: '100.00', currency: 'EUR' }
		]);
		expect(groups).toEqual([
			{ currency: 'EUR', total: 100, count: 1 },
			{ currency: 'USD', total: 100, count: 1 }
		]);
		// And no caller can accidentally read a combined figure off it.
		expect(groups.length).toBe(2);
	});

	it('sums within a currency exactly (no float drift)', () => {
		// The classic 0.1 + 0.2 case: a float reduce yields 0.30000000000000004.
		const [group] = groupAmountsByCurrency([
			{ amount: '0.10', currency: 'GBP' },
			{ amount: '0.20', currency: 'GBP' }
		]);
		expect(group.total).toBe(0.3);
	});

	it('normalises currency codes (case + whitespace) into one bucket', () => {
		expect(
			groupAmountsByCurrency([
				{ amount: '10.00', currency: 'usd' },
				{ amount: '10.00', currency: '  USD ' }
			])
		).toEqual([{ currency: 'USD', total: 20, count: 2 }]);
	});

	it('keeps a missing / malformed code as ONE group of its own rather than dropping the row', () => {
		// Dropping the row would understate the selection — the exact failure
		// mode the helper exists to prevent. Every shape of "no code" lands in
		// the same `null` group, so an unknown row is counted exactly once.
		expect(
			groupAmountsByCurrency([
				{ amount: '5.00', currency: null },
				{ amount: '5.00' },
				{ amount: '5.00', currency: 'US' },
				{ amount: '5.00', currency: '' }
			])
		).toEqual([{ currency: null, total: 20, count: 4 }]);
	});

	it('NEVER files an unknown-currency row under a real currency (decisions §200)', () => {
		// The bug: the org's code was the fallback, so a row nobody could put a
		// currency on was ADDED to real org-currency money — here EUR 100 + an
		// unknown 40 would have read as one "€140.00".
		const groups = groupAmountsByCurrency([
			{ amount: '100.00', currency: 'EUR' },
			{ amount: '40.00', currency: null }
		]);
		expect(groups).toEqual([
			{ currency: 'EUR', total: 100, count: 1 },
			{ currency: null, total: 40, count: 1 }
		]);
		expect(groups.find((g) => g.currency === 'EUR')?.total).toBe(100);
	});

	it('orders groups by currency code ascending, the unknown group last', () => {
		const codes = groupAmountsByCurrency([
			{ amount: '3', currency: null },
			{ amount: '1', currency: 'ZAR' },
			{ amount: '9999', currency: 'AUD' },
			{ amount: '5', currency: 'GBP' }
		]).map((g) => g.currency);
		expect(codes).toEqual(['AUD', 'GBP', 'ZAR', null]);
	});

	it('counts non-numeric / null amounts as members but not as value', () => {
		// `sumMoney` skips unparseable entries; the row is still part of the
		// selection, so `count` must include it.
		expect(
			groupAmountsByCurrency([
				{ amount: '10.00', currency: 'USD' },
				{ amount: null, currency: 'USD' }
			])
		).toEqual([{ currency: 'USD', total: 10, count: 2 }]);
	});
});

describe('spansMultipleCurrencies', () => {
	it('is false for nothing selected and for one currency', () => {
		expect(spansMultipleCurrencies([])).toBe(false);
		expect(spansMultipleCurrencies(groupAmountsByCurrency([{ amount: '1', currency: 'USD' }]))).toBe(
			false
		);
	});

	it('is true the moment a second currency joins the selection', () => {
		expect(
			spansMultipleCurrencies(
				groupAmountsByCurrency([
					{ amount: '1', currency: 'USD' },
					{ amount: '1', currency: 'EUR' }
				])
			)
		).toBe(true);
	});

	it('counts an unknown-currency row as a currency of its own', () => {
		// Nothing proves it is the same currency as the row beside it, so the
		// selection is reported as mixed rather than let through to a run that
		// may refuse it.
		expect(
			spansMultipleCurrencies(
				groupAmountsByCurrency([
					{ amount: '1', currency: 'USD' },
					{ amount: '1', currency: null }
				])
			)
		).toBe(true);
	});
});

describe('formatCurrencyTotals', () => {
	it('returns [] for nothing, leaving the "no total" reading to the caller', () => {
		expect(formatCurrencyTotals([])).toEqual([]);
	});

	it('formats an exact decimal STRING without routing it through a float', () => {
		// The shape `GET /api/expenses/summary` sends. 30.30 is exactly the sum
		// of 10.10 + 20.20 — a float reduce would have produced 30.299999….
		expect(formatCurrencyTotals([{ currency: 'USD', total: '30.30' }])).toEqual(['$30.30']);
	});

	it('renders EACH currency in its own — never one combined figure', () => {
		// The bug: EUR 5.05 + USD 30.30 shown as a single "$35.35".
		const out = formatCurrencyTotals([
			{ currency: 'EUR', total: '5.05' },
			{ currency: 'USD', total: '30.30' }
		]);
		expect(out).toHaveLength(2);
		expect(out[0]).toContain('5.05');
		expect(out[1]).toBe('$30.30');
		expect(out.join(' · ')).not.toBe('$35.35');
	});

	it('renders a total with no usable code BARE, never under a borrowed one', () => {
		// `api/bank_reconciliation.py::_currency_totals` reports an unestablished
		// currency under `""` so it is "not folded into another currency's
		// figure". Labelling it with the org's code folded it back in on screen.
		expect(formatCurrencyTotals([{ currency: '', total: '1.00' }])).toEqual(['1.00']);
		expect(formatCurrencyTotals([{ currency: null, total: '1.00' }])).toEqual(['1.00']);
		expect(formatCurrencyTotals([{ total: '1.00' }])).toEqual(['1.00']);
	});

	it('keeps a backend "" total beside a real one, each in its own rendering', () => {
		const out = formatCurrencyTotals([
			{ currency: '', total: '12.50' },
			{ currency: 'EUR', total: '100.00' }
		]);
		expect(out).toHaveLength(2);
		expect(out[0]).toBe('12.50');
		expect(out[0]).not.toMatch(/[$€£¥]|[A-Z]{3}/);
		expect(out[1]).toContain('€');
	});

	it('renders a mixed [EUR row, null row] selection as two figures, the unknown one bare', () => {
		const out = formatCurrencyTotals(
			groupAmountsByCurrency([
				{ amount: '100.00', currency: 'EUR' },
				{ amount: '40.00', currency: null }
			])
		);
		expect(out).toEqual([expect.stringContaining('€'), '40.00']);
		expect(out[0]).toContain('100.00');
		expect(out.join(' · ')).not.toContain('140');
	});

	it('preserves input order rather than re-sorting', () => {
		// The server already orders `by_currency`; re-sorting here would make the
		// headline figure on a KPI card jump between renders.
		const out = formatCurrencyTotals([
			{ currency: 'USD', total: '2.00' },
			{ currency: 'EUR', total: '1.00' }
		]);
		expect(out[0]).toBe('$2.00');
	});

	it('accepts a locally-computed CurrencyGroup unchanged', () => {
		const groups = groupAmountsByCurrency([
			{ amount: '10.00', currency: 'USD' },
			{ amount: '5.00', currency: 'USD' }
		]);
		expect(formatCurrencyTotals(groups)).toEqual(['$15.00']);
	});
});
