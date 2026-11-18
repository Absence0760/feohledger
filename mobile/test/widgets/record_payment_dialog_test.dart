import 'dart:async';
import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/payment_queue.dart';
import 'package:feohledger_mobile/stores/payment_queue_store.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/widgets/record_payment_dialog.dart';

// The invoice "Record as paid" dialog on its own: what it requires before it
// will submit, the paid-on bounds, the in-flight lock, and the pre-fill from
// the ERP's report (the web's `erpPrefill`) with the split one-click
// (`POST /api/exceptions/{id}/record-erp-payment`, decisions §426).

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

DateTime _today() {
  final now = DateTime.now();
  return DateTime(now.year, now.month, now.day);
}

PaymentQueueItem _item({Map<String, dynamic>? erpReported}) =>
    PaymentQueueItem.fromJson({
      'id': 'inv1',
      'invoice_number': 'INV-1',
      'vendor_name': 'Acme Supplies',
      'amount': '100.00',
      'currency': 'USD',
      'status': 'approved',
      'payable_amount': '100.00',
      'record_outside_refusal': null,
      'erp_reported_payment': erpReported,
    });

/// A `paid_not_recorded` report as `_queue_erp_reported_payments` sends it.
Map<String, dynamic> _reported({
  String? method = 'check',
  String? reference = ' BILL-145 ',
  String? paidOn = '2026-10-20',
  List<Map<String, dynamic>>? parts,
  bool? splitStands,
}) =>
    {
      'exception_id': 'exc1',
      'erp_name': 'QuickBooks Online',
      'method': method,
      'reference': reference,
      'paid_on': paidOn,
      'amount': '100.00',
      'amount_currency': 'USD',
      'payment_parts': parts,
      'split_stands': splitStands,
    };

final _split = [
  {'method': 'check', 'amount': '60.00'},
  {'method': 'wire', 'amount': '40.00'},
];

/// Records every POST and answers it with [answer] (default: recorded).
List<http.Request> _configure({
  Future<http.Response> Function(http.Request)? answer,
}) {
  final posts = <http.Request>[];
  ApiClient().debugConfigure(
    client: MockClient((req) async {
      if (req.method == 'POST') {
        posts.add(req);
        return answer?.call(req) ??
            _json({'id': 'p1', 'status': 'completed'}, 201);
      }
      return _json({'items': [], 'total': 0});
    }),
  );
  return posts;
}

Future<void> _open(WidgetTester tester, PaymentQueueItem item) async {
  await tester.binding.setSurfaceSize(const Size(800, 1400));
  addTearDown(() => tester.binding.setSurfaceSize(null));
  await tester.pumpWidget(MaterialApp(
    localizationsDelegates: appLocalizationsDelegates,
    supportedLocales: AppLocalizations.supportedLocales,
    home: Scaffold(
      body: Builder(
        builder: (context) => TextButton(
          onPressed: () => showRecordInvoicePaymentDialog(context, item),
          child: const Text('open'),
        ),
      ),
    ),
  ));
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
}

Finder get _submit => find.byKey(const Key('record-submit'));

bool _enabled(WidgetTester tester) =>
    tester.widget<FilledButton>(_submit).onPressed != null;

