import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/home_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/contract_store.dart';
import 'package:feohledger_mobile/stores/dashboard_store.dart';
import 'package:feohledger_mobile/stores/exception_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/stores/payment_queue_store.dart';
import 'package:feohledger_mobile/stores/vendor_store.dart';

import '../support/role_permissions.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _meBody(
  List<String> roles, {
  List<String>? permissions,
}) =>
    {
      'id': 'u1',
      'email': 'demo@acme.com',
      'full_name': 'Demo User',
      'organization_id': 'org1',
      'roles': roles,
      'permissions': permissions ?? systemRolePermissions(roles),
    };

/// Minimal-but-valid dashboard payload so DashboardStore.fetch resolves to a
/// loaded state (and the dashboard child's spinner leaves the tree).
Map<String, dynamic> _dashboardBody() => {
      'total_invoices': 3,
      'total_amount': 4200,
      'pipeline': {'ready_for_review': 1, 'approved': 2},
      'vendor_spend': [
        {'vendor': 'Acme', 'amount': 1000, 'invoice_count': 2},
      ],
      'aging': {
        'current': 100,
        'days_30': 50,
        'days_60': 25,
        'days_90_plus': 10,
      },
      'monthly_trend': [],
      'upcoming_payments': [],
    };

Map<String, dynamic> _invoiceJson(String id, {String status = 'pending'}) => {
      'id': id,
      'invoice_number': 'INV-$id',
      'vendor_name': 'Acme',
      'amount': 100,
      'currency': 'USD',
      'status': status,
      'created_at': '2026-01-01T12:00:00',
    };

/// A MockClient that answers every fetch the HomeScreen's child screens make:
/// auth (login/me/logout), dashboard, invoices, and payments. The IndexedStack
/// builds ALL visible children at once, so each must resolve or their infinite
/// loaders leak a pending Timer past tear-down.
MockClient _homeClient(
  List<String> roles, {
  List<Map<String, dynamic>>? invoices,
  List<String>? permissions,
}) {
  return MockClient((req) async {
    final path = req.url.path;
    if (req.method == 'POST' && path == '/api/auth/login') {
      return _json({'access_token': 'tok-123'});
    }
    if (req.method == 'GET' && path == '/api/auth/me') {
      return _json(_meBody(roles, permissions: permissions));
    }
    if (req.method == 'POST' && path == '/api/auth/logout') {
      return _json({});
    }
    if (req.method == 'GET' && path == '/api/dashboard') {
      return _json(_dashboardBody());
    }
    if (req.method == 'GET' && path == '/api/invoices') {
      // Honour the `status` filter the way the real endpoint does. The
      // Approvals tab now asks the server for `status=ready_for_review`
      // instead of slicing the Invoices tab's already-fetched page, so a
      // fake that ignores the filter would hand it rows the API would
      // never have returned.
      final wanted = req.url.queryParameters['status'];
      final all = invoices ?? [_invoiceJson('1')];
      final rows = wanted == null
          ? all
          : all.where((i) => i['status'] == wanted).toList();
      return _json({'invoices': rows});
    }
    if (req.method == 'GET' && path == '/api/payments') {
      return _json({'payments': <Map<String, dynamic>>[]});
    }
    if (req.method == 'GET' && path == '/api/vendors') {
      return _json({'items': <Map<String, dynamic>>[]});
    }
    if (req.method == 'GET' && path == '/api/payments/queue') {
      return _json({
        'items': <Map<String, dynamic>>[],
        'total': 0,
        'total_amount': 0,
        'total_savings': 0,
      });
    }
    if (req.method == 'GET' && path == '/api/payments/summary') {
      return _json({
        'total_paid': 0,
        'total_pending': 0,
        'payment_count': 0,
        'total_rebates': 0,
        'queue_count': 0,
      });
    }
    if (req.method == 'GET' && path == '/api/payments/runs/') {
      return _json({'items': <Map<String, dynamic>>[], 'total': 0});
    }
    if (req.method == 'GET' && path == '/api/contracts') {
      return _json({'items': <Map<String, dynamic>>[]});
    }
    if (req.method == 'GET' && path == '/api/exceptions') {
      return _json({'items': <Map<String, dynamic>>[]});
    }
    return http.Response('not found', 404);
  });
}

