import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/organization.dart';

/// The org's reporting (base) currency, for **aggregate** figures the server
/// denominates in it without naming it.
///
/// The mobile counterpart of the web `orgCurrency` store
/// (`frontend/src/lib/stores/orgSettings.svelte.ts`). Both read
/// `GET /api/organization` — gated only by `get_current_user`, with a
/// role-projected settings allow-list that admits `reporting_currency`,
/// `payments.home_currency` and `invoice_defaults.currency` to EVERY role for
/// precisely this consumer (`backend/app/services/org_settings_view.py`) — and
/// resolve them through the same three rungs the backend used when it
/// denominated the figures. See [OrgSettings.resolvedReportingCurrency].
///
/// **Not for per-row amounts.** An invoice, a contract, a payment and a
/// payment-queue row each carry their own `currency`; formatting those with
/// this code would relabel a USD invoice as GBP for a GBP-reporting tenant.
/// It is for the figures with no row to read from — today the adaptive
/// approval-patterns per-vendor averages, and the cash-flow forecast leg when
/// its cash-position sibling did not land a currency.
///
/// [currency] is `null` until resolved, and stays `null` for an org that
/// declares nothing usable. That is deliberate and is the whole reason this
/// store exists in this shape: a value substituted here would be indistinguishable
/// from one the tenant actually configured, and the figure it labelled would
/// look exactly as authoritative as a correct one (`docs/decisions.md` §119).
/// Callers pass it straight to `utils/money.dart`, which renders a bare
/// grouped figure for a `null` code.
///
/// Not offline-cached: it is one small field on a privileged configuration
/// read, re-resolved per session, and a stale currency code mislabels every
/// figure on the screen rather than merely ageing it.
class OrgCurrencyStore extends ChangeNotifier {
  static final OrgCurrencyStore instance = OrgCurrencyStore._();
  OrgCurrencyStore._();

  String? _currency;
  Future<void>? _inflight;
  bool _loaded = false;

  /// The resolved ISO 4217 code, or `null` for "not proven" — either not
  /// loaded yet or genuinely unset on the org.
  String? get currency => _currency;

  /// Has a successful load settled the question? False while in flight and
  /// after a failure, so a later screen can retry.
  bool get loaded => _loaded;

  /// Resolve once per session. Safe to call from any screen's `initState`;
  /// concurrent callers share the one in-flight request.
  ///
  /// A failure is swallowed and does NOT mark the store loaded: the figures
  /// render bare (honest) and the next screen that needs a currency tries
  /// again. Throwing here would take down a dashboard over a label.
  Future<void> ensureLoaded() {
    if (_loaded) return Future.value();
    return _inflight ??= _load();
  }

  Future<void> _load() async {
    try {
      final settings = await OrganizationApi.get();
      _currency = settings.resolvedReportingCurrency;
      _loaded = true;
      notifyListeners();
    } catch (_) {
      // Transient failure, or a signed-out race. Keep `null` — a figure with
      // no symbol is the correct rendering of "we were never told".
    } finally {
      _inflight = null;
    }
  }

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this the next account on the device would format its figures in
  /// the previous tenant's currency. Tests use it to decouple from run order.
  void reset() {
    _currency = null;
    _loaded = false;
    _inflight = null;
  }
}
