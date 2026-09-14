// Regression coverage for a setState-after-dispose on the contract detail
// screen — the same shape as `invoice_detail_screen`'s `_load()`: await the
// GET, then setState in BOTH branches with no `mounted` check. Backing out of
// a contract before a slow GET (or its 10s timeout) resolved threw
// "setState() called after dispose()"; the FlutterError that raised was then
// caught by the network-error `catch`, whose own unguarded setState threw
// again — this time out of the async gap entirely.
import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/contract_detail_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _contractJson({String? currency = 'USD'}) => {
      'id': 'c1',
      'contract_number': 'CTR-001',
      'title': 'Cleaning services',
      'contract_type': 'service',
      'status': 'active',
      'vendor_name': 'Acme Corp',
      'currency': ?currency,
      'total_value': 120000,
      'spend_limit': 100000,
      'created_at': '2026-01-01T12:00:00',
      'spend': {
        'invoiced_total': 25000,
        'invoice_count': 3,
        'spend_limit': 100000,
        'remaining': 75000,
        'over_limit': false,
      },
      'line_items': [
        {
          'id': 'li1',
          'description': 'Monthly clean',
          'quantity': 12,
          'unit_price': 1000,
          'total': 12000,
        },
      ],
    };

void main() {
  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    FlutterSecureStorage.setMockInitialValues({});
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  /// Push the detail route, pop it while its GET is still in flight, then
  /// release the response. Waits on the real signal (the route leaving the
  /// tree), never a fixed delay.
  Future<void> pushPopComplete(
    WidgetTester tester,
    Completer<void> gate,
  ) async {
    final navigator = GlobalKey<NavigatorState>();
    await tester.pumpWidget(MaterialApp(
      navigatorKey: navigator,
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: const Scaffold(body: Text('list')),
    ));

    navigator.currentState!.push(
      MaterialPageRoute(
        builder: (_) => const ContractDetailScreen(contractId: 'c1'),
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.byType(ContractDetailScreen), findsOneWidget);

    navigator.currentState!.pop();
    await tester.pump();
    final gone = find.byType(ContractDetailScreen);
    for (var i = 0; i < 40 && gone.evaluate().isNotEmpty; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    expect(gone, findsNothing);

    gate.complete();
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
  }

  testWidgets('a late success response does not setState after dispose',
      (tester) async {
    final gate = Completer<void>();
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        await gate.future;
        return _json(_contractJson());
      }),
    );

    await pushPopComplete(tester, gate);

    expect(tester.takeException(), isNull);
  });

  testWidgets('a late failure response does not setState after dispose',
      (tester) async {
    final gate = Completer<void>();
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        await gate.future;
        return http.Response('boom', 500);
      }),
    );

    await pushPopComplete(tester, gate);

    expect(tester.takeException(), isNull);
  });

  testWidgets(
      'every figure on the detail — header, limit, spend, line items — uses '
      'the contract\'s own currency', (tester) async {
    // Seven call sites on one screen, all of them previously fed by a
    // module-level dollar formatter — directly above a `Currency` detail row
    // printing the real code. The spend rollup and the line items are
    // rendered by their own methods, so the currency has to be threaded
    // through both; this is what proves it reaches them.
    // A tall surface so the whole detail — including the line items at the
    // bottom — is laid out; a sliver does not build its off-screen children,
    // so a phone-sized viewport would silently skip half the assertions.
    tester.view.physicalSize = const Size(1200, 4000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => _json(_contractJson(currency: 'EUR')),
      ),
    );

    await tester.pumpWidget(MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: const ContractDetailScreen(contractId: 'c1'),
    ));
    for (var i = 0; i < 20 && find.text('EUR').evaluate().isEmpty; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }

    expect(find.text('€120,000.00'), findsOneWidget); // header value
    expect(find.textContaining('€100,000.00'), findsWidgets); // spend limit
    expect(find.text('€25,000.00'), findsOneWidget); // invoiced to date
    expect(find.text('€75,000.00'), findsOneWidget); // remaining
    expect(find.textContaining('€1,000.00'), findsOneWidget); // unit price
    expect(find.text('€12,000.00'), findsOneWidget); // line total
    expect(find.textContaining(r'$'), findsNothing);
  });

  testWidgets('a contract with no currency renders its figures bare',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => _json(_contractJson(currency: null)),
      ),
    );

    await tester.pumpWidget(MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: const ContractDetailScreen(contractId: 'c1'),
    ));
    for (var i = 0;
        i < 20 && find.text('Cleaning services').evaluate().isEmpty;
        i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }

    expect(find.text('120,000.00'), findsOneWidget);
    expect(find.textContaining(r'$'), findsNothing);
  });
}
