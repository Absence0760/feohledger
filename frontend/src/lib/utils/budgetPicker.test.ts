import { describe, expect, it } from 'vitest';
import type { BudgetCheck, BudgetSpend } from '#lib/types/budget.ts';
import {
	budgetCheckDisplay,
	budgetCheckView,
	budgetHeadroomView,
	budgetOptionLabel,
	sameCurrency,
	type BudgetCheckAnswer
} from './budgetPicker';

const check = (over: Partial<BudgetCheck> = {}): BudgetCheck => ({
	budget_id: 'b1',
	amount: '600.00',
	allocated: '1000.00',
	committed: '0',
	actual: '500.00',
	remaining: '500.00',
	remaining_after: '-100.00',
	would_overspend: true,
	currency: 'USD',
	unverifiable_po_count: 0,
	...over
});

const ready = (result: BudgetCheck, amount = '600.00', budgetId = 'b1'): BudgetCheckAnswer => ({
	budgetId,
	amount,
	status: 'ready',
	result
});

const base = { budgetId: 'b1', budgetCurrency: 'USD', currency: 'USD', amount: '600.00' };

describe('budgetOptionLabel', () => {
	it('names the period beside the budget, since names repeat across periods', () => {
		expect(
			budgetOptionLabel({ id: '1', name: 'Marketing', dimension_value: 'Mkt', period: '2026', currency: 'USD' })
		).toBe('Marketing (2026)');
	});

	it('omits an absent or blank period', () => {
		expect(
			budgetOptionLabel({ id: '1', name: 'Marketing', dimension_value: 'Mkt', period: null, currency: 'USD' })
		).toBe('Marketing');
		expect(
			budgetOptionLabel({ id: '1', name: 'Marketing', dimension_value: 'Mkt', period: '  ', currency: 'USD' })
		).toBe('Marketing');
	});
});

describe('sameCurrency', () => {
	it('compares codes case-insensitively and treats a missing code as no match', () => {
		expect(sameCurrency('usd', 'USD ')).toBe(true);
		expect(sameCurrency('EUR', 'USD')).toBe(false);
		expect(sameCurrency(null, 'USD')).toBe(false);
		expect(sameCurrency('', '')).toBe(false);
	});
});

describe('budgetCheckView', () => {
	it('says nothing when no budget is chosen', () => {
		expect(budgetCheckView({ ...base, budgetId: '', answer: null })).toEqual({ kind: 'none' });
	});

	it('refuses to compare a budget in another currency, whatever the check said', () => {
		// The answer is a real overspend in EUR — it must not reach the USD
		// requisition as a verdict at face value.
		const view = budgetCheckView({
			...base,
			budgetCurrency: 'eur',
			answer: ready(check({ currency: 'EUR' }))
		});
		expect(view).toEqual({ kind: 'mismatch', budgetCurrency: 'EUR' });
	});

	it('catches the mismatch from the check itself when the budget currency is not yet known', () => {
		const view = budgetCheckView({
			...base,
			budgetCurrency: null,
			answer: ready(check({ currency: 'GBP', would_overspend: false }))
		});
		expect(view).toEqual({ kind: 'mismatch', budgetCurrency: 'GBP' });
	});

	it('reads as checking until the answer for THIS budget and THIS total lands', () => {
		expect(budgetCheckView({ ...base, answer: null })).toEqual({ kind: 'checking' });
		expect(
			budgetCheckView({ ...base, answer: { budgetId: 'b1', amount: '600.00', status: 'loading', result: null } })
		).toEqual({ kind: 'checking' });
		// A stale answer for an earlier total is not this total's answer.
		expect(budgetCheckView({ ...base, answer: ready(check(), '500.00') })).toEqual({ kind: 'checking' });
		// Nor is an answer for a budget the user has since replaced.
		expect(budgetCheckView({ ...base, answer: ready(check(), '600.00', 'b0') })).toEqual({
			kind: 'checking'
		});
	});

	it('reports a failed check as failed, never as within budget', () => {
		expect(
			budgetCheckView({ ...base, answer: { budgetId: 'b1', amount: '600.00', status: 'failed', result: null } })
		).toEqual({ kind: 'failed' });
	});

	it("reads the backend's would_overspend verdict as given", () => {
		const over = check();
		expect(budgetCheckView({ ...base, answer: ready(over) })).toEqual({ kind: 'overspend', check: over });
		const within = check({ remaining_after: '400.00', would_overspend: false, amount: '100.00' });
		expect(budgetCheckView({ ...base, amount: '100.00', answer: ready(within, '100.00') })).toEqual({
			kind: 'within',
			check: within
		});
	});
});

describe('budgetCheckDisplay', () => {
	it('shows a settled verdict as not busy', () => {
		const d = budgetCheckDisplay({ ...base, answer: ready(check()) });
		expect(d).toEqual({ view: { kind: 'overspend', check: check() }, busy: false });
	});

	it('holds the last settled verdict for this budget, busy, while the new total is checked', () => {
		// The answer is for 600.00; the form now totals 50.00 and is re-checking.
		const d = budgetCheckDisplay({ ...base, amount: '50.00', answer: ready(check()) });
		expect(d.busy).toBe(true);
		expect(d.view.kind).toBe('overspend');
	});

	it('holds a failed check too, rather than flipping to checking', () => {
		const d = budgetCheckDisplay({
			...base,
			amount: '50.00',
			answer: { budgetId: 'b1', amount: '600.00', status: 'failed', result: null }
		});
		expect(d).toEqual({ view: { kind: 'failed' }, busy: true });
	});

	it("never holds another budget's verdict", () => {
		const d = budgetCheckDisplay({ ...base, budgetId: 'b2', answer: ready(check()) });
		expect(d).toEqual({ view: { kind: 'checking' }, busy: true });
	});

	it('is checking with nothing to hold before the first answer', () => {
		expect(budgetCheckDisplay({ ...base, answer: null })).toEqual({
			view: { kind: 'checking' },
			busy: true
		});
	});

	it('never holds a verdict across a currency change', () => {
		const d = budgetCheckDisplay({
			...base,
			budgetCurrency: null,
			currency: 'EUR',
			amount: '50.00',
			answer: ready(check())
		});
		expect(d.view.kind).toBe('mismatch');
	});
});

describe('budgetHeadroomView', () => {
	const spend = (over: Partial<BudgetSpend> = {}): BudgetSpend => ({
		budget_id: 'b1',
		name: 'Ops',
		dimension: 'department',
		dimension_value: 'Ops',
		currency: 'USD',
		allocated: 100,
		committed: 150,
		actual: 0,
		remaining: -50,
		utilization_pct: 150,
		excluded_row_count: 0,
		unverifiable_po_count: 0,
		...over
	});

	it('says nothing until the spend has loaded', () => {
		expect(budgetHeadroomView(null, 'USD')).toEqual({ kind: 'none' });
	});

	it("reads over / within from the sign of the backend's remaining", () => {
		expect(budgetHeadroomView(spend(), 'USD').kind).toBe('over');
		expect(budgetHeadroomView(spend({ remaining: 0 }), 'USD').kind).toBe('within');
		expect(budgetHeadroomView(spend({ remaining: 25 }), 'usd').kind).toBe('within');
	});

	it('says nothing for a budget in another currency, whose figure would not include this requisition', () => {
		expect(budgetHeadroomView(spend(), 'EUR')).toEqual({ kind: 'none' });
	});
});
