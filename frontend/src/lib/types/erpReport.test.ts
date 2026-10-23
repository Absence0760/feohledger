import { describe, expect, it } from 'vitest';
import { erpPrefill, isErpPaidReport } from './erpReport.ts';

describe('isErpPaidReport', () => {
	it('is true only for a paid_not_recorded report', () => {
		expect(isErpPaidReport({ kind: 'paid_not_recorded' })).toBe(true);
		expect(isErpPaidReport({ kind: 'settled_short' })).toBe(false);
		expect(isErpPaidReport(null)).toBe(false);
		expect(isErpPaidReport(undefined)).toBe(false);
	});
});

describe('erpPrefill', () => {
	const today = '2026-10-22';

	it('takes the rail, reference and date the ERP reported', () => {
		expect(
			erpPrefill({ method: 'check', reference: ' BILL-145 ', paid_on: '2026-10-20' }, today)
		).toEqual({ method: 'check', reference: 'BILL-145', paidOn: '2026-10-20' });
	});

	it('leaves the method blank when the ERP named none, instead of guessing ACH', () => {
		expect(erpPrefill({ method: null, reference: 'X', paid_on: today }, today).method).toBe('');
	});

	it('never pre-fills a rail that cannot be recorded outside FeohLedger', () => {
		expect(erpPrefill({ method: 'virtual_card', reference: 'X', paid_on: today }, today).method).toBe(
			''
		);
		expect(erpPrefill({ method: 'barter', reference: 'X', paid_on: today }, today).method).toBe('');
	});

	it('falls back to today for a missing, malformed or future date', () => {
		expect(erpPrefill({ paid_on: null }, today).paidOn).toBe(today);
		expect(erpPrefill({ paid_on: 'yesterday' }, today).paidOn).toBe(today);
		expect(erpPrefill({ paid_on: '2026-10-30' }, today).paidOn).toBe(today);
	});

	it('starts empty with no report at all', () => {
		expect(erpPrefill(null, today)).toEqual({ method: '', reference: '', paidOn: today });
	});
});
