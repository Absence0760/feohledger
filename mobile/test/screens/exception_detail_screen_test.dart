import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/exception_detail_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/exception_store.dart';

Widget _host(Widget home) => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: home,
    );

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _detailJson({
  String status = 'open',
  String? assignedTo,
  String? assignedToUserId,
  String? dueAt = '2026-01-05T12:00:00',
  // `_exception_dict` joins the related INVOICE's currency through beside its
  // amount, because the figure IS that invoice's amount. `null` models the
  // no-invoice-joined case the endpoint also produces.
  Object? currency = 'USD',
}) =>
    {
      'id': '1',
      'invoice_id': 'inv-1',
      'invoice_number': 'INV-1',
      'vendor_name': 'Acme Corp',
      'amount': 250,
      'currency': ?currency,
      'exception_type': 'po_mismatch',
      'type_label': 'PO Mismatch',
      'severity': 'warning',
      'description': 'Amount differs from PO',
      'status': status,
      'assigned_to': assignedTo,
      'assigned_to_user_id': assignedToUserId,
      'is_overdue': false,
      'due_at': dueAt,
      'created_at': '2026-01-01T12:00:00',
    };

Map<String, dynamic> _me(List<String> roles) => {
      'id': 'u1',
      'email': 'demo@acme.com',
      'full_name': 'Demo User',
      'organization_id': 'org1',
      'roles': roles,
    };

