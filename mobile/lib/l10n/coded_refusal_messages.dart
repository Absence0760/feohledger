/// Stating the backend's coded approval / exception-queue refusals in the
/// reader's language — the mobile half of
/// `backend/app/api/refusals.py::coded_refusal`, and the mirror of the web
/// `frontend/src/lib/api/codedRefusals.ts`.
///
/// A refusal a user is expected to act on arrives as
/// `detail = {code, message, params}`: a stable `code`, the typed `params` its
/// sentence needs (money as exact decimal strings beside their currency), and
/// the English `message` as the fallback. On mobile these reach the reader
/// where an approver acts: the approve / reject / recall verdicts and
/// refusals on an invoice (the detail screen and the approvals swipe), a
/// skipped row of a bulk status change (the invoices list), and resolving /
/// dismissing an exception (the detail screen and the queue swipe), and
/// recording a payment made outside FeohLedger (the Pay tab's queue row and
/// run, backend decisions §251), approving or executing a run (the Pay tab's
/// run actions), and a wrong code at the sign-in MFA step. Only those
/// codes are mapped here; the web registry
/// covers the surfaces mobile does not ship (credit memos, expense reports,
/// the invoice stale-edit prompt).
///
/// **Anything this build cannot state completely → `null`**, and the caller
/// renders its own generic failure (or [ApiException.message]): an unknown
/// code, or a known code whose params are missing or malformed — a sentence
/// with a blank where a figure belongs is worse than none.
library;

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/invoice_status_labels.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/utils/money.dart';

final _exactDecimal = RegExp(r'^-?\d+(\.\d+)?$');
final _currencyCode = RegExp(r'^[A-Za-z]{3}$');

String? _str(Map params, String key) {
  final v = params[key];
  return v is String && v.trim().isNotEmpty ? v : null;
}

String? _currency(Map params, String key) {
  final v = _str(params, key);
  return v != null && _currencyCode.hasMatch(v) ? v.toUpperCase() : null;
}

/// An exact decimal string formatted with its currency, or `null` when either
/// half is malformed.
String? _money(Map params, String amountKey, String currencyKey) {
  final amount = _str(params, amountKey);
  final code = _currency(params, currencyKey);
  if (amount == null || code == null || !_exactDecimal.hasMatch(amount)) {
    return null;
  }
  return formatMoneyString(amount, currency: code);
}

/// A figure in the INVOICE's own currency, which is `null` for an invoice
/// that names none. Then it renders bare (the `formatMoneyString` rule), never
/// with a borrowed symbol; only a present-but-malformed currency falls back.
String? _invoiceMoney(Map params, String amountKey, String currencyKey) {
  if (params[currencyKey] != null) {
    return _money(params, amountKey, currencyKey);
  }
  final amount = _str(params, amountKey);
  if (amount == null || !_exactDecimal.hasMatch(amount)) return null;
  return formatMoneyString(amount);
}

/// The structuring and measured-figure notes a money-gate refusal carries
/// after its sentence, or `null` when one of them is malformed.
List<String>? _gateNotes(AppLocalizations l, Map params) {
  final notes = <String>[];
  if (params['recent_spend'] != null) {
    final recent = _invoiceMoney(params, 'recent_spend', 'currency');
    final aggregate = _invoiceMoney(params, 'aggregate_amount', 'currency');
    final days = params['window_days'];
    if (recent == null || aggregate == null) return null;
    if (days is! int || days < 0) return null;
    notes.add(l.codedRefusalGateStructuring(recent, days, aggregate));
  }
  final limitCurrency = _currency(params, 'limit_currency');
  if (limitCurrency == null) return null;
  if (params['expressible'] == false) {
    notes.add(l.codedRefusalGateInexpressible(limitCurrency));
  } else if (params['measured_amount'] != null) {
    final measured = _money(params, 'measured_amount', 'limit_currency');
    if (measured == null) return null;
    notes.add(l.codedRefusalGateMeasured(measured, limitCurrency));
  }
  return notes;
}

