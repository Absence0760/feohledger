/**
 * Localizing a refused ERP push — what the invoice modal's ERP panel says when
 * a send was refused before (or right after) it reached the ERP.
 *
 * A refusal lands on the append-only `invoice.erp_failed` audit row twice: as
 * the server's English (`details.error`, e.g. "QuickBooks Online post refused:
 * vendor_not_linked") and as `details.refusal = {code, params}`
 * (`backend/app/services/erp.py::erp_refusal_record`). The code is one of
 * `backend/app/services/erp_adapters/base.py::ERP_REFUSAL_CODES`; `params`
 * carries the adapter's `provider` literal and, for `account_not_linked` /
 * `account_inactive`, the GL `accounts` concerned. A refusal QuickBooks itself
 * answered with carries Intuit's numeric `intuit_code` too, and the sentences
 * for an unmapped or unattributed fault name it, so it can be looked up. This module states the code
 * in the reader's language AND says what to do about it — the English string
 * is neither, it names a reason code to a person who cannot act on one.
 *
 * **Anything this build cannot state → `null`**, and the caller shows the
 * server's sentence: an unknown code (a newer backend), a row written before
 * refusals were coded (no `refusal`), a malformed param.
 *
 * `erpRefusals.test.ts` reads `ERP_REFUSAL_CODES` out of the backend source and
 * fails when a code there has no message here, or one here is not there.
 *
 * Imports nothing that reaches `$app/*`; the caller passes `m`.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';
import { formatList } from '#lib/utils/list.ts';
import { exceptionTypeFallback, exceptionTypeLabelKey } from '#lib/types/exception.ts';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;
type Params = Record<string, unknown>;

/** An open duplicate / fraud exception refused the push before any adapter ran
 *  (`backend/app/services/erp_export_gate.py`, decisions §286). It names the
 *  exception TYPE, not a provider, and carries `possibly_posted: true` when an
 *  earlier attempt may have created the bill anyway. The same code is the 409
 *  the send doors answer with, which `codedRefusals.ts` states through this
 *  module. */
export const EXCEPTION_BLOCKS_ERP_EXPORT = 'exception_blocks_erp_export';

/** FeohLedger is already paying the invoice — a live payment or virtual card —
 *  so the push was refused before any adapter ran (same module, decisions
 *  §309). `params.claim` names the rail (`payment` / `virtual_card`), and
 *  `possibly_posted: true` appears as on the exception refusal. Also a 409 the
 *  send doors answer with. */
export const PAYMENT_BLOCKS_ERP_EXPORT = 'payment_blocks_erp_export';

/** The ERP's shared API quota stopped the call before it was made
 *  (`backend/app/services/erp_call_budget.py`, decisions §322): a push's
 *  `invoice.erp_failed` refusal, and the 429 a chart / vendor / PO sync answers
 *  (`codedRefusals.ts`). `params.reason` says whose allowance ran out. */
export const ERP_CALL_BUDGET_EXHAUSTED = 'erp_call_budget_exhausted';

/** The sentence per `reason`. */
const CALL_BUDGET_KEYS: Record<string, MessageKey> = {
	tenant_budget: 'erpRefusal.callBudgetTenant',
	subscription_quota: 'erpRefusal.callBudgetQuota',
	rate_limited: 'erpRefusal.callBudgetRateLimited'
};

/** The sentence per `claim`, plain and possibly-posted. */
const CLAIM_KEYS: Record<string, { plain: MessageKey; posted: MessageKey }> = {
	payment: {
		plain: 'erpRefusal.paymentBlocksExport',
		posted: 'erpRefusal.paymentBlocksExportPossiblyPosted'
	},
	virtual_card: {
		plain: 'erpRefusal.cardBlocksExport',
		posted: 'erpRefusal.cardBlocksExportPossiblyPosted'
	}
};

