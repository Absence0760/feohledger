import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/config.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/login_screen.dart';
import 'package:feohledger_mobile/screens/settings_screen.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _meBody({
  String fullName = 'Demo User',
  String email = 'demo@acme.com',
  List<String> roles = const ['admin'],
}) =>
    {
      'id': 'u1',
      'email': email,
      'full_name': fullName,
      'organization_id': 'org1',
      'roles': roles,
    };

/// MockClient that logs a user in (with the given profile) and 200s on logout.
MockClient _happyClient({
  String fullName = 'Demo User',
  String email = 'demo@acme.com',
  List<String> roles = const ['admin'],
}) {
  return MockClient((req) async {
    final path = req.url.path;
    if (req.method == 'POST' && path == '/api/auth/login') {
      return _json({'access_token': 'tok-123'});
    }
    if (req.method == 'GET' && path == '/api/auth/me') {
      return _json(_meBody(fullName: fullName, email: email, roles: roles));
    }
    if (req.method == 'POST' && path == '/api/auth/logout') {
      return _json({});
    }
    return http.Response('not found', 404);
  });
}

/// Drive a real login so the screen renders behind an authenticated user.
Future<void> _loginAs({
  String fullName = 'Demo User',
  String email = 'demo@acme.com',
  List<String> roles = const ['admin'],
}) async {
  ApiClient().debugConfigure(
    client: _happyClient(fullName: fullName, email: email, roles: roles),
  );
  final result = await AuthStore.instance.login(email, 'demo', 'acme');
  expect(result.isSuccess, isTrue);
}

