import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/organization.dart';

/// Organization-settings state — loads the safe editable subset (company
/// profile + invoice defaults) and saves edits over `PATCH /api/organization`.
/// The read is open to any authed user, but the mobile edit surface is gated to
/// admins (the backend PATCH is admin-only). Not offline-cached — a privileged
/// configuration read where stale data would mislead an editor.
class OrgSettingsStore extends ChangeNotifier {
  static final OrgSettingsStore instance = OrgSettingsStore._();
  OrgSettingsStore._();

  OrgSettings? _settings;
  bool _loading = false;
  bool _saving = false;
  String? _error;

  OrgSettings? get settings => _settings;
  bool get loading => _loading;
  bool get saving => _saving;
  String? get error => _error;

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _settings = null;
    _loading = false;
    _saving = false;
    _error = null;
  }

  Future<void> fetch() async {
    _loading = true;
    _error = null;
    notifyListeners();
    try {
      _settings = await OrganizationApi.get();
      _loading = false;
      notifyListeners();
    } catch (e) {
      _loading = false;
      _error = describeApiError(e);
      notifyListeners();
    }
  }

  /// Persist [update]. On success refreshes [settings] from the server response
  /// and returns true; on failure records the error and returns false.
  Future<bool> save(OrgSettingsUpdate update) async {
    _saving = true;
    _error = null;
    notifyListeners();
    try {
      _settings = await OrganizationApi.update(update);
      _saving = false;
      notifyListeners();
      return true;
    } catch (e) {
      _saving = false;
      _error = describeApiError(e);
      notifyListeners();
      return false;
    }
  }
}
