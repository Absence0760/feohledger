/**
 * Localizing a refused ERP push — what the invoice modal's ERP panel says when
 * a send was refused before (or right after) it reached the ERP.
 *
 * A refusal lands on the append-only `invoice.erp_failed` audit row twice: as
 * the server's English (`details.error`, e.g. "QuickBooks Online post refused:
 * vendor_not_linked") and as `details.refusal = {code, params}`
 * (`backend/app/services/erp.py::erp_refusal_record`). The code is one of
 * `backend/app/services/erp_adapters/base.py::ERP_REFUSAL_CODES`; `params`
 * carries the adapter's `provider` literal and, for `account_not_linked`, the
 * GL `accounts` that resolved to no ERP account. This module states the code
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

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;
type Params = Record<string, unknown>;

/** The refusals raised outside an adapter, which name no provider. */
const PROVIDERLESS = new Set(['not_connected', 'erp_credentials_unavailable']);

/** Every code's sentence, each naming `{provider}` unless in `PROVIDERLESS`. */
const KEYS: Record<string, MessageKey> = {
	vendor_not_linked: 'erpRefusal.vendorNotLinked',
	account_not_linked: 'erpRefusal.accountNotLinked',
	not_connected: 'erpRefusal.notConnected',
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
	erp_credentials_unavailable: 'erpRefusal.credentialsUnavailable'
};

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
	return t(key, { provider });
}
