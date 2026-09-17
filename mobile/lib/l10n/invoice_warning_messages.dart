/// Turning an `InvoiceWarning` into a sentence in the reader's language — the
/// mobile half of `backend/app/services/invoice_warning_catalog.py`, and the
/// mirror of the web `frontend/src/lib/api/invoiceWarnings.ts`.
///
/// The backend composes each finding from the row's own data ("PO 4412 not
/// found", a variance, an amount) and emits a stable `code` plus typed
/// `params` beside the English `message`. This maps that pair onto an ARB key
/// and a set of locale-formatted parameters.
///
/// Two properties make it safe to render:
///
/// * **Unknown or absent code → `null`**, and the caller renders
///   `warning.message`, the backend's own sentence. Every warning persisted
///   before the catalogue existed carries no code at all (`refresh_warnings`
///   re-derives one on the invoice's next write, and nothing backfills), so
///   the fallback is the NORMAL path for an untouched row, not an edge case.
/// * **A known code with a missing or malformed parameter → `null` too.** A
///   half-filled translated sentence is worse than the server's complete one.
///
/// **The code→key map is not hand-authored.** It is transcribed from
/// `frontend/src/lib/api/invoiceWarningMessages.generated.ts`, which
/// `pnpm gen:warning-messages` generates from the backend catalogue, and
/// `test/l10n/invoice_warning_messages_test.dart` fails if the two ever hold
/// different codes or different parameter kinds. Adding a `WarningSpec`
/// backend-side therefore reddens mobile CI, not just the web's.
library;

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/utils/numbers.dart';

/// What each warning parameter holds, and therefore how it renders.
///
/// Kept beside the sentences because the two are one fact: the localized
/// message says `{amount}` where the English fallback says
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

/// The sentence to show for one warning: localized when the code is known,
/// otherwise the backend's own English `message`.
///
/// Every render site goes through this, so none of them can end up showing a
/// finding in a different language from its neighbour.
String invoiceWarningText(AppLocalizations l, InvoiceWarning w) =>
    localizeInvoiceWarning(l, w) ?? w.message;

