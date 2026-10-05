import { describe, expect, it } from 'vitest';

import { formatUtilization, overBudgetCount } from './budgetRollupSummary';
import type { BudgetCurrencyRollup, BudgetRollup } from '#lib/types/budget.ts';

function row(over: Partial<BudgetCurrencyRollup> = {}): BudgetCurrencyRollup {
	return {
		currency: 'USD',
		budget_count: 1,
		allocated: '1000.00',
		committed: '0.00',
		actual: '0.00',
		remaining: '1000.00',
		utilization_pct: '0.00',
		over_budget_count: 0,
		excluded_row_count: 0,
		...over
	};
}

function rollup(rows: BudgetCurrencyRollup[]): BudgetRollup {
	return {
		budget_count: rows.reduce((n, r) => n + r.budget_count, 0),
		by_currency: rows,
		excluded_row_count: rows.reduce((n, r) => n + r.excluded_row_count, 0),
		insufficient_data: rows.length === 0
	};
}

describe('overBudgetCount', () => {
	it('is 0 for a missing rollup', () => {
		expect(overBudgetCount(null)).toBe(0);
	});

	it('is 0 when nothing is over', () => {
		expect(overBudgetCount(rollup([row(), row({ currency: 'EUR' })]))).toBe(0);
	});

	it('folds the per-currency counts', () => {
		// A COUNT may cross currencies; the amounts beside it may not.
		expect(
			overBudgetCount(
				rollup([row({ over_budget_count: 2 }), row({ currency: 'EUR', over_budget_count: 1 })])
			)
		).toBe(3);
	});
});

describe('formatUtilization', () => {
	it('renders a percentage', () => {
		expect(formatUtilization('12.00')).toBe('12.00%');
	});

	it('returns null for null — never "0%"', () => {
		// "0% of the budget is used" and "there is no budget to use" are opposite
		// facts; the caller renders its own not-applicable state.
		expect(formatUtilization(null)).toBeNull();
		expect(formatUtilization(undefined)).toBeNull();
	});

	it('keeps a genuine zero', () => {
		expect(formatUtilization('0.00')).toBe('0.00%');
	});

	it('refuses a blank or unreadable figure rather than rendering NaN%', () => {
		expect(formatUtilization('   ')).toBeNull();
		expect(formatUtilization('n/a')).toBeNull();
	});
});
