/// Stating a coded ERP refusal in the reader's language — the mobile mirror of
/// the web `frontend/src/lib/api/erpRefusals.ts` (decisions §284, §332).
///
/// A refused ERP call is recorded as `{code, params}`
/// (`backend/app/services/erp.py::erp_refusal_record`). The code is one of
/// `backend/app/services/erp_adapters/base.py::ERP_REFUSAL_CODES`; `params`
/// carries the adapter's `provider` literal and, per code, the GL `accounts`,
/// Intuit's numeric `intuit_code`, an exception `exception_type`, a payment
/// `claim`, a vendor `hold`, a call-budget `reason`, and `possibly_posted`.
///
/// On mobile it reaches the reader on the exception detail screen, for the
/// payment write-back rows (`erp_report.kind` = `payment_writeback_failed` /
/// `payment_void_writeback_failed`), whose catalogued sentence says only that
/// the write-back failed — this says why, and what to do.
///
/// [erpRefusalText] returns `null` for anything it cannot state completely: an
/// unknown code (a newer backend), or a known code whose params are missing or
/// malformed. [erpRefusalNote] turns that `null` into the generic
/// `erpRefusalUnknown` sentence, never the raw code.
///
/// `test/l10n/erp_refusal_messages_test.dart` pins [erpRefusalCodes] to the
/// web registry's keys, which the web test pins to the backend's.
library;

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';

/// An open duplicate / fraud exception refused the push (decisions §286).
const exceptionBlocksErpExport = 'exception_blocks_erp_export';

/// FeohLedger is already paying the invoice: a live payment or card (§309).
const paymentBlocksErpExport = 'payment_blocks_erp_export';

/// The invoice's vendor is one the payment path will not pay.
const vendorBlocksErpExport = 'vendor_blocks_erp_export';

/// The ERP's shared API quota stopped the call before it was made (§322).
const erpCallBudgetExhausted = 'erp_call_budget_exhausted';

/// `erp_report.kind` of the rows whose refusal this states.
const erpWritebackRefusalKinds = {
  'payment_writeback_failed',
  'payment_void_writeback_failed',
};

typedef _Sentence = String Function(AppLocalizations l, String provider);

