/**
 * The coded money-path / MFA refusals (`backend/app/api/refusals.py::
 * coded_refusal`) are stated in the reader's language from their code and
 * params, and anything this build cannot state completely degrades to `null`
 * so the caller renders the server's English `message`.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { en } from '../i18n/locales/en';
import { messages as de } from '../i18n/locales/de';
import { interpolate } from '../i18n/interpolate';
import { setActiveFormatLocale } from '../i18n/formatLocale';
import type { MessageKey } from '../i18n/messages';
import {
	CODED_REFUSAL_CODES,
	INVOICE_REQUIRED_FIELDS_MISSING,
	INVOICE_STALE_EDIT,
	PLAN_FEATURE_REQUIRED,
	codedRefusalText,
	localizeApiDetail
} from './codedRefusals';

const translator =
	(messages: Record<string, string>, locale: string) =>
	(key: MessageKey, params?: Record<string, string | number>) =>
		interpolate(messages[key], params, locale);

const enT = translator(en, 'en');
const deT = translator(de, 'de');

afterEach(() => setActiveFormatLocale(undefined));

/** Shaped exactly as `coded_refusal` serializes. */
function detail(code: string, params: Record<string, unknown> = {}, message = 'server English') {
	return { code, message, params };
}

/** The params `review._enforce_approval_thresholds._gate_params` sends. */
function gateParams(overrides: Record<string, unknown> = {}) {
	return {
		amount: '12000.00',
		currency: 'USD',
		limit: '10000.00',
		limit_currency: 'USD',
		recent_spend: null,
		aggregate_amount: null,
		window_days: null,
		expressible: true,
		measured_amount: null,
		...overrides
	};
}

describe('the registry pins the backend', () => {
	it('every code it states is a literal the backend actually emits', () => {
		// Read the backend source rather than re-typing the list: a code renamed
		// on the server leaves a dead entry here, and this fails naming it.
		const appDir = new URL('../../../../backend/app/', import.meta.url).pathname;
		const files: string[] = [];
		const walk = (dir: string) => {
			for (const name of readdirSync(dir)) {
				const full = join(dir, name);
				if (statSync(full).isDirectory()) walk(full);
				else if (name.endsWith('.py')) files.push(full);
			}
		};
		walk(appDir);
		const source = files.map((f) => readFileSync(f, 'utf8')).join('\n');
		for (const code of CODED_REFUSAL_CODES) {
			expect(source, `backend emits no "${code}"`).toContain(`"${code}"`);
		}
	});

	it('the exception codes are the ones /bulk/resolve reports per row', () => {
		expect(CODED_REFUSAL_CODES).toContain('segregation_raiser');
		expect(CODED_REFUSAL_CODES).toContain('segregation_implicated');
	});
});

