import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/exception_store.dart';

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'items': items, 'total': items.length, 'page': 1}),
      200,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _exceptionJson(
  String id, {
  String status = 'open',
  String type = 'duplicate',
  String severity = 'error',
}) =>
    {
      'id': id,
      'invoice_id': 'inv-$id',
      'invoice_number': 'INV-$id',
      'vendor_name': 'Acme',
      'amount': 250,
      'exception_type': type,
      'type_label': 'Duplicate Invoice',
      'severity': severity,
      'description': 'Looks like a dupe',
      'status': status,
      'is_overdue': false,
      'created_at': '2026-01-01T12:00:00',
    };

void main() {
  final store = ExceptionStore.instance;

  setUpAll(() async {
    // Private in-memory cache so parallel test isolates don't share a file.
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    ExceptionStore.instance.reset();
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  group('fetch', () {
    test('success populates exceptions and marks them live (not cached)',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([_exceptionJson('1')])),
      );

      await store.fetch();

      expect(store.exceptions, hasLength(1));
      expect(store.exceptions.first.id, '1');
      expect(store.exceptions.first.typeLabel, 'Duplicate Invoice');
      expect(store.fromCache, isFalse);
      expect(store.error, isNull);
      expect(store.loading, isFalse);
    });

    test('falls back to the offline cache when the network fails', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([_exceptionJson('1')])),
      );
      await store.fetch();
      expect(store.fromCache, isFalse);

      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.fetch();

      expect(store.exceptions, hasLength(1));
      expect(store.exceptions.first.id, '1');
      expect(store.fromCache, isTrue);
    });

    test('the offline cache keeps the exact amount and its currency', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([
              {
                ..._exceptionJson('1'),
                'amount': '12345678901234.56',
                'currency': 'ZAR',
              },
            ])),
      );
      await store.fetch();

      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.fetch();

      expect(store.fromCache, isTrue);
      // Not rounded through a double on the way in or out of the cache, and
      // still labelled — an uncached currency rendered the figure bare.
      expect(store.exceptions.first.amount, '12345678901234.56');
      expect(store.exceptions.first.currency, 'ZAR');
    });

    test('surfaces an error when the network fails and no cache exists',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );

      await store.fetch();

      expect(store.error, isNotNull);
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

      store.setStatusFilter('open');

      expect(store.statusFilter, 'open');
      expect(await sentStatus.future, 'open');
    });

    test(
        'a slow stale filter response landing after a faster later one is '
        'discarded (issue #182 request-sequencing guard)', () async {
      // Same race as InvoiceStore/VendorStore's regression test, but driven by
      // rapid status-filter chip taps rather than a search box — the guard is
      // general to any fetch() re-triggered before its predecessor resolves.
      final firstRequestStarted = Completer<void>();
      final releaseFirstResponse = Completer<void>();

      ApiClient().debugConfigure(
        client: MockClient((req) async {
          final status = req.url.queryParameters['status'];
          if (status == 'open') {
            firstRequestStarted.complete();
            await releaseFirstResponse.future;
            return _list([_exceptionJson('stale')]);
          }
          return _list([_exceptionJson('fresh')]);
        }),
      );

      store.setStatusFilter('open');
      await firstRequestStarted.future;

      store.setStatusFilter('escalated');
      await _waitUntil(() => store.exceptions.any((e) => e.id == 'fresh'));

      expect(store.exceptions, hasLength(1));
      expect(store.exceptions.first.id, 'fresh');

      releaseFirstResponse.complete();
      await Future<void>.delayed(const Duration(milliseconds: 20));

      expect(store.exceptions, hasLength(1));
      expect(store.exceptions.first.id, 'fresh',
          reason: 'the earlier, slower response must not clobber the later, '
              'faster one that already landed');
    });
  });

  group('actions', () {
    test('resolve posts the resolve action and then refetches', () async {
      var resolveCalls = 0;
      var listCalls = 0;
      String? sentAction;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/resolve')) {
            resolveCalls++;
            sentAction =
                (jsonDecode(req.body) as Map<String, dynamic>)['action']
                    as String?;
            return http.Response(
              jsonEncode({'id': '1', 'status': 'resolved'}),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          listCalls++;
          return _list([_exceptionJson('1', status: 'resolved')]);
        }),
      );

      final ok = await store.resolve('1');

      expect(ok, isTrue);
      expect(resolveCalls, 1);
      expect(sentAction, 'resolve');
      expect(listCalls, 1, reason: 'resolve should trigger a refetch');
    });

    test('escalate posts the escalate action', () async {
      String? sentAction;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/resolve')) {
            sentAction =
                (jsonDecode(req.body) as Map<String, dynamic>)['action']
                    as String?;
            return http.Response('{}', 200);
          }
          return _list([]);
        }),
      );

      final ok = await store.escalate('1');

      expect(ok, isTrue);
      expect(sentAction, 'escalate');
    });

    test('dismiss posts the dismiss action', () async {
      String? sentAction;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/resolve')) {
            sentAction =
                (jsonDecode(req.body) as Map<String, dynamic>)['action']
                    as String?;
            return http.Response('{}', 200);
          }
          return _list([]);
        }),
      );

      final ok = await store.dismiss('1');

      expect(ok, isTrue);
      expect(sentAction, 'dismiss');
    });

    test('an action returns false and records the error on failure', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      final ok = await store.resolve('1');

      expect(ok, isFalse);
      expect(store.error, isNotNull);
    });
  });

  group('getById', () {
    test('loads a single exception detail with the detail-only fields',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          expect(req.url.path.endsWith('/exceptions/1'), isTrue);
          return http.Response(
            jsonEncode({
              ..._exceptionJson('1'),
              'assigned_to': 'Demo Manager',
              'assigned_to_user_id': 'user-9',
              'due_at': '2026-01-02T12:00:00',
              'resolved_by': null,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }),
      );

      final exc = await store.getById('1');

      expect(exc, isNotNull);
      expect(exc!.id, '1');
      expect(exc.assignedTo, 'Demo Manager');
      expect(exc.assignedToUserId, 'user-9');
      expect(exc.dueAt, isNotNull);
    });

    test('returns null + records the error on a 404', () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => http.Response('{"detail":"not found"}', 404),
        ),
      );

      final exc = await store.getById('missing');

      expect(exc, isNull);
      expect(store.error, isNotNull);
    });
  });

  group('assign', () {
    test('posts {user_id} and patches the in-memory row with the new assignee',
        () async {
      // Seed a list so there's a row to patch in place.
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([_exceptionJson('1')])),
      );
      await store.fetch();
      expect(store.exceptions.first.assignedTo, isNull);

      String? sentUserId;
      var sawKey = false;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/assign')) {
            final body = jsonDecode(req.body) as Map<String, dynamic>;
            sawKey = body.containsKey('user_id');
            sentUserId = body['user_id'] as String?;
            return http.Response(
              jsonEncode({
                ..._exceptionJson('1'),
                'assigned_to': 'Casey Clerk',
                'assigned_to_user_id': 'user-42',
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return _list([]);
        }),
      );

      final updated = await store.assign('1', userId: 'user-42');

      expect(sawKey, isTrue);
      expect(sentUserId, 'user-42');
      expect(updated, isNotNull);
      expect(updated!.assignedTo, 'Casey Clerk');
      // The in-memory list row reflects the change without a refetch.
      expect(store.exceptions.first.assignedTo, 'Casey Clerk');
      expect(store.exceptions.first.assignedToUserId, 'user-42');
    });

    test('unassign sends user_id: null', () async {
      String? sentUserId = 'sentinel';
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/assign')) {
            sentUserId =
                (jsonDecode(req.body) as Map<String, dynamic>)['user_id']
                    as String?;
            return http.Response(
              jsonEncode({..._exceptionJson('1'), 'assigned_to': null}),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return _list([]);
        }),
      );

      final updated = await store.assign('1');

      expect(sentUserId, isNull);
      expect(updated!.assignedTo, isNull);
    });
  });

  group('selection + bulkResolve', () {
    test('selection mutators toggle membership and mode', () async {
      store.enterSelectionMode('1');
      expect(store.selectionMode, isTrue);
      expect(store.isSelected('1'), isTrue);
      expect(store.selectedCount, 1);

      store.toggleSelected('2');
      expect(store.selectedCount, 2);
      store.toggleSelected('1');
      expect(store.isSelected('1'), isFalse);

      store.exitSelectionMode();
      expect(store.selectionMode, isFalse);
      expect(store.selectedCount, 0);
    });

    test('bulkResolveSelected parses the partial-success {updated, skipped}',
        () async {
      store.enterSelectionMode('1');
      store.toggleSelected('2');

      List<dynamic>? sentIds;
      String? sentAction;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' &&
              req.url.path.endsWith('/exceptions/bulk/resolve')) {
            final body = jsonDecode(req.body) as Map<String, dynamic>;
            sentIds = body['ids'] as List?;
            sentAction = body['action'] as String?;
            return http.Response(
              jsonEncode({
                'updated': 1,
                'skipped': [
                  {'id': '2', 'reason': 'already_resolved'},
                ],
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          // The refetch after a successful bulk call.
          return _list([]);
        }),
      );

      final result = await store.bulkResolveSelected(action: 'resolve');

      expect(result, isNotNull);
      expect(result!.updated, 1);
      expect(result.skippedCount, 1);
      expect(result.skipped.first.id, '2');
      expect(result.skipped.first.reason, 'already_resolved');
      expect(sentAction, 'resolve');
      expect(sentIds, containsAll(<String>['1', '2']));
      // A successful bulk call exits selection mode.
      expect(store.selectionMode, isFalse);
    });

    test('bulkResolveSelected is a no-op (null) with an empty selection',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([])),
      );
      final result = await store.bulkResolveSelected();
      expect(result, isNull);
    });

    test('bulkResolveSelected returns null + records error on failure',
        () async {
      store.enterSelectionMode('1');
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      final result = await store.bulkResolveSelected();

      expect(result, isNull);
      expect(store.error, isNotNull);
      // Selection survives a failure so the user can retry.
      expect(store.selectionMode, isTrue);
    });
  });
}

/// Polls [cond] until it's true (or a bounded number of iterations elapse) —
/// used where a fire-and-forget store call (mirroring the real screen's
/// unawaited filter setter) needs a deterministic point to assert from.
Future<void> _waitUntil(bool Function() cond, {int maxIterations = 100}) async {
  for (var i = 0; i < maxIterations && !cond(); i++) {
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
}
