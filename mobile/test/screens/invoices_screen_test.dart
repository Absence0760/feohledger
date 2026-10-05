import 'dart:async';
import 'dart:convert';

import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/invoices_screen.dart';
import 'package:feohledger_mobile/services/file_share.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/widgets/bulk_action_bar.dart';
import 'package:feohledger_mobile/widgets/invoice_list_tile.dart';

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

// Wraps a screen with the localization delegates it now needs. No explicit
// `locale` → defaults to `en`, so the English assertions below still hold.
Widget _localized(Widget home) => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
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
}) =>
    {
      'id': id,
      'invoice_number': 'INV-$id',
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

  testWidgets('falls back to the empty state on a network error with no cache',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => throw Exception('offline')),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.text('No invoices found'));

    // No cached rows exist, so the store ends with an empty list + error set,
    // and the screen shows its empty placeholder (not a spinner).
    expect(find.text('No invoices found'), findsOneWidget);
    expect(store.error, isNotNull);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('renders the search bar, status filter chips and camera action',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([])),
    );

    await tester.pumpWidget(_localized(const InvoicesScreen()));
    await _pumpUntil(tester, find.text('No invoices found'));

    expect(find.byType(SearchBar), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'All'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Pending'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Approved'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Paid'), findsOneWidget);
    expect(find.byIcon(Icons.camera_alt), findsOneWidget);
    // Advanced search action is present.
    expect(find.byIcon(Icons.tune), findsOneWidget);
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
}

Future<void> _pumpUntilTrue(WidgetTester tester, bool Function() c) async {
  for (var i = 0; i < 30 && !c(); i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}
