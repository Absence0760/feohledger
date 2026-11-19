import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/l10n/ambient_localizations.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/models/payment_queue.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/utils/money.dart';

/// What a record-as-paid call came back with. [ok] or not; on a refusal,
/// [message] is the server's English sentence and [detail] its raw decoded
/// `detail` — a coded refusal (`{code, message, params}`) the screen states in
/// the reader's language through `localizeCodedRefusal`, with [message] as the
/// fallback. [paymentCount] is how many payments a run-level record booked.
class RecordOutcome {
  final bool ok;
  final String? message;
  final Object? detail;
  final int? paymentCount;

  const RecordOutcome._(this.ok, this.message, this.detail, this.paymentCount);

  const RecordOutcome.recorded({int? paymentCount})
      : this._(true, null, null, paymentCount);

  factory RecordOutcome.failed(Object error) => RecordOutcome._(
        false,
        describeApiError(error),
        error is ApiException ? error.detail : null,
        null,
      );
}

/// Payment queue + summary + runs. Holds the approved-invoice queue, the
/// per-row selection (checkbox + chosen [PaymentMethod]), the KPI summary, and
/// the payment-run list. Drives "create a draft run from the selection" and
/// "execute / cancel a run". Mirrors the other ChangeNotifier singletons.
///
/// Money: every amount is carried as a display string (see [moneyToDisplay]);
/// the store never sums money on the device. The "selected total" shown in the
/// UI counts rows, not currency — the authoritative total is computed by the
/// backend when the run is created.
class PaymentQueueStore extends ChangeNotifier {
  static final PaymentQueueStore instance = PaymentQueueStore._();
  PaymentQueueStore._();

  List<PaymentQueueItem> _queue = [];
  List<PaymentRun> _runs = [];
  PaymentSummary? _summary;
  bool _loading = false;
  String? _error;
  Object? _runActionErrorDetail;
  bool _fromCache = false;
  String? _executionMode;

  // Per-invoice selection state. Absent ⇒ unselected. Present ⇒ selected with
  // the chosen method (defaults to ACH the first time a row is ticked).
  final Map<String, PaymentMethod> _selection = {};

  List<PaymentQueueItem> get queue => _queue;
  List<PaymentRun> get runs => _runs;
  PaymentSummary? get summary => _summary;
  bool get loading => _loading;
  String? get error => _error;

  /// The raw decoded `detail` of the last refused [approveRun] /
  /// [executeRun] — maker-checker and the CFO-approval wait answer with a
  /// coded refusal (`payment_run_segregation` / `payment_run_awaiting_cfo`)
  /// the screen states through `localizeCodedRefusal`, with [error] as the
  /// fallback. `null` after a success or a transport failure.
  Object? get runActionErrorDetail => _runActionErrorDetail;
  bool get fromCache => _fromCache;

  /// The tenant's payment execution mode as the server last stated it
  /// (`processor` / `record_only`), or `null` while unknown — not yet read,
  /// the read failed, offline. Never cached: a stale mode is exactly the
  /// signal that must not outlive its source.
  String? get executionMode => _executionMode;

  /// Whether Execute may be offered. Fails closed on an unknown mode — see
  /// [canDispatchPayments].
  bool get canDispatch => canDispatchPayments(_executionMode);

  /// A CONFIRMED record-only tenant. An unknown mode hides the send controls
  /// (fail closed) but is not evidence of this, so the banner reads this, not
  /// `!canDispatch`.
  bool get isRecordOnly => _executionMode == kExecutionModeRecordOnly;

  bool isSelected(String invoiceId) => _selection.containsKey(invoiceId);

  /// The rail a row will ACTUALLY be paid on: the backend's pin when it sent
  /// one, else the operator's pick, else ACH.
  ///
  /// A pinned row is the only rail `POST /api/payments/runs` accepts for that
  /// invoice, so a stale operator pick must never win over it. Takes the row
  /// rather than an id so the pin travels with the data and no caller can look
  /// it up wrongly — the mobile counterpart of the web page's `methodFor()`.
  PaymentMethod methodFor(PaymentQueueItem item) =>
      item.requiredMethod ?? _selection[item.id] ?? PaymentMethod.ach;