describe('codedRefusalText', () => {
	it.each([
		['approval_segregation', 'refusal.approvalSegregation'],
		['approval_level_reuse', 'refusal.approvalLevelReuse'],
		['approval_not_named_approver', 'refusal.approvalNotNamedApprover'],
		['approval_max_amount_misconfigured', 'refusal.approvalMaxMisconfigured'],
		['segregation_raiser', 'refusal.exceptionSegregationRaiser'],
		['segregation_implicated', 'refusal.exceptionSegregationImplicated'],
		['credit_memo_vendor_mismatch', 'refusal.creditMemoVendorMismatch'],
		['credit_memo_vendor_unresolved', 'refusal.creditMemoVendorUnresolved'],
		['credit_memo_entity_mismatch', 'refusal.creditMemoEntityMismatch'],
		['credit_memo_currency_mismatch', 'refusal.creditMemoCurrencyMismatch'],
		[INVOICE_STALE_EDIT, 'refusal.invoiceStaleEdit'],
		['mfa_code_invalid', 'refusal.mfaCodeInvalid']
	] as [string, MessageKey][])('%s is the fixed sentence %s', (code, key) => {
		expect(codedRefusalText(code, {}, deT)).toBe(de[key]);
	});

	it('states the CFO gate with both figures in their own currencies', () => {
		setActiveFormatLocale('en-US');
		const out = codedRefusalText(
			'approval_cfo_required',
			gateParams({ amount: '9000.00', currency: 'GBP', measured_amount: '11403.00' }),
			enT
		);
		expect(out).toBe(
			'Invoice amount £9,000.00 exceeds $10,000.00. CFO approval required. ' +
				'Measured as $11,403.00 — the limit is set in USD.'
		);
	});

	it('formats the figures for the reader, not the server', () => {
		setActiveFormatLocale('de-DE');
		const out = codedRefusalText('approval_max_amount_exceeded', gateParams(), deT) ?? '';
		expect(out).toContain('12.000,00');
		expect(out).toContain('10.000,00');
		expect(out).not.toContain('server English');
	});

	it('carries the structuring note, pluralised', () => {
		setActiveFormatLocale('en-US');
		const params = gateParams({
			amount: '4000.00',
			recent_spend: '7000.00',
			aggregate_amount: '11000.00',
			window_days: 30
		});
		const out = codedRefusalText('approval_cfo_required', params, enT) ?? '';
		expect(out).toContain('combined with $7,000.00');
		expect(out).toContain('(last 30 days)');
		expect(out).toContain('it totals $11,000.00.');
		expect(codedRefusalText('approval_cfo_required', { ...params, window_days: 1 }, enT)).toContain(
			'(last 1 day)'
		);
	});

	it('says an inexpressible invoice could not be measured, never a measured figure', () => {
		const out =
			codedRefusalText(
				'approval_cfo_required',
				gateParams({ expressible: false, measured_amount: null }),
				enT
			) ?? '';
		expect(out).toContain('could not be expressed in USD');
		expect(out).not.toContain('Measured as');
	});

	it('states a code-less invoice bare, never in a borrowed currency', () => {
		setActiveFormatLocale('en-US');
		const out =
			codedRefusalText(
				'approval_cfo_required',
				gateParams({
					amount: '4000.00',
					currency: null,
					recent_spend: '7000.00',
					aggregate_amount: '11000.00',
					window_days: 30
				}),
				enT
			) ?? '';
		expect(out).toContain('Invoice amount 4,000.00 exceeds $10,000.00.');
		expect(out).toContain('combined with 7,000.00');
		expect(out).toContain('it totals 11,000.00.');
	});

	it('a malformed CFO threshold names the configured limit', () => {
		const out = codedRefusalText('approval_cfo_required', gateParams({ limit: null }), enT);
		expect(out).toContain('exceeds the configured limit. CFO approval required.');
	});

	it('the max gate has no unknown-limit sentence — a null limit falls back', () => {
		expect(
			codedRefusalText('approval_max_amount_exceeded', gateParams({ limit: null }), enT)
		).toBeNull();
	});

	it('states the expense CFO gate, including the inexpressible total', () => {
		setActiveFormatLocale('en-US');
		const base = { currency: 'USD', limit: '5000.00', report_currency: 'EUR' };
		expect(codedRefusalText('expense_cfo_required', { ...base, amount: '6000.00' }, enT)).toBe(
			'Report total $6,000.00 exceeds $5,000.00. CFO approval required.'
		);
		expect(codedRefusalText('expense_cfo_required', { ...base, amount: null }, enT)).toContain(
			'(no rate from EUR)'
		);
		expect(
			codedRefusalText('expense_cfo_required', { ...base, amount: null, limit: null }, enT)
		).toContain('against the configured limit');
	});

	it('names a status by its label, and an unknown one verbatim', () => {
		expect(codedRefusalText('credit_memo_invoice_settled', { status: 'paid' }, enT)).toContain(
			`The invoice is ${en['invoices.status.paid']}:`
		);
		expect(codedRefusalText('credit_memo_not_applicable', { status: 'void' }, enT)).toContain(
			en['creditMemos.status.void']
		);
		expect(
			codedRefusalText('credit_memo_not_editable', { status: 'from_the_future' }, enT)
		).toContain('from_the_future');
	});

	it('states the remaining balance with its currency, or bare without one', () => {
		setActiveFormatLocale('en-US');
		expect(
			codedRefusalText('credit_memo_exceeds_balance', { remaining: '40.00', currency: 'EUR' }, enT)
		).toContain('(€40.00)');
		expect(
			codedRefusalText('credit_memo_exceeds_balance', { remaining: '40.00', currency: null }, enT)
		).toContain('(40.00)');
	});

	it('labels the missing invoice fields', () => {
		const out = codedRefusalText(
			INVOICE_REQUIRED_FIELDS_MISSING,
			{ fields: ['vendor', 'amount'] },
			enT
		);
		expect(out).toContain(en['invoices.col.vendor']);
		expect(out).toContain(en['invoices.col.amount']);
	});

	it.each([
		['an unknown code', 'from_a_newer_backend', {}],
		['a figure that is not an exact decimal', 'approval_cfo_required', gateParams({ amount: 9000 })],
		['a bad currency', 'approval_cfo_required', gateParams({ currency: 'dollars' })],
		[
			'a structuring note missing its window',
			'approval_cfo_required',
			gateParams({ recent_spend: '1.00', aggregate_amount: '2.00' })
		],
		['a status that is not a string', 'credit_memo_invoice_settled', { status: 7 }],
		['an empty field list', INVOICE_REQUIRED_FIELDS_MISSING, { fields: [] }],
		['a balance with a malformed currency', 'credit_memo_exceeds_balance', { remaining: '1.00', currency: 'x' }]
	])('is null for %s, so the server sentence renders', (_label, code, params) => {
		expect(codedRefusalText(code, params as Record<string, unknown>, enT)).toBeNull();
	});
});

