import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/contract_store.dart';

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'items': items, 'total': items.length, 'page': 1}),
      200,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _contractJson(
  String id, {
  String status = 'active',
  String contractType = 'service',
}) =>
    {
      'id': id,
      'contract_number': 'CTR-$id',
      'title': 'Contract $id',
      'contract_type': contractType,
      'status': status,
      'vendor_name': 'Acme',
      'currency': 'USD',
      'total_value': 1000,
      'created_at': '2026-01-01T12:00:00',
    };

void main() {
  final store = ContractStore.instance;

  setUpAll(() async {
    // Private in-memory cache so parallel test isolates don't share a file.
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    ContractStore.instance.reset();
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  group('fetch', () {
    test('success populates contracts and marks them as live (not cached)',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([_contractJson('1')])),
      );

      await store.fetch();

      expect(store.contracts, hasLength(1));
      expect(store.contracts.first.id, '1');
      expect(store.fromCache, isFalse);
      expect(store.loadError, isNull);
      expect(store.loading, isFalse);
    });

    test('falls back to the offline cache when the network fails', () async {
      // 1) prime the cache with a successful fetch
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([_contractJson('1')])),
      );
      await store.fetch();
      expect(store.fromCache, isFalse);

      // 2) same filters, but the network now fails -> serve from cache
      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.fetch();

      expect(store.contracts, hasLength(1));
      expect(store.contracts.first.id, '1');
      expect(store.fromCache, isTrue);
    });

    test('surfaces an error when the network fails and no cache exists',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );

      await store.fetch();

      expect(store.loadError, isNotNull);
      expect(store.fromCache, isFalse);
    });
  });

  group('filters', () {
    test('setStatusFilter updates the getter and carries it into the request',
        () async {
      final sentStatus = Completer<String?>();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (!sentStatus.isCompleted) {
            sentStatus.complete(req.url.queryParameters['status']);
          }
          return _list([]);
        }),
      );

      store.setStatusFilter('active');

      expect(store.statusFilter, 'active');
      expect(await sentStatus.future, 'active');
    });

    test('setSearch carries the query into the request', () async {
      final sentSearch = Completer<String?>();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (!sentSearch.isCompleted) {
            sentSearch.complete(req.url.queryParameters['search']);
          }
          return _list([]);
        }),
      );

      store.setSearch('hosting');

      expect(await sentSearch.future, 'hosting');
    });
  });

  group('lifecycle actions', () {
    test('activate posts and then refetches the list', () async {
      var activateCalls = 0;
      var listCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/activate')) {
            activateCalls++;
            return http.Response(
              jsonEncode(_contractJson('1', status: 'active')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          listCalls++;
          return _list([_contractJson('1', status: 'active')]);
        }),
      );

      final ok = await store.activate('1');

      expect(ok, isTrue);
      expect(activateCalls, 1);
      expect(listCalls, 1, reason: 'activate should trigger a refetch');
    });

    test('activate returns false and records the error on failure', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      final ok = await store.activate('1');

      expect(ok, isFalse);
      expect(store.actionError, isNotNull);
      // A refused action is not a failed load: the list stays live, so the
      // screen raises no "couldn't refresh" banner over it.
      expect(store.loadError, isNull);
    });

    test('clearFilters drops the chip and the query and refetches unfiltered',
        () async {
      final queries = <Map<String, String>>[];
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          queries.add(req.url.queryParameters);
          return _list([]);
        }),
      );
      store.setStatusFilter('expired');
      store.setSearch('acme');
      await store.fetch();
      expect(store.isFiltered, isTrue);

      store.clearFilters();
      await store.fetch();

      expect(store.isFiltered, isFalse);
      expect(store.statusFilter, isNull);
      expect(queries.last.containsKey('status'), isFalse);
      expect(queries.last.containsKey('search'), isFalse);
    });

    test('terminate posts to the terminate endpoint and refetches', () async {
      var terminateCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/terminate')) {
            terminateCalls++;
            return http.Response(
              jsonEncode(_contractJson('1', status: 'terminated')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return _list([_contractJson('1', status: 'terminated')]);
        }),
      );

      final ok = await store.terminate('1');

      expect(ok, isTrue);
      expect(terminateCalls, 1);
    });
  });
}
