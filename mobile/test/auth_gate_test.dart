// Regression coverage for the forced-logout dead end.
//
// `main.dart` held the only route to HomeScreen in an imperative
// `pushReplacement` fired once from the splash screen, and nothing above
// HomeScreen listened to AuthStore. So when `ApiClient` tore the session down
// on a 401 (expired / revoked token) the tree never re-routed: the user sat on
// a home screen with no user — nav collapsed to clerk-level tabs, every tab
// erroring, Settings reading "Unknown" — with no way back to login short of
// quitting the app. `mobile/CLAUDE.md` § API integration promises "401
// responses auto-clear session **and return to login**"; only the clear half
// was implemented.
//
// The root route is now a function of auth state (`AuthGate`), so these pin
// the routing contract rather than any one screen's behaviour.
import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/main.dart';
import 'package:feohledger_mobile/screens/home_screen.dart';
import 'package:feohledger_mobile/screens/login_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/services/push_service.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/dashboard_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _dashboardBody() => {
      'total_invoices': 0,
      'total_amount': 0,
      'pipeline': <String, int>{},
      'vendor_spend': <Map<String, dynamic>>[],
      'aging': {'current': 0, 'days_30': 0, 'days_60': 0, 'days_90_plus': 0},
      'monthly_trend': <Map<String, dynamic>>[],
      'upcoming_payments': <Map<String, dynamic>>[],
    };

/// Answers every request the clerk-role home tabs make (dashboard, invoices,
/// contracts) plus the auth profile load.
MockClient _homeClient() => MockClient((req) async {
      final path = req.url.path;
      if (path == '/api/auth/me') {
        return _json({
          'id': 'u1',
          'email': 'demo@acme.com',
          'full_name': 'Demo User',
          'organization_id': 'org1',
          'roles': ['ap_clerk'],
        });
      }
      if (path == '/api/dashboard') return _json(_dashboardBody());
      if (path == '/api/invoices') {
        return _json({'invoices': <Map<String, dynamic>>[]});
      }
      if (path == '/api/contracts') {
        return _json({'items': <Map<String, dynamic>>[]});
      }
      return _json({});
    });

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    AuthStore.instance.reset();
    DashboardStore.instance.reset();
    InvoiceStore.instance.reset();
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
    // Settings queries local_auth in initState; keep biometrics "unavailable".
    messenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/local_auth'),
      (call) async => call.method == 'getAvailableBiometrics' ? <String>[] : false,
    );
  });

  tearDown(() {
    messenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/local_auth'),
      null,
    );
  });

  /// Pumps bounded frames — never pumpAndSettle; the loaders animate forever.
  Future<void> pumpFrames(WidgetTester tester, {int frames = 30}) async {
    for (var i = 0; i < frames; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
  }

  testWidgets('with no stored session the gate renders the login screen',
      (tester) async {
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure(client: _homeClient());

    await tester.pumpWidget(const APApp());
    await pumpFrames(tester);

    expect(find.byType(LoginScreen), findsOneWidget);
    expect(find.byType(HomeScreen), findsNothing);
  });

  testWidgets('a restored session renders the home screen', (tester) async {
    FlutterSecureStorage.setMockInitialValues({
      'auth_token': 'tok',
      'tenant_slug': 'acme',
    });
    ApiClient().debugConfigure(client: _homeClient());

    await tester.pumpWidget(const APApp());
    await pumpFrames(tester);

    expect(find.byType(HomeScreen), findsOneWidget);
    expect(find.byType(LoginScreen), findsNothing);
  });

  testWidgets('a 401 forced logout returns to login instead of stranding the '
      'user on the home screen', (tester) async {
    FlutterSecureStorage.setMockInitialValues({
      'auth_token': 'tok',
      'tenant_slug': 'acme',
    });
    ApiClient().debugConfigure(client: _homeClient());

    await tester.pumpWidget(const APApp());
    await pumpFrames(tester);
    expect(find.byType(HomeScreen), findsOneWidget);

    // The token is revoked server-side; the next request 401s.
    ApiClient().debugConfigure(
      client: MockClient((req) async => _json({'detail': 'nope'}, 401)),
      timeout: const Duration(milliseconds: 50),
    );
    await ApiClient().setToken('tok');
    await expectLater(
      ApiClient().get('/invoices'),
      throwsA(isA<ApiException>()),
    );
    await pumpFrames(tester);

    expect(find.byType(LoginScreen), findsOneWidget);
    expect(find.byType(HomeScreen), findsNothing);
  });

  testWidgets('a forced logout also pops routes pushed above the gate',
      (tester) async {
    FlutterSecureStorage.setMockInitialValues({
      'auth_token': 'tok',
      'tenant_slug': 'acme',
    });
    ApiClient().debugConfigure(client: _homeClient());

    await tester.pumpWidget(const APApp());
    await pumpFrames(tester);
    expect(find.byType(HomeScreen), findsOneWidget);

    // The user drills into a detail route (the gate is the FIRST route, so a
    // login screen rendered underneath would otherwise stay invisible).
    PushService.navigatorKey.currentState!.push(
      MaterialPageRoute(
        builder: (_) => const Scaffold(body: Text('pushed detail')),
      ),
    );
    await pumpFrames(tester, frames: 10);
    expect(find.text('pushed detail'), findsOneWidget);

    ApiClient().debugConfigure(
      client: MockClient((req) async => _json({'detail': 'nope'}, 401)),
      timeout: const Duration(milliseconds: 50),
    );
    await ApiClient().setToken('tok');
    await expectLater(
      ApiClient().get('/invoices'),
      throwsA(isA<ApiException>()),
    );
    await pumpFrames(tester);

    expect(find.text('pushed detail'), findsNothing);
    expect(find.byType(LoginScreen), findsOneWidget);
  });
}