describe('the no-rail pilot refusals (issue #517)', () => {
	it.each([
		['payments_record_only', 'refusal.paymentsRecordOnly'],
		['external_payment_segregation', 'refusal.externalPaymentSegregation'],
		['external_payment_card_live', 'refusal.externalPaymentCardLive'],
		['external_payment_credit_conflict', 'refusal.externalPaymentCreditConflict'],
		['external_payment_nothing_to_pay', 'refusal.externalPaymentNothingToPay'],
		['external_payment_paid_on_future', 'refusal.externalPaymentPaidOnFuture'],
		['nacha_requires_record_only', 'refusal.nachaRequiresRecordOnly'],
		['nacha_already_exported', 'refusal.nachaAlreadyExported']
	] as [string, MessageKey][])('%s is the fixed sentence %s', (code, key) => {
		expect(codedRefusalText(code, {}, deT)).toBe(de[key]);
	});

	it('states the record-only refusal whatever the reason it resolved for', () => {
		for (const reason of ['configured', 'unknown_mode', 'no_processor_in_deployed_environment']) {
			expect(codedRefusalText('payments_record_only', { reason }, enT)).toBe(
				en['refusal.paymentsRecordOnly']
			);
		}
	});

	it('names the invoice status, the payment status and the exception type by their labels', () => {
		expect(
			codedRefusalText('external_payment_not_payable', { status: 'paid' }, enT)
		).toContain(`(this one is ${en['invoices.status.paid']})`);
		expect(
			codedRefusalText('external_payment_payment_live', { payment_status: 'submitted' }, enT)
		).toContain(`(${en['payments.status.submitted']})`);
		expect(
			codedRefusalText('external_payment_blocking_exception', { exception_type: 'duplicate' }, enT)
		).toContain(`unresolved ${en['exceptions.type.duplicate']} exception`);
		// A type this build predates renders readably rather than falling back.
		expect(
			codedRefusalText('external_payment_blocking_exception', { exception_type: 'brand_new_kind' }, enT)
		).toContain('brand new kind');
	});

	it('names the run an invoice is already in by its short id', () => {
		expect(
			codedRefusalText(
				'external_payment_in_run',
				{ payment_run_id: '0f9e8d7c-1111-2222-3333-444455556666' },
				enT
			)
		).toContain('payment run 0f9e8d7c.');
	});

	it('states the amount owed with its currency, or bare without one', () => {
		setActiveFormatLocale('en-US');
		expect(
			codedRefusalText('external_payment_amount_mismatch', { amount: '980.00', currency: 'USD' }, enT)
		).toContain('($980.00)');
		expect(
			codedRefusalText('external_payment_amount_mismatch', { amount: '980.00', currency: null }, enT)
		).toContain('(980.00)');
	});

	it('prefixes the invoice a whole-run refusal names', () => {
		// `POST /runs/{id}/record-outside` adds `invoice_number` to the same codes.
		expect(
			codedRefusalText(
				'external_payment_segregation',
				{ invoice_number: 'INV-77' },
				deT
			)
		).toBe(
			interpolate(de['refusal.externalPaymentForInvoice'], {
				invoice: 'INV-77',
				reason: de['refusal.externalPaymentSegregation']
			}, 'de')
		);
		expect(
			codedRefusalText(
				'external_payment_not_payable',
				{ invoice_number: 'INV-78', status: 'approved' },
				enT
			)
		).toMatch(/^Invoice INV-78: Only an approved invoice/);
	});

	it('states a staged amount that changed, with both figures, for the invoice', () => {
		setActiveFormatLocale('en-US');
		const out = codedRefusalText(
			'external_payment_amount_changed',
			{ invoice_number: 'INV-5', staged_amount: '1000.00', amount: '980.00', currency: 'USD' },
			enT
		);
		expect(out).toMatch(/^Invoice INV-5: /);
		expect(out).toContain('($1,000.00 staged, $980.00 owed now)');
		expect(
			codedRefusalText(
				'external_payment_amount_changed',
				{ invoice_number: 'INV-5', staged_amount: '1000.00', amount: 980, currency: 'USD' },
				enT
			)
		).toBeNull();
	});

	it('names the invoice a NACHA file cannot carry', () => {
		for (const [code, key] of [
			['nacha_payment_not_ach', 'refusal.nachaPaymentNotAch'],
			['nacha_vendor_bank_missing', 'refusal.nachaVendorBankMissing'],
			['nacha_currency_not_usd', 'refusal.nachaCurrencyNotUsd'],
			['nacha_amount_too_large', 'refusal.nachaAmountTooLarge'],
			// `params.reason` is a dispatcher machine code — never shown.
			['nacha_payment_not_payable', 'refusal.nachaPaymentNotPayable']
		] as [string, MessageKey][]) {
			expect(
				codedRefusalText(code, { invoice_number: 'INV-9', reason: 'invoice_blocked:fraud_flag' }, deT)
			).toBe(
				interpolate(de[key], { invoice: 'INV-9' }, 'de')
			);
		}
	});

	it('labels the bank-file settings that are missing', () => {
		const out = codedRefusalText(
			'nacha_not_configured',
			{ missing: ['company_id', 'odfi_routing', 'a_field_from_the_future'] },
			enT
		);
		expect(out).toContain(en['org.payments.nachaField.companyId']);
		expect(out).toContain(en['org.payments.nachaField.odfiRouting']);
		expect(out).toContain('a_field_from_the_future');
	});

	it.each([
		['a not-payable refusal with no status', 'external_payment_not_payable', {}],
		['a blocking refusal with no type', 'external_payment_blocking_exception', {}],
		['an in-run refusal with no run', 'external_payment_in_run', {}],
		['an amount that is not an exact decimal', 'external_payment_amount_mismatch', { amount: 980, currency: 'USD' }],
		['an amount with a malformed currency', 'external_payment_amount_mismatch', { amount: '1.00', currency: 'dollars' }],
		// The run door sends `invoice_number: null` for an invoice with no number;
		// the server's English names it by id, so that is the better sentence.
		['a run refusal whose invoice has no number', 'external_payment_card_live', { invoice_number: null }],
		['a NACHA refusal with no invoice', 'nacha_payment_not_ach', {}],
		['an empty missing-field list', 'nacha_not_configured', { missing: [] }],
		['a missing-field list that is not strings', 'nacha_not_configured', { missing: [1] }]
	])('is null for %s, so the server sentence renders', (_label, code, params) => {
		expect(codedRefusalText(code, params as Record<string, unknown>, enT)).toBeNull();
	});
});

