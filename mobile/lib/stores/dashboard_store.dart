import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/services/offline_store.dart';

class DashboardStore extends ChangeNotifier {
  static final DashboardStore instance = DashboardStore._();
  DashboardStore._();

  DashboardData? _data;
  bool _loading = false;
  String? _error;
  bool _fromCache = false;

  DashboardData? get data => _data;
  bool get loading => _loading;
  String? get error => _error;
  bool get fromCache => _fromCache;

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _data = null;
    _loading = false;
    _error = null;
    _fromCache = false;
  }

  Future<void> fetch() async {
    _loading = true;
    _error = null;
    notifyListeners();

    try {
      final result = await OfflineStore.instance.cachedFetch<DashboardData>(
        key: 'dashboard',
        fetch: DashboardApi.get,
        // Cached in the WIRE shape `DashboardData.fromJson` reads, so the
        // offline screen shows what the live one did. It used to drop the
        // `reporting` block (figures lost their currency), every unconverted
        // count (the face-value disclosures vanished offline), and the
        // upcoming list (the Upcoming KPI read 0 / 0).
        toCache: (d) => {
          'total_invoices': d.totalInvoices,
          'total_amount': d.totalAmount,
          'reporting': {
            'reporting_currency': d.reportingCurrency,
            'total_amount': d.totalAmount,
            'unconverted_count': d.unconvertedCount,
          },
          'pipeline': d.pipeline,
          'vendor_spend': d.topVendors
              .map((v) => {
                    'vendor': v.vendorName,
                    'amount': v.totalAmount,
                    'invoice_count': v.invoiceCount,
                    'unconverted_count': v.unconvertedCount,
                  })
              .toList(),
          'aging_reporting': {
            'current': d.aging.current,
            'days_30': d.aging.thirtyDays,
            'days_60': d.aging.sixtyDays,
            'days_90_plus': d.aging.ninetyPlus,
            'unconverted_count': d.aging.unconvertedCount,
          },
          'monthly_trend': d.trends
              .map(
                  (t) => {'month': t.month, 'count': t.count, 'amount': t.amount})
              .toList(),
          // fromJson counts the list; the rows themselves are not rendered.
          'upcoming_payments': List<Map<String, dynamic>>.filled(
            d.upcoming.count,
            const <String, dynamic>{},
          ),
          'upcoming_total_amount_reporting': d.upcoming.totalAmount,
          'upcoming_unconverted_count': d.upcoming.unconvertedCount,
          // Only when the live payload had them: a cache written from an
          // older backend must not invent a `0` overdue.
          if (d.overdue != null) ...{
            'overdue_count': d.overdue!.count,
            'overdue_total': d.overdue!.totalsToJson(),
          },
          if (d.dueSoon != null) ...{
            'due_soon_count': d.dueSoon!.count,
            'due_soon_total': d.dueSoon!.totalsToJson(),
          },
        },
        fromCache: (json) =>
            DashboardData.fromJson(json as Map<String, dynamic>),
      );
      _data = result.data;
      _fromCache = result.fromCache;
      _loading = false;
      notifyListeners();
    } catch (e) {
      // Try loading from cache on failure
      try {
        final cached = await OfflineStore.instance.get('dashboard');
        if (cached != null) {
          _data = DashboardData.fromJson(cached as Map<String, dynamic>);
          _fromCache = true;
          _loading = false;
          notifyListeners();
          return;
        }
      } catch (_) {}
      // No cache to fall back on — don't keep claiming the data is cached.
      _fromCache = false;
      _loading = false;
      _error = describeApiError(e);
      notifyListeners();
    }
  }
}
