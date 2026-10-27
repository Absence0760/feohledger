/**
 * A refused ERP push is stated in the reader's language with what to do, from
 * the `{code, params}` `invoice.erp_failed` records — and every code the
 * backend can refuse with has a message here (decisions §284).
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { en } from '../i18n/locales/en';
import { messages as de } from '../i18n/locales/de';
import { messages as es } from '../i18n/locales/es';
import { messages as fr } from '../i18n/locales/fr';
import { messages as ja } from '../i18n/locales/ja';
import { messages as ptBR } from '../i18n/locales/pt-BR';
import { interpolate } from '../i18n/interpolate';
import type { MessageKey } from '../i18n/messages';
import { ERP_REFUSAL_CODES, erpRefusalText } from './erpRefusals';

const translator =
	(messages: Record<string, string>, locale: string) =>
	(key: MessageKey, params?: Record<string, string | number>) =>
		interpolate(messages[key], params, locale);

const enT = translator(en, 'en');

/** `ERP_REFUSAL_CODES` as the backend declares it, read from its source. */
function backendCodes(): string[] {
	const path = new URL(
		'../../../../backend/app/services/erp_adapters/base.py',
		import.meta.url
	).pathname;
	const source = readFileSync(path, 'utf8');
	const block = source.match(/ERP_REFUSAL_CODES: frozenset\[str\] = frozenset\(\s*\{([\s\S]*?)\}\s*\)/);
	expect(block, 'ERP_REFUSAL_CODES not found in base.py').not.toBeNull();
	return [...block![1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
}

describe('the registry pins the backend', () => {
	it('every code the backend can refuse with has a message', () => {
		const missing = backendCodes().filter((c) => !ERP_REFUSAL_CODES.includes(c));
		expect(missing, 'add a message in erpRefusals.ts for each').toEqual([]);
	});

	it('every code stated here is one the backend registers', () => {
		const backend = new Set(backendCodes());
		expect(ERP_REFUSAL_CODES.filter((c) => !backend.has(c))).toEqual([]);
	});

	it('every code renders a full sentence in every locale', () => {
		const locales = { en, de, es, fr, ja, 'pt-BR': ptBR } as Record<string, Record<string, string>>;
		for (const [locale, messages] of Object.entries(locales)) {
			const t = translator(messages, locale);
			for (const code of ERP_REFUSAL_CODES) {
				const params = {
					provider: 'QuickBooks Online',
					accounts: ['6100'],
					intuit_code: '6000',
					exception_type: 'duplicate',
					claim: 'payment',
					reason: 'tenant_budget'
				};
				const text = erpRefusalText({ code, params }, t);
				expect(text, `${locale} ${code}`).toBeTruthy();
				expect(text, `${locale} ${code} left a placeholder`).not.toMatch(/\{\w+\}/);
			}
		}
	});
});

describe('erpRefusalText', () => {
	it('says whose API allowance stopped the call, per reason (§322)', () => {
		const say = (reason: unknown) =>
			erpRefusalText(
				{
					code: 'erp_call_budget_exhausted',
					params: { provider: 'Blackbaud FE NXT', reason, retry_after: '2026-10-27T00:00:00+00:00' }
				},
				enT
			);
		expect(say('tenant_budget')).toBe(
			"This workspace has used its share of today's Blackbaud FE NXT API calls, so FeohLedger made no call. The allowance resets at midnight UTC — retry then."
		);
		expect(say('subscription_quota')).toContain('its daily quota is used');
		expect(say('rate_limited')).toContain('Retry in a minute');
		// A reason this build does not know: the server's sentence instead.
		expect(say('from_a_newer_backend')).toBeNull();
		expect(say(undefined)).toBeNull();
	});

	it('tells the user what to do about an unlinked vendor', () => {
		expect(
			erpRefusalText(
				{ code: 'vendor_not_linked', params: { provider: 'QuickBooks Online' } },
				enT
			)
		).toBe(
			"This vendor isn't in QuickBooks Online yet. Sync vendors from QuickBooks Online, or add the vendor there, then retry."
		);
	});

	it('names the GL accounts that are not linked', () => {
		const text = erpRefusalText(
			{
				code: 'account_not_linked',
				params: { provider: 'NetSuite', accounts: ['Job Materials', '6100'] }
			},
			enT
		);
		expect(text).toMatch(/^Not linked to an account in NetSuite: Job Materials.*6100\. /);
		expect(text).toContain('recode the invoice to a linked account');
	});

	it('says which line is uncoded when no account was named', () => {
		expect(
			erpRefusalText({ code: 'account_not_linked', params: { provider: 'Xero', accounts: [] } }, enT)
		).toContain('A line has no GL account');
	});

	it('is localized', () => {
		expect(
			erpRefusalText(
				{ code: 'vendor_not_linked', params: { provider: 'Xero' } },
				translator(de, 'de')
			)
		).toContain('Dieser Lieferant ist noch nicht in Xero');
	});

	it('states the provider-less refusals without a provider', () => {
		expect(erpRefusalText({ code: 'not_connected', params: {} }, enT)).toContain(
			"The ERP isn't connected"
		);
		expect(erpRefusalText({ code: 'invoice_due_before_date', params: {} }, enT)).toBe(
			'This invoice is due before its invoice date, which the ERP rejects. Correct the dates, then retry.'
		);
	});

	it('says which open exception stopped the push, and what to do (§286)', () => {
		expect(
			erpRefusalText(
				{ code: 'exception_blocks_erp_export', params: { exception_type: 'duplicate' } },
				enT
			)
		).toBe(
			`This invoice has an open ${en['exceptions.type.duplicate']} exception. Resolve or dismiss it in Exceptions, then send it to the ERP.`
		);
		expect(
			erpRefusalText(
				{ code: 'exception_blocks_erp_export', params: { exception_type: 'fraud_flag' } },
				translator(de, 'de')
			)
		).toContain(`(${de['exceptions.type.fraudFlag']})`);
		// A type this build predates still reads, de-underscored.
		expect(
			erpRefusalText(
				{ code: 'exception_blocks_erp_export', params: { exception_type: 'brand_new_kind' } },
				enT
			)
		).toContain('open brand new kind exception');
	});

	it('says the bill may already be in the ERP when an earlier attempt may have posted it', () => {
		const params = { exception_type: 'fraud_flag', possibly_posted: true };
		expect(erpRefusalText({ code: 'exception_blocks_erp_export', params }, enT)).toBe(
			`This invoice has an open ${en['exceptions.type.fraudFlag']} exception, and an earlier attempt may already have created the bill in the ERP. Check the ERP before anyone pays it, then resolve or dismiss the exception in Exceptions.`
		);
		const locales = { de, es, fr, ja, 'pt-BR': ptBR } as Record<string, Record<string, string>>;
		for (const [locale, messages] of Object.entries(locales)) {
			const text = erpRefusalText(
				{ code: 'exception_blocks_erp_export', params },
				translator(messages, locale)
			);
			expect(text, locale).toBe(
				interpolate(messages['erpRefusal.exceptionBlocksExportPossiblyPosted'], {
					type: messages['exceptions.type.fraudFlag']
				}, locale)
			);
			expect(text, `${locale} left a placeholder`).not.toMatch(/\{\w+\}/);
		}
		// Only a literal `true` switches the sentence: a first-attempt refusal
		// carries no flag, and a malformed one reads as the plain refusal.
		for (const flag of [undefined, false, 'true', 1]) {
			expect(
				erpRefusalText(
					{
						code: 'exception_blocks_erp_export',
						params: { exception_type: 'duplicate', possibly_posted: flag }
					},
					enT
				)
			).toBe(
				`This invoice has an open ${en['exceptions.type.duplicate']} exception. Resolve or dismiss it in Exceptions, then send it to the ERP.`
			);
		}
	});

	it('says FeohLedger is already paying the invoice, per rail, in every locale', () => {
		const code = 'payment_blocks_erp_export';
		expect(erpRefusalText({ code, params: { claim: 'payment' } }, enT)).toBe(
			en['erpRefusal.paymentBlocksExport']
		);
		expect(erpRefusalText({ code, params: { claim: 'virtual_card' } }, enT)).toBe(
			en['erpRefusal.cardBlocksExport']
		);
		const locales = { en, de, es, fr, ja, 'pt-BR': ptBR } as Record<
			string,
			Record<string, string>
		>;
		const cases: [string, boolean, string][] = [
			['payment', false, 'erpRefusal.paymentBlocksExport'],
			['payment', true, 'erpRefusal.paymentBlocksExportPossiblyPosted'],
			['virtual_card', false, 'erpRefusal.cardBlocksExport'],
			['virtual_card', true, 'erpRefusal.cardBlocksExportPossiblyPosted']
		];
		for (const [locale, messages] of Object.entries(locales)) {
			for (const [claim, posted, key] of cases) {
				const params = posted ? { claim, possibly_posted: true } : { claim };
				const text = erpRefusalText({ code, params }, translator(messages, locale));
				expect(messages[key], `${locale} ${key}`).toBeTruthy();
				expect(text, `${locale} ${key}`).toBe(messages[key]);
			}
		}
		// Only a literal `true` switches to the possibly-posted sentence.
		expect(
			erpRefusalText({ code, params: { claim: 'payment', possibly_posted: 'true' } }, enT)
		).toBe(en['erpRefusal.paymentBlocksExport']);
	});

	it.each([
		['no refusal recorded (a row from before coded refusals)', undefined],
		['a paid refusal with no claim', { code: 'payment_blocks_erp_export', params: {} }],
		[
			'a paid refusal with an unknown claim',
			{ code: 'payment_blocks_erp_export', params: { claim: 'cheque' } }
		],
		['an exception refusal with no type', { code: 'exception_blocks_erp_export', params: {} }],
		['an unknown code (a newer backend)', { code: 'brand_new', params: { provider: 'X' } }],
		['a missing provider', { code: 'vendor_not_linked', params: {} }],
		['a blank provider', { code: 'vendor_not_linked', params: { provider: ' ' } }],
		['accounts that are not strings', { code: 'account_not_linked', params: { provider: 'X', accounts: [1] } }],
		['accounts absent', { code: 'account_not_linked', params: { provider: 'X' } }]
	])('falls back to the server sentence for %s', (_label, refusal) => {
		expect(erpRefusalText(refusal, enT)).toBeNull();
	});
});

describe('refusals QuickBooks answered with (Intuit Fault codes)', () => {
	const qbo = 'QuickBooks Online';

	it('says a closed period is the date, and what to do', () => {
		expect(
			erpRefusalText({ code: 'period_closed', params: { provider: qbo, intuit_code: '6210' } }, enT)
		).toBe(
			'QuickBooks Online refused this bill because its date is in a closed period (the books are closed). Change the invoice date or reopen the period in QuickBooks Online, then retry.'
		);
	});

	it('says the vendor is inactive', () => {
		expect(
			erpRefusalText({ code: 'vendor_inactive', params: { provider: qbo, intuit_code: '2500' } }, enT)
		).toContain('its vendor is inactive or deleted there. Make the vendor active in QuickBooks Online');
	});

	it('names the inactive accounts by our GL code', () => {
		expect(
			erpRefusalText(
				{ code: 'account_inactive', params: { provider: qbo, intuit_code: '2500', accounts: ['6100'] } },
				enT
			)
		).toContain('because 6100 is inactive or deleted there');
	});

	it('names the Intuit code of an unmapped fault', () => {
		expect(
			erpRefusalText({ code: 'intuit_fault', params: { provider: qbo, intuit_code: '6000' } }, enT)
		).toMatch(/^QuickBooks Online refused this bill \(Intuit error 6000\)\. /);
		expect(
			erpRefusalText(
				{ code: 'reference_invalid', params: { provider: qbo, intuit_code: '2500' } },
				translator(fr, 'fr')
			)
		).toContain('erreur Intuit 2500');
	});

	it.each([
		['an inactive account with none named', { code: 'account_inactive', params: { provider: qbo, accounts: [] } }],
		['an unmapped fault without its code', { code: 'intuit_fault', params: { provider: qbo } }],
		['a code that is not a number', { code: 'intuit_fault', params: { provider: qbo, intuit_code: 'Acme' } }]
	])('falls back to the server sentence for %s', (_label, refusal) => {
		expect(erpRefusalText(refusal, enT)).toBeNull();
	});
});
