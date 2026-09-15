import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/login_screen.dart';
import 'package:feohledger_mobile/widgets/brand_mark.dart';

/// Wraps the screen in a localized MaterialApp (defaults to `en`) so
/// `AppLocalizations.of(context)` resolves and the English assertions hold.
Widget _host(Widget home) => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: home,
    );

void main() {
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  testWidgets('renders the tenant/email/password fields and Sign In button',
      (tester) async {
    await tester.pumpWidget(_host(const LoginScreen()));

    expect(find.widgetWithText(TextFormField, 'Tenant'), findsOneWidget);
    expect(find.widgetWithText(TextFormField, 'Email'), findsOneWidget);
    expect(find.widgetWithText(TextFormField, 'Password'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Sign In'), findsOneWidget);
    expect(find.byType(BrandMark), findsOneWidget);
    // tenant defaults to 'acme' (value + hint both read "acme")
    expect(find.text('acme'), findsWidgets);
  });

  testWidgets('blocks submit and shows validation errors when email/password '
      'are empty', (tester) async {
    // Any network hit would fail the test — submit must not reach the API.
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        fail('login should not be attempted with an invalid form');
      }),
    );

    await tester.pumpWidget(_host(const LoginScreen()));

    // Clear the prefilled tenant so all three are empty.
    await tester.enterText(find.widgetWithText(TextFormField, 'Tenant'), '');
    await tester.tap(find.widgetWithText(FilledButton, 'Sign In'));
    await tester.pump();

    expect(find.text('Required'), findsNWidgets(3));
  });

  testWidgets('surfaces the auth error after a failed sign-in', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => http.Response('nope', 401)),
    );

    await tester.pumpWidget(_host(const LoginScreen()));

    await tester.enterText(
        find.widgetWithText(TextFormField, 'Email'), 'demo@acme.com');
    await tester.enterText(
        find.widgetWithText(TextFormField, 'Password'), 'wrong');
    await tester.tap(find.widgetWithText(FilledButton, 'Sign In'));
    await tester.pumpAndSettle();

    expect(find.text('Invalid credentials'), findsOneWidget);
  });

  // The successful-login path navigates to the real HomeScreen (which spins an
  // infinite progress indicator); that's covered at the right altitude by
  // auth_store_test + home_screen_test, not by mounting the whole tree here.
}
