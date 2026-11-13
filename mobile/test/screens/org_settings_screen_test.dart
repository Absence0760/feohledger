import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/org_settings_screen.dart';
import 'package:feohledger_mobile/stores/org_settings_store.dart';

/// Localized host (defaults to `en`) so `AppLocalizations.of(context)` resolves.
Widget _host(Widget home) => MaterialApp(
      localizationsDelegates: appLocalizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: home,
    );

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _orgJson({String name = 'Acme Corp'}) => {
      'id': 'org1',
      'name': name,
      'slug': 'acme',
      'plan': 'pro',
      'created_at': '2026-01-01T00:00:00',
      'settings': {
        'company': {
          'address': '1 Main St',
          'phone': '555-0100',
          'website': 'https://acme.test',
          'tax_id': '12-3456789',
        },
        'invoice_defaults': {
          'currency': 'USD',
          'payment_terms': 'Net 30',
          'number_prefix': 'INV-',
        },
      },
    };

Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 20 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    OrgSettingsStore.instance.reset();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  // A tall surface so the whole form (incl. the Save button) is laid out at
  // once — the form is a ListView and lazy children aren't built off-screen.
  Future<void> pumpForm(WidgetTester tester) async {
    tester.view.physicalSize = const Size(1000, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(_host(const OrgSettingsScreen()));
    await _pumpUntil(tester, find.text('Acme Corp'));
  }

  testWidgets('seeds the form from the loaded settings', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _json(_orgJson())),
    );

    await pumpForm(tester);

    // The name + a company field + an invoice-default are seeded into fields.
    expect(find.text('Acme Corp'), findsOneWidget);
    expect(find.text('1 Main St'), findsOneWidget);
    expect(find.text('Net 30'), findsOneWidget);
    expect(find.text('Save changes'), findsOneWidget);
  });

  testWidgets('required name validation blocks an empty save', (tester) async {
    var patchCalls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'PATCH') {
          patchCalls++;
          return _json(_orgJson());
        }
        return _json(_orgJson());
      }),
    );

    await pumpForm(tester);

    // Clear the required org-name field, then attempt to save.
    await tester.enterText(
      find.widgetWithText(TextFormField, 'Organization name *'),
      '',
    );
    await tester.tap(find.text('Save changes'));
    await tester.pumpAndSettle();

    expect(find.text('Organization name is required'), findsOneWidget);
    expect(patchCalls, 0, reason: 'invalid form must not hit the API');
  });

  testWidgets('a valid edit PATCHes the settings and confirms', (tester) async {
    Map<String, dynamic>? patchBody;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'PATCH') {
          patchBody = jsonDecode(req.body) as Map<String, dynamic>;
          return _json(_orgJson(name: 'Acme Renamed'));
        }
        return _json(_orgJson());
      }),
    );

    await pumpForm(tester);

    await tester.enterText(
      find.widgetWithText(TextFormField, 'Organization name *'),
      'Acme Renamed',
    );
    await tester.tap(find.text('Save changes'));
    await tester.pumpAndSettle();

    expect(patchBody, isNotNull);
    expect(patchBody!['name'], 'Acme Renamed');
    expect(find.text('Organization settings saved'), findsOneWidget);
  });
}
