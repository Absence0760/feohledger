import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/mfa_challenge.dart';
import 'package:feohledger_mobile/screens/mfa_screen.dart';

const _totpAndEmail = MFAChallenge(
  challengeToken: 'chal-abc',
  methods: ['totp', 'email'],
  mustEnroll: false,
);

void main() {
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  Future<void> pump(WidgetTester tester, {MFAChallenge? challenge}) async {
    await tester.pumpWidget(
      MaterialApp(
        localizationsDelegates: appLocalizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: MfaScreen(challenge: challenge ?? _totpAndEmail),
      ),
    );
  }

  testWidgets('renders the code field, Verify button, and TOTP prompt',
      (tester) async {
    await pump(tester);

    expect(find.widgetWithText(TextFormField, 'Code'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Verify'), findsOneWidget);
    expect(find.textContaining('authenticator app'), findsWidgets);
  });

  testWidgets('blocks submit and shows a validation error on an empty code',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        fail('verify should not be attempted with an empty code');
      }),
    );
    await pump(tester);

    await tester.tap(find.widgetWithText(FilledButton, 'Verify'));
    await tester.pump();

    expect(find.text('Required'), findsOneWidget);
  });

  testWidgets('rejects a code shorter than 6 digits', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        fail('verify should not be attempted with a too-short code');
      }),
    );
    await pump(tester);

    await tester.enterText(find.widgetWithText(TextFormField, 'Code'), '123');
    await tester.tap(find.widgetWithText(FilledButton, 'Verify'));
    await tester.pump();

    expect(find.text('Enter at least 6 digits'), findsOneWidget);
  });

  testWidgets('a wrong/expired code surfaces the friendly error and stays put',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.url.path == '/api/auth/mfa/verify') {
          return http.Response('invalid', 401);
        }
        return http.Response('not found', 404);
      }),
    );
    await ApiClient().setTenant('acme');
    await pump(tester);

    await tester.enterText(
      find.widgetWithText(TextFormField, 'Code'),
      '000000',
    );
    await tester.tap(find.widgetWithText(FilledButton, 'Verify'));
    await tester.pumpAndSettle();

    expect(find.textContaining('Invalid or expired code'), findsOneWidget);
    // Still on the MFA screen — the verify failed, no navigation.
    expect(find.widgetWithText(FilledButton, 'Verify'), findsOneWidget);
  });

  testWidgets(
    'switching to the email factor requests an OTP and shows the email prompt',
    (tester) async {
      var emailRequested = false;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path == '/api/auth/mfa/challenge/email') {
            emailRequested = true;
            return http.Response('', 204);
          }
          return http.Response('not found', 404);
        }),
      );
      await ApiClient().setTenant('acme');
      await pump(tester);

      await tester.tap(find.text('Use an email code instead'));
      await tester.pumpAndSettle();

      expect(emailRequested, isTrue);
      expect(find.textContaining('emailed you'), findsWidgets);
      // The resend affordance is now visible.
      expect(find.text('Resend email code'), findsOneWidget);
    },
  );

  testWidgets('email-only challenge defaults to the email factor',
      (tester) async {
    var emailRequested = false;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.url.path == '/api/auth/mfa/challenge/email') {
          emailRequested = true;
          return http.Response('', 204);
        }
        return http.Response('not found', 404);
      }),
    );
    await ApiClient().setTenant('acme');
    await pump(
      tester,
      challenge: const MFAChallenge(
        challengeToken: 'chal-xyz',
        methods: ['email'],
        mustEnroll: true,
      ),
    );

    await tester.pumpAndSettle();

    // The org-enforcement note shows and the email prompt is the default.
    expect(find.textContaining('organization requires'), findsOneWidget);
    expect(find.textContaining('emailed you'), findsWidgets);
    // No way to switch to TOTP (not offered).
    expect(find.text('Use authenticator app instead'), findsNothing);
    // Email is the only factor, so the OTP is auto-requested on open — the
    // "we emailed you" copy must be truthful.
    expect(emailRequested, isTrue);
  });
}
