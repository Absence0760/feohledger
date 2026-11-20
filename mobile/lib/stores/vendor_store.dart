import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/l10n/ambient_localizations.dart';
import 'package:feohledger_mobile/models/vendor.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/utils/sequenced_fetch.dart';

/// Vendor list + verify/reject/ERP-sync actions. Mirrors [ContractStore]:
/// in-memory singleton, offline-cached list, action methods refetch on success.
class VendorStore extends ChangeNotifier with SequencedFetch {
  static final VendorStore instance = VendorStore._();
  VendorStore._();

  List<Vendor> _vendors = [];
  bool _loading = false;

  /// The last **fetch** failure, as caught — the list screen words it with
  /// `describeLoadError` (the server's reason vs the connection sentence) and
  /// it alone drives the list's error state and "couldn't refresh" banner.
  /// Actions never write it.
  Object? _loadError;

  /// The last failed **action** (verify / reject / ERP sync), as a sentence
  /// ([describeApiError]). Kept apart from [loadError] so a refused verify
  /// over a loaded list is not reported as a failed refresh.
  String? _actionError;
  String? _statusFilter;
  String? _searchQuery;
  bool _fromCache = false;

  List<Vendor> get vendors => _vendors;
  bool get loading => _loading;
  Object? get loadError => _loadError;
  String? get actionError => _actionError;

  /// Whether a status chip or search query is narrowing the list — the
  /// screen shows the filtered-empty state, not "No vendors found", when it is.
  bool get isFiltered => _statusFilter != null || _searchQuery != null;
  String? get statusFilter => _statusFilter;
  String? get searchQuery => _searchQuery;
  bool get fromCache => _fromCache;

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _vendors = [];
    _loading = false;
    _loadError = null;
    _actionError = null;
    _statusFilter = null;
    _searchQuery = null;
    _fromCache = false;
    _lastRejected = null;
    debugResetSequence();
  }

  void setStatusFilter(String? status) {
    _statusFilter = status;
    fetch();
  }

  void setSearch(String? query) {
    _searchQuery = query;
    fetch();
  }

  /// Drop the status chip and the search query together and refetch — the
  /// filtered-empty state's "Show all".
  void clearFilters() {
    _statusFilter = null;
    _searchQuery = null;
    fetch();
  }

  Future<void> fetch() async {
    // See SequencedFetch — discards a response superseded by a newer fetch()
    // (e.g. a stale search result resolving after a later keystroke's).
    final token = nextRequestToken();
    _loading = true;
    _loadError = null;
    notifyListeners();

    final cacheKey = 'vendors_${_statusFilter ?? 'all'}_${_searchQuery ?? ''}';

    try {
      final result = await VendorApi.list(
        status: _statusFilter,
        search: _searchQuery,
      );
      if (!isCurrentRequest(token)) return;
      _vendors = result;
      _fromCache = false;
      _loading = false;

      await OfflineStore.instance.put(
        cacheKey,
        _vendors.map(_vendorToJson).toList(),
      );

      notifyListeners();
    } catch (e) {
      if (!isCurrentRequest(token)) return;
      try {
        final cached = await OfflineStore.instance.get(cacheKey);
        if (cached != null) {
          if (!isCurrentRequest(token)) return;
          _vendors = (cached as List)
              .map((j) => Vendor.fromJson(j as Map<String, dynamic>))
              .toList();
          _fromCache = true;
          _loading = false;
          notifyListeners();
          return;
        }
      } catch (_) {}
      if (!isCurrentRequest(token)) return;
      _fromCache = false;
      _loading = false;
      _loadError = e;
      notifyListeners();
    }
  }

  Future<bool> verify(String id) => _act(() => VendorApi.verify(id));

  /// The vendor the last successful [reject] returned — carries how many of
  /// its virtual cards are still live, which the screen must surface.
  Vendor? _lastRejected;
  Vendor? get lastRejected => _lastRejected;

  Future<bool> reject(String id) {
    _lastRejected = null;
    return _act(() async => _lastRejected = await VendorApi.reject(id));
  }

  Future<bool> _act(Future<void> Function() action) async {
    _actionError = null;
    try {
      await action();
      await fetch();
      return true;
    } catch (e) {
      _actionError = describeApiError(e);
      notifyListeners();
      return false;
    }
  }

  /// Pull vendors from the connected ERP, then refetch the list. Returns the
  /// sync's counts as a localized summary on success, or null on failure
  /// (with [actionError] set). A 400 (no ERP configured) is a failure here.
  Future<String?> syncErp() async {
    _actionError = null;
    try {
      final result = await VendorApi.syncErp();
      await fetch();
      return _syncMessage(result);
    } catch (e) {
      _actionError = describeApiError(e);
      notifyListeners();
      return null;
    }
  }

  Map<String, dynamic> _vendorToJson(Vendor v) => {
    'id': v.id,
    'name': v.name,
    'code': v.code,
    'email': v.email,
    'phone': v.phone,
    'status': v.status.value,
    'source': v.source,
    'payment_terms': v.paymentTerms,
    'verified_by': v.verifiedBy,
    'erp_vendor_id': v.erpVendorId,
    'invoice_count': v.invoiceCount,
    // The caller's Verify / Reject verdicts, so a cached list offers the
    // same controls the live one did.
    'status_actions': v.statusActions,
  };
}

/// The ERP sync toast in the reader's language, built from the counts beside
/// the server's English `message` (`backend/app/api/vendors.py`). A count may
/// arrive as a number or as the list of the rows it counts.
String _syncMessage(Map<String, dynamic> result) {
  final l = ambientLocalizations();
  int? count(String key) {
    final v = result[key];
    if (v is int) return v;
    if (v is List) return v.length;
    return null;
  }

  final created = count('created');
  final updated = count('updated');
  final unchanged = count('unchanged');
  if (created == null && updated == null && unchanged == null) {
    return l.vendorSyncComplete;
  }
  final parts = [
    l.vendorSyncSummary(created ?? 0, updated ?? 0, unchanged ?? 0),
  ];
  final conflicts = count('conflicts') ?? 0;
  final unlinked = count('unlinked') ?? 0;
  final restored = count('restored') ?? 0;
  if (conflicts > 0) parts.add(l.vendorSyncNotLinked(conflicts));
  if (unlinked > 0) parts.add(l.vendorSyncUnlinked(unlinked));
  if (restored > 0) parts.add(l.vendorSyncRelinked(restored));
  return parts.join(' ');
}
