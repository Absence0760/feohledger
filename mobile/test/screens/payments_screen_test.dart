import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/payments_screen.dart';

Widget _localized(Widget home) => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: home,
    );

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'payments': items}),
      200,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _paymentJson(
  String id, {
  double amount = 1250.50,
  String method = 'ach',
  String status = 'completed',
  String? reference = 'REF-001',
  // `PaymentResponse.currency` joins the invoice's code through, so a fixture
  // omitting it describes a payment whose invoice carries none. `null` models
  // exactly that (a legacy row), which is the case that must render bare.
  Object? currency = 'USD',
}) =>
    {
      'id': id,
      'invoice_id': 'inv-$id',
      'amount': amount,
      'currency': ?currency,
      'method': method,
      'status': status,
      'reference': reference,
      'created_at': '2026-01-01T12:00:00',
    };

/// Pumps fixed bounded frames until [finder] appears (or the budget runs out),
/// so the screen's fetch resolves and the loading spinner leaves the tree.
/// Never use pumpAndSettle here — a CircularProgressIndicator animates forever.
Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 20 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  testWidgets('shows a loading spinner while the fetch is in flight',
      (tester) async {
    // Hold the response open so the screen stays in its loading state.
    final completer = Completer<http.Response>();
    ApiClient().debugConfigure(
      client: MockClient((req) async => completer.future),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await tester.pump();

    expect(find.byType(CircularProgressIndicator), findsOneWidget);

    // Resolve the fetch before the test ends so no Timer/ticker is left pending.
    completer.complete(_list([]));
    await _pumpUntil(tester, find.text('No payments'));
  });

  testWidgets('renders the app bar title', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([])),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await _pumpUntil(tester, find.text('No payments'));

    expect(find.widgetWithText(AppBar, 'Payments'), findsOneWidget);
  });

  testWidgets('renders an empty state when there are no payments',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([])),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await _pumpUntil(tester, find.text('No payments'));

    expect(find.text('No payments'), findsOneWidget);
    expect(find.byType(ListTile), findsNothing);
  });

  testWidgets('renders a list tile per payment with formatted amount',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([
            _paymentJson('1', amount: 1250.50, reference: 'REF-001'),
            _paymentJson('2', amount: 99.00, reference: 'REF-002'),
          ])),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await _pumpUntil(tester, find.byType(ListTile));

    expect(find.byType(ListTile), findsNWidgets(2));
    expect(find.text(r'$1,250.50'), findsOneWidget);
    expect(find.text(r'$99.00'), findsOneWidget);
  });

  testWidgets('renders each payment in the currency its own invoice carries',
      (tester) async {
    // A payment is denominated in its invoice's currency, and one tenant's
    // history mixes them. The screen used to run every row through a
    // module-level `NumberFormat.currency(symbol: '\$')`, so a EUR payment
    // and a JPY payment both read as dollars — and JPY additionally grew two
    // decimal places the currency does not have.
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([
            _paymentJson('1', amount: 1250.50, currency: 'EUR'),
            _paymentJson('2', amount: 1250, currency: 'JPY', reference: 'R2'),
          ])),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await _pumpUntil(tester, find.byType(ListTile));

    expect(find.text('€1,250.50'), findsOneWidget);
    expect(find.text('¥1,250'), findsOneWidget);
    expect(find.textContaining(r'$'), findsNothing);
  });

  testWidgets('renders the bare figure when no currency was joined',
      (tester) async {
    // `PaymentResponse.currency` is explicit that `None` "is NOT a licence to
    // substitute a default". The digits still render; the symbol does not.
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([
            _paymentJson('1', amount: 1250.50, currency: null),
          ])),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await _pumpUntil(tester, find.byType(ListTile));

    expect(find.text('1,250.50'), findsOneWidget);
    expect(find.textContaining(r'$'), findsNothing);
  });

  testWidgets('subtitle shows the method label and reference', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([
            _paymentJson('1', method: 'wire', reference: 'WIRE-9'),
          ])),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await _pumpUntil(tester, find.byType(ListTile));

    expect(find.text('Wire • WIRE-9'), findsOneWidget);
  });

  testWidgets('subtitle falls back to a truncated id when reference is null',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([
            _paymentJson('abcdef1234567890', method: 'check', reference: null),
          ])),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await _pumpUntil(tester, find.byType(ListTile));

    // id.substring(0, 8) of 'abcdef1234567890' -> 'abcdef12'
    expect(find.text('Check • abcdef12'), findsOneWidget);
  });

  testWidgets('renders the status chip label for each payment status',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([
            _paymentJson('1', status: 'pending', reference: 'A'),
            _paymentJson('2', status: 'processing', reference: 'B'),
            _paymentJson('3', status: 'completed', reference: 'C'),
            _paymentJson('4', status: 'failed', reference: 'D'),
            _paymentJson('5', status: 'cancelled', reference: 'E'),
          ])),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await _pumpUntil(tester, find.byType(ListTile));

    expect(find.text('Pending'), findsOneWidget);
    expect(find.text('Processing'), findsOneWidget);
    expect(find.text('Completed'), findsOneWidget);
    expect(find.text('Failed'), findsOneWidget);
    expect(find.text('Cancelled'), findsOneWidget);
  });

  testWidgets('renders a method icon (CircleAvatar) for each payment',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([
            _paymentJson('1', method: 'ach', reference: 'A'),
            _paymentJson('2', method: 'virtual_card', reference: 'B'),
          ])),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await _pumpUntil(tester, find.byType(ListTile));

    expect(find.byType(CircleAvatar), findsNWidgets(2));
    expect(find.byIcon(Icons.account_balance), findsOneWidget);
    expect(find.byIcon(Icons.credit_card), findsOneWidget);
  });

  testWidgets('shows an error message when the fetch fails', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => http.Response('boom', 500)),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await _pumpUntil(tester, find.textContaining('Error:'));

    expect(find.textContaining('Error:'), findsOneWidget);
    expect(find.byType(ListTile), findsNothing);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('shows an error message when the transport throws',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => throw Exception('network down')),
    );

    await tester.pumpWidget(_localized(const PaymentsScreen()));
    await _pumpUntil(tester, find.textContaining('Error:'));

    expect(find.textContaining('Error:'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });
}
