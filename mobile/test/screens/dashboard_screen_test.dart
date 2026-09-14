import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/dashboard_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/dashboard_store.dart';
import 'package:feohledger_mobile/widgets/kpi_card.dart';

// Wraps a screen with the localization delegates it now needs. No explicit
// `locale` → defaults to `en`, so the English assertions below still hold.
Widget _localized(Widget home) => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: home,
    );

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

/// A full dashboard payload, in the wire shape the API returns from
/// GET /api/dashboard (consumed by DashboardData.fromJson).
Map<String, dynamic> _dashboardJson({
  int totalInvoices = 12,
  num totalAmount = 45000,
  Map<String, int>? pipeline,
  List<Map<String, dynamic>>? vendorSpend,
  Map<String, dynamic>? aging,
  List<Map<String, dynamic>>? upcomingPayments,
  num upcomingTotalAmount = 3000,
  // The endpoint serves a face-value figure AND a reporting-currency
  // counterpart for each money key, plus the code the latter set is in. The
  // screen reads the counterparts, because only those are in one currency —
  // `total_amount` / `aging` / `upcoming_total_amount` are naive sums across
  // currencies and no single code describes them. `null` models a backend
  // predating the `reporting` block, where the figures render bare.
  Object? reportingCurrency = 'USD',
}) =>
    {
      'total_invoices': totalInvoices,
      'total_amount': totalAmount,
      if (reportingCurrency != null)
        'reporting': {
          'reporting_currency': reportingCurrency,
          'total_amount': totalAmount,
          'total_count': totalInvoices,
          'unconverted_count': 0,
          'by_currency': <Map<String, dynamic>>[],
        },
      if (reportingCurrency != null)
        'aging_reporting': {
          ...(aging ??
              {
                'current': 10000,
                'days_30': 8000,
                'days_60': 5000,
                'days_90_plus': 2000,
              }),
          'unconverted_count': 0,
        },
      if (reportingCurrency != null)
        'upcoming_total_amount_reporting': upcomingTotalAmount,
      'pipeline': pipeline ?? {'ready_for_review': 3, 'approved': 5},
      'vendor_spend': vendorSpend ??
          [
            {'vendor': 'Acme Corp', 'amount': 20000, 'invoice_count': 4},
            {'vendor': 'Globex', 'amount': 15000, 'invoice_count': 3},
          ],
      'aging': aging ??
          {
            'current': 10000,
            'days_30': 8000,
            'days_60': 5000,
            'days_90_plus': 2000,
          },
      'monthly_trend': <Map<String, dynamic>>[],
      'upcoming_payments': upcomingPayments ??
          [
            {'amount': 1000},
            {'amount': 2000},
          ],
      // Server-computed aggregate (see models/payment.dart) — the screen
      // never folds upcoming_payments' amounts itself.
      'upcoming_total_amount': upcomingTotalAmount,
    };