void main() {
  setUp(() {
    PaymentQueueStore.instance.reset();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  group('validation', () {
    testWidgets('submit stays disabled until a non-blank reference is entered',
        (tester) async {
      _configure();
      await _open(tester, _item());

      // A fresh row starts on a rail (ACH) and today, so the reference is the
      // one thing the person must supply.
      expect(_enabled(tester), isFalse);
      await tester.enterText(find.byKey(const Key('record-reference')), '   ');
      await tester.pump();
      expect(_enabled(tester), isFalse);
      await tester.enterText(
          find.byKey(const Key('record-reference')), 'CHK-1001');
      await tester.pump();
      expect(_enabled(tester), isTrue);
    });

    testWidgets('a rail the ERP did not state must be chosen before submit',
        (tester) async {
      _configure();
      // `virtual_card` is FeohLedger's own card program, never a way to pay
      // outside it, so it pre-fills nothing.
      await _open(tester, _item(erpReported: _reported(method: 'virtual_card')));

      expect(find.text('BILL-145'), findsOneWidget);
      expect(_enabled(tester), isFalse);

      await tester.tap(find.byKey(const Key('record-method')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Wire').last);
      await tester.pumpAndSettle();
      expect(_enabled(tester), isTrue);
    });

    testWidgets('the paid-on picker runs from 2000 to today, never later',
        (tester) async {
      _configure();
      await _open(tester, _item());

      await tester.tap(find.text('Paid on'));
      await tester.pumpAndSettle();
      final picker = tester.widget<DatePickerDialog>(find.byType(DatePickerDialog));
      expect(picker.lastDate, _today());
      expect(picker.firstDate, DateTime(2000));
      expect(picker.initialDate, _today());
    });

    testWidgets('a future ERP date falls back to today', (tester) async {
      _configure();
      final tomorrow = _today().add(const Duration(days: 1));
      await _open(
        tester,
        _item(erpReported: _reported(paidOn: formatIsoDate(tomorrow))),
      );
      expect(find.text(formatDate(_today())), findsOneWidget);
      expect(find.text(formatDate(tomorrow)), findsNothing);
    });
  });

  testWidgets('while recording, submit, fields and cancel are locked',
      (tester) async {
    final gate = Completer<http.Response>();
    final posts = _configure(answer: (_) => gate.future);
    await _open(tester, _item());
    await tester.enterText(find.byKey(const Key('record-reference')), 'CHK-1');
    await tester.pump();

    await tester.tap(_submit);
    await tester.pump();

    expect(posts, hasLength(1));
    expect(_enabled(tester), isFalse);
    expect(find.text('Recording…'), findsOneWidget);
    expect(
      tester.widget<TextField>(find.byKey(const Key('record-reference'))).enabled,
      isFalse,
    );
    expect(
      tester.widget<TextButton>(find.widgetWithText(TextButton, 'Cancel'))
          .onPressed,
      isNull,
    );
    // A second tap while in flight sends nothing.
    await tester.tap(_submit, warnIfMissed: false);
    await tester.pump();
    expect(posts, hasLength(1));

    gate.complete(_json({'id': 'p1', 'status': 'completed'}, 201));
    await tester.pumpAndSettle();
    expect(find.byType(AlertDialog), findsNothing);
  });

  group('pre-filled from the ERP report', () {
    testWidgets('a stated rail, reference and date pre-fill and are posted',
        (tester) async {
      final posts = _configure();
      await _open(tester, _item(erpReported: _reported()));

      expect(
        find.text('Filled in from the payment QuickBooks Online reported. '
            'Check the details before you record it.'),
        findsOneWidget,
      );
      expect(find.byKey(const Key('record-erp-amount')), findsOneWidget);
      // One rail is just the method; no breakdown is shown for it.
      expect(find.byKey(const Key('record-erp-split')), findsNothing);
      expect(find.text(formatDate(DateTime(2026, 10, 20))), findsOneWidget);
      expect(_enabled(tester), isTrue);

      await tester.tap(_submit);
      await tester.pumpAndSettle();
      expect(posts.single.url.path, '/api/payments/record-outside');
      final body = jsonDecode(posts.single.body) as Map<String, dynamic>;
      expect(body['method'], 'check');
      expect(body['reference'], 'BILL-145');
      expect(body['paid_on'], '2026-10-20');
    });

    testWidgets(
        'a split that stands is the record: no rail is asked for, and it '
        'books through the exception one-click', (tester) async {
      final posts = _configure();
      await _open(
        tester,
        _item(erpReported: _reported(parts: _split, splitStands: true)),
      );

      expect(find.byKey(const Key('record-erp-split')), findsOneWidget);
      expect(find.text('How QuickBooks Online says it was paid'),
          findsOneWidget);
      expect(find.byKey(const Key('record-method')), findsNothing);
      expect(find.text('Split across the methods above'), findsOneWidget);
      expect(_enabled(tester), isTrue);

      await tester.tap(_submit);
      await tester.pumpAndSettle();
      expect(posts.single.url.path, '/api/exceptions/exc1/record-erp-payment');
      final body = jsonDecode(posts.single.body) as Map<String, dynamic>;
      expect(body.containsKey('method'), isFalse);
      expect(body['reference'], 'BILL-145');
      expect(body['paid_on'], '2026-10-20');
    });

    testWidgets('a split judged not to add up keeps the chosen rail',
        (tester) async {
      final posts = _configure();
      await _open(
        tester,
        _item(erpReported: _reported(parts: _split, splitStands: false)),
      );

      expect(find.byKey(const Key('record-erp-split')), findsOneWidget);
      expect(find.byKey(const Key('record-method')), findsOneWidget);
      await tester.tap(_submit);
      await tester.pumpAndSettle();
      expect(posts.single.url.path, '/api/payments/record-outside');
    });
  });

  group('QueueErpReportedPayment', () {
    test('a malformed breakdown reads as none, never a partial list', () {
      final r = QueueErpReportedPayment.fromJson(_reported(parts: [
        {'method': 'check', 'amount': '60.00'},
        {'method': 'wire', 'amount': 40},
      ]))!;
      expect(r.parts, isEmpty);
      expect(r.showsSplit, isFalse);
    });

    test('an unrecordable rail counts as not stated', () {
      final r = QueueErpReportedPayment.fromJson(_reported(parts: [
        {'method': 'virtual_card', 'amount': '60.00'},
        {'method': null, 'amount': '40.00'},
      ], splitStands: true))!;
      // Both parts are "not stated": one rail, so no split to show.
      expect(r.parts.map((p) => p.rail), [null, null]);
      expect(r.splitIsTheRecord, isFalse);
    });

    test('no exception id, no report', () {
      expect(QueueErpReportedPayment.fromJson({'method': 'check'}), isNull);
    });
  });
}
