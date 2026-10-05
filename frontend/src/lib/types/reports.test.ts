import { describe, expect, it } from 'vitest';

import { formatMoney } from '$lib/utils/money';

import { cellCurrency, type ResultColumn } from './reports';

const AMOUNT: ResultColumn = {
	key: 'amount_sum',
	label: 'Sum of Amount',
	kind: 'measure',
	type: 'money',
	currency_key: 'currency'
};

describe('cellCurrency', () => {
	it("labels each row's figure with that row's own currency", () => {
		const usd = { vendor_name: 'Acme', currency: 'USD', amount_sum: '100.00' };
		const eur = { vendor_name: 'Acme', currency: 'EUR', amount_sum: '50.00' };
		expect(cellCurrency(AMOUNT, usd)).toBe('USD');
		expect(cellCurrency(AMOUNT, eur)).toBe('EUR');
		// Each figure wears its own symbol.
		expect(formatMoney(usd.amount_sum, { currency: cellCurrency(AMOUNT, usd) })).toContain('$');
		expect(formatMoney(eur.amount_sum, { currency: cellCurrency(AMOUNT, eur) })).toContain('€');
	});

	it('abstains (null) when the measure names no currency column', () => {
		const col: ResultColumn = { key: 'amount_sum', label: 'x', kind: 'measure', type: 'money' };
		expect(cellCurrency(col, { amount_sum: '1.00', currency: 'USD' })).toBeNull();
	});

	it('abstains (null) when the row carries no code, rather than guessing', () => {
		expect(cellCurrency(AMOUNT, { amount_sum: '1.00', currency: null })).toBeNull();
		expect(cellCurrency(AMOUNT, { amount_sum: '1.00', currency: '' })).toBeNull();
		expect(cellCurrency(AMOUNT, { amount_sum: '1.00' })).toBeNull();
	});
});
