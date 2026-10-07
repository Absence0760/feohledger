import { describe, expect, it } from 'vitest';
import { en } from '#lib/i18n/locales/en.ts';
import { PAYMENT_METHODS } from './payment';
import {
	EXTERNAL_PAYMENT_METHODS,
	NACHA_FIELDS,
	NACHA_FIELD_LABEL_KEYS,
	NACHA_SEC_CODES,
	canDispatchPayments,
	canDownloadBankFile,
	isBusinessDay,
	isExternalPayment,
	isValidAbaRouting,
	localToday,
	nachaFieldLabelKey,
	nachaForSave,
	nachaProblems,
	nextBusinessDay,
	utcToday
} from './paymentExecution';

/**
 * The no-rail pilot's client-side mirror of the backend's execution-mode and
 * NACHA rules (issue #517). The backend enforces every one of these; what is
 * pinned here is that the UI never OFFERS something the server would refuse,
 * and that it fails closed when it does not know.
 */

const RAW = import.meta.glob(
	[
		'../../../../backend/app/services/nacha.py',
		'../../../../backend/app/schemas/payment.py'
	],
	{ query: '?raw', import: 'default', eager: true }
) as Record<string, string>;

function source(suffix: string): string {
	const hit = Object.entries(RAW).find(([path]) => path.endsWith(suffix));
	expect(hit, `${suffix} not found through import.meta.glob`).toBeDefined();
	return hit![1];
}

describe('execution mode', () => {
	it('offers send controls only on a confirmed processor tenant (fail closed)', () => {
		expect(canDispatchPayments('processor')).toBe(true);
		expect(canDispatchPayments('record_only')).toBe(false);
		// Not loaded yet, the read failed, or a value this build predates.
		expect(canDispatchPayments(null)).toBe(false);
		expect(canDispatchPayments(undefined)).toBe(false);
		expect(canDispatchPayments('something_new')).toBe(false);
	});

	it('offers the bank file only on a confirmed record-only tenant', () => {
		// The server refuses it anywhere else (`nacha_requires_record_only`):
		// on a processor tenant the file plus an Execute would pay twice.
		expect(canDownloadBankFile('record_only')).toBe(true);
		expect(canDownloadBankFile('processor')).toBe(false);
		expect(canDownloadBankFile(null)).toBe(false);
	});

	it('recognises a payment recorded outside FeohLedger by its provider', () => {
		expect(isExternalPayment({ provider: 'external' })).toBe(true);
		expect(isExternalPayment({ provider: 'modern_treasury' })).toBe(false);
		expect(isExternalPayment({ provider: null })).toBe(false);
		expect(isExternalPayment({})).toBe(false);
	});
});

describe('the rails a payment made outside can be recorded on', () => {
	it('is every rail except the virtual card', () => {
		expect([...EXTERNAL_PAYMENT_METHODS].sort()).toEqual(
			PAYMENT_METHODS.filter((m) => m !== 'virtual_card').sort()
		);
	});

	it('matches the backend schema refusing virtual_card', () => {
		const py = source('schemas/payment.py');
		expect(py).toMatch(/class RecordPaymentOutsideRequest/);
		expect(py).toMatch(/if v == PaymentMethod\.virtual_card:/);
	});
});

describe('dates', () => {
	it('defaults the NACHA effective date to the next weekday', () => {
		// Local-calendar dates; months are 0-based.
		expect(nextBusinessDay(new Date(2026, 9, 7, 15, 0))).toBe('2026-10-08'); // Wed → Thu
		expect(nextBusinessDay(new Date(2026, 9, 9, 9, 0))).toBe('2026-10-12'); // Fri → Mon
		expect(nextBusinessDay(new Date(2026, 9, 10, 9, 0))).toBe('2026-10-12'); // Sat → Mon
		expect(nextBusinessDay(new Date(2026, 9, 11, 23, 59))).toBe('2026-10-12'); // Sun → Mon
		expect(nextBusinessDay(new Date(2026, 11, 31, 12, 0))).toBe('2027-01-01'); // year end
	});

	it('states today on the reader\'s own calendar for the paid-on limit', () => {
		// Local-calendar fields, so this holds in whatever TZ the suite runs.
		expect(localToday(new Date(2026, 9, 6, 23, 30))).toBe('2026-10-06');
		expect(localToday(new Date(2027, 0, 1, 0, 5))).toBe('2027-01-01');
	});

	it('knows a weekend effective date is not a business day', () => {
		expect(isBusinessDay('2026-10-09')).toBe(true); // Friday
		expect(isBusinessDay('2026-10-10')).toBe(false); // Saturday
		expect(isBusinessDay('2026-10-11')).toBe(false); // Sunday
		expect(isBusinessDay('2026-10-12')).toBe(true); // Monday
		expect(isBusinessDay('')).toBe(false);
		expect(isBusinessDay('2026-02-30')).toBe(false);
		expect(isBusinessDay('10/12/2026')).toBe(false);
	});

	it('states today in UTC, the floor of a NACHA effective date', () => {
		expect(utcToday(new Date(Date.UTC(2026, 9, 6, 23, 30)))).toBe('2026-10-06');
		expect(utcToday(new Date(Date.UTC(2026, 0, 1, 0, 0)))).toBe('2026-01-01');
	});
});

