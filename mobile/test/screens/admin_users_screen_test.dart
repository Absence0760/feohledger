import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/admin_users_screen.dart';
import 'package:feohledger_mobile/stores/admin_user_store.dart';

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

http.Response _users(List<Map<String, dynamic>> items) =>
    _json({'items': items, 'total': items.length, 'page': 1});

http.Response _roles() => _json([
      {'id': 'r1', 'name': 'admin', 'is_system': true},
      {'id': 'r2', 'name': 'ap_manager', 'is_system': true},
      {'id': 'r3', 'name': 'ap_clerk', 'is_system': true},
      {'id': 'r4', 'name': 'cfo', 'is_system': true},
    ]);

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

MockClient _client(List<Map<String, dynamic>> users) => MockClient((req) async {
      if (req.url.path.endsWith('/admin/roles')) return _roles();
      return _users(users);
    });

Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 20 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    AdminUserStore.instance.reset();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  testWidgets('lists each org user once loaded', (tester) async {
    ApiClient().debugConfigure(
      client: _client([_userJson('1'), _userJson('2', active: false)]),
    );

    await tester.pumpWidget(_host(const AdminUsersScreen()));
    await _pumpUntil(tester, find.text('User 1'));

    expect(find.text('User 1'), findsOneWidget);
    expect(find.text('User 2'), findsOneWidget);
    // Deactivated users carry the Inactive badge.
    expect(find.text('Inactive'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('tapping a user opens the action sheet', (tester) async {
    ApiClient().debugConfigure(client: _client([_userJson('1')]));

    await tester.pumpWidget(_host(const AdminUsersScreen()));
    await _pumpUntil(tester, find.text('User 1'));

    await tester.tap(find.text('User 1'));
    await tester.pumpAndSettle();

    expect(find.text('Edit roles'), findsOneWidget);
    expect(find.text('Deactivate user'), findsOneWidget);
  });

  testWidgets('a meaningful error surfaces a Retry affordance', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => http.Response('boom', 500)),
    );

    await tester.pumpWidget(_host(const AdminUsersScreen()));
    await _pumpUntil(tester, find.text('Could not load users'));

    expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
  });

  testWidgets('editing roles PATCHes the chosen role set', (tester) async {
    Map<String, dynamic>? patchBody;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'PATCH') {
          patchBody = jsonDecode(req.body) as Map<String, dynamic>;
          return _json(_userJson('1', roles: ['ap_manager']));
        }
        if (req.url.path.endsWith('/admin/roles')) return _roles();
        return _users([_userJson('1')]);
      }),
    );

    await tester.pumpWidget(_host(const AdminUsersScreen()));
    await _pumpUntil(tester, find.text('User 1'));

    await tester.tap(find.text('User 1'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Edit roles'));
    await tester.pumpAndSettle();

    // Toggle ap_manager on (ap_clerk is already checked from the seed).
    await tester.ensureVisible(find.text('ap_manager'));
    await tester.tap(find.text('ap_manager'));
    await tester.pump();
    await tester.ensureVisible(find.widgetWithText(FilledButton, 'Apply'));
    await tester.tap(find.widgetWithText(FilledButton, 'Apply'));
    await tester.pumpAndSettle();

    expect(patchBody, isNotNull);
    expect(
      (patchBody!['role_names'] as List).toSet(),
      {'ap_clerk', 'ap_manager'},
    );
  });

  testWidgets('create-user flow POSTs the form and refreshes the list',
      (tester) async {
    Map<String, dynamic>? postBody;
    var created = false;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'POST' && req.url.path.endsWith('/admin/users')) {
          postBody = jsonDecode(req.body) as Map<String, dynamic>;
          created = true;
          return _json({
            ..._userJson('9', name: 'Ada', roles: ['ap_clerk']),
            'temporary_password': 'Temp-Pass-1234',
          }, 201);
        }
        if (req.url.path.endsWith('/admin/roles')) return _roles();
        // Before create: only User 1. After: User 1 + the new Ada.
        return _users(
          created ? [_userJson('1'), _userJson('9', name: 'Ada')] : [_userJson('1')],
        );
      }),
    );

    await tester.pumpWidget(_host(const AdminUsersScreen()));
    await _pumpUntil(tester, find.text('User 1'));

    // Open the create sheet.
    await tester.tap(find.widgetWithText(FloatingActionButton, 'Create user'));
    await tester.pumpAndSettle();
    expect(find.text('New user'), findsOneWidget);

    // Fill the form and pick a role.
    await tester.enterText(
        find.widgetWithText(TextFormField, 'Full name'), 'Ada Lovelace');
    await tester.enterText(
        find.widgetWithText(TextFormField, 'Email'), 'ada@example.com');
    // The role checkbox in the sheet (the same label also appears on a user
    // tile behind the modal, so scope to the CheckboxListTile).
    final clerkCheckbox = find.widgetWithText(CheckboxListTile, 'ap_clerk');
    await tester.ensureVisible(clerkCheckbox);
    await tester.tap(clerkCheckbox);
    await tester.pump();

    await tester.ensureVisible(find.widgetWithText(FilledButton, 'Create'));
    await tester.tap(find.widgetWithText(FilledButton, 'Create'));
    await tester.pumpAndSettle();

    // The body carried the validated form.
    expect(postBody, isNotNull);
    expect(postBody!['email'], 'ada@example.com');
    expect(postBody!['full_name'], 'Ada Lovelace');
    expect((postBody!['role_names'] as List), ['ap_clerk']);

    // The one-time temp password is surfaced for the admin to hand over.
    expect(find.text('Temp-Pass-1234'), findsOneWidget);
    await tester.tap(find.widgetWithText(FilledButton, 'Close'));
    await tester.pumpAndSettle();

    // The list refreshed and now shows the new user.
    expect(find.text('Ada 9'), findsOneWidget);
  });

  testWidgets('create-user validation rejects a bad email', (tester) async {
    var postCalls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'POST') postCalls++;
        if (req.url.path.endsWith('/admin/roles')) return _roles();
        return _users([_userJson('1')]);
      }),
    );

    await tester.pumpWidget(_host(const AdminUsersScreen()));
    await _pumpUntil(tester, find.text('User 1'));

    await tester.tap(find.widgetWithText(FloatingActionButton, 'Create user'));
    await tester.pumpAndSettle();

    await tester.enterText(
        find.widgetWithText(TextFormField, 'Full name'), 'Ada');
    await tester.enterText(
        find.widgetWithText(TextFormField, 'Email'), 'not-an-email');
    await tester.tap(find.widgetWithText(FilledButton, 'Create'));
    await tester.pumpAndSettle();

    // The form blocks submit — no POST went out, the error is shown, and the
    // sheet stays open.
    expect(postCalls, 0);
    expect(find.text('Enter a valid email address'), findsOneWidget);
    expect(find.text('New user'), findsOneWidget);
  });

  testWidgets('delete-user confirms then DELETEs and refreshes',
      (tester) async {
    var deleteCalls = 0;
    var deleted = false;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'DELETE') {
          deleteCalls++;
          deleted = true;
          expect(req.url.path, endsWith('/admin/users/2'));
          return http.Response('', 204);
        }
        if (req.url.path.endsWith('/admin/roles')) return _roles();
        return _users(
          deleted ? [_userJson('1')] : [_userJson('1'), _userJson('2')],
        );
      }),
    );

    await tester.pumpWidget(_host(const AdminUsersScreen()));
    await _pumpUntil(tester, find.text('User 2'));

    await tester.tap(find.text('User 2'));
    await tester.pumpAndSettle();
    expect(find.text('Delete user'), findsWidgets);

    // Tap the action-sheet delete tile.
    await tester.tap(find.widgetWithText(ListTile, 'Delete user'));
    await tester.pumpAndSettle();

    // Confirm dialog, then confirm.
    expect(find.text('Delete User 2?'), findsOneWidget);
    await tester.tap(find.widgetWithText(FilledButton, 'Delete user'));
    await tester.pumpAndSettle();

    expect(deleteCalls, 1);
    // The list refreshed; User 2 is gone.
    expect(find.text('User 2'), findsNothing);
    expect(find.text('User 1'), findsOneWidget);
  });
}
