// GENERATED FILE — do not edit by hand.
//
// Source of truth: backend/app/services/invoice_warning_catalog.py
// Regenerate:      pnpm gen:warning-messages
// Drift check:     pnpm check:warning-messages  (runs in CI)
//
// Maps every `InvoiceWarning.code` the backend can emit to the message
// key that states it in the reader's language, plus what each of that
// message's parameters IS so the client can format it for the locale.
// decisions.md §155 shipped a localized frame around server-English
// findings and named this as the fix: a label per `type` could not work,
// because one type is up to five different sentences.
import type { MessageKey } from '$lib/i18n/messages';

/** What a warning parameter holds, and therefore how it renders. */
export type WarningParamKind =
	| 'text'
	| 'currency'
	| 'money'
	| 'number'
	| 'percent'
	| 'count'
	| 'date';

/** Warning code → the localized sentence that states it. */
export const INVOICE_WARNING_MESSAGE_KEYS = {
	// Missing vendor name
	'missing_vendor_name': 'invoices.warning.missingVendorName',
	// Missing invoice number
	'missing_invoice_number': 'invoices.warning.missingInvoiceNumber',
	// Missing or zero amount
	'missing_amount': 'invoices.warning.missingAmount',
	// Duplicate invoice number for this vendor
	'duplicate_invoice_number': 'invoices.warning.duplicateInvoiceNumber',
	// Potential duplicate: {similarity}% match to {invoiceNumber} from {vendorName}{crossEntityCount, plural, =0 {} one { (plus # near-identical invoice under another entity)} other { (plus # near-identical invoices under another entity)}}
	'duplicate_similar': 'invoices.warning.duplicateSimilar',
	// Potential duplicate: {similarity}% match to {invoiceNumber}{crossEntityCount, plural, =0 {} one { (plus # near-identical invoice under another entity)} other { (plus # near-identical invoices under another entity)}}
	'duplicate_similar_unnamed_vendor': 'invoices.warning.duplicateSimilarUnnamedVendor',
	// Potential duplicate: {similarity}% match to another invoice from {vendorName}{crossEntityCount, plural, =0 {} one { (plus # near-identical invoice under another entity)} other { (plus # near-identical invoices under another entity)}}
	'duplicate_similar_unnumbered': 'invoices.warning.duplicateSimilarUnnumbered',
	// Potential duplicate: {similarity}% match to another invoice{crossEntityCount, plural, =0 {} one { (plus # near-identical invoice under another entity)} other { (plus # near-identical invoices under another entity)}}
	'duplicate_similar_unnumbered_unnamed_vendor': 'invoices.warning.duplicateSimilarUnnumberedUnnamedVendor',
	// Potential duplicate: {similarity}% match to a near-identical invoice under another entity
	'duplicate_similar_cross_entity': 'invoices.warning.duplicateSimilarCrossEntity',
	// Round amount: {amount} {currency}
	'round_amount': 'invoices.warning.roundAmount',
	// Invoice date is in the future
	'future_invoice_date': 'invoices.warning.futureInvoiceDate',
	// Rush payment: due within {days, plural, one {# day} other {# days}} of the invoice date
	'rush_payment': 'invoices.warning.rushPayment',
	// Invoice is past due
	'past_due': 'invoices.warning.pastDue',
	// Vendor is unverified
	'unverified_vendor': 'invoices.warning.unverifiedVendor',
	// Vendor email uses personal domain: {domain}
	'personal_email_domain': 'invoices.warning.personalEmailDomain',
	// New vendor (created {days, plural, one {# day} other {# days}} ago) submitting large invoice {amount} {currency}
	'new_vendor_large_amount': 'invoices.warning.newVendorLargeAmount',
	// Remit-to address changed since the last approved invoice for this vendor
	'remit_to_changed': 'invoices.warning.remitToChanged',
	// Amount {amount} {currency} is {sigma}σ above this vendor's historical mean ({mean} {currency})
	'amount_above_vendor_mean': 'invoices.warning.amountAboveVendorMean',
	// AI-flagged anomaly: {reason}
	'llm_anomaly': 'invoices.warning.llmAnomaly',
	// Line items total {lineItemsTotal} {currency} but the invoice amount is {headerAmount} {currency}
	'line_total_mismatch': 'invoices.warning.lineTotalMismatch',
	// Unit price {deltaPct}% over this vendor's baseline for {item} ({unitPrice} {currency} vs {baselineUnitPrice} {currency})
	'price_variance_over': 'invoices.warning.priceVarianceOver',
	// Unit price {deltaPct}% under this vendor's baseline for {item} ({unitPrice} {currency} vs {baselineUnitPrice} {currency})
	'price_variance_under': 'invoices.warning.priceVarianceUnder',
	// PO {poNumber} not found
	'po_not_found': 'invoices.warning.poNotFound',
	// Amount variance {variancePct}% vs PO {poNumber} (invoice {invoiceAmount} {currency} vs PO {poTotal} {currency})
	'po_amount_variance': 'invoices.warning.poAmountVariance',
	// Amount variance {variancePct}% vs PO {poNumber}, which records no currency (invoice {invoiceAmount} {currency} vs PO {poTotal})
	'po_amount_variance_po_currency_unknown': 'invoices.warning.poAmountVariancePoCurrencyUnknown',
	// Invoice is in {invoiceCurrency} but PO {poNumber} is in {poCurrency} — the amounts were not compared
	'po_currency_mismatch': 'invoices.warning.poCurrencyMismatch',
	// Partial 3-way match — {matchType} match against PO {poNumber}, but only part of the ordered quantity has been received
	'po_partial_receipt': 'invoices.warning.poPartialReceipt',
	// Over-receipt: {receivedQuantity} received against {orderedQuantity} ordered (+{excessQuantity}) on PO {poNumber}
	'po_over_receipt': 'invoices.warning.poOverReceipt',
	// More goods received than ordered on PO {poNumber}
	'po_over_receipt_unquantified': 'invoices.warning.poOverReceiptUnquantified',
	// Failed quality inspection for PO {poNumber}
	'quality_inspection_failed': 'invoices.warning.qualityInspectionFailed',
	// Failed quality inspection for PO {poNumber}: {notes}
	'quality_inspection_failed_notes': 'invoices.warning.qualityInspectionFailedNotes',
	// Quality inspection required but missing for PO {poNumber}
	'quality_inspection_missing': 'invoices.warning.qualityInspectionMissing',
	// Partial acceptance: {acceptedQuantity} of the ordered quantity accepted on PO {poNumber}
	'quality_partial_acceptance': 'invoices.warning.qualityPartialAcceptance',
	// Partial quality acceptance on inspection for PO {poNumber}
	'quality_partial_acceptance_unquantified': 'invoices.warning.qualityPartialAcceptanceUnquantified',
	// Amount {amount} {currency} is {deltaPct}% over the recurring template “{templateName}” expected amount {expectedAmount} {currency}
	'recurring_variance_over': 'invoices.warning.recurringVarianceOver',
	// Amount {amount} {currency} is {deltaPct}% under the recurring template “{templateName}” expected amount {expectedAmount} {currency}
	'recurring_variance_under': 'invoices.warning.recurringVarianceUnder',
	// Invoice dated {invoiceDate} is after contract {contractNumber} expired ({endDate})
	'contract_expired': 'invoices.warning.contractExpired',
	// Invoice dated {invoiceDate} predates contract {contractNumber} start ({startDate})
	'contract_not_started': 'invoices.warning.contractNotStarted',
	// Spend recorded against terminated contract {contractNumber}
	'contract_terminated': 'invoices.warning.contractTerminated',
	// Spend recorded against cancelled contract {contractNumber}
	'contract_cancelled': 'invoices.warning.contractCancelled',
	// Invoice vendor does not match contract {contractNumber} vendor
	'contract_vendor_mismatch': 'invoices.warning.contractVendorMismatch',
	// Cumulative spend {cumulativeSpend} {currency} exceeds contract {contractNumber} limit {spendLimit} {currency}
	'contract_spend_limit_exceeded': 'invoices.warning.contractSpendLimitExceeded',
	// Cumulative spend {cumulativeSpend} {currency} exceeds contract {contractNumber} limit {spendLimit} {currency} (not-to-exceed)
	'contract_spend_limit_exceeded_not_to_exceed': 'invoices.warning.contractSpendLimitExceededNotToExceed',
	// GL account {glAccount} is outside contract {contractNumber} allowed accounts
	'contract_gl_not_allowed': 'invoices.warning.contractGlNotAllowed',
	// Amounts don't add up: subtotal ({subtotal}) + tax ({tax}) + shipping ({shipping}) − discount ({discount}) = {expected}, but total is {amount}.
	'self_correction_total_reconciliation': 'invoices.warning.selfCorrectionTotalReconciliation',
	// Due date ({dueDate}) is before invoice date ({invoiceDate}).
	'self_correction_date_ordering': 'invoices.warning.selfCorrectionDateOrdering',
	// Line items total ({lineItemsTotal}) doesn't match invoice amount ({amount}).
	'self_correction_line_items_sum': 'invoices.warning.selfCorrectionLineItemsSum',
	// Line {lineNumber}: {quantity} × {unitPrice} = {expected}, but total is {total}.
	'self_correction_line_item_math': 'invoices.warning.selfCorrectionLineItemMath',
	// AI suggested GL code(s) not in active chart: {codes}
	'gl_codes_not_in_chart': 'invoices.warning.glCodesNotInChart',
	// Cached vendor GL code '{code}' is no longer in the active chart of accounts.
	'gl_code_stale_prior': 'invoices.warning.glCodeStalePrior',
} as const satisfies Record<string, MessageKey>;