/** The refusals raised outside an adapter, which name no provider. */
const PROVIDERLESS = new Set([
	'not_connected',
	'erp_credentials_unavailable',
	'invoice_due_before_date'
]);

/** Every code's sentence, each naming `{provider}` unless in `PROVIDERLESS`. */
const KEYS: Record<string, MessageKey> = {
	vendor_not_linked: 'erpRefusal.vendorNotLinked',
	account_not_linked: 'erpRefusal.accountNotLinked',
	not_connected: 'erpRefusal.notConnected',
	invoice_due_before_date: 'erpRefusal.invoiceDueBeforeDate',
	amount_mismatch: 'erpRefusal.amountMismatch',
	line_amount_missing: 'erpRefusal.lineAmountMissing',
	posted_total_mismatch: 'erpRefusal.postedTotalMismatch',
	posted_total_unconfirmed: 'erpRefusal.postedTotalUnconfirmed',
	previous_bill_removed: 'erpRefusal.previousBillRemoved',
	tax_not_itemised: 'erpRefusal.taxNotItemised',
	tax_rate_unresolved: 'erpRefusal.taxRateUnresolved',
	missing_dates: 'erpRefusal.missingDates',
	duplicate_document_number: 'erpRefusal.duplicateNumber',
	duplicate_invoice_number: 'erpRefusal.duplicateNumber',
	doc_number_too_long: 'erpRefusal.docNumberTooLong',
	currency_unknown: 'erpRefusal.currencyUnknown',
	currency_not_enabled: 'erpRefusal.currencyNotEnabled',
	period_closed: 'erpRefusal.periodClosed',
	vendor_inactive: 'erpRefusal.vendorInactive',
	account_inactive: 'erpRefusal.accountInactive',
	reference_invalid: 'erpRefusal.referenceInvalid',
	company_subscription_inactive: 'erpRefusal.companySubscriptionInactive',
	intuit_fault: 'erpRefusal.intuitFault',
	idempotency_lookup_incomplete: 'erpRefusal.idempotencyLookupIncomplete',
	existing_invoice_not_open: 'erpRefusal.existingInvoiceNotOpen',
	line_amount_precision: 'erpRefusal.lineAmountPrecision',
	currency_not_supported: 'erpRefusal.currencyNotSupported',
	foreign_currency_supplier: 'erpRefusal.foreignCurrencySupplier',
	correlation_total_mismatch: 'erpRefusal.correlationTotalMismatch',
	tax_type_not_resolved: 'erpRefusal.taxTypeNotResolved',
	tax_mismatch: 'erpRefusal.taxMismatch',
	tax_not_stated: 'erpRefusal.taxNotStated',
	subscription_key_missing: 'erpRefusal.subscriptionKeyMissing',
	ap_account_not_configured: 'erpRefusal.apAccountNotConfigured',
	currency_not_configured: 'erpRefusal.currencyNotConfigured',
	currency_mismatch: 'erpRefusal.currencyMismatch',
	invoice_date_missing: 'erpRefusal.invoiceDateMissing',
	due_date_missing: 'erpRefusal.dueDateMissing',
	amount_not_positive: 'erpRefusal.amountNotPositive',
	job_unconfirmed: 'erpRefusal.jobUnconfirmed',
	job_failed: 'erpRefusal.jobFailed',
	[ERP_CALL_BUDGET_EXHAUSTED]: 'erpRefusal.callBudgetTenant',
	erp_credentials_unavailable: 'erpRefusal.credentialsUnavailable',
	[EXCEPTION_BLOCKS_ERP_EXPORT]: 'erpRefusal.exceptionBlocksExport',
	[PAYMENT_BLOCKS_ERP_EXPORT]: 'erpRefusal.paymentBlocksExport',
	// A FeohLedger payment written back to the ERP (decisions §295)
	payment_account_not_mapped: 'erpRefusal.paymentAccountNotMapped',
	bill_not_in_erp: 'erpRefusal.billNotInErp',
	payment_exceeds_balance: 'erpRefusal.paymentExceedsBalance',
	bill_payment_voided: 'erpRefusal.billPaymentVoided'
};

