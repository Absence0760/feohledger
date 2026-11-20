// A store's `error` is a sentence for a person, never an exception's
// `toString()` (`ApiException(403): …`, `ClientException: …`): the server's
// own `detail` when it sent one, else a localized description. Success
// toasts the stores build are localized the same way. The source-scan half
// of this rule is `store_error_text_guard_test.dart`.

import 'dart:convert';

import 'package:flutter/widgets.dart' show Locale;
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/locale_store.dart';
import 'package:feohledger_mobile/stores/payment_queue_store.dart';
import 'package:feohledger_mobile/stores/vendor_store.dart';
import 'package:feohledger_mobile/stores/workflow_store.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

/// Serves every GET a run action's refetch makes with an empty page, and
/// answers POSTs with [post].
MockClient _paymentsClient(Map<String, dynamic> post) => MockClient(
      (req) async => req.method == 'POST'
          ? _json(post)
          : _json({'items': [], 'total': 0, 'page': 1}),
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() {
    OfflineStore.instance.debugUseMemory();
    // `AuthStore.login` persists the tenant before it sends anything; without
    // the mock that write throws MissingPluginException, and the sign-in test
    // would be reading the storage failure, not the network one.
    FlutterSecureStorage.setMockInitialValues({});
  });

  setUp(() async {
    await LocaleStore.instance.setLocale(null);
    WorkflowStore.instance.reset();
    VendorStore.instance.reset();
    PaymentQueueStore.instance.reset();
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  tearDown(() async {
    await LocaleStore.instance.setLocale(null);
  });

  group('error text', () {
    test('a 403 with a detail yields the server sentence, not ApiException',
        () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async =>
              _json({'detail': 'Your role does not permit this.'}, 403),
        ),
      );

      await WorkflowStore.instance.fetch();

      expect(WorkflowStore.instance.error, 'Your role does not permit this.');
    });

    test('a refusal with no detail yields the localized status sentence',
        () async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response('<html>', 403)),
      );

      await WorkflowStore.instance.fetch();

      expect(
        WorkflowStore.instance.error,
        "The server couldn't complete the request (HTTP 403).",
      );
    });

    test('a network failure yields the localized unreachable sentence',
        () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => throw http.ClientException('Failed host lookup'),
        ),
      );

      await WorkflowStore.instance.fetch();

      expect(
        WorkflowStore.instance.error,
        "Couldn't reach the server. Check your connection and try again.",
      );
    });

    test('the description follows the chosen display language', () async {
      await LocaleStore.instance.setLocale(const Locale('de'));
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => throw http.ClientException('Failed host lookup'),
        ),
      );

      final ok = await VendorStore.instance.syncErp();

      expect(ok, isNull);
      expect(
        VendorStore.instance.actionError,
        'Der Server ist nicht erreichbar. Prüfen Sie Ihre Verbindung und '
        'versuchen Sie es erneut.',
      );
    });

    test('a failed sign-in on the network says so, without the exception',
        () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => throw http.ClientException('Connection refused'),
        ),
      );

      final result =
          await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

      expect(result.isSuccess, isFalse);
      expect(
        AuthStore.instance.error,
        "Couldn't reach the server. Check your connection and try again.",
      );
    });
  });

  group('success toasts', () {
    test('cancelRun states the released invoices, not the English message',
        () async {
      ApiClient().debugConfigure(
        client: _paymentsClient({
          'status': 'cancelled',
          'released_invoices': 3,
          'message': 'server English',
        }),
      );

      final message = await PaymentQueueStore.instance.cancelRun('run1');

      expect(
        message,
        'Draft run cancelled; 3 invoices returned to the queue.',
      );
    });

    test('executeRun states the counts, in the chosen language', () async {
      await LocaleStore.instance.setLocale(const Locale('fr'));
      ApiClient().debugConfigure(
        client: _paymentsClient({
          'status': 'completed',
          'provider': 'mock',
          'payments_completed': 2,
          'payments_in_flight': 1,
          'payments_failed': 0,
          'cards_issued': 1,
          'message': 'server English',
        }),
      );

      final message = await PaymentQueueStore.instance.executeRun('run1');

      // Each count takes its own plural ("2 terminés", "0 en échec" — French
      // treats 0 as singular), never a "terminé(s)" hedge; the provider the
      // run went through is named, as the server's sentence did.
      expect(
        message,
        'Lot de paiement exécuté via mock : 2 terminés, 1 en cours, '
        '0 en échec. 1 carte virtuelle émise.',
      );
    });

    test('executeRun pluralizes a single completion in Spanish', () async {
      await LocaleStore.instance.setLocale(const Locale('es'));
      ApiClient().debugConfigure(
        client: _paymentsClient({
          'status': 'completed',
          'provider': 'mock',
          'payments_completed': 1,
          'payments_in_flight': 0,
          'payments_failed': 2,
          'cards_issued': 0,
        }),
      );

      final message = await PaymentQueueStore.instance.executeRun('run1');

      expect(
        message,
        'Lote de pago ejecutado mediante mock: 1 completado, 0 en curso, '
        '2 fallidos.',
      );
    });

    test('approveRun is localized', () async {
      await LocaleStore.instance.setLocale(const Locale('de'));
      ApiClient().debugConfigure(
        client: _paymentsClient({'message': 'Run approved by CFO'}),
      );

      final message = await PaymentQueueStore.instance.approveRun('run1');

      expect(message, 'Lauf vom CFO freigegeben');
    });

    test('syncErp states the counts and the unlinked notes', () async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => req.method == 'POST'
              ? _json({
                  'success': true,
                  'message': 'server English',
                  'created': 2,
                  'updated': 1,
                  'unchanged': 4,
                  'conflicts': [
                    {'name': 'Acme'},
                  ],
                  'unlinked': [],
                  'restored': 0,
                })
              : _json({'items': [], 'total': 0, 'page': 1}),
        ),
      );

      final message = await VendorStore.instance.syncErp();

      expect(
        message,
        'Synced 2 new, 1 updated, 4 unchanged. '
        '1 not linked (the name matches more than one vendor).',
      );
    });
  });
}