/**
 * Warning code → each of its parameters' kind.
 *
 * The English FALLBACK (`InvoiceWarning.message`, rendered by the backend)
 * spells the currency code and the `%` out, because nothing downstream will
 * format them. The localized value leaves both to the client formatters, so
 * the two English strings are deliberately not identical.
 */
export const INVOICE_WARNING_PARAM_KINDS = {
	'missing_vendor_name': {},
	'missing_invoice_number': {},
	'missing_amount': {},
	'duplicate_invoice_number': {},
	'duplicate_similar': { similarity: 'percent', invoiceNumber: 'text', vendorName: 'text', crossEntityCount: 'count' },
	'duplicate_similar_unnamed_vendor': { similarity: 'percent', invoiceNumber: 'text', crossEntityCount: 'count' },
	'duplicate_similar_unnumbered': { similarity: 'percent', vendorName: 'text', crossEntityCount: 'count' },
	'duplicate_similar_unnumbered_unnamed_vendor': { similarity: 'percent', crossEntityCount: 'count' },
	'duplicate_similar_cross_entity': { similarity: 'percent' },
	'round_amount': { amount: 'money', currency: 'currency' },
	'future_invoice_date': {},
	'rush_payment': { days: 'count' },
	'past_due': {},
	'unverified_vendor': {},
	'personal_email_domain': { domain: 'text' },
	'new_vendor_large_amount': { days: 'count', amount: 'money', currency: 'currency' },
	'remit_to_changed': {},
	'amount_above_vendor_mean': { amount: 'money', sigma: 'number', mean: 'money', currency: 'currency' },
	'llm_anomaly': { reason: 'text' },
	'line_total_mismatch': { lineItemsTotal: 'money', headerAmount: 'money', currency: 'currency' },
	'price_variance_over': { deltaPct: 'percent', item: 'text', unitPrice: 'money', baselineUnitPrice: 'money', currency: 'currency' },
	'price_variance_under': { deltaPct: 'percent', item: 'text', unitPrice: 'money', baselineUnitPrice: 'money', currency: 'currency' },
	'po_not_found': { poNumber: 'text' },
	'po_amount_variance': { variancePct: 'percent', poNumber: 'text', invoiceAmount: 'money', poTotal: 'money', currency: 'currency' },
	'po_amount_variance_po_currency_unknown': { variancePct: 'percent', poNumber: 'text', invoiceAmount: 'money', poTotal: 'number', currency: 'currency' },
	'po_currency_mismatch': { invoiceCurrency: 'text', poNumber: 'text', poCurrency: 'text' },
	'po_partial_receipt': { matchType: 'text', poNumber: 'text' },
	'po_over_receipt': { receivedQuantity: 'number', orderedQuantity: 'number', excessQuantity: 'number', poNumber: 'text' },
	'po_over_receipt_unquantified': { poNumber: 'text' },
	'quality_inspection_failed': { poNumber: 'text' },
	'quality_inspection_failed_notes': { poNumber: 'text', notes: 'text' },
	'quality_inspection_missing': { poNumber: 'text' },
	'quality_partial_acceptance': { acceptedQuantity: 'number', poNumber: 'text' },
	'quality_partial_acceptance_unquantified': { poNumber: 'text' },
	'recurring_variance_over': { amount: 'money', deltaPct: 'percent', templateName: 'text', expectedAmount: 'money', currency: 'currency' },
	'recurring_variance_under': { amount: 'money', deltaPct: 'percent', templateName: 'text', expectedAmount: 'money', currency: 'currency' },
	'contract_expired': { invoiceDate: 'date', contractNumber: 'text', endDate: 'date' },
	'contract_not_started': { invoiceDate: 'date', contractNumber: 'text', startDate: 'date' },
	'contract_terminated': { contractNumber: 'text' },
	'contract_cancelled': { contractNumber: 'text' },
	'contract_vendor_mismatch': { contractNumber: 'text' },
	'contract_spend_limit_exceeded': { cumulativeSpend: 'money', contractNumber: 'text', spendLimit: 'money', currency: 'currency' },
	'contract_spend_limit_exceeded_not_to_exceed': { cumulativeSpend: 'money', contractNumber: 'text', spendLimit: 'money', currency: 'currency' },
	'contract_gl_not_allowed': { glAccount: 'text', contractNumber: 'text' },
	'self_correction_total_reconciliation': { subtotal: 'number', tax: 'number', shipping: 'number', discount: 'number', expected: 'number', amount: 'number' },
	'self_correction_date_ordering': { dueDate: 'date', invoiceDate: 'date' },
	'self_correction_line_items_sum': { lineItemsTotal: 'number', amount: 'number' },
	'self_correction_line_item_math': { lineNumber: 'count', quantity: 'number', unitPrice: 'number', expected: 'number', total: 'number' },
	'gl_codes_not_in_chart': { codes: 'text' },
	'gl_code_stale_prior': { code: 'text' },
} as const satisfies Record<
	keyof typeof INVOICE_WARNING_MESSAGE_KEYS,
	Record<string, WarningParamKind>
>;