describe('localizeApiDetail', () => {
	it('reaches the money-path table from a raw detail', () => {
		expect(localizeApiDetail(detail('approval_segregation'), deT)).toBe(
			de['refusal.approvalSegregation']
		);
	});

	it('still reaches the auth refusals and the GL-chart refusal', () => {
		expect(localizeApiDetail(detail('step_up_failed'), deT)).toBe(de['authRefusal.stepUpFailed']);
		const chart = {
			code: 'gl_codes_outside_chart',
			on_lines: false,
			foreign: ['6000'],
			retired: [],
			unknown: [],
			message: 'x'
		};
		expect(localizeApiDetail(chart, enT)).toContain("'6000'");
	});

	it('is null for anything it cannot state', () => {
		expect(localizeApiDetail('plain string detail', enT)).toBeNull();
		expect(localizeApiDetail(detail('from_a_newer_backend'), enT)).toBeNull();
		expect(localizeApiDetail(undefined, enT)).toBeNull();
	});
});

describe('the plan-feature refusal', () => {
	it('names the feature and the tier that grants it, in the reader’s language', () => {
		expect(codedRefusalText(PLAN_FEATURE_REQUIRED, { feature: 'scim' }, enT)).toBe(
			'SCIM user provisioning is not included in your plan. It is available on the Scale ' +
				'plan — upgrade in Billing to use it.'
		);
		const out = codedRefusalText(PLAN_FEATURE_REQUIRED, { feature: 'sso' }, deT) ?? '';
		expect(out).toContain(de['planFeature.sso']);
		expect(out).toContain('Growth');
	});

	it('falls back to the server sentence for a feature this build does not know', () => {
		expect(codedRefusalText(PLAN_FEATURE_REQUIRED, { feature: 'teleportation' }, enT)).toBeNull();
		expect(codedRefusalText(PLAN_FEATURE_REQUIRED, {}, enT)).toBeNull();
	});
});

