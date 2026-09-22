import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/audit_entry.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/utils/sequenced_fetch.dart';

/// Immutable bundle of the advanced-search filters (vendor, PO, amount range,
/// due-date range). Held alongside the quick status-chip filter + search box.
/// `isEmpty` lets the UI show an "advanced filters active" indicator.
class InvoiceSearchFilters {
  final String? vendor;
  final String? poNumber;
  final double? amountMin;
  final double? amountMax;
  final DateTime? dueDateFrom;
  final DateTime? dueDateTo;

  const InvoiceSearchFilters({
    this.vendor,
    this.poNumber,
    this.amountMin,
    this.amountMax,
    this.dueDateFrom,
    this.dueDateTo,
  });

  static const empty = InvoiceSearchFilters();

  bool get isEmpty =>
      (vendor == null || vendor!.isEmpty) &&
      (poNumber == null || poNumber!.isEmpty) &&
      amountMin == null &&
      amountMax == null &&
      dueDateFrom == null &&
      dueDateTo == null;

  int get activeCount => [
        vendor != null && vendor!.isNotEmpty,
        poNumber != null && poNumber!.isNotEmpty,
        amountMin != null,
        amountMax != null,
        dueDateFrom != null,
        dueDateTo != null,
      ].where((e) => e).length;
}

class InvoiceStore extends ChangeNotifier with SequencedFetch {
  static final InvoiceStore instance = InvoiceStore._();
  InvoiceStore._();

  List<Invoice> _invoices = [];
  bool _loading = false;
  String? _error;

  // ----- Approvals queue -----
  // The Approvals tab's list is fetched SEPARATELY, server-filtered to
  // `ready_for_review`, and never shares the Invoices tab's filter, list or
  // request sequence. It used to be a client-side `.where()` over whatever the
  // Invoices tab last fetched, so tapping a status chip there (e.g. `paid`)
  // emptied the approvals queue — and because both screens live in one
  // IndexedStack, switching tabs never re-fetched and pull-to-refresh re-applied
  // the same wrong filter.
  static const pendingApprovalStatus = 'ready_for_review';
  final RequestSequence _pendingSeq = RequestSequence();
  List<Invoice> _pending = [];
  bool _pendingLoading = false;
  String? _pendingError;
  bool _pendingFromCache = false;
  bool _pendingLoaded = false;
  String? _statusFilter;
  String? _searchQuery;
  InvoiceSearchFilters _filters = InvoiceSearchFilters.empty;
  bool _fromCache = false;

  // ----- Bulk-operation selection -----
  // Multi-select mode for bulk delete / status-change. The set of selected
  // invoice ids lives in the store so the list's ListenableBuilder reacts to
  // selection changes the same way it reacts to data changes.
  bool _selectionMode = false;
  final Set<String> _selectedIds = <String>{};

  List<Invoice> get invoices => _invoices;
  bool get loading => _loading;
  String? get error => _error;
  String? get statusFilter => _statusFilter;
  InvoiceSearchFilters get filters => _filters;
  bool get fromCache => _fromCache;

  bool get selectionMode => _selectionMode;
  Set<String> get selectedIds => Set.unmodifiable(_selectedIds);
  int get selectedCount => _selectedIds.length;
  bool isSelected(String id) => _selectedIds.contains(id);

