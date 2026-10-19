// PushService.registerWithBackend — the sign-in retry for the device
// registration that push init attempts before any session exists. Without it
// a device only reached the backend if its FCM identifier changed while the
// user was signed in, because `onRegistered` fires on change, not per launch.

import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/services/push_service.dart';

void main() {
  late List<http.Request> requests;

  setUp(() {
    requests = [];
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        requests.add(req);
        return http.Response(
          jsonEncode({'platform': 'android', 'updated_at': '2026-01-01T00:00:00Z'}),
          200,
          headers: {'content-type': 'application/json'},
        );
      }),
    );
  });

  tearDown(() {
    PushService.instance.debugSetRegistration(null);
    ApiClient().debugConfigure();
  });

  test('sends the current FCM registration to the device-token endpoint', () async {
    PushService.instance.debugSetRegistration('fid-123');

    await PushService.instance.registerWithBackend();

    expect(requests, hasLength(1));
    expect(requests.single.method, 'POST');
    expect(requests.single.url.path, endsWith('/notifications/device-token'));
    final body = jsonDecode(requests.single.body) as Map<String, dynamic>;
    expect(body['token'], 'fid-123');
  });

  test('sends nothing before FCM has issued a registration', () async {
    await PushService.instance.registerWithBackend();

    expect(requests, isEmpty);
  });

  test('a backend refusal is logged, never thrown into the auth gate', () async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => http.Response('{"detail":"no"}', 401)),
    );
    PushService.instance.debugSetRegistration('fid-123');

    await expectLater(PushService.instance.registerWithBackend(), completes);
  });
}
