import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/admin_user.dart';
import 'package:feohledger_mobile/utils/sequenced_fetch.dart';

/// Admin user-management state — the org's users + the available roles, plus
/// role-assignment and activate/deactivate mutators. Admin-only (mirrors the
/// backend `require_roles(ROLE_ADMIN)` on `/api/admin/*`). Not offline-cached:
/// a privileged control-plane read where stale data would be misleading
/// (mirrors `CashFlowStore`).
class AdminUserStore extends ChangeNotifier with SequencedFetch {
  static final AdminUserStore instance = AdminUserStore._();
  AdminUserStore._();

  List<AdminUser> _users = [];
  List<AdminRole> _roles = [];
  bool _loading = false;
  String? _error;
  String? _searchQuery;

  List<AdminUser> get users => _users;
  List<AdminRole> get roles => _roles;
  bool get loading => _loading;
  String? get error => _error;
  String? get searchQuery => _searchQuery;

  /// The four system role names (admin/ap_manager/ap_clerk/cfo). The role
  /// editor offers only these — custom roles confer no access today, so
  /// assigning them on mobile would mislead (see backend admin.py note).
  List<String> get systemRoleNames =>
      _roles.where((r) => r.isSystem).map((r) => r.name).toList();

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _users = [];
    _roles = [];
    _loading = false;
    _error = null;
    _searchQuery = null;
    debugResetSequence();
  }

  void setSearch(String? query) {
    _searchQuery = (query == null || query.isEmpty) ? null : query;
    fetch();
  }

  Future<void> fetch() async {
    // See SequencedFetch — discards a response superseded by a newer fetch()
    // (e.g. a stale search result resolving after a later keystroke's).
    final token = nextRequestToken();
    _loading = true;
    _error = null;
    notifyListeners();

    try {
      // Roles rarely change; fetch them alongside the users so the role editor
      // always has the current list. Both are small admin reads.
      final results = await Future.wait([
        AdminApi.listUsers(search: _searchQuery),
        AdminApi.listRoles(),
      ]);
      if (!isCurrentRequest(token)) return;
      _users = results[0] as List<AdminUser>;
      _roles = results[1] as List<AdminRole>;
      _loading = false;
      notifyListeners();
    } catch (e) {
      if (!isCurrentRequest(token)) return;
      _users = [];
      _loading = false;
      _error = e.toString();
      notifyListeners();
    }
  }

  /// Replace [user]'s role set, then refetch. Returns true on success; records
  /// the error and returns false otherwise.
  Future<bool> setRoles(String userId, List<String> roleNames) =>
      _act(() => AdminApi.setRoles(userId, roleNames));

  /// Activate / deactivate [user], then refetch.
  Future<bool> setActive(String userId, bool active) =>
      _act(() => AdminApi.setActive(userId, active));

  /// Create a user, then refetch the list. Returns the [CreateUserResult]
  /// (the user, and when their emailed set-password link expires) on success, or null on
  /// failure — in which case [error] holds the reason (e.g. a 409 for an
  /// email already in use).
  Future<CreateUserResult?> createUser({
    required String email,
    required String fullName,
    required List<String> roleNames,
  }) async {
    try {
      final result = await AdminApi.createUser(
        email: email,
        fullName: fullName,
        roleNames: roleNames,
      );
      await fetch();
      return result;
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return null;
    }
  }

  /// Delete [userId], then refetch. Returns true on success; records the
  /// backend's error (self-delete / still-referenced 409) and returns false
  /// otherwise.
  Future<bool> deleteUser(String userId) async {
    try {
      await AdminApi.deleteUser(userId);
      await fetch();
      return true;
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return false;
    }
  }

  Future<bool> _act(Future<AdminUser> Function() action) async {
    try {
      await action();
      await fetch();
      return true;
    } catch (e) {
      _error = e.toString();
      notifyListeners();
      return false;
    }
  }
}
