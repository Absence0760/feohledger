// GENERATED FILE — do not edit by hand.
//
// Source of truth: backend/app/services/invoice_warning_catalog.py
// Signatures:      mobile/lib/l10n/app_en.arb (argument order and type)
// Regenerate:      pnpm gen:warning-messages
// Drift check:     pnpm check:warning-messages  (runs in CI)
//
// The mobile twin of frontend/src/lib/api/invoiceWarningMessages.generated.ts
// — the same codes and parameter kinds, generated from the same catalogue,
// plus the code → AppLocalizations dispatch Dart needs where the web can
// look a message key up by name. The formatters each arm calls (`_money`,
// `_percent`, …) and the fallback rules live in the library this is a part
// of, `invoice_warning_messages.dart`.
part of 'invoice_warning_messages.dart';

/// What each warning parameter holds, and therefore how it renders.
///
/// The localized message says `{amount}` where the English fallback says
/// `{amount} {currency}`, precisely so the client can format the figure for
/// the reader instead of embedding en-US digits in a translated sentence.
const Map<String, Map<String, String>> invoiceWarningParamKinds = {
  'missing_vendor_name': {},
  'missing_invoice_number': {},
  'missing_amount': {},
  'duplicate_invoice_number': {},
  'duplicate_similar': {
    'similarity': 'percent',
    'invoiceNumber': 'text',
    'vendorName': 'text',
    'crossEntityCount': 'count',
  },
  'duplicate_similar_unnamed_vendor': {
    'similarity': 'percent',
    'invoiceNumber': 'text',
    'crossEntityCount': 'count',
  },
  'duplicate_similar_unnumbered': {
    'similarity': 'percent',
    'vendorName': 'text',
    'crossEntityCount': 'count',
  },
  'duplicate_similar_unnumbered_unnamed_vendor': {
    'similarity': 'percent',
    'crossEntityCount': 'count',
  },
  'duplicate_similar_cross_entity': {'similarity': 'percent'},
  'round_amount': {'amount': 'money', 'currency': 'currency'},
  'future_invoice_date': {},
  'rush_payment': {'days': 'count'},
  'past_due': {},
  'ai_allowance_reached': {'included': 'count'},
  'ai_spend_cap_reached': {},
  'unverified_vendor': {},
  'personal_email_domain': {'domain': 'text'},
  'new_vendor_large_amount': {
    'days': 'count',
    'amount': 'money',
    'currency': 'currency',
  },
  'remit_to_changed': {},
  'amount_above_vendor_mean': {
    'amount': 'money',
    'sigma': 'number',
    'mean': 'money',
    'currency': 'currency',
  },
  'llm_anomaly': {'reason': 'text'},
  'line_total_mismatch': {
    'lineItemsTotal': 'money',
    'headerAmount': 'money',
    'currency': 'currency',
  },
  'price_variance_over': {
    'deltaPct': 'percent',
    'item': 'text',
    'unitPrice': 'money',
    'baselineUnitPrice': 'money',
    'currency': 'currency',
  },
  'price_variance_under': {
    'deltaPct': 'percent',
    'item': 'text',
    'unitPrice': 'money',
    'baselineUnitPrice': 'money',
    'currency': 'currency',
  },
  'po_not_found': {'poNumber': 'text'},
  'po_amount_variance': {
    'variancePct': 'percent',
    'poNumber': 'text',
    'invoiceAmount': 'money',
    'poTotal': 'money',
    'currency': 'currency',
  },
  'po_amount_variance_po_currency_unknown': {
    'variancePct': 'percent',
    'poNumber': 'text',
    'invoiceAmount': 'money',
    'poTotal': 'number',
    'currency': 'currency',
  },
  'po_currency_mismatch': {
    'invoiceCurrency': 'text',
    'poNumber': 'text',
    'poCurrency': 'text',
  },
  'po_cancelled': {'poNumber': 'text'},
  'po_total_unverifiable': {'poNumber': 'text'},
  'po_partial_receipt': {'matchType': 'text', 'poNumber': 'text'},
  'po_over_receipt': {
    'receivedQuantity': 'number',
    'orderedQuantity': 'number',
    'excessQuantity': 'number',
    'poNumber': 'text',
  },
  'po_over_receipt_unquantified': {'poNumber': 'text'},
  'quality_inspection_failed': {'poNumber': 'text'},
  'quality_inspection_failed_notes': {'poNumber': 'text', 'notes': 'text'},
  'quality_inspection_missing': {'poNumber': 'text'},
  'quality_partial_acceptance': {
    'acceptedQuantity': 'number',
    'poNumber': 'text',
  },
  'quality_partial_acceptance_unquantified': {'poNumber': 'text'},
  'recurring_variance_over': {
    'amount': 'money',
    'deltaPct': 'percent',
    'templateName': 'text',
    'expectedAmount': 'money',
    'currency': 'currency',
  },
  'recurring_variance_under': {
    'amount': 'money',
    'deltaPct': 'percent',
    'templateName': 'text',
    'expectedAmount': 'money',
    'currency': 'currency',
  },
  'contract_expired': {
    'invoiceDate': 'date',
    'contractNumber': 'text',
    'endDate': 'date',
  },
  'contract_not_started': {
    'invoiceDate': 'date',
    'contractNumber': 'text',
    'startDate': 'date',
  },
  'contract_terminated': {'contractNumber': 'text'},
  'contract_cancelled': {'contractNumber': 'text'},
  'contract_vendor_mismatch': {'contractNumber': 'text'},
  'contract_spend_limit_exceeded': {
    'cumulativeSpend': 'money',
    'contractNumber': 'text',
    'spendLimit': 'money',
    'currency': 'currency',
  },
  'contract_spend_limit_exceeded_not_to_exceed': {
    'cumulativeSpend': 'money',
    'contractNumber': 'text',
    'spendLimit': 'money',
    'currency': 'currency',
  },
  'contract_gl_not_allowed': {'glAccount': 'text', 'contractNumber': 'text'},
  'self_correction_total_reconciliation': {
    'subtotal': 'number',
    'tax': 'number',
    'shipping': 'number',
    'discount': 'number',
    'expected': 'number',
    'amount': 'number',
  },
  'self_correction_date_ordering': {'dueDate': 'date', 'invoiceDate': 'date'},
  'self_correction_line_items_sum': {
    'lineItemsTotal': 'number',
    'amount': 'number',
  },
  'self_correction_currency_conflict': {
    'printedCurrency': 'text',
    'fieldCurrency': 'text',
  },
  'self_correction_currency_mixed': {'printedCurrencies': 'text'},
  'self_correction_line_item_math': {
    'lineNumber': 'count',
    'quantity': 'number',
    'unitPrice': 'number',
    'expected': 'number',
    'total': 'number',
  },
  'gl_codes_not_in_chart': {'codes': 'text'},
  'gl_code_stale_prior': {'code': 'text'},
  'po_match.issue.po_not_found': {'poNumber': 'text'},
  'po_match.issue.currency_mismatch': {
    'invoiceCurrency': 'text',
    'poCurrency': 'text',
  },
  'po_match.issue.po_cancelled': {},
  'po_match.issue.po_total_unverifiable': {},
  'po_match.issue.amount_mismatch': {
    'invoiceAmount': 'money',
    'poTotal': 'money',
    'currency': 'currency',
    'variancePct': 'percent',
  },
  'po_match.issue.amount_mismatch_po_currency_unknown': {
    'invoiceAmount': 'money',
    'poTotal': 'number',
    'currency': 'currency',
    'variancePct': 'percent',
  },
  'po_match.issue.amount_mismatch_currency_unknown': {
    'invoiceAmount': 'number',
    'poTotal': 'number',
    'variancePct': 'percent',
  },
  'po_match.issue.partial_receipt': {'receivedPct': 'percent'},
  'po_match.issue.over_receipt': {
    'receivedQuantity': 'number',
    'orderedQuantity': 'number',
    'excessQuantity': 'number',
  },
  'po_match.issue.inspection_failed': {},
  'po_match.issue.inspection_failed_notes': {'notes': 'text'},
  'po_match.issue.partial_acceptance': {'acceptedQuantity': 'number'},
  'po_match.issue.partial_acceptance_unquantified': {},
  'po_match.issue.inspection_required_missing': {},
  'exception.missing_data_after_extraction': {},
  'exception.price_variance_findings': {'count': 'count'},
  'exception.contract_noncompliant_findings': {'count': 'count'},
  'exception.erp_paid_unrecorded_blocking_exception': {'erp': 'text'},
  'exception.erp_paid_unrecorded_blocking_exception_unnamed': {},
  'exception.erp_paid_unrecorded_in_run': {'erp': 'text'},
  'exception.erp_paid_unrecorded_in_run_unnamed': {},
  'exception.erp_paid_unrecorded_payment_live': {'erp': 'text'},
  'exception.erp_paid_unrecorded_payment_live_unnamed': {},
  'exception.erp_paid_unrecorded_card_live': {'erp': 'text'},
  'exception.erp_paid_unrecorded_card_live_unnamed': {},
  'exception.erp_paid_unrecorded_credit_conflict': {'erp': 'text'},
  'exception.erp_paid_unrecorded_credit_conflict_unnamed': {},
  'exception.erp_paid_unrecorded_nothing_to_pay': {'erp': 'text'},
  'exception.erp_paid_unrecorded_nothing_to_pay_unnamed': {},
  'exception.erp_paid_unrecorded_not_payable': {'erp': 'text'},
  'exception.erp_paid_unrecorded_not_payable_unnamed': {},
  'exception.erp_paid_unrecorded_other': {'erp': 'text'},
  'exception.erp_paid_unrecorded_other_unnamed': {},
  'exception.erp_paid_withdrawn': {'erp': 'text'},
  'exception.erp_paid_withdrawn_unnamed': {},
  'exception.erp_paid_retry_failed': {'erp': 'text'},
  'exception.erp_paid_retry_failed_unnamed': {},
  'exception.erp_settled_short': {'erp': 'text'},
  'exception.erp_settled_short_unnamed': {},
  'exception.erp_voided_after_post': {'erp': 'text'},
  'exception.erp_voided_after_post_unnamed': {},
  'exception.erp_payment_writeback_failed': {'erp': 'text'},
  'exception.erp_payment_writeback_failed_unnamed': {},
  'exception.erp_payment_writeback_balance_open': {
    'erp': 'text',
    'amount': 'money',
    'currency': 'currency',
  },
  'exception.erp_payment_writeback_balance_open_unnamed': {
    'amount': 'money',
    'currency': 'currency',
  },
  'exception.erp_payment_void_writeback_failed': {'erp': 'text'},
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
};