  int get selectedCount => _selection.length;
  bool get hasSelection => _selection.isNotEmpty;

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _queue = [];
    _runs = [];
    _summary = null;
    _loading = false;
    _error = null;
    _fromCache = false;
    _executionMode = null;
    _selection.clear();
  }

  /// Tick / untick a queue row.
  ///
  /// A row a payment run would refuse ([PaymentQueueItem.isSelectable]) can
  /// never enter the selection: `create_payment_run_for_invoices` hard-409s the
  /// WHOLE batch on it, so one fully-credited or exception-held invoice used to
  /// take every other invoice in the run down with it. The guard lives HERE,
  /// with the state change, rather than only on the checkbox — the screen
  /// disables the control too, but the store is the chokepoint every caller
  /// goes through.
  void toggleSelection(PaymentQueueItem item) {
    if (!item.isSelectable) return;
    if (_selection.containsKey(item.id)) {
      _selection.remove(item.id);
    } else {
      // Seed from the row's own pin when it has one — defaulting a
      // card-claimed invoice to ACH would stage a run the backend refuses.
      _selection[item.id] = methodFor(item);
    }
    notifyListeners();
  }

  /// Choose the rail for a row (implicitly selecting it).
  ///
  /// A pinned row ignores [method] entirely: the backend accepts exactly one
  /// rail for it, so honouring the operator's pick would be honouring a choice
  /// they were never really offered.
  void setMethod(PaymentQueueItem item, PaymentMethod method) {
    if (!item.isSelectable) return;
    _selection[item.id] = item.requiredMethod ?? method;
    notifyListeners();
  }

  void clearSelection() {
    _selection.clear();
    notifyListeners();
  }

  /// Load the queue + summary together (the two halves of the payments-page
  /// header). Runs are loaded separately by [fetchRuns].
  Future<void> fetch() async {
    _loading = true;
    _error = null;
    notifyListeners();

    // Read beside the queue, and on its own failure path: the mode only
    // decides which controls are OFFERED (the server enforces it on every
    // dispatching call), so a failed read hides Execute rather than taking
    // the queue down with it.
    final mode = _loadExecutionMode();
    try {
      final results = await Future.wait([
        PaymentApi.queue(),
        PaymentApi.summary(),
      ]);
      await mode;
      _queue = results[0] as List<PaymentQueueItem>;
      _summary = results[1] as PaymentSummary;
      _fromCache = false;
      _loading = false;

      _reconcileSelection();

      await OfflineStore.instance.put(
        'payment_queue',
        _queue.map(_queueItemToJson).toList(),
      );

      notifyListeners();
    } catch (e) {
      await mode;
      try {
        final cached = await OfflineStore.instance.get('payment_queue');
        if (cached != null) {
          _queue = (cached as List)
              .map((j) => PaymentQueueItem.fromJson(j as Map<String, dynamic>))
              .toList();
          _fromCache = true;
          _loading = false;
          // The cached rows carry their own `blocked` / `required_method`, so
          // the offline queue is reconciled exactly like a live one — a row
          // that was blocked when it was cached stays unselectable offline.
          _reconcileSelection();
          notifyListeners();
          return;
        }
      } catch (_) {}
      _fromCache = false;
      _loading = false;
      _error = describeApiError(e);
      notifyListeners();
    }
  }

  /// `GET /api/payments/execution-mode`. Any failure — offline, a 403, a
  /// payload without a string `mode` — leaves the mode unknown, which
  /// [canDispatch] reads as record-only. Never throws.
  Future<void> _loadExecutionMode() async {
    String? mode;
    try {
      final data = await PaymentApi.executionMode();
      final raw = data['mode'];
      mode = raw is String ? raw : null;
    } catch (_) {
      mode = null;
    }
    _executionMode = mode;
  }

  /// Re-derive the selection against the queue that was just loaded.
  ///
  /// Three things can change under a selected row between fetches, and all
  /// three are resolved here rather than at send time, so the checkbox and the
  /// method dropdown show the truth as soon as the list refreshes:
  ///
  /// * it left the queue (paid / voided) — dropped;
  /// * it became unselectable (an exception was raised on it, a credit memo
  ///   covered it) — dropped, because a run containing it would 409 as a whole;
  /// * it gained a pinned rail (a virtual card was issued against it) — its
  ///   stored method is overwritten with the pin.
  void _reconcileSelection() {
    final byId = {for (final item in _queue) item.id: item};
    _selection.removeWhere((id, _) {
      final row = byId[id];
      return row == null || !row.isSelectable;
    });
    for (final entry in _selection.entries.toList()) {
      final pinned = byId[entry.key]!.requiredMethod;
      if (pinned != null) _selection[entry.key] = pinned;
    }
  }

  Future<void> fetchRuns() async {
    try {
      _runs = await PaymentApi.runs();
      notifyListeners();
    } catch (e) {
      _error = describeApiError(e);
      notifyListeners();
    }
  }

  /// Create a draft run from the current selection. On success, clears the
  /// selection and refreshes the queue + runs, and returns the run's
  /// localized summary (which flags CFO approval when required). Returns
  /// null on failure with [error] set.
  Future<String?> createRunFromSelection() async {
    if (_selection.isEmpty) return null;
    // Belt-and-braces on the rail: `_reconcileSelection` already pinned every
    // selected row that has a `required_method`, but resolving again against
    // the live queue means a future selection path that skips [setMethod]
    // still can't send a rail the backend refuses. A row no longer in the
    // queue keeps its stored method — the server is the authority on it.
    final byId = {for (final item in _queue) item.id: item};
    final selections = _selection.entries.map((e) {
      final row = byId[e.key];
      return PaymentRunSelection(
        invoiceId: e.key,
        method: row == null ? e.value : methodFor(row),
      );
    }).toList();
    try {
      final result = await PaymentApi.createRun(selections);
      _selection.clear();
      await fetch();
      await fetchRuns();
      return _runCreatedMessage(result);
    } catch (e) {
      _error = describeApiError(e);
      notifyListeners();
      return null;
    }
  }

  /// CFO sign-off on a draft run that trips the org's approval threshold.
  /// Returns a localized confirmation, or null on failure with [error] set.
  ///
  /// This authorizes execution; it moves no money. Refusals are routine — 409
  /// (not a draft / doesn't need sign-off / already signed off) and 403 (the
  /// maker-checker check: the run's creator can't approve their own run) — so
  /// [error] carries the server's own `detail` sentence via [describeApiError].
  Future<String?> approveRun(String runId) async {
    _runActionErrorDetail = null;
    try {
      final result = await PaymentApi.approveRun(runId);
      await fetchRuns();
      return ambientLocalizations().payRunApproved;
    } catch (e) {
      _error = describeApiError(e);
      _runActionErrorDetail = e is ApiException ? e.detail : null;
      notifyListeners();
      return null;
    }
  }

  /// Execute a draft run. Returns a localized summary, or null on failure.
  ///
  /// A refusal here is routine, not exceptional — the CFO-approval gate and the
  /// segregation-of-duties check both answer 403 — so [error] carries the
  /// server's own `detail` sentence (see [describeApiError]), never the raw
  /// JSON body it arrived in.
  Future<String?> executeRun(String runId) async {
    _runActionErrorDetail = null;
    try {
      final result = await PaymentApi.executeRun(runId);
      await fetch();
      await fetchRuns();
      return _runExecutedMessage(result);
    } catch (e) {
      _error = describeApiError(e);
      _runActionErrorDetail = e is ApiException ? e.detail : null;
      notifyListeners();
      return null;
    }
  }

  /// Cancel a draft run (releases its invoices back to the queue).
  Future<String?> cancelRun(String runId) async {
    try {
      final result = await PaymentApi.cancelRun(runId);
      await fetch();
      await fetchRuns();
      return _runCancelledMessage(result);
    } catch (e) {
      _error = describeApiError(e);
      notifyListeners();
      return null;
    }
  }

  /// Record [item] as paid outside FeohLedger — `POST
  /// /api/payments/record-outside`. On success the row leaves the queue, so
  /// the queue + summary are re-read (and the selection reconciled, dropping
  /// it if it was ticked). On a refusal nothing is re-read: the sheet states
  /// it beside the form.
  ///
  /// [method] is a raw code from [kExternalPaymentMethods]; [paidOn] is
  /// `YYYY-MM-DD`.
  Future<RecordOutcome> recordOutside(
    PaymentQueueItem item, {
    required String method,
    required String reference,
    required String paidOn,
  }) async {
    try {
      await PaymentApi.recordOutside(
        invoiceId: item.id,
        method: method,
        reference: reference.trim(),
        paidOn: paidOn,
      );
    } catch (e) {
      return RecordOutcome.failed(e);
    }
    _selection.remove(item.id);
    await fetch();
    return const RecordOutcome.recorded();
  }

  /// Record the ERP's split payment for [item] through its exception's
  /// one-click — `POST /api/exceptions/{id}/record-erp-payment` — the path the
  /// web queue dialog takes when [QueueErpReportedPayment.splitIsTheRecord]:
  /// `record-outside` refuses a split that stands, and this books it and
  /// resolves the exception in one transaction. No rail is sent: the split is
  /// the record. Same outcome handling as [recordOutside].
  Future<RecordOutcome> recordErpSplit(
    PaymentQueueItem item,
    QueueErpReportedPayment reported, {
    required String reference,
    required String paidOn,
  }) async {
    try {
      await ExceptionApi.recordErpPayment(
        reported.exceptionId,
        reference: reference.trim(),
        paidOn: paidOn,
      );
    } catch (e) {
      return RecordOutcome.failed(e);
    }
    _selection.remove(item.id);
    await fetch();
    return const RecordOutcome.recorded();
  }

  /// A run's detail with the caller's verdict on recording it. Throws on a
  /// failed read; the sheet then offers nothing (fail closed).
  Future<PaymentRunRecordDetail> loadRunRecordDetail(String runId) =>
      PaymentApi.runDetail(runId);

  /// Record a whole run as paid by the customer's own bank — `POST
  /// /api/payments/runs/{id}/record-outside`. On success the queue, summary
  /// and runs are re-read; [RecordOutcome.paymentCount] is what it booked.
  Future<RecordOutcome> recordRunOutside(
    String runId, {
    required String reference,
    required String paidOn,
  }) async {
    final Map<String, dynamic> result;
    try {
      result = await PaymentApi.recordRunOutside(
        runId,
        reference: reference.trim(),
        paidOn: paidOn,
      );
    } catch (e) {
      return RecordOutcome.failed(e);
    }
    await fetch();
    await fetchRuns();
    final count = result['payment_count'];
    return RecordOutcome.recorded(paymentCount: count is int ? count : null);
  }

  Map<String, dynamic> _queueItemToJson(PaymentQueueItem i) => {
        'id': i.id,
        'invoice_number': i.invoiceNumber,
        'vendor_name': i.vendorName,
        'amount': i.amountDisplay,
        'currency': i.currency,
        'due_date': i.dueDate?.toIso8601String(),
        'payment_terms': i.paymentTerms,
        'status': i.status,
        'is_overdue': i.isOverdue,
        'discount_eligible': i.discountEligible,
        'discount_date': i.discountDate?.toIso8601String(),
        'discount_amount': i.discountAmountDisplay,
        // Round-trip the refusal verdict too. Without it a cached row came
        // back with `blocked: false` and no pin, so the offline queue offered
        // a checkbox on an invoice the backend refuses — the exact hazard the
        // live queue closes.
        'blocked': i.blocked,
        'blocked_reason': i.blockedReason,
        'required_method': i.requiredMethodCode,
        // The record verdict too, so an offline row a recording would be
        // refused on still shows the action disabled with its reason.
        'record_outside_refusal': i.recordOutsideRefusal,
        'payable_amount': i.payableAmountDisplay,
        'erp_reported_payment': i.erpReportedPayment?.toJson(),
      };
}

