import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/vendors_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/vendor_store.dart';
import 'package:feohledger_mobile/widgets/async_list_body.dart';
import 'package:feohledger_mobile/widgets/load_error_state.dart';
import 'package:feohledger_mobile/widgets/vendor_list_tile.dart';

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

http.Response _list(List<Map<String, dynamic>> items) =>
    _json({'items': items, 'total': items.length, 'page': 1});

Map<String, dynamic> _vendorJson(String id, {String status = 'unverified'}) => {
  'id': id,
  'name': 'Vendor $id',
  'code': 'V$id',
  'email': 'v$id@example.com',
  'status': status,
  'source': 'manual',
  'invoice_count': 1,
  'created_at': '2026-01-01T12:00:00',
};

Map<String, dynamic> _me(List<String> roles) => {
  'id': 'u1',
  'email': 'demo@acme.com',
  'full_name': 'Demo User',
  'organization_id': 'org1',
  'roles': roles,
};

Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 20 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

Future<void> _pumpUntilTrue(WidgetTester tester, bool Function() c) async {
  for (var i = 0; i < 20 && !c(); i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    VendorStore.instance.reset();
    await OfflineStore.instance.clear();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  /// Set the AuthStore's user to the given roles via a one-shot login, then
  /// swap the client to [screenClient] for the screen under test.
  Future<void> loginThen(List<String> roles, MockClient screenClient) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.url.path == '/api/auth/login') {
          return _json({'access_token': 'tok'});
        }
        return _json(_me(roles));
      }),
    );
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
    ApiClient().debugConfigure(client: screenClient);
  }

  testWidgets('a failed first load shows the shared error state with Retry, '
      'never the exception', (tester) async {
    var calls = 0;
    await loginThen(
      ['ap_manager'],
      MockClient((req) async {
        calls++;
        return calls == 1
            ? http.Response('boom', 500)
            : _list([_vendorJson('1')]);
      }),
    );

    await tester.pumpWidget(_localized(const VendorsScreen()));
    await _pumpUntil(tester, find.byType(LoadErrorState));

    // The shared first-load state, worded from the cause: a server answered
    // (a bare 500 carries no sentence of its own), so not "check your
    // connection" — and never the exception, nor "No vendors found".
    expect(find.byType(LoadErrorState), findsOneWidget);
    expect(
      find.text(
        "Couldn't load vendors: "
        "The server couldn't complete the request (HTTP 500).",
      ),
      findsOneWidget,
    );
    expect(find.text('No vendors found'), findsNothing);
    expect(VendorStore.instance.loadError, isA<ApiException>());
    expect(find.textContaining('boom'), findsNothing);
    expect(find.textContaining('ApiException'), findsNothing);
    expect(find.textContaining('Request failed'), findsNothing);

    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await _pumpUntil(tester, find.byType(VendorListTile));
    expect(find.byType(VendorListTile), findsOneWidget);
  });

  testWidgets('renders one VendorListTile per vendor once loaded', (
    tester,
  ) async {
    await loginThen([
      'ap_manager',
    ], MockClient((req) async => _list([_vendorJson('1'), _vendorJson('2')])));

    await tester.pumpWidget(_localized(const VendorsScreen()));
    await _pumpUntil(tester, find.byType(VendorListTile));

    expect(find.byType(VendorListTile), findsNWidgets(2));
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('renders the status filter chips', (tester) async {
    await loginThen(['ap_manager'], MockClient((req) async => _list([])));

    await tester.pumpWidget(_localized(const VendorsScreen()));
    await _pumpUntil(tester, find.text('No vendors found'));

    expect(find.widgetWithText(FilterChip, 'All'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Unverified'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Active'), findsOneWidget);
  });

  testWidgets('manager sees the ERP-sync action; the action POSTs sync-erp', (
    tester,
  ) async {
    var syncCalls = 0;
    await loginThen(
      ['ap_manager'],
      MockClient((req) async {
        if (req.method == 'POST' && req.url.path.endsWith('/sync-erp')) {
          syncCalls++;
          return _json({'success': true, 'message': 'Synced 0', 'created': 0});
        }
        return _list([_vendorJson('1')]);
      }),
    );

    await tester.pumpWidget(_localized(const VendorsScreen()));
    await _pumpUntil(tester, find.byType(VendorListTile));

    expect(find.byTooltip('Sync from ERP'), findsOneWidget);
    await tester.tap(find.byTooltip('Sync from ERP'));
    await _pumpUntilTrue(tester, () => syncCalls >= 1);

    expect(syncCalls, 1);
  });

  testWidgets('cfo (read-only) does NOT see the ERP-sync action', (
    tester,
  ) async {
    await loginThen([
      'cfo',
    ], MockClient((req) async => _list([_vendorJson('1')])));

    await tester.pumpWidget(_localized(const VendorsScreen()));
    await _pumpUntil(tester, find.byType(VendorListTile));

    expect(find.byTooltip('Sync from ERP'), findsNothing);
  });

  testWidgets('action sheet verify POSTs /verify and refetches', (
    tester,
  ) async {
    String? verifiedPath;
    var listCalls = 0;
    await loginThen(
      ['ap_manager'],
      MockClient((req) async {
        if (req.method == 'POST' && req.url.path.endsWith('/verify')) {
          verifiedPath = req.url.path;
          return _json(_vendorJson('1', status: 'active'));
        }
        listCalls++;
        return _list([_vendorJson('1')]);
      }),
    );

    await tester.pumpWidget(_localized(const VendorsScreen()));
    await _pumpUntil(tester, find.byType(VendorListTile));

    await tester.tap(find.byType(VendorListTile).first);
    await tester.pumpAndSettle();
    expect(find.text('Verify'), findsOneWidget);

    await tester.tap(find.text('Verify'));
    await _pumpUntilTrue(tester, () => verifiedPath != null && listCalls >= 2);

    expect(verifiedPath, endsWith('/vendors/1/verify'));
    expect(
      listCalls,
      greaterThanOrEqualTo(2),
      reason: 'verify should refetch after the initial load',
    );
  });

  testWidgets('the empty state can be pulled to refresh', (tester) async {
    var calls = 0;
    await loginThen(
      ['ap_manager'],
      MockClient((req) async {
        calls++;
        return _list([]);
      }),
    );

    await tester.pumpWidget(_localized(const VendorsScreen()));
    await _pumpUntil(tester, find.text('No vendors found'));
    expect(calls, 1);

    await tester.fling(
      find.text('No vendors found'),
      const Offset(0, 300),
      1000,
    );
    await tester.pumpAndSettle();
    await _pumpUntilTrue(tester, () => calls >= 2);

    expect(calls, 2);
  });

  testWidgets('a failed verify over a loaded list raises no stale banner', (
    tester,
  ) async {
    await loginThen(
      ['ap_manager'],
      MockClient((req) async {
        if (req.method == 'POST') {
          return _json({'detail': 'Not permitted'}, 403);
        }
        return _list([_vendorJson('1')]);
      }),
    );

    await tester.pumpWidget(_localized(const VendorsScreen()));
    await _pumpUntil(tester, find.byType(VendorListTile));

    bool? ok;
    VendorStore.instance.verify('1').then((v) => ok = v);
    await _pumpUntilTrue(tester, () => ok != null);

    expect(ok, isFalse);
    expect(VendorStore.instance.actionError, 'Not permitted');
    expect(VendorStore.instance.loadError, isNull);
    // The rows are live: the refusal is the action's, not the list's.
    expect(find.byType(StaleDataBanner), findsNothing);
    expect(find.byType(VendorListTile), findsOneWidget);
  });

  testWidgets('a status chip that filters every row out says so, and Show all '
      'clears it', (tester) async {
    await loginThen(
      ['ap_manager'],
      MockClient(
        (req) async => req.url.queryParameters['status'] == 'rejected'
            ? _list([])
            : _list([_vendorJson('1')]),
      ),
    );

    await tester.pumpWidget(_localized(const VendorsScreen()));
    await _pumpUntil(tester, find.byType(VendorListTile));

    await tester.tap(find.widgetWithText(FilterChip, 'Rejected'));
    await _pumpUntil(
      tester,
      find.text('No vendors match your search or filter.'),
    );
    // Not the whole-directory copy: other statuses hold vendors.
    expect(find.text('No vendors found'), findsNothing);

    await tester.tap(find.widgetWithText(OutlinedButton, 'Show all'));
    await _pumpUntil(tester, find.byType(VendorListTile));

    expect(VendorStore.instance.statusFilter, isNull);
    expect(VendorStore.instance.isFiltered, isFalse);
    expect(find.byType(VendorListTile), findsOneWidget);
  });

  testWidgets('a search that matches nothing says so, and Show all clears the '
      'search box too', (tester) async {
    await loginThen(
      ['ap_manager'],
      MockClient(
        (req) async => req.url.queryParameters['search'] == 'zzz'
            ? _list([])
            : _list([_vendorJson('1')]),
      ),
    );

    await tester.pumpWidget(_localized(const VendorsScreen()));
    await _pumpUntil(tester, find.byType(VendorListTile));

    await tester.enterText(find.byType(SearchBar), 'zzz');
    // Past the search debounce.
    await tester.pump(const Duration(milliseconds: 350));
    await _pumpUntil(
      tester,
      find.text('No vendors match your search or filter.'),
    );
    expect(VendorStore.instance.searchQuery, 'zzz');

    await tester.tap(find.widgetWithText(OutlinedButton, 'Show all'));
    await _pumpUntil(tester, find.byType(VendorListTile));

    expect(VendorStore.instance.searchQuery, isNull);
    expect(
      tester.widget<SearchBar>(find.byType(SearchBar)).controller!.text,
      isEmpty,
    );
    expect(find.byType(VendorListTile), findsOneWidget);
  });
}
