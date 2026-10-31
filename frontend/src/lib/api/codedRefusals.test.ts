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
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import { messages as de } from '../i18n/locales/de';
import { messages as fr } from '../i18n/locales/fr';
import { messages as es } from '../i18n/locales/es';
import { messages as ptBR } from '../i18n/locales/pt-BR';
import { messages as ja } from '../i18n/locales/ja';
import { interpolate } from '../i18n/interpolate';
import { setActiveFormatLocale } from '../i18n/formatLocale';
import type { MainMessageKey, MessageKey } from '../i18n/messages';
import {
	CODED_REFUSAL_CODES,
	INVOICE_REQUIRED_FIELDS_MISSING,
	INVOICE_STALE_APPROVAL,
	INVOICE_STALE_EDIT,
	PLAN_FEATURE_REQUIRED,
	RATE_LIMITED,
	SIGNUP_EMAIL_HAS_WORKSPACE,
	codedRefusalText,
	localizeApiDetail,
	refusalField
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
		['approval_segregation_receiver', 'refusal.approvalSegregationReceiver'],
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
		[INVOICE_STALE_APPROVAL, 'refusal.invoiceStaleApproval'],
		['mfa_code_invalid', 'refusal.mfaCodeInvalid'],
		['erp_not_connected', 'erpRefusal.notConnected'],
		['invoice_due_before_date', 'refusal.invoiceDueBeforeDate'],
		['sso_email_in_other_workspace', 'refusal.ssoEmailInOtherWorkspace'],
		['email_in_this_workspace', 'refusal.emailInThisWorkspace'],
		['email_in_other_workspace', 'refusal.emailInOtherWorkspace'],
		['email_check_throttled', 'refusal.emailCheckThrottled'],
		['workspace_close_confirmation_mismatch', 'refusal.workspaceCloseConfirmationMismatch'],
		['workspace_close_in_progress', 'refusal.workspaceCloseInProgress'],
		['workspace_close_audit_unavailable', 'refusal.workspaceCloseAuditUnavailable'],
		['workspace_close_incomplete', 'refusal.workspaceCloseIncomplete'],
		['workspace_close_actor_not_admin', 'refusal.workspaceCloseActorNotAdmin'],
		['signup_slug_held', 'refusal.signupSlugHeld']
	] as [string, MainMessageKey][])('%s is the fixed sentence %s', (code, key) => {
		expect(codedRefusalText(code, {}, deT)).toBe(de[key]);
	});

	it('states a refused workspace closure only when it can read the blockers', () => {
		expect(
			codedRefusalText('workspace_close_refused', { blockers: ['payments'] }, deT)
		).toBe(de['refusal.workspaceCloseRefused']);
		expect(codedRefusalText('workspace_close_refused', {}, deT)).toBeNull();
		expect(codedRefusalText('workspace_close_refused', { blockers: [1] }, deT)).toBeNull();
	});

	it('states an incomplete ERP listing by why it stopped', () => {
		const provider = 'QuickBooks Online';
		expect(
			codedRefusalText('erp_listing_incomplete', { provider, gap: 'fetch_failed' }, enT)
		).toBe(
			'QuickBooks Online could not be read in full, so nothing was synced. Try again in a few minutes.'
		);
		expect(codedRefusalText('erp_listing_incomplete', { provider, gap: 'truncated' }, enT)).toBe(
			'QuickBooks Online has more records than one sync can read, so nothing was synced. Contact support.'
		);
		// A gap this build does not know → the server's sentence.
		expect(codedRefusalText('erp_listing_incomplete', { provider, gap: 'other' }, enT)).toBeNull();
	});

	it('points a sign-in on the wrong workspace at the account’s own workspace by host only', () => {
		const url = 'https://example.feohledger.com/login?email=a%40b.c';
		const text = codedRefusalText('wrong_workspace', { sign_in_url: url }, enT);
		expect(text).toBe(
			'This account belongs to another workspace, example.feohledger.com. Sign in there instead.'
		);
		// The link is the page's button; the sentence never repeats the URL,
		// its query string or the (percent-encoded) address in it.
		expect(text).not.toContain('?email=');
		expect(text).not.toContain('a%40b.c');
		expect(text).not.toContain('https://');
		// A port stays: on a laptop the workspace is `acme.localhost:7777`.
		expect(
			codedRefusalText('wrong_workspace', { sign_in_url: 'http://acme.localhost:7777/login' }, enT)
		).toBe('This account belongs to another workspace, acme.localhost:7777. Sign in there instead.');
		const noLink =
			"This account belongs to a different workspace. Sign in on that workspace's own page.";
		expect(codedRefusalText('wrong_workspace', { sign_in_url: null }, enT)).toBe(noLink);
		// Not an http(s) URL → nothing from it is named.
		expect(codedRefusalText('wrong_workspace', { sign_in_url: 'javascript:alert(1)' }, enT)).toBe(
			noLink
		);
		expect(codedRefusalText('wrong_workspace', { sign_in_url: 'not a url' }, enT)).toBe(noLink);
	});

	it('points a signup for an address that has an account at that workspace', () => {
		const url = 'https://example-co.feohledger.com/login?email=a%40example.com';
		expect(codedRefusalText(SIGNUP_EMAIL_HAS_WORKSPACE, { sign_in_url: url }, enT)).toBe(
			`This email address already has a FeohLedger workspace. Sign in at ${url}.`
		);
		expect(codedRefusalText(SIGNUP_EMAIL_HAS_WORKSPACE, { sign_in_url: null }, enT)).toBe(
			"This email address already has a FeohLedger workspace. Sign in on your workspace's own page."
		);
		// Stated in the reader's language, the link carried through verbatim.
		expect(codedRefusalText(SIGNUP_EMAIL_HAS_WORKSPACE, { sign_in_url: url }, deT)).toBe(
			de['refusal.signupEmailHasWorkspace'].replace('{url}', url)
		);
		// The transport localizes it the same way from the raw `detail`.
		expect(
			localizeApiDetail(
				{
					code: SIGNUP_EMAIL_HAS_WORKSPACE,
					message: 'server English',
					params: { sign_in_url: url }
				},
				enT
			)
		).toBe(`This email address already has a FeohLedger workspace. Sign in at ${url}.`);
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
			`The invoice is ${EN_ALL['invoices.status.paid']}:`
		);
		expect(codedRefusalText('credit_memo_not_applicable', { status: 'void' }, enT)).toContain(
			EN_ALL['creditMemos.status.void']
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
		expect(out).toContain(EN_ALL['invoices.col.vendor']);
		expect(out).toContain(EN_ALL['invoices.col.amount']);
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
	] as [string, MainMessageKey][])('%s is the fixed sentence %s', (code, key) => {
		expect(codedRefusalText(code, {}, deT)).toBe(de[key]);
	});

	it('states the record-only refusal whatever the reason it resolved for', () => {
		for (const reason of ['configured', 'unknown_mode', 'no_processor_in_deployed_environment']) {
			expect(codedRefusalText('payments_record_only', { reason }, enT)).toBe(
				EN_ALL['refusal.paymentsRecordOnly']
			);
		}
	});

	it('names the invoice status, the payment status and the exception type by their labels', () => {
		expect(
			codedRefusalText('external_payment_not_payable', { status: 'paid' }, enT)
		).toContain(`(this one is ${EN_ALL['invoices.status.paid']})`);
		expect(
			codedRefusalText('external_payment_payment_live', { payment_status: 'submitted' }, enT)
		).toContain(`(${EN_ALL['payments.status.submitted']})`);
		expect(
			codedRefusalText('external_payment_blocking_exception', { exception_type: 'duplicate' }, enT)
		).toContain(`unresolved ${EN_ALL['exceptions.type.duplicate']} exception`);
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
		] as [string, MainMessageKey][]) {
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
		expect(out).toContain(EN_ALL['org.payments.nachaField.companyId']);
		expect(out).toContain(EN_ALL['org.payments.nachaField.odfiRouting']);
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
				'plan — upgrade under Settings → Subscription to use it.'
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

describe('the commerce refusals', () => {
	const t = (key: string) => key;

	it('states every commerce connection and sync refusal', () => {
		expect(codedRefusalText('commerce_not_configured', {}, t)).toBe('refusal.commerceNotConfigured');
		expect(codedRefusalText('commerce_oauth_not_available', { provider: 'x' }, t)).toBe(
			'refusal.commerceOauthNotAvailable'
		);
		expect(codedRefusalText('commerce_sync_not_retryable', { status: 'synced' }, t)).toBe(
			'refusal.commerceSyncNotRetryable'
		);
		expect(codedRefusalText('commerce_sync_not_found', {}, t)).toBe('refusal.commerceSyncNotFound');
		expect(codedRefusalText('commerce_unknown_entity', {}, t)).toBe('refusal.commerceUnknownEntity');
		expect(codedRefusalText('commerce_unknown_provider', {}, t)).toBe(
			'refusal.commerceUnknownProvider'
		);
	});
});

describe('the ERP send refused because FeohLedger is already paying the invoice', () => {
	it('states the 409 the send doors answer with, per rail', () => {
		expect(
			localizeApiDetail(detail('payment_blocks_erp_export', { claim: 'payment' }), enT)
		).toBe(EN_ALL['erpRefusal.paymentBlocksExport']);
		expect(codedRefusalText('payment_blocks_erp_export', { claim: 'virtual_card' }, deT)).toBe(
			de['erpRefusal.cardBlocksExport']
		);
		expect(
			codedRefusalText(
				'payment_blocks_erp_export',
				{ claim: 'payment', possibly_posted: true },
				enT
			)
		).toContain('an earlier attempt may already have created the bill in the ERP');
	});

	it('falls back to the server sentence when the claim is missing', () => {
		expect(codedRefusalText('payment_blocks_erp_export', {}, enT)).toBeNull();
	});
});

describe('the ERP send refused because of the vendor', () => {
	it('states the 409 the send doors answer with, per hold', () => {
		expect(
			localizeApiDetail(detail('vendor_blocks_erp_export', { hold: 'payments_blocked' }), enT)
		).toBe(EN_ALL['erpRefusal.vendorBlocked']);
		expect(codedRefusalText('vendor_blocks_erp_export', { hold: 'sanctions_match' }, deT)).toBe(
			de['erpRefusal.vendorSanctionsMatch']
		);
		expect(
			codedRefusalText(
				'vendor_blocks_erp_export',
				{ hold: 'vendor_rejected', possibly_posted: true },
				enT
			)
		).toBe(
			`${EN_ALL['erpRefusal.vendorRejected']} ${EN_ALL['erpRefusal.vendorHoldPossiblyPosted']}`
		);
	});

	it('falls back to the server sentence when the hold is missing', () => {
		expect(codedRefusalText('vendor_blocks_erp_export', {}, enT)).toBeNull();
	});
});

describe('the ERP send refused for an open duplicate or fraud exception (§286)', () => {
	it('states the 409 the send doors answer with, in the reader’s language', () => {
		expect(
			localizeApiDetail(
				detail('exception_blocks_erp_export', { exception_type: 'duplicate' }),
				enT
			)
		).toBe(
			`This invoice has an open ${EN_ALL['exceptions.type.duplicate']} exception. Resolve or dismiss it in Exceptions, then send it to the ERP.`
		);
		expect(
			codedRefusalText('exception_blocks_erp_export', { exception_type: 'fraud_flag' }, deT)
		).toContain(`(${de['exceptions.type.fraudFlag']})`);
	});

	it('falls back to the server sentence when the type is missing', () => {
		expect(codedRefusalText('exception_blocks_erp_export', {}, enT)).toBeNull();
	});

	it('says the bill may already be in the ERP on a retry after a timed-out push', () => {
		expect(
			codedRefusalText(
				'exception_blocks_erp_export',
				{ exception_type: 'duplicate', possibly_posted: true },
				enT
			)
		).toContain('an earlier attempt may already have created the bill in the ERP');
	});
});

describe('the self-service signup refusals (issue #635 finding 18)', () => {
	const FIXED: [string, MainMessageKey][] = [
		['slug_required', 'refusal.slugRequired'],
		['slug_format', 'refusal.slugFormat'],
		['slug_hyphen_edge', 'refusal.slugHyphenEdge'],
		['slug_double_hyphen', 'refusal.slugDoubleHyphen'],
		['signup_email_invalid', 'refusal.signupEmailInvalid'],
		['signup_name_has_link', 'refusal.signupNameHasLink'],
		['captcha_required', 'refusal.captchaRequired'],
		['captcha_unavailable', 'refusal.captchaUnavailable'],
		['captcha_failed', 'refusal.captchaFailed'],
		['signup_email_send_failed', 'refusal.signupEmailSendFailed'],
		['signup_link_invalid', 'refusal.signupLinkInvalid'],
		['signup_unavailable', 'refusal.signupUnavailable'],
		['signup_provisioning_failed', 'refusal.signupProvisioningFailed']
	];

	it.each(FIXED)('%s is stated in the reader\'s language', (code, key) => {
		expect(codedRefusalText(code, { field: 'slug' }, deT)).toBe(de[key]);
		expect(codedRefusalText(code, {}, enT)).toBe(en[key]);
	});

	it('names the workspace address in the reserved / taken refusals', () => {
		expect(codedRefusalText('slug_taken', { field: 'slug', slug: 'acme' }, enT)).toBe(
			'“acme” is already taken. Choose another workspace address.'
		);
		expect(codedRefusalText('slug_reserved', { slug: 'admin' }, deT)).toBe(
			de['refusal.slugReserved'].replace('{slug}', 'admin')
		);
		// No address to name: the server's sentence, never a blank.
		expect(codedRefusalText('slug_taken', {}, enT)).toBeNull();
	});

	it('never calls the workspace address a "slug" in any locale', () => {
		for (const [, key] of FIXED) expect(en[key].toLowerCase()).not.toContain('slug');
		expect(interpolate(en['refusal.slugTaken'], { slug: 'x' }, 'en')).not.toMatch(/slug/i);
		expect(interpolate(en['refusal.slugReserved'], { slug: 'x' }, 'en')).not.toMatch(/slug/i);
	});

	it('reads the form field a refusal is about', () => {
		expect(refusalField({ field: 'admin_email' })).toBe('admin_email');
		expect(refusalField({})).toBeNull();
		expect(refusalField({ field: 3 })).toBeNull();
	});
});

describe('the shared rate limiter’s 429 (`rate_limited`)', () => {
	it('states the wait in seconds, pluralised, in the reader’s language', () => {
		expect(codedRefusalText(RATE_LIMITED, { retry_after_seconds: 1 }, enT)).toBe(
			'Too many attempts. Wait 1 second and try again.'
		);
		expect(codedRefusalText(RATE_LIMITED, { retry_after_seconds: 42 }, enT)).toBe(
			'Too many attempts. Wait 42 seconds and try again.'
		);
		expect(codedRefusalText(RATE_LIMITED, { retry_after_seconds: 42 }, deT)).toBe(
			'Zu viele Versuche. Warten Sie 42 Sekunden und versuchen Sie es dann erneut.'
		);
	});

	it('rounds a longer wait UP to whole minutes, then whole hours', () => {
		const say = (s: number) => codedRefusalText(RATE_LIMITED, { retry_after_seconds: s }, enT);
		expect(say(60)).toBe('Too many attempts. Wait 1 minute and try again.');
		// 61 s is not "1 minute": the reader would come back too early.
		expect(say(61)).toBe('Too many attempts. Wait 2 minutes and try again.');
		expect(say(3540)).toBe('Too many attempts. Wait 59 minutes and try again.');
		// Rounds to 60 minutes → said as the hour it is.
		expect(say(3541)).toBe('Too many attempts. Wait 1 hour and try again.');
		expect(say(3600)).toBe('Too many attempts. Wait 1 hour and try again.');
		expect(say(3601)).toBe('Too many attempts. Wait 2 hours and try again.');
	});

	it('states it in every shipped locale', () => {
		for (const [locale, messages] of [
			['fr', fr],
			['es', es],
			['pt-BR', ptBR],
			['ja', ja]
		] as const) {
			const t = translator(messages, locale);
			for (const s of [5, 300, 7200]) {
				const text = codedRefusalText(RATE_LIMITED, { retry_after_seconds: s }, t);
				expect(text, `${locale} ${s}s`).toBeTruthy();
				expect(text).not.toContain('{');
				expect(text).not.toContain('Too many');
			}
		}
	});

	it('falls back to the server sentence for a missing or malformed wait', () => {
		for (const bad of [undefined, null, 0, -5, 1.5, '30']) {
			expect(codedRefusalText(RATE_LIMITED, { retry_after_seconds: bad }, enT)).toBeNull();
		}
	});

	it('reaches the transport from a raw limiter detail', () => {
		const raw = {
			code: 'rate_limited',
			message: 'Too many requests. Please wait 90 seconds before trying again.',
			params: { retry_after_seconds: 90 }
		};
		expect(localizeApiDetail(raw, deT)).toBe(
			'Zu viele Versuche. Warten Sie 2 Minuten und versuchen Sie es dann erneut.'
		);
	});
});

describe('the vendor merge refusals (`services/vendor_merge.py`)', () => {
	const MERGE: [string, MainMessageKey][] = [
		['vendor_merge_self', 'refusal.vendorMergeSelf'],
		['vendor_merge_no_duplicates', 'refusal.vendorMergeNoDuplicates'],
		['vendor_merge_canonical_not_found', 'refusal.vendorMergeCanonicalNotFound'],
		['vendor_merge_duplicate_not_found', 'refusal.vendorMergeDuplicateNotFound'],
		['vendor_merge_invalid_id', 'refusal.vendorMergeInvalidId'],
		['vendor_merge_cross_entity', 'refusal.vendorMergeCrossEntity'],
		['vendor_merge_blocked_duplicate', 'refusal.vendorMergeBlockedDuplicate']
	];

	it.each(MERGE)('%s is the fixed sentence %s in every shipped locale', (code, key) => {
		expect(CODED_REFUSAL_CODES).toContain(code);
		for (const [locale, messages] of [
			['en', en],
			['de', de],
			['fr', fr],
			['es', es],
			['pt-BR', ptBR],
			['ja', ja]
		] as const) {
			const text = codedRefusalText(code, {}, translator(messages, locale));
			expect(text, `${locale} ${code}`).toBe((messages as Record<string, string>)[key]);
			expect(text, `${locale} ${code}`).toBeTruthy();
		}
	});

	it('reaches the transport from the raw merge detail, in the reader’s language', () => {
		const raw = detail(
			'vendor_merge_blocked_duplicate',
			{},
			'Cannot merge a payments-blocked vendor into an unblocked canonical — resolve the ' +
				"block first (see the vendor's screening review) or exclude it from this merge."
		);
		expect(localizeApiDetail(raw, deT)).toBe(de['refusal.vendorMergeBlockedDuplicate']);
		expect(localizeApiDetail(raw, deT)).not.toContain('payments-blocked');
	});
});

describe('why /cards/generate minted nothing, and the CSV near-duplicate rows', () => {
	const LOCALES = [
		['en', en],
		['de', de],
		['fr', fr],
		['es', es],
		['pt-BR', ptBR],
		['ja', ja]
	] as const;
	const backendSource = (rel: string) =>
		readFileSync(new URL(`../../../../backend/app/${rel}`, import.meta.url).pathname, 'utf8');

	it('states every code the backend defines — the reverse of the literal pin above', () => {
		const skipCodes = [
			...backendSource('services/card_issuance.py').matchAll(/"(card_skip_[a-z_]+)"/g)
		].map((x) => x[1]);
		const csvCodes = [
			...backendSource('services/csv_import.py').matchAll(/"(csv_invoice_number_[a-z_]+)"/g)
		].map((x) => x[1]);
		expect(skipCodes.length).toBeGreaterThan(10);
		expect(csvCodes).toHaveLength(3);
		for (const code of [...skipCodes, ...csvCodes]) {
			expect(CODED_REFUSAL_CODES, `no sentence for "${code}"`).toContain(code);
		}
	});

	it.each([
		['card_skip_not_found', {}],
		['card_skip_not_payable', { status: 'ready_for_review' }],
		['card_skip_vendor_changed', {}],
		['card_skip_lock_timeout', {}],
		['card_skip_already_carded', { card_id: 'c1' }],
		['card_skip_blocked', { exception_type: 'quality_hold' }],
		['card_skip_vendor_not_payable', { vendor_status: 'unverified' }],
		['card_skip_live_payment', {}],
		['card_skip_credit_conflict', {}],
		['card_skip_nothing_owed', {}],
		['card_skip_vendor_unscreenable', {}],
		['card_skip_compliance_hold', { verdict: 'hold' }],
		['card_skip_erp_bill', { erp_reason: 'erp_bill_not_open' }],
		['card_skip_issue_failed', {}]
	] as [string, Record<string, unknown>][])('%s is stated in every shipped locale', (code, params) => {
		for (const [locale, messages] of LOCALES) {
			// A skip is `coded_refusal`'s shape plus the invoice id.
			const out = localizeApiDetail(
				{ invoice_id: 'i1', ...detail(code, params) },
				translator(messages as Record<string, string>, locale)
			);
			expect(out, `${locale} ${code}`).toBeTruthy();
			expect(out, `${locale} ${code}`).not.toContain('{');
			expect(out).not.toBe('server English');
		}
	});

	it('names the invoice status, the exception type and the vendor status by their labels', () => {
		expect(codedRefusalText('card_skip_not_payable', { status: 'ready_for_review' }, enT)).toContain(
			`(this one is ${EN_ALL['invoices.status.readyForReview']})`
		);
		expect(
			codedRefusalText('card_skip_blocked', { exception_type: 'duplicate' }, enT)
		).toContain(EN_ALL['exceptions.type.duplicate']);
		expect(
			codedRefusalText('card_skip_vendor_not_payable', { vendor_status: 'unverified' }, deT)
		).toContain(de['vendors.status.unverified']);
		// A status this build has no label for is named verbatim, never blanked.
		expect(
			codedRefusalText('card_skip_vendor_not_payable', { vendor_status: 'frozen' }, enT)
		).toContain('frozen');
		// A missing param falls back to the server's sentence.
		expect(codedRefusalText('card_skip_blocked', {}, enT)).toBeNull();
		expect(codedRefusalText('card_skip_not_payable', {}, enT)).toBeNull();
	});

	it('states a CSV row that matches an existing invoice, naming both numbers', () => {
		const row = {
			row: 3,
			message: 'server English',
			code: 'csv_invoice_number_matches_existing',
			params: { invoice_number: 'INV-1', existing_invoice_id: 'x', existing_invoice_number: 'INV-001' }
		};
		expect(localizeApiDetail(row, deT)).toBe(
			interpolate(
				de['refusal.csvInvoiceNumberMatchesExisting'],
				{ number: 'INV-1', existing: 'INV-001' },
				'de'
			)
		);
	});

	it('states a match in another entity without naming that entity’s invoice (§40)', () => {
		for (const [locale, messages] of LOCALES) {
			const out = codedRefusalText(
				'csv_invoice_number_matches_other_entity',
				{ invoice_number: 'INV-1' },
				translator(messages as Record<string, string>, locale)
			);
			expect(out, locale).toContain('INV-1');
			expect(out, locale).not.toContain('{');
		}
		expect(codedRefusalText('csv_invoice_number_matches_other_entity', {}, enT)).toBeNull();
	});

	it('states a CSV row that matches an earlier row of the file, naming that row', () => {
		const out = localizeApiDetail(
			{
				row: 3,
				message: 'server English',
				code: 'csv_invoice_number_matches_row',
				params: { invoice_number: '2026 7', other_row: 2, other_invoice_number: '2026-0007' }
			},
			enT
		);
		expect(out).toBe(
			'Invoice number 2026 7 is the same as 2026-0007 on row 2 for this vendor once formatting ' +
				'is ignored, so it was not imported.'
		);
		// A row number that did not arrive as a number falls back.
		expect(
			codedRefusalText(
				'csv_invoice_number_matches_row',
				{ invoice_number: 'a', other_row: '2', other_invoice_number: 'b' },
				enT
			)
		).toBeNull();
	});
});
