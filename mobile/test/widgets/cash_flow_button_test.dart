import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/widgets/cash_flow_button.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

MockClient _client(List<String> roles) => MockClient((req) async {
      final path = req.url.path;
      if (req.method == 'POST' && path == '/api/auth/login') {
        return _json({'access_token': 'tok-123'});
      }
      if (req.method == 'GET' && path == '/api/auth/me') {
        return _json({
          'id': 'u1',
          'email': 'demo@acme.com',
          'full_name': 'Demo User',
          'organization_id': 'org1',
          'roles': roles,
        });
      }
      return http.Response('not found', 404);
    });

Widget _host() => const MaterialApp(
      home: Scaffold(
        appBar: null,
        body: Row(children: [CashFlowButton()]),
      ),
    );

void main() {
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({});
  });

  Future<void> loginAs(List<String> roles) async {
    ApiClient().debugConfigure(client: _client(roles));
    final result =
        await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
    expect(result.isSuccess, isTrue);
  }

  testWidgets('renders the action for an admin', (tester) async {
    await loginAs(['admin']);
    await tester.pumpWidget(_host());
    expect(find.byIcon(Icons.show_chart), findsOneWidget);
  });

  testWidgets('renders the action for a CFO', (tester) async {
    await loginAs(['cfo']);
    await tester.pumpWidget(_host());
    expect(find.byIcon(Icons.show_chart), findsOneWidget);
  });

  testWidgets('hides the action for an ap_manager', (tester) async {
    await loginAs(['ap_manager']);
    await tester.pumpWidget(_host());
    expect(find.byIcon(Icons.show_chart), findsNothing);
  });

  testWidgets('hides the action for an ap_clerk', (tester) async {
    await loginAs(['ap_clerk']);
    await tester.pumpWidget(_host());
    expect(find.byIcon(Icons.show_chart), findsNothing);
  });
}
