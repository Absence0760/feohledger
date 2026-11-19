import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/goods_receipt.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/utils/sequenced_fetch.dart';

/// Quality-inspection list + the record-an-inspection write.
///
/// Shaped like [VendorStore] (list, filter, action-then-refetch) with two
/// deliberate differences:
///
/// * **The outcome filter is sent to the server** (`?result=`), never applied to
///   the loaded page. The list is paginated, so a client-side filter would hide
///   every matching row past the page boundary — which is how the approvals tab
///   once reported "all caught up" while invoices sat awaiting review.
/// * **Not offline-cached.** Every other list cached here is something to *read*
///   on a plane; this one exists to be written to, and the write needs the
///   network. Serving a stale pass/fail from disk would be worse than an honest
///   error: a `fail` is what puts a quality hold on a payable invoice, so a row
///   that looks current but predates a re-inspection is a wrong answer about
///   whether an invoice can be paid.
class InspectionStore extends ChangeNotifier with SequencedFetch {
  static final InspectionStore instance = InspectionStore._();
  InspectionStore._();

  List<Inspection> _inspections = [];
  bool _loading = false;
  String? _error;
  InspectionResult? _resultFilter;

  List<Inspection> get inspections => _inspections;
  bool get loading => _loading;
  String? get error => _error;

  /// The selected outcome chip, or null for "all".
  InspectionResult? get resultFilter => _resultFilter;

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _inspections = [];
    _loading = false;
    _error = null;
    _resultFilter = null;
    debugResetSequence();
  }

  void setResultFilter(InspectionResult? result) {
    _resultFilter = result;
    fetch();
  }

  Future<void> fetch() async {
    // See SequencedFetch — discards a response superseded by a newer fetch()
    // (e.g. two quick chip taps racing each other).
    final token = nextRequestToken();
    _loading = true;
    _error = null;
    notifyListeners();

    try {
      final rows = await InspectionApi.list(result: _resultFilter?.value);
      if (!isCurrentRequest(token)) return;
      _inspections = rows;
      _error = null;
      _loading = false;
      notifyListeners();
    } catch (e) {
      if (!isCurrentRequest(token)) return;
      _loading = false;
      _error = describeApiError(e);
      notifyListeners();
    }
  }

  /// Fetch one inspection for the detail screen.
  ///
  /// Throws the caught error as-is (a 404 / 403 [ApiException], or a network
  /// failure) for the detail screen to word with `describeLoadError` — the
  /// server's reason or the connection sentence. It writes no store state: a
  /// failed detail read is not a failed list load, and recording it in
  /// [error] would put the list's error state over rows that loaded fine.
  Future<Inspection> getById(String id) => InspectionApi.getById(id);

  /// Record an inspection (`POST /api/inspections`, admin / ap_manager).
  ///
  /// Returns the created row on success and refetches so the list reflects the
  /// active filter — the new row may not belong to it (recording a `pass` while
  /// the Fail chip is selected), and a blind prepend would put it there anyway.
  /// Returns null with [error] set on failure, so the caller can surface the
  /// server's own refusal (a 403 from `require_roles`, a quantity outside
  /// `Numeric(12, 4)`).
  Future<Inspection?> record(InspectionDraft draft) async {
    try {
      final created = await InspectionApi.create(draft);
      await fetch();
      return created;
    } catch (e) {
      _error = describeApiError(e);
      notifyListeners();
      return null;
    }
  }

  /// The goods receipts the record form can link an inspection to, plus how many
  /// the tenant has in total.
  ///
  /// One page, newest first — `GET /api/goods-receipts` offers no search, so
  /// this is the honest bound, and the `total` is what lets the form say there
  /// are more receipts than it is offering instead of implying the picker holds
  /// every one. Returns null with [error] set on failure so the caller can say
  /// why the form could not open.
  Future<({List<GoodsReceipt> items, int total})?> loadReceiptOptions() async {
    try {
      return await GoodsReceiptApi.list(pageSize: 50);
    } catch (e) {
      _error = describeApiError(e);
      notifyListeners();
      return null;
    }
  }
}