describe('NACHA settings', () => {
	it('uses the SEC codes the backend writes', () => {
		const py = source('services/nacha.py');
		const m = /^SEC_CODES: tuple\[str, \.\.\.\] = \(([^)]*)\)$/m.exec(py);
		expect(m, 'SEC_CODES not found in services/nacha.py').not.toBeNull();
		const codes = [...m![1].matchAll(/"([A-Z]+)"/g)].map((x) => x[1]);
		expect([...NACHA_SEC_CODES]).toEqual(codes);
	});

	it('names every field the backend can report as missing', () => {
		// `originator_problems` appends field-name literals; each must have a
		// label, or `nacha_not_configured` would print a raw key.
		const py = source('services/nacha.py');
		const reported = [...py.matchAll(/problems\.append\("([a-z_]+)"\)/g)].map((x) => x[1]);
		expect(reported.length).toBeGreaterThan(2);
		expect([...NACHA_FIELDS].sort()).toEqual([...new Set(reported)].sort());
		for (const field of NACHA_FIELDS) {
			expect(en[NACHA_FIELD_LABEL_KEYS[field]], field).toBeTruthy();
		}
		expect(nachaFieldLabelKey('company_name')).toBe('org.payments.nachaField.companyName');
		expect(nachaFieldLabelKey('nope')).toBeNull();
	});

	it('checks the ABA routing checksum', () => {
		expect(isValidAbaRouting('021000021')).toBe(true);
		expect(isValidAbaRouting('011000015')).toBe(true);
		expect(isValidAbaRouting('021000022')).toBe(false); // bad check digit
		expect(isValidAbaRouting('02100002')).toBe(false); // 8 digits
		expect(isValidAbaRouting('02100002a')).toBe(false);
	});

	it('mirrors originator_problems', () => {
		const good = { company_name: 'ACME CORP', company_id: '1123456789', odfi_routing: '021000021' };
		expect(nachaProblems(good)).toEqual([]);
		expect(nachaProblems({ ...good, bank_name: 'FIRST NATIONAL BANK' })).toEqual([]);
		expect(nachaProblems({ ...good, company_name: '   ' })).toEqual(['company_name']);
		expect(nachaProblems({ ...good, company_name: 'A'.repeat(17) })).toEqual(['company_name']);
		expect(nachaProblems({ ...good, company_id: '123' })).toEqual(['company_id']);
		expect(nachaProblems({ ...good, company_id: '112345678é' })).toEqual(['company_id']);
		expect(nachaProblems({ ...good, odfi_routing: '123456789' })).toEqual(['odfi_routing']);
		expect(nachaProblems({ ...good, bank_name: 'B'.repeat(24) })).toEqual(['bank_name']);
		expect(nachaProblems({})).toEqual(['company_name', 'company_id', 'odfi_routing']);
	});

	it('saves a blank form as no bank file, and trims what it keeps', () => {
		expect(
			nachaForSave({ company_name: ' ', company_id: '', odfi_routing: '', bank_name: '' })
		).toBeNull();
		expect(
			nachaForSave({
				company_name: ' ACME ',
				company_id: '1123456789',
				odfi_routing: '0210 00021',
				bank_name: ''
			})
		).toEqual({ company_name: 'ACME', company_id: '1123456789', odfi_routing: '021000021' });
		expect(
			nachaForSave({
				company_name: 'ACME',
				company_id: '1123456789',
				odfi_routing: '021000021',
				bank_name: ' CHASE '
			})
		).toEqual({
			company_name: 'ACME',
			company_id: '1123456789',
			odfi_routing: '021000021',
			bank_name: 'CHASE'
		});
	});
});
