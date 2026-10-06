/// Stating the backend's coded approval / exception-queue refusals in the
/// reader's language — the mobile half of
/// `backend/app/api/refusals.py::coded_refusal`, and the mirror of the web
/// `frontend/src/lib/api/codedRefusals.ts`.
///
/// A refusal a user is expected to act on arrives as
/// `detail = {code, message, params}`: a stable `code`, the typed `params` its
/// sentence needs (money as exact decimal strings beside their currency), and
/// the English `message` as the fallback. On mobile these reach the reader
/// where an approver acts: approving an invoice (the detail screen and the
/// approvals swipe) and resolving / dismissing an exception (the detail screen
/// and the queue swipe). Only those codes are mapped here; the web registry
/// covers the surfaces mobile does not ship (credit memos, expense reports,
/// the invoice stale-edit prompt).
///
/// **Anything this build cannot state completely → `null`**, and the caller
/// renders its own generic failure (or [ApiException.message]): an unknown
/// code, or a known code whose params are missing or malformed — a sentence
/// with a blank where a figure belongs is worse than none.
library;

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
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

/// The codes this module states — pinned against the backend in the test.
const codedRefusalCodes = <String>{
  'approval_segregation',
  'approval_level_reuse',
  'approval_not_named_approver',
  'approval_max_amount_exceeded',
  'approval_max_amount_misconfigured',
  'approval_cfo_required',
  'segregation_raiser',
  'segregation_implicated',
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
  }
  return null;
}
