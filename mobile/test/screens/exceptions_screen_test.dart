import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/exceptions_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/exception_store.dart';
import 'package:feohledger_mobile/widgets/bulk_action_bar.dart';
import 'package:feohledger_mobile/widgets/exception_list_tile.dart';

import '../support/untranslated_text.dart';

Map<String, dynamic> _me(List<String> roles) => {
      'id': 'u1',
      'email': 'demo@acme.com',
      'full_name': 'Demo User',
      'organization_id': 'org1',
      'roles': roles,
    };

/// Log the AuthStore in as a user with [roles] (so the bulk-select affordance
/// shows), then swap the client to [screenClient] for the screen under test.
Future<void> _loginThen(List<String> roles, MockClient screenClient) async {
  ApiClient().debugConfigure(
    client: MockClient((req) async {
      if (req.url.path == '/api/auth/login') {
        return http.Response(
          jsonEncode({'access_token': 'tok'}),
          200,
          headers: {'content-type': 'application/json'},
        );
      }
      return http.Response(
        jsonEncode(_me(roles)),
        200,
        headers: {'content-type': 'application/json'},
      );
    }),
  );
  await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
  ApiClient().debugConfigure(client: screenClient);
}

Widget _localized(Widget home) => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: home,
    );

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'items': items, 'total': items.length, 'page': 1}),
      200,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _exceptionJson(
  String id, {
  String status = 'open',
  String type = 'duplicate',
  String typeLabel = 'Duplicate Invoice',
}) =>
    {
      'id': id,
      'invoice_id': 'inv-$id',
      'invoice_number': 'INV-$id',
      'vendor_name': 'Acme Corp',
      'amount': 250,
      'exception_type': type,
      'type_label': typeLabel,
      'severity': 'error',
      'status': status,
      'is_overdue': false,
      'created_at': '2026-01-01T12:00:00',
    };

/// Pumps fixed bounded frames until [finder] is present (or the budget is
/// exhausted). Never pumpAndSettle — a pending fetch leaves a spinner
/// animating forever.
Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 20 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