describe('the goods-receipt refusals', () => {
	const t = (key: string, params?: Record<string, string | number>) =>
		params ? `${key} ${JSON.stringify(params)}` : key;

	it('names the PO or the receipt number the refusal is about', () => {
		expect(codedRefusalText('goods_receipt_po_cancelled', { poNumber: 'PO-1042' }, t)).toBe(
			'refusal.goodsReceiptPoCancelled {"poNumber":"PO-1042"}'
		);
		expect(codedRefusalText('goods_receipt_number_taken', { grNumber: 'DN-7' }, t)).toBe(
			'refusal.goodsReceiptNumberTaken {"grNumber":"DN-7"}'
		);
		expect(codedRefusalText('goods_receipt_already_cancelled', { grNumber: 'GR-1' }, t)).toBe(
			'refusal.goodsReceiptAlreadyCancelled {"grNumber":"GR-1"}'
		);
	});

	it('falls back to the server sentence when the number is missing', () => {
		expect(codedRefusalText('goods_receipt_po_cancelled', {}, t)).toBeNull();
		expect(codedRefusalText('goods_receipt_number_taken', { grNumber: '' }, t)).toBeNull();
	});

	it('states the line refusals', () => {
		for (const code of [
			'goods_receipt_line_not_on_po',
			'goods_receipt_line_duplicated',
			'goods_receipt_line_required',
			'goods_receipt_nothing_received',
			'goods_receipt_idempotency_reused',
			'goods_receipt_not_manual'
		]) {
			expect(codedRefusalText(code, {}, t), code).toMatch(/^refusal\.goodsReceipt/);
		}
	});
});

