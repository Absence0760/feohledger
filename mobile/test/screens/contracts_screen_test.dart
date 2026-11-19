import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/contracts_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/contract_store.dart';
import 'package:feohledger_mobile/widgets/async_list_body.dart';
import 'package:feohledger_mobile/widgets/contract_list_tile.dart';

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
  jsonEncode({'items': items, 'total': items.length, 'page': 1}),
  200,
  headers: {'content-type': 'application/json'},
);

Map<String, dynamic> _contractJson(String id) => {
  'id': id,
  'contract_number': 'CTR-$id',
  'title': 'Contract $id',
  'contract_type': 'service',
  'status': 'active',
  'vendor_name': 'Acme',
  'currency': 'USD',
  'total_value': 1000,
  'created_at': '2026-01-01T12:00:00',
};

Widget _localized(Widget home) => MaterialApp(
  localizationsDelegates: appLocalizationsDelegates,
  supportedLocales: AppLocalizations.supportedLocales,
  home: home,
);

/// Bounded frames until [finder] matches — never pumpAndSettle, since a
/// pending fetch leaves a spinner animating forever.
Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 20 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

const _loadError =
    "Couldn't load contracts. Check your connection and tap Retry.";

void main() {
  setUpAll(() {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    ContractStore.instance.reset();
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  testWidgets('renders one ContractListTile per contract', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => _list([_contractJson('1'), _contractJson('2')]),
      ),
    );

    await tester.pumpWidget(_localized(const ContractsScreen()));
    await _pumpUntil(tester, find.byType(ContractListTile));

    expect(find.byType(ContractListTile), findsNWidgets(2));
    expect(find.byType(StaleDataBanner), findsNothing);
  });

  testWidgets('a failed load shows Retry, not "No contracts found"', (
    tester,
  ) async {
    var fail = true;
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async =>
            fail ? http.Response('boom', 500) : _list([_contractJson('1')]),
      ),
    );

    await tester.pumpWidget(_localized(const ContractsScreen()));
    await _pumpUntil(tester, find.widgetWithText(FilledButton, 'Retry'));

    expect(find.text(_loadError), findsOneWidget);
    expect(find.text('No contracts found'), findsNothing);

    fail = false;
    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await _pumpUntil(tester, find.byType(ContractListTile));
    expect(find.byType(ContractListTile), findsOneWidget);
    expect(find.text(_loadError), findsNothing);
  });

  testWidgets('an empty result shows the empty copy, which pulls to refresh', (
    tester,
  ) async {
    var calls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        calls++;
        return _list([]);
      }),
    );

    await tester.pumpWidget(_localized(const ContractsScreen()));
    await _pumpUntil(tester, find.text('No contracts found'));
    expect(calls, 1);

    await tester.fling(
      find.text('No contracts found'),
      const Offset(0, 300),
      1000,
    );
    await tester.pumpAndSettle();
    expect(calls, 2);
  });

  testWidgets('rows served from the offline cache carry the saved-copy banner', (
    tester,
  ) async {
    // Prime the cache with a successful fetch, then lose the network.
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([_contractJson('1')])),
    );
    await ContractStore.instance.fetch();
    ApiClient().debugConfigure(
      client: MockClient((req) async => throw Exception('offline')),
    );

    await tester.pumpWidget(_localized(const ContractsScreen()));
    await _pumpUntil(tester, find.byType(StaleDataBanner));

    expect(ContractStore.instance.fromCache, isTrue);
    expect(find.byType(ContractListTile), findsOneWidget);
    expect(
      find.text(
        "Showing a saved copy — couldn't reach the server. "
        'Pull down to try again.',
      ),
      findsOneWidget,
    );
  });
}
