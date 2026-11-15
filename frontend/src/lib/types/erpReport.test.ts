import { describe, expect, it } from 'vitest';
import {
	erpPaymentParts,
	erpPrefill,
	erpRefusalNote,
	isErpPaidReport,
	isPoRematchClears,
	poRematchNote,
	spansSeveralRails,
	splitNoteKey
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

describe('erpPaymentParts', () => {
	it('keeps each part and its exact amount string, untouched', () => {
		expect(
			erpPaymentParts({
				payment_parts: [
					{ method: 'check', amount: '150.00' },
					{ method: 'card', amount: '100.10' }
				]
			})
		).toEqual([
			{ rail: 'check', amount: '150.00' },
			{ rail: 'card', amount: '100.10' }
		]);
	});

	it('reads a rail that cannot be recorded outside FeohLedger as not stated, as the server books it', () => {
		const parts = erpPaymentParts({
			payment_parts: [
				{ method: null, amount: '1.00' },
				{ method: 'virtual_card', amount: '2.00' },
				{ method: 'barter', amount: '3.00' },
				{ method: ' CHECK ', amount: '4.00' }
			]
		});
		expect(parts.map((p) => p.rail)).toEqual([null, null, null, 'check']);
	});

	it('reads a malformed breakdown as none, never as part of one', () => {
		expect(erpPaymentParts({ payment_parts: [{ method: 'check', amount: '1.00' }, { method: 'card' }] })).toEqual([]);
		expect(
			erpPaymentParts({
				payment_parts: [{ method: 'card', amount: 5 as unknown as string }]
			})
		).toEqual([]);
		expect(erpPaymentParts({ payment_parts: null })).toEqual([]);
		expect(erpPaymentParts({})).toEqual([]);
		expect(erpPaymentParts(null)).toEqual([]);
	});
});

describe('spansSeveralRails', () => {
	it('counts rails only, a part with none stated as a rail of its own', () => {
		expect(spansSeveralRails([{ rail: 'check', amount: '1.00' }, { rail: 'card', amount: '1.00' }])).toBe(true);
		expect(spansSeveralRails([{ rail: 'check', amount: '1.00' }, { rail: null, amount: '1.00' }])).toBe(true);
		expect(spansSeveralRails([{ rail: 'card', amount: '1.00' }, { rail: 'card', amount: '2.00' }])).toBe(false);
		expect(spansSeveralRails([{ rail: null, amount: '1.00' }])).toBe(false);
		expect(spansSeveralRails([])).toBe(false);
	});
});

describe('splitNoteKey', () => {
	it('reads the server verdict in three states, never guessing a judgement', () => {
		expect(splitNoteKey(true)).toBe('exceptions.erpPayment.splitStands');
		expect(splitNoteKey(false)).toBe('exceptions.erpPayment.splitDropped');
		// Not judged: only the record decides; never "does not add up".
		expect(splitNoteKey(null)).toBe('exceptions.erpPayment.splitUnjudged');
		expect(splitNoteKey(undefined)).toBe('exceptions.erpPayment.splitUnjudged');
	});
});

describe('the payment queue row reads its parts like the exception row', () => {
	it('takes the same breakdown from the queue payload', () => {
		const queued = {
			exception_id: 'e1',
			erp_name: 'QuickBooks Online',
			method: null,
			reference: '501',
			paid_on: '2026-11-15',
			amount: '250.00',
			amount_currency: 'USD',
			payment_parts: [
				{ method: 'check', amount: '150.00' },
				{ method: 'card', amount: '100.00' }
			],
			split_stands: true
		};
		const parts = erpPaymentParts(queued);
		expect(parts).toEqual([
			{ rail: 'check', amount: '150.00' },
			{ rail: 'card', amount: '100.00' }
		]);
		expect(spansSeveralRails(parts)).toBe(true);
		expect(erpPaymentParts({ ...queued, payment_parts: null })).toEqual([]);
	});
});
