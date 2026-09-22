import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/models/cash_flow.dart';
import 'package:feohledger_mobile/stores/cash_flow_store.dart';

/// The analytics endpoints serialise money as EXACT decimal strings (the
/// backend never floats currency), so these fixtures do too.
Map<String, dynamic> _forecastBody({
  int horizonDays = 90,
  List<Map<String, dynamic>>? periods,
  Map<String, dynamic>? totals,
}) =>
    {
      'granularity': 'week',
      'horizon_days': horizonDays,
      'include_pending': true,
      'generated_at': '2026-06-20T00:00:00Z',
      'periods': periods ??
          [
            {
              'period': '2026-W26',
              'scheduled_amount': '3000.00',
              'committed_amount': '2000.00',
              'pending_amount': '1000.00',
              'discount_eligible_amount': '500.00',
              'count': 4,
            },
          ],
      'totals': totals ??
          {
            'scheduled_amount': '3000.00',
            'committed_amount': '2000.00',
            'pending_amount': '1000.00',
            'discount_eligible_amount': '500.00',
            'count': 4,
          },
    };

Map<String, dynamic> _positionBody({
  String opening = '10000.00',
  String? threshold = '5000.00',
  List<Map<String, dynamic>>? periods,
  List<Map<String, dynamic>>? breaches,
}) =>
    {
      'granularity': 'week',
      'horizon_days': 90,
      'opening_balance': opening,
      'opening_balance_source': 'settings',
      'opening_balance_currency': null,
      'threshold': threshold,
      'periods': periods ??
          [
            {
              'period': '2026-W26',
              'period_start': '2026-06-22',
              'period_end': '2026-06-28',
              'opening': '10000.00',
              'outflow': '3000.00',
              'inflow': '0.00',
              'closing': '7000.00',
              'below_threshold': false,
            },
          ],
      'breaches': breaches ?? [],
    };

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

/// Route the two endpoints the store hits to the right fixture.
MockClient _client({
  Map<String, dynamic>? forecast,
  Map<String, dynamic>? position,
}) =>
    MockClient((req) async {
      if (req.url.path.endsWith('/analytics/cashflow_forecast')) {
        return _json(forecast ?? _forecastBody());
      }
      if (req.url.path.endsWith('/analytics/cash_position')) {
        return _json(position ?? _positionBody());
      }
      return http.Response('not found', 404);
    });

void main() {
  final store = CashFlowStore.instance;

  setUp(() {
    store.reset();
    ApiClient().debugConfigure();
  });

  test('fetch combines forecast + cash position into one payload', () async {
    ApiClient().debugConfigure(client: _client());

    await store.fetch();

    expect(store.error, isNull);
    expect(store.loading, isFalse);
    final data = store.data!;
    expect(data.horizonDays, 90);
    expect(data.forecastPeriods, hasLength(1));
    expect(data.forecastPeriods.first.period, '2026-W26');
    // Money carried through as display strings, no float math on device.
    expect(data.totals.committedAmountDisplay, '2000.00');
    expect(data.openingBalanceDisplay, '10000.00');
    expect(data.openingBalanceSource, 'settings');
    expect(data.positionPeriods, hasLength(1));
    expect(data.projectedEndBalanceDisplay, '7000.00');
    expect(data.hasBreach, isFalse);
  });

  test('surfaces breaches when a period drops below the threshold', () async {
    ApiClient().debugConfigure(
      client: _client(
        position: _positionBody(
          periods: [
            {
              'period': '2026-W30',
              'opening': '6000.00',
              'outflow': '4000.00',
              'inflow': '0.00',
              'closing': '2000.00',
              'below_threshold': true,
            },
          ],
          breaches: [
            {
              'period': '2026-W30',
              'closing': '2000.00',
              'shortfall': '3000.00',
            },
          ],
        ),
      ),
    );

    await store.fetch();

    expect(store.data!.hasBreach, isTrue);
    expect(store.data!.breaches.first.shortfallDisplay, '3000.00');
    expect(store.data!.positionPeriods.first.belowThreshold, isTrue);
  });

  test('renders exact decimal strings verbatim, and a legacy float too',
      () async {
    // The analytics endpoints now send money as exact decimal strings; a
    // build running against an older backend still gets JSON numbers. Both
    // must render, and neither may be turned into a number on the device —
    // `moneyToDisplay` is display-only for exactly that reason.
    ApiClient().debugConfigure(
      client: _client(
        position: _positionBody(
          opening: '10000.50',
          periods: [
            {
              'period': '2026-W26',
              // A legacy float, as a pre-migration backend would send it.
              'opening': 10000.5,
              'outflow': 3000.25,
              'inflow': 0.0,
              'closing': 7000.25,
              'below_threshold': false,
            },
          ],
        ),
      ),
    );

    await store.fetch();

    // The exact string survives its trailing zero; a float renders as Dart
    // prints it. Neither path does arithmetic.
    expect(store.data!.openingBalanceDisplay, '10000.50');
    expect(store.data!.positionPeriods.first.openingDisplay, '10000.5');
    expect(store.data!.projectedEndBalanceDisplay, '7000.25');
  });

  group('unconverted counts', () {
    // Both legs keep a commitment with no rate into the reporting currency at
    // FACE value and count it. The two counts live on different payloads and
    // describe different figures (the forecast totals vs. the running
    // balance), so each is read from its own key.
    test('each leg reads its own count', () {
      final data = CashFlowData.fromJson(
        forecast: {
          ..._forecastBody(),
          'totals': {
            ...(_forecastBody()['totals'] as Map<String, dynamic>),
            'unconverted_count': 2,
          },
        },
        position: {..._positionBody(), 'unconverted_count': 5},
      );
      expect(data.totals.unconvertedCount, 2);
      expect(data.positionUnconvertedCount, 5);
    });

    test('absent keys (an older backend) read as nothing to disclose', () {
      final data = CashFlowData.fromJson(
        forecast: _forecastBody(),
        position: _positionBody(),
      );
      expect(data.totals.unconvertedCount, 0);
      expect(data.positionUnconvertedCount, 0);
    });

    test('an explicit null reads as zero too', () {
      final data = CashFlowData.fromJson(
        forecast: {
          ..._forecastBody(),
          'totals': {
            ...(_forecastBody()['totals'] as Map<String, dynamic>),
            'unconverted_count': null,
          },
        },
        position: {..._positionBody(), 'unconverted_count': null},
      );
      expect(data.totals.unconvertedCount, 0);
      expect(data.positionUnconvertedCount, 0);
    });
  });

  test('surfaces an error and clears data when the network fails', () async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => throw Exception('offline')),
    );

    await store.fetch();

    expect(store.error, isNotNull);
    expect(store.data, isNull);
  });

  test('setHorizon refetches with the new horizon param', () async {
    var lastHorizon = '';
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        lastHorizon = req.url.queryParameters['horizon_days'] ?? '';
        if (req.url.path.endsWith('/analytics/cashflow_forecast')) {
          return _json(_forecastBody(horizonDays: 30));
        }
        return _json(_positionBody());
      }),
    );

    await store.setHorizon(30);

    expect(store.horizonDays, 30);
    expect(lastHorizon, '30');
    expect(store.data, isNotNull);
  });

  test('setHorizon no-ops when the horizon is unchanged', () async {
    var calls = 0;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        calls++;
        if (req.url.path.endsWith('/analytics/cashflow_forecast')) {
          return _json(_forecastBody());
        }
        return _json(_positionBody());
      }),
    );

    // default horizon is 90 — selecting it again must not hit the API.
    await store.setHorizon(90);

    expect(calls, 0);
    expect(store.data, isNull);
  });
}
