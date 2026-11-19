import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/utils/sequenced_fetch.dart';

/// Payment history — `GET /api/payments` — for the Payments screen.
///
/// The screen used to hold this list in `setState`, call the API itself, keep
/// no offline copy, and let a slow pull-to-refresh overwrite a newer one. The
/// store owns all three: [fetch] is ordered by [SequencedFetch], served from
/// the offline cache when the network fails ([fromCache]), and its failure is
/// [loadError] — the caught error, which the screen words through
/// `describeLoadError`. Nothing on this screen acts on a payment yet, so
/// [actionError] is only reset here; it exists so a future action's refusal
/// has somewhere to go other than the list's load state.
///
/// Money stays a display string end to end ([Payment.amountDisplay]); the
/// store never sums it.
class PaymentStore extends ChangeNotifier with SequencedFetch {
  static final PaymentStore instance = PaymentStore._();
  PaymentStore._();

  static const _cacheKey = 'payments_all';

  List<Payment> _payments = [];
  bool _loading = false;
  Object? _loadError;
  String? _actionError;
  bool _fromCache = false;

  List<Payment> get payments => _payments;
  bool get loading => _loading;

  /// The last **fetch** failure, as caught, when no cached copy could stand
  /// in for it. Drives the list's error state and its "couldn't refresh"
  /// banner, and nothing else.
  Object? get loadError => _loadError;

  /// The last failed action on a payment, as a sentence. Kept apart from
  /// [loadError] so a refused action is never reported as a failed refresh.
  String? get actionError => _actionError;

  /// The rows on screen came from the offline cache, not the server.
  bool get fromCache => _fromCache;

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession`, so one account's payments are never in
  /// memory for the next. Tests use it to decouple from run order.
  void reset() {
    _payments = [];
    _loading = false;
    _loadError = null;
    _actionError = null;
    _fromCache = false;
    debugResetSequence();
  }

  Future<void> fetch() async {
    // See SequencedFetch — a response superseded by a newer fetch() is
    // discarded rather than written over the newer list.
    final token = nextRequestToken();
    _loading = true;
    _loadError = null;
    notifyListeners();

    try {
      final result = await OfflineStore.instance.cachedFetch<List<Payment>>(
        key: _cacheKey,
        fetch: () => PaymentApi.list(),
        fromCache: (json) => (json as List)
            .map((j) => Payment.fromJson(j as Map<String, dynamic>))
            .toList(),
        toCache: (payments) => payments.map((p) => p.toJson()).toList(),
      );
      if (!isCurrentRequest(token)) return;
      _payments = result.data;
      _fromCache = result.fromCache;
    } catch (e) {
      if (!isCurrentRequest(token)) return;
      // No cached copy to stand in. Rows already on screen stay (the screen
      // says "couldn't refresh" over them); they are no longer a cache copy.
      _fromCache = false;
      _loadError = e;
    }
    _loading = false;
    notifyListeners();
  }
}
