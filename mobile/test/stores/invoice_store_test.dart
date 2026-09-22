import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'invoices': items}),
      200,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _invoiceJson(
  String id, {
  String status = 'pending',
  String vendor = 'Acme',
}) =>
    {
      'id': id,
      'invoice_number': 'INV-$id',
      'vendor_name': vendor,
      'amount': 100,
      'currency': 'USD',
      'status': status,
      'created_at': '2026-01-01T12:00:00',
    };

void main() {
  final store = InvoiceStore.instance;

  setUpAll(() async {
    // Private in-memory cache so parallel test isolates don't share a file.
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    InvoiceStore.instance.reset();
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  group('fetch', () {
    test('success populates invoices and marks them as live (not cached)',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([_invoiceJson('1')])),
      );

      await store.fetch();

      expect(store.invoices, hasLength(1));
      expect(store.invoices.first.id, '1');
      expect(store.fromCache, isFalse);
      expect(store.error, isNull);
      expect(store.loading, isFalse);
    });

    test('falls back to the offline cache when the network fails', () async {
      // 1) prime the cache with a successful fetch
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([_invoiceJson('1')])),
      );
      await store.fetch();
      expect(store.fromCache, isFalse);

      // 2) same filters, but the network now fails -> serve from cache
      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.fetch();

      expect(store.invoices, hasLength(1));
      expect(store.invoices.first.id, '1');
      expect(store.fromCache, isTrue);
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
    test('list requests carry page_size, the name the backend declares',
        () async {
      // FastAPI's pagination dependency declares `page_size`; an unknown
      // `per_page` is silently dropped, so the old spelling meant the caller's
      // page size never reached the server.
      Map<String, String>? params;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          params ??= req.url.queryParameters;
          return _list([]);
        }),
      );

      await store.fetch();

      expect(params!['page_size'], '20');
      expect(params!.containsKey('per_page'), isFalse);
    });

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

      store.setStatusFilter('approved');

      expect(store.statusFilter, 'approved');
      expect(await sentStatus.future, 'approved');
    });

    test('setFilters carries the advanced-search params into the request',
        () async {
      final sent = Completer<Map<String, String>>();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (!sent.isCompleted) sent.complete(req.url.queryParameters);
          return _list([]);
        }),
      );

      store.setFilters(const InvoiceSearchFilters(
        vendor: 'Acme',
        poNumber: 'PO-9',
        amountMin: 100,
        amountMax: 5000,
      ));

      expect(store.filters.isEmpty, isFalse);
      expect(store.filters.activeCount, 4);
      final params = await sent.future;
      expect(params['vendor'], 'Acme');
      expect(params['po_number'], 'PO-9');
      expect(params['amount_min'], '100.0');
      expect(params['amount_max'], '5000.0');
    });

    test('setFilters sends due-date range as YYYY-MM-DD', () async {
      final sent = Completer<Map<String, String>>();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (!sent.isCompleted) sent.complete(req.url.queryParameters);
          return _list([]);
        }),
      );

      store.setFilters(InvoiceSearchFilters(
        dueDateFrom: DateTime(2026, 2, 1),
        dueDateTo: DateTime(2026, 3, 15),
      ));

      final params = await sent.future;
      expect(params['due_date_from'], '2026-02-01');
      expect(params['due_date_to'], '2026-03-15');
    });

    test(
        'a slow stale search response landing after a faster later one is '
        'discarded (issue #182 request-sequencing guard)', () async {
      // Simulate: user types "a" (server artificially slow to respond), then
      // quickly types "ac" (normal-speed response). The "a" request's result
      // must not clobber the list after "ac"'s response has already landed —
      // that's the exact stale-response race the bug report describes.
      final firstRequestStarted = Completer<void>();
      final releaseFirstResponse = Completer<void>();

      ApiClient().debugConfigure(
        client: MockClient((req) async {
          final search = req.url.queryParameters['search'];
          if (search == 'a') {
            firstRequestStarted.complete();
            // Held open until after the second ("ac") request completes.
            await releaseFirstResponse.future;
            return _list([_invoiceJson('stale', vendor: 'Stale Corp')]);
          }
          return _list([_invoiceJson('fresh', vendor: 'Fresh Corp')]);
        }),
      );

      // Mirrors the search box's real call pattern: fire-and-forget.
      store.setSearch('a');
      await firstRequestStarted.future;

      store.setSearch('ac');
      await _waitUntil(() => store.invoices.any((i) => i.id == 'fresh'));

      expect(store.invoices, hasLength(1));
      expect(store.invoices.first.id, 'fresh');

      // Now release the stale first response — the guard must discard it
      // rather than let it overwrite the already-current "fresh" result.
      releaseFirstResponse.complete();
      await Future<void>.delayed(const Duration(milliseconds: 20));

      expect(store.invoices, hasLength(1));
      expect(store.invoices.first.id, 'fresh',
          reason: 'the earlier, slower response must not clobber the later, '
              'faster one that already landed');
    });

    test('setFilters with empty omits all advanced params', () async {
      final sent = Completer<Map<String, String>>();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (!sent.isCompleted) sent.complete(req.url.queryParameters);
          return _list([]);
        }),
      );

      store.setFilters(InvoiceSearchFilters.empty);

      expect(store.filters.isEmpty, isTrue);
      final params = await sent.future;
      expect(params.containsKey('vendor'), isFalse);
      expect(params.containsKey('amount_min'), isFalse);
      expect(params.containsKey('due_date_from'), isFalse);
    });
  });

  group('approve / reject', () {
    test('approve posts and then refetches the list', () async {
      var approveCalls = 0;
      var listCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/approve')) {
            approveCalls++;
            return http.Response(
              jsonEncode(_invoiceJson('1', status: 'approved')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          listCalls++;
          return _list([_invoiceJson('1', status: 'approved')]);
        }),
      );

      final ok = await store.approve('1');

      expect(ok, isTrue);
      expect(approveCalls, 1);
      expect(listCalls, 1, reason: 'approve should trigger a refetch');
    });

    test('approve returns false and records the error on failure', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      final ok = await store.approve('1');

      expect(ok, isFalse);
      expect(store.error, isNotNull);
    });

    test('reject posts the reason and refetches', () async {
      String? sentReason;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/reject')) {
            sentReason = (jsonDecode(req.body) as Map)['reason'] as String?;
            return http.Response(
              jsonEncode(_invoiceJson('1', status: 'rejected')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return _list([]);
        }),
      );

      final ok = await store.reject('1', 'Wrong amount');

      expect(ok, isTrue);
      expect(sentReason, 'Wrong amount');
    });
  });

  group('update (edit fields)', () {
    test('PATCHes the partial body and refetches the list', () async {
      String? method;
      String? path;
      Map<String, dynamic>? sentBody;
      var listCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'PATCH') {
            method = req.method;
            path = req.url.path;
            sentBody = jsonDecode(req.body) as Map<String, dynamic>;
            return http.Response(
              jsonEncode(_invoiceJson('1', vendor: 'Globex')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          listCalls++;
          return _list([_invoiceJson('1', vendor: 'Globex')]);
        }),
      );

      final updated = await store.update('1', {
        'vendor': 'Globex',
        'amount': '250.00', // string-Decimal, never a float
      });

      expect(updated, isNotNull);
      expect(updated!.vendorName, 'Globex');
      expect(method, 'PATCH');
      expect(path, '/api/invoices/1');
      // Money goes over the wire as a string (Decimal-safe), not a JS number.
      expect(sentBody!['amount'], '250.00');
      expect(sentBody!['amount'], isA<String>());
      expect(listCalls, 1, reason: 'a successful edit refetches the list');
    });

    test('returns null and records the error on failure', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 409)),
      );

      final updated = await store.update('1', {'vendor': 'X'});

      expect(updated, isNull);
      expect(store.error, isNotNull);
    });

    test('records the server\'s detail sentence, not the exception form',
        () async {
      // A refusal is something the user acts on — e.g. a GL code outside the
      // invoice's chart — so the screen shows `error` verbatim.
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => http.Response(
            jsonEncode({'detail': "GL account '9999' is retired"}),
            422,
            headers: {'content-type': 'application/json'},
          ),
        ),
      );

      final updated = await store.update('1', {'gl_account': '9999'});

      expect(updated, isNull);
      expect(store.error, "GL account '9999' is retired");
    });
  });

  group('fetchAuditLog (activity timeline)', () {
    test('parses the bare audit-log array oldest-first', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          expect(req.url.path, '/api/invoices/1/audit-log');
          return http.Response(
            jsonEncode([
              {
                'id': 'a1',
                'actor_id': 'u1',
                'actor_name': 'Demo User',
                'action': 'invoice.uploaded',
                'entity_type': 'invoice',
                'entity_id': '1',
                'details': null,
                'created_at': '2026-01-01T10:00:00',
              },
              {
                'id': 'a2',
                'actor_id': 'u1',
                'actor_name': 'Demo User',
                'action': 'invoice.edited',
                'entity_type': 'invoice',
                'entity_id': '1',
                'details': {
                  'changes': {
                    'amount': {'old': '100.00', 'new': '250.00'},
                  },
                },
                'created_at': '2026-01-02T10:00:00',
              },
            ]),
            200,
            headers: {'content-type': 'application/json'},
          );
        }),
      );

      final entries = await store.fetchAuditLog('1');

      expect(entries, hasLength(2));
      expect(entries.first.action, 'invoice.uploaded');
      expect(entries.first.actionLabel, 'Uploaded invoice');
      final edit = entries[1];
      expect(edit.changes, hasLength(1));
      expect(edit.changes.first.field, 'amount');
      expect(edit.changes.first.oldDisplay, '100.00');
      expect(edit.changes.first.newDisplay, '250.00');
    });

    test('rethrows when the request fails', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      expect(store.fetchAuditLog('1'), throwsA(isA<ApiException>()));
    });
  });

  group('selection mode', () {
    test('enter/toggle/select-all/clear/exit track the selected set', () async {
      store.reset();
      // Populate the list via a real fetch so selectAll has rows to enumerate.
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([
              _invoiceJson('1'),
              _invoiceJson('2'),
              _invoiceJson('3'),
            ])),
      );
      await store.fetch();
      expect(store.invoices, hasLength(3));

      expect(store.selectionMode, isFalse);

      store.enterSelectionMode('1');
      expect(store.selectionMode, isTrue);
      expect(store.isSelected('1'), isTrue);
      expect(store.selectedCount, 1);

      store.toggleSelected('2');
      expect(store.selectedCount, 2);
      store.toggleSelected('2'); // toggling off
      expect(store.isSelected('2'), isFalse);

      store.selectAll();
      expect(store.selectedCount, 3);

      store.clearSelection();
      expect(store.selectedCount, 0);
      expect(store.selectionMode, isTrue, reason: 'clear keeps the mode on');

      store.exitSelectionMode();
      expect(store.selectionMode, isFalse);
      expect(store.selectedCount, 0);
    });
  });

  group('bulk operations', () {
    test('bulkDeleteSelected posts ids, exits selection, and refetches',
        () async {
      Map<String, dynamic>? sentBody;
      var listCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/bulk/delete')) {
            sentBody = jsonDecode(req.body) as Map<String, dynamic>;
            return http.Response(
              jsonEncode({'deleted': 2, 'skipped': ['3']}),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          listCalls++;
          return _list([]);
        }),
      );

      store
        ..reset()
        ..enterSelectionMode('1')
        ..toggleSelected('2');

      final result = await store.bulkDeleteSelected();

      expect(result, isNotNull);
      expect(result!.count, 2);
      expect(result.skipped, ['3']);
      expect((sentBody!['ids'] as List).toSet(), {'1', '2'});
      expect(store.selectionMode, isFalse, reason: 'exits selection on success');
      expect(listCalls, 1, reason: 'success triggers a refetch');
    });

    test('bulkStatusSelected sends the target status', () async {
      Map<String, dynamic>? sentBody;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/bulk/status')) {
            sentBody = jsonDecode(req.body) as Map<String, dynamic>;
            return http.Response(
              jsonEncode({'updated': 1, 'skipped': []}),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return _list([]);
        }),
      );

      store
        ..reset()
        ..enterSelectionMode('1');

      final result = await store.bulkStatusSelected('approved');

      expect(result!.count, 1);
      expect(sentBody!['status'], 'approved');
      expect(sentBody!['ids'], ['1']);
    });

    test('bulk action with nothing selected is a no-op (null, no request)',
        () async {
      var calls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          calls++;
          return _list([]);
        }),
      );

      store.reset();
      expect(await store.bulkDeleteSelected(), isNull);
      expect(await store.bulkStatusSelected('approved'), isNull);
      expect(calls, 0);
    });

    test('failure records the error, returns null, keeps selection', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      store
        ..reset()
        ..enterSelectionMode('1');

      final result = await store.bulkDeleteSelected();

      expect(result, isNull);
      expect(store.error, isNotNull);
      expect(store.selectionMode, isTrue,
          reason: 'a failed bulk op leaves the selection intact to retry');
    });
  });

  group('exportSelected', () {
    test('posts ids + format and returns the bytes + server filename', () async {
      Map<String, dynamic>? sentBody;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/bulk/export')) {
            sentBody = jsonDecode(req.body) as Map<String, dynamic>;
            return http.Response(
              'id,vendor\n1,Acme\n',
              200,
              headers: {
                'content-type': 'text/csv',
                'content-disposition':
                    'attachment; filename="invoices-export.csv"',
              },
            );
          }
          return _list([]);
        }),
      );

      store
        ..reset()
        ..enterSelectionMode('1')
        ..toggleSelected('2');

      final result = await store.exportSelected('csv');

      expect(result, isNotNull);
      expect(String.fromCharCodes(result!.bytes), contains('Acme'));
      expect(result.filename, 'invoices-export.csv');
      expect(sentBody!['format'], 'csv');
      expect((sentBody!['ids'] as List).toSet(), {'1', '2'});
      expect(store.selectionMode, isTrue,
          reason: 'export is non-mutating — selection is preserved');
    });

    test('falls back to a default filename when the header is absent', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          return http.Response(
            '<Invoices/>',
            200,
            headers: {'content-type': 'application/xml'},
          );
        }),
      );

      store
        ..reset()
        ..enterSelectionMode('1');

      final result = await store.exportSelected('xml');

      expect(result!.filename, 'invoices-export.xml');
    });

    test('nothing selected is a no-op (null, no request)', () async {
      var calls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          calls++;
          return _list([]);
        }),
      );

      store.reset();
      expect(await store.exportSelected('csv'), isNull);
      expect(calls, 0);
    });

    test('failure records the error and returns null', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      store
        ..reset()
        ..enterSelectionMode('1');

      final result = await store.exportSelected('csv');

      expect(result, isNull);
      expect(store.error, isNotNull);
    });
  });

  // The Approvals tab used to render a client-side `.where()` over whatever the
  // Invoices tab had last fetched. Both screens live in one IndexedStack, so
  // picking a status chip on Invoices (e.g. `paid`) emptied the approvals queue
  // and nothing re-fetched on tab switch; pull-to-refresh re-applied the same
  // wrong filter, so it could not self-correct.
  group('fetchPending (approvals queue)', () {
    test('asks the server for ready_for_review, not a client-side slice',
        () async {
      String? sentStatus;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          sentStatus = req.url.queryParameters['status'];
          return _list([_invoiceJson('1', status: 'ready_for_review')]);
        }),
      );

      await store.fetchPending();

      expect(sentStatus, 'ready_for_review');
      expect(store.pending.map((i) => i.id), ['1']);
      expect(store.pendingError, isNull);
      expect(store.pendingLoading, isFalse);
    });

    test('does not read or mutate the Invoices tab filter', () async {
      final statuses = <String?>[];
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          statuses.add(req.url.queryParameters['status']);
          return _list(
            req.url.queryParameters['status'] == 'ready_for_review'
                ? [_invoiceJson('9', status: 'ready_for_review')]
                : [_invoiceJson('1', status: 'paid')],
          );
        }),
      );

      // The user filters the Invoices tab to `paid` …
      store.setStatusFilter('paid');
      await Future<void>.delayed(Duration.zero);
      // … and then opens Approvals.
      await store.fetchPending();

      expect(statuses, contains('paid'));
      expect(statuses, contains('ready_for_review'));
      // The approvals queue is unaffected by the other tab's chip …
      expect(store.pending.map((i) => i.id), ['9']);
      // … and the chip itself is untouched.
      expect(store.statusFilter, 'paid');
      expect(store.invoices.map((i) => i.id), ['1']);
    });

    test('falls back to its own cached queue when the network fails', () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _list([_invoiceJson('1', status: 'ready_for_review')]),
        ),
      );
      await store.fetchPending();
      expect(store.pendingFromCache, isFalse);

      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.fetchPending();

      expect(store.pending.map((i) => i.id), ['1']);
      expect(store.pendingFromCache, isTrue);
      expect(store.pendingError, isNull);
    });

    test('surfaces an error rather than an empty queue when nothing is cached',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      await store.fetchPending();

      // An empty approvals list and an unreachable one must not look alike.
      expect(store.pendingError, isNotNull);
      expect(store.pending, isEmpty);
    });

    test('approving refreshes the approvals queue, not just the invoice list',
        () async {
      var approved = false;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST' && req.url.path.endsWith('/approve')) {
            approved = true;
            return http.Response(
              jsonEncode(_invoiceJson('1', status: 'approved')),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          if (req.url.queryParameters['status'] == 'ready_for_review') {
            return _list(
              approved ? [] : [_invoiceJson('1', status: 'ready_for_review')],
            );
          }
          return _list([]);
        }),
      );

      await store.fetchPending();
      expect(store.pending, hasLength(1));

      expect(await store.approve('1'), isTrue);

      expect(store.pending, isEmpty);
    });

    test('a superseded pending fetch never clobbers a newer one', () async {
      final gate = Completer<http.Response>();
      var call = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          call++;
          if (call == 1) return gate.future; // slow first request
          return _list([_invoiceJson('2', status: 'ready_for_review')]);
        }),
      );

      final slow = store.fetchPending();
      final fast = store.fetchPending();
      await fast;
      gate.complete(_list([_invoiceJson('1', status: 'ready_for_review')]));
      await slow;

      expect(store.pending.map((i) => i.id), ['2']);
    });
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
