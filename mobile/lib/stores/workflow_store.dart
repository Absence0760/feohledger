import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/workflow.dart';

/// Read-only workflow-definition list. Mirrors the admin stores
/// (`AdminUserStore` / `OrgSettingsStore`): an in-memory singleton that loads
/// from `GET /api/workflows`, with loading / error state. Deliberately NOT
/// offline-cached — workflow definitions are a privileged admin read, not an
/// operational list a clerk needs on a plane. There are no mutators: the
/// no-code builder stays on the web.
class WorkflowStore extends ChangeNotifier {
  static final WorkflowStore instance = WorkflowStore._();
  WorkflowStore._();

  List<WorkflowDefinition> _workflows = [];
  bool _loading = false;
  String? _error;

  List<WorkflowDefinition> get workflows => _workflows;
  bool get loading => _loading;
  String? get error => _error;

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _workflows = [];
    _loading = false;
    _error = null;
  }

  Future<void> fetch() async {
    _loading = true;
    _error = null;
    notifyListeners();
    try {
      _workflows = await WorkflowApi.list();
      _error = null;
    } catch (e) {
      _error = describeApiError(e);
    } finally {
      _loading = false;
      notifyListeners();
    }
  }
}
