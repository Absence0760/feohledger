import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/adaptive.dart';
import 'package:feohledger_mobile/utils/sequenced_fetch.dart';

/// Adaptive-workflow read models (approval patterns, baseline anomalies,
/// advisory suggestions) plus the one safe write — dismissing a suggestion.
///
/// **Three independent sections, three independent states.** Each tab loads on
/// its own and keeps its own loading / error, so a failing anomaly scan doesn't
/// blank the patterns the reader is looking at — the same per-panel arrangement
/// the web `/adaptive` page uses instead of one page-wide spinner. Each also
/// holds its own [RequestSequence]: they are separate requests, so one must
/// neither cancel nor be cancelled by another.
///
/// Not offline-cached — a privileged analytics read over live approval history,
/// recomputed server-side on every call, exactly like [CashFlowStore].
class AdaptiveStore extends ChangeNotifier with SequencedFetch {
  static final AdaptiveStore instance = AdaptiveStore._();
  AdaptiveStore._();

  // ── Approval patterns (uses the mixin's own sequence) ──────────────
  ApprovalPatterns? _patterns;
  bool _patternsLoading = false;
  String? _patternsError;

  ApprovalPatterns? get patterns => _patterns;
  bool get patternsLoading => _patternsLoading;
  String? get patternsError => _patternsError;

  // ── Anomalies ──────────────────────────────────────────────────────
  final RequestSequence _anomalySeq = RequestSequence();
  AnomalyBatch? _anomalies;
  bool _anomaliesLoading = false;
  String? _anomaliesError;

  AnomalyBatch? get anomalies => _anomalies;
  bool get anomaliesLoading => _anomaliesLoading;
  String? get anomaliesError => _anomaliesError;

  // ── Suggestions ────────────────────────────────────────────────────
  final RequestSequence _suggestionSeq = RequestSequence();
  List<WorkflowSuggestion> _suggestions = [];
  bool _suggestionsLoading = false;
  String? _suggestionsError;
  String _suggestionStatus = 'open';

  List<WorkflowSuggestion> get suggestions => _suggestions;
  bool get suggestionsLoading => _suggestionsLoading;
  String? get suggestionsError => _suggestionsError;

  /// `open` (default) or `all` — the two the screen offers, mirroring the web
  /// page's Show filter.
  String get suggestionStatus => _suggestionStatus;

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _patterns = null;
    _patternsLoading = false;
    _patternsError = null;
    _anomalies = null;
    _anomaliesLoading = false;
    _anomaliesError = null;
    _suggestions = [];
    _suggestionsLoading = false;
    _suggestionsError = null;
    _suggestionStatus = 'open';
    debugResetSequence();
    _anomalySeq.reset();
    _suggestionSeq.reset();
  }

  Future<void> fetchPatterns() async {
    final token = nextRequestToken();
    _patternsLoading = true;
    _patternsError = null;
    notifyListeners();
    try {
      final data = await AdaptiveApi.approvalPatterns();
      if (!isCurrentRequest(token)) return;
      _patterns = data;
      _patternsError = null;
    } catch (e) {
      if (!isCurrentRequest(token)) return;
      _patternsError = describeApiError(e);
    }
    _patternsLoading = false;
    notifyListeners();
  }

  Future<void> fetchAnomalies() async {
    final token = _anomalySeq.next();
    _anomaliesLoading = true;
    _anomaliesError = null;
    notifyListeners();
    try {
      final data = await AdaptiveApi.anomalies();
      if (!_anomalySeq.isCurrent(token)) return;
      _anomalies = data;
      _anomaliesError = null;
    } catch (e) {
      if (!_anomalySeq.isCurrent(token)) return;
      _anomaliesError = describeApiError(e);
    }
    _anomaliesLoading = false;
    notifyListeners();
  }

  Future<void> setSuggestionStatus(String status) async {
    if (status == _suggestionStatus) return;
    _suggestionStatus = status;
    await fetchSuggestions();
  }

  Future<void> fetchSuggestions() async {
    final token = _suggestionSeq.next();
    _suggestionsLoading = true;
    _suggestionsError = null;
    notifyListeners();
    try {
      final rows = await AdaptiveApi.suggestions(status: _suggestionStatus);
      if (!_suggestionSeq.isCurrent(token)) return;
      _suggestions = rows;
      _suggestionsError = null;
    } catch (e) {
      if (!_suggestionSeq.isCurrent(token)) return;
      _suggestionsError = describeApiError(e);
    }
    _suggestionsLoading = false;
    notifyListeners();
  }

  /// Dismiss one advisory suggestion. Idempotent server-side.
  ///
  /// The response carries the row's new state, so it is patched in place rather
  /// than triggering a refetch: `GET /suggestions` recomputes and upserts the
  /// whole derived set on every read, and re-running that to learn one row's
  /// status is work the answer is already in hand for. Under the `open` filter
  /// the row no longer matches, so it leaves the list; under `all` it stays and
  /// flips to Dismissed.
  Future<bool> dismissSuggestion(String id) async {
    try {
      final dismissed = await AdaptiveApi.dismissSuggestion(id);
      if (_suggestionStatus == 'open') {
        _suggestions = _suggestions.where((s) => s.id != id).toList();
      } else if (dismissed != null) {
        _suggestions = _suggestions
            .map((s) => s.id == id ? dismissed : s)
            .toList();
      }
      _suggestionsError = null;
      notifyListeners();
      return true;
    } catch (e) {
      _suggestionsError = describeApiError(e);
      notifyListeners();
      return false;
    }
  }
}
