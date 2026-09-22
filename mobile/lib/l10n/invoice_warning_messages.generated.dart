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
  'self_correction_line_item_math': {
    'lineNumber': 'count',
    'quantity': 'number',
    'unitPrice': 'number',
    'expected': 'number',
    'total': 'number',
  },
  'gl_codes_not_in_chart': {'codes': 'text'},
  'gl_code_stale_prior': {'code': 'text'},
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
  }
  return null;
}