/// Every code that names only `{provider}`, by code.
final Map<String, _Sentence> _providerSentences = {
  'vendor_not_linked': (l, p) => l.erpRefusalVendorNotLinked(p),
  'amount_mismatch': (l, p) => l.erpRefusalAmountMismatch(p),
  'line_amount_missing': (l, p) => l.erpRefusalLineAmountMissing,
  'posted_total_mismatch': (l, p) => l.erpRefusalPostedTotalMismatch(p),
  'posted_total_unconfirmed': (l, p) => l.erpRefusalPostedTotalUnconfirmed(p),
  'previous_bill_removed': (l, p) => l.erpRefusalPreviousBillRemoved(p),
  'tax_not_itemised': (l, p) => l.erpRefusalTaxNotItemised(p),
  'tax_rate_unresolved': (l, p) => l.erpRefusalTaxRateUnresolved(p),
  'missing_dates': (l, p) => l.erpRefusalMissingDates(p),
  'bill_refused': (l, p) => l.erpRefusalBillRefused(p),
  'duplicate_document_number': (l, p) => l.erpRefusalDuplicateNumber(p),
  'duplicate_invoice_number': (l, p) => l.erpRefusalDuplicateNumber(p),
  'doc_number_too_long': (l, p) => l.erpRefusalDocNumberTooLong(p),
  'currency_unknown': (l, p) => l.erpRefusalCurrencyUnknown(p),
  'currency_not_enabled': (l, p) => l.erpRefusalCurrencyNotEnabled(p),
  'period_closed': (l, p) => l.erpRefusalPeriodClosed(p),
  'vendor_inactive': (l, p) => l.erpRefusalVendorInactive(p),
  'company_subscription_inactive': (l, p) =>
      l.erpRefusalCompanySubscriptionInactive(p),
  'idempotency_lookup_incomplete': (l, p) =>
      l.erpRefusalIdempotencyLookupIncomplete(p),
  'purchases_not_permitted': (l, p) => l.erpRefusalPurchasesNotPermitted(p),
  'existing_invoice_not_open': (l, p) => l.erpRefusalExistingInvoiceNotOpen(p),
  'line_amount_precision': (l, p) => l.erpRefusalLineAmountPrecision(p),
  'pay_to_vendor_differs': (l, p) => l.erpRefusalPayToVendorDiffers(p),
  'bill_not_posted': (l, p) => l.erpRefusalBillNotPosted(p),
  'bill_rejected': (l, p) => l.erpRefusalBillRejected(p),
  'currency_not_set_up': (l, p) => l.erpRefusalCurrencyNotSetUp(p),
  'currency_not_supported': (l, p) => l.erpRefusalCurrencyNotSupported(p),
  'foreign_currency_supplier': (l, p) => l.erpRefusalForeignCurrencySupplier(p),
  'correlation_total_mismatch': (l, p) =>
      l.erpRefusalCorrelationTotalMismatch(p),
  'tax_type_not_resolved': (l, p) => l.erpRefusalTaxTypeNotResolved(p),
  'tax_mismatch': (l, p) => l.erpRefusalTaxMismatch(p),
  'tax_not_stated': (l, p) => l.erpRefusalTaxNotStated(p),
  'subscription_key_missing': (l, p) => l.erpRefusalSubscriptionKeyMissing(p),
  'ap_account_not_configured': (l, p) => l.erpRefusalApAccountNotConfigured(p),
  'currency_not_configured': (l, p) => l.erpRefusalCurrencyNotConfigured(p),
  'currency_mismatch': (l, p) => l.erpRefusalCurrencyMismatch(p),
  'invoice_date_missing': (l, p) => l.erpRefusalInvoiceDateMissing(p),
  'due_date_missing': (l, p) => l.erpRefusalDueDateMissing(p),
  'amount_not_positive': (l, p) => l.erpRefusalAmountNotPositive(p),
  'job_unconfirmed': (l, p) => l.erpRefusalJobUnconfirmed(p),
  'job_failed': (l, p) => l.erpRefusalJobFailed(p),
  'bill_left_unposted': (l, p) => l.erpRefusalBillLeftUnposted(p),
  'merge_sync_pending': (l, p) => l.erpRefusalMergeSyncPending(p),
  'payment_account_not_mapped': (l, p) =>
      l.erpRefusalPaymentAccountNotMapped(p),
  'bill_not_in_erp': (l, p) => l.erpRefusalBillNotInErp(p),
  'payment_exceeds_balance': (l, p) => l.erpRefusalPaymentExceedsBalance(p),
  'bill_payment_voided': (l, p) => l.erpRefusalBillPaymentVoided(p),
  'payments_scope_missing': (l, p) => l.erpRefusalPaymentsScopeMissing(p),
};

/// The refusals raised outside an adapter, which name no provider.
final Map<String, String Function(AppLocalizations l)> _providerless = {
  'not_connected': (l) => l.erpRefusalNotConnected,
  'erp_credentials_unavailable': (l) => l.erpRefusalCredentialsUnavailable,
  'invoice_due_before_date': (l) => l.erpRefusalInvoiceDueBeforeDate,
};

/// Codes whose sentence needs more than `{provider}` — stated below.
const _special = {
  'account_not_linked',
  'account_inactive',
  'reference_invalid',
  'intuit_fault',
  erpCallBudgetExhausted,
  exceptionBlocksErpExport,
  paymentBlocksErpExport,
  vendorBlocksErpExport,
  'erp_retries_exhausted',
  'erp_push_interrupted',
};

/// Every code this build states. Pinned against the web registry.
final Set<String> erpRefusalCodes = {
  ..._providerSentences.keys,
  ..._providerless.keys,
  ..._special,
};

final _intuitCode = RegExp(r'^\d{1,8}$');

String? _nonBlank(Object? v) => v is String && v.trim().isNotEmpty ? v : null;

/// A list of non-empty strings, or null.
List<String>? _accounts(Object? v) {
  if (v is! List) return null;
  if (!v.every((a) => a is String && a.isNotEmpty)) return null;
  return v.cast<String>();
}

/// The exception types that block an ERP push (`api/payments.
/// ERP_EXPORT_BLOCKING_EXCEPTION_TYPES`). Another type cannot be named in the
/// reader's language here, so its refusal is not stated.
String? _exceptionTypeLabel(AppLocalizations l, Object? type) => switch (type) {
  'duplicate' => l.erpRefusalTypeDuplicate,
  'fraud_flag' => l.erpRefusalTypeFraudFlag,
  _ => null,
};

