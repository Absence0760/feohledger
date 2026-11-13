import 'dart:async';
import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/approvals_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/widgets/invoice_list_tile.dart';

/// Wraps the screen in a MaterialApp carrying the localization delegates so the
/// localized `AppLocalizations.of(context)` resolves (defaults to English).
Widget _localized() => MaterialApp(
      localizationsDelegates: appLocalizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: const ApprovalsScreen(),
    );

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'invoices': items}),
      200,
      headers: {'content-type': 'application/json'},
    );

/// A fake that filters SERVER-side on `?status=`, the way the real
/// `GET /api/invoices` does. The approvals queue is a server-filtered request
/// now, so a fake that ignored the param would let a client-side filter pass.
MockClient _apiWith(List<Map<String, dynamic>> all) => MockClient((req) async {
      final status = req.url.queryParameters['status'];
      return _list(
        status == null
            ? all
            : all.where((i) => i['status'] == status).toList(),
      );
    });

Map<String, dynamic> _invoiceJson(
  String id, {
  String status = 'ready_for_review',
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

/// Pumps bounded frames until [finder] appears (or the budget runs out), so we
/// never call pumpAndSettle on a screen that may be showing a perpetual spinner.
Future<void> _pumpUntil(
  WidgetTester tester,
  Finder finder, {
  int maxFrames = 30,
}) async {
  for (var i = 0; i < maxFrames && finder.evaluate().isEmpty; i++) {
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
    FlutterSecureStorage.setMockInitialValues({});
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
    // Drain any queue the previous test left in the singleton so the screen
    // under test starts empty.
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([])),
    );
    await store.fetchPending();
  });

  testWidgets('shows the spinner while the initial fetch is in flight',
      (tester) async {
    final gate = Completer<void>();
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        await gate.future; // hold the fetch open
        return _list([]);
      }),
    );

    await tester.pumpWidget(_localized());
    // Let the post-frame callback fire and the store flip to loading.
    await tester.pump();
    await tester.pump();

    expect(find.byType(CircularProgressIndicator), findsOneWidget);

    // Resolve the fetch so no Timer/ticker is left pending at teardown.
    gate.complete();
    await _pumpUntil(tester, find.text('All caught up!'));
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('renders the empty "All caught up!" state when nothing is pending',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([])),
    );

    await tester.pumpWidget(_localized());
    await _pumpUntil(tester, find.text('All caught up!'));

    expect(find.text('All caught up!'), findsOneWidget);
    expect(find.text('No invoices waiting for approval'), findsOneWidget);
    expect(find.byIcon(Icons.check_circle), findsOneWidget);
    expect(find.byType(InvoiceListTile), findsNothing);
  });

  testWidgets('asks the server for ready_for_review rather than filtering a '
      'list the Invoices tab happened to fetch', (tester) async {
    String? sentStatus;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        sentStatus = req.url.queryParameters['status'];
        return _list([]);
      }),
    );

    await tester.pumpWidget(_localized());
    await _pumpUntil(tester, find.text('All caught up!'));

    expect(sentStatus, 'ready_for_review');
    expect(find.text('All caught up!'), findsOneWidget);
    expect(find.byType(InvoiceListTile), findsNothing);
  });

  // The regression: both tabs are children of one IndexedStack, so switching
  // to Approvals never re-ran initState. The screen rendered a client-side
  // slice of whatever the Invoices tab last fetched — pick the `paid` chip
  // there and the approvals queue read "All caught up!" while invoices sat in
  // ready_for_review, with pull-to-refresh unable to correct it.
  testWidgets('a status chip picked on the Invoices tab cannot empty the '
      'approvals queue', (tester) async {
    ApiClient().debugConfigure(
      client: _apiWith([
        _invoiceJson('1', status: 'ready_for_review', vendor: 'Vendor One'),
        _invoiceJson('2', status: 'paid', vendor: 'Vendor Two'),
      ]),
    );

    // The user filters the (shared, singleton) Invoices list to `paid` first.
    store.setStatusFilter('paid');
    await tester.pumpWidget(_localized());
    await _pumpUntil(tester, find.byType(InvoiceListTile));

    expect(find.byType(InvoiceListTile), findsOneWidget);
    expect(find.text('Vendor One'), findsOneWidget);
    expect(find.text('All caught up!'), findsNothing);

    // Pull-to-refresh re-issues the approvals request, not the Invoices one.
    await tester.fling(find.byType(ListView), const Offset(0, 400), 1000);
    await _pumpUntil(tester, find.byType(InvoiceListTile));
    expect(find.text('Vendor One'), findsOneWidget);
  });

  testWidgets('a failed load shows an error + Retry, never "All caught up!"',
      (tester) async {
    // Drop the queue the setUp drain cached — with a cache present the store
    // (correctly) serves it instead of surfacing the error, which is the
    // offline path covered in invoice_store_test.
    await OfflineStore.instance.clear();
    var calls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        calls++;
        if (calls == 1) return http.Response('boom', 500);
        return _list([_invoiceJson('1')]);
      }),
    );

    await tester.pumpWidget(_localized());
    await _pumpUntil(tester, find.text('Could not load pending approvals'));

    expect(find.text('Could not load pending approvals'), findsOneWidget);
    expect(find.text('All caught up!'), findsNothing);

    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await _pumpUntil(tester, find.byType(InvoiceListTile));
    expect(find.byType(InvoiceListTile), findsOneWidget);
  });

  testWidgets('lists only the ready_for_review invoices with a plural count',
      (tester) async {
    ApiClient().debugConfigure(
      client: _apiWith([
        _invoiceJson('1', status: 'ready_for_review', vendor: 'Vendor One'),
        _invoiceJson('2', status: 'approved', vendor: 'Vendor Two'),
        _invoiceJson('3', status: 'ready_for_review', vendor: 'Vendor Three'),
      ]),
    );

    await tester.pumpWidget(_localized());
    await _pumpUntil(tester, find.byType(InvoiceListTile));

    // Only the two ready_for_review rows render.
    expect(find.byType(InvoiceListTile), findsNWidgets(2));
    expect(find.text('Vendor One'), findsOneWidget);
    expect(find.text('Vendor Three'), findsOneWidget);
    expect(find.text('Vendor Two'), findsNothing);
    expect(find.text('2 invoices pending'), findsOneWidget);
  });

  testWidgets('uses the singular "1 invoice pending" label for one item',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([_invoiceJson('1')])),
    );

    await tester.pumpWidget(_localized());
    await _pumpUntil(tester, find.byType(InvoiceListTile));

    expect(find.text('1 invoice pending'), findsOneWidget);
    expect(find.text('1 invoices pending'), findsNothing);
  });

  testWidgets('renders the "Pending Approvals" app bar title', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([])),
    );

    await tester.pumpWidget(_localized());
    await _pumpUntil(tester, find.text('All caught up!'));

    expect(
      find.widgetWithText(AppBar, 'Pending Approvals'),
      findsOneWidget,
    );
  });

  testWidgets('swipe start-to-end approves the invoice via the store',
      (tester) async {
    var approveCalls = 0;
    var listCalls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'POST' && req.url.path.endsWith('/approve')) {
          approveCalls++;
          return http.Response(
            jsonEncode(_invoiceJson('1', status: 'approved')),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        listCalls++;
        // After approval, the refetch returns an empty pending list.
        return _list(approveCalls == 0 ? [_invoiceJson('1')] : []);
      }),
    );

    await tester.pumpWidget(_localized());
    await _pumpUntil(tester, find.byType(InvoiceListTile));
    expect(find.byType(InvoiceListTile), findsOneWidget);

    // Swipe left-to-right (startToEnd) triggers confirmDismiss -> approve().
    await tester.drag(
      find.byType(InvoiceListTile),
      const Offset(600, 0),
    );
    await _pumpUntil(tester, find.text('All caught up!'));

    expect(approveCalls, 1, reason: 'swipe-right should approve once');
    expect(
      listCalls,
      greaterThanOrEqualTo(2),
      reason: 'initial fetch + refetch after approve',
    );
    expect(find.text('All caught up!'), findsOneWidget);
  });

  testWidgets('swipe end-to-start does not approve (reject needs a reason)',
      (tester) async {
    var approveCalls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'POST') {
          approveCalls++;
          return http.Response('{}', 200,
              headers: {'content-type': 'application/json'});
        }
        return _list([_invoiceJson('1')]);
      }),
    );

    await tester.pumpWidget(_localized());
    await _pumpUntil(tester, find.byType(InvoiceListTile));

    // Swipe right-to-left (endToStart) — confirmDismiss returns false.
    await tester.drag(
      find.byType(InvoiceListTile),
      const Offset(-600, 0),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));

    expect(approveCalls, 0, reason: 'reject swipe must not call approve');
    // The row snaps back — still present and pending.
    expect(find.byType(InvoiceListTile), findsOneWidget);
  });
}