/// The localized sentence, or `null` when this build cannot state the finding
/// — see the module note. Split out so a test can tell "fell back" apart from
/// "localized to something that happens to equal the English".
String? localizeInvoiceWarning(AppLocalizations l, InvoiceWarning w) {
  final p = w.params;
  // A money parameter is denominated in the invoice's own currency, which the
  // backend puts in the params beside it. Absent means the figure renders
  // bare — never a substituted default (`docs/decisions.md` §79/§82).
  final currency = p['currency'];
  switch (w.code) {
    case 'missing_vendor_name':
      return l.invoiceWarningMissingVendorName;
    case 'missing_invoice_number':
      return l.invoiceWarningMissingInvoiceNumber;
    case 'missing_amount':
      return l.invoiceWarningMissingAmount;
    case 'duplicate_invoice_number':
      return l.invoiceWarningDuplicateInvoiceNumber;
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
    case 'duplicate_similar_unnumbered_unnamed_vendor':
      final similarity = _percent(p['similarity'], currency);
      if (similarity == null) return null;
      final crossEntityCount = _count(p['crossEntityCount']);
      if (crossEntityCount == null) return null;
      return l.invoiceWarningDuplicateSimilarUnnumberedUnnamedVendor(
        similarity,
        crossEntityCount,
      );
    case 'duplicate_similar_cross_entity':
      final similarity = _percent(p['similarity'], currency);
      if (similarity == null) return null;
      return l.invoiceWarningDuplicateSimilarCrossEntity(similarity);
    case 'round_amount':
      final amount = _money(p['amount'], currency);
      if (amount == null) return null;
      return l.invoiceWarningRoundAmount(amount);
    case 'future_invoice_date':
      return l.invoiceWarningFutureInvoiceDate;
    case 'rush_payment':
      final days = _count(p['days']);
      if (days == null) return null;
      return l.invoiceWarningRushPayment(days);
    case 'past_due':
      return l.invoiceWarningPastDue;
    case 'unverified_vendor':
      return l.invoiceWarningUnverifiedVendor;
    case 'personal_email_domain':
      final domain = _text(p['domain'], currency);
      if (domain == null) return null;
      return l.invoiceWarningPersonalEmailDomain(domain);
    case 'new_vendor_large_amount':
      final days = _count(p['days']);
      if (days == null) return null;
      final amount = _money(p['amount'], currency);
      if (amount == null) return null;
      return l.invoiceWarningNewVendorLargeAmount(days, amount);
    case 'remit_to_changed':
      return l.invoiceWarningRemitToChanged;
    case 'amount_above_vendor_mean':
      final amount = _money(p['amount'], currency);
      if (amount == null) return null;
      final sigma = _number(p['sigma'], currency);
      if (sigma == null) return null;
      final mean = _money(p['mean'], currency);
      if (mean == null) return null;
      return l.invoiceWarningAmountAboveVendorMean(amount, sigma, mean);
    case 'llm_anomaly':
      final reason = _text(p['reason'], currency);
      if (reason == null) return null;
      return l.invoiceWarningLlmAnomaly(reason);
    case 'line_total_mismatch':
      final lineItemsTotal = _money(p['lineItemsTotal'], currency);
      if (lineItemsTotal == null) return null;
      final headerAmount = _money(p['headerAmount'], currency);
      if (headerAmount == null) return null;
      return l.invoiceWarningLineTotalMismatch(lineItemsTotal, headerAmount);
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
    case 'po_not_found':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningPoNotFound(poNumber);
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
    case 'po_partial_receipt':
      final matchType = _text(p['matchType'], currency);
      if (matchType == null) return null;
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningPoPartialReceipt(matchType, poNumber);
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
    case 'po_over_receipt_unquantified':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningPoOverReceiptUnquantified(poNumber);
    case 'quality_inspection_failed':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningQualityInspectionFailed(poNumber);
    case 'quality_inspection_failed_notes':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      final notes = _text(p['notes'], currency);
      if (notes == null) return null;
      return l.invoiceWarningQualityInspectionFailedNotes(poNumber, notes);
    case 'quality_inspection_missing':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningQualityInspectionMissing(poNumber);
    case 'quality_partial_acceptance':
      final acceptedQuantity = _number(p['acceptedQuantity'], currency);
      if (acceptedQuantity == null) return null;
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningQualityPartialAcceptance(
        acceptedQuantity,
        poNumber,
      );
    case 'quality_partial_acceptance_unquantified':
      final poNumber = _text(p['poNumber'], currency);
      if (poNumber == null) return null;
      return l.invoiceWarningQualityPartialAcceptanceUnquantified(poNumber);
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
    case 'contract_terminated':
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      return l.invoiceWarningContractTerminated(contractNumber);
    case 'contract_cancelled':
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      return l.invoiceWarningContractCancelled(contractNumber);
    case 'contract_vendor_mismatch':
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      return l.invoiceWarningContractVendorMismatch(contractNumber);
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
    case 'contract_gl_not_allowed':
      final glAccount = _text(p['glAccount'], currency);
      if (glAccount == null) return null;
      final contractNumber = _text(p['contractNumber'], currency);
      if (contractNumber == null) return null;
      return l.invoiceWarningContractGlNotAllowed(glAccount, contractNumber);
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
    case 'self_correction_date_ordering':
      final dueDate = _date(p['dueDate'], currency);
      if (dueDate == null) return null;
      final invoiceDate = _date(p['invoiceDate'], currency);
      if (invoiceDate == null) return null;
      return l.invoiceWarningSelfCorrectionDateOrdering(dueDate, invoiceDate);
    case 'self_correction_line_items_sum':
      final lineItemsTotal = _number(p['lineItemsTotal'], currency);
      if (lineItemsTotal == null) return null;
      final amount = _number(p['amount'], currency);
      if (amount == null) return null;
      return l.invoiceWarningSelfCorrectionLineItemsSum(lineItemsTotal, amount);
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
    case 'gl_codes_not_in_chart':
      final codes = _text(p['codes'], currency);
      if (codes == null) return null;
      return l.invoiceWarningGlCodesNotInChart(codes);
    case 'gl_code_stale_prior':
      final code = _text(p['code'], currency);
      if (code == null) return null;
      return l.invoiceWarningGlCodeStalePrior(code);
  }
  return null;
}

// Each formatter takes the same (raw, currency) pair so the arms above can be
// written one way per kind; only `_money` has anything to do with the currency.

/// A plural selector: an integer, because it drives ICU plural selection.
int? _count(String? raw) => raw == null ? null : int.tryParse(raw.trim());

String? _text(String? raw, String? currency) => raw;

/// Exact decimal digits off the wire, in the invoice's own currency.
String? _money(String? raw, String? currency) =>
    raw == null ? null : formatMoneyString(raw, currency: currency);

String? _percent(String? raw, String? currency) =>
    raw == null ? null : formatPercentString(raw);

String? _number(String? raw, String? currency) =>
    raw == null ? null : formatDecimalString(raw);

String? _date(String? raw, String? currency) {
  if (raw == null) return null;
  final parsed = DateTime.tryParse(raw);
  // Not a date this build can parse: the server's own string beats a blank.
  return parsed == null ? raw : formatDate(parsed);
}
