import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/stores/admin_user_store.dart';

http.Response _users(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'items': items, 'total': items.length, 'page': 1}),
      200,
      headers: {'content-type': 'application/json'},
    );

http.Response _roles() => http.Response(
      jsonEncode([
        {'id': 'r1', 'name': 'admin', 'is_system': true},
        {'id': 'r2', 'name': 'ap_manager', 'is_system': true},
        {'id': 'r3', 'name': 'ap_clerk', 'is_system': true},
        {'id': 'r4', 'name': 'cfo', 'is_system': true},
        {'id': 'r5', 'name': 'auditor', 'is_system': false},
      ]),
      200,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _userJson(
  String id, {
  String name = 'User',
  bool active = true,
  List<String> roles = const ['ap_clerk'],
}) =>
    {
      'id': id,
      'email': 'u$id@example.com',
      'full_name': '$name $id',
      'is_active': active,
      'roles': roles.map((r) => {'id': 'role-$r', 'name': r}).toList(),
      'created_at': '2026-01-01T12:00:00',
    };

/// Route a request to the users / roles / patch handlers based on path + method.
MockClient _client({
  required List<Map<String, dynamic>> users,
  void Function(http.Request req)? onPatch,
}) {
  return MockClient((req) async {
    if (req.method == 'PATCH') {
      onPatch?.call(req);
      // Echo back a plausible updated user.
      return http.Response(
        jsonEncode(_userJson('1')),
        200,
        headers: {'content-type': 'application/json'},
      );
    }
    if (req.url.path.endsWith('/admin/roles')) return _roles();
    return _users(users);
  });
}

void main() {
  final store = AdminUserStore.instance;

  setUp(() {
    store.reset();
    ApiClient().debugConfigure();
  });

  group('fetch', () {
    test('loads users + roles and exposes the system role names', () async {
      ApiClient().debugConfigure(
        client: _client(users: [_userJson('1'), _userJson('2')]),
      );

      await store.fetch();

      expect(store.users, hasLength(2));
      expect(store.users.first.email, 'u1@example.com');
      expect(store.roles, hasLength(5));
      // Only the four system roles are offered in the editor (custom roles
      // confer no access today).
      expect(store.systemRoleNames,
          containsAll(['admin', 'ap_manager', 'ap_clerk', 'cfo']));
      expect(store.systemRoleNames, isNot(contains('auditor')));
      expect(store.error, isNull);
    });

    test('records the error on failure', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      await store.fetch();

      expect(store.users, isEmpty);
      expect(store.error, isNotNull);
    });

    test('setSearch carries the query into the users request', () async {
      final sent = Completer<String?>();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path.endsWith('/admin/roles')) return _roles();
          if (!sent.isCompleted) {
            sent.complete(req.url.queryParameters['search']);
          }
          return _users([]);
        }),
      );

      store.setSearch('alice');

      expect(store.searchQuery, 'alice');
      expect(await sent.future, 'alice');
    });
  });

  group('mutations', () {
    test('setRoles PATCHes role_names and refetches', () async {
      Map<String, dynamic>? body;
      var listCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'PATCH') {
            body = jsonDecode(req.body) as Map<String, dynamic>;
            return http.Response(
              jsonEncode(_userJson('1', roles: ['ap_manager'])),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (req.url.path.endsWith('/admin/roles')) return _roles();
          listCalls++;
          return _users([_userJson('1', roles: ['ap_manager'])]);
        }),
      );

      final ok = await store.setRoles('1', ['ap_manager']);

      expect(ok, isTrue);
      expect(body!['role_names'], ['ap_manager']);
      expect(listCalls, 1, reason: 'success triggers a refetch');
    });

    test('setActive PATCHes is_active', () async {
      Map<String, dynamic>? body;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'PATCH') {
            body = jsonDecode(req.body) as Map<String, dynamic>;
            return http.Response(
              jsonEncode(_userJson('1', active: false)),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (req.url.path.endsWith('/admin/roles')) return _roles();
          return _users([_userJson('1', active: false)]);
        }),
      );

      final ok = await store.setActive('1', false);

      expect(ok, isTrue);
      expect(body!['is_active'], false);
    });

    test('mutation failure returns false + records the error', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('nope', 403)),
      );

      final ok = await store.setActive('1', false);

      expect(ok, isFalse);
      expect(store.error, isNotNull);
    });
  });

  group('createUser', () {
    test('POSTs the body, returns the link expiry, and refreshes', () async {
      Map<String, dynamic>? body;
      var listCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/admin/users')) {
            body = jsonDecode(req.body) as Map<String, dynamic>;
            return http.Response(
              jsonEncode({
                ..._userJson('9', name: 'Ada', roles: ['ap_clerk']),
                'set_password_link_expires_at': '2026-11-20T12:00:00Z',
              }),
              201,
              headers: {'content-type': 'application/json'},
            );
          }
          if (req.url.path.endsWith('/admin/roles')) return _roles();
          listCalls++;
          // The refetch should see the new user.
          return _users([_userJson('9', name: 'Ada')]);
        }),
      );

      final result = await store.createUser(
        email: 'ada@example.com',
        fullName: 'Ada Lovelace',
        roleNames: ['ap_clerk'],
      );

      expect(result, isNotNull);
      expect(result!.setPasswordLinkExpiresAt, '2026-11-20T12:00:00Z');
      expect(body!['email'], 'ada@example.com');
      expect(body!['full_name'], 'Ada Lovelace');
      expect(body!['role_names'], ['ap_clerk']);
      // The list was refetched and now carries the new user.
      expect(listCalls, 1, reason: 'success triggers a refetch');
      expect(store.users.any((u) => u.fullName == 'Ada 9'), isTrue);
      expect(store.error, isNull);
    });

    test('returns null + records the error on a 409 (email in use)', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST') {
            return http.Response('{"detail":"Email already in use"}', 409);
          }
          return _users([]);
        }),
      );

      final result = await store.createUser(
        email: 'dup@example.com',
        fullName: 'Dup',
        roleNames: const [],
      );

      expect(result, isNull);
      expect(store.error, isNotNull);
      expect(store.error, contains('Email already in use'));
    });
  });

  group('deleteUser', () {
    test('DELETEs the user and refreshes the list', () async {
      var deleteCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'DELETE') {
            deleteCalls++;
            expect(req.url.path, endsWith('/admin/users/7'));
            return http.Response('', 204);
          }
          if (req.url.path.endsWith('/admin/roles')) return _roles();
          // After delete, user 7 is gone.
          return _users([_userJson('1')]);
        }),
      );

      final ok = await store.deleteUser('7');

      expect(ok, isTrue);
      expect(deleteCalls, 1);
      expect(store.users.any((u) => u.id == '7'), isFalse);
      expect(store.users.any((u) => u.id == '1'), isTrue);
    });

    test('returns false + records the error on a 409 (self / referenced)',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'DELETE') {
            return http.Response('{"detail":"Cannot delete yourself"}', 409);
          }
          return _users([]);
        }),
      );

      final ok = await store.deleteUser('me');

      expect(ok, isFalse);
      expect(store.error, isNotNull);
      expect(store.error, contains('Cannot delete yourself'));
    });
  });
}