Future<void> _loginThen(List<String> roles, MockClient screenClient) async {
  ApiClient().debugConfigure(
    client: MockClient((req) async {
      if (req.url.path == '/api/auth/login') return _json({'access_token': 't'});
      return _json(_me(roles));
    }),
  );
  await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
  ApiClient().debugConfigure(client: screenClient);
}

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
  setUpAll(() => OfflineStore.instance.debugUseMemory());

  setUp(() async {
    ExceptionStore.instance.reset();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  /// A tall surface so the whole detail ListView (incl. the bottom action
  /// buttons) builds — a ListView only lays out its visible children, so the
  /// default 600px test window would leave the actions unbuilt.
  void setTallSurface(WidgetTester tester) {
    tester.view.physicalSize = const Size(1200, 2000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
  }

  testWidgets('loads detail and renders the full fields', (tester) async {
    setTallSurface(tester);
    ApiClient().debugConfigure(
      client: MockClient((req) async => _json(_detailJson())),
    );

    await tester.pumpWidget(
      _host(const ExceptionDetailScreen(exceptionId: '1')),
    );
    await _pumpUntil(tester, find.text('PO Mismatch'));

    expect(find.text('PO Mismatch'), findsOneWidget);
    expect(find.text('Amount differs from PO'), findsOneWidget);
    // The amount wears the related invoice's currency, not this screen's old
    // module-level `\$`.
    expect(find.text(r'$250.00'), findsOneWidget);
    expect(find.text('INV-1'), findsOneWidget);
    expect(find.text('Acme Corp'), findsOneWidget);
    // Actionable → the three action buttons are present.
    expect(find.widgetWithText(FilledButton, 'Resolve'), findsOneWidget);
    expect(find.widgetWithText(OutlinedButton, 'Escalate'), findsOneWidget);
    expect(find.widgetWithText(OutlinedButton, 'Dismiss'), findsOneWidget);
    // Unassigned by default.
    expect(find.text('Unassigned'), findsOneWidget);
  });

  testWidgets('renders the error state on a 404 with a retry', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _json({'detail': 'nope'}, 404)),
    );

    await tester.pumpWidget(
      _host(const ExceptionDetailScreen(exceptionId: 'missing')),
    );
    await _pumpUntil(tester, find.text('Retry'));

    expect(find.text('Retry'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Resolve'), findsNothing);
  });

  testWidgets('resolve POSTs the action and pops', (tester) async {
    setTallSurface(tester);
    String? sentAction;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'POST' && req.url.path.endsWith('/resolve')) {
          sentAction =
              (jsonDecode(req.body) as Map<String, dynamic>)['action']
                  as String?;
          return _json({'id': '1', 'status': 'resolved'});
        }
        // getById + the post-resolve refetch list.
        if (req.method == 'GET' &&
            RegExp(r'/exceptions/[^/]+$').hasMatch(req.url.path)) {
          return _json(_detailJson());
        }
        return _json({'items': [], 'total': 0, 'page': 1});
      }),
    );

    await tester.pumpWidget(
      _host(const ExceptionDetailScreen(exceptionId: '1')),
    );
    await _pumpUntil(tester, find.widgetWithText(FilledButton, 'Resolve'));

    await tester.tap(find.widgetWithText(FilledButton, 'Resolve'));
    await _pumpUntilTrue(tester, () => sentAction != null);

    expect(sentAction, 'resolve');
  });

  testWidgets('admin can open the assignee picker and assign a user',
      (tester) async {
    // Default window so the bounded (60%-height) bottom-sheet picker fits;
    // scroll the detail list to the Assign button.
    String? sentUserId;
    await _loginThen(
      ['admin'],
      MockClient((req) async {
        if (req.method == 'GET' && req.url.path.endsWith('/admin/users')) {
          return _json({
            'items': [
              {
                'id': 'user-42',
                'email': 'casey@acme.com',
                'full_name': 'Casey Clerk',
                'is_active': true,
                'roles': [
                  {'id': 'r', 'name': 'ap_clerk'},
                ],
                'created_at': '2026-01-01T00:00:00',
              },
            ],
            'total': 1,
            'page': 1,
          });
        }
        if (req.method == 'POST' && req.url.path.endsWith('/assign')) {
          sentUserId =
              (jsonDecode(req.body) as Map<String, dynamic>)['user_id']
                  as String?;
          return _json(_detailJson(
            assignedTo: 'Casey Clerk',
            assignedToUserId: 'user-42',
          ));
        }
        // getById.
        return _json(_detailJson());
      }),
    );
    addTearDown(AuthStore.instance.logout);

    await tester.pumpWidget(
      _host(const ExceptionDetailScreen(exceptionId: '1')),
    );
    await _pumpUntil(tester, find.text('Acme Corp'));
    await tester.scrollUntilVisible(find.text('Assign'), 200);
    await tester.pump(const Duration(milliseconds: 50));
    await tester.tap(find.text('Assign'));
    await _pumpUntil(tester, find.text('Casey Clerk'));
    await tester.ensureVisible(find.text('Casey Clerk'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Casey Clerk'));
    await _pumpUntilTrue(tester, () => sentUserId != null);
    // Let the sheet pop animation finish (bounded — a SnackBar's real timer
    // would hang pumpAndSettle).
    await _pumpUntilTrue(
      tester,
      () => find.text('Unassigned').evaluate().isEmpty &&
          find.text('Casey Clerk').evaluate().length == 1,
    );

    expect(sentUserId, 'user-42');
    // The picked assignee is reflected on the panel (sheet has popped).
    expect(find.text('Casey Clerk'), findsOneWidget);
    expect(find.text('Unassigned'), findsNothing);
  });

  testWidgets('non-admin (ap_manager) gets no assignee picker', (tester) async {
    setTallSurface(tester);
    await _loginThen(
      ['ap_manager'],
      MockClient((req) async => _json(_detailJson())),
    );
    addTearDown(AuthStore.instance.logout);

    await tester.pumpWidget(
      _host(const ExceptionDetailScreen(exceptionId: '1')),
    );
    await _pumpUntil(tester, find.text('Unassigned'));

    // ap_manager can still act, but cannot reassign (no org-user list access).
    expect(find.text('Assign'), findsNothing);
    expect(find.widgetWithText(FilledButton, 'Resolve'), findsOneWidget);
  });

  testWidgets('the amount follows the related invoice\'s currency',
      (tester) async {
    // The exception queue's figure is the invoice's amount, so the invoice's
    // code is the only honest label for it — the org's reporting currency
    // would be a different wrong answer, since a GBP-reporting tenant holds
    // USD invoices. `/api/exceptions` did not send a currency at all before
    // this change, which is why the screen asserted dollars.
    ApiClient().debugConfigure(
      client: MockClient((req) async => _json(_detailJson(currency: 'EUR'))),
    );

    await tester.pumpWidget(_host(const ExceptionDetailScreen(exceptionId: '1')));
    await _pumpUntil(tester, find.text('PO Mismatch'));

    expect(find.text('€250.00'), findsOneWidget);
    expect(find.textContaining(r'$'), findsNothing);
  });

  testWidgets('an exception with no joined invoice currency renders bare',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _json(_detailJson(currency: null))),
    );

    await tester.pumpWidget(_host(const ExceptionDetailScreen(exceptionId: '1')));
    await _pumpUntil(tester, find.text('PO Mismatch'));

    expect(find.text('250.00'), findsOneWidget);
    expect(find.textContaining(r'$'), findsNothing);
  });
}
