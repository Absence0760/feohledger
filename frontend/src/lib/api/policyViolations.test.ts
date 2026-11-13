/**
 * Each expense policy violation is stated from its `{code, params}` in every
 * shipped locale, and anything this build cannot state completely falls back
 * to the server's English `message` (decisions §378).
 */
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it } from 'vitest';
import { en } from '../i18n/locales/en';
import { messages as de } from '../i18n/locales/de';
import { messages as fr } from '../i18n/locales/fr';
import { messages as es } from '../i18n/locales/es';
import { messages as ptBR } from '../i18n/locales/pt-BR';
import { messages as ja } from '../i18n/locales/ja';
import { interpolate } from '../i18n/interpolate';
import { setActiveFormatLocale } from '../i18n/formatLocale';
import type { MainMessageKey, MessageKey } from '../i18n/messages';
import type { PolicyViolation } from '../types/expense';
import { formatMoney } from '../utils/money';
import { POLICY_VIOLATION_CODES, policyViolationText } from './policyViolations';

const translator =
	(messages: Record<string, string>, locale: string) =>
	(key: MessageKey, params?: Record<string, string | number>) =>
		interpolate(messages[key], params, locale);

const LOCALES = [
	['en', en],
	['de', de],
	['fr', fr],
	['es', es],
	['pt-BR', ptBR],
	['ja', ja]
] as const;

const enT = translator(en, 'en');

afterEach(() => setActiveFormatLocale(undefined));

/** Shaped exactly as `expense_policy._violation` builds it. */
function violation(code: string, params: Record<string, unknown> | undefined): PolicyViolation {
	return { code, message: 'server English', params };
}

const RESOLVED = {
	amount: '75.00',
	amount_currency: 'USD',
	limit: '50.00',
	currency: 'USD',
	comparison: 'resolved'
};
const UNRESOLVED = {
	amount: '10000.00',
	amount_currency: 'JPY',
	limit: '50.00',
	currency: 'USD',
	comparison: 'unresolved'
};
const MILEAGE = { miles: '120.00', rate: '0.6700' };

/** Every (code, params) → key the module must state, one row per sentence. */
const CASES: [string, Record<string, unknown>, MainMessageKey][] = [
	['category_limit', { ...RESOLVED, category: 'travel' }, 'expenses.violation.categoryLimit'],
	['category_limit', { ...RESOLVED, category: null }, 'expenses.violation.categoryLimitUncategorised'],
	['category_limit', { ...UNRESOLVED, category: 'travel' }, 'expenses.violation.categoryLimitUnresolved'],
	[
		'category_limit',
		{ ...UNRESOLVED, category: null },
		'expenses.violation.categoryLimitUncategorisedUnresolved'
	],
	['receipt_required', RESOLVED, 'expenses.violation.receiptRequired'],
	['receipt_required', UNRESOLVED, 'expenses.violation.receiptRequiredUnresolved'],
	['preapproval_required', RESOLVED, 'expenses.violation.preapprovalRequired'],
	['preapproval_required', UNRESOLVED, 'expenses.violation.preapprovalRequiredUnresolved'],
	['per_diem_exceeded', RESOLVED, 'expenses.violation.perDiemExceeded'],
	['per_diem_exceeded', UNRESOLVED, 'expenses.violation.perDiemExceededUnresolved'],
	[
		'mileage_amount_mismatch',
		{ ...RESOLVED, ...MILEAGE, direction: 'over' },
		'expenses.violation.mileageOver'
	],
	[
		'mileage_amount_mismatch',
		{ ...RESOLVED, ...MILEAGE, direction: 'under' },
		'expenses.violation.mileageUnder'
	],
	[
		'mileage_amount_mismatch',
		{ ...UNRESOLVED, ...MILEAGE, direction: null },
		'expenses.violation.mileageUnresolved'
	]
];

describe('the catalogue pins the backend', () => {
	it('states exactly the codes the policy engine defines', () => {
		const engine = readFileSync(
			new URL('../../../../backend/app/services/expense_policy.py', import.meta.url).pathname,
			'utf8'
		);
		const codes = [...engine.matchAll(/^VIOLATION_[A-Z_]+ = "([a-z_]+)"$/gm)].map((x) => x[1]);
		expect(codes.length).toBe(5);
		expect([...POLICY_VIOLATION_CODES].sort()).toEqual([...codes].sort());
	});
});

describe('policyViolationText', () => {
	it.each(CASES)('%s with %j is the sentence %s', (code, params, key) => {
		const p = params as Record<string, string>;
		const expected = interpolate(
			en[key],
			{
				amount: formatMoney(p.amount, { currency: p.amount_currency }),
				limit: formatMoney(p.limit, { currency: p.currency }),
				currency: p.currency,
				category: p.category ?? '',
				miles: p.miles ?? '',
				rate: p.rate ?? ''
			},
			'en'
		);
		expect(policyViolationText(violation(code, params), enT)).toBe(expected);
	});

	it.each(CASES)('%s with %j fills every placeholder in every locale', (code, params) => {
		for (const [locale, messages] of LOCALES) {
			setActiveFormatLocale(locale);
			const text = policyViolationText(
				violation(code, params),
				translator(messages as Record<string, string>, locale)
			);
			expect(text, `${locale} ${code}`).not.toBe('server English');
			expect(text, `${locale} ${code}`).not.toMatch(/[{}]/);
			if (typeof params.category === 'string') expect(text).toContain(params.category);
			if (typeof params.miles === 'string') {
				// Named exactly as sent — never rounded to a currency's places.
				expect(text).toContain('120.00');
				expect(text).toContain('0.6700');
			}
		}
	});

	it('states the money in the reader’s format, in the currency each figure is in', () => {
		setActiveFormatLocale('de');
		const text = policyViolationText(
			violation('category_limit', {
				...UNRESOLVED,
				amount: '1250.50',
				amount_currency: 'EUR',
				category: null
			}),
			translator(de as Record<string, string>, 'de')
		);
		expect(text).toContain(formatMoney('1250.50', { currency: 'EUR', locale: 'de' }));
		expect(text).toContain(formatMoney('50.00', { currency: 'USD', locale: 'de' }));
		expect(text).not.toContain('1250.50');
	});

	it.each([
		['a code this build predates', 'from_a_newer_backend', RESOLVED],
		['a row stored before params existed', 'receipt_required', undefined],
		['a float amount', 'receipt_required', { ...RESOLVED, amount: 75 }],
		['a malformed currency', 'receipt_required', { ...RESOLVED, currency: 'dollars' }],
		['no comparison', 'per_diem_exceeded', { ...RESOLVED, comparison: undefined }],
		['a blank category', 'category_limit', { ...RESOLVED, category: '  ' }],
		['a mileage row with no rate', 'mileage_amount_mismatch', { ...RESOLVED, miles: '10', direction: 'over' }],
		[
			'a resolved mileage row with no direction',
			'mileage_amount_mismatch',
			{ ...RESOLVED, ...MILEAGE, direction: null }
		]
	] as [string, string, Record<string, unknown> | undefined][])(
		'falls back to the server English for %s',
		(_why, code, params) => {
			expect(policyViolationText(violation(code, params), enT)).toBe('server English');
		}
	);
});
