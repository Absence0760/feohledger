import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/payment_store.dart';

Map<String, dynamic> _payment(String id, {Object amount = '100.00'}) => {
      'id': id,
      'invoice_id': 'inv-$id',
      'amount': amount,
      'currency': 'USD',
      'method': 'ach',
      'status': 'completed',
      'reference': 'REF-$id',
      'created_at': '2026-01-01T12:00:00',
    };

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'items': items, 'total': items.length, 'page': 1}),
      200,
      headers: {'content-type': 'application/json'},
    );

void main() {
  final store = PaymentStore.instance;

  setUpAll(() {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    store.reset();
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  test('fetch loads the payments with money kept as served strings', () async {
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        expect(req.url.path, '/api/payments');
        return _list([_payment('1', amount: '1234.56'), _payment('2')]);
      }),
    );

    await store.fetch();

    expect(store.payments, hasLength(2));
    expect(store.payments.first.amountDisplay, '1234.56');
    expect(store.loading, isFalse);
    expect(store.loadError, isNull);
    expect(store.actionError, isNull);
    expect(store.fromCache, isFalse);
  });

  test('a failed fetch with no cache is a loadError, never an actionError',
      () async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => http.Response(
            jsonEncode({'detail': 'Not permitted'}),
            403,
            headers: {'content-type': 'application/json'},
          )),
    );

    await store.fetch();

    expect(store.payments, isEmpty);
    expect(store.loadError, isA<ApiException>());
    expect(store.actionError, isNull);
    expect(store.loading, isFalse);
    expect(store.fromCache, isFalse);
  });

  test('offline, the last loaded list is served from the cache', () async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([_payment('1', amount: 99)])),
    );
    await store.fetch();
    expect(store.fromCache, isFalse);

    ApiClient().debugConfigure(
      client: MockClient((req) async => throw Exception('offline')),
    );
    await store.fetch();

    expect(store.payments, hasLength(1));
    expect(store.payments.single.id, '1');
    expect(store.payments.single.amountDisplay, '99');
    expect(store.fromCache, isTrue);
    // A cached copy stands in for the failure: no error state over it.
    expect(store.loadError, isNull);
  });

  test('a refresh that fails with no cache keeps the rows on screen', () async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([_payment('1')])),
    );
    await store.fetch();
    await OfflineStore.instance.clear();

    ApiClient().debugConfigure(
      client: MockClient((req) async => throw Exception('offline')),
    );
    await store.fetch();

    // The screen says "couldn't refresh" over these; it does not blank them.
    expect(store.payments, hasLength(1));
    expect(store.loadError, isNotNull);
    expect(store.fromCache, isFalse);
  });

  test('a slow, older fetch cannot overwrite a newer one', () async {
    final slow = Completer<http.Response>();
    var calls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        calls++;
        return calls == 1 ? slow.future : _list([_payment('new')]);
      }),
    );

    final first = store.fetch();
    final second = store.fetch();
    await second;
    expect(store.payments.single.id, 'new');

    // The first request lands last, with an older list. It is discarded.
    slow.complete(_list([_payment('old')]));
    await first;

    expect(store.payments.single.id, 'new');
    expect(store.loading, isFalse);
  });

  test('a superseded fetch that fails does not raise an error', () async {
    final slow = Completer<http.Response>();
    var calls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        calls++;
        return calls == 1 ? slow.future : _list([_payment('new')]);
      }),
    );

    final first = store.fetch();
    await store.fetch();
    slow.completeError(Exception('offline'));
    await first;

    expect(store.loadError, isNull);
    expect(store.payments.single.id, 'new');
  });

  test('reset drops the list and every flag', () async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => _list([_payment('1')])),
    );
    await store.fetch();

    store.reset();

    expect(store.payments, isEmpty);
    expect(store.loading, isFalse);
    expect(store.loadError, isNull);
    expect(store.actionError, isNull);
    expect(store.fromCache, isFalse);
  });
}