/// The localized, actionable sentence for a recorded `{code, params}` refusal,
/// or `null` when this build cannot state it completely. Never throws.
String? erpRefusalText(AppLocalizations l, Object? refusal) {
  if (refusal is! Map) return null;
  final code = refusal['code'];
  if (code is! String || code.isEmpty) return null;
  final rawParams = refusal['params'];
  final Map params = rawParams is Map ? rawParams : const {};
  final possiblyPosted = params['possibly_posted'] == true;

  if (_providerless[code] case final sentence?) return sentence(l);

  switch (code) {
    // A push that made no attempt (decisions §428): no provider is named, and
    // a possibly-posted one adds where to look first.
    case 'erp_retries_exhausted' || 'erp_push_interrupted':
      final plain = code == 'erp_retries_exhausted'
          ? l.erpRefusalRetriesExhausted
          : l.erpRefusalPushInterrupted;
      return possiblyPosted
          ? '$plain ${l.erpRefusalVendorHoldPossiblyPosted}'
          : plain;
    case exceptionBlocksErpExport:
      final type = _exceptionTypeLabel(l, params['exception_type']);
      if (type == null) return null;
      // An earlier attempt may have created the bill anyway: say where to
      // look first.
      return possiblyPosted
          ? l.erpRefusalExceptionBlocksExportPossiblyPosted(type)
          : l.erpRefusalExceptionBlocksExport(type);
    case paymentBlocksErpExport:
      return switch (params['claim']) {
        'payment' =>
          possiblyPosted
              ? l.erpRefusalPaymentBlocksExportPossiblyPosted
              : l.erpRefusalPaymentBlocksExport,
        'virtual_card' =>
          possiblyPosted
              ? l.erpRefusalCardBlocksExportPossiblyPosted
              : l.erpRefusalCardBlocksExport,
        _ => null,
      };
    case vendorBlocksErpExport:
      final plain = switch (params['hold']) {
        'payments_blocked' => l.erpRefusalVendorBlocked,
        'sanctions_match' => l.erpRefusalVendorSanctionsMatch,
        'sanctions_review' => l.erpRefusalVendorSanctionsReview,
        'vendor_rejected' => l.erpRefusalVendorRejected,
        'vendor_status_unknown' => l.erpRefusalVendorStatusUnknown,
        _ => null,
      };
      if (plain == null) return null;
      // What to do stays first; a possibly-posted refusal adds where to look.
      return possiblyPosted
          ? '$plain ${l.erpRefusalVendorHoldPossiblyPosted}'
          : plain;
  }

  // Every other sentence names the adapter's fixed literal ("QuickBooks
  // Online"); a refusal recorded without one cannot be stated.
  final provider = _nonBlank(params['provider']);
  if (provider == null) return null;

  switch (code) {
    case 'account_not_linked':
      final accounts = _accounts(params['accounts']);
      if (accounts == null) return null;
      if (accounts.isEmpty)
        return l.erpRefusalAccountNotLinkedUncoded(provider);
      return l.erpRefusalAccountNotLinked(provider, accounts.join(', '));
    case 'account_inactive':
      final accounts = _accounts(params['accounts']);
      if (accounts == null || accounts.isEmpty) return null;
      return l.erpRefusalAccountInactive(provider, accounts.join(', '));
    case erpCallBudgetExhausted:
      return switch (params['reason']) {
        'tenant_budget' => l.erpRefusalCallBudgetTenant(provider),
        'subscription_quota' => l.erpRefusalCallBudgetQuota(provider),
        'rate_limited' => l.erpRefusalCallBudgetRateLimited(provider),
        _ => null,
      };
    case 'reference_invalid' || 'intuit_fault':
      final intuit = params['intuit_code'];
      if (intuit is! String || !_intuitCode.hasMatch(intuit)) return null;
      return code == 'reference_invalid'
          ? l.erpRefusalReferenceInvalid(provider, intuit)
          : l.erpRefusalIntuitFault(provider, intuit);
  }

  return _providerSentences[code]?.call(l, provider);
}

/// Why the ERP refused a payment write-back, for the exception detail screen,
/// or `null` on any other row and on a write-back that failed without a coded
/// refusal (its catalogued sentence already says it failed). A code this build
/// cannot state reads as the generic [AppLocalizations.erpRefusalUnknown] —
/// never the raw code. [erp] is the ERP's name, or the reader's "your ERP".
String? erpRefusalNote(
  AppLocalizations l,
  String? reportKind,
  Object? refusal,
  String erp,
) {
  if (!erpWritebackRefusalKinds.contains(reportKind)) return null;
  if (refusal is! Map) return null;
  final code = refusal['code'];
  if (code is! String || code.isEmpty) return null;
  return erpRefusalText(l, refusal) ?? l.erpRefusalUnknown(erp);
}