  /// Invoices awaiting approval, as returned by the server for
  /// `status=ready_for_review` — NOT a slice of [invoices].
  List<Invoice> get pending => _pending;
  bool get pendingLoading => _pendingLoading;
  String? get pendingError => _pendingError;
  bool get pendingFromCache => _pendingFromCache;

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _invoices = [];
    _loading = false;
    _error = null;
    _pending = [];
    _pendingLoading = false;
    _pendingError = null;
    _pendingFromCache = false;
    _pendingLoaded = false;
    _pendingSeq.reset();
    _statusFilter = null;
    _searchQuery = null;
    _filters = InvoiceSearchFilters.empty;
    _fromCache = false;
    _selectionMode = false;
    _selectedIds.clear();
    debugResetSequence();
  }

  // ----- Selection mutators -----

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

  /// Select every invoice currently in the list.
  void selectAll() {
    _selectedIds
      ..clear()
      ..addAll(_invoices.map((i) => i.id));
    notifyListeners();
  }

  void clearSelection() {
    _selectedIds.clear();
    notifyListeners();
  }

  void setStatusFilter(String? status) {
    _statusFilter = status;
    fetch();
  }

  void setSearch(String? query) {
    _searchQuery = query;
    fetch();
  }

  /// Apply the advanced-search filters (vendor / PO / amount range / due-date
  /// range) and refetch. Pass [InvoiceSearchFilters.empty] to clear them.
  void setFilters(InvoiceSearchFilters filters) {
    _filters = filters;
    fetch();
  }

  Future<void> fetch() async {
    // Captured before the first await — if a newer fetch() starts before this
    // one's response lands, that response is stale and gets discarded instead
    // of clobbering state a later request has already written (issue #182).
    final token = nextRequestToken();
    _loading = true;
    _error = null;
    notifyListeners();

    final f = _filters;
    final cacheKey = 'invoices_${_statusFilter ?? 'all'}_${_searchQuery ?? ''}'
        '_${f.vendor ?? ''}_${f.poNumber ?? ''}_${f.amountMin ?? ''}'
        '_${f.amountMax ?? ''}_${f.dueDateFrom?.toIso8601String() ?? ''}'
        '_${f.dueDateTo?.toIso8601String() ?? ''}';

    try {
      final result = await InvoiceApi.list(
        status: _statusFilter,
        search: _searchQuery,
        vendor: f.vendor,
        poNumber: f.poNumber,
        amountMin: f.amountMin,
        amountMax: f.amountMax,
        dueDateFrom: f.dueDateFrom,
        dueDateTo: f.dueDateTo,
      );
      if (!isCurrentRequest(token)) return; // superseded — discard silently
      _invoices = result;
      _fromCache = false;
      _loading = false;

      // Cache the raw invoice data for offline use
      await OfflineStore.instance.put(
        cacheKey,
        _invoices.map((i) => _invoiceToJson(i)).toList(),
      );

      notifyListeners();
    } catch (e) {
      if (!isCurrentRequest(token)) return; // superseded — discard silently
      // Try cache on failure
      try {
        final cached = await OfflineStore.instance.get(cacheKey);
        if (cached != null) {
          if (!isCurrentRequest(token)) return;
          _invoices = (cached as List)
              .map((j) => Invoice.fromJson(j as Map<String, dynamic>))
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

  /// Load the approvals queue — `GET /api/invoices?status=ready_for_review`.
  ///
  /// A dedicated request with its own [RequestSequence] and its own cache key:
  /// it must not disturb (or be disturbed by) the Invoices tab's [fetch], and
  /// it must not read `_statusFilter`, which belongs to that tab's chips.
  Future<void> fetchPending() async {
    final token = _pendingSeq.next();
    _pendingLoading = true;
    _pendingError = null;
    notifyListeners();

    const cacheKey = 'invoices_pending_approval';
    try {
      final result = await InvoiceApi.list(status: pendingApprovalStatus);
      if (!_pendingSeq.isCurrent(token)) return; // superseded — discard
      _pending = result;
      _pendingFromCache = false;
      _pendingLoading = false;
      _pendingLoaded = true;

      await OfflineStore.instance.put(
        cacheKey,
        _pending.map(_invoiceToJson).toList(),
      );

      notifyListeners();
    } catch (e) {
      if (!_pendingSeq.isCurrent(token)) return;
      // Same offline posture as [fetch]: a cached queue beats an error screen.
      try {
        final cached = await OfflineStore.instance.get(cacheKey);
        if (cached != null) {
          if (!_pendingSeq.isCurrent(token)) return;
          _pending = (cached as List)
              .map((j) => Invoice.fromJson(j as Map<String, dynamic>))
              .toList();
          _pendingFromCache = true;
          _pendingLoading = false;
          _pendingLoaded = true;
          notifyListeners();
          return;
        }
      } catch (_) {}
      if (!_pendingSeq.isCurrent(token)) return;
      _pendingFromCache = false;
      _pendingLoading = false;
      // Surfaced as an error state, never as an empty "all caught up" queue —
      // an approvals list that silently reads empty on a failed request is
      // indistinguishable from having nothing to approve.
      _pendingError = describeApiError(e);
      notifyListeners();
    }
  }

  /// Refresh every list this store currently serves after a mutation.
  ///
  /// The Invoices tab and the Approvals tab are separate fetches, so refreshing
  /// only the former would leave the approvals queue holding a row that is no
  /// longer pending. The approvals fetch is skipped until that list has been
  /// loaded at least once, so the Invoices tab doesn't issue a second request
  /// for a screen the user has never opened.
  Future<void> _refreshAfterMutation() async {
    await fetch();
    if (_pendingLoaded) await fetchPending();
  }

  Future<bool> approve(String id) async {
    try {
      await InvoiceApi.approve(id);
      await _refreshAfterMutation();
      return true;
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return false;
    }
  }

  Future<bool> reject(String id, String reason) async {
    try {
      await InvoiceApi.reject(id, reason);
      await _refreshAfterMutation();
      return true;
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return false;
    }
  }

  /// Edit invoice fields. [changes] is the partial PATCH body (money values as
  /// string-Decimal). On success returns the updated [Invoice] and refreshes
  /// the list so the edited row reflects the change; on failure records the
  /// error and returns null. Not a money-moving write — no idempotency key
  /// needed (a repeated PATCH is naturally idempotent: it sets the same fields).
  ///
  /// A refusal here is routine, not exceptional — a GL code outside the
  /// invoice's chart (`docs/decisions.md` §194/§199), a financial field after
  /// approval, a stale edit — and retrying will not help, so [error] carries
  /// the server's own `detail` sentence (see [describeApiError]) for the
  /// screen to show, never the raw `ApiException(422): …` form.
  Future<Invoice?> update(String id, Map<String, dynamic> changes) async {
    try {
      final updated = await InvoiceApi.update(id, changes);
      await _refreshAfterMutation();
      return updated;
    } catch (e) {
      _error = describeApiError(e);
      notifyListeners();
      return null;
    }
  }

  /// Fetch the invoice's activity timeline (audit log). Read-only; the screen
  /// owns its own loading/error UI, so this just returns the entries (oldest
  /// first) or rethrows for the caller to surface.
  Future<List<AuditEntry>> fetchAuditLog(String id) {
    return InvoiceApi.auditLog(id);
  }

  /// Bulk-delete the currently-selected invoices. On success exits selection
  /// mode and refetches the list; returns the `{deleted, skipped}` result so
  /// the screen can announce partials. Returns null + records the error on
  /// failure. No-op (null) when nothing is selected.
  Future<BulkResult?> bulkDeleteSelected() async {
    if (_selectedIds.isEmpty) return null;
    final ids = _selectedIds.toList();
    try {
      final result = await InvoiceApi.bulkDelete(ids);
      exitSelectionMode();
      await _refreshAfterMutation();
      return result;
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return null;
    }
  }

  /// Bulk status-change the currently-selected invoices to [status] (the target
  /// status value, e.g. `approved`). Same success/failure contract as
  /// [bulkDeleteSelected].
  Future<BulkResult?> bulkStatusSelected(String status) async {
    if (_selectedIds.isEmpty) return null;
    final ids = _selectedIds.toList();
    try {
      final result = await InvoiceApi.bulkStatus(ids, status);
      exitSelectionMode();
      await _refreshAfterMutation();
      return result;
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return null;
    }
  }

  /// Export the currently-selected invoices in [format] (`csv` or `xml`).
  /// Returns the rendered file's bytes + filename for the caller to hand to the
  /// platform share sheet; the read is non-mutating, so it leaves selection mode
  /// intact (the user can keep acting on the same set). Returns null + records
  /// the error on failure. No-op (null) when nothing is selected.
  Future<({Uint8List bytes, String filename})?> exportSelected(
    String format,
  ) async {
    if (_selectedIds.isEmpty) return null;
    final ids = _selectedIds.toList();
    try {
      return await InvoiceApi.bulkExport(ids, format);
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return null;
    }
  }

  Map<String, dynamic> _invoiceToJson(Invoice i) => {
        'id': i.id,
        'invoice_number': i.invoiceNumber,
        'vendor_name': i.vendorName,
        'amount': i.amount,
        'currency': i.currency,
        'status': i.status.value,
        'invoice_date': i.invoiceDate?.toIso8601String(),
        'due_date': i.dueDate?.toIso8601String(),
        'description': i.description,
        'po_number': i.poNumber,
        'gl_account': i.glAccount,
        'created_at': i.createdAt.toIso8601String(),
      };
}
