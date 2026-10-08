import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/vendor_store.dart';

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
  jsonEncode({'items': items, 'total': items.length, 'page': 1}),
  200,
  headers: {'content-type': 'application/json'},
);

Map<String, dynamic> _vendorJson(
  String id, {
  String status = 'unverified',
  String source = 'manual',
}) => {
  'id': id,
  'name': 'Vendor $id',
  'code': 'V$id',
  'email': 'v$id@example.com',
  'status': status,
  'source': source,
  'invoice_count': 2,
  'created_at': '2026-01-01T12:00:00',
};

void main() {
  final store = VendorStore.instance;

  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    VendorStore.instance.reset();
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  group('fetch', () {
    test(
      'success populates vendors and marks them live (not cached)',
      () async {
        ApiClient().debugConfigure(
          client: MockClient((req) async => _list([_vendorJson('1')])),
        );

        await store.fetch();

        expect(store.vendors, hasLength(1));
        expect(store.vendors.first.id, '1');
        expect(store.fromCache, isFalse);
        expect(store.error, isNull);
        expect(store.loading, isFalse);
      },
    );

    test('falls back to the offline cache when the network fails', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([_vendorJson('1')])),
      );
      await store.fetch();
      expect(store.fromCache, isFalse);

      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.fetch();

      expect(store.vendors, hasLength(1));
      expect(store.fromCache, isTrue);
    });

    test(
      'surfaces an error when the network fails and no cache exists',
      () async {
        ApiClient().debugConfigure(
          client: MockClient((req) async => throw Exception('offline')),
        );

        await store.fetch();

        expect(store.error, isNotNull);
        expect(store.fromCache, isFalse);
      },
    );
  });

  group('filters', () {
    test('setStatusFilter carries the status into the request', () async {
      final sent = Completer<String?>();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (!sent.isCompleted) {
            sent.complete(req.url.queryParameters['status']);
          }
          return _list([]);
        }),
      );

      store.setStatusFilter('unverified');

      expect(store.statusFilter, 'unverified');
      expect(await sent.future, 'unverified');
    });

    test('setSearch carries the query into the request', () async {
      final sent = Completer<String?>();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (!sent.isCompleted) {
            sent.complete(req.url.queryParameters['search']);
          }
          return _list([]);
        }),
      );

      store.setSearch('acme');

      expect(await sent.future, 'acme');
    });

    test('a slow stale search response landing after a faster later one is '
        'discarded (issue #182 request-sequencing guard)', () async {
      // Same race as InvoiceStore's regression test: an early, slow response
      // must not clobber a later, faster one that already landed.
      final firstRequestStarted = Completer<void>();
      final releaseFirstResponse = Completer<void>();

      ApiClient().debugConfigure(
        client: MockClient((req) async {
          final search = req.url.queryParameters['search'];
          if (search == 'a') {
            firstRequestStarted.complete();
            await releaseFirstResponse.future;
            return _list([_vendorJson('stale')]);
          }
          return _list([_vendorJson('fresh')]);
        }),
      );

      store.setSearch('a');
      await firstRequestStarted.future;

      store.setSearch('ac');
      await _waitUntil(() => store.vendors.any((v) => v.id == 'fresh'));

      expect(store.vendors, hasLength(1));
      expect(store.vendors.first.id, 'fresh');

      releaseFirstResponse.complete();
      await Future<void>.delayed(const Duration(milliseconds: 20));

      expect(store.vendors, hasLength(1));
      expect(
        store.vendors.first.id,
        'fresh',
        reason:
            'the earlier, slower response must not clobber the later, '
            'faster one that already landed',
      );
    });
  });

  group('actions', () {
    test('verify posts to /verify and refetches', () async {
      var verifyCalls = 0;
      var listCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/verify')) {
            verifyCalls++;
            return http.Response(
              jsonEncode(_vendorJson('1', status: 'active')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          listCalls++;
          return _list([_vendorJson('1', status: 'active')]);
        }),
      );

      final ok = await store.verify('1');

      expect(ok, isTrue);
      expect(verifyCalls, 1);
      expect(listCalls, 1, reason: 'verify should trigger a refetch');
    });

    test('reject posts to /reject and refetches', () async {
      var rejectCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/reject')) {
            rejectCalls++;
            return http.Response(
              jsonEncode(_vendorJson('1', status: 'rejected')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return _list([_vendorJson('1', status: 'rejected')]);
        }),
      );

      final ok = await store.reject('1');

      expect(ok, isTrue);
      expect(rejectCalls, 1);
      expect(store.lastRejected?.cardsNotClosed, 0);
    });

    test(
      'reject keeps the still-live card counts for the screen to surface',
      () async {
        ApiClient().debugConfigure(
          client: MockClient((req) async {
            if (req.method == 'POST' && req.url.path.endsWith('/reject')) {
              return http.Response(
                jsonEncode({
                  ..._vendorJson('1', status: 'rejected'),
                  'card_revocation': {
                    'vendor_id': '1',
                    'cancelled': 0,
                    'not_closed': [
                      {
                        'card_id': 'c1',
                        'last_four': '4242',
                        'outcome': 'card_cancel_rejected',
                      },
                    ],
                    'requires_payment_void': [],
                  },
                }),
                200,
                headers: {'content-type': 'application/json'},
              );
            }
            return _list([_vendorJson('1', status: 'rejected')]);
          }),
        );

        expect(await store.reject('1'), isTrue);
        expect(store.lastRejected?.cardsNotClosed, 1);
        expect(store.lastRejected?.cardsRequireVoid, 0);
      },
    );

    test('verify returns false + records error on failure', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      final ok = await store.verify('1');

      expect(ok, isFalse);
      expect(store.error, isNotNull);
    });

    test('syncErp posts to /sync-erp and returns the server message', () async {
      var syncCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/sync-erp')) {
            syncCalls++;
            return http.Response(
              jsonEncode({
                'success': true,
                'message': 'Synced 2 new, 0 updated, 0 unchanged',
                'created': 2,
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return _list([_vendorJson('1', source: 'erp_sync')]);
        }),
      );

      final message = await store.syncErp();

      expect(syncCalls, 1);
      expect(message, contains('Synced 2 new'));
    });

    test(
      'syncErp returns null + records error when no ERP configured (400)',
      () async {
        ApiClient().debugConfigure(
          client: MockClient(
            (req) async => http.Response('No ERP configured', 400),
          ),
        );

        final message = await store.syncErp();

        expect(message, isNull);
        expect(store.error, isNotNull);
      },
    );
  });
}

/// Polls [cond] until it's true (or a bounded number of iterations elapse) —
/// used where a fire-and-forget store call (mirroring the real screen's
/// unawaited `setSearch`) needs a deterministic point to assert from.
Future<void> _waitUntil(bool Function() cond, {int maxIterations = 100}) async {
  for (var i = 0; i < maxIterations && !cond(); i++) {
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
}
