import 'dart:async';
import 'dart:convert';

import 'dart:typed_data';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/invoices_screen.dart';
import 'package:feohledger_mobile/services/file_share.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/widgets/bulk_action_bar.dart';
import 'package:feohledger_mobile/widgets/invoice_list_tile.dart';

import '../support/untranslated_text.dart';

/// Records the last share invocation so the export test can assert the bytes +
/// filename reached the platform share sheet without touching a plugin channel.
class _FakeFileShare extends FileShare {
  _FakeFileShare() : super.forTest();
  int calls = 0;
  Uint8List? lastBytes;
  String? lastFilename;
  String? lastMimeType;

  @override
  Future<void> shareBytes({
    required Uint8List bytes,
    required String filename,
    required String mimeType,
  }) async {
    calls++;
    lastBytes = bytes;
    lastFilename = filename;
    lastMimeType = mimeType;
  }
}

Map<String, dynamic> _me(List<String> roles) => {
      'id': 'u1',
      'email': 'demo@acme.com',
      'full_name': 'Demo User',
      'organization_id': 'org1',
      'roles': roles,
    };

/// Signs the singleton [AuthStore] in as a user with [roles], then hands later
/// requests to [rest].
Future<void> _signInAs(
  List<String> roles, {
  required Future<http.Response> Function(http.Request) rest,
}) async {
  final me = _me(roles);
  ApiClient().debugConfigure(
    client: MockClient((req) async {
      if (req.url.path == '/api/auth/login') {
        return http.Response(jsonEncode({'access_token': 'tok'}), 200,
            headers: {'content-type': 'application/json'});
      }
      if (req.url.path == '/api/auth/me') {
        return http.Response(jsonEncode(me), 200,
            headers: {'content-type': 'application/json'});
      }
      return rest(req);
    }),
  );
  await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
}

// Wraps a screen with the localization delegates it now needs. No explicit
// `locale` → defaults to `en`, so the English assertions below still hold.
Widget _localized(Widget home) => MaterialApp(
      localizationsDelegates: appLocalizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: home,
    );

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'invoices': items}),
      200,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _invoiceJson(
  String id, {
  String status = 'pending',
  String vendor = 'Acme Corp',
  String? number,
}) =>
    {
      'id': id,
      'invoice_number': number ?? 'INV-$id',
      'vendor_name': vendor,
      'amount': 100,
      'currency': 'USD',
      'status': status,
      'created_at': '2026-01-01T12:00:00',
    };

/// Pumps fixed bounded frames until [finder] is present (or the budget is
/// exhausted). Never use pumpAndSettle on this screen — a pending fetch leaves
/// a CircularProgressIndicator animating forever.
Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 20 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

/// Scrolls the horizontal status-chip row until [chip] is built and on
/// screen. The row is a lazy ListView, so a chip past the window's right
/// edge does not exist until the row scrolls to it.
Future<void> _scrollChipRowTo(WidgetTester tester, Finder chip) async {
  await tester.scrollUntilVisible(
    chip,
    150,
    scrollable: find
        .ancestor(
          of: find.byType(FilterChip).first,
          matching: find.byType(Scrollable),
        )
        .first,
  );
  await tester.pump();
}