/// Pumps bounded frames until [condition] holds — used to wait on the real
/// signal that an async store action (POST then refetch) has landed, rather
/// than guessing a fixed delay.
Future<void> _pumpUntilTrue(WidgetTester tester, bool Function() condition) async {
  for (var i = 0; i < 20 && !condition(); i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  final store = ExceptionStore.instance;

  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    ExceptionStore.instance.reset();
    await OfflineStore.instance.clear();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
    store.setStatusFilter(null);
  });

  testWidgets('renders one ExceptionListTile per exception once loaded',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([
            _exceptionJson('1', typeLabel: 'Duplicate Invoice'),
            _exceptionJson('2', type: 'po_mismatch', typeLabel: 'PO Mismatch'),
          ])),
    );

    await tester.pumpWidget(_localized(const ExceptionsScreen()));
    await _pumpUntil(tester, find.byType(ExceptionListTile));

    expect(find.byType(ExceptionListTile), findsNWidgets(2));
    expect(find.text('Duplicate Invoice'), findsOneWidget);
    expect(find.text('PO Mismatch'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('renders the empty state when the queue is clear', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([])),
    );

    await tester.pumpWidget(_localized(const ExceptionsScreen()));
    await _pumpUntil(tester, find.text('No exceptions'));

    expect(find.text('No exceptions'), findsOneWidget);
    expect(find.byType(ExceptionListTile), findsNothing);
  });

  testWidgets('renders the status filter chips', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([])),
    );

    await tester.pumpWidget(_localized(const ExceptionsScreen()));
    await _pumpUntil(tester, find.text('No exceptions'));

    expect(find.widgetWithText(FilterChip, 'All'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Open'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Escalated'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Resolved'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Dismissed'), findsOneWidget);
  });

  testWidgets('tapping a status chip narrows the request', (tester) async {
    String? lastStatus;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        lastStatus = req.url.queryParameters['status'];
        return _list([]);
      }),
    );

    await tester.pumpWidget(_localized(const ExceptionsScreen()));
    await _pumpUntil(tester, find.text('No exceptions'));

    await tester.tap(find.widgetWithText(FilterChip, 'Escalated'));
    await tester.pump(const Duration(milliseconds: 50));

    expect(store.statusFilter, 'escalated');
    expect(lastStatus, 'escalated');
  });

  testWidgets('tapping a row opens the detail screen and resolve POSTs',
      (tester) async {
    String? sentAction;
    var detailCalls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'POST' && req.url.path.endsWith('/resolve')) {
          sentAction =
              (jsonDecode(req.body) as Map<String, dynamic>)['action']
                  as String?;
          return http.Response('{}', 200);
        }
        // The detail screen's GET /exceptions/{id}.
        if (req.method == 'GET' &&
            RegExp(r'/exceptions/[^/]+$').hasMatch(req.url.path)) {
          detailCalls++;
          return http.Response(
            jsonEncode(_exceptionJson('1')),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return _list([_exceptionJson('1')]);
      }),
    );

    await tester.pumpWidget(_localized(const ExceptionsScreen()));
    await _pumpUntil(tester, find.byType(ExceptionListTile));

    // Tapping a row navigates to the detail screen; wait for its GET to land
    // and the detail action buttons to render.
    await tester.tap(find.byType(ExceptionListTile).first);
    await _pumpUntil(tester, find.widgetWithText(FilledButton, 'Resolve'));
    expect(detailCalls, greaterThanOrEqualTo(1));
    expect(find.widgetWithText(OutlinedButton, 'Escalate'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Resolve'));
    await _pumpUntilTrue(tester, () => sentAction != null);

    expect(sentAction, 'resolve');
  });

  testWidgets('swiping a row right resolves the exception', (tester) async {
    String? sentAction;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'POST' && req.url.path.endsWith('/resolve')) {
          sentAction =
              (jsonDecode(req.body) as Map<String, dynamic>)['action']
                  as String?;
          return http.Response('{}', 200);
        }
        return _list([_exceptionJson('1')]);
      }),
    );

    await tester.pumpWidget(_localized(const ExceptionsScreen()));
    await _pumpUntil(tester, find.byType(ExceptionListTile));

    await tester.drag(
      find.byType(ExceptionListTile).first,
      const Offset(600, 0),
    );
    // Pump frames to drive the dismiss animation + confirmDismiss callback, then
    // wait on the real signal (the POST landing), not a fixed delay.
    await _pumpUntilTrue(tester, () => sentAction != null);

    expect(sentAction, 'resolve');
  });

  testWidgets(
      'multi-select then bulk-resolve POSTs the selection and shows counts',
      (tester) async {
    List<dynamic>? sentIds;
    await _loginThen(
      ['ap_manager'],
      MockClient((req) async {
        if (req.method == 'POST' &&
            req.url.path.endsWith('/exceptions/bulk/resolve')) {
          sentIds =
              (jsonDecode(req.body) as Map<String, dynamic>)['ids'] as List?;
          return http.Response(
            jsonEncode({
              'updated': 2,
              'skipped': [
                {'id': '3', 'reason': 'already_resolved'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return _list([_exceptionJson('1'), _exceptionJson('2')]);
      }),
    );
    addTearDown(AuthStore.instance.logout);

    await tester.pumpWidget(_localized(const ExceptionsScreen()));
    await _pumpUntil(tester, find.byType(ExceptionListTile));

    // The checklist app-bar action enters selection mode (admin/ap_manager).
    await tester.tap(find.byTooltip('Select exceptions'));
    await tester.pump(const Duration(milliseconds: 50));
    expect(store.selectionMode, isTrue);
    expect(find.byType(BulkActionBar), findsOneWidget);

    // Select both rows.
    await tester.tap(find.byType(ExceptionListTile).at(0));
    await tester.tap(find.byType(ExceptionListTile).at(1));
    await tester.pump(const Duration(milliseconds: 50));
    expect(store.selectedCount, 2);

    // Bulk-resolve via the shared bar's status slot, which this screen names
    // for what it does here.
    expect(find.widgetWithText(TextButton, 'Status'), findsNothing);
    expect(find.widgetWithText(TextButton, 'Delete'), findsNothing);
    expect(find.widgetWithText(TextButton, 'Dismiss'), findsOneWidget);
    await tester.tap(find.widgetWithText(TextButton, 'Resolve'));
    await _pumpUntilTrue(tester, () => sentIds != null);
    await tester.pump(const Duration(milliseconds: 50));

    expect(sentIds, containsAll(<String>['1', '2']));
    expect(find.text('Resolved 2 exceptions (1 skipped)'), findsOneWidget);
    // Selection mode exits on a successful bulk call.
    expect(store.selectionMode, isFalse);
  });

  // The selection app bar, the shared bar and the result snackbars were
  // English literals (`N selected`, `Resolved N exception(s)`, `(M skipped)`).
  // Drive the flow under `en` and `ja`; a string both runs render identically
  // is a literal (`test/support/untranslated_text.dart`). The rows are their
  // own extraction turn, so their subtrees are skipped.
  testWidgets('the bulk-selection flow renders no hardcoded English',
      (tester) async {
    Future<Set<String>> runFlow(Locale locale) async {
      final seen = <String>{};
      void capture() => seen.addAll(renderedStrings(
            tester,
            skipSubtreesOf: {ExceptionListTile},
          ));
      Future<void> bulk(IconData icon) async {
        await tester.tap(find.byIcon(Icons.checklist));
        await tester.pump(const Duration(milliseconds: 50));
        await tester.tap(find.byType(ExceptionListTile).at(0));
        await tester.tap(find.byType(ExceptionListTile).at(1));
        await tester.pump(const Duration(milliseconds: 50));
        capture(); // selection app bar + BulkActionBar
        await tester.tap(find.byIcon(icon));
        await _pumpUntil(tester, find.byType(SnackBar));
        expect(find.byType(SnackBar), findsOneWidget);
        await tester.pump(const Duration(milliseconds: 300));
        capture();
        tester
            .state<ScaffoldMessengerState>(find.byType(ScaffoldMessenger))
            .removeCurrentSnackBar();
        await tester.pump();
      }

      store.reset();
      await _loginThen(
        ['ap_manager'],
        MockClient((req) async {
          if (req.url.path.endsWith('/exceptions/bulk/resolve')) {
            return http.Response(
              jsonEncode({
                'updated': 1,
                'skipped': [
                  {'id': '2', 'reason': 'already_resolved'},
                ],
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return _list([_exceptionJson('1'), _exceptionJson('2')]);
        }),
      );
      await tester.pumpWidget(MaterialApp(
        // Keyed so the second run mounts a fresh screen (and its fetch).
        key: ValueKey(locale),
        locale: locale,
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: const ExceptionsScreen(),
      ));
      await _pumpUntil(tester, find.byType(ExceptionListTile));
      capture(); // the normal app bar, incl. the select action

      await bulk(Icons.swap_horiz); // resolve
      await _pumpUntil(tester, find.byType(ExceptionListTile));
      await bulk(Icons.delete_outline); // dismiss

      await AuthStore.instance.logout();
      return seen;
    }

    final english = await runFlow(const Locale('en'));
    expect(english, containsAll(<String>[
      'Select exceptions',
      '2 selected',
      'Cancel selection',
      'Resolve',
      'Dismiss',
      'Resolved 1 exception (1 skipped)',
      'Dismissed 1 exception (1 skipped)',
    ]));
    expectNoUntranslatedStrings(
      english: english,
      other: await runFlow(const Locale('ja')),
      surface: 'ExceptionsScreen bulk flow',
    );
  });
}