// The run actions' success toasts, in the reader's language. The server's
// `message` is an English sentence, so these are built from the structured
// fields beside it (`backend/app/api/payments.py`); a reply missing a count
// gets the plain sentence rather than a guessed figure.

String _runCreatedMessage(Map<String, dynamic> result) {
  final l = ambientLocalizations();
  final count = result['payment_count'];
  final total = result['total_amount'];
  // One key with a `select` on the CFO gate, so a translation can place the
  // approval note where its grammar wants it rather than after a fixed join.
  final cfo = result['requires_cfo_approval'] == true ? 'required' : 'none';
  if (count is int && total is String && total.trim().isNotEmpty) {
    // The run total through the shared money helper: the run's own currency
    // symbol when the server could prove one, the bare exact figure when it
    // sent `currency: null` (a mixed or unknown run — decisions §79/§82).
    final amount = formatMoneyString(
      total,
      currency: result['currency'] as String?,
    );
    return l.payRunCreated(count, amount, cfo);
  }
  return l.payRunCreatedPlain(cfo);
}

String _runExecutedMessage(Map<String, dynamic> result) {
  final l = ambientLocalizations();
  final provider = result['provider'];
  final completed = result['payments_completed'];
  final inFlight = result['payments_in_flight'];
  final failed = result['payments_failed'];
  final executed = provider is String &&
          provider.isNotEmpty &&
          completed is int &&
          inFlight is int &&
          failed is int
      ? l.payRunExecuted(provider, completed, inFlight, failed)
      : l.payRunExecutedPlain;
  final cards = result['cards_issued'];
  return cards is int && cards > 0
      ? '$executed ${l.payRunExecutedCards(cards)}'
      : executed;
}

String _runCancelledMessage(Map<String, dynamic> result) {
  final l = ambientLocalizations();
  final released = result['released_invoices'];
  return released is int ? l.payRunCancelled(released) : l.payRunCancelledPlain;
}