void main() {
  final store = InvoiceStore.instance;

  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    InvoiceStore.instance.reset();
    await OfflineStore.instance.clear();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
    FileShare.debugOverride(null);
    // Stores are singletons — reset the filter so chip-selection tests start
    // from the 'All' (null) baseline regardless of test ordering.
    store.setStatusFilter(null);
  });

  tearDown(() => FileShare.debugOverride(null));

  testWidgets('shows a loading spinner while the initial fetch is in flight',
      (tester) async {
    // Hold the response open so the fetch never resolves during this test.
    final gate = Completer<http.Response>();
    ApiClient().debugConfigure(
      client: MockClient((req) async => gate.future),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    // One pump runs the post-frame fetch; the store flips loading=true.
    await tester.pump();

    expect(find.byType(CircularProgressIndicator), findsOneWidget);

    // Release the fetch and let the spinner leave the tree so no Timer/ticker
    // remains pending after disposal.
    gate.complete(_list([]));
    await _pumpUntil(tester, find.text('No invoices found'));
  });

  testWidgets('renders one InvoiceListTile per invoice once loaded',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([
            _invoiceJson('1', vendor: 'Acme Corp'),
            _invoiceJson('2', vendor: 'Globex'),
          ])),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.byType(InvoiceListTile));

    expect(find.byType(InvoiceListTile), findsNWidgets(2));
    expect(find.text('Acme Corp'), findsOneWidget);
    expect(find.text('Globex'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('renders the empty state when the list comes back empty',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([])),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.text('No invoices found'));

    expect(find.text('No invoices found'), findsOneWidget);
    expect(find.byType(InvoiceListTile), findsNothing);
  });

  // A failed first load used to render "No invoices found" — on a dropped
  // connection the user was told they had no invoices.
  testWidgets('a failed load with no cache shows the error + Retry, not empty',
      (tester) async {
    var calls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        calls++;
        if (calls == 1) throw Exception('offline');
        return _list([_invoiceJson('1')]);
      }),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.widgetWithText(FilledButton, 'Retry'));

    expect(find.text('No invoices found'), findsNothing);
    expect(store.loadError, isNotNull);
    expect(find.byType(CircularProgressIndicator), findsNothing);
    // No server answered, so the connection sentence — never the exception.
    expect(find.textContaining('Exception'), findsNothing);

    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await _pumpUntil(tester, find.byType(InvoiceListTile));
    expect(find.byType(InvoiceListTile), findsOneWidget);
    expect(store.loadError, isNull);
  });

  testWidgets('a filter that matches nothing says so, and Show all clears it',
      (tester) async {
    final statuses = <String?>[];
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        final status = req.url.queryParameters['status'];
        statuses.add(status);
        return _list(status == null ? [_invoiceJson('1')] : []);
      }),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.byType(InvoiceListTile));

    // The chips are a lazy horizontal list; at the test window's width
    // "Paid" sits past the right edge (and is not built), so scroll the row
    // to it as a user would.
    final paid = find.widgetWithText(FilterChip, 'Paid');
    await _scrollChipRowTo(tester, paid);
    await tester.tap(paid);
    await _pumpUntil(tester, find.text('No invoices match these filters'));

    expect(find.text('No invoices match these filters'), findsOneWidget);
    // "No invoices found" under a chip would claim the whole list is empty.
    expect(find.text('No invoices found'), findsNothing);

    await tester.tap(find.widgetWithText(OutlinedButton, 'Show all'));
    await _pumpUntil(tester, find.byType(InvoiceListTile));

    expect(store.statusFilter, isNull);
    expect(store.isFiltered, isFalse);
    expect(statuses.last, isNull);
    expect(find.byType(InvoiceListTile), findsOneWidget);
  });

  testWidgets('a paged list offers Load more and appends the next page',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        final number = int.parse(req.url.queryParameters['page'] ?? '1');
        final first = (number - 1) * 20;
        final count = (25 - first).clamp(0, 20).toInt();
        return http.Response(
          jsonEncode({
            'items': [
              for (var i = first; i < first + count; i++) _invoiceJson('$i'),
            ],
            'total': 25,
            'page': number,
            'page_size': 20,
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      }),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.byType(InvoiceListTile));

    final loadMore = find.byKey(const ValueKey('list-load-more'));
    await tester.scrollUntilVisible(
      loadMore,
      400,
      scrollable: find
          .descendant(
            of: find.byType(CustomScrollView),
            matching: find.byType(Scrollable),
          )
          .first,
    );
    expect(find.text('Showing 20 of 25'), findsOneWidget);

    await tester.tap(loadMore);
    for (var i = 0; i < 30 && store.invoices.length < 25; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }

    expect(store.invoices, hasLength(25));
    await tester.scrollUntilVisible(
      find.text('Showing all 25'),
      400,
      scrollable: find
          .descendant(
            of: find.byType(CustomScrollView),
            matching: find.byType(Scrollable),
          )
          .first,
    );
    expect(find.byKey(const ValueKey('list-load-more')), findsNothing);
  });

  testWidgets('renders the search bar, status filter chips and camera action',
      (tester) async {
    // A clerk: entering invoices (capture → POST /api/invoices/upload) is
    // open to ap_clerk (backend api/invoice_entry.py).
    await _signInAs(['ap_clerk'], rest: (_) async => _list([]));

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.text('No invoices found'));

    expect(find.byType(SearchBar), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'All'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Pending'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Overdue'), findsOneWidget);
    // The row is a lazy horizontal list: the chips past the window's right
    // edge are built only once it scrolls to them.
    await _scrollChipRowTo(
        tester, find.widgetWithText(FilterChip, 'Approved'));
    expect(find.widgetWithText(FilterChip, 'Approved'), findsOneWidget);
    await _scrollChipRowTo(tester, find.widgetWithText(FilterChip, 'Paid'));
    expect(find.widgetWithText(FilterChip, 'Paid'), findsOneWidget);
    expect(find.byIcon(Icons.camera_alt), findsOneWidget);
    // Advanced search action is present.
    expect(find.byIcon(Icons.tune), findsOneWidget);
  });

  testWidgets('hides the camera action from a custom-role-only user',
      (tester) async {
    await _signInAs(['Read Only'], rest: (_) async => _list([]));

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.text('No invoices found'));

    expect(find.byIcon(Icons.camera_alt), findsNothing);
  });

  testWidgets('opening advanced search and applying filters refetches',
      (tester) async {
    Map<String, String>? lastParams;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        lastParams = req.url.queryParameters;
        return _list([]);
      }),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.text('No invoices found'));

    await tester.tap(find.byIcon(Icons.tune));
    await tester.pumpAndSettle();
    expect(find.text('Advanced Search'), findsOneWidget);

    await tester.enterText(
        find.widgetWithText(TextFormField, 'Vendor'), 'Globex');
    await tester.tap(find.text('Apply'));
    await tester.pumpAndSettle();

    expect(store.filters.vendor, 'Globex');
    expect(lastParams?['vendor'], 'Globex');
  });

  testWidgets('the "All" chip is selected by default', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([])),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.text('No invoices found'));

    final allChip = tester.widget<FilterChip>(
      find.widgetWithText(FilterChip, 'All'),
    );
    expect(allChip.selected, isTrue);
    expect(store.statusFilter, isNull);
  });

  testWidgets('tapping a status chip carries that status into the request',
      (tester) async {
    String? lastStatus;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        lastStatus = req.url.queryParameters['status'];
        return _list([]);
      }),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.text('No invoices found'));

    await tester.tap(find.widgetWithText(FilterChip, 'Approved'));
    await _pumpUntil(tester, find.byType(InvoiceListTile));
    // Empty result — give the refetch a couple of frames to land.
    await tester.pump(const Duration(milliseconds: 50));

    expect(store.statusFilter, 'approved');
    expect(lastStatus, 'approved');
  });

  // The Overdue quick view spans statuses, so it and a status chip are
  // mutually exclusive: the chip row shows exactly one of them selected.
  group('Overdue chip', () {
    bool selected(WidgetTester tester, String label) =>
        tester.widget<FilterChip>(find.widgetWithText(FilterChip, label))
            .selected;

    testWidgets('tapping it selects Overdue, deselects All and asks for it',
        (tester) async {
      String? lastOverdue;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          lastOverdue = req.url.queryParameters['overdue'];
          return _list([]);
        }),
      );

      await tester.pumpWidget(_localized(const InvoicesScreen()));
      await _pumpUntil(tester, find.text('No invoices found'));
      expect(selected(tester, 'All'), isTrue);
      expect(selected(tester, 'Overdue'), isFalse);

      await tester.tap(find.widgetWithText(FilterChip, 'Overdue'));
      await tester.pump(const Duration(milliseconds: 50));

      expect(store.overdueOnly, isTrue);
      expect(store.statusFilter, isNull);
      expect(lastOverdue, 'true');
      expect(selected(tester, 'Overdue'), isTrue);
      expect(selected(tester, 'All'), isFalse,
          reason: 'All under the Overdue view would claim the whole list');
    });

    testWidgets('a status chip leaves the Overdue view', (tester) async {
      String? lastOverdue;
      String? lastStatus;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          lastOverdue = req.url.queryParameters['overdue'];
          lastStatus = req.url.queryParameters['status'];
          return _list([]);
        }),
      );

      await tester.pumpWidget(_localized(const InvoicesScreen()));
      await _pumpUntil(tester, find.text('No invoices found'));
      await tester.tap(find.widgetWithText(FilterChip, 'Overdue'));
      await tester.pump(const Duration(milliseconds: 50));
      expect(store.overdueOnly, isTrue);

      await tester.tap(find.widgetWithText(FilterChip, 'Approved'));
      await tester.pump(const Duration(milliseconds: 50));

      expect(store.overdueOnly, isFalse);
      expect(store.statusFilter, 'approved');
      expect(lastStatus, 'approved');
      expect(lastOverdue, isNull);
      expect(selected(tester, 'Approved'), isTrue);
      expect(selected(tester, 'Overdue'), isFalse);
    });

    testWidgets('showOverdue() empties a search the user typed',
        (tester) async {
      String? lastSearch;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          lastSearch = req.url.queryParameters['search'];
          return _list([]);
        }),
      );

      await tester.pumpWidget(_localized(const InvoicesScreen()));
      await _pumpUntil(tester, find.text('No invoices found'));
      await tester.enterText(find.byType(SearchBar), 'acme');
      // Past the 300ms debounce, so the query reached the store.
      await tester.pump(const Duration(milliseconds: 350));
      expect(lastSearch, 'acme');

      // The dashboard's Overdue card, from outside this screen.
      store.showOverdue();
      await tester.pump(const Duration(milliseconds: 50));

      expect(store.searchQuery, isNull);
      expect(lastSearch, isNull);
      final box = tester.widget<SearchBar>(find.byType(SearchBar));
      expect(box.controller?.text, isEmpty,
          reason: 'the box must not show a query the list no longer applies');
      expect(selected(tester, 'Overdue'), isTrue);
    });
  });

  testWidgets('typing in the search box drives the store search filter',
      (tester) async {
    String? lastSearch;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        lastSearch = req.url.queryParameters['search'];
        return _list([]);
      }),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.text('No invoices found'));

    await tester.enterText(find.byType(SearchBar), 'acme');
    // The search box debounces 300ms before firing (issue #182) — a short
    // pump must NOT have triggered the store yet.
    await tester.pump(const Duration(milliseconds: 50));
    expect(lastSearch, isNull);

    await tester.pump(const Duration(milliseconds: 300));
    expect(lastSearch, 'acme');
  });

  testWidgets('rapid keystrokes only fire one debounced request',
      (tester) async {
    var searchRequests = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.url.queryParameters.containsKey('search')) searchRequests++;
        return _list([]);
      }),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.text('No invoices found'));

    // Each keystroke restarts the debounce timer — none of these should reach
    // the store on their own.
    await tester.enterText(find.byType(SearchBar), 'a');
    await tester.pump(const Duration(milliseconds: 100));
    await tester.enterText(find.byType(SearchBar), 'ac');
    await tester.pump(const Duration(milliseconds: 100));
    await tester.enterText(find.byType(SearchBar), 'acm');
    await tester.pump(const Duration(milliseconds: 100));
    expect(searchRequests, 0,
        reason: 'still typing — the debounce timer keeps restarting');

    // Let the last keystroke's debounce elapse — exactly one request fires.
    await tester.pump(const Duration(milliseconds: 300));
    expect(searchRequests, 1);
  });

  testWidgets('serves cached rows when the network fails after a prior load',
      (tester) async {
    // 1) Prime the cache + store via a successful fetch.
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([_invoiceJson('1')])),
    );
    await store.fetch();
    expect(store.fromCache, isFalse);

    // 2) Network now down; mounting the screen triggers a fetch that falls
    // back to the cache for the same (null filter, no search) key.
    ApiClient().debugConfigure(
      client: MockClient((req) async => throw Exception('offline')),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.byType(InvoiceListTile));

    expect(find.byType(InvoiceListTile), findsOneWidget);
    expect(store.fromCache, isTrue);
  });

  testWidgets(
      'bulk Export action: picks CSV, POSTs bulk/export, hands bytes to share',
      (tester) async {
    final fake = _FakeFileShare();
    FileShare.debugOverride(fake);

    var exportCalls = 0;
    // First serve a successful login + /me (admin → canBulkEditInvoices), then
    // the invoice list, then the export bytes.
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.url.path == '/api/auth/login') {
          return http.Response(
            jsonEncode({'access_token': 'tok'}),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (req.url.path == '/api/auth/me') {
          return http.Response(
            jsonEncode(_me(['admin'])),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        if (req.method == 'POST' && req.url.path.endsWith('/bulk/export')) {
          exportCalls++;
          return http.Response(
            'id,vendor\n1,Acme Corp\n',
            200,
            headers: {
              'content-type': 'text/csv',
              'content-disposition':
                  'attachment; filename="invoices-export.csv"',
            },
          );
        }
        return _list([_invoiceJson('1', vendor: 'Acme Corp')]);
      }),
    );
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.byType(InvoiceListTile));

    // Enter selection mode and select the row.
    store.enterSelectionMode('1');
    await tester.pump();
    expect(find.byType(BulkActionBar), findsOneWidget);

    // Tap Export → choose CSV from the format sheet.
    await tester.tap(find.widgetWithText(TextButton, 'Export'));
    await tester.pumpAndSettle();
    expect(find.text('Export as…'), findsOneWidget);
    await tester.tap(find.text('CSV'));
    await _pumpUntilTrue(tester, () => fake.calls >= 1);

    expect(exportCalls, 1);
    expect(fake.calls, 1);
    expect(fake.lastFilename, 'invoices-export.csv');
    expect(fake.lastMimeType, 'text/csv');
    expect(String.fromCharCodes(fake.lastBytes!), contains('Acme Corp'));
    // Non-mutating read keeps the selection.
    expect(store.selectionMode, isTrue);
  });

  // The multi-select flow — selection app bar, the bulk bar, the export and
  // status sheets, the delete confirmation, and the result snackbars — was
  // English literals around an otherwise localized screen. Drive the whole
  // flow under `en` and again under `ja`; any string the two runs render
  // identically is a literal nobody routed through AppLocalizations
  // (`test/support/untranslated_text.dart`). The rows themselves are skipped:
  // InvoiceListTile and its StatusBadge are their own extraction turn.
  testWidgets('the bulk-selection flow renders no hardcoded English',
      (tester) async {
    Future<Set<String>> runFlow(Locale locale) async {
      final l = lookupAppLocalizations(locale);
      final seen = <String>{};
      void capture() => seen.addAll(renderedStrings(
            tester,
            skipSubtreesOf: {InvoiceListTile},
          ));
      Future<void> snack() async {
        await _pumpUntil(tester, find.byType(SnackBar));
        expect(find.byType(SnackBar), findsOneWidget);
        await tester.pump(const Duration(milliseconds: 300));
        capture();
        tester
            .state<ScaffoldMessengerState>(find.byType(ScaffoldMessenger))
            .removeCurrentSnackBar();
        await tester.pump();
      }

      final fake = _FakeFileShare();
      FileShare.debugOverride(fake);
      store.reset();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          final json = {'content-type': 'application/json'};
          if (req.url.path == '/api/auth/login') {
            return http.Response(
                jsonEncode({'access_token': 'tok'}), 200, headers: json);
          }
          if (req.url.path == '/api/auth/me') {
            return http.Response(jsonEncode(_me(['admin'])), 200,
                headers: json);
          }
          if (req.url.path.endsWith('/bulk/export')) {
            return http.Response('id\n1\n', 200, headers: {
              'content-type': 'text/csv',
              'content-disposition': 'attachment; filename="x.csv"',
            });
          }
          // One row moved/deleted, one skipped: exercises the plural and the
          // skipped-count suffix in every result line, and (for a status
          // change) the per-row reason, stated from its code.
          if (req.url.path.endsWith('/bulk/status')) {
            return http.Response(
                jsonEncode({
                  'updated': 1,
                  'skipped': [
                    {
                      'id': '2',
                      'reason': 'invoice is in an immutable status (paid)',
                      'code': 'invoice_status_immutable',
                      'params': {'status': 'paid'},
                    },
                  ],
                }),
                200,
                headers: json);
          }
          if (req.url.path.endsWith('/bulk/delete')) {
            return http.Response(
                jsonEncode({'deleted': 1, 'skipped': ['2']}), 200,
                headers: json);
          }
          return _list([
            _invoiceJson('1', status: 'ready_for_review'),
            _invoiceJson('2', status: 'ready_for_review'),
          ]);
        }),
      );
      await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

      await tester.pumpWidget(MaterialApp(
        // Keyed by locale so the second run mounts a fresh screen (and its
        // initState fetch) instead of updating the first run's.
        key: ValueKey(locale),
        locale: locale,
        localizationsDelegates: appLocalizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: const InvoicesScreen(),
      ));
      await _pumpUntil(tester, find.byType(InvoiceListTile));
      capture(); // the normal app bar, incl. the select-multiple action

      Future<void> selectBoth() async {
        await tester.tap(find.byIcon(Icons.checklist));
        await tester.pump();
        await tester.tap(find.byIcon(Icons.select_all));
        await tester.pump();
        expect(store.selectedCount, 2);
      }

      await selectBoth();
      capture(); // selection app bar + BulkActionBar

      // Export: format sheet, then the share hand-off.
      await tester.tap(find.byIcon(Icons.ios_share));
      await tester.pumpAndSettle();
      capture();
      await tester.tap(find.text(l.invoicesExportFormatCsv));
      await _pumpUntilTrue(tester, () => fake.calls >= 1);
      await tester.pumpAndSettle();

      // Status: target sheet, then the result dialog naming the skipped row.
      await tester.tap(find.byIcon(Icons.swap_horiz));
      await tester.pumpAndSettle();
      capture();
      await tester.tap(find.widgetWithText(ListTile, l.invoiceStatusApproved));
      await _pumpUntil(tester, find.byType(AlertDialog));
      await tester.pumpAndSettle();
      capture();
      await tester.tap(find.widgetWithText(TextButton, l.commonClose));
      await tester.pumpAndSettle();

      // Delete: confirmation dialog, then the result snackbar.
      await selectBoth();
      await tester.tap(find.byIcon(Icons.delete_outline));
      await tester.pumpAndSettle();
      capture();
      await tester.tap(find.widgetWithText(FilledButton, l.commonDelete));
      await snack();

      await AuthStore.instance.logout();
      return seen;
    }

    final english = await runFlow(const Locale('en'));
    // The English run proves the capture reached each step it guards.
    expect(english, containsAll(<String>[
      'Select multiple',
      '2 selected',
      'Cancel selection',
      'Export as…',
      'Change status to…',
      'Delete invoices?',
      'Moved 1 invoice to Approved (1 skipped)',
      'This invoice is Paid, so it can no longer be changed.',
      'Deleted 1 invoice (1 skipped)',
    ]));
    expectNoUntranslatedStrings(
      english: english,
      other: await runFlow(const Locale('ja')),
      // File-format names are the same token in every language.
      // An invoice number is the same token in every language too.
      allowed: {'CSV', 'XML', 'INV-2'},
      surface: 'InvoicesScreen bulk flow',
    );
  });

  // `POST /api/invoices/bulk/status` 422s a `rejected` target with no `reason`
  // (`review.reject_invoice` records it on the audit row and the
  // `review_rejected` exception the supplier corrects from). The sheet used to
  // offer "Rejected" and send `{ids, status}` only, so bulk-reject failed on
  // every tap.
  group('bulk reject', () {
    MockClient rejectClient(List<Map<String, dynamic>> bodies) => MockClient(
          (req) async {
            if (req.url.path == '/api/auth/login') {
              return http.Response(jsonEncode({'access_token': 'tok'}), 200,
                  headers: {'content-type': 'application/json'});
            }
            if (req.url.path == '/api/auth/me') {
              return http.Response(jsonEncode(_me(['admin'])), 200,
                  headers: {'content-type': 'application/json'});
            }
            if (req.method == 'POST' &&
                req.url.path.endsWith('/bulk/status')) {
              final body = jsonDecode(req.body) as Map<String, dynamic>;
              bodies.add(body);
              // Mirror the backend's refusal of a reasonless bulk rejection.
              final reason = (body['reason'] as String? ?? '').trim();
              if (body['status'] == 'rejected' && reason.isEmpty) {
                return http.Response(
                  jsonEncode({
                    'detail': 'A rejection reason is required when '
                        'bulk-rejecting invoices.',
                  }),
                  422,
                  headers: {'content-type': 'application/json'},
                );
              }
              return http.Response(
                  jsonEncode({'updated': 1, 'skipped': <Object>[]}), 200,
                  headers: {'content-type': 'application/json'});
            }
            return _list([_invoiceJson('1', status: 'ready_for_review')]);
          },
        );

    Future<void> openRejectChoice(WidgetTester tester) async {
      await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
      await tester.pumpWidget(_localized(const InvoicesScreen()));
      await _pumpUntil(tester, find.byType(InvoiceListTile));
      store.enterSelectionMode('1');
      await tester.pump();
      await tester.tap(find.widgetWithText(TextButton, 'Status'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(ListTile, 'Rejected'));
      await tester.pumpAndSettle();
    }

    testWidgets('asks for a reason and sends it with the rejection',
        (tester) async {
      final bodies = <Map<String, dynamic>>[];
      ApiClient().debugConfigure(client: rejectClient(bodies));
      await openRejectChoice(tester);

      // A reason prompt, not an immediate request.
      expect(find.byType(AlertDialog), findsOneWidget);
      expect(bodies, isEmpty);
      await tester.enterText(
          find.descendant(
              of: find.byType(AlertDialog), matching: find.byType(TextField)),
          '  Duplicate of INV-0 ');
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, 'Reject'));
      await _pumpUntilTrue(tester, () => bodies.isNotEmpty);
      await tester.pump();

      expect(bodies.single['status'], 'rejected');
      expect(bodies.single['ids'], ['1']);
      expect(bodies.single['reason'], 'Duplicate of INV-0');
      expect(store.selectionMode, isFalse,
          reason: 'a successful bulk op exits selection mode');
    });

    testWidgets('cancelling the reason prompt sends nothing', (tester) async {
      final bodies = <Map<String, dynamic>>[];
      ApiClient().debugConfigure(client: rejectClient(bodies));
      await openRejectChoice(tester);

      await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
      await tester.pumpAndSettle();
      expect(bodies, isEmpty);
      expect(store.selectionMode, isTrue);
    });

    testWidgets('a blank reason cannot be submitted', (tester) async {
      final bodies = <Map<String, dynamic>>[];
      ApiClient().debugConfigure(client: rejectClient(bodies));
      await openRejectChoice(tester);

      await tester.enterText(
          find.descendant(
              of: find.byType(AlertDialog), matching: find.byType(TextField)),
          '   ');
      await tester.pump();
      final confirm = tester.widget<FilledButton>(
          find.widgetWithText(FilledButton, 'Reject'));
      expect(confirm.onPressed, isNull);
      expect(bodies, isEmpty);
    });
  });

  // A bulk approve moves only rows awaiting review (backend decisions §418),
  // and every row it skips says why.
  group('bulk status picker and skipped rows', () {
    MockClient listClient(
      List<Map<String, dynamic>> rows, {
      Object skipped = const <Object>[],
    }) =>
        MockClient((req) async {
          final json = {'content-type': 'application/json'};
          if (req.url.path == '/api/auth/login') {
            return http.Response(
                jsonEncode({'access_token': 'tok'}), 200, headers: json);
          }
          if (req.url.path == '/api/auth/me') {
            return http.Response(jsonEncode(_me(['admin'])), 200,
                headers: json);
          }
          if (req.url.path.endsWith('/bulk/status')) {
            return http.Response(
                jsonEncode({'updated': 1, 'skipped': skipped}), 200,
                headers: json);
          }
          return _list(rows);
        });

    Future<void> openPicker(WidgetTester tester, List<String> ids) async {
      await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
      await tester.pumpWidget(_localized(const InvoicesScreen()));
      await _pumpUntil(tester, find.byType(InvoiceListTile));
      store.enterSelectionMode(ids.first);
      for (final id in ids.skip(1)) {
        store.toggleSelected(id);
      }
      await tester.pump();
      await tester.tap(find.widgetWithText(TextButton, 'Status'));
      await tester.pumpAndSettle();
    }

    testWidgets('offers Approved when every selected row awaits review',
        (tester) async {
      ApiClient().debugConfigure(
        client: listClient([
          _invoiceJson('1', status: 'ready_for_review'),
          _invoiceJson('2', status: 'ready_for_review'),
        ]),
      );
      await openPicker(tester, ['1', '2']);
      expect(find.widgetWithText(ListTile, 'Approved'), findsOneWidget);
    });

    testWidgets('hides Approved when any selected row is not in review',
        (tester) async {
      ApiClient().debugConfigure(
        client: listClient([
          _invoiceJson('1', status: 'ready_for_review'),
          _invoiceJson('2', status: 'new'),
        ]),
      );
      await openPicker(tester, ['1', '2']);
      expect(find.widgetWithText(ListTile, 'Approved'), findsNothing);
      // The other targets are still there.
      expect(find.widgetWithText(ListTile, 'Rejected'), findsOneWidget);
    });

    testWidgets('each skipped row is listed with its localized reason, else '
        'the server\'s English', (tester) async {
      ApiClient().debugConfigure(
        client: listClient(
          [
            _invoiceJson('1', status: 'ready_for_review'),
            _invoiceJson('2', status: 'ready_for_review'),
            _invoiceJson('3', status: 'ready_for_review'),
          ],
          skipped: [
            {
              'id': '2',
              'reason': 'server english',
              'code': 'approval_segregation',
              'params': <String, dynamic>{},
            },
            {'id': '3', 'reason': 'something this build cannot state'},
          ],
        ),
      );
      await openPicker(tester, ['1', '2', '3']);
      await tester.tap(find.widgetWithText(ListTile, 'Approved'));
      await _pumpUntil(tester, find.byType(AlertDialog));
      await tester.pumpAndSettle();

      expect(find.text('Moved 1 invoice to Approved (2 skipped)'),
          findsOneWidget);
      final two = find.byKey(const ValueKey('bulk-skip-2'));
      expect(find.descendant(of: two, matching: find.text('INV-2')),
          findsOneWidget);
      expect(
        find.descendant(
            of: two, matching: find.textContaining('Segregation of duties')),
        findsOneWidget,
      );
      expect(find.text('server english'), findsNothing);
      final three = find.byKey(const ValueKey('bulk-skip-3'));
      expect(
        find.descendant(
            of: three,
            matching: find.text('something this build cannot state')),
        findsOneWidget,
      );
    });

    testWidgets('a skipped row with no invoice number is named, not shown '
        'by its id', (tester) async {
      const id = '6f1c2e9a-0b7d-4c55-9a1e-3d2f4b5c6d7e';
      ApiClient().debugConfigure(
        client: listClient(
          [_invoiceJson(id, status: 'ready_for_review', number: '')],
          skipped: [
            {'id': id, 'reason': 'skipped'},
          ],
        ),
      );
      await openPicker(tester, [id]);
      await tester.tap(find.widgetWithText(ListTile, 'Approved'));
      await _pumpUntil(tester, find.byType(AlertDialog));
      await tester.pumpAndSettle();

      final row = find.byKey(const ValueKey('bulk-skip-$id'));
      expect(
        find.descendant(of: row, matching: find.text('Invoice (no number)')),
        findsOneWidget,
      );
      expect(find.textContaining(id), findsNothing);
    });

    testWidgets('a batch with no skips just confirms', (tester) async {
      ApiClient().debugConfigure(
        client: listClient([_invoiceJson('1', status: 'ready_for_review')]),
      );
      await openPicker(tester, ['1']);
      await tester.tap(find.widgetWithText(ListTile, 'Approved'));
      await _pumpUntil(tester, find.byType(SnackBar));
      expect(find.byType(AlertDialog), findsNothing);
      expect(find.text('Moved 1 invoice to Approved'), findsOneWidget);
    });
  });
}

Future<void> _pumpUntilTrue(WidgetTester tester, bool Function() c) async {
  for (var i = 0; i < 30 && !c(); i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}
