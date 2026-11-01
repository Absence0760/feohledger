import { describe, expect, it } from 'vitest';
import {
	erpPrefill,
	erpRefusalNote,
	isErpPaidReport,
	isPoRematchClears,
	poRematchNote
} from './erpReport.ts';

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

describe('erpRefusalNote', () => {
	const t = (key: string, params?: Record<string, string | number>) =>
		`${key}${params ? ' ' + JSON.stringify(params) : ''}`;
	const refusal = { code: 'payment_account_not_mapped', params: { provider: 'QuickBooks Online' } };

	it('states the coded refusal of a failed payment write-back or void', () => {
		for (const kind of ['payment_writeback_failed', 'payment_void_writeback_failed']) {
			expect(erpRefusalNote({ kind, refusal }, t)).toBe(
				'erpRefusal.paymentAccountNotMapped {"provider":"QuickBooks Online"}'
			);
		}
	});

	it('is null with no refusal, an unknown code, or any other row', () => {
		expect(erpRefusalNote({ kind: 'payment_writeback_failed', refusal: null }, t)).toBeNull();
		expect(
			erpRefusalNote({ kind: 'payment_writeback_failed', refusal: { code: 'newer_code' } }, t)
		).toBeNull();
		expect(erpRefusalNote({ kind: 'payment_sync_back_failed', refusal }, t)).toBeNull();
		expect(erpRefusalNote(null, t)).toBeNull();
	});
});

describe('poRematchNote', () => {
	const t = (key: string, params?: Record<string, string | number>) =>
		`${key}${params ? ' ' + JSON.stringify(params) : ''}`;
	const date = (day: string) => `<${day}>`;

	it('names the PO, the ERP and the localised day', () => {
		const report = { kind: 'po_rematch_clears', po_number: 'PO-7', observed_on: '2026-10-26' };
		expect(isPoRematchClears(report)).toBe(true);
		expect(poRematchNote(report, 'NetSuite', t, date)).toBe(
			'exceptions.poRematch.note {"po":"PO-7","erp":"NetSuite","date":"<2026-10-26>"}'
		);
	});

	it('says what changed without inventing a missing PO number or day', () => {
		expect(poRematchNote({ kind: 'po_rematch_clears', po_number: 'PO-7' }, 'X', t, date)).toBe(
			'exceptions.poRematch.noteNoDate {"po":"PO-7","erp":"X"}'
		);
		expect(
			poRematchNote({ kind: 'po_rematch_clears', po_number: ' ', observed_on: 'bad' }, 'X', t, date)
		).toBe('exceptions.poRematch.noteNoPo {"erp":"X"}');
	});

	it('is null for any other report', () => {
		expect(poRematchNote({ kind: 'paid_not_recorded' }, 'X', t, date)).toBeNull();
		expect(poRematchNote(null, 'X', t, date)).toBeNull();
		expect(isPoRematchClears(undefined)).toBe(false);
	});
});
