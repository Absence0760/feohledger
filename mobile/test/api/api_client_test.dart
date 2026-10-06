// Regression coverage for issue #183: getList, patch and delete had no
// network timeout guard, so a connected-but-silent backend hung the awaited
// http call forever — the store's catch block (which drives the offline-cache
// fallback) never ran. get/post already had a `.timeout(...)` guard; this
// file proves getList/patch/delete now match that pattern.
//
// The 10s production timeout is shrunk via `ApiClient().debugConfigure(
// timeout: ...)` (a test-only seam — see api_client.dart) so these tests
// don't burn 10 real seconds per case.

// The `error logging` group at the bottom covers a second defect in the same
// file: get/post returned `_handleResponse(response)` unawaited from inside
// their try block, so the catch never ran for a 4xx/5xx.
//
import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';

void main() {
  setUp(() {
    // Reset to a clean session before every test; timeout defaults back to
    // the real 10s unless a test overrides it below.
    ApiClient().debugConfigure();
  });

  group('getList', () {
    test('times out instead of hanging forever on a silent network',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) => Completer<http.Response>().future),
        timeout: const Duration(milliseconds: 50),
      );

      await expectLater(
        ApiClient().getList('/things'),
        throwsA(isA<TimeoutException>()),
      );
    });

    test('a normal fast call still succeeds', () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => http.Response(
            jsonEncode({
              'items': [
                {'id': '1'},
                {'id': '2'},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          ),
        ),
        timeout: const Duration(milliseconds: 50),
      );

      final result = await ApiClient().getList('/things');

      expect(result, hasLength(2));
      expect(result.first['id'], '1');
    });
  });

  group('patch', () {
    test('times out instead of hanging forever on a silent network',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) => Completer<http.Response>().future),
        timeout: const Duration(milliseconds: 50),
      );

      await expectLater(
        ApiClient().patch('/things/1', {'name': 'updated'}),
        throwsA(isA<TimeoutException>()),
      );
    });

    test('a normal fast call still succeeds', () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => http.Response(
            jsonEncode({'id': '1', 'name': 'updated'}),
            200,
            headers: {'content-type': 'application/json'},
          ),
        ),
        timeout: const Duration(milliseconds: 50),
      );

      final result = await ApiClient().patch('/things/1', {'name': 'updated'});

      expect(result['name'], 'updated');
    });
  });

  group('delete', () {
    test('times out instead of hanging forever on a silent network',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) => Completer<http.Response>().future),
        timeout: const Duration(milliseconds: 50),
      );

      await expectLater(
        ApiClient().delete('/things/1'),
        throwsA(isA<TimeoutException>()),
      );
    });

    test('a normal fast call still succeeds', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('', 204)),
        timeout: const Duration(milliseconds: 50),
      );

      await expectLater(ApiClient().delete('/things/1'), completes);
    });
  });

  // `get` and `post` wrap their call in try/catch purely to log the failure
  // before rethrowing — but they returned `_handleResponse(response)` without
  // awaiting it, which unwinds the try and hands the still-pending future to
  // the caller. Every error _handleResponse raises (ApiException on 4xx/5xx,
  // a decode failure on a malformed body) therefore bypassed the catch, and
  // the diagnostic never fired for the exact responses it exists for. The
  // exception still reached the caller, so only the log was lost — which is
  // why nothing else caught it. Dart's `unawaited_return_in_try_block` lint
  // (new in the analyzer CI runs) flags the shape; these pin the behaviour.
  group('error logging', () {
    final logged = <String>[];
    late DebugPrintCallback originalDebugPrint;

    setUp(() {
      logged.clear();
      originalDebugPrint = debugPrint;
      debugPrint = (String? message, {int? wrapWidth}) {
        if (message != null) logged.add(message);
      };
    });

    tearDown(() {
      debugPrint = originalDebugPrint;
    });

    test('get logs the failure when the backend returns 500', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
        timeout: const Duration(milliseconds: 50),
      );

      await expectLater(
        ApiClient().get('/things'),
        throwsA(isA<ApiException>()),
      );
      expect(
        logged.any((m) => m.startsWith('[API] GET /things FAILED:')),
        isTrue,
        reason: 'the catch block never ran for the error response',
      );
    });

    test('post logs the failure when the backend returns 500', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
        timeout: const Duration(milliseconds: 50),
      );

      await expectLater(
        ApiClient().post('/things', {'name': 'x'}),
        throwsA(isA<ApiException>()),
      );
      expect(
        logged.any((m) => m.startsWith('[API] POST /things FAILED:')),
        isTrue,
        reason: 'the catch block never ran for the error response',
      );
    });
  });

  // A 401 used to tear down the whole device session on EVERY verb,
  // unconditionally — including the routes that legitimately 401 while no
  // credential exists yet (`/auth/login`, `/auth/mfa/verify`,
  // `/auth/mfa/challenge/email`). One mistyped MFA code therefore deleted the
  // stored `tenant_slug`; the user then entered the CORRECT code, the
  // control-plane-only auth routes succeeded, and every tenant-scoped request
  // afterwards went out with no `X-Tenant-Slug` and 400'd until they signed out
  // and back in. The always-reachable variant: one mistyped password silently
  // turned Face ID off and reset the display language, because all four keys
  // shared one FlutterSecureStorage and the teardown called `deleteAll()`.
  group('401 handling', () {
    const storage = FlutterSecureStorage();

    setUp(() {
      FlutterSecureStorage.setMockInitialValues({
        'biometric_enabled': 'true',
        'display_locale': 'de',
      });
    });

    http.Response unauthorized(http.BaseRequest _) => http.Response(
          jsonEncode({'detail': 'Invalid credentials'}),
          401,
          headers: {'content-type': 'application/json'},
        );

    test('a 401 on a request that carried NO token keeps the session state',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => unauthorized(req)),
        timeout: const Duration(milliseconds: 50),
      );
      // The tenant is chosen on the login form, BEFORE any token exists —
      // exactly the state a failed login / MFA verify runs in.
      await ApiClient().setTenant('acme');

      await expectLater(
        ApiClient().post('/auth/mfa/verify', {'code': '000000'}),
        throwsA(isA<ApiException>()),
      );

      expect(await storage.read(key: 'tenant_slug'), 'acme');
      expect(ApiClient().hasToken, isFalse);
      expect(await storage.read(key: 'biometric_enabled'), 'true');
      expect(await storage.read(key: 'display_locale'), 'de');
    });

    test('a 401 on an authenticated request still ends the session', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => unauthorized(req)),
        timeout: const Duration(milliseconds: 50),
      );
      await ApiClient().setTenant('acme');
      await ApiClient().setToken('expired-token');

      await expectLater(
        ApiClient().get('/invoices'),
        throwsA(isA<ApiException>()),
      );

      expect(ApiClient().hasToken, isFalse);
      expect(await storage.read(key: 'auth_token'), isNull);
      expect(await storage.read(key: 'tenant_slug'), isNull);
    });

    test('ending a session keeps the device preferences', () async {
      ApiClient().debugConfigure(timeout: const Duration(milliseconds: 50));
      await ApiClient().setTenant('acme');
      await ApiClient().setToken('tok');

      await ApiClient().clearSession();

      expect(await storage.read(key: 'auth_token'), isNull);
      expect(await storage.read(key: 'tenant_slug'), isNull);
      // Biometric unlock + display language are DEVICE preferences, not session
      // state — `deleteAll()` used to take them with it.
      expect(await storage.read(key: 'biometric_enabled'), 'true');
      expect(await storage.read(key: 'display_locale'), 'de');
    });

    test('every verb honours the no-token rule', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => unauthorized(req)),
        timeout: const Duration(milliseconds: 50),
      );
      await ApiClient().setTenant('acme');

      final calls = <Future<void>>[
        ApiClient().get('/a'),
        ApiClient().getList('/b'),
        ApiClient().patch('/c'),
        ApiClient().delete('/d'),
        ApiClient().getBytes('/e'),
        ApiClient().postBytes('/f'),
      ];
      for (final call in calls) {
        await expectLater(call, throwsA(isA<ApiException>()));
      }

      expect(await storage.read(key: 'tenant_slug'), 'acme');
    });
  });

  // A refusal is routine on the money path (the payment-run CFO gate answers
  // 403), and the app pastes the exception's message straight into a snackbar.
  // Echoing the raw response body put `{"detail":"..."}` on screen.
  group('error messages', () {
    test('extracts FastAPI\'s `detail` sentence', () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => http.Response(
            jsonEncode({'detail': 'This run exceeds the CFO threshold.'}),
            403,
            headers: {'content-type': 'application/json'},
          ),
        ),
        timeout: const Duration(milliseconds: 50),
      );

      try {
        await ApiClient().post('/payments/runs/1/execute');
        fail('expected an ApiException');
      } on ApiException catch (e) {
        expect(e.statusCode, 403);
        expect(e.message, 'This run exceeds the CFO threshold.');
        expect(describeApiError(e), 'This run exceeds the CFO threshold.');
      }
    });

    test('joins a 422 validation `detail` list', () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => http.Response(
            jsonEncode({
              'detail': [
                {'loc': ['body', 'amount'], 'msg': 'field required'},
                {'loc': ['body', 'method'], 'msg': 'unknown method'},
              ],
            }),
            422,
            headers: {'content-type': 'application/json'},
          ),
        ),
        timeout: const Duration(milliseconds: 50),
      );

      try {
        await ApiClient().post('/things');
        fail('expected an ApiException');
      } on ApiException catch (e) {
        expect(e.message, 'field required; unknown method');
      }
    });

    test('falls back to a status line for a non-JSON body', () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => http.Response('<html>502 Bad Gateway</html>', 502),
        ),
        timeout: const Duration(milliseconds: 50),
      );

      try {
        await ApiClient().post('/things');
        fail('expected an ApiException');
      } on ApiException catch (e) {
        expect(e.message, 'Request failed (502)');
        expect(e.detail, isNull);
      }
    });

    test(
        'a structured `detail` yields its `message` and keeps the structure '
        'on the exception', () async {
      // The GL-chart refusal: a stable code + params for the screen to state
      // in the reader's language, and the English `message` as the fallback.
      final detail = {
        'code': 'gl_codes_outside_chart',
        'on_lines': false,
        'foreign': ['6000'],
        'retired': <String>[],
        'unknown': <String>[],
        'message': "GL account '6000' belongs to another entity's chart.",
      };
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => http.Response(
            jsonEncode({'detail': detail}),
            422,
            headers: {'content-type': 'application/json'},
          ),
        ),
        timeout: const Duration(milliseconds: 50),
      );

      try {
        await ApiClient().patch('/things', {'gl_account': '6000'});
        fail('expected an ApiException');
      } on ApiException catch (e) {
        expect(
          e.message,
          "GL account '6000' belongs to another entity's chart.",
        );
        expect(e.detail, detail);
      }
    });
  });
}
