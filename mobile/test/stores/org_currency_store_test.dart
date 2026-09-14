import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/models/organization.dart';
import 'package:feohledger_mobile/stores/org_currency_store.dart';

/// The org response with only the keys `org_settings_view.NON_ADMIN_SETTINGS`
/// projects to a non-admin, since that is what this store's consumers get.
Map<String, dynamic> _orgJson(Map<String, dynamic> settings) => {
      'id': 'org1',
      'name': 'Acme Corp',
      'slug': 'acme',
      'plan': 'pro',
      'created_at': '2026-01-01T00:00:00',
      'settings': settings,
    };

http.Response _ok(Map<String, dynamic> body) => http.Response(
      jsonEncode(body),
      200,
      headers: {'content-type': 'application/json'},
    );

OrgSettings _settings(Map<String, dynamic> settings) =>
    OrgSettings.fromJson(_orgJson(settings));

void main() {
  group('OrgSettings.resolvedReportingCurrency — the rung chain', () {
    // The order is not a preference: it mirrors
    // `currency_conversion.resolve_reporting_currency`, which decides what the
    // API's cross-currency rollups are actually denominated in. A client that
    // resolves a different rung labels a correct figure with a wrong code.
    test('rung 1 — reporting_currency wins outright', () {
      final s = _settings({
        'reporting_currency': 'GBP',
        'payments': {'home_currency': 'EUR'},
        'invoice_defaults': {'currency': 'USD'},
      });
      expect(s.resolvedReportingCurrency, 'GBP');
    });

    test('rung 2 — payments.home_currency, which the follow-up omitted', () {
      // The entry describing this work called `invoice_defaults.currency` the
      // second rung. It is the THIRD. An org whose only signal is
      // `payments.home_currency` would otherwise resolve to nothing on mobile
      // while the server denominated its figures in EUR.
      final s = _settings({
        'payments': {'home_currency': 'EUR'},
        'invoice_defaults': {'currency': 'USD'},
      });
      expect(s.resolvedReportingCurrency, 'EUR');
    });

    test('rung 3 — invoice_defaults.currency, the last a client can see', () {
      final s = _settings({
        'invoice_defaults': {'currency': 'ZAR'},
      });
      expect(s.resolvedReportingCurrency, 'ZAR');
    });

    test('all three unset resolves to NULL, not to a default', () {
      // The load-bearing case (`docs/decisions.md` §119). The fourth backend
      // rung is `settings.reporting_currency_default`, which no client can
      // read, so substituting USD here would be a guess indistinguishable
      // from a configured answer.
      expect(_settings({}).resolvedReportingCurrency, isNull);
      expect(
        _settings({
          'payments': <String, dynamic>{},
          'invoice_defaults': <String, dynamic>{},
        }).resolvedReportingCurrency,
        isNull,
      );
    });

    test('a rung that is present but unusable falls THROUGH, not out', () {
      // An empty string or a truncated code is a misconfiguration, not an
      // answer — the same `usableCode` length rule the web resolver applies.
      final s = _settings({
        'reporting_currency': '',
        'payments': {'home_currency': 'EU'},
        'invoice_defaults': {'currency': 'jpy'},
      });
      expect(s.resolvedReportingCurrency, 'JPY');
    });

    test('the edit-form default is SEPARATE from the resolved code', () {
      // `defaultCurrency` substitutes USD because a form field needs a value.
      // Sharing that defaulting with the resolver is exactly what §119 warns
      // against: a rung that always answers makes the rungs after it dead.
      final s = _settings({'reporting_currency': 'GBP'});
      expect(s.configuredInvoiceCurrency, isNull);
      expect(s.defaultCurrency, 'USD');
      expect(s.resolvedReportingCurrency, 'GBP');
    });
  });

  group('OrgCurrencyStore', () {
    final store = OrgCurrencyStore.instance;

    setUp(() {
      store.reset();
      ApiClient().debugConfigure();
    });

    test('resolves and caches the code after one load', () async {
      var calls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          calls++;
          return _ok(_orgJson({'reporting_currency': 'GBP'}));
        }),
      );

      await store.ensureLoaded();
      expect(store.currency, 'GBP');
      expect(store.loaded, isTrue);

      await store.ensureLoaded();
      expect(calls, 1, reason: 'a resolved currency is not re-fetched');
    });

    test('concurrent callers share one in-flight request', () async {
      var calls = 0;
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          calls++;
          return _ok(_orgJson({'reporting_currency': 'EUR'}));
        }),
      );

      await Future.wait([
        store.ensureLoaded(),
        store.ensureLoaded(),
        store.ensureLoaded(),
      ]);

      expect(calls, 1);
      expect(store.currency, 'EUR');
    });

    test('an org declaring nothing usable stays NULL and is settled', () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _ok(_orgJson({}))),
      );

      await store.ensureLoaded();

      expect(store.currency, isNull);
      expect(store.loaded, isTrue,
          reason: 'the question was answered — the answer is "none"');
    });

    test('a failure keeps NULL, does not throw, and stays retryable', () async {
      // A dashboard must not die over a label, and a transient failure is not
      // evidence that the org has no currency — so the store does not settle.
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('boom', 500)),
      );

      await store.ensureLoaded();

      expect(store.currency, isNull);
      expect(store.loaded, isFalse);

      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _ok(_orgJson({'reporting_currency': 'ZAR'})),
        ),
      );
      await store.ensureLoaded();
      expect(store.currency, 'ZAR');
    });

    test('notifies listeners when the code lands', () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _ok(_orgJson({'reporting_currency': 'JPY'})),
        ),
      );
      var notified = 0;
      void listener() => notified++;
      store.addListener(listener);
      addTearDown(() => store.removeListener(listener));

      await store.ensureLoaded();

      // A figure rendered bare while this was in flight has to pick up its
      // symbol when it lands, which needs the notification.
      expect(notified, 1);
    });

    test('reset drops the code so the next account cannot inherit it',
        () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _ok(_orgJson({'reporting_currency': 'GBP'})),
        ),
      );
      await store.ensureLoaded();
      expect(store.currency, 'GBP');

      store.reset();

      expect(store.currency, isNull);
      expect(store.loaded, isFalse);
    });
  });
}
