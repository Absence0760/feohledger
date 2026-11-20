import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/notification_store.dart';

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
  String eventType = 'invoice_approved',
  String entityType = 'invoice',
  String? entityId = 'inv-1',
  String title = 'Invoice approved',
  String? body = 'INV-001 was approved',
  bool read = false,
}) =>
    {
      'id': id,
      'event_type': eventType,
      'entity_type': entityType,
      'entity_id': entityId,
      'title': title,
      'body': body,
      'read_at': read ? '2026-01-02T10:00:00Z' : null,
      'created_at': '2026-01-01T12:00:00Z',
    };

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

  group('fetch', () {
    test('success populates notifications + unread and marks them live',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _page([
              _notificationJson('1'),
              _notificationJson('2', read: true),
            ], unread: 1)),
      );

      await store.fetch();

      expect(store.notifications, hasLength(2));
      expect(store.notifications.first.id, '1');
      expect(store.notifications.first.isRead, isFalse);
      expect(store.notifications[1].isRead, isTrue);
      expect(store.unread, 1);
      expect(store.fromCache, isFalse);
      expect(store.loadError, isNull);
      expect(store.loading, isFalse);
    });

    test('falls back to the offline cache when the network fails', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _page([_notificationJson('1')])),
      );
      await store.fetch();
      expect(store.fromCache, isFalse);

      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.fetch();

      expect(store.notifications, hasLength(1));
      expect(store.notifications.first.id, '1');
      expect(store.fromCache, isTrue);
      // Unread recomputed from the cached rows.
      expect(store.unread, 1);
    });

    test('surfaces an error when the network fails and no cache exists',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );

      await store.fetch();

      expect(store.loadError, isNotNull);
      expect(store.fromCache, isFalse);
      expect(store.notifications, isEmpty);
    });
  });

  group('filters', () {
    test('setUnreadOnly carries unread_only into the request', () async {
      final sentUnreadOnly = Completer<String?>();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (!sentUnreadOnly.isCompleted) {
            sentUnreadOnly.complete(req.url.queryParameters['unread_only']);
          }
          return _page([]);
        }),
      );

      store.setUnreadOnly(true);

      expect(store.unreadOnly, isTrue);
      expect(await sentUnreadOnly.future, 'true');
    });
  });

  group('refreshUnreadCount', () {
    test('updates the badge from the cheap endpoint', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          expect(req.url.path, endsWith('/notifications/unread-count'));
          return http.Response(jsonEncode({'unread': 4}), 200,
              headers: {'content-type': 'application/json'});
        }),
      );

      await store.refreshUnreadCount();

      expect(store.unread, 4);
    });

    test('keeps the last known count on failure', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _page([_notificationJson('1')])),
      );
      await store.fetch();
      expect(store.unread, 1);

      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.refreshUnreadCount();

      expect(store.unread, 1, reason: 'stale count survives a failed refresh');
    });
  });

  group('markRead', () {
    test('optimistically flips the row + decrements unread, then POSTs',
        () async {
      var postCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/read')) {
            postCalls++;
            return http.Response(
              jsonEncode({'id': '1', 'read_at': '2026-01-02T10:00:00Z'}),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return _page([_notificationJson('1')], unread: 1);
        }),
      );
      await store.fetch();
      expect(store.unread, 1);
      expect(store.notifications.first.isRead, isFalse);

      final ok = await store.markRead('1');

      expect(ok, isTrue);
      expect(postCalls, 1);
      expect(store.notifications.first.isRead, isTrue);
      expect(store.unread, 0);
    });

    test('re-marking an already-read row is a no-op (idempotent)', () async {
      var postCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/read')) {
            postCalls++;
            return http.Response('{}', 200);
          }
          return _page([_notificationJson('1', read: true)], unread: 0);
        }),
      );
      await store.fetch();

      final ok = await store.markRead('1');

      expect(ok, isTrue);
      expect(postCalls, 0, reason: 'no request for an already-read row');
      expect(store.unread, 0);
    });

    test('reconciles via refetch and returns false when the POST fails',
        () async {
      var listCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/read')) {
            return http.Response('boom', 500);
          }
          listCalls++;
          return _page([_notificationJson('1')], unread: 1);
        }),
      );
      await store.fetch();
      expect(listCalls, 1);

      final ok = await store.markRead('1');

      expect(ok, isFalse);
      expect(store.actionError, isNotNull);
      // The reconciling refetch succeeded: the list is live, so nothing may
      // read the refused mark-read as a failed refresh.
      expect(store.loadError, isNull);
      expect(listCalls, 2, reason: 'a failed mark-read refetches to reconcile');
      // The refetch restored the real (unread) state.
      expect(store.notifications.first.isRead, isFalse);
      expect(store.unread, 1);
    });
  });

  group('markAllRead', () {
    test('POSTs read-all then refetches the cleared list', () async {
      var readAllCalls = 0;
      var listCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/read-all')) {
            readAllCalls++;
            return http.Response(jsonEncode({'updated': 2}), 200,
                headers: {'content-type': 'application/json'});
          }
          listCalls++;
          // After read-all the server reports everything read.
          return _page([
            _notificationJson('1', read: true),
            _notificationJson('2', read: true),
          ], unread: 0);
        }),
      );

      final ok = await store.markAllRead();

      expect(ok, isTrue);
      expect(readAllCalls, 1);
      expect(listCalls, 1, reason: 'read-all triggers a refetch');
      expect(store.unread, 0);
      expect(store.notifications.every((n) => n.isRead), isTrue);
    });

    test('returns false and records the error on failure', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      final ok = await store.markAllRead();

      expect(ok, isFalse);
      expect(store.actionError, isNotNull);
      // A refused action is not a failed load: no "couldn't refresh" banner.
      expect(store.loadError, isNull);
    });
  });
}
