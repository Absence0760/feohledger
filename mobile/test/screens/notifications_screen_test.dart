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

  testWidgets('renders the error state with a Retry on network failure',
      (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => throw Exception('offline')),
    );

    await tester.pumpWidget(_localized(const NotificationsScreen()));
    await _pumpUntil(tester, find.text("Couldn't load notifications"));

    expect(find.text("Couldn't load notifications"), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
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
