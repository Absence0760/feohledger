import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/models/payment_queue.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/payment_queue_store.dart';

/// One row of `GET /api/payments/queue`, mirroring the dict
/// `app/api/payments.py::payment_queue` builds — including the three refusal
/// fields (`blocked` / `blocked_reason` / `required_method`) it stamps from
/// `services/payment_runs.run_refusal_reasons`.
Map<String, dynamic> _queueItem(
  String id, {
  double amount = 100,
  bool blocked = false,
  String? blockedReason,
  String? requiredMethod,
}) =>
    {
      'id': id,
      'invoice_number': 'INV-$id',
      'vendor_name': 'Vendor $id',
      'amount': amount,
      'currency': 'USD',
      'due_date': '2026-02-01',
      'status': 'approved',
      'is_overdue': false,
      'discount_eligible': false,
      'blocked': blocked,
      'blocked_reason': blockedReason,
      'required_method': requiredMethod,
    };

/// The parsed row, for the store methods that take the item rather than a bare
/// id (the pin and the blocked verdict travel with the data).
PaymentQueueItem _item(
  String id, {
  bool blocked = false,
  String? blockedReason,
  String? requiredMethod,
}) =>
    PaymentQueueItem.fromJson(_queueItem(
      id,
      blocked: blocked,
      blockedReason: blockedReason,
      requiredMethod: requiredMethod,
    ));

const _summary = {
  'total_paid': 1000.0,
  'total_pending': 200.0,
  'payment_count': 5,
  'total_rebates': 12.0,
  'queue_count': 2,
};

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

/// Answers the run POST with [post] and every refetch GET with an empty page.
MockClient _runsClient(Map<String, dynamic> post) => MockClient(
      (req) async => req.method == 'POST'
          ? _json(post)
          : _json({'items': [], 'total': 0, 'page': 1}),
    );

