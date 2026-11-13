import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/workflows_screen.dart';
import 'package:feohledger_mobile/stores/workflow_store.dart';

Widget _host(Widget home) => MaterialApp(
      localizationsDelegates: appLocalizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: home,
    );

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'items': items, 'total': items.length, 'page': 1}),
      200,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _workflowJson(
  String id, {
  String name = 'Default Workflow',
  bool isActive = true,
  bool isDefault = true,
}) =>
    {
      'id': id,
      'name': name,
      'is_active': isActive,
      'is_default': isDefault,
      'steps_config': {
        'steps': [
          {'number': 1, 'type': 'extraction', 'name': 'Extract', 'config': {}},
        ],
      },
      'created_at': '2026-01-01T12:00:00',
    };

Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 20 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    WorkflowStore.instance.reset();
    ApiClient().debugConfigure();
  });

  testWidgets('renders one row per workflow once loaded', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([
            _workflowJson('1', name: 'Default Workflow'),
            _workflowJson('2', name: 'Rush Approval', isDefault: false),
          ])),
    );

    await tester.pumpWidget(_host(const WorkflowsScreen()));
    await _pumpUntil(tester, find.text('Default Workflow'));

    expect(find.text('Default Workflow'), findsOneWidget);
    expect(find.text('Rush Approval'), findsOneWidget);
    // Status + default badges render.
    expect(find.text('Active'), findsNWidgets(2));
    expect(find.text('Default'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('shows the empty state when there are no workflows',
      (tester) async {
    ApiClient().debugConfigure(client: MockClient((req) async => _list([])));

    await tester.pumpWidget(_host(const WorkflowsScreen()));
    await _pumpUntil(tester, find.text('No workflows found'));

    expect(find.text('No workflows found'), findsOneWidget);
  });

  testWidgets('shows an error state with Retry when the request fails',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => http.Response('boom', 500)),
    );

    await tester.pumpWidget(_host(const WorkflowsScreen()));
    await _pumpUntil(tester, find.text('Could not load workflows'));

    expect(find.text('Could not load workflows'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
  });
}
