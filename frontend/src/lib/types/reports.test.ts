import { describe, expect, it } from 'vitest';

import { formatMoney } from '#lib/utils/money.ts';
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import { interpolate } from '#lib/i18n/interpolate.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

import {
	AGG_LABEL_KEYS,
	REPORT_FIELD_LABEL_KEYS,
	REPORT_SOURCE_LABEL_KEYS,
	cellCurrency,
	fieldLabel,
	measureLabel,
	resultColumnLabel,
	sourceLabel,
	type ResultColumn
} from './reports';

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

/**
 * The Report Builder renders catalog sources and fields by catalog KEY, in the
 * reader's language, rather than the server's English `label` (decisions
 * §360). This reads the backend catalog itself, so a field added there without
 * a label here fails — and the English value must equal the server's label, so
 * the English UI did not change.
 */
const BACKEND = import.meta.glob('../../../../backend/app/services/report_builder.py', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;
const SOURCE = Object.values(BACKEND)[0] ?? '';

function backendFields(): Map<string, Set<string>> {
	const out = new Map<string, Set<string>>();
	for (const [, key, label] of SOURCE.matchAll(/(?:Dimension|Measure|FilterDef)\(\s*"([a-z_]+)",\s*"([^"]+)"/g)) {
		if (!out.has(key)) out.set(key, new Set());
		out.get(key)!.add(label);
	}
	return out;
}

function backendSources(): Map<string, string> {
	return new Map(
		[...SOURCE.matchAll(/_source\(\s*"([a-z_]+)",\s*"([^"]+)"/g)].map((x) => [x[1], x[2]] as [string, string])
	);
}

const en = (key: MessageKey, params?: Record<string, string | number>) =>
	params ? interpolate(EN_ALL[key], params, 'en') : EN_ALL[key];

describe('report catalog labels', () => {
	it('reads the backend catalog', () => {
		expect(backendFields().size).toBeGreaterThan(20);
		expect([...backendSources().keys()].sort()).toEqual(['expenses', 'invoices', 'payments', 'vendors']);
	});

	it('labels every backend field key, and a key means one thing in every source', () => {
		for (const [key, labels] of backendFields()) {
			expect(labels.size, `${key} is labelled differently across sources: ${[...labels]}`).toBe(1);
			expect(REPORT_FIELD_LABEL_KEYS, `${key} has no label key`).toHaveProperty(key);
			expect(en(REPORT_FIELD_LABEL_KEYS[key]), key).toBe([...labels][0]);
		}
		expect(Object.keys(REPORT_FIELD_LABEL_KEYS).sort()).toEqual([...backendFields().keys()].sort());
	});

	it('labels every data source with the server’s English', () => {
		for (const [key, label] of backendSources()) {
			expect(en(REPORT_SOURCE_LABEL_KEYS[key]), key).toBe(label);
		}
	});

	it('falls back to the server label for a key this build predates', () => {
		expect(fieldLabel(en, 'brand_new_field', 'Brand New')).toBe('Brand New');
		expect(sourceLabel(en, 'brand_new_source', 'Brand New Source')).toBe('Brand New Source');
		expect(fieldLabel(en, 'vendor_name', 'ignored')).toBe('Vendor');
	});
});

describe('measure labels', () => {
	it('follows the server rule: a row count is "Count", anything else "<Agg> of <field>"', () => {
		// backend report_builder._measure_label
		expect(measureLabel(en, 'id', 'count')).toBe('Count');
		expect(measureLabel(en, 'amount', 'sum')).toBe('Sum of Amount');
		expect(measureLabel(en, 'risk_score', 'avg')).toBe('Average of Risk Score');
		expect(Object.keys(AGG_LABEL_KEYS).sort()).toEqual(['avg', 'count', 'max', 'min', 'sum']);
	});

	it('names result columns from their keys', () => {
		const col = (key: string, kind: ResultColumn['kind'], label: string): ResultColumn => ({ key, kind, label });
		expect(resultColumnLabel(en, col('vendor_name', 'dimension', 'Vendor'))).toBe('Vendor');
		expect(resultColumnLabel(en, col('amount_sum', 'measure', 'Sum of Amount'))).toBe('Sum of Amount');
		expect(resultColumnLabel(en, col('tax_amount_max', 'measure', 'Max of Tax'))).toBe('Max of Tax');
		expect(resultColumnLabel(en, col('id_count', 'measure', 'Count'))).toBe('Count');
		// A shape this build cannot read keeps the server's label.
		expect(resultColumnLabel(en, col('mystery', 'measure', 'Server Label'))).toBe('Server Label');
		expect(resultColumnLabel(en, col('amount_median', 'measure', 'Median of Amount'))).toBe('Median of Amount');
	});
});