String? _approvalGate(AppLocalizations l, Map params, {required bool cfo}) {
  final amount = _invoiceMoney(params, 'amount', 'currency');
  if (amount == null) return null;
  final String head;
  if (params['limit'] == null) {
    // Only the CFO gate has an unknown-limit sentence; the max gate names
    // its malformed threshold under a code of its own.
    if (!cfo) return null;
    head = l.codedRefusalApprovalCfoRequiredUnknownLimit(amount);
  } else {
    final limit = _money(params, 'limit', 'limit_currency');
    if (limit == null) return null;
    head = cfo
        ? l.codedRefusalApprovalCfoRequired(amount, limit)
        : l.codedRefusalApprovalMaxExceeded(amount, limit);
  }
  final notes = _gateNotes(l, params);
  return notes == null ? null : [head, ...notes].join(' ');
}

/// An `InvoiceStatus` value named by its label, or `null` for a value this
/// build does not know (never [InvoiceStatus.fromString]'s `new` fallback).
String? _status(AppLocalizations l, Map params, String key) {
  final v = _str(params, key);
  for (final s in InvoiceStatus.values) {
    if (s.value == v) return invoiceStatusLabel(l, s);
  }
  return null;
}

/// The invoice fields `invoice_required_fields_missing` names, labelled as
/// the edit sheet labels them; an unknown field keeps its server name.
String? _requiredFields(AppLocalizations l, Map params) {
  final fields = params['fields'];
  if (fields is! List || fields.isEmpty) return null;
  if (!fields.every((f) => f is String && f.isNotEmpty)) return null;
  final labels = fields.cast<String>().map((f) => switch (f) {
        'vendor' => l.invoiceEditVendor,
        'invoice_number' => l.invoiceEditInvoiceNumber,
        'amount' => l.invoiceEditAmount,
        _ => f,
      });
  return l.codedRefusalInvoiceRequiredFieldsMissing(labels.join(', '));
}

/// A `Payment.status` named by its label, or `null` for one this build does
/// not know.
String? _paymentStatus(AppLocalizations l, Map params, String key) =>
    switch (_str(params, key)) {
      'pending' => l.paymentStatusPending,
      'processing' => l.paymentStatusProcessing,
      'completed' => l.paymentStatusCompleted,
      'failed' => l.paymentStatusFailed,
      'cancelled' => l.paymentStatusCancelled,
      _ => null,
    };

/// A run id as the web shows it in a sentence: its first eight characters.
String? _runRef(Map params) {
  final run = _str(params, 'payment_run_id');
  if (run == null) return null;
  return run.length > 8 ? run.substring(0, 8) : run;
}

/// The `external_payment_*` sentence. The run-level record names the invoice
/// that stopped the WHOLE run in `params.invoice_number`, so a sentence from
/// that door is prefixed with it — and a malformed number states nothing
/// rather than a sentence about an unnamed invoice. Mirrors the web
/// `forInvoice`.
String? _forInvoice(AppLocalizations l, Map params, String? sentence) {
  if (sentence == null || !params.containsKey('invoice_number')) {
    return sentence;
  }
  final invoice = _str(params, 'invoice_number');
  return invoice == null
      ? null
      : l.codedRefusalExternalPaymentForInvoice(invoice, sentence);
}

