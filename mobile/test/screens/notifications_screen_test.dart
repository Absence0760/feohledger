import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/notifications_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/notification_store.dart';
import 'package:feohledger_mobile/widgets/async_list_body.dart';
import 'package:feohledger_mobile/widgets/notification_list_tile.dart';

Widget _localized(Widget home) => MaterialApp(
      localizationsDelegates: appLocalizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: home,
    );

http.Response _page(
  List<Map<String, dynamic>> items, {
  int? unread,
}) =>
    http.Response(
      jsonEncode({
        'items': items,
        'total': items.length,
        'unread': unread ?? items.where((i) => i['read_at'] == null).length,
        'page': 1,
        'page_size': 20,
      }),
      200,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _notificationJson(
  String id, {
  String title = 'Invoice approved',
  String entityType = 'invoice',
  String? entityId = 'inv-1',
  bool read = false,
}) =>
    {
      'id': id,
      'event_type': 'invoice_approved',
      'entity_type': entityType,
      'entity_id': entityId,
      'title': title,
      'body': 'INV-$id was approved',
      'read_at': read ? '2026-01-02T10:00:00Z' : null,
      'created_at': '2026-01-01T12:00:00Z',
    };

Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 20 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

Future<void> _pumpUntilTrue(
  WidgetTester tester,
  bool Function() condition,
) async {
  for (var i = 0; i < 20 && !condition(); i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  final store = NotificationStore.instance;

  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    NotificationStore.instance.reset();
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  testWidgets('renders one tile per notification once loaded', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _page([
            _notificationJson('1', title: 'First notification'),
            _notificationJson('2', title: 'Second notification'),
          ])),
    );

    await tester.pumpWidget(_localized(const NotificationsScreen()));
    await _pumpUntil(tester, find.byType(NotificationListTile));

    expect(find.byType(NotificationListTile), findsNWidgets(2));
    expect(find.text('First notification'), findsOneWidget);
    expect(find.text('Second notification'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('renders the empty state when there are no notifications',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _page([])),
    );

    await tester.pumpWidget(_localized(const NotificationsScreen()));
    await _pumpUntil(tester, find.text('No notifications'));

    expect(find.text('No notifications'), findsOneWidget);
    expect(find.byType(NotificationListTile), findsNothing);
  });

  testWidgets('a failed load shows Retry, never the empty copy', (
    tester,
  ) async {
    var calls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        calls++;
        if (calls == 1) throw http.ClientException('offline');
        return _page([_notificationJson('1')]);
      }),
    );

    await tester.pumpWidget(_localized(const NotificationsScreen()));
    await _pumpUntil(tester, find.widgetWithText(FilledButton, 'Retry'));

    // No server answered, so the connection sentence — and never "No
    // notifications", which would claim there is nothing to read.
    expect(
      find.text(
        "Couldn't reach FeohLedger. Check your connection and tap Retry.",
      ),
      findsOneWidget,
    );
    expect(find.text('No notifications'), findsNothing);

    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await _pumpUntil(tester, find.byType(NotificationListTile));
    expect(find.byType(NotificationListTile), findsOneWidget);
  });

  testWidgets('a server refusal is worded with its reason', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => http.Response(
          jsonEncode({'detail': 'Not permitted'}),
          403,
          headers: {'content-type': 'application/json'},
        ),
      ),
    );

    await tester.pumpWidget(_localized(const NotificationsScreen()));
    await _pumpUntil(tester, find.widgetWithText(FilledButton, 'Retry'));

    expect(
      find.text("Couldn't load notifications: Not permitted"),
      findsOneWidget,
    );
  });

  testWidgets('the empty state can be pulled to refresh', (tester) async {
    var calls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        calls++;
        return _page([]);
      }),
    );

    await tester.pumpWidget(_localized(const NotificationsScreen()));
    await _pumpUntil(tester, find.text('No notifications'));
    expect(calls, 1);

    await tester.fling(
      find.text('No notifications'),
      const Offset(0, 300),
      1000,
    );
    await tester.pumpAndSettle();
    await _pumpUntilTrue(tester, () => calls >= 2);

    expect(calls, 2);
  });

  testWidgets('a failed mark-read over a loaded list raises no stale banner', (
    tester,
  ) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'POST') {
          return http.Response(
            jsonEncode({'detail': 'Not permitted'}),
            403,
            headers: {'content-type': 'application/json'},
          );
        }
        return _page([_notificationJson('1')]);
      }),
    );

    await tester.pumpWidget(_localized(const NotificationsScreen()));
    await _pumpUntil(tester, find.byType(NotificationListTile));

    bool? ok;
    store.markAllRead().then((v) => ok = v);
    await _pumpUntilTrue(tester, () => ok != null);

    expect(ok, isFalse);
    expect(store.actionError, 'Not permitted');
    expect(store.loadError, isNull);
    // The rows are live: the refusal is the action's, not the list's.
    expect(find.byType(StaleDataBanner), findsNothing);
    expect(find.byType(NotificationListTile), findsOneWidget);
  });

  testWidgets('the Unread chip filtering every row out says so, and Show all '
      'clears it', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => req.url.queryParameters['unread_only'] == 'true'
            ? _page([])
            : _page([_notificationJson('1', read: true)]),
      ),
    );

    await tester.pumpWidget(_localized(const NotificationsScreen()));
    await _pumpUntil(tester, find.byType(NotificationListTile));

    await tester.tap(find.widgetWithText(FilterChip, 'Unread'));
    await _pumpUntil(tester, find.text('No unread notifications'));

    expect(find.text('No unread notifications'), findsOneWidget);
    // Not the whole-inbox copy: read notifications exist.
    expect(find.text('No notifications'), findsNothing);

    await tester.tap(find.widgetWithText(OutlinedButton, 'Show all'));
    await _pumpUntil(tester, find.byType(NotificationListTile));

    expect(store.unreadOnly, isFalse);
    expect(find.byType(NotificationListTile), findsOneWidget);
  });

  testWidgets('the All / Unread filter chips narrow the request',
      (tester) async {
    String? lastUnreadOnly;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        lastUnreadOnly = req.url.queryParameters['unread_only'];
        return _page([]);
      }),
    );

    await tester.pumpWidget(_localized(const NotificationsScreen()));
    await _pumpUntil(tester, find.text('No notifications'));

    expect(find.widgetWithText(FilterChip, 'All'), findsOneWidget);
    expect(find.widgetWithText(FilterChip, 'Unread'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilterChip, 'Unread'));
    await tester.pump(const Duration(milliseconds: 50));

    expect(store.unreadOnly, isTrue);
    expect(lastUnreadOnly, 'true');
  });

  testWidgets('tapping a notification marks it read', (tester) async {
    var readPosts = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'POST' && req.url.path.endsWith('/read')) {
          readPosts++;
          return http.Response(
            jsonEncode({'id': '1', 'read_at': '2026-01-02T10:00:00Z'}),
            200,
            headers: {'content-type': 'application/json'},
          );
        }
        return _page([_notificationJson('1', entityType: 'contract',
            entityId: null)], unread: 1);
      }),
    );

    await tester.pumpWidget(_localized(const NotificationsScreen()));
    await _pumpUntil(tester, find.byType(NotificationListTile));
    expect(store.unread, 1);

    // contract row → no navigation, just mark read.
    await tester.tap(find.byType(NotificationListTile).first);
    await _pumpUntilTrue(tester, () => readPosts >= 1);

    expect(readPosts, 1);
    expect(store.notifications.first.isRead, isTrue);
    expect(store.unread, 0);
  });

  testWidgets('mark-all-read action shows only when unread and clears the badge',
      (tester) async {
    var readAllPosts = 0;
    var allRead = false;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'POST' && req.url.path.endsWith('/read-all')) {
          readAllPosts++;
          allRead = true;
          return http.Response(jsonEncode({'updated': 1}), 200,
              headers: {'content-type': 'application/json'});
        }
        return _page([_notificationJson('1', read: allRead)],
            unread: allRead ? 0 : 1);
      }),
    );

    await tester.pumpWidget(_localized(const NotificationsScreen()));
    await _pumpUntil(tester, find.byType(NotificationListTile));

    // The mark-all-read action is present while there's an unread row.
    expect(find.byTooltip('Mark all read'), findsOneWidget);

    await tester.tap(find.byTooltip('Mark all read'));
    await _pumpUntilTrue(tester, () => readAllPosts >= 1 && store.unread == 0);

    expect(readAllPosts, 1);
    expect(store.unread, 0);
    // With nothing unread, the action disappears.
    await tester.pump();
    expect(find.byTooltip('Mark all read'), findsNothing);
  });
}