void main() {
  final store = PaymentQueueStore.instance;

  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    PaymentQueueStore.instance.reset();
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  // Route helper: serves queue + summary (+ optional run endpoints).
  MockClient makeClient({
    List<Map<String, dynamic>>? queue,
    Map<String, dynamic>? summary,
    http.Response Function(http.Request req)? onPost,
    http.Response Function()? mode,
    void Function(http.Request req)? onGet,
  }) {
    return MockClient((req) async {
      if (req.method == 'POST') {
        if (onPost != null) return onPost(req);
        return _json({'message': 'ok'});
      }
      onGet?.call(req);
      final path = req.url.path;
      if (mode != null && path.endsWith('/payments/execution-mode')) {
        return mode();
      }
      if (path.endsWith('/payments/queue')) {
        return _json({
          'items': queue ?? [_queueItem('1')],
          'total': (queue ?? [_queueItem('1')]).length,
          'total_amount': 100.0,
          'total_savings': 0.0,
        });
      }
      if (path.endsWith('/payments/summary')) {
        return _json(summary ?? _summary);
      }
      if (path.contains('/payments/runs')) {
        return _json({'items': [], 'total': 0, 'page': 1});
      }
      return _json({});
    });
  }

  group('fetch', () {
    test('loads queue + summary together', () async {
      ApiClient().debugConfigure(
        client: makeClient(queue: [_queueItem('1'), _queueItem('2')]),
      );

      await store.fetch();

      expect(store.queue, hasLength(2));
      expect(store.summary, isNotNull);
      expect(store.summary!.queueCount, 2);
      expect(store.queueLoadError, isNull);
      expect(store.fromCache, isFalse);
    });

    test('drops a selection for an invoice that left the queue', () async {
      ApiClient().debugConfigure(client: makeClient(queue: [_queueItem('1')]));
      await store.fetch();
      store.toggleSelection(_item('1'));
      expect(store.isSelected('1'), isTrue);

      // Next fetch returns an empty queue — the stale selection is cleared.
      ApiClient().debugConfigure(client: makeClient(queue: []));
      await store.fetch();

      expect(store.isSelected('1'), isFalse);
      expect(store.hasSelection, isFalse);
    });

    test('falls back to the cached queue when the network fails', () async {
      ApiClient().debugConfigure(client: makeClient(queue: [_queueItem('1')]));
      await store.fetch();
      expect(store.fromCache, isFalse);

      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.fetch();

      expect(store.queue, hasLength(1));
      expect(store.fromCache, isTrue);
    });
  });

  group('selection', () {
    test('toggle adds with ACH default, toggle again removes', () {
      store.toggleSelection(_item('a'));
      expect(store.isSelected('a'), isTrue);
      expect(store.methodFor(_item('a')), PaymentMethod.ach);
      expect(store.selectedCount, 1);

      store.toggleSelection(_item('a'));
      expect(store.isSelected('a'), isFalse);
      expect(store.selectedCount, 0);
    });

    test('setMethod implicitly selects and records the method', () {
      store.setMethod(_item('b'), PaymentMethod.wire);
      expect(store.isSelected('b'), isTrue);
      expect(store.methodFor(_item('b')), PaymentMethod.wire);
    });

    test('clearSelection empties the map', () {
      store.toggleSelection(_item('a'));
      store.toggleSelection(_item('b'));
      store.clearSelection();
      expect(store.hasSelection, isFalse);
    });
  });

  // The queue lists rows a payment run would refuse so an operator can see what
  // to go and clear — but `create_payment_run_for_invoices` hard-409s the WHOLE
  // batch on one of them, so they must be impossible to select. The rail-pinned
  // row is the other half: payable, but on exactly one rail.
  group('refusal verdict (blocked / required_method)', () {
    test('a blocked row cannot be ticked', () {
      final blocked = _item('1', blocked: true, blockedReason: 'duplicate');

      store.toggleSelection(blocked);

      expect(store.isSelected('1'), isFalse);
      expect(store.hasSelection, isFalse);
    });

    test('a blocked row cannot be selected by picking a method either', () {
      final blocked =
          _item('1', blocked: true, blockedReason: 'fully_credited');

      store.setMethod(blocked, PaymentMethod.wire);

      expect(store.isSelected('1'), isFalse);
    });

    test('a rail pinned to a code this build cannot name is unselectable', () {
      // Fail-closed: the backend named a rail we have no `PaymentMethod` for,
      // so honouring it is impossible and guessing ACH would stage the run on
      // the very rail the backend just refused.
      final pinnedUnknown = _item(
        '1',
        blockedReason: 'live_virtual_card',
        requiredMethod: 'rtp_instant',
      );

      store.toggleSelection(pinnedUnknown);

      expect(store.isSelected('1'), isFalse);
    });

    test('ticking a pinned row seeds its rail, not the ACH default', () {
      final pinned = _item(
        '1',
        blockedReason: 'live_virtual_card',
        requiredMethod: 'virtual_card',
      );

      store.toggleSelection(pinned);

      expect(store.isSelected('1'), isTrue);
      expect(store.methodFor(pinned), PaymentMethod.virtualCard);
    });

    test('setMethod on a pinned row ignores the requested rail', () {
      final pinned = _item(
        '1',
        blockedReason: 'live_virtual_card',
        requiredMethod: 'virtual_card',
      );

      store.setMethod(pinned, PaymentMethod.ach);

      expect(store.isSelected('1'), isTrue);
      expect(store.methodFor(pinned), PaymentMethod.virtualCard);
    });

    test('a fetch drops a selection for a row that became blocked', () async {
      ApiClient().debugConfigure(client: makeClient(queue: [_queueItem('1')]));
      await store.fetch();
      store.toggleSelection(_item('1'));
      expect(store.isSelected('1'), isTrue);

      // An exception was raised on it between refreshes.
      ApiClient().debugConfigure(
        client: makeClient(queue: [
          _queueItem('1', blocked: true, blockedReason: 'fraud_flag'),
        ]),
      );
      await store.fetch();

      expect(store.isSelected('1'), isFalse);
    });

    test('a fetch re-pins a selected row that gained a required rail',
        () async {
      ApiClient().debugConfigure(client: makeClient(queue: [_queueItem('1')]));
      await store.fetch();
      store.setMethod(_item('1'), PaymentMethod.wire);
      expect(store.methodFor(_item('1')), PaymentMethod.wire);

      // A virtual card was issued against it between refreshes.
      ApiClient().debugConfigure(
        client: makeClient(queue: [
          _queueItem('1',
              blockedReason: 'live_virtual_card',
              requiredMethod: 'virtual_card'),
        ]),
      );
      await store.fetch();

      expect(store.isSelected('1'), isTrue);
      expect(store.methodFor(store.queue.first), PaymentMethod.virtualCard);
    });

    test('the offline cache round-trips the verdict', () async {
      ApiClient().debugConfigure(
        client: makeClient(queue: [
          _queueItem('1', blocked: true, blockedReason: 'duplicate'),
        ]),
      );
      await store.fetch();

      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.fetch();

      expect(store.fromCache, isTrue);
      final cachedRow = store.queue.single;
      expect(cachedRow.blocked, isTrue);
      expect(cachedRow.blockedReason, 'duplicate');
      // …and it is still unselectable offline, not a fresh working checkbox.
      store.toggleSelection(cachedRow);
      expect(store.isSelected('1'), isFalse);
    });
  });

  group('createRunFromSelection', () {
    test('posts the selection, clears it, and returns the server message',
        () async {
      http.Request? captured;
      ApiClient().debugConfigure(
        client: makeClient(
          queue: [_queueItem('1')],
          onPost: (req) {
            if (req.url.path.endsWith('/payments/runs')) {
              captured = req;
              return _json({
                'id': 'run1',
                'status': 'draft',
                'requires_cfo_approval': false,
                'message': 'Payment run created with 2 payments',
              });
            }
            return _json({'items': [], 'total': 0});
          },
        ),
      );

      store.setMethod(_item('1'), PaymentMethod.wire);
      store.setMethod(_item('2'), PaymentMethod.check);

      final message = await store.createRunFromSelection();

      expect(message, contains('Payment run created'));
      expect(store.hasSelection, isFalse, reason: 'selection clears on success');

      // The POST body carries both invoices with their chosen methods.
      final body = jsonDecode(captured!.body) as Map<String, dynamic>;
      final items = body['items'] as List;
      expect(items, hasLength(2));
      final byInvoice = {
        for (final i in items) i['invoice_id']: i['method'],
      };
      expect(byInvoice['1'], 'wire');
      expect(byInvoice['2'], 'check');
    });

    test('sends a pinned row on its required rail, never the stored pick',
        () async {
      http.Request? captured;
      ApiClient().debugConfigure(
        client: makeClient(
          queue: [
            _queueItem('1',
                blockedReason: 'live_virtual_card',
                requiredMethod: 'virtual_card'),
          ],
          onPost: (req) {
            if (req.url.path.endsWith('/payments/runs')) {
              captured = req;
              return _json({'id': 'run1', 'message': 'Payment run created'});
            }
            return _json({'items': [], 'total': 0});
          },
        ),
      );
      await store.fetch();

      // The operator asks for ACH; the backend accepts only the card rail for
      // this invoice, so ACH must never reach the wire.
      store.setMethod(store.queue.single, PaymentMethod.ach);
      expect(await store.createRunFromSelection(), isNotNull);

      final items =
          (jsonDecode(captured!.body) as Map<String, dynamic>)['items'] as List;
      expect(items, hasLength(1));
      expect(items.single['invoice_id'], '1');
      expect(items.single['method'], 'virtual_card');
    });

    test('createRunFromSelection states the run total in its currency, and '
        'the CFO gate in the same sentence', () async {
      ApiClient().debugConfigure(
        client: _runsClient({
          'id': 'run1',
          'status': 'draft',
          'payment_count': 2,
          'total_amount': '1500.00',
          'currency': 'USD',
          'requires_cfo_approval': true,
          'message': 'server English',
        }),
      );
      store.toggleSelection(_item('1'));

      final message =
          await store.createRunFromSelection();

      expect(
        message,
        r'Payment run created with 2 payments totalling $1,500.00. '
        'CFO approval is required before it can be executed.',
      );
    });

    test('createRunFromSelection states a bare total when the run has no '
        'provable currency', () async {
      ApiClient().debugConfigure(
        client: _runsClient({
          'id': 'run1',
          'status': 'draft',
          'payment_count': 1,
          'total_amount': '250.00',
          'currency': null,
          'requires_cfo_approval': false,
        }),
      );
      store.toggleSelection(_item('1'));

      final message =
          await store.createRunFromSelection();

      expect(message, 'Payment run created with 1 payment totalling 250.00.');
    });

    test('returns null and keeps the selection when nothing is selected',
        () async {
      ApiClient().debugConfigure(client: makeClient());
      final message = await store.createRunFromSelection();
      expect(message, isNull);
    });

    test('returns null + records error on failure', () async {
      ApiClient().debugConfigure(
        client: makeClient(
          onPost: (req) {
            if (req.url.path.endsWith('/payments/runs')) {
              return http.Response('boom', 500);
            }
            return _json({});
          },
        ),
      );

      store.toggleSelection(_item('1'));
      final message = await store.createRunFromSelection();

      expect(message, isNull);
      expect(store.actionError, isNotNull);
      // Selection is preserved so the user can retry.
      expect(store.hasSelection, isTrue);
    });
  });

  group('run lifecycle', () {
    test('executeRun posts to /execute and returns the message', () async {
      var executeCalls = 0;
      ApiClient().debugConfigure(
        client: makeClient(
          onPost: (req) {
            if (req.url.path.endsWith('/execute')) {
              executeCalls++;
              return _json({'status': 'completed', 'message': 'Run executed'});
            }
            return _json({'items': [], 'total': 0});
          },
        ),
      );

      final message = await store.executeRun('run1');

      expect(executeCalls, 1);
      expect(message, contains('executed'));
    });

    test('approveRun posts to /approve and returns the message', () async {
      final approvePaths = <String>[];
      ApiClient().debugConfigure(
        client: makeClient(
          onPost: (req) {
            if (req.url.path.endsWith('/approve')) {
              approvePaths.add(req.url.path);
              return _json({
                'id': 'run1',
                'status': 'draft',
                'cfo_approved_by': 'u1',
                'cfo_approved_at': '2026-01-11T09:00:00',
                'message': 'Run approved by CFO',
              });
            }
            return _json({'items': [], 'total': 0});
          },
        ),
      );

      final message = await store.approveRun('run1');

      expect(approvePaths, ['/api/payments/runs/run1/approve']);
      expect(message, 'Run approved by CFO');
    });

    test('approveRun surfaces the server sentence on a refusal', () async {
      ApiClient().debugConfigure(
        client: makeClient(
          onPost: (req) {
            if (req.url.path.endsWith('/approve')) {
              return _json(
                {'detail': 'Run is already CFO-approved'},
                403,
              );
            }
            return _json({'items': [], 'total': 0});
          },
        ),
      );

      expect(await store.approveRun('run1'), isNull);
      expect(store.actionError, 'Run is already CFO-approved');
    });

    test('cancelRun posts to /cancel and returns the message', () async {
      var cancelCalls = 0;
      ApiClient().debugConfigure(
        client: makeClient(
          onPost: (req) {
            if (req.url.path.endsWith('/cancel')) {
              cancelCalls++;
              return _json({'status': 'cancelled', 'message': 'Run cancelled'});
            }
            return _json({'items': [], 'total': 0});
          },
        ),
      );

      final message = await store.cancelRun('run1');

      expect(cancelCalls, 1);
      expect(message, contains('cancelled'));
    });
  });

  // The no-rail pilot (issue #517, backend decisions §251): the store mirrors
  // the server's execution mode, failing closed, and drives the two record
  // doors the web payments page calls.
  group('execution mode', () {
    MockClient modeClient(http.Response Function() mode) =>
        makeClient(mode: mode);

    test('a processor tenant may dispatch', () async {
      ApiClient().debugConfigure(
        client: modeClient(
            () => _json({'mode': 'processor', 'reason': 'configured'})),
      );
      await store.fetch();
      expect(store.executionMode, 'processor');
      expect(store.canDispatch, isTrue);
      expect(store.isRecordOnly, isFalse);
    });

    test('a record-only tenant may not, and is confirmed record-only',
        () async {
      ApiClient().debugConfigure(
        client: modeClient(() => _json({
              'mode': 'record_only',
              'reason': 'no_processor_in_deployed_environment',
            })),
      );
      await store.fetch();
      expect(store.canDispatch, isFalse);
      expect(store.isRecordOnly, isTrue);
    });

    test('a failed read fails closed without taking the queue down', () async {
      ApiClient().debugConfigure(
        client: modeClient(() => _json({'detail': 'Forbidden'}, 403)),
      );
      await store.fetch();
      expect(store.executionMode, isNull);
      expect(store.canDispatch, isFalse, reason: 'unknown reads record-only');
      expect(store.isRecordOnly, isFalse,
          reason: 'unknown is not evidence of record-only');
      expect(store.queue, hasLength(1));
      expect(store.queueLoadError, isNull);
    });

    test('a mode this build does not know fails closed too', () async {
      ApiClient().debugConfigure(
        client: modeClient(() => _json({'mode': 'something_new'})),
      );
      await store.fetch();
      expect(store.canDispatch, isFalse);
      expect(store.isRecordOnly, isFalse);
    });

    test('reset forgets it', () async {
      ApiClient().debugConfigure(
        client: modeClient(() => _json({'mode': 'processor'})),
      );
      await store.fetch();
      store.reset();
      expect(store.executionMode, isNull);
      expect(store.canDispatch, isFalse);
    });
  });

  group('record outside FeohLedger', () {
    test('the queue row carries the record verdict and the amount owed', () {
      final row = PaymentQueueItem.fromJson({
        ..._queueItem('1'),
        'record_outside_refusal': 'external_payment_card_live',
        'payable_amount': '95.00',
      });
      expect(row.recordOutsideRefusal, 'external_payment_card_live');
      expect(row.payableAmountDisplay, '95.00');
      // Independent of the run builder's verdict.
      expect(row.blocked, isFalse);
    });

    test('the offline cache round-trips the record verdict', () async {
      ApiClient().debugConfigure(
        client: makeClient(queue: [
          {
            ..._queueItem('1'),
            'record_outside_refusal': 'external_payment_in_run',
            'payable_amount': '100.00',
          },
        ]),
      );
      await store.fetch();
      ApiClient().debugConfigure(
        client: MockClient((req) async => throw Exception('offline')),
      );
      await store.fetch();
      expect(store.fromCache, isTrue);
      expect(store.queue.single.recordOutsideRefusal, 'external_payment_in_run');
      expect(store.queue.single.payableAmountDisplay, '100.00');
    });

    test('recordOutside posts the web body (no amount) and re-reads the queue',
        () async {
      http.Request? posted;
      var queueReads = 0;
      ApiClient().debugConfigure(
        client: makeClient(
          queue: [],
          onPost: (req) {
            posted = req;
            return _json({'id': 'p1', 'status': 'completed'}, 201);
          },
          onGet: (req) {
            if (req.url.path.endsWith('/payments/queue')) queueReads++;
          },
        ),
      );
      store.toggleSelection(_item('1'));

      final outcome = await store.recordOutside(
        _item('1'),
        method: 'check',
        reference: '  CHK-1001 ',
        paidOn: '2026-11-16',
      );

      expect(outcome.ok, isTrue);
      expect(posted!.url.path, '/api/payments/record-outside');
      expect(jsonDecode(posted!.body), {
        'invoice_id': '1',
        'method': 'check',
        'reference': 'CHK-1001',
        'paid_on': '2026-11-16',
      });
      expect(queueReads, 1);
      expect(store.isSelected('1'), isFalse);
    });

    test('a coded refusal comes back with its detail and nothing is re-read',
        () async {
      var queueReads = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'POST') {
            return _json({
              'detail': {
                'code': 'external_payment_segregation',
                'message': 'Segregation of duties',
                'params': null,
              },
            }, 403);
          }
          if (req.url.path.endsWith('/payments/queue')) queueReads++;
          return _json({});
        }),
      );

      final outcome = await store.recordOutside(
        _item('1'),
        method: 'ach',
        reference: 'R',
        paidOn: '2026-11-16',
      );

      expect(outcome.ok, isFalse);
      expect((outcome.detail as Map)['code'], 'external_payment_segregation');
      expect(queueReads, 0);
    });

    test('loadRunRecordDetail reads the caller verdict from actions', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _json({
              'id': 'run1',
              'status': 'exported',
              'total_amount': '250.00',
              'currency': 'USD',
              'payment_count': 2,
              'requires_cfo_approval': false,
              'created_at': '2026-11-10T12:00:00',
              'actions': {
                'execute': {
                  'code': 'payments_record_only',
                  'message': 'x',
                  'params': null,
                },
                'record_outside': null,
              },
            })),
      );
      final detail = await store.loadRunRecordDetail('run1');
      expect(detail.run.status, PaymentRunStatus.exported);
      expect(detail.recordOutside.allowed, isTrue);
    });

    test('a run detail without the verdict offers nothing (fail closed)',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _json({
              'id': 'run1',
              'status': 'draft',
              'total_amount': '250.00',
              'created_at': '2026-11-10T12:00:00',
            })),
      );
      final detail = await store.loadRunRecordDetail('run1');
      expect(detail.recordOutside.answered, isFalse);
      expect(detail.recordOutside.allowed, isFalse);
    });

    test('recordRunOutside posts reference + paid_on and reports the count',
        () async {
      http.Request? posted;
      ApiClient().debugConfigure(
        client: makeClient(onPost: (req) {
          posted = req;
          return _json({
            'id': 'run1',
            'status': 'completed',
            'payment_count': 3,
            'total_amount': '300.00',
          });
        }),
      );

      final outcome = await store.recordRunOutside(
        'run1',
        reference: 'BATCH-9',
        paidOn: '2026-11-16',
      );

      expect(outcome.ok, isTrue);
      expect(outcome.paymentCount, 3);
      expect(posted!.url.path, '/api/payments/runs/run1/record-outside');
      expect(jsonDecode(posted!.body),
          {'reference': 'BATCH-9', 'paid_on': '2026-11-16'});
    });
  });

  // Each tab has its own load error and a refused action has its own slot, so
  // one failure can't put the wrong surface into an error state; each list is
  // ordered by its own request sequence.
  group('separate errors + request ordering', () {
    MockClient runsFailClient() => MockClient((req) async {
          final path = req.url.path;
          if (path.contains('/payments/runs')) {
            return _json({'detail': 'Runs unavailable'}, 500);
          }
          if (path.endsWith('/payments/queue')) {
            return _json({
              'items': [_queueItem('1')],
              'total': 1,
              'total_amount': 100.0,
              'total_savings': 0.0,
            });
          }
          if (path.endsWith('/payments/summary')) return _json(_summary);
          return _json({});
        });

    test('a failed runs read is the Runs tab\'s error only', () async {
      ApiClient().debugConfigure(client: runsFailClient());
      await store.fetch();
      await store.fetchRuns();

      expect(store.runsLoadError, isA<ApiException>());
      expect(store.queueLoadError, isNull);
      expect(store.actionError, isNull);
      expect(store.queue, hasLength(1));
      expect(store.runsLoading, isFalse);
    });

    test('a failed queue read with no cache is the Queue tab\'s error only',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path.contains('/payments/runs')) {
            return _json({'items': [], 'total': 0, 'page': 1});
          }
          throw Exception('offline');
        }),
      );
      await store.fetch();
      await store.fetchRuns();

      expect(store.queueLoadError, isNotNull);
      expect(store.runsLoadError, isNull);
      expect(store.actionError, isNull);
    });

    test('a refused action is an actionError, never a load error', () async {
      ApiClient().debugConfigure(
        client: makeClient(
          onPost: (req) => _json({'detail': 'Run is not a draft'}, 409),
        ),
      );
      await store.fetch();
      await store.fetchRuns();

      expect(await store.cancelRun('run1'), isNull);

      expect(store.actionError, 'Run is not a draft');
      expect(store.queueLoadError, isNull);
      expect(store.runsLoadError, isNull);
    });

    test('a slow, older runs read cannot overwrite a newer one', () async {
      final slow = Completer<http.Response>();
      var runsCalls = 0;
      Map<String, dynamic> run(String id) => {
            'id': id,
            'status': 'draft',
            'total_amount': 10.0,
            'currency': 'USD',
            'payment_count': 1,
            'requires_cfo_approval': false,
            'cfo_approved_at': null,
            'created_at': '2026-01-10T12:00:00',
          };
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          runsCalls++;
          return runsCalls == 1
              ? slow.future
              : _json({'items': [run('new')], 'total': 1, 'page': 1});
        }),
      );

      final first = store.fetchRuns();
      await store.fetchRuns();
      expect(store.runs.single.id, 'new');

      slow.complete(_json({'items': [run('old')], 'total': 1, 'page': 1}));
      await first;

      expect(store.runs.single.id, 'new');
      expect(store.runsLoading, isFalse);
    });

    test('a slow, older queue read cannot overwrite a newer one', () async {
      final slowQueue = Completer<http.Response>();
      var queueCalls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          final path = req.url.path;
          if (path.endsWith('/payments/queue')) {
            queueCalls++;
            final items = [_queueItem(queueCalls == 1 ? 'old' : 'new')];
            final body = _json({
              'items': items,
              'total': 1,
              'total_amount': 100.0,
              'total_savings': 0.0,
            });
            if (queueCalls == 1) {
              return slowQueue.future.then((_) => body);
            }
            return body;
          }
          if (path.endsWith('/payments/summary')) return _json(_summary);
          return _json({});
        }),
      );

      final first = store.fetch();
      await store.fetch();
      expect(store.queue.single.id, 'new');

      slowQueue.complete(http.Response('', 200));
      await first;

      expect(store.queue.single.id, 'new');
      expect(store.loading, isFalse);
    });

    test('a queue refresh does not discard an in-flight runs read', () async {
      final slowRuns = Completer<http.Response>();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          final path = req.url.path;
          if (path.contains('/payments/runs')) return slowRuns.future;
          if (path.endsWith('/payments/queue')) {
            return _json({
              'items': [_queueItem('1')],
              'total': 1,
              'total_amount': 100.0,
              'total_savings': 0.0,
            });
          }
          if (path.endsWith('/payments/summary')) return _json(_summary);
          return _json({});
        }),
      );

      final runs = store.fetchRuns();
      await store.fetch();
      slowRuns.complete(_json({
        'items': [
          {
            'id': 'r1',
            'status': 'exported',
            'total_amount': 10.0,
            'currency': 'USD',
            'payment_count': 1,
            'created_at': '2026-01-10T12:00:00',
          },
        ],
        'total': 1,
        'page': 1,
      }));
      await runs;

      expect(store.runs.single.id, 'r1');
    });
  });
}