String? _externalPayment(AppLocalizations l, String code, Map params) {
  switch (code) {
    case 'external_payment_not_payable':
      final status = _status(l, params, 'status');
      return status == null
          ? null
          : l.codedRefusalExternalPaymentNotPayable(status);
    case 'external_payment_segregation':
      return l.codedRefusalExternalPaymentSegregation;
    // The row's blocked chip already names the exception type; the sentence
    // says what to do about it.
    case 'external_payment_blocking_exception':
      return l.codedRefusalExternalPaymentBlockingException;
    case 'external_payment_card_live':
      return l.codedRefusalExternalPaymentCardLive;
    case 'external_payment_credit_conflict':
      return l.codedRefusalExternalPaymentCreditConflict;
    case 'external_payment_nothing_to_pay':
      return l.codedRefusalExternalPaymentNothingToPay;
    case 'external_payment_payment_live':
      final status = _paymentStatus(l, params, 'payment_status');
      return status == null
          ? null
          : l.codedRefusalExternalPaymentPaymentLive(status);
    // With `exception_id` the invoice also holds an ERP split only its
    // exception can book (backend decisions §426, round-13 amendment).
    case 'external_payment_in_run':
      final run = _runRef(params);
      if (run == null) return null;
      return _str(params, 'exception_id') == null
          ? l.codedRefusalExternalPaymentInRun(run)
          : l.codedRefusalExternalPaymentErpSplitInRun(run);
    case 'external_payment_amount_mismatch':
      final amount = _invoiceMoney(params, 'amount', 'currency');
      return amount == null
          ? null
          : l.codedRefusalExternalPaymentAmountMismatch(amount);
    case 'external_payment_amount_changed':
      final staged = _invoiceMoney(params, 'staged_amount', 'currency');
      final amount = _invoiceMoney(params, 'amount', 'currency');
      return staged == null || amount == null
          ? null
          : l.codedRefusalExternalPaymentAmountChanged(staged, amount);
    case 'external_payment_erp_split':
      final run = _runRef(params);
      return run == null
          ? l.codedRefusalExternalPaymentErpSplit
          : l.codedRefusalExternalPaymentErpSplitInRun(run);
    case 'external_payment_paid_on_future':
      return l.codedRefusalExternalPaymentPaidOnFuture;
  }
  return null;
}

/// The codes this module states — pinned against the backend in the test.
const codedRefusalCodes = <String>{
  'approval_segregation',
  'approval_segregation_receiver',
  'approval_segregation_inspector',
  'approval_level_reuse',
  'approval_not_named_approver',
  'approval_max_amount_exceeded',
  'approval_max_amount_misconfigured',
  'approval_cfo_required',
  'segregation_raiser',
  'segregation_implicated',
  'segregation_approver',
  'invoice_stale_approval',
  'invoice_not_recallable',
  'invoice_recall_in_erp',
  'invoice_recall_claimed',
  'invoice_not_in_review',
  'invoice_bulk_not_found',
  'invoice_status_immutable',
  'invoice_recall_one_at_a_time',
  'invoice_required_fields_missing',
  'invoice_transition_invalid',
  'invoice_transition_recall_only',
  'invoice_once_approved_not_reread',
  // The no-rail pilot (backend decisions §251): the mode refusal every
  // dispatching endpoint answers on a record-only tenant, the record doors'
  // own refusals, and the run verdicts the run-level record reads.
  'payments_record_only',
  'external_payment_not_payable',
  'external_payment_segregation',
  'external_payment_blocking_exception',
  'external_payment_card_live',
  'external_payment_credit_conflict',
  'external_payment_nothing_to_pay',
  'external_payment_payment_live',
  'external_payment_in_run',
  'external_payment_amount_mismatch',
  'external_payment_amount_changed',
  'external_payment_erp_split',
  'external_payment_paid_on_future',
  'payment_run_segregation',
  'payment_run_awaiting_cfo',
  'payment_run_not_recordable',
  'payment_run_nothing_to_record',
  // The sign-in challenge's wrong code (`api/auth.py::verify_mfa`).
  'mfa_code_invalid',
};