void main() {
  // The settings screen calls BiometricService.isAvailable in initState, which
  // hits the local_auth method channel. In the test VM that channel is
  // unregistered, so without a stub it throws MissingPluginException (which the
  // service does NOT catch — it only catches PlatformException), surfacing as an
  // unhandled async error. We register a no-op handler that reports "no
  // biometrics" so the screen settles into its non-biometric branch. We are NOT
  // exercising the biometric toggle here (the platform channel can't be driven
  // in tests) — this only keeps the screen from crashing during render.
  const localAuthChannel = MethodChannel('plugins.flutter.io/local_auth');

  setUp(() {
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();

    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(localAuthChannel, (call) async {
      switch (call.method) {
        case 'getAvailableBiometrics':
          return <String>[];
        case 'isDeviceSupported':
          return false;
        case 'authenticate':
          return false;
        default:
          return null;
      }
    });
  });

  tearDown(() async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(localAuthChannel, null);
    // Reset shared singleton state so it doesn't leak into sibling tests.
    // logout() swallows network errors, but point it at a mock so it never
    // touches a real localhost client during teardown.
    ApiClient().debugConfigure(
      client: MockClient((req) async => _json({})),
    );
    await AuthStore.instance.logout();
    AppConfig.apiBaseUrl = AppConfig.defaultApiUrl;
    AppConfig.tenantSlug = null;
  });

  // Localization delegates are required now that the screen reads
  // AppLocalizations; with no explicit `locale` the default is `en`, so the
  // existing English assertions below still hold.
  Widget wrap() => const MaterialApp(
        localizationsDelegates: appLocalizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: SettingsScreen(),
      );

  testWidgets('renders the logged-in user name, email and role chips',
      (tester) async {
    await _loginAs(
      fullName: 'Ada Lovelace',
      email: 'ada@acme.com',
      roles: ['admin', 'cfo'],
    );

    await tester.pumpWidget(wrap());
    await tester.pump();

    expect(find.text('Ada Lovelace'), findsOneWidget);
    expect(find.text('ada@acme.com'), findsOneWidget);
    // One Chip per role.
    expect(find.widgetWithText(Chip, 'admin'), findsOneWidget);
    expect(find.widgetWithText(Chip, 'cfo'), findsOneWidget);
  });

  testWidgets('avatar shows the first letter of the full name, uppercased',
      (tester) async {
    await _loginAs(fullName: 'ada lovelace', roles: ['ap_clerk']);

    await tester.pumpWidget(wrap());
    await tester.pump();

    final avatar = tester.widget<CircleAvatar>(find.byType(CircleAvatar));
    expect(((avatar.child as Text).data), 'A');
  });

  testWidgets('falls back to placeholders when there is no logged-in user',
      (tester) async {
    // No login — AuthStore.instance.user is null after the tearDown logout.
    expect(AuthStore.instance.user, isNull);

    await tester.pumpWidget(wrap());
    await tester.pump();

    expect(find.text('Unknown'), findsOneWidget);
    // Avatar placeholder for a missing name.
    final avatar = tester.widget<CircleAvatar>(find.byType(CircleAvatar));
    expect(((avatar.child as Text).data), '?');
  });

  testWidgets('shows the tenant slug and API server connection rows',
      (tester) async {
    await _loginAs();
    // login() set the tenant to 'acme' on AppConfig via ApiClient.setTenant.

    await tester.pumpWidget(wrap());
    await tester.pump();

    expect(find.widgetWithText(ListTile, 'Tenant'), findsOneWidget);
    expect(find.text('acme'), findsOneWidget);
    expect(find.widgetWithText(ListTile, 'API Server'), findsOneWidget);
    expect(find.text(AppConfig.apiBaseUrl), findsOneWidget);
  });

  testWidgets('renders "Not set" when no tenant is configured', (tester) async {
    await _loginAs();
    AppConfig.tenantSlug = null;

    await tester.pumpWidget(wrap());
    await tester.pump();

    expect(find.text('Not set'), findsOneWidget);
  });

  testWidgets('does not render the biometric toggle when biometrics are '
      'unavailable', (tester) async {
    await _loginAs();

    await tester.pumpWidget(wrap());
    // Let the (stubbed, unavailable) biometric probe resolve.
    await tester.pump();
    await tester.pump();

    expect(find.byType(SwitchListTile), findsNothing);
    expect(find.text('Biometric Unlock'), findsNothing);
  });

  testWidgets('always renders the Sign Out row', (tester) async {
    await _loginAs();

    await tester.pumpWidget(wrap());
    await tester.pump();

    // The language picker added a row, so Sign Out sits below the 600px test
    // viewport in the ListView — scroll it into view before asserting.
    await tester.scrollUntilVisible(
      find.widgetWithText(ListTile, 'Sign Out'),
      200,
    );

    expect(find.widgetWithText(ListTile, 'Sign Out'), findsOneWidget);
    expect(find.byIcon(Icons.logout), findsOneWidget);
  });


  // ── Navigation sections ───────────────────────────────────────────────
  //
  // The Settings list is the app's hub for everything that isn't a bottom-nav
  // tab, and each row is gated against the backend gate of the surface it opens
  // — per entry, not per section. These tests pin that mapping, because a row
  // shown to a role the API refuses is a guaranteed 403 on first paint, and a
  // row hidden from a role the API admits is a dead end.

  /// Scroll the settings list to the bottom so every lazily-built row is in the
  /// tree before asserting on what is and isn't there.
  Future<void> scrollToBottom(WidgetTester tester) async {
    for (var i = 0; i < 6; i++) {
      await tester.drag(find.byType(ListView), const Offset(0, -300));
      await tester.pumpAndSettle();
    }
  }

  testWidgets('every role gets Procurement → Quality Inspections',
      (tester) async {
    // `GET /api/inspections` and its detail are `get_current_user`, so the entry
    // point is not role-gated; the record affordance inside the screen is.
    for (final roles in [['ap_clerk'], ['cfo'], ['admin']]) {
      await _loginAs(roles: roles);
      await tester.pumpWidget(wrap());
      await tester.pump();
      await scrollToBottom(tester);

      expect(find.text('Procurement'), findsOneWidget, reason: '$roles');
      expect(
        find.widgetWithText(ListTile, 'Quality Inspections'),
        findsOneWidget,
        reason: '$roles',
      );
    }
  });

  testWidgets('an admin gets all four Administration rows', (tester) async {
    await _loginAs(roles: ['admin']);

    await tester.pumpWidget(wrap());
    await tester.pump();
    await scrollToBottom(tester);

    expect(find.text('Administration'), findsOneWidget);
    expect(find.widgetWithText(ListTile, 'User Management'), findsOneWidget);
    expect(
      find.widgetWithText(ListTile, 'Organization Settings'),
      findsOneWidget,
    );
    expect(find.widgetWithText(ListTile, 'Workflows'), findsOneWidget);
    expect(
      find.widgetWithText(ListTile, 'Adaptive Workflows'),
      findsOneWidget,
    );
  });

  testWidgets('a manager gets Adaptive Workflows and none of the admin rows',
      (tester) async {
    // The half a group-level `isOrgAdmin` gate got wrong: every /api/adaptive
    // read is admin | ap_manager | cfo, so hiding the whole section from a
    // manager hid a surface the backend admits.
    await _loginAs(roles: ['ap_manager']);

    await tester.pumpWidget(wrap());
    await tester.pump();
    await scrollToBottom(tester);

    expect(find.text('Administration'), findsOneWidget);
    expect(
      find.widgetWithText(ListTile, 'Adaptive Workflows'),
      findsOneWidget,
    );
    expect(find.widgetWithText(ListTile, 'User Management'), findsNothing);
    expect(
      find.widgetWithText(ListTile, 'Organization Settings'),
      findsNothing,
    );
  });

  testWidgets('a CFO gets Adaptive Workflows too', (tester) async {
    await _loginAs(roles: ['cfo']);

    await tester.pumpWidget(wrap());
    await tester.pump();
    await scrollToBottom(tester);

    expect(
      find.widgetWithText(ListTile, 'Adaptive Workflows'),
      findsOneWidget,
    );
    expect(find.widgetWithText(ListTile, 'User Management'), findsNothing);
  });

  testWidgets('a clerk gets no Administration section at all', (tester) async {
    // Clerks are outside `_READ_ROLES` as well as the admin surfaces, so the
    // section has nothing to show them.
    await _loginAs(roles: ['ap_clerk']);

    await tester.pumpWidget(wrap());
    await tester.pump();
    await scrollToBottom(tester);

    expect(find.text('Administration'), findsNothing);
    expect(
      find.widgetWithText(ListTile, 'Adaptive Workflows'),
      findsNothing,
    );
    expect(find.widgetWithText(ListTile, 'User Management'), findsNothing);
  });

  // Sign Out ends the session and nothing more. Returning to login is the root
  // AuthGate's job (test/auth_gate_test.dart) — this screen used to rebuild the
  // whole navigator stack with `pushAndRemoveUntil(..., (_) => false)`, which
  // removed the gate route itself and left the app with no reactive auth
  // routing for the rest of its lifetime.
  testWidgets('tapping Sign Out ends the session without rebuilding the stack',
      (tester) async {
    await _loginAs();
    expect(AuthStore.instance.loggedIn, isTrue);

    await tester.pumpWidget(wrap());
    await tester.pump();

    await tester.scrollUntilVisible(
      find.widgetWithText(ListTile, 'Sign Out'),
      200,
    );
    await tester.tap(find.widgetWithText(ListTile, 'Sign Out'));
    await tester.pumpAndSettle();

    expect(AuthStore.instance.loggedIn, isFalse);
    expect(
      find.byType(LoginScreen),
      findsNothing,
      reason: 'the screen must not navigate — the AuthGate re-routes',
    );
  });
}
