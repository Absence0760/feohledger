import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/stores/inspection_store.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
  jsonEncode(body),
  status,
  headers: {'content-type': 'application/json'},
);

http.Response _list(List<Map<String, dynamic>> items, {int? total}) =>
    _json({
      'items': items,
      'total': total ?? items.length,
      'page': 1,
      'page_size': 20,
    });

Map<String, dynamic> _inspectionJson(
  String id, {
  String number = 'QI-1',
  String result = 'pass',
  String? grNumber = 'GR-1',
}) => {
  'id': id,
  'inspection_number': number,
  'po_id': 'po1',
  'gr_id': 'gr1',
  'gr_number': grNumber,
  'result': result,
  'inspected_date': '2026-03-04',
  'inspector': 'Dana',
  'accepted_quantity': 10.0,
  'rejected_quantity': null,
  'deviation_notes': null,
  'status': 'completed',
  'created_at': '2026-03-04T09:00:00',
};

Map<String, dynamic> _receiptJson(String id, {String? poNumber = 'PO-9'}) => {
  'id': id,
  'gr_number': 'GR-$id',
  'po_id': 'po-$id',
  'po_number': poNumber,
  'received_date': '2026-03-01',
  'status': 'received',
  'line_count': 2,
};

/// `setResultFilter` fires `fetch()` without awaiting it (the screen's chips
/// are not async), so a test that asserts on the REQUEST has to let the
/// in-flight call reach the mock client.
Future<void> _settle() => Future<void>.delayed(const Duration(milliseconds: 20));

void main() {
  final store = InspectionStore.instance;

  setUp(() {
    InspectionStore.instance.reset();
    ApiClient().debugConfigure();
  });

  test('fetch populates the list and parses each row', () async {
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => _list([
          _inspectionJson('1', number: 'QI-A', result: 'fail'),
          _inspectionJson('2', number: 'QI-B'),
        ]),
      ),
    );

    await store.fetch();

    expect(store.inspections, hasLength(2));
    expect(store.loading, isFalse);
    expect(store.error, isNull);
    expect(store.inspections.first.inspectionNumber, 'QI-A');
    expect(store.inspections.first.result, InspectionResult.fail);
    expect(store.inspections.first.grNumber, 'GR-1');
    expect(store.inspections.first.acceptedQuantityDisplay, '10');
  });

  test('fetch surfaces an error and leaves the list empty', () async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => http.Response('boom', 500)),
    );

    await store.fetch();

    expect(store.error, isNotNull);
    expect(store.inspections, isEmpty);
    expect(store.loading, isFalse);
  });

  test('the outcome filter is sent to the SERVER, not applied to the page',
      () async {
    // A client-side filter over a paginated list hides every matching row past
    // the page boundary — the defect the approvals tab already shipped once.
    final queries = <String?>[];
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        queries.add(req.url.queryParameters['result']);
        return _list([_inspectionJson('1', result: 'fail')]);
      }),
    );

    await store.fetch();
    store.setResultFilter(InspectionResult.fail);
    await _settle();

    expect(store.resultFilter, InspectionResult.fail);
    expect(queries, [null, 'fail']);
  });

  test('clearing the filter drops the query param again', () async {
    final queries = <String?>[];
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        queries.add(req.url.queryParameters['result']);
        return _list([]);
      }),
    );

    store.setResultFilter(InspectionResult.partial);
    await _settle();
    store.setResultFilter(null);
    await _settle();

    expect(store.resultFilter, isNull);
    expect(queries, ['partial', null]);
  });

  test('every list request carries the canonical page params', () async {
    Map<String, String>? params;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        params = req.url.queryParameters;
        return _list([]);
      }),
    );

    await store.fetch();

    expect(params?['page'], '1');
    // `page_size`, not `per_page` — FastAPI silently drops an unknown param.
    expect(params?['page_size'], '20');
  });

  test('record POSTs the draft and refetches the list', () async {
    Map<String, dynamic>? posted;
    var listCalls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.method == 'POST') {
          posted = jsonDecode(req.body) as Map<String, dynamic>;
          return _json(_inspectionJson('new', number: 'QI-NEW'), 201);
        }
        listCalls++;
        return _list([_inspectionJson('new', number: 'QI-NEW')]);
      }),
    );

    final created = await store.record(
      const InspectionDraft(
        inspectionNumber: 'QI-NEW',
        grId: 'gr1',
        poId: 'po1',
        result: InspectionResult.partial,
        acceptedQuantity: '4.5',
      ),
    );

    expect(created, isNotNull);
    expect(created!.inspectionNumber, 'QI-NEW');
    expect(posted!['gr_id'], 'gr1');
    expect(posted!['accepted_quantity'], '4.5');
    // Refetched rather than prepended: the new row may not match the active
    // filter, and a blind prepend would put it there anyway.
    expect(listCalls, 1);
    expect(store.inspections, hasLength(1));
  });

  test('a refused record returns null and records the server refusal',
      () async {
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => _json({'detail': 'Your role does not permit this.'}, 403),
      ),
    );

    final created = await store.record(
      const InspectionDraft(
        inspectionNumber: 'QI-X',
        grId: 'gr1',
        result: InspectionResult.pass,
      ),
    );

    expect(created, isNull);
    expect(store.error, contains('Your role does not permit this.'));
  });

  test('getById returns the row, or throws without touching list state',
      () async {
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => _json(_inspectionJson('qi9', number: 'QI-9')),
      ),
    );
    expect((await store.getById('qi9'))?.inspectionNumber, 'QI-9');

    ApiClient().debugConfigure(
      client: MockClient((req) async => _json({'detail': 'Not found'}, 404)),
    );
    await expectLater(store.getById('nope'), throwsA(isA<ApiException>()));
    // The detail screen words it; the list's error state never sees it.
    expect(store.error, isNull);
  });

  test('loadReceiptOptions returns one page plus the whole-set total', () async {
    // The total is what lets the form say there are more receipts than the
    // picker is offering, instead of implying the list is everything.
    ApiClient().debugConfigure(
      client: MockClient(
        (req) async => _list([_receiptJson('a'), _receiptJson('b')], total: 57),
      ),
    );

    final page = await store.loadReceiptOptions();

    expect(page!.items, hasLength(2));
    expect(page.total, 57);
    expect(page.items.first.label, 'GR-a → PO-9');
  });

  test('loadReceiptOptions returns null with the error on failure', () async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => http.Response('boom', 500)),
    );

    expect(await store.loadReceiptOptions(), isNull);
    expect(store.error, isNotNull);
  });

  test('reset clears the list, the error and the filter', () async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([_inspectionJson('1')])),
    );
    store.setResultFilter(InspectionResult.fail);
    await store.fetch();
    expect(store.inspections, isNotEmpty);

    store.reset();

    expect(store.inspections, isEmpty);
    expect(store.resultFilter, isNull);
    expect(store.error, isNull);
  });
}