/** The sentences that name Intuit's numeric fault code as `{code}`. */
const NAMES_INTUIT_CODE = new Set(['reference_invalid', 'intuit_fault']);

/** The codes this build states (pinned against the backend registry). */
export const ERP_REFUSAL_CODES = Object.keys(KEYS);

/** A `{code, params}` refusal as `invoice.erp_failed` records it, or null. */
export function parseErpRefusal(value: unknown): { code: string; params: Params } | null {
	if (!value || typeof value !== 'object') return null;
	const { code, params } = value as Params;
	if (typeof code !== 'string' || !code) return null;
	return {
		code,
		params: params && typeof params === 'object' && !Array.isArray(params) ? (params as Params) : {}
	};
}

/**
 * The localized, actionable sentence for a recorded refusal, or `null` — then
 * the caller renders the server's English. Never throws.
 */
export function erpRefusalText(value: unknown, t: Translate): string | null {
	const refusal = parseErpRefusal(value);
	if (!refusal) return null;
	const key = KEYS[refusal.code];
	if (!key) return null;
	if (PROVIDERLESS.has(refusal.code)) return t(key);
	if (refusal.code === EXCEPTION_BLOCKS_ERP_EXPORT) {
		const type = refusal.params.exception_type;
		if (typeof type !== 'string' || !type.trim()) return null;
		const label = exceptionTypeLabelKey(type);
		// An earlier attempt timed out or failed in a way the ERP may still
		// have applied (`possibly_posted`), so the bill may already be there,
		// payable from the ERP: say so, and where to look first.
		const sentence: MessageKey =
			refusal.params.possibly_posted === true
				? 'erpRefusal.exceptionBlocksExportPossiblyPosted'
				: key;
		return t(sentence, { type: label ? t(label) : exceptionTypeFallback(type) });
	}
	if (refusal.code === PAYMENT_BLOCKS_ERP_EXPORT) {
		const claim = refusal.params.claim;
		const keys = typeof claim === 'string' ? CLAIM_KEYS[claim] : undefined;
		if (!keys) return null;
		return t(refusal.params.possibly_posted === true ? keys.posted : keys.plain);
	}
	// The adapter's fixed literal ("QuickBooks Online"). Every other sentence
	// names it, so a refusal recorded without one is stated by the server.
	const provider = refusal.params.provider;
	if (typeof provider !== 'string' || !provider.trim()) return null;

	if (refusal.code === 'account_not_linked') {
		const accounts = refusal.params.accounts;
		if (!Array.isArray(accounts) || !accounts.every((a) => typeof a === 'string' && a)) {
			return null;
		}
		if (accounts.length === 0) return t('erpRefusal.accountNotLinkedUncoded', { provider });
		return t(key, { provider, accounts: formatList(accounts as string[]) });
	}
	if (refusal.code === 'account_inactive') {
		const accounts = refusal.params.accounts;
		if (
			!Array.isArray(accounts) ||
			accounts.length === 0 ||
			!accounts.every((a) => typeof a === 'string' && a)
		) {
			return null;
		}
		return t(key, { provider, accounts: formatList(accounts as string[]) });
	}
	if (refusal.code === ERP_CALL_BUDGET_EXHAUSTED) {
		const reason = refusal.params.reason;
		const sentence = typeof reason === 'string' ? CALL_BUDGET_KEYS[reason] : undefined;
		return sentence ? t(sentence, { provider }) : null;
	}
	if (NAMES_INTUIT_CODE.has(refusal.code)) {
		const code = refusal.params.intuit_code;
		if (typeof code !== 'string' || !/^\d{1,8}$/.test(code)) return null;
		return t(key, { provider, code });
	}
	return t(key, { provider });
}