/// Pumps bounded fixed frames until [finder] appears or we run out of
/// budget — never pumpAndSettle on a screen that may hold a spinner/ticker.
Future<void> _pumpUntil(
  WidgetTester tester,
  Finder finder, {
  int maxFrames = 20,
}) async {
  for (var i = 0; i < maxFrames && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  final store = DashboardStore.instance;

  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    DashboardStore.instance.reset();
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  // NOTE on ordering: DashboardStore is a leaky singleton with no reset seam,
  // and its loading/error UI branches are guarded by `store.data == null`.
  // Once any test populates `_data`, that field is never nulled again within
  // the isolate. So the three `data == null`-dependent tests (spinner, error,
  // retry) run FIRST, and the spinner test resolves to an error so it leaves
  // `_data` null for the error test that follows. The retry test is last of
  // this group because it transitions the store null -> populated on success.
  // See `notTestable` for the durable fix (a @visibleForTesting reset).

  testWidgets('shows a spinner while the dashboard is loading', (tester) async {
    // Hold the response open so the screen stays in its loading state.
    final gate = Completer<http.Response>();
    ApiClient().debugConfigure(
      client: MockClient((req) async => gate.future),
    );

    await tester.pumpWidget(_localized(const DashboardScreen()));
    // One frame to run the post-frame fetch + rebuild into loading.
    await tester.pump();
    await tester.pump();

    expect(find.byType(CircularProgressIndicator), findsOneWidget);

    // Resolve to an error so the in-flight request settles (no leaked Timer)
    // while leaving `_data` null for the error-state test that follows.
    gate.completeError(Exception('offline'));
    await _pumpUntil(tester, find.text('Retry'));
  });

  testWidgets('shows an error state with a Retry button when fetch fails and '
      'no cache exists', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => throw Exception('offline')),
    );

    await tester.pumpWidget(_localized(const DashboardScreen()));
    await _pumpUntil(tester, find.text('Retry'));

    expect(find.textContaining('Error:'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
    expect(find.byType(KpiCard), findsNothing);
  });

  testWidgets('Retry re-issues the fetch and renders the dashboard on success',
      (tester) async {
    var failNext = true;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (failNext) {
          throw Exception('offline');
        }
        return _json(_dashboardJson());
      }),
    );

    await tester.pumpWidget(_localized(const DashboardScreen()));
    await _pumpUntil(tester, find.text('Retry'));
    expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);

    // Next fetch will succeed.
    failNext = false;
    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await _pumpUntil(tester, find.byType(KpiCard));

    expect(find.byType(KpiCard), findsNWidgets(4));
    expect(find.text('Retry'), findsNothing);
  });

  testWidgets('renders KPI cards, aging buckets and top vendors once loaded',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.url.path == '/api/dashboard') {
          return _json(_dashboardJson());
        }
        return http.Response('not found', 404);
      }),
    );

    await tester.pumpWidget(_localized(const DashboardScreen()));
    await _pumpUntil(tester, find.byType(KpiCard));

    // Four KPI cards: Total Invoices, Upcoming, For Review, Approved.
    expect(find.byType(KpiCard), findsNWidgets(4));
    expect(find.text('Total Invoices'), findsOneWidget);
    expect(find.text('Upcoming'), findsOneWidget);
    expect(find.text('For Review'), findsOneWidget);
    expect(find.text('Approved'), findsOneWidget);

    // KPI values come straight from the payload.
    expect(find.text('12'), findsOneWidget); // total invoices
    expect(find.text('3'), findsOneWidget); // ready_for_review
    expect(find.text('5'), findsOneWidget); // approved

    // And the money figures wear the code the payload named — a US tenant's
    // `$`, from `reporting.reporting_currency`, not from a hardcoded symbol.
    expect(find.text(r'$45K'), findsOneWidget); // whole-book rollup
    expect(find.text(r'$3K'), findsOneWidget); // upcoming total
    expect(find.text(r'$10K'), findsOneWidget); // current aging band
    expect(find.text('2'), findsOneWidget); // upcoming count (2 entries)

    // Section headers + aging bucket labels.
    expect(find.text('Invoice Aging'), findsOneWidget);
    expect(find.text('Current'), findsOneWidget);
    expect(find.text('30 Days'), findsOneWidget);
    expect(find.text('60 Days'), findsOneWidget);
    expect(find.text('90+'), findsOneWidget);

    // Top vendors section.
    expect(find.text('Top Vendors'), findsOneWidget);
    expect(find.text('Acme Corp'), findsOneWidget);
    expect(find.text('Globex'), findsOneWidget);
    expect(find.text('4 invoices'), findsOneWidget);
  });

  testWidgets('missing pipeline keys fall back to 0 for For Review/Approved',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => _json(_dashboardJson(
          pipeline: {},
          vendorSpend: [
            {'vendor': 'PipelineMarker', 'amount': 1, 'invoice_count': 1},
          ],
        )),
      ),
    );

    await tester.pumpWidget(_localized(const DashboardScreen()));
    // Wait for THIS fetch to land (a leaky prior `_data` could otherwise show
    // stale cards first) by keying on a vendor unique to this payload.
    await _pumpUntil(tester, find.text('PipelineMarker'));

    expect(find.text('For Review'), findsOneWidget);
    expect(find.text('Approved'), findsOneWidget);
    // Both pipeline KPIs render '0' when their key is absent.
    expect(find.text('0'), findsNWidgets(2));
  });

  testWidgets('caps top vendors at five rows', (tester) async {
    final vendors = List.generate(
      8,
      (i) => {
        'vendor': 'Vendor $i',
        'amount': 1000 * (i + 1),
        'invoice_count': i + 1,
      },
    );
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => _json(_dashboardJson(vendorSpend: vendors)),
      ),
    );

    await tester.pumpWidget(_localized(const DashboardScreen()));
    // Key on a vendor unique to this payload so we assert against the fresh
    // fetch, not a leaky prior `_data`.
    await _pumpUntil(tester, find.text('Vendor 0'));

    expect(find.text('Vendor 0'), findsOneWidget);
    // Vendors render at the bottom of a lazy ListView, so the 5th capped row
    // ('Vendor 4') must be scrolled into view before it is built.
    await tester.scrollUntilVisible(find.text('Vendor 4'), 200);
    expect(find.text('Vendor 4'), findsOneWidget);
    // The 6th vendor (index 5) and beyond are dropped by .take(5) — they are
    // never built at all, so even after scrolling they don't exist.
    expect(find.text('Vendor 5'), findsNothing);
  });

  testWidgets('falls back to cached data when the network later fails',
      (tester) async {
    // Prime the offline cache with a successful fetch.
    ApiClient().debugConfigure(
      client: MockClient((req) async => _json(_dashboardJson())),
    );
    await store.fetch();
    expect(store.fromCache, isFalse);

    // Now the network fails — the screen should still render from cache.
    ApiClient().debugConfigure(
      client: MockClient((req) async => throw Exception('offline')),
    );

    await tester.pumpWidget(_localized(const DashboardScreen()));
    // Wait for the in-screen refetch to fail over to cache (which flips
    // fromCache and surfaces the banner) — not just for the KPI cards, which
    // the primed live data already rendered.
    await _pumpUntil(tester, find.textContaining('Showing cached data'));

    expect(find.byType(KpiCard), findsNWidgets(4));
    expect(find.text('Acme Corp'), findsOneWidget);
    // No error surface — cached data satisfies the screen.
    expect(find.textContaining('Error:'), findsNothing);
    // ...but the user is told the data is stale.
    expect(find.textContaining('Showing cached data'), findsOneWidget);
  });

  testWidgets('does not show the cached-data banner on a live fetch',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _json(_dashboardJson())),
    );

    await tester.pumpWidget(_localized(const DashboardScreen()));
    await _pumpUntil(tester, find.byType(KpiCard));

    expect(find.textContaining('Showing cached data'), findsNothing);
  });

  testWidgets('money figures follow the org reporting currency, not a `\$`',
      (tester) async {
    // The dashboard's figures are rollups with no row to read a currency
    // from, so the payload names the one it denominated them in. This screen
    // used to declare `NumberFormat.compactCurrency(symbol: '\$')` at module
    // level, so a ZAR-reporting tenant read its whole book in dollars.
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.url.path == '/api/dashboard') {
          return _json(_dashboardJson(reportingCurrency: 'ZAR'));
        }
        return http.Response('not found', 404);
      }),
    );

    await tester.pumpWidget(_localized(const DashboardScreen()));
    await _pumpUntil(tester, find.byType(KpiCard));

    expect(find.text('R45K'), findsOneWidget);
    expect(find.text('R3K'), findsOneWidget);
    expect(find.textContaining(r'$'), findsNothing);
  });

  testWidgets('a payload with no reporting block renders figures bare',
      (tester) async {
    // Degradation, not a fallback: without `reporting` the only figures on
    // offer are naive cross-currency sums, and there is no code that honestly
    // describes them.
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.url.path == '/api/dashboard') {
          return _json(_dashboardJson(reportingCurrency: null));
        }
        return http.Response('not found', 404);
      }),
    );

    await tester.pumpWidget(_localized(const DashboardScreen()));
    await _pumpUntil(tester, find.byType(KpiCard));

    expect(find.text('45K'), findsOneWidget);
    expect(find.textContaining(r'$'), findsNothing);
  });
}
