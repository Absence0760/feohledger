import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/exception.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/utils/sequenced_fetch.dart';

class ExceptionStore extends ChangeNotifier with SequencedFetch {
  static final ExceptionStore instance = ExceptionStore._();
  ExceptionStore._();

  List<ApException> _exceptions = [];
  bool _loading = false;
  String? _error;
  String? _statusFilter;
  bool _fromCache = false;

  // Multi-select mode for bulk resolve/escalate/dismiss. Mirrors InvoiceStore:
  // the set holds the selected exception ids; the screen drives the toggles.
  bool _selectionMode = false;
  final Set<String> _selectedIds = <String>{};

  List<ApException> get exceptions => _exceptions;
  bool get loading => _loading;
  String? get error => _error;
  String? get statusFilter => _statusFilter;
  bool get fromCache => _fromCache;

  bool get selectionMode => _selectionMode;
  Set<String> get selectedIds => Set.unmodifiable(_selectedIds);
  int get selectedCount => _selectedIds.length;
  bool isSelected(String id) => _selectedIds.contains(id);

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _exceptions = [];
    _loading = false;
    _error = null;
    _statusFilter = null;
    _fromCache = false;
    _selectionMode = false;
    _selectedIds.clear();
    debugResetSequence();
  }

  // ----- Selection mutators (mirror InvoiceStore) -----

  /// Enter multi-select mode (no-op if already on); optionally seed the first
  /// selected id (e.g. from a long-press on a row).
  void enterSelectionMode([String? firstId]) {
    _selectionMode = true;
    if (firstId != null) _selectedIds.add(firstId);
    notifyListeners();
  }

  /// Leave multi-select mode and clear the selection.
  void exitSelectionMode() {
    _selectionMode = false;
    _selectedIds.clear();
    notifyListeners();
  }

  void toggleSelected(String id) {
    if (!_selectedIds.remove(id)) _selectedIds.add(id);
    notifyListeners();
  }

  void setStatusFilter(String? status) {
    _statusFilter = status;
    fetch();
  }

  Future<void> fetch() async {
    // See SequencedFetch — discards a response superseded by a newer fetch()
    // (e.g. rapid status-filter chip taps racing each other).
    final token = nextRequestToken();
    _loading = true;
    _error = null;
    notifyListeners();

    final cacheKey = 'exceptions_${_statusFilter ?? 'all'}';

    try {
      final result = await ExceptionApi.list(status: _statusFilter);
      if (!isCurrentRequest(token)) return;
      _exceptions = result;
      _fromCache = false;
      _loading = false;

      // Cache the raw exception data for offline use.
      await OfflineStore.instance.put(
        cacheKey,
        _exceptions.map((e) => _exceptionToJson(e)).toList(),
      );

      notifyListeners();
    } catch (e) {
      if (!isCurrentRequest(token)) return;
      // Try cache on failure.
      try {
        final cached = await OfflineStore.instance.get(cacheKey);
        if (cached != null) {
          if (!isCurrentRequest(token)) return;
          _exceptions = (cached as List)
              .map((j) => ApException.fromJson(j as Map<String, dynamic>))
              .toList();
          _fromCache = true;
          _loading = false;
          notifyListeners();
          return;
        }
      } catch (_) {}
      if (!isCurrentRequest(token)) return;
      // No cache to fall back on — make sure we don't keep claiming the
      // (now absent) data came from cache.
      _fromCache = false;
      _loading = false;
      _error = e.toString();
      notifyListeners();
    }
  }

  Future<bool> resolve(String id, {String resolution = 'Resolved on mobile'}) =>
      _act(id, action: 'resolve', resolution: resolution);

  Future<bool> escalate(
    String id, {
    String resolution = 'Escalated on mobile',
  }) =>
      _act(id, action: 'escalate', resolution: resolution);

  Future<bool> dismiss(String id, {String resolution = 'Dismissed on mobile'}) =>
      _act(id, action: 'dismiss', resolution: resolution);

  Future<bool> _act(
    String id, {
    required String action,
    required String resolution,
  }) async {
    try {
      await ExceptionApi.act(id, action: action, resolution: resolution);
      await fetch();
      return true;
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return false;
    }
  }

  /// Load one exception's full detail (used by the detail screen). Returns null
  /// + records the error on failure (e.g. 404 / network).
  Future<ApException?> getById(String id) async {
    try {
      return await ExceptionApi.getById(id);
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return null;
    }
  }

  /// Assign (or unassign with [userId] == null) an exception. Returns the
  /// updated exception on success (so the caller can reflect the new assignee),
  /// null + records the error on failure. Patches the in-memory row in place so
  /// the list reflects the change without a full refetch.
  Future<ApException?> assign(String id, {String? userId}) async {
    try {
      final updated = await ExceptionApi.assign(id, userId: userId);
      final idx = _exceptions.indexWhere((e) => e.id == id);
      if (idx != -1) {
        _exceptions[idx] = updated;
        notifyListeners();
      }
      return updated;
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return null;
    }
  }

  /// Bulk resolve/escalate/dismiss the currently-selected exceptions. On a
  /// successful call exits selection mode and refetches; returns the partial
  /// `{updated, skipped}` result so the screen can announce counts. Returns null
  /// + records the error on failure. No-op (null) when nothing is selected.
  Future<BulkResolveResult?> bulkResolveSelected({
    String action = 'resolve',
    String resolution = 'Bulk-resolved on mobile',
  }) async {
    if (_selectedIds.isEmpty) return null;
    final ids = _selectedIds.toList();
    try {
      final result = await ExceptionApi.bulkResolve(
        ids,
        action: action,
        resolution: resolution,
      );
      exitSelectionMode();
      await fetch();
      return result;
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return null;
    }
  }

  Map<String, dynamic> _exceptionToJson(ApException e) => {
        'id': e.id,
        'invoice_id': e.invoiceId,
        'invoice_number': e.invoiceNumber,
        'vendor_name': e.vendorName,
        'amount': e.amount,
        // Cached with the figure: without it an offline-rendered amount loses
        // its symbol (the list falls back to a bare figure).
        'currency': e.currency,
        'exception_type': e.exceptionType,
        'type_label': e.typeLabel,
        'severity': e.severity.value,
        'description': e.description,
        'status': e.status.value,
        'resolution': e.resolution,
        'assigned_to': e.assignedTo,
        'assigned_to_user_id': e.assignedToUserId,
        'is_overdue': e.isOverdue,
        'created_at': e.createdAt.toIso8601String(),
        'resolved_by': e.resolvedBy,
        'resolved_at': e.resolvedAt?.toIso8601String(),
        'due_at': e.dueAt?.toIso8601String(),
        'time_to_resolution_hours': e.timeToResolutionHours,
      };
}
