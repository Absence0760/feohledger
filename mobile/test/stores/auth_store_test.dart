import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _meBody(List<String> roles) => {
      'id': 'u1',
      'email': 'demo@acme.com',
      'full_name': 'Demo User',
      'organization_id': 'org1',
      'roles': roles,
    };

/// A MockClient that logs a user in with the given [roles] and 200s on logout.
MockClient _happyClient(List<String> roles) {
  return MockClient((req) async {
    final path = req.url.path;
    if (req.method == 'POST' && path == '/api/auth/login') {
      return _json({'access_token': 'tok-123'});
    }
    if (req.method == 'GET' && path == '/api/auth/me') {
      return _json(_meBody(roles));
    }
    if (req.method == 'POST' && path == '/api/auth/logout') {
      return _json({});
    }
    return http.Response('not found', 404);
  });
}

void main() {
  final store = AuthStore.instance;

  setUp(() {
    FlutterSecureStorage.setMockInitialValues({});
  });

  Future<void> loginAs(List<String> roles) async {
    ApiClient().debugConfigure(client: _happyClient(roles));
    final result = await store.login('demo@acme.com', 'demo', 'acme');
    expect(result.isSuccess, isTrue);
  }

  group('login', () {
    test('success populates the user and clears loading/error', () async {
      await loginAs(['admin']);

      expect(store.loggedIn, isTrue);
      expect(store.user?.email, 'demo@acme.com');
      expect(store.loading, isFalse);
      expect(store.error, isNull);
    });

    test('persists the token on the ApiClient', () async {
      await loginAs(['ap_clerk']);
      expect(ApiClient().hasToken, isTrue);
    });

    test('invalid credentials (4xx) surface a friendly error', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('bad creds', 401)),
      );

      final result = await store.login('demo@acme.com', 'wrong', 'acme');

      expect(result.isSuccess, isFalse);
      expect(result.outcome, LoginOutcome.failure);
      expect(store.error, 'Invalid credentials');
      expect(store.loading, isFalse);
    });

    test('a transport failure surfaces a connection error', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('network down')),
      );

      final result = await store.login('demo@acme.com', 'demo', 'acme');

      expect(result.isSuccess, isFalse);
      expect(store.error, startsWith('Connection failed'));
    });

    test('notifies listeners on the loading->done transition', () async {
      ApiClient().debugConfigure(client: _happyClient(['admin']));
      var notifications = 0;
      void listener() => notifications++;
      store.addListener(listener);

      await store.login('demo@acme.com', 'demo', 'acme');
      store.removeListener(listener);

      // at least: loading=true notify, then success notify
      expect(notifications, greaterThanOrEqualTo(2));
    });
  });

  group('role gating', () {
    test('admin can approve and view payments', () async {
      await loginAs(['admin']);
      expect(store.canApprove, isTrue);
      expect(store.canViewPayments, isTrue);
    });

    test('ap_manager can approve and view payments', () async {
      await loginAs(['ap_manager']);
      expect(store.canApprove, isTrue);
      expect(store.canViewPayments, isTrue);
    });

    test('cfo can view payments but cannot approve', () async {
      await loginAs(['cfo']);
      expect(store.canApprove, isFalse);
      expect(store.canViewPayments, isTrue);
    });

    test('ap_clerk can neither approve nor view payments', () async {
      await loginAs(['ap_clerk']);
      expect(store.canApprove, isFalse);
      expect(store.canViewPayments, isFalse);
      expect(store.isClerkOnly, isTrue);
    });

    test('ap_clerk enters invoices only before approval '
        '(mirrors backend api/invoice_entry.py)', () async {
      Invoice inv(String status, {String? approvedBy}) => Invoice.fromJson({
            'id': 'i1',
            'status': status,
            'approved_by': approvedBy,
            'created_at': '2026-01-01T12:00:00',
          });

      await loginAs(['ap_clerk']);
      expect(store.canEditInvoice, isFalse);
      expect(store.canEnterInvoice, isTrue);
      expect(store.canApproveInvoice, isFalse);
      expect(store.canEditInvoiceRow(inv('new')), isTrue);
      expect(store.canEditInvoiceRow(inv('rejected')), isTrue);
      expect(store.canEditInvoiceRow(inv('failed')), isTrue);
      expect(
          store.canEditInvoiceRow(inv('approved', approvedBy: 'A')), isFalse);
      expect(store.canEditInvoiceRow(inv('failed', approvedBy: 'A')), isFalse);
      expect(store.canEditInvoiceRow(inv('paid', approvedBy: 'A')), isFalse);

      await loginAs(['ap_manager']);
      expect(store.canEditInvoiceRow(inv('approved', approvedBy: 'A')), isTrue);
      expect(store.canEditInvoiceRow(inv('paid', approvedBy: 'A')), isFalse);
    });

    test('cash flow forecast is admin/cfo only (mirrors backend _CFO_ROLES)',
        () async {
      await loginAs(['admin']);
      expect(store.canViewCashFlow, isTrue);
      await loginAs(['cfo']);
      expect(store.canViewCashFlow, isTrue);
      // ap_manager is deliberately excluded — it's a privileged CFO surface.
      await loginAs(['ap_manager']);
      expect(store.canViewCashFlow, isFalse);
      await loginAs(['ap_clerk']);
      expect(store.canViewCashFlow, isFalse);
    });
  });

  group('logout', () {
    test('clears the user', () async {
      await loginAs(['admin']);
      expect(store.loggedIn, isTrue);

      await store.logout();

      expect(store.loggedIn, isFalse);
      expect(store.user, isNull);
    });
  });

  group('MFA challenge login', () {
    test('login returns mfaRequired (no token, no user) on a challenge',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path == '/api/auth/login') {
            return _json({
              'mfa_required': true,
              'mfa_challenge_token': 'chal-abc',
              'methods': ['totp', 'email'],
              'must_enroll': false,
            });
          }
          // /me must NOT be called before the second factor clears.
          return http.Response('unexpected ${req.url.path}', 500);
        }),
      );

      final result = await store.login('demo@acme.com', 'demo', 'acme');

      expect(result.outcome, LoginOutcome.mfaRequired);
      expect(result.challenge, isNotNull);
      expect(result.challenge!.challengeToken, 'chal-abc');
      expect(result.challenge!.supportsTotp, isTrue);
      expect(result.challenge!.supportsEmail, isTrue);
      // Critically: no token stored, not yet logged in.
      expect(ApiClient().hasToken, isFalse);
      expect(store.loggedIn, isFalse);
      expect(store.loading, isFalse);
    });

    test('completeMfa trades the code for a token and loads the user',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          final path = req.url.path;
          if (req.method == 'POST' && path == '/api/auth/mfa/verify') {
            return _json({'access_token': 'real-tok'});
          }
          if (req.method == 'GET' && path == '/api/auth/me') {
            return _json(_meBody(['admin']));
          }
          return http.Response('not found', 404);
        }),
      );
      await ApiClient().setTenant('acme');

      final result = await store.completeMfa(
        challengeToken: 'chal-abc',
        code: '123456',
        method: 'totp',
      );

      expect(result.isSuccess, isTrue);
      expect(store.loggedIn, isTrue);
      expect(store.user?.email, 'demo@acme.com');
      expect(ApiClient().hasToken, isTrue);
      expect(store.error, isNull);
    });

    test('completeMfa surfaces a friendly error on a bad/expired code (401)',
        () async {
      // AuthStore is a singleton; clear any user a prior test left behind so
      // the `loggedIn == false` assertion reflects THIS flow, not leaked state.
      await store.logout();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path == '/api/auth/mfa/verify') {
            return http.Response('invalid', 401);
          }
          return http.Response('not found', 404);
        }),
      );
      await ApiClient().setTenant('acme');

      final result = await store.completeMfa(
        challengeToken: 'chal-abc',
        code: '000000',
        method: 'totp',
      );

      expect(result.isSuccess, isFalse);
      expect(result.outcome, LoginOutcome.failure);
      expect(store.error, contains('Invalid or expired code'));
      expect(store.loggedIn, isFalse);
      expect(ApiClient().hasToken, isFalse);
    });

    test('requestEmailOtp posts the challenge token and returns true on 204',
        () async {
      var hit = false;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path == '/api/auth/mfa/challenge/email') {
            hit = true;
            return http.Response('', 204);
          }
          return http.Response('not found', 404);
        }),
      );
      await ApiClient().setTenant('acme');

      final ok = await store.requestEmailOtp('chal-abc');

      expect(ok, isTrue);
      expect(hit, isTrue);
    });
  });

  group('init (session restore)', () {
    // Restoring a session must distinguish "the token was rejected" from
    // "the network is down". Only the former may tear the session down —
    // tearing down on a transport failure would wipe the offline cache at
    // exactly the moment offline mode is supposed to serve it (issue #176).
    setUp(() {
      FlutterSecureStorage.setMockInitialValues({
        'auth_token': 'stored-token',
        'tenant_slug': 'acme',
      });
      OfflineStore.instance.debugUseMemory(
        tenantSlug: 'acme',
        userId: 'user-a',
      );
    });

    test('restores the user when the stored token is still good', () async {
      ApiClient().debugConfigure(client: _happyClient(['admin']));

      final restored = await store.init();

      expect(restored, isTrue);
      expect(store.user?.email, 'demo@acme.com');
      expect(OfflineStore.instance.hasScope, isTrue);
    });

    test('a 401 tears the session down', () async {
      await OfflineStore.instance.put('dashboard', {'total_invoices': 7});
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('nope', 401)),
      );

      final restored = await store.init();

      expect(restored, isFalse);
      expect(ApiClient().hasToken, isFalse);
      expect(store.loggedIn, isFalse);
      expect(OfflineStore.instance.hasScope, isFalse);
    });

    test('a transport failure keeps the token AND the offline cache', () async {
      await OfflineStore.instance.put('dashboard', {'total_invoices': 7});
      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );

      final restored = await store.init();

      expect(restored, isFalse);
      expect(
        ApiClient().hasToken,
        isTrue,
        reason: 'being offline says nothing about the token',
      );
      final cached = await OfflineStore.instance.get('dashboard');
      expect(
        (cached as Map)['total_invoices'],
        7,
        reason: 'a network blip must not destroy the offline cache',
      );
    });
  });
}
