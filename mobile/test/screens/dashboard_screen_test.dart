import 'dart:async';
import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/dashboard_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/dashboard_store.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/widgets/kpi_card.dart';
import 'package:feohledger_mobile/widgets/load_error_state.dart';
import 'package:feohledger_mobile/widgets/partial_conversion_note.dart';

// Wraps a screen with the localization delegates it now needs. No explicit
// `locale` → defaults to `en`, so the English assertions below still hold.
Widget _localized(Widget home) => MaterialApp(
  localizationsDelegates: appLocalizationsDelegates,
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
  // How many rows each reporting rollup added at FACE value for want of a
  // rate lock. The endpoint always serves these beside the figures; `0` is
  // the fully-converted book.
  int unconverted = 0,
  int agingUnconverted = 0,
  int upcomingUnconverted = 0,
  // `overdue_*` / `due_soon_*` — the whole-book counts. Omitted by default,
  // modelling a backend predating them, which the legacy assertions pin.
  Map<String, dynamic>? due,
}) => {
  ...?due,
  'total_invoices': totalInvoices,
  'total_amount': totalAmount,
  if (reportingCurrency != null)
    'reporting': {
      'reporting_currency': reportingCurrency,
      'total_amount': totalAmount,
      'total_count': totalInvoices,
      'unconverted_count': unconverted,
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
      'unconverted_count': agingUnconverted,
    },
  if (reportingCurrency != null)
    'upcoming_total_amount_reporting': upcomingTotalAmount,
  if (reportingCurrency != null)
    'upcoming_unconverted_count': upcomingUnconverted,
  'pipeline': pipeline ?? {'ready_for_review': 3, 'approved': 5},
  'vendor_spend':
      vendorSpend ??
      [
        {'vendor': 'Acme Corp', 'amount': 20000, 'invoice_count': 4},
        {'vendor': 'Globex', 'amount': 15000, 'invoice_count': 3},
      ],
  'aging':
      aging ??
      {
        'current': 10000,
        'days_30': 8000,
        'days_60': 5000,
        'days_90_plus': 2000,
      },
  'monthly_trend': <Map<String, dynamic>>[],
  'upcoming_payments':
      upcomingPayments ??
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
    ApiClient().debugConfigure(client: MockClient((req) async => gate.future));

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

    // A localized sentence naming what happened and the next action — never
    // the exception's own text.
    expect(
      find.text(
        "Couldn't load the dashboard. Check your connection and tap "
        'Retry.',
      ),
      findsOneWidget,
    );
    expect(find.textContaining('offline'), findsNothing);
    expect(find.textContaining('Exception'), findsNothing);
    expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
    expect(find.byType(KpiCard), findsNothing);
  });

  testWidgets(
    'Retry re-issues the fetch and renders the dashboard on success',
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
    },
  );

  testWidgets('renders KPI cards, aging buckets and top vendors once loaded', (
    tester,
  ) async {
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

    // Top vendors section — last on the screen since "Needs your attention"
    // leads, so below the fold of the test viewport; the lazy list builds
    // its rows only once they are scrolled into view.
    await tester.scrollUntilVisible(find.text('Globex'), 200);
    expect(find.text('Top Vendors'), findsOneWidget);
    expect(find.text('Acme Corp'), findsOneWidget);
    expect(find.text('Globex'), findsOneWidget);
    expect(find.text('4 invoices'), findsOneWidget);
  });

  testWidgets('missing pipeline keys fall back to 0 for For Review/Approved', (
    tester,
  ) async {
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => _json(
          _dashboardJson(
            pipeline: {},
            vendorSpend: [
              {'vendor': 'PipelineMarker', 'amount': 1, 'invoice_count': 1},
            ],
          ),
        ),
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
    await _pumpUntil(tester, find.byType(KpiCard));
    // Key on a vendor unique to this payload so we assert against the fresh
    // fetch, not a leaky prior `_data`. Top vendors is the last section, below
    // the fold, so it has to be scrolled to before its rows are built.
    await tester.scrollUntilVisible(find.text('Vendor 0'), 200);

    expect(find.text('Vendor 0'), findsOneWidget);
    // Vendors render at the bottom of a lazy ListView, so the 5th capped row
    // ('Vendor 4') must be scrolled into view before it is built.
    await tester.scrollUntilVisible(find.text('Vendor 4'), 200);
    expect(find.text('Vendor 4'), findsOneWidget);
    // The 6th vendor (index 5) and beyond are dropped by .take(5) — they are
    // never built at all, so even after scrolling they don't exist.
    expect(find.text('Vendor 5'), findsNothing);
  });

  testWidgets('falls back to cached data when the network later fails', (
    tester,
  ) async {
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
    // No error surface — cached data satisfies the screen.
    expect(find.byType(LoadErrorState), findsNothing);
    // ...but the user is told the data is stale.
    expect(find.textContaining('Showing cached data'), findsOneWidget);
    // The cached copy keeps what the live one showed: the Upcoming count and
    // total (2, $3K) and the reporting currency. The cache used to drop the
    // upcoming list and the `reporting` block, so offline read 0 / bare 3K.
    // Asserted top-down, in screen order: the list is lazy, so a row above
    // the fold is disposed once the vendors scroll in.
    expect(find.text('2'), findsOneWidget);
    expect(find.text(r'$3K'), findsOneWidget);
    await tester.scrollUntilVisible(find.text(r'$45K'), 200);
    expect(find.text(r'$45K'), findsOneWidget);
    await tester.scrollUntilVisible(find.text('Acme Corp'), 200);
    expect(find.text('Acme Corp'), findsOneWidget);
  });

  testWidgets('does not show the cached-data banner on a live fetch', (
    tester,
  ) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _json(_dashboardJson())),
    );

    await tester.pumpWidget(_localized(const DashboardScreen()));
    await _pumpUntil(tester, find.byType(KpiCard));

    expect(find.textContaining('Showing cached data'), findsNothing);
  });

  testWidgets('money figures follow the org reporting currency, not a `\$`', (
    tester,
  ) async {
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

  testWidgets('a payload with no reporting block renders figures bare', (
    tester,
  ) async {
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

  group('hierarchy, empty state and accessibility', () {
    testWidgets('what needs the user leads; the book overview follows', (
      tester,
    ) async {
      final semantics = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async => _json(_dashboardJson())),
      );

      await tester.pumpWidget(_localized(const DashboardScreen()));
      await _pumpUntil(tester, find.byType(KpiCard));

      final review = tester.getTopLeft(find.text('For Review')).dy;
      final upcoming = tester.getTopLeft(find.text('Upcoming')).dy;
      final aging = tester.getTopLeft(find.text('Invoice Aging')).dy;
      final total = tester.getTopLeft(find.text('Total Invoices')).dy;
      expect(review, upcoming); // same row
      expect(review, lessThan(aging));
      expect(aging, lessThan(total));

      // Section titles are headings a screen reader can jump between.
      for (final title in [
        'Needs your attention',
        'Invoice Aging',
        'Overview',
      ]) {
        expect(
          tester.getSemantics(find.text(title)),
          isSemantics(label: title, isHeader: true),
        );
      }
      // A vendor row announces once, not as three fragments.
      await tester.scrollUntilVisible(find.text('Acme Corp'), 200);
      expect(
        find.bySemanticsLabel(r'Acme Corp, $20K, 4 invoices'),
        findsOneWidget,
      );
      // No empty notice on a populated book.
      expect(find.textContaining('No invoices yet'), findsNothing);
      semantics.dispose();
    });

    testWidgets('an empty book says so instead of a screen of zeros', (
      tester,
    ) async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _json(
            _dashboardJson(
              totalInvoices: 0,
              totalAmount: 0,
              pipeline: {},
              vendorSpend: [],
              upcomingPayments: [],
              upcomingTotalAmount: 0,
              aging: {
                'current': 0,
                'days_30': 0,
                'days_60': 0,
                'days_90_plus': 0,
              },
            ),
          ),
        ),
      );

      await tester.pumpWidget(_localized(const DashboardScreen()));
      await _pumpUntil(tester, find.textContaining('No invoices yet'));

      expect(
        find.text(
          'No invoices yet. Captured and uploaded invoices will '
          'appear here.',
        ),
        findsOneWidget,
      );
      await tester.scrollUntilVisible(find.text('No vendor spend yet.'), 200);
      expect(find.text('No vendor spend yet.'), findsOneWidget);
    });

    testWidgets('survives a 200% text scale on a phone without overflow', (
      tester,
    ) async {
      tester.view.physicalSize = const Size(390 * 3, 844 * 3);
      tester.view.devicePixelRatio = 3;
      addTearDown(tester.view.reset);
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _json(
            _dashboardJson(
              reportingCurrency: 'ZAR',
              unconverted: 2,
              agingUnconverted: 1,
              aging: {
                'current': 45300,
                'days_30': 1234567,
                'days_60': 98765,
                'days_90_plus': 4321,
              },
            ),
          ),
        ),
      );

      await tester.pumpWidget(
        MaterialApp(
          localizationsDelegates: appLocalizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          builder: (context, child) => MediaQuery(
            data: MediaQuery.of(context)
                .copyWith(textScaler: const TextScaler.linear(2)),
            child: child!,
          ),
          home: const DashboardScreen(),
        ),
      );
      await _pumpUntil(tester, find.byType(KpiCard));
      expect(tester.takeException(), isNull);

      // At this scale the aging bands go two across, so "Current" and
      // "60 Days" start a row each rather than sharing one with "30 Days".
      await tester.scrollUntilVisible(find.text('60 Days'), 200);
      expect(
        tester.getTopLeft(find.text('60 Days')).dy,
        greaterThan(tester.getTopLeft(find.text('30 Days')).dy),
      );
      await tester.scrollUntilVisible(find.text('Acme Corp'), 200);
      expect(tester.takeException(), isNull);
    });
  });

  group('part-converted rollups are disclosed beside their figures', () {
    // Each reporting-currency rollup keeps an invoice it could not convert in
    // at FACE value and counts it. The figure is correctly NAMED either way;
    // what the reader cannot see is that it mixes currencies — so each count
    // is said beside the figure it describes, and nothing is said at zero.
    testWidgets('the book, the upcoming total, the aging bands and the vendor '
        'ranking each say how many rows went in at face value', (tester) async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _json(
            _dashboardJson(
              reportingCurrency: 'EUR',
              unconverted: 3,
              upcomingUnconverted: 1,
              agingUnconverted: 2,
              vendorSpend: [
                {
                  'vendor': 'PartialCo',
                  'amount': 20000,
                  'invoice_count': 4,
                  'unconverted_count': 1,
                },
                {
                  'vendor': 'CleanCo',
                  'amount': 15000,
                  'invoice_count': 3,
                  'unconverted_count': 0,
                },
              ],
            ),
          ),
        ),
      );

      await tester.pumpWidget(_localized(const DashboardScreen()));
      await _pumpUntil(tester, find.byType(KpiCard));

      // In screen order (Upcoming, aging, the book, vendors), scrolling to
      // each: the list is lazy, so a note is only built once it is near the
      // viewport and disposed once it scrolls well past.
      expect(
        find.text(
          'Upcoming: partial — 1 invoice with no exchange rate into '
          'EUR, counted at face value, so the total mixes currencies by '
          'that much.',
        ),
        findsOneWidget,
      );
      final agingNote = find.text(
        'Partial: 2 invoices with no exchange rate into EUR, counted '
        'at face value — the bands below mix currencies by that much.',
      );
      await tester.scrollUntilVisible(agingNote, 200);
      expect(agingNote, findsOneWidget);
      final bookNote = find.text(
        'Total Invoices: partial — 3 invoices with no exchange rate '
        'into EUR, counted at face value, so the total mixes currencies '
        'by that much.',
      );
      await tester.scrollUntilVisible(bookNote, 200);
      expect(bookNote, findsOneWidget);

      final vendorNote = find.text(
        'Partial: 1 invoice with no exchange rate into EUR, counted at face '
        'value — so these vendors are not all ranked in the same currency. '
        'Book the missing rate before acting on the order.',
      );
      await tester.scrollUntilVisible(vendorNote, 200);
      expect(vendorNote, findsOneWidget);
      // The row says WHICH vendor; the clean one keeps its plain subtitle.
      await tester.scrollUntilVisible(find.text('CleanCo'), 200);
      expect(find.text('4 invoices · 1 at face value'), findsOneWidget);
      expect(find.text('3 invoices'), findsOneWidget);
      // No count of `PartialConversionNote`s here: the list is lazy, so the
      // KPI notes are disposed once the vendors scroll in. Each note is
      // pinned by its exact sentence above instead.
    });

    testWidgets('a fully converted book says nothing', (tester) async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _json(
            _dashboardJson(
              vendorSpend: [
                {
                  'vendor': 'ConvertedCo',
                  'amount': 20000,
                  'invoice_count': 4,
                  'unconverted_count': 0,
                },
              ],
            ),
          ),
        ),
      );

      await tester.pumpWidget(_localized(const DashboardScreen()));
      await _pumpUntil(tester, find.byType(KpiCard));

      // Checked at the top of the list and again at the bottom: the list is
      // lazy, so one look covers only the rows built near the viewport.
      expect(find.byType(PartialConversionNote), findsNothing);
      expect(find.textContaining('face value'), findsNothing);
      await tester.scrollUntilVisible(find.text('ConvertedCo'), 200);
      expect(find.byType(PartialConversionNote), findsNothing);
      expect(find.textContaining('face value'), findsNothing);
      expect(find.text('4 invoices'), findsOneWidget);
    });

    testWidgets('the vendor note counts only the vendors it renders', (
      tester,
    ) async {
      // The list shows five; a sixth vendor's unconverted invoice is not part
      // of the ranking on screen, so a note about it would describe rows the
      // reader cannot find.
      final vendors = [
        for (var i = 0; i < 5; i++)
          {
            'vendor': 'Shown $i',
            'amount': 9000 - i,
            'invoice_count': 1,
            'unconverted_count': 0,
          },
        {
          'vendor': 'Hidden',
          'amount': 1,
          'invoice_count': 1,
          'unconverted_count': 7,
        },
      ];
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _json(_dashboardJson(vendorSpend: vendors)),
        ),
      );

      await tester.pumpWidget(_localized(const DashboardScreen()));
      await _pumpUntil(tester, find.byType(KpiCard));
      await tester.scrollUntilVisible(find.text('Shown 4'), 200);

      expect(find.byType(PartialConversionNote), findsNothing);
      expect(find.textContaining('face value'), findsNothing);
    });
  });

  group('overdue and due soon', () {
    // Ten upcoming rows — the server's cap — while fifteen invoices are
    // overdue and two fall due this week.
    final tenRows = List<Map<String, dynamic>>.generate(
      10,
      (i) => {'amount': 10, 'is_overdue': true},
    );
    final due = <String, dynamic>{
      'overdue_count': 15,
      'overdue_total': [
        {'currency': 'EUR', 'amount': 1000, 'count': 1},
        {'currency': 'USD', 'amount': 2120, 'count': 14},
      ],
      'due_soon_count': 2,
      'due_soon_total': [
        {'currency': 'USD', 'amount': 2000, 'count': 2},
      ],
    };

    testWidgets('Overdue reads the whole-book count per currency, and '
        'Upcoming means due soon, not the capped list', (tester) async {
      final semantics = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async =>
              _json(_dashboardJson(upcomingPayments: tenRows, due: due)),
        ),
      );

      await tester.pumpWidget(_localized(const DashboardScreen()));
      await _pumpUntil(tester, find.byType(KpiCard));

      final overdueMoney =
          '${formatMoneyCompact(1000, currency: 'EUR')} · '
          '${formatMoneyCompact(2120, currency: 'USD')}';
      expect(find.text('Overdue'), findsOneWidget);
      expect(find.text('15'), findsOneWidget);
      expect(find.text(overdueMoney), findsOneWidget);
      expect(
        find.bySemanticsLabel('Overdue: 15, $overdueMoney'),
        findsOneWidget,
      );

      // Upcoming: the two due this week, in their own currency — not the ten
      // capped rows, and not the reporting rollup of them.
      // Each card in the row is its own announcement (KpiCard is a semantics
      // container; the two used to merge into one node).
      final soonMoney = formatMoneyCompact(2000, currency: 'USD');
      expect(find.bySemanticsLabel('Upcoming: 2, $soonMoney'), findsOneWidget);
      expect(find.bySemanticsLabel('For Review: 3'), findsOneWidget);
      expect(find.text('10'), findsNothing);
      semantics.dispose();
    });

    testWidgets('nothing overdue reads 0 with no money line', (tester) async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _json(
            _dashboardJson(
              due: {
                'overdue_count': 0,
                'overdue_total': <Object>[],
                'due_soon_count': 0,
                'due_soon_total': <Object>[],
              },
            ),
          ),
        ),
      );

      await tester.pumpWidget(_localized(const DashboardScreen()));
      await _pumpUntil(tester, find.byType(KpiCard));

      final card = find.widgetWithText(KpiCard, 'Overdue');
      expect(card, findsOneWidget);
      expect(tester.widget<KpiCard>(card).value, '0');
      expect(tester.widget<KpiCard>(card).subtitle, isNull);
    });

    testWidgets('a payload without the counts shows no Overdue figure', (
      tester,
    ) async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _json(_dashboardJson())),
      );

      await tester.pumpWidget(_localized(const DashboardScreen()));
      await _pumpUntil(tester, find.byType(KpiCard));

      // Absent, never a `0` nobody reported.
      expect(find.text('Overdue'), findsNothing);
      expect(find.byType(KpiCard), findsNWidgets(4));
    });

    testWidgets('tapping Overdue opens the overdue invoices, and says so to '
        'a screen reader', (tester) async {
      final semantics = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async => _json(_dashboardJson(due: due))),
      );
      var opened = 0;

      await tester.pumpWidget(
        _localized(DashboardScreen(onShowOverdue: () => opened++)),
      );
      await _pumpUntil(tester, find.byType(KpiCard));

      final card = find.widgetWithText(KpiCard, 'Overdue');
      expect(card, findsOneWidget);
      expect(
        tester.getSemantics(card),
        isSemantics(
          isButton: true,
          hasTapAction: true,
          hint: 'Shows the overdue invoices',
        ),
      );

      await tester.tap(card);
      await tester.pump();
      expect(opened, 1);

      // The other cards are figures, not buttons.
      expect(
        tester.getSemantics(find.widgetWithText(KpiCard, 'For Review')),
        isSemantics(isButton: false, hasTapAction: false),
      );
      semantics.dispose();
    });

    testWidgets('without a tap-through the Overdue card is not a button', (
      tester,
    ) async {
      final semantics = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async => _json(_dashboardJson(due: due))),
      );

      await tester.pumpWidget(_localized(const DashboardScreen()));
      await _pumpUntil(tester, find.byType(KpiCard));

      expect(
        tester.getSemantics(find.widgetWithText(KpiCard, 'Overdue')),
        isSemantics(isButton: false, hasTapAction: false, hint: ''),
      );
      semantics.dispose();
    });

    testWidgets('the offline cache keeps the counts', (tester) async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async =>
              _json(_dashboardJson(upcomingPayments: tenRows, due: due)),
        ),
      );
      await store.fetch();

      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.fetch();

      expect(store.fromCache, isTrue);
      expect(store.data!.overdue!.count, 15);
      expect(
        store.data!.overdue!.byCurrency.map((c) => (c.currency, c.amount)),
        [('EUR', 1000.0), ('USD', 2120.0)],
      );
      expect(store.data!.dueSoon!.count, 2);
    });

    testWidgets('a currency-less group renders without a symbol', (
      tester,
    ) async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _json(
            _dashboardJson(
              due: {
                'overdue_count': 1,
                'overdue_total': [
                  {'currency': null, 'amount': 500, 'count': 1},
                ],
                'due_soon_count': 0,
                'due_soon_total': <Object>[],
              },
            ),
          ),
        ),
      );

      await tester.pumpWidget(_localized(const DashboardScreen()));
      await _pumpUntil(tester, find.byType(KpiCard));

      final card = find.widgetWithText(KpiCard, 'Overdue');
      expect(tester.widget<KpiCard>(card).subtitle, formatMoneyCompact(500));
    });
  });
}