/// The localized refusal for [detail] (a decoded error `detail`), or `null`
/// when [detail] is not a coded refusal this build can state.
String? localizeCodedRefusal(AppLocalizations l, Object? detail) {
  if (detail is! Map) return null;
  final code = detail['code'];
  if (code is! String) return null;
  final raw = detail['params'];
  final params = raw is Map ? raw : const {};
  switch (code) {
    case 'approval_segregation':
      return l.codedRefusalApprovalSegregation;
    case 'approval_segregation_receiver':
      return l.codedRefusalApprovalSegregationReceiver;
    case 'approval_segregation_inspector':
      return l.codedRefusalApprovalSegregationInspector;
    case 'approval_level_reuse':
      return l.codedRefusalApprovalLevelReuse;
    case 'approval_not_named_approver':
      return l.codedRefusalApprovalNotNamedApprover;
    case 'approval_max_amount_misconfigured':
      return l.codedRefusalApprovalMaxMisconfigured;
    case 'approval_max_amount_exceeded':
      return _approvalGate(l, params, cfo: false);
    case 'approval_cfo_required':
      return _approvalGate(l, params, cfo: true);
    // The same codes `/bulk/resolve` reports per skipped row.
    case 'segregation_raiser':
      return l.codedRefusalExceptionSegregationRaiser;
    case 'segregation_implicated':
      return l.codedRefusalExceptionSegregationImplicated;
    // Backend decisions §420: an approver may not clear a receipt or
    // inspection hold on the invoice they approved.
    case 'segregation_approver':
      return l.codedRefusalExceptionSegregationApprover;
    // The invoice changed after this screen loaded it; nothing was approved.
    case 'invoice_stale_approval':
      return l.codedRefusalInvoiceStaleApproval;
    // `services/review.recall_refusal` — recalling an approval (backend
    // decisions §374). `params.claim` names the rail; an unknown one keeps the
    // caller's generic failure.
    case 'invoice_not_recallable':
      return l.codedRefusalInvoiceNotRecallable;
    case 'invoice_recall_in_erp':
      return l.codedRefusalInvoiceRecallInErp;
    case 'invoice_recall_claimed':
      return switch (_str(params, 'claim')) {
        'payment' => l.codedRefusalPaymentBlocksRecall,
        'virtual_card' => l.codedRefusalCardBlocksRecall,
        _ => null,
      };
    // `services/review.refuse_unless_in_review` — the approve / reject
    // verdict on an invoice not in review, and a bulk approve / reject skip
    // (backend decisions §418).
    case 'invoice_not_in_review':
      return l.codedRefusalInvoiceNotInReview;
    // `api/invoices.bulk_status_change`'s own skips (backend decisions §387)
    // and the state machine's 409s it reports per row. A status or target
    // this build does not know keeps the server's sentence.
    case 'invoice_bulk_not_found':
      return l.codedRefusalInvoiceBulkNotFound;
    case 'invoice_status_immutable':
      final status = _status(l, params, 'status');
      return status == null
          ? null
          : l.codedRefusalInvoiceStatusImmutable(status);
    case 'invoice_recall_one_at_a_time':
      return l.codedRefusalInvoiceRecallOneAtATime;
    case 'invoice_required_fields_missing':
      return _requiredFields(l, params);
    case 'invoice_transition_invalid':
    case 'invoice_transition_recall_only':
      final from = _status(l, params, 'status');
      final to = _status(l, params, 'target');
      if (from == null || to == null) return null;
      return code == 'invoice_transition_invalid'
          ? l.codedRefusalInvoiceTransitionInvalid(from, to)
          : l.codedRefusalInvoiceTransitionRecallOnly(from, to);
    case 'invoice_once_approved_not_reread':
      return l.codedRefusalInvoiceOnceApprovedNotReread;
    // `api/payments.py::_refuse_record_only`: the sentence is the same for
    // every `params.reason`.
    case 'payments_record_only':
      return l.codedRefusalPaymentsRecordOnly;
    // `services/payment_controls.py` — the run verdicts. Maker-checker names
    // the step in `params.step` for the steps that are not approve / execute.
    case 'payment_run_segregation':
      return switch (_str(params, 'step')) {
        'record_outside' => l.codedRefusalPaymentRunSegregationRecord,
        'nacha_export' => l.codedRefusalPaymentRunSegregationExport,
        _ => l.codedRefusalPaymentRunSegregation,
      };
    case 'payment_run_awaiting_cfo':
      return l.codedRefusalPaymentRunAwaitingCfo;
    case 'payment_run_not_recordable':
      return l.codedRefusalPaymentRunNotRecordable;
    case 'payment_run_nothing_to_record':
      return l.codedRefusalPaymentRunNothingToRecord;
    // `api/auth.py::verify_mfa` — a wrong authenticator code at sign-in (the
    // MFA screen; backend decisions §NEW-R1).
    case 'mfa_code_invalid':
      return l.codedRefusalMfaCodeInvalid;
  }
  if (code.startsWith('external_payment_')) {
    return _forInvoice(l, params, _externalPayment(l, code, params));
  }
  return null;
}
