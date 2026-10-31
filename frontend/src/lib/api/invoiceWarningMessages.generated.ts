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
//
// Three families share the map, each namespaced in its wire code: the
// warnings themselves, the PO-match panel's `po_match.issue.*` entries,
// and the `exception.*` descriptions no warning states (an exception that
// mirrors a warning carries the warning's own code).
import type { MessageKey } from '#lib/i18n/messages.ts';

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
	// AI reading is paused: this month's {included, plural, one {# AI-read invoice is} other {# AI-read invoices are}} used. Enter the details manually, or upgrade your plan under Settings → Subscription.
	'ai_allowance_reached': 'invoices.warning.aiAllowanceReached',
	// AI reading is paused: this month's AI spending cap is reached. Enter the details manually, or raise the cap under Settings → Subscription.
	'ai_spend_cap_reached': 'invoices.warning.aiSpendCapReached',
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
	// PO {poNumber} is cancelled — the invoice was not matched against it
	'po_cancelled': 'invoices.warning.poCancelled',
	// The total of PO {poNumber} could not be confirmed from the ERP on its last sync — the amounts were not compared
	'po_total_unverifiable': 'invoices.warning.poTotalUnverifiable',
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
	// Amounts are printed in {printedCurrency}, but the invoice currency reads {fieldCurrency}. The amounts were left blank — enter them from the document.
	'self_correction_currency_conflict': 'invoices.warning.selfCorrectionCurrencyConflict',
	// Amounts are printed in more than one currency ({printedCurrencies}). The amounts were left blank — enter them from the document.
	'self_correction_currency_mixed': 'invoices.warning.selfCorrectionCurrencyMixed',
	// Line {lineNumber}: {quantity} × {unitPrice} = {expected}, but total is {total}.
	'self_correction_line_item_math': 'invoices.warning.selfCorrectionLineItemMath',
	// AI suggested GL code(s) not in active chart: {codes}
	'gl_codes_not_in_chart': 'invoices.warning.glCodesNotInChart',
	// Cached vendor GL code '{code}' is no longer in the active chart of accounts.
	'gl_code_stale_prior': 'invoices.warning.glCodeStalePrior',
	// PO {poNumber} not found
	'po_match.issue.po_not_found': 'invoices.poMatch.issue.poNotFound',
	// Currency mismatch: invoice in {invoiceCurrency}, PO in {poCurrency} — amounts not compared
	'po_match.issue.currency_mismatch': 'invoices.poMatch.issue.currencyMismatch',
	// PO is cancelled — amounts not compared
	'po_match.issue.po_cancelled': 'invoices.poMatch.issue.poCancelled',
	// PO total not confirmed by the ERP on its last sync — amounts not compared
	'po_match.issue.po_total_unverifiable': 'invoices.poMatch.issue.poTotalUnverifiable',
	// Amount mismatch: invoice {invoiceAmount} {currency} vs PO {poTotal} {currency} ({variancePct}%)
	'po_match.issue.amount_mismatch': 'invoices.poMatch.issue.amountMismatch',
	// Amount mismatch: invoice {invoiceAmount} {currency} vs PO {poTotal} ({variancePct}%)
	'po_match.issue.amount_mismatch_po_currency_unknown': 'invoices.poMatch.issue.amountMismatchPoCurrencyUnknown',
	// Amount mismatch: invoice {invoiceAmount} vs PO {poTotal} ({variancePct}%)
	'po_match.issue.amount_mismatch_currency_unknown': 'invoices.poMatch.issue.amountMismatchCurrencyUnknown',
	// Partial receipt: {receivedPct}% of ordered quantity received
	'po_match.issue.partial_receipt': 'invoices.poMatch.issue.partialReceipt',
	// Over-receipt: {receivedQuantity} received against {orderedQuantity} ordered (+{excessQuantity})
	'po_match.issue.over_receipt': 'invoices.poMatch.issue.overReceipt',
	// Failed quality inspection
	'po_match.issue.inspection_failed': 'invoices.poMatch.issue.inspectionFailed',
	// Failed quality inspection: {notes}
	'po_match.issue.inspection_failed_notes': 'invoices.poMatch.issue.inspectionFailedNotes',
	// Partial acceptance: {acceptedQuantity} of ordered quantity accepted
	'po_match.issue.partial_acceptance': 'invoices.poMatch.issue.partialAcceptance',
	// Partial acceptance: part of ordered quantity accepted
	'po_match.issue.partial_acceptance_unquantified': 'invoices.poMatch.issue.partialAcceptanceUnquantified',
	// Quality inspection required but missing
	'po_match.issue.inspection_required_missing': 'invoices.poMatch.issue.inspectionRequiredMissing',
	// Required fields missing after extraction
	'exception.missing_data_after_extraction': 'exceptions.description.missingDataAfterExtraction',
	// Line-item price variance vs vendor history on {count, plural, one {# line} other {# lines}}
	'exception.price_variance_findings': 'exceptions.description.priceVarianceFindings',
	// {count, plural, one {# contract-compliance finding} other {# contract-compliance findings}}
	'exception.contract_noncompliant_findings': 'exceptions.description.contractNoncompliantFindings',
	// {erp} reports this bill paid, but FeohLedger couldn't record that payment because another exception on this invoice is blocking payment. Clear that exception and FeohLedger records the payment automatically.
	'exception.erp_paid_unrecorded_blocking_exception': 'exceptions.description.erpPaidUnrecordedBlockingException',
	// Your ERP reports this bill paid, but FeohLedger couldn't record that payment because another exception on this invoice is blocking payment. Clear that exception and FeohLedger records the payment automatically.
	'exception.erp_paid_unrecorded_blocking_exception_unnamed': 'exceptions.description.erpPaidUnrecordedBlockingExceptionUnnamed',
	// {erp} reports this bill paid, but FeohLedger couldn't record that payment because the invoice is also in a FeohLedger payment run, so the supplier could be paid twice. Take it out of the run or cancel the run, then record the ERP's payment from this exception.
	'exception.erp_paid_unrecorded_in_run': 'exceptions.description.erpPaidUnrecordedInRun',
	// Your ERP reports this bill paid, but FeohLedger couldn't record that payment because the invoice is also in a FeohLedger payment run, so the supplier could be paid twice. Take it out of the run or cancel the run, then record the ERP's payment from this exception.
	'exception.erp_paid_unrecorded_in_run_unnamed': 'exceptions.description.erpPaidUnrecordedInRunUnnamed',
	// {erp} reports this bill paid, but FeohLedger couldn't record that payment because FeohLedger has its own payment in progress for this invoice, so the supplier could be paid twice. Void that payment if the ERP's payment is the real one, then record the ERP's payment from this exception.
	'exception.erp_paid_unrecorded_payment_live': 'exceptions.description.erpPaidUnrecordedPaymentLive',
	// Your ERP reports this bill paid, but FeohLedger couldn't record that payment because FeohLedger has its own payment in progress for this invoice, so the supplier could be paid twice. Void that payment if the ERP's payment is the real one, then record the ERP's payment from this exception.
	'exception.erp_paid_unrecorded_payment_live_unnamed': 'exceptions.description.erpPaidUnrecordedPaymentLiveUnnamed',
	// {erp} reports this bill paid, but FeohLedger couldn't record that payment because a virtual card is still live for this invoice, so the supplier could be paid twice. Cancel the card, then record the ERP's payment from this exception.
	'exception.erp_paid_unrecorded_card_live': 'exceptions.description.erpPaidUnrecordedCardLive',
	// Your ERP reports this bill paid, but FeohLedger couldn't record that payment because a virtual card is still live for this invoice, so the supplier could be paid twice. Cancel the card, then record the ERP's payment from this exception.
	'exception.erp_paid_unrecorded_card_live_unnamed': 'exceptions.description.erpPaidUnrecordedCardLiveUnnamed',
	// {erp} reports this bill paid, but FeohLedger couldn't record that payment because an applied credit memo no longer matches this invoice's supplier or currency, so the amount owed can't be stated. Correct the credit memo, then record the ERP's payment from this exception.
	'exception.erp_paid_unrecorded_credit_conflict': 'exceptions.description.erpPaidUnrecordedCreditConflict',
	// Your ERP reports this bill paid, but FeohLedger couldn't record that payment because an applied credit memo no longer matches this invoice's supplier or currency, so the amount owed can't be stated. Correct the credit memo, then record the ERP's payment from this exception.
	'exception.erp_paid_unrecorded_credit_conflict_unnamed': 'exceptions.description.erpPaidUnrecordedCreditConflictUnnamed',
	// {erp} reports this bill paid, but FeohLedger couldn't record that payment because credit memos already cover the whole invoice, so there is no payment to record. Check the bill in the ERP, then resolve this exception.
	'exception.erp_paid_unrecorded_nothing_to_pay': 'exceptions.description.erpPaidUnrecordedNothingToPay',
	// Your ERP reports this bill paid, but FeohLedger couldn't record that payment because credit memos already cover the whole invoice, so there is no payment to record. Check the bill in the ERP, then resolve this exception.
	'exception.erp_paid_unrecorded_nothing_to_pay_unnamed': 'exceptions.description.erpPaidUnrecordedNothingToPayUnnamed',
	// {erp} reports this bill paid, but FeohLedger couldn't record that payment because the invoice is not at a status that can be marked paid. Check the invoice, then record the ERP's payment from this exception or resolve it.
	'exception.erp_paid_unrecorded_not_payable': 'exceptions.description.erpPaidUnrecordedNotPayable',
	// Your ERP reports this bill paid, but FeohLedger couldn't record that payment because the invoice is not at a status that can be marked paid. Check the invoice, then record the ERP's payment from this exception or resolve it.
	'exception.erp_paid_unrecorded_not_payable_unnamed': 'exceptions.description.erpPaidUnrecordedNotPayableUnnamed',
	// {erp} reports this bill paid, but FeohLedger couldn't record that payment because FeohLedger refused to record it. Check the invoice, then record the ERP's payment from this exception or resolve it.
	'exception.erp_paid_unrecorded_other': 'exceptions.description.erpPaidUnrecordedOther',
	// Your ERP reports this bill paid, but FeohLedger couldn't record that payment because FeohLedger refused to record it. Check the invoice, then record the ERP's payment from this exception or resolve it.
	'exception.erp_paid_unrecorded_other_unnamed': 'exceptions.description.erpPaidUnrecordedOtherUnnamed',
	// {erp} reported this bill paid, but no longer shows it paid. FeohLedger did not record that payment. Check the bill in the ERP: if it is still owed, resolve this exception and pay it as usual.
	'exception.erp_paid_withdrawn': 'exceptions.description.erpPaidWithdrawn',
	// Your ERP reported this bill paid, but no longer shows it paid. FeohLedger did not record that payment. Check the bill in the ERP: if it is still owed, resolve this exception and pay it as usual.
	'exception.erp_paid_withdrawn_unnamed': 'exceptions.description.erpPaidWithdrawnUnnamed',
	// {erp} reports this bill paid, but FeohLedger couldn't record that payment automatically. Check the details, then record the ERP's payment from this exception.
	'exception.erp_paid_retry_failed': 'exceptions.description.erpPaidRetryFailed',
	// Your ERP reports this bill paid, but FeohLedger couldn't record that payment automatically. Check the details, then record the ERP's payment from this exception.
	'exception.erp_paid_retry_failed_unnamed': 'exceptions.description.erpPaidRetryFailedUnnamed',
	// {erp} reports this bill paid, but FeohLedger's own payment for it settled for less than AP authorized, so the invoice is held at Payment scheduled. Open the payment and either accept the settled amount or void the payment.
	'exception.erp_settled_short': 'exceptions.description.erpSettledShort',
	// Your ERP reports this bill paid, but FeohLedger's own payment for it settled for less than AP authorized, so the invoice is held at Payment scheduled. Open the payment and either accept the settled amount or void the payment.
	'exception.erp_settled_short_unnamed': 'exceptions.description.erpSettledShortUnnamed',
	// {erp} reports this bill voided or cancelled, but FeohLedger had already sent it on for payment, so it was not cancelled here. Check whether a payment is on its way to the supplier, and stop or recover it if the bill is really void.
	'exception.erp_voided_after_post': 'exceptions.description.erpVoidedAfterPost',
	// Your ERP reports this bill voided or cancelled, but FeohLedger had already sent it on for payment, so it was not cancelled here. Check whether a payment is on its way to the supplier, and stop or recover it if the bill is really void.
	'exception.erp_voided_after_post_unnamed': 'exceptions.description.erpVoidedAfterPostUnnamed',
	// {erp} did not record this invoice's payment, so the bill still shows as unpaid there although the money was paid. Resolve what stopped it, then use Retry ERP sync on the payment's run in Payments.
	'exception.erp_payment_writeback_failed': 'exceptions.description.erpPaymentWritebackFailed',
	// Your ERP did not record this invoice's payment, so the bill still shows as unpaid there although the money was paid. Resolve what stopped it, then use Retry ERP sync on the payment's run in Payments.
	'exception.erp_payment_writeback_failed_unnamed': 'exceptions.description.erpPaymentWritebackFailedUnnamed',
	// {erp} recorded this invoice's payment, but the bill still shows {amount} {currency} open there (a discount or credit taken in FeohLedger). Close the difference in the ERP.
	'exception.erp_payment_writeback_balance_open': 'exceptions.description.erpPaymentWritebackBalanceOpen',
	// Your ERP recorded this invoice's payment, but the bill still shows {amount} {currency} open there (a discount or credit taken in FeohLedger). Close the difference in the ERP.
	'exception.erp_payment_writeback_balance_open_unnamed': 'exceptions.description.erpPaymentWritebackBalanceOpenUnnamed',
	// {erp} still shows this bill paid by a payment that was voided in FeohLedger, because voiding it in the ERP failed. Void that payment in the ERP so the books agree.
	'exception.erp_payment_void_writeback_failed': 'exceptions.description.erpPaymentVoidWritebackFailed',
	// Your ERP still shows this bill paid by a payment that was voided in FeohLedger, because voiding it in the ERP failed. Void that payment in the ERP so the books agree.
	'exception.erp_payment_void_writeback_failed_unnamed': 'exceptions.description.erpPaymentVoidWritebackFailedUnnamed',
	// The ERP shows this bill settled, at least in part by a vendor credit applied there. FeohLedger will not pay it until someone confirms what is still owed.
	'exception.erp_credit_settlement': 'exceptions.description.erpCreditSettlement',
	// The ERP shows this bill paid by a payment FeohLedger did not make, while a FeohLedger payment is scheduled for it. Stop or void one of them before both reach the supplier.
	'exception.erp_paid_while_scheduled': 'exceptions.description.erpPaidWhileScheduled',
	// FeohLedger paid this invoice, and the ERP shows the bill also paid by a payment FeohLedger did not make — the supplier may have been paid twice. Recover one of the payments.
	'exception.erp_paid_twice': 'exceptions.description.erpPaidTwice',
	// The ERP shows part of this bill paid or credited outside FeohLedger; a FeohLedger payment for the full amount would overpay it. Reconcile what is still owed before paying.
	'exception.erp_partly_paid_outside': 'exceptions.description.erpPartlyPaidOutside',
	// The ERP reported this bill paid, and FeohLedger recorded that payment, but the ERP no longer shows it paid (the payment was deleted or voided there). Confirm whether it is still owed.
	'exception.erp_recorded_payment_withdrawn': 'exceptions.description.erpRecordedPaymentWithdrawn',
	// The bill this invoice was posted as has been deleted or voided in the ERP; FeohLedger will not pay it until someone confirms it is still owed.
	'exception.erp_bill_deleted_blocking': 'exceptions.description.erpBillDeletedBlocking',
	// The bill this invoice was posted as has been deleted or voided in the ERP. Confirm the books still agree.
	'exception.erp_bill_deleted': 'exceptions.description.erpBillDeleted',
	// FeohLedger cannot check this invoice's bill in the ERP, because the ERP did not say which bill it created when the invoice was posted, so it will not pay it. Confirm in the ERP whether it is still owed, then record it as paid outside FeohLedger or re-post it.
	'exception.erp_bill_unlinked_no_document': 'exceptions.description.erpBillUnlinkedNoDocument',
	// FeohLedger cannot check this invoice's bill in the ERP, because the posting did not record which ERP company it went to, so it will not pay it. Confirm in the ERP whether it is still owed, then record it as paid outside FeohLedger or re-post it.
	'exception.erp_bill_unlinked_no_company': 'exceptions.description.erpBillUnlinkedNoCompany',
	// FeohLedger cannot check this invoice's bill in the ERP, because it was posted to a different ERP company than the one now connected, so it will not pay it. Confirm in the ERP whether it is still owed, then record it as paid outside FeohLedger or re-post it.
	'exception.erp_bill_unlinked_other_company': 'exceptions.description.erpBillUnlinkedOtherCompany',
	// FeohLedger was about to pay this invoice, but the ERP shows its bill already paid. It was stopped before any money moved. Confirm what is still owed before paying it; if a virtual card was already issued for this invoice, cancel it.
	'exception.erp_dispatch_stopped_paid': 'exceptions.description.erpDispatchStoppedPaid',
	// FeohLedger was about to pay this invoice, but the ERP shows its bill partly paid. It was stopped before any money moved. Confirm what is still owed before paying it; if a virtual card was already issued for this invoice, cancel it.
	'exception.erp_dispatch_stopped_partially_paid': 'exceptions.description.erpDispatchStoppedPartiallyPaid',
	// FeohLedger was about to pay this invoice, but the ERP shows its bill deleted or voided. It was stopped before any money moved. Confirm what is still owed before paying it; if a virtual card was already issued for this invoice, cancel it.
	'exception.erp_dispatch_stopped_deleted': 'exceptions.description.erpDispatchStoppedDeleted',
	// FeohLedger was about to pay this invoice, but the ERP could not confirm its bill is still open and unpaid. It was stopped before any money moved. Confirm what is still owed before paying it; if a virtual card was already issued for this invoice, cancel it.
	'exception.erp_dispatch_stopped_unconfirmed': 'exceptions.description.erpDispatchStoppedUnconfirmed',
	// A payment the ERP reported was voided in FeohLedger, but the ERP may still show the bill paid. FeohLedger will not pay it again until someone confirms the ERP's payment is undone.
	'exception.erp_reported_payment_voided': 'exceptions.description.erpReportedPaymentVoided',
	// This invoice's payment settled, but FeohLedger could not finish recording it, so the invoice has not moved to Paid although the money has gone. Nothing retries this automatically: use Retry ERP sync on the payment's run in Payments.
	'exception.erp_sync_back_failed': 'exceptions.description.erpSyncBackFailed',
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
	'ai_allowance_reached': { included: 'count' },
	'ai_spend_cap_reached': {},
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
	'po_cancelled': { poNumber: 'text' },
	'po_total_unverifiable': { poNumber: 'text' },
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
	'self_correction_currency_conflict': { printedCurrency: 'text', fieldCurrency: 'text' },
	'self_correction_currency_mixed': { printedCurrencies: 'text' },
	'self_correction_line_item_math': { lineNumber: 'count', quantity: 'number', unitPrice: 'number', expected: 'number', total: 'number' },
	'gl_codes_not_in_chart': { codes: 'text' },
	'gl_code_stale_prior': { code: 'text' },
	'po_match.issue.po_not_found': { poNumber: 'text' },
	'po_match.issue.currency_mismatch': { invoiceCurrency: 'text', poCurrency: 'text' },
	'po_match.issue.po_cancelled': {},
	'po_match.issue.po_total_unverifiable': {},
	'po_match.issue.amount_mismatch': { invoiceAmount: 'money', poTotal: 'money', currency: 'currency', variancePct: 'percent' },
	'po_match.issue.amount_mismatch_po_currency_unknown': { invoiceAmount: 'money', poTotal: 'number', currency: 'currency', variancePct: 'percent' },
	'po_match.issue.amount_mismatch_currency_unknown': { invoiceAmount: 'number', poTotal: 'number', variancePct: 'percent' },
	'po_match.issue.partial_receipt': { receivedPct: 'percent' },
	'po_match.issue.over_receipt': { receivedQuantity: 'number', orderedQuantity: 'number', excessQuantity: 'number' },
	'po_match.issue.inspection_failed': {},
	'po_match.issue.inspection_failed_notes': { notes: 'text' },
	'po_match.issue.partial_acceptance': { acceptedQuantity: 'number' },
	'po_match.issue.partial_acceptance_unquantified': {},
	'po_match.issue.inspection_required_missing': {},
	'exception.missing_data_after_extraction': {},
	'exception.price_variance_findings': { count: 'count' },
	'exception.contract_noncompliant_findings': { count: 'count' },
	'exception.erp_paid_unrecorded_blocking_exception': { erp: 'text' },
	'exception.erp_paid_unrecorded_blocking_exception_unnamed': {},
	'exception.erp_paid_unrecorded_in_run': { erp: 'text' },
	'exception.erp_paid_unrecorded_in_run_unnamed': {},
	'exception.erp_paid_unrecorded_payment_live': { erp: 'text' },
	'exception.erp_paid_unrecorded_payment_live_unnamed': {},
	'exception.erp_paid_unrecorded_card_live': { erp: 'text' },
	'exception.erp_paid_unrecorded_card_live_unnamed': {},
	'exception.erp_paid_unrecorded_credit_conflict': { erp: 'text' },
	'exception.erp_paid_unrecorded_credit_conflict_unnamed': {},
	'exception.erp_paid_unrecorded_nothing_to_pay': { erp: 'text' },
	'exception.erp_paid_unrecorded_nothing_to_pay_unnamed': {},
	'exception.erp_paid_unrecorded_not_payable': { erp: 'text' },
	'exception.erp_paid_unrecorded_not_payable_unnamed': {},
	'exception.erp_paid_unrecorded_other': { erp: 'text' },
	'exception.erp_paid_unrecorded_other_unnamed': {},
	'exception.erp_paid_withdrawn': { erp: 'text' },
	'exception.erp_paid_withdrawn_unnamed': {},
	'exception.erp_paid_retry_failed': { erp: 'text' },
	'exception.erp_paid_retry_failed_unnamed': {},
	'exception.erp_settled_short': { erp: 'text' },
	'exception.erp_settled_short_unnamed': {},
	'exception.erp_voided_after_post': { erp: 'text' },
	'exception.erp_voided_after_post_unnamed': {},
	'exception.erp_payment_writeback_failed': { erp: 'text' },
	'exception.erp_payment_writeback_failed_unnamed': {},
	'exception.erp_payment_writeback_balance_open': { erp: 'text', amount: 'money', currency: 'currency' },
	'exception.erp_payment_writeback_balance_open_unnamed': { amount: 'money', currency: 'currency' },
	'exception.erp_payment_void_writeback_failed': { erp: 'text' },
	'exception.erp_payment_void_writeback_failed_unnamed': {},
	'exception.erp_credit_settlement': {},
	'exception.erp_paid_while_scheduled': {},
	'exception.erp_paid_twice': {},
	'exception.erp_partly_paid_outside': {},
	'exception.erp_recorded_payment_withdrawn': {},
	'exception.erp_bill_deleted_blocking': {},
	'exception.erp_bill_deleted': {},
	'exception.erp_bill_unlinked_no_document': {},
	'exception.erp_bill_unlinked_no_company': {},
	'exception.erp_bill_unlinked_other_company': {},
	'exception.erp_dispatch_stopped_paid': {},
	'exception.erp_dispatch_stopped_partially_paid': {},
	'exception.erp_dispatch_stopped_deleted': {},
	'exception.erp_dispatch_stopped_unconfirmed': {},
	'exception.erp_reported_payment_voided': {},
	'exception.erp_sync_back_failed': {},
} as const satisfies Record<
	keyof typeof INVOICE_WARNING_MESSAGE_KEYS,
	Record<string, WarningParamKind>
>;
