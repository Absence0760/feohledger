import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/adaptive_store.dart';
import 'package:feohledger_mobile/stores/admin_user_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/cash_flow_store.dart';
import 'package:feohledger_mobile/stores/contract_store.dart';
import 'package:feohledger_mobile/stores/dashboard_store.dart';
import 'package:feohledger_mobile/stores/exception_store.dart';
import 'package:feohledger_mobile/stores/inspection_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/stores/notification_store.dart';
import 'package:feohledger_mobile/stores/org_currency_store.dart';
import 'package:feohledger_mobile/stores/org_settings_store.dart';
import 'package:feohledger_mobile/stores/payment_queue_store.dart';
import 'package:feohledger_mobile/stores/vendor_store.dart';
import 'package:feohledger_mobile/stores/workflow_store.dart';

/// Owns the lifetime of a signed-in session's **local** state.
///
/// Everything that survives a screen — the process-lifetime `ChangeNotifier`
/// store singletons and the SQLite [OfflineStore] — holds one tenant's
/// financial data. The device outlives the session, so that state has to be
/// bound to the session that produced it and torn down when it ends:
///
/// * [beginSession] namespaces the offline cache to `(tenant, user)` and, when
///   that differs from the last session the cache saw, purges the cache and
///   resets the stores (covers a crash / kill before logout, and an install
///   upgrading from the un-namespaced cache schema).
/// * [endSession] clears the cache and resets every store.
///
/// [endSession] is wired into `ApiClient.clearSession()`, which is the single
/// place a session ends — explicit logout, a 401 on any request (expired /
/// revoked token), and the failed session restore in `AuthStore.init()` all
/// funnel through it. `LocaleStore` is deliberately not reset: the display
/// language is a device preference, not account data.
class SessionManager {
  const SessionManager._();

  /// Bind local state to the signed-in `(tenant, user)`. Call from the auth
  /// layer once the profile is loaded and BEFORE publishing the user, since a
  /// scope change resets the stores.
  static Future<void> beginSession({
    required String tenantSlug,
    required String userId,
  }) async {
    final changed = await OfflineStore.instance.setScope(
      tenantSlug: tenantSlug,
      userId: userId,
    );
    if (changed) resetStores();
  }

  /// Tear down every trace of the session held in memory or on disk.
  static Future<void> endSession() async {
    OfflineStore.instance.clearScope();
    try {
      await OfflineStore.instance.clear();
    } catch (e) {
      // A cache that refuses to clear must not wedge sign-out. The scope is
      // already dropped, so nothing can be read back until a new session
      // installs its own namespace — and a different one purges on the way in.
      debugPrint('[session] Offline cache clear failed: $e');
    }
    resetStores();
  }

  /// Drop the in-memory state of every account-scoped store singleton.
  ///
  /// Every `ChangeNotifier` store singleton under `lib/stores/` belongs here —
  /// `test/services/session_test.dart` fails if a new one is added without
  /// being listed. Its only exemptions are `LocaleStore` (a device preference,
  /// not account data) and `sequenced_fetch.dart` (the `SequencedFetch` mixin,
  /// not a store singleton).
  static void resetStores() {
    AdaptiveStore.instance.reset();
    AdminUserStore.instance.reset();
    AuthStore.instance.reset();
    CashFlowStore.instance.reset();
    ContractStore.instance.reset();
    DashboardStore.instance.reset();
    ExceptionStore.instance.reset();
    InspectionStore.instance.reset();
    InvoiceStore.instance.reset();
    NotificationStore.instance.reset();
    OrgCurrencyStore.instance.reset();
    OrgSettingsStore.instance.reset();
    PaymentQueueStore.instance.reset();
    VendorStore.instance.reset();
    WorkflowStore.instance.reset();
  }
}
