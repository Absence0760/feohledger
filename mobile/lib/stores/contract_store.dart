import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/contract.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/utils/sequenced_fetch.dart';

class ContractStore extends ChangeNotifier with SequencedFetch {
  static final ContractStore instance = ContractStore._();
  ContractStore._();

  List<Contract> _contracts = [];
  bool _loading = false;
  String? _error;
  String? _statusFilter;
  String? _searchQuery;
  bool _fromCache = false;

  List<Contract> get contracts => _contracts;
  bool get loading => _loading;
  String? get error => _error;
  String? get statusFilter => _statusFilter;
  bool get fromCache => _fromCache;

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _contracts = [];
    _loading = false;
    _error = null;
    _statusFilter = null;
    _searchQuery = null;
    _fromCache = false;
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

  Future<void> fetch() async {
    // See SequencedFetch — discards a response superseded by a newer fetch().
    final token = nextRequestToken();
    _loading = true;
    _error = null;
    notifyListeners();

    final cacheKey =
        'contracts_${_statusFilter ?? 'all'}_${_searchQuery ?? ''}';

    try {
      final result = await ContractApi.list(
        status: _statusFilter,
        search: _searchQuery,
      );
      if (!isCurrentRequest(token)) return;
      _contracts = result;
      _fromCache = false;
      _loading = false;

      // Cache the raw contract data for offline use
      await OfflineStore.instance.put(
        cacheKey,
        _contracts.map((c) => _contractToJson(c)).toList(),
      );

      notifyListeners();
    } catch (e) {
      if (!isCurrentRequest(token)) return;
      // Try cache on failure
      try {
        final cached = await OfflineStore.instance.get(cacheKey);
        if (cached != null) {
          if (!isCurrentRequest(token)) return;
          _contracts = (cached as List)
              .map((j) => Contract.fromJson(j as Map<String, dynamic>))
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
      _error = describeApiError(e);
      notifyListeners();
    }
  }

  Future<bool> activate(String id) async {
    try {
      await ContractApi.activate(id);
      await fetch();
      return true;
    } catch (e) {
      _error = describeApiError(e);
      notifyListeners();
      return false;
    }
  }

  Future<bool> terminate(String id) async {
    try {
      await ContractApi.terminate(id);
      await fetch();
      return true;
    } catch (e) {
      _error = describeApiError(e);
      notifyListeners();
      return false;
    }
  }

  Future<bool> cancel(String id) async {
    try {
      await ContractApi.cancel(id);
      await fetch();
      return true;
    } catch (e) {
      _error = describeApiError(e);
      notifyListeners();
      return false;
    }
  }

  Map<String, dynamic> _contractToJson(Contract c) => {
        'id': c.id,
        'contract_number': c.contractNumber,
        'title': c.title,
        'description': c.description,
        'contract_type': c.contractType.value,
        'status': c.status.value,
        'vendor_id': c.vendorId,
        'vendor_name': c.vendorName,
        'currency': c.currency,
        'total_value': c.totalValue,
        'spend_limit': c.spendLimit,
        'not_to_exceed': c.notToExceed,
        'start_date': c.startDate?.toIso8601String(),
        'end_date': c.endDate?.toIso8601String(),
        'signed_date': c.signedDate?.toIso8601String(),
        'auto_renew': c.autoRenew,
        'renewal_term_months': c.renewalTermMonths,
        'renewal_notice_days': c.renewalNoticeDays,
        'payment_terms': c.paymentTerms,
        'created_at': c.createdAt.toIso8601String(),
        'updated_at': c.updatedAt?.toIso8601String(),
      };
}