/// The localized sentence for [warningCode], or `null` when this build does
/// not know the code or a parameter the sentence needs is missing — see
/// `localizeInvoiceWarning`, the only caller.
String? _localizeWarningCode(
  AppLocalizations l,
  String? warningCode,
  Map<String, String> p,
  String? currency,
) {
  switch (warningCode) {
    // Missing vendor name
    case 'missing_vendor_name':
      return l.invoiceWarningMissingVendorName;
    // Missing invoice number
    case 'missing_invoice_number':
      return l.invoiceWarningMissingInvoiceNumber;
    // Missing or zero amount
    case 'missing_amount':
      return l.invoiceWarningMissingAmount;
    // Duplicate invoice number for this vendor
    case 'duplicate_invoice_number':
      return l.invoiceWarningDuplicateInvoiceNumber;
    // Potential duplicate: {similarity}% match to {invoiceNumber} from {vendorName}{crossEntityCount, plural, =0 {} one { (plus # near-identical invoice under another entity)} other { (plus # near-identical invoices under another entity)}}
    case 'duplicate_similar':
      final similarity = _percent(p['similarity'], currency);
      if (similarity == null) return null;
      final invoiceNumber = _text(p['invoiceNumber'], currency);
      if (invoiceNumber == null) return null;
      final vendorName = _text(p['vendorName'], currency);
      if (vendorName == null) return null;
      final crossEntityCount = _count(p['crossEntityCount']);
      if (crossEntityCount == null) return null;
      return l.invoiceWarningDuplicateSimilar(
        similarity,
        invoiceNumber,
        vendorName,
        crossEntityCount,
      );
    // Potential duplicate: {similarity}% match to {invoiceNumber}{crossEntityCount, plural, =0 {} one { (plus # near-identical invoice under another entity)} other { (plus # near-identical invoices under another entity)}}
    case 'duplicate_similar_unnamed_vendor':
      final similarity = _percent(p['similarity'], currency);
      if (similarity == null) return null;
      final invoiceNumber = _text(p['invoiceNumber'], currency);
      if (invoiceNumber == null) return null;
      final crossEntityCount = _count(p['crossEntityCount']);
      if (crossEntityCount == null) return null;
      return l.invoiceWarningDuplicateSimilarUnnamedVendor(
        similarity,
        invoiceNumber,
        crossEntityCount,
      );
    // Potential duplicate: {similarity}% match to another invoice from {vendorName}{crossEntityCount, plural, =0 {} one { (plus # near-identical invoice under another entity)} other { (plus # near-identical invoices under another entity)}}
    case 'duplicate_similar_unnumbered':
      final similarity = _percent(p['similarity'], currency);
      if (similarity == null) return null;
      final vendorName = _text(p['vendorName'], currency);
      if (vendorName == null) return null;
      final crossEntityCount = _count(p['crossEntityCount']);
      if (crossEntityCount == null) return null;
      return l.invoiceWarningDuplicateSimilarUnnumbered(
        similarity,
        vendorName,
        crossEntityCount,
      );
    // Potential duplicate: {similarity}% match to another invoice{crossEntityCount, plural, =0 {} one { (plus # near-identical invoice under another entity)} other { (plus # near-identical invoices under another entity)}}
    case 'duplicate_similar_unnumbered_unnamed_vendor':
      final similarity = _percent(p['similarity'], currency);
      if (similarity == null) return null;
      final crossEntityCount = _count(p['crossEntityCount']);
      if (crossEntityCount == null) return null;
      return l.invoiceWarningDuplicateSimilarUnnumberedUnnamedVendor(
        similarity,
        crossEntityCount,
      );
    // Potential duplicate: {similarity}% match to a near-identical invoice under another entity
    case 'duplicate_similar_cross_entity':
      final similarity = _percent(p['similarity'], currency);
      if (similarity == null) return null;
      return l.invoiceWarningDuplicateSimilarCrossEntity(similarity);
    // Round amount: {amount} {currency}
    case 'round_amount':
      final amount = _money(p['amount'], currency);
      if (amount == null) return null;
      return l.invoiceWarningRoundAmount(amount);
    // Invoice date is in the future
    case 'future_invoice_date':
      return l.invoiceWarningFutureInvoiceDate;
    // Rush payment: due within {days, plural, one {# day} other {# days}} of the invoice date
    case 'rush_payment':
      final days = _count(p['days']);
      if (days == null) return null;
      return l.invoiceWarningRushPayment(days);
    // Invoice is past due
    case 'past_due':
      return l.invoiceWarningPastDue;
    // AI reading is paused: this month's {included, plural, one {# AI-read invoice is} other {# AI-read invoices are}} used. Enter the details manually, or upgrade your plan under Settings → Subscription.
    case 'ai_allowance_reached':
      final included = _count(p['included']);
      if (included == null) return null;
      return l.invoiceWarningAiAllowanceReached(included);
    // AI reading is paused: this month's AI spending cap is reached. Enter the details manually, or raise the cap under Settings → Subscription.
    case 'ai_spend_cap_reached':
      return l.invoiceWarningAiSpendCapReached;
    // Vendor is unverified
    case 'unverified_vendor':
      return l.invoiceWarningUnverifiedVendor;
    // Vendor email uses personal domain: {domain}
    case 'personal_email_domain':
      final domain = _text(p['domain'], currency);
      if (domain == null) return null;
      return l.invoiceWarningPersonalEmailDomain(domain);
    // New vendor (created {days, plural, one {# day} other {# days}} ago) submitting large invoice {amount} {currency}
    case 'new_vendor_large_amount':
      final days = _count(p['days']);
      if (days == null) return null;
      final amount = _money(p['amount'], currency);
      if (amount == null) return null;
      return l.invoiceWarningNewVendorLargeAmount(days, amount);
    // Remit-to address changed since the last approved invoice for this vendor
    case 'remit_to_changed':
      return l.invoiceWarningRemitToChanged;
    // Amount {amount} {currency} is {sigma}σ above this vendor's historical mean ({mean} {currency})
    case 'amount_above_vendor_mean':
      final amount = _money(p['amount'], currency);
      if (amount == null) return null;
      final sigma = _number(p['sigma'], currency);
      if (sigma == null) return null;
      final mean = _money(p['mean'], currency);
      if (mean == null) return null;
      return l.invoiceWarningAmountAboveVendorMean(amount, sigma, mean);
    // AI-flagged anomaly: {reason}
    case 'llm_anomaly':
      final reason = _text(p['reason'], currency);
      if (reason == null) return null;
      return l.invoiceWarningLlmAnomaly(reason);
    // Line items total {lineItemsTotal} {currency} but the invoice amount is {headerAmount} {currency}
    case 'line_total_mismatch':
      final lineItemsTotal = _money(p['lineItemsTotal'], currency);
      if (lineItemsTotal == null) return null;
      final headerAmount = _money(p['headerAmount'], currency);
      if (headerAmount == null) return null;
      return l.invoiceWarningLineTotalMismatch(lineItemsTotal, headerAmount);
    // Unit price {deltaPct}% over this vendor's baseline for {item} ({unitPrice} {currency} vs {baselineUnitPrice} {currency})
    case 'price_variance_over':
      final deltaPct = _percent(p['deltaPct'], currency);
      if (deltaPct == null) return null;
      final item = _text(p['item'], currency);
      if (item == null) return null;
      final unitPrice = _money(p['unitPrice'], currency);
      if (unitPrice == null) return null;
      final baselineUnitPrice = _money(p['baselineUnitPrice'], currency);
      if (baselineUnitPrice == null) return null;
      return l.invoiceWarningPriceVarianceOver(
        deltaPct,
        item,
        unitPrice,
        baselineUnitPrice,
      );
    // Unit price {deltaPct}% under this vendor's baseline for {item} ({unitPrice} {currency} vs {baselineUnitPrice} {currency})
    case 'price_variance_under':
      final deltaPct = _percent(p['deltaPct'], currency);
      if (deltaPct == null) return null;
      final item = _text(p['item'], currency);
      if (item == null) return null;
      final unitPrice = _money(p['unitPrice'], currency);
      if (unitPrice == null) return null;
      final baselineUnitPrice = _money(p['baselineUnitPrice'], currency);
      if (baselineUnitPrice == null) return null;
      return l.invoiceWarningPriceVarianceUnder(
        deltaPct,
        item,
        unitPrice,
        baselineUnitPrice,
      );
    // PO {poNumber} not found
    case 'po_not_found':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningPoNotFound(poNumber);
    // Amount variance {variancePct}% vs PO {poNumber} (invoice {invoiceAmount} {currency} vs PO {poTotal} {currency})
    case 'po_amount_variance':
      final variancePct = _percent(p['variancePct'], currency);
      if (variancePct == null) return null;
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      final invoiceAmount = _money(p['invoiceAmount'], currency);
      if (invoiceAmount == null) return null;
      final poTotal = _money(p['poTotal'], currency);
      if (poTotal == null) return null;
      return l.invoiceWarningPoAmountVariance(
        variancePct,
        poNumber,
        invoiceAmount,
        poTotal,
      );
    // Amount variance {variancePct}% vs PO {poNumber}, which records no currency (invoice {invoiceAmount} {currency} vs PO {poTotal})
    case 'po_amount_variance_po_currency_unknown':
      final variancePct = _percent(p['variancePct'], currency);
      if (variancePct == null) return null;
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      final invoiceAmount = _money(p['invoiceAmount'], currency);
      if (invoiceAmount == null) return null;
      final poTotal = _number(p['poTotal'], currency);
      if (poTotal == null) return null;
      return l.invoiceWarningPoAmountVariancePoCurrencyUnknown(
        variancePct,
        poNumber,
        invoiceAmount,
        poTotal,
      );
    // Invoice is in {invoiceCurrency} but PO {poNumber} is in {poCurrency} — the amounts were not compared
    case 'po_currency_mismatch':
      final invoiceCurrency = _text(p['invoiceCurrency'], currency);
      if (invoiceCurrency == null) return null;
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      final poCurrency = _text(p['poCurrency'], currency);
      if (poCurrency == null) return null;
      return l.invoiceWarningPoCurrencyMismatch(
        invoiceCurrency,
        poNumber,
        poCurrency,
      );
    // PO {poNumber} is cancelled — the invoice was not matched against it
    case 'po_cancelled':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningPoCancelled(poNumber);
    // The total of PO {poNumber} could not be confirmed from the ERP on its last sync — the amounts were not compared
    case 'po_total_unverifiable':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningPoTotalUnverifiable(poNumber);
    // Partial 3-way match — {matchType} match against PO {poNumber}, but only part of the ordered quantity has been received
    case 'po_partial_receipt':
      final matchType = _text(p['matchType'], currency);
      if (matchType == null) return null;
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningPoPartialReceipt(matchType, poNumber);
    // Over-receipt: {receivedQuantity} received against {orderedQuantity} ordered (+{excessQuantity}) on PO {poNumber}
    case 'po_over_receipt':
      final receivedQuantity = _number(p['receivedQuantity'], currency);
      if (receivedQuantity == null) return null;
      final orderedQuantity = _number(p['orderedQuantity'], currency);
      if (orderedQuantity == null) return null;
      final excessQuantity = _number(p['excessQuantity'], currency);
      if (excessQuantity == null) return null;
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningPoOverReceipt(
        receivedQuantity,
        orderedQuantity,
        excessQuantity,
        poNumber,
      );
    // More goods received than ordered on PO {poNumber}
    case 'po_over_receipt_unquantified':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningPoOverReceiptUnquantified(poNumber);
    // Failed quality inspection for PO {poNumber}
    case 'quality_inspection_failed':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningQualityInspectionFailed(poNumber);
    // Failed quality inspection for PO {poNumber}: {notes}
    case 'quality_inspection_failed_notes':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      final notes = _text(p['notes'], currency);
      if (notes == null) return null;
      return l.invoiceWarningQualityInspectionFailedNotes(poNumber, notes);
    // Quality inspection required but missing for PO {poNumber}
    case 'quality_inspection_missing':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningQualityInspectionMissing(poNumber);
    // Partial acceptance: {acceptedQuantity} of the ordered quantity accepted on PO {poNumber}
    case 'quality_partial_acceptance':
      final acceptedQuantity = _number(p['acceptedQuantity'], currency);
      if (acceptedQuantity == null) return null;
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningQualityPartialAcceptance(
        acceptedQuantity,
        poNumber,
      );
    // Partial quality acceptance on inspection for PO {poNumber}
    case 'quality_partial_acceptance_unquantified':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningQualityPartialAcceptanceUnquantified(poNumber);
    // Amount {amount} {currency} is {deltaPct}% over the recurring template “{templateName}” expected amount {expectedAmount} {currency}
    case 'recurring_variance_over':
      final amount = _money(p['amount'], currency);
      if (amount == null) return null;
      final deltaPct = _percent(p['deltaPct'], currency);
      if (deltaPct == null) return null;
      final templateName = _text(p['templateName'], currency);
      if (templateName == null) return null;
      final expectedAmount = _money(p['expectedAmount'], currency);
      if (expectedAmount == null) return null;
      return l.invoiceWarningRecurringVarianceOver(
        amount,
        deltaPct,
        templateName,
        expectedAmount,
      );
    // Amount {amount} {currency} is {deltaPct}% under the recurring template “{templateName}” expected amount {expectedAmount} {currency}
    case 'recurring_variance_under':
      final amount = _money(p['amount'], currency);
      if (amount == null) return null;
      final deltaPct = _percent(p['deltaPct'], currency);
      if (deltaPct == null) return null;
      final templateName = _text(p['templateName'], currency);
      if (templateName == null) return null;
      final expectedAmount = _money(p['expectedAmount'], currency);
      if (expectedAmount == null) return null;
      return l.invoiceWarningRecurringVarianceUnder(
        amount,
        deltaPct,
        templateName,
        expectedAmount,
      );
    // Invoice dated {invoiceDate} is after contract {contractNumber} expired ({endDate})
    case 'contract_expired':
      final invoiceDate = _date(p['invoiceDate'], currency);
      if (invoiceDate == null) return null;
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      final endDate = _date(p['endDate'], currency);
      if (endDate == null) return null;
      return l.invoiceWarningContractExpired(
        invoiceDate,
        contractNumber,
        endDate,
      );
    // Invoice dated {invoiceDate} predates contract {contractNumber} start ({startDate})
    case 'contract_not_started':
      final invoiceDate = _date(p['invoiceDate'], currency);
      if (invoiceDate == null) return null;
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      final startDate = _date(p['startDate'], currency);
      if (startDate == null) return null;
      return l.invoiceWarningContractNotStarted(
        invoiceDate,
        contractNumber,
        startDate,
      );
    // Spend recorded against terminated contract {contractNumber}
    case 'contract_terminated':
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      return l.invoiceWarningContractTerminated(contractNumber);
    // Spend recorded against cancelled contract {contractNumber}
    case 'contract_cancelled':
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      return l.invoiceWarningContractCancelled(contractNumber);
    // Invoice vendor does not match contract {contractNumber} vendor
    case 'contract_vendor_mismatch':
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      return l.invoiceWarningContractVendorMismatch(contractNumber);
    // Cumulative spend {cumulativeSpend} {currency} exceeds contract {contractNumber} limit {spendLimit} {currency}
    case 'contract_spend_limit_exceeded':
      final cumulativeSpend = _money(p['cumulativeSpend'], currency);
      if (cumulativeSpend == null) return null;
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      final spendLimit = _money(p['spendLimit'], currency);
      if (spendLimit == null) return null;
      return l.invoiceWarningContractSpendLimitExceeded(
        cumulativeSpend,
        contractNumber,
        spendLimit,
      );
    // Cumulative spend {cumulativeSpend} {currency} exceeds contract {contractNumber} limit {spendLimit} {currency} (not-to-exceed)
    case 'contract_spend_limit_exceeded_not_to_exceed':
      final cumulativeSpend = _money(p['cumulativeSpend'], currency);
      if (cumulativeSpend == null) return null;
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      final spendLimit = _money(p['spendLimit'], currency);
      if (spendLimit == null) return null;
      return l.invoiceWarningContractSpendLimitExceededNotToExceed(
        cumulativeSpend,
        contractNumber,
        spendLimit,
      );
    // GL account {glAccount} is outside contract {contractNumber} allowed accounts
    case 'contract_gl_not_allowed':
      final glAccount = _text(p['glAccount'], currency);
      if (glAccount == null) return null;
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      return l.invoiceWarningContractGlNotAllowed(glAccount, contractNumber);
    // Amounts don't add up: subtotal ({subtotal}) + tax ({tax}) + shipping ({shipping}) − discount ({discount}) = {expected}, but total is {amount}.
    case 'self_correction_total_reconciliation':
      final subtotal = _number(p['subtotal'], currency);
      if (subtotal == null) return null;
      final tax = _number(p['tax'], currency);
      if (tax == null) return null;
      final shipping = _number(p['shipping'], currency);
      if (shipping == null) return null;
      final discount = _number(p['discount'], currency);
      if (discount == null) return null;
      final expected = _number(p['expected'], currency);
      if (expected == null) return null;
      final amount = _number(p['amount'], currency);
      if (amount == null) return null;
      return l.invoiceWarningSelfCorrectionTotalReconciliation(
        subtotal,
        tax,
        shipping,
        discount,
        expected,
        amount,
      );
    // Due date ({dueDate}) is before invoice date ({invoiceDate}).
    case 'self_correction_date_ordering':
      final dueDate = _date(p['dueDate'], currency);
      if (dueDate == null) return null;
      final invoiceDate = _date(p['invoiceDate'], currency);
      if (invoiceDate == null) return null;
      return l.invoiceWarningSelfCorrectionDateOrdering(dueDate, invoiceDate);
    // Line items total ({lineItemsTotal}) doesn't match invoice amount ({amount}).
    case 'self_correction_line_items_sum':
      final lineItemsTotal = _number(p['lineItemsTotal'], currency);
      if (lineItemsTotal == null) return null;
      final amount = _number(p['amount'], currency);
      if (amount == null) return null;
      return l.invoiceWarningSelfCorrectionLineItemsSum(lineItemsTotal, amount);
    // Amounts are printed in {printedCurrency}, but the invoice currency reads {fieldCurrency}. The amounts were left blank — enter them from the document.
    case 'self_correction_currency_conflict':
      final printedCurrency = _text(p['printedCurrency'], currency);
      if (printedCurrency == null) return null;
      final fieldCurrency = _text(p['fieldCurrency'], currency);
      if (fieldCurrency == null) return null;
      return l.invoiceWarningSelfCorrectionCurrencyConflict(
        printedCurrency,
        fieldCurrency,
      );
    // Amounts are printed in more than one currency ({printedCurrencies}). The amounts were left blank — enter them from the document.
    case 'self_correction_currency_mixed':
      final printedCurrencies = _text(p['printedCurrencies'], currency);
      if (printedCurrencies == null) return null;
      return l.invoiceWarningSelfCorrectionCurrencyMixed(printedCurrencies);
    // Line {lineNumber}: {quantity} × {unitPrice} = {expected}, but total is {total}.
    case 'self_correction_line_item_math':
      final lineNumber = _text(p['lineNumber'], currency);
      if (lineNumber == null) return null;
      final quantity = _number(p['quantity'], currency);
      if (quantity == null) return null;
      final unitPrice = _number(p['unitPrice'], currency);
      if (unitPrice == null) return null;
      final expected = _number(p['expected'], currency);
      if (expected == null) return null;
      final total = _number(p['total'], currency);
      if (total == null) return null;
      return l.invoiceWarningSelfCorrectionLineItemMath(
        lineNumber,
        quantity,
        unitPrice,
        expected,
        total,
      );
    // AI suggested GL code(s) not in active chart: {codes}
    case 'gl_codes_not_in_chart':
      final codes = _text(p['codes'], currency);
      if (codes == null) return null;
      return l.invoiceWarningGlCodesNotInChart(codes);
    // Cached vendor GL code '{code}' is no longer in the active chart of accounts.
    case 'gl_code_stale_prior':
      final code = _text(p['code'], currency);
      if (code == null) return null;
      return l.invoiceWarningGlCodeStalePrior(code);
    // PO {poNumber} not found
    case 'po_match.issue.po_not_found':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoicePoMatchIssuePoNotFound(poNumber);
    // Currency mismatch: invoice in {invoiceCurrency}, PO in {poCurrency} — amounts not compared
    case 'po_match.issue.currency_mismatch':
      final invoiceCurrency = _text(p['invoiceCurrency'], currency);
      if (invoiceCurrency == null) return null;
      final poCurrency = _text(p['poCurrency'], currency);
      if (poCurrency == null) return null;
      return l.invoicePoMatchIssueCurrencyMismatch(invoiceCurrency, poCurrency);
    // PO is cancelled — amounts not compared
    case 'po_match.issue.po_cancelled':
      return l.invoicePoMatchIssuePoCancelled;
    // PO total not confirmed by the ERP on its last sync — amounts not compared
    case 'po_match.issue.po_total_unverifiable':
      return l.invoicePoMatchIssuePoTotalUnverifiable;
    // Amount mismatch: invoice {invoiceAmount} {currency} vs PO {poTotal} {currency} ({variancePct}%)
    case 'po_match.issue.amount_mismatch':
      final invoiceAmount = _money(p['invoiceAmount'], currency);
      if (invoiceAmount == null) return null;
      final poTotal = _money(p['poTotal'], currency);
      if (poTotal == null) return null;
      final variancePct = _percent(p['variancePct'], currency);
      if (variancePct == null) return null;
      return l.invoicePoMatchIssueAmountMismatch(
        invoiceAmount,
        poTotal,
        variancePct,
      );
    // Amount mismatch: invoice {invoiceAmount} {currency} vs PO {poTotal} ({variancePct}%)
    case 'po_match.issue.amount_mismatch_po_currency_unknown':
      final invoiceAmount = _money(p['invoiceAmount'], currency);
      if (invoiceAmount == null) return null;
      final poTotal = _number(p['poTotal'], currency);
      if (poTotal == null) return null;
      final variancePct = _percent(p['variancePct'], currency);
      if (variancePct == null) return null;
      return l.invoicePoMatchIssueAmountMismatchPoCurrencyUnknown(
        invoiceAmount,
        poTotal,
        variancePct,
      );
    // Amount mismatch: invoice {invoiceAmount} vs PO {poTotal} ({variancePct}%)
    case 'po_match.issue.amount_mismatch_currency_unknown':
      final invoiceAmount = _number(p['invoiceAmount'], currency);
      if (invoiceAmount == null) return null;
      final poTotal = _number(p['poTotal'], currency);
      if (poTotal == null) return null;
      final variancePct = _percent(p['variancePct'], currency);
      if (variancePct == null) return null;
      return l.invoicePoMatchIssueAmountMismatchCurrencyUnknown(
        invoiceAmount,
        poTotal,
        variancePct,
      );
    // Partial receipt: {receivedPct}% of ordered quantity received
    case 'po_match.issue.partial_receipt':
      final receivedPct = _percent(p['receivedPct'], currency);
      if (receivedPct == null) return null;
      return l.invoicePoMatchIssuePartialReceipt(receivedPct);
    // Over-receipt: {receivedQuantity} received against {orderedQuantity} ordered (+{excessQuantity})
    case 'po_match.issue.over_receipt':
      final receivedQuantity = _number(p['receivedQuantity'], currency);
      if (receivedQuantity == null) return null;
      final orderedQuantity = _number(p['orderedQuantity'], currency);
      if (orderedQuantity == null) return null;
      final excessQuantity = _number(p['excessQuantity'], currency);
      if (excessQuantity == null) return null;
      return l.invoicePoMatchIssueOverReceipt(
        receivedQuantity,
        orderedQuantity,
        excessQuantity,
      );
    // Failed quality inspection
    case 'po_match.issue.inspection_failed':
      return l.invoicePoMatchIssueInspectionFailed;
    // Failed quality inspection: {notes}
    case 'po_match.issue.inspection_failed_notes':
      final notes = _text(p['notes'], currency);
      if (notes == null) return null;
      return l.invoicePoMatchIssueInspectionFailedNotes(notes);
    // Partial acceptance: {acceptedQuantity} of ordered quantity accepted
    case 'po_match.issue.partial_acceptance':
      final acceptedQuantity = _number(p['acceptedQuantity'], currency);
      if (acceptedQuantity == null) return null;
      return l.invoicePoMatchIssuePartialAcceptance(acceptedQuantity);
    // Partial acceptance: part of ordered quantity accepted
    case 'po_match.issue.partial_acceptance_unquantified':
      return l.invoicePoMatchIssuePartialAcceptanceUnquantified;
    // Quality inspection required but missing
    case 'po_match.issue.inspection_required_missing':
      return l.invoicePoMatchIssueInspectionRequiredMissing;
    // Required fields missing after extraction
    case 'exception.missing_data_after_extraction':
      return l.exceptionDescriptionMissingDataAfterExtraction;
    // Line-item price variance vs vendor history on {count, plural, one {# line} other {# lines}}
    case 'exception.price_variance_findings':
      final count = _count(p['count']);
      if (count == null) return null;
      return l.exceptionDescriptionPriceVarianceFindings(count);
    // {count, plural, one {# contract-compliance finding} other {# contract-compliance findings}}
    case 'exception.contract_noncompliant_findings':
      final count = _count(p['count']);
      if (count == null) return null;
      return l.exceptionDescriptionContractNoncompliantFindings(count);
    // {erp} reports this bill paid, but FeohLedger couldn't record that payment because another exception on this invoice is blocking payment. Clear that exception and FeohLedger records the payment automatically.
    case 'exception.erp_paid_unrecorded_blocking_exception':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaidUnrecordedBlockingException(erp);
    // Your ERP reports this bill paid, but FeohLedger couldn't record that payment because another exception on this invoice is blocking payment. Clear that exception and FeohLedger records the payment automatically.
    case 'exception.erp_paid_unrecorded_blocking_exception_unnamed':
      return l.exceptionDescriptionErpPaidUnrecordedBlockingExceptionUnnamed;
    // {erp} reports this bill paid, but FeohLedger couldn't record that payment because the invoice is also in a FeohLedger payment run, so the supplier could be paid twice. Take it out of the run or cancel the run, then record the ERP's payment from this exception.
    case 'exception.erp_paid_unrecorded_in_run':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaidUnrecordedInRun(erp);
    // Your ERP reports this bill paid, but FeohLedger couldn't record that payment because the invoice is also in a FeohLedger payment run, so the supplier could be paid twice. Take it out of the run or cancel the run, then record the ERP's payment from this exception.
    case 'exception.erp_paid_unrecorded_in_run_unnamed':
      return l.exceptionDescriptionErpPaidUnrecordedInRunUnnamed;
    // {erp} reports this bill paid, but FeohLedger couldn't record that payment because FeohLedger has its own payment in progress for this invoice, so the supplier could be paid twice. Void that payment if the ERP's payment is the real one, then record the ERP's payment from this exception.
    case 'exception.erp_paid_unrecorded_payment_live':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaidUnrecordedPaymentLive(erp);
    // Your ERP reports this bill paid, but FeohLedger couldn't record that payment because FeohLedger has its own payment in progress for this invoice, so the supplier could be paid twice. Void that payment if the ERP's payment is the real one, then record the ERP's payment from this exception.
    case 'exception.erp_paid_unrecorded_payment_live_unnamed':
      return l.exceptionDescriptionErpPaidUnrecordedPaymentLiveUnnamed;
    // {erp} reports this bill paid, but FeohLedger couldn't record that payment because a virtual card is still live for this invoice, so the supplier could be paid twice. Cancel the card, then record the ERP's payment from this exception.
    case 'exception.erp_paid_unrecorded_card_live':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaidUnrecordedCardLive(erp);
    // Your ERP reports this bill paid, but FeohLedger couldn't record that payment because a virtual card is still live for this invoice, so the supplier could be paid twice. Cancel the card, then record the ERP's payment from this exception.
    case 'exception.erp_paid_unrecorded_card_live_unnamed':
      return l.exceptionDescriptionErpPaidUnrecordedCardLiveUnnamed;
    // {erp} reports this bill paid, but FeohLedger couldn't record that payment because an applied credit memo no longer matches this invoice's supplier or currency, so the amount owed can't be stated. Correct the credit memo, then record the ERP's payment from this exception.
    case 'exception.erp_paid_unrecorded_credit_conflict':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaidUnrecordedCreditConflict(erp);
    // Your ERP reports this bill paid, but FeohLedger couldn't record that payment because an applied credit memo no longer matches this invoice's supplier or currency, so the amount owed can't be stated. Correct the credit memo, then record the ERP's payment from this exception.
    case 'exception.erp_paid_unrecorded_credit_conflict_unnamed':
      return l.exceptionDescriptionErpPaidUnrecordedCreditConflictUnnamed;
    // {erp} reports this bill paid, but FeohLedger couldn't record that payment because credit memos already cover the whole invoice, so there is no payment to record. Check the bill in the ERP, then resolve this exception.
    case 'exception.erp_paid_unrecorded_nothing_to_pay':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaidUnrecordedNothingToPay(erp);
    // Your ERP reports this bill paid, but FeohLedger couldn't record that payment because credit memos already cover the whole invoice, so there is no payment to record. Check the bill in the ERP, then resolve this exception.
    case 'exception.erp_paid_unrecorded_nothing_to_pay_unnamed':
      return l.exceptionDescriptionErpPaidUnrecordedNothingToPayUnnamed;
    // {erp} reports this bill paid, but FeohLedger couldn't record that payment because the invoice is not at a status that can be marked paid. Check the invoice, then record the ERP's payment from this exception or resolve it.
    case 'exception.erp_paid_unrecorded_not_payable':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaidUnrecordedNotPayable(erp);
    // Your ERP reports this bill paid, but FeohLedger couldn't record that payment because the invoice is not at a status that can be marked paid. Check the invoice, then record the ERP's payment from this exception or resolve it.
    case 'exception.erp_paid_unrecorded_not_payable_unnamed':
      return l.exceptionDescriptionErpPaidUnrecordedNotPayableUnnamed;
    // {erp} reports this bill paid, but FeohLedger couldn't record that payment because FeohLedger refused to record it. Check the invoice, then record the ERP's payment from this exception or resolve it.
    case 'exception.erp_paid_unrecorded_other':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaidUnrecordedOther(erp);
    // Your ERP reports this bill paid, but FeohLedger couldn't record that payment because FeohLedger refused to record it. Check the invoice, then record the ERP's payment from this exception or resolve it.
    case 'exception.erp_paid_unrecorded_other_unnamed':
      return l.exceptionDescriptionErpPaidUnrecordedOtherUnnamed;
    // {erp} reported this bill paid, but no longer shows it paid. FeohLedger did not record that payment. Check the bill in the ERP: if it is still owed, resolve this exception and pay it as usual.
    case 'exception.erp_paid_withdrawn':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaidWithdrawn(erp);
    // Your ERP reported this bill paid, but no longer shows it paid. FeohLedger did not record that payment. Check the bill in the ERP: if it is still owed, resolve this exception and pay it as usual.
    case 'exception.erp_paid_withdrawn_unnamed':
      return l.exceptionDescriptionErpPaidWithdrawnUnnamed;
    // {erp} reports this bill paid, but FeohLedger couldn't record that payment automatically. Check the details, then record the ERP's payment from this exception.
    case 'exception.erp_paid_retry_failed':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaidRetryFailed(erp);
    // Your ERP reports this bill paid, but FeohLedger couldn't record that payment automatically. Check the details, then record the ERP's payment from this exception.
    case 'exception.erp_paid_retry_failed_unnamed':
      return l.exceptionDescriptionErpPaidRetryFailedUnnamed;
    // {erp} reports this bill paid, but FeohLedger's own payment for it settled for less than AP authorized, so the invoice is held at Payment scheduled. Open the payment and either accept the settled amount or void the payment.
    case 'exception.erp_settled_short':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpSettledShort(erp);
    // Your ERP reports this bill paid, but FeohLedger's own payment for it settled for less than AP authorized, so the invoice is held at Payment scheduled. Open the payment and either accept the settled amount or void the payment.
    case 'exception.erp_settled_short_unnamed':
      return l.exceptionDescriptionErpSettledShortUnnamed;
    // {erp} reports this bill voided or cancelled, but FeohLedger had already sent it on for payment, so it was not cancelled here. Check whether a payment is on its way to the supplier, and stop or recover it if the bill is really void.
    case 'exception.erp_voided_after_post':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpVoidedAfterPost(erp);
    // Your ERP reports this bill voided or cancelled, but FeohLedger had already sent it on for payment, so it was not cancelled here. Check whether a payment is on its way to the supplier, and stop or recover it if the bill is really void.
    case 'exception.erp_voided_after_post_unnamed':
      return l.exceptionDescriptionErpVoidedAfterPostUnnamed;
    // {erp} did not record this invoice's payment, so the bill still shows as unpaid there although the money was paid. Resolve what stopped it, then use Retry ERP sync on the payment's run in Payments.
    case 'exception.erp_payment_writeback_failed':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaymentWritebackFailed(erp);
    // Your ERP did not record this invoice's payment, so the bill still shows as unpaid there although the money was paid. Resolve what stopped it, then use Retry ERP sync on the payment's run in Payments.
    case 'exception.erp_payment_writeback_failed_unnamed':
      return l.exceptionDescriptionErpPaymentWritebackFailedUnnamed;
    // {erp} recorded this invoice's payment, but the bill still shows {amount} {currency} open there (a discount or credit taken in FeohLedger). Close the difference in the ERP.
    case 'exception.erp_payment_writeback_balance_open':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      final amount = _money(p['amount'], currency);
      if (amount == null) return null;
      return l.exceptionDescriptionErpPaymentWritebackBalanceOpen(erp, amount);
    // Your ERP recorded this invoice's payment, but the bill still shows {amount} {currency} open there (a discount or credit taken in FeohLedger). Close the difference in the ERP.
    case 'exception.erp_payment_writeback_balance_open_unnamed':
      final amount = _money(p['amount'], currency);
      if (amount == null) return null;
      return l.exceptionDescriptionErpPaymentWritebackBalanceOpenUnnamed(
        amount,
      );
    // {erp} still shows this bill paid by a payment that was voided in FeohLedger, because voiding it in the ERP failed. Void that payment in the ERP so the books agree.
    case 'exception.erp_payment_void_writeback_failed':
      final erp = _text(p['erp'], currency);
      if (erp == null) return null;
      return l.exceptionDescriptionErpPaymentVoidWritebackFailed(erp);
    // Your ERP still shows this bill paid by a payment that was voided in FeohLedger, because voiding it in the ERP failed. Void that payment in the ERP so the books agree.
    case 'exception.erp_payment_void_writeback_failed_unnamed':
      return l.exceptionDescriptionErpPaymentVoidWritebackFailedUnnamed;
    // The ERP shows this bill settled, at least in part by a vendor credit applied there. FeohLedger will not pay it until someone confirms what is still owed.
    case 'exception.erp_credit_settlement':
      return l.exceptionDescriptionErpCreditSettlement;
    // The ERP shows this bill paid by a payment FeohLedger did not make, while a FeohLedger payment is scheduled for it. Stop or void one of them before both reach the supplier.
    case 'exception.erp_paid_while_scheduled':
      return l.exceptionDescriptionErpPaidWhileScheduled;
    // FeohLedger paid this invoice, and the ERP shows the bill also paid by a payment FeohLedger did not make — the supplier may have been paid twice. Recover one of the payments.
    case 'exception.erp_paid_twice':
      return l.exceptionDescriptionErpPaidTwice;
    // The ERP shows part of this bill paid or credited outside FeohLedger; a FeohLedger payment for the full amount would overpay it. Reconcile what is still owed before paying.
    case 'exception.erp_partly_paid_outside':
      return l.exceptionDescriptionErpPartlyPaidOutside;
    // The ERP reported this bill paid, and FeohLedger recorded that payment, but the ERP no longer shows it paid (the payment was deleted or voided there). Confirm whether it is still owed.
    case 'exception.erp_recorded_payment_withdrawn':
      return l.exceptionDescriptionErpRecordedPaymentWithdrawn;
    // The bill this invoice was posted as has been deleted or voided in the ERP; FeohLedger will not pay it until someone confirms it is still owed.
    case 'exception.erp_bill_deleted_blocking':
      return l.exceptionDescriptionErpBillDeletedBlocking;
    // The bill this invoice was posted as has been deleted or voided in the ERP. Confirm the books still agree.
    case 'exception.erp_bill_deleted':
      return l.exceptionDescriptionErpBillDeleted;
    // FeohLedger cannot check this invoice's bill in the ERP, because the ERP did not say which bill it created when the invoice was posted, so it will not pay it. Confirm in the ERP whether it is still owed, then record it as paid outside FeohLedger or re-post it.
    case 'exception.erp_bill_unlinked_no_document':
      return l.exceptionDescriptionErpBillUnlinkedNoDocument;
    // FeohLedger cannot check this invoice's bill in the ERP, because the posting did not record which ERP company it went to, so it will not pay it. Confirm in the ERP whether it is still owed, then record it as paid outside FeohLedger or re-post it.
    case 'exception.erp_bill_unlinked_no_company':
      return l.exceptionDescriptionErpBillUnlinkedNoCompany;
    // FeohLedger cannot check this invoice's bill in the ERP, because it was posted to a different ERP company than the one now connected, so it will not pay it. Confirm in the ERP whether it is still owed, then record it as paid outside FeohLedger or re-post it.
    case 'exception.erp_bill_unlinked_other_company':
      return l.exceptionDescriptionErpBillUnlinkedOtherCompany;
    // FeohLedger was about to pay this invoice, but the ERP shows its bill already paid. It was stopped before any money moved. Confirm what is still owed before paying it; if a virtual card was already issued for this invoice, cancel it.
    case 'exception.erp_dispatch_stopped_paid':
      return l.exceptionDescriptionErpDispatchStoppedPaid;
    // FeohLedger was about to pay this invoice, but the ERP shows its bill partly paid. It was stopped before any money moved. Confirm what is still owed before paying it; if a virtual card was already issued for this invoice, cancel it.
    case 'exception.erp_dispatch_stopped_partially_paid':
      return l.exceptionDescriptionErpDispatchStoppedPartiallyPaid;
    // FeohLedger was about to pay this invoice, but the ERP shows its bill deleted or voided. It was stopped before any money moved. Confirm what is still owed before paying it; if a virtual card was already issued for this invoice, cancel it.
    case 'exception.erp_dispatch_stopped_deleted':
      return l.exceptionDescriptionErpDispatchStoppedDeleted;
    // FeohLedger was about to pay this invoice, but the ERP could not confirm its bill is still open and unpaid. It was stopped before any money moved. Confirm what is still owed before paying it; if a virtual card was already issued for this invoice, cancel it.
    case 'exception.erp_dispatch_stopped_unconfirmed':
      return l.exceptionDescriptionErpDispatchStoppedUnconfirmed;
    // A payment the ERP reported was voided in FeohLedger, but the ERP may still show the bill paid. FeohLedger will not pay it again until someone confirms the ERP's payment is undone.
    case 'exception.erp_reported_payment_voided':
      return l.exceptionDescriptionErpReportedPaymentVoided;
    // This invoice's payment settled, but FeohLedger could not finish recording it, so the invoice has not moved to Paid although the money has gone. Nothing retries this automatically: use Retry ERP sync on the payment's run in Payments.
    case 'exception.erp_sync_back_failed':
      return l.exceptionDescriptionErpSyncBackFailed;
  }
  return null;
}