void main() {
  // Ensure the test binding exists before we touch its binary messenger.
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    DashboardStore.instance.reset();
    InvoiceStore.instance.reset();
    ContractStore.instance.reset();
    ExceptionStore.instance.reset();
    VendorStore.instance.reset();
    PaymentQueueStore.instance.reset();
    await OfflineStore.instance.clear();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();

    // The Settings child queries local_auth in initState. No platform
    // implementation is registered in the test VM, so stub the default
    // local_auth channel to keep biometrics "unavailable" (switch hidden)
    // without leaking a MissingPluginException out of the fire-and-forget call.
    messenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/local_auth'),
      (call) async {
        switch (call.method) {
          case 'getAvailableBiometrics':
            return <String>[];
          case 'isDeviceSupported':
            return false;
        }
        return null;
      },
    );
  });

  tearDown(() {
    messenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/local_auth'),
      null,
    );
  });

  Future<void> loginAs(
    List<String> roles, {
    List<Map<String, dynamic>>? invoices,
    List<String>? permissions,
  }) async {
    ApiClient().debugConfigure(
      client: _homeClient(roles, invoices: invoices, permissions: permissions),
    );
    final result =
        await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
    expect(result.isSuccess, isTrue);
  }

  /// The labels of the bottom-nav items, in order. Reading the nav model
  /// directly avoids matching child-screen AppBar titles, which share the same
  /// strings ("Dashboard", "Invoices", "Settings") and are all mounted at once
  /// inside the IndexedStack.
  List<String?> navLabels(WidgetTester tester) => tester
      .widget<BottomNavigationBar>(find.byType(BottomNavigationBar))
      .items
      .map((i) => i.label)
      .toList();

  /// Finds a bottom-nav item by its label. Scoped to the BottomNavigationBar so
  /// it never collides with the identically-named child-screen AppBar titles
  /// (all child screens are mounted at once inside the IndexedStack).
  Finder navItem(String label) => find.descendant(
        of: find.byType(BottomNavigationBar),
        matching: find.text(label),
      );

  /// Pumps bounded fixed frames until the home nav is mounted and every child
  /// loader has resolved — never pumpAndSettle (the spinners animate forever).
  Future<void> pumpHome(WidgetTester tester) async {
    await tester.pumpWidget(MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: const HomeScreen(),
    ));
    for (var i = 0;
        i < 30 &&
            find.byType(CircularProgressIndicator).evaluate().isNotEmpty;
        i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    // One more drain so any post-fetch notifyListeners settles.
    await tester.pump(const Duration(milliseconds: 50));
  }

  testWidgets('clerk sees Dashboard / Invoices / Contracts / Settings (4 tabs)',
      (tester) async {
    await loginAs(['ap_clerk']);
    await pumpHome(tester);

    expect(find.byType(BottomNavigationBar), findsOneWidget);
    // Clerk: no approval / payment privileges, but Contracts is all-roles.
    expect(
      navLabels(tester),
      ['Dashboard', 'Invoices', 'Contracts', 'Settings'],
    );

    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  // The CFO holds `invoice.approve` (POST /api/invoices/{id}/approve is
  // require_permission(PERM_INVOICE_APPROVE)) and is the ONLY role the
  // `require_cfo_above` gate accepts for a high-value invoice, so hiding
  // Approvals from them left such an invoice unapprovable from mobile. The
  // exception queue stays admin/ap_manager (require_roles on that router).
  testWidgets('cfo sees Approvals, Vendors / Pay / Payments but not Exceptions',
      (tester) async {
    await loginAs(['cfo']);
    await pumpHome(tester);

    expect(
      navLabels(tester),
      [
        'Dashboard',
        'Invoices',
        'Contracts',
        'Approvals',
        'Vendors',
        'Pay',
        'Payments',
        'Settings',
      ],
    );
  });

  testWidgets(
      'a custom role granted invoice.approve gets Approvals; the tab follows '
      'the permission, not the role name', (tester) async {
    await loginAs(['ap_clerk', 'approver'], permissions: ['invoice.approve']);
    await pumpHome(tester);

    expect(
      navLabels(tester),
      ['Dashboard', 'Invoices', 'Contracts', 'Approvals', 'Settings'],
    );
  });

  testWidgets(
      'ap_manager sees all nine tabs including Approvals, Exceptions, '
      'Vendors, Pay, Payments', (tester) async {
    await loginAs(['ap_manager']);
    await pumpHome(tester);

    expect(
      navLabels(tester),
      [
        'Dashboard',
        'Invoices',
        'Contracts',
        'Approvals',
        'Exceptions',
        'Vendors',
        'Pay',
        'Payments',
        'Settings',
      ],
    );
  });

  testWidgets('admin sees all nine tabs', (tester) async {
    await loginAs(['admin']);
    await pumpHome(tester);

    expect(navLabels(tester), hasLength(9));
    expect(
      navLabels(tester),
      [
        'Dashboard',
        'Invoices',
        'Contracts',
        'Approvals',
        'Exceptions',
        'Vendors',
        'Pay',
        'Payments',
        'Settings',
      ],
    );
  });

  testWidgets('first tab (Dashboard) is selected and shown on mount',
      (tester) async {
    await loginAs(['admin']);
    await pumpHome(tester);

    expect(
      tester
          .widget<BottomNavigationBar>(find.byType(BottomNavigationBar))
          .currentIndex,
      0,
    );
    // The Dashboard child's own AppBar title proves it is the foreground page.
    expect(find.widgetWithText(AppBar, 'Dashboard'), findsOneWidget);
  });

  testWidgets('tapping a nav item switches the foreground child screen',
      (tester) async {
    await loginAs(['admin']);
    await pumpHome(tester);

    // Move to the Settings tab (last item, index 8 for admin).
    await tester.tap(navItem('Settings'));
    await tester.pump();

    expect(
      tester
          .widget<BottomNavigationBar>(find.byType(BottomNavigationBar))
          .currentIndex,
      8,
    );
    // IndexedStack keeps all children mounted; Settings renders the logged-in
    // user's email in its profile header.
    expect(find.text('demo@acme.com'), findsOneWidget);
    expect(find.widgetWithText(AppBar, 'Settings'), findsOneWidget);
  });

  testWidgets('switching to the Invoices tab shows fetched invoice content',
      (tester) async {
    await loginAs(
      ['ap_clerk'],
      invoices: [_invoiceJson('1'), _invoiceJson('2')],
    );
    await pumpHome(tester);

    await tester.tap(navItem('Invoices'));
    await tester.pump();

    expect(find.widgetWithText(AppBar, 'Invoices'), findsOneWidget);
    // Two invoices fetched -> the empty-state copy must NOT be shown.
    expect(find.text('No invoices found'), findsNothing);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('Approvals tab shows the all-caught-up empty state when no '
      'invoices are awaiting review', (tester) async {
    // ap_manager (can approve) with only a non-review invoice -> pending is
    // empty, so the Approvals child paints its empty state, not a spinner.
    await loginAs(
      ['ap_manager'],
      invoices: [_invoiceJson('1', status: 'pending')],
    );
    await pumpHome(tester);

    await tester.tap(navItem('Approvals'));
    await tester.pump();

    expect(find.text('All caught up!'), findsOneWidget);
  });
}
