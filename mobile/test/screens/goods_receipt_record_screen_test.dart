import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_de.dart';
import 'package:feohledger_mobile/models/goods_receipt.dart';
import 'package:feohledger_mobile/screens/goods_receipt_record_screen.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/goods_receipt_store.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
  jsonEncode(body),
  status,
  headers: {'content-type': 'application/json'},
);

Map<String, dynamic> _poRow(String id, {String status = 'open'}) => {
  'id': id,
  'po_number': 'PO-$id',
  'vendor_name': 'Acme Supply',
  'status': status,
};

Map<String, dynamic> _poDetail(
  String id, {
  String status = 'open',
  List<Map<String, dynamic>>? lines,
}) => {
  ..._poRow(id, status: status),
  'quantity_received_total': 9.3,
  'line_items':
      lines ??
      [
        // 10 ordered, 9.3 in: 0.7 outstanding — worked in ten-thousandths,
        // never 0.6999999999999993.
        {
          'id': 'l1',
          'description': 'Widgets',
          'sku': 'W-1',
          'quantity': 10.0,
          'quantity_received': 9.3,
        },
        // Fully received: starts blank, not a 0 that would claim a shortfall.
        {
          'id': 'l2',
          'description': 'Bolts',
          'sku': null,
          'quantity': 5.0,
          'quantity_received': 5.0,
        },
      ],
};

/// A backend that lists one PO, serves its detail, and answers the POST with
/// [onPost]. Captures each POST so a test can read the body and headers.
MockClient _backend({
  String poStatus = 'open',
  List<Map<String, dynamic>>? lines,
  http.Response Function(http.Request)? onPost,
  List<http.Request>? posts,
}) => MockClient((req) async {
  if (req.method == 'GET' && req.url.path == '/api/purchase-orders') {
    return _json({
      'items': [_poRow('1', status: poStatus)],
      'total': 1,
      'page': 1,
      'page_size': 50,
    });
  }
  if (req.method == 'GET' && req.url.path == '/api/purchase-orders/1') {
    return _json(_poDetail('1', status: poStatus, lines: lines));
  }
  if (req.method == 'POST' && req.url.path == '/api/goods-receipts') {
    posts?.add(req);
    return onPost?.call(req) ??
        _json({'id': 'gr1', 'gr_number': 'GR-PO-1-1', 'line_items': []}, 201);
  }
  return _json({}, 404);
});

/// Hosts the screen behind a push so its pop result can be read.
class _Host extends StatefulWidget {
  const _Host();
  @override
  State<_Host> createState() => _HostState();
}

class _HostState extends State<_Host> {
  String? recorded;
  @override
  Widget build(BuildContext context) => Scaffold(
    body: Column(
      children: [
        Text('recorded:${recorded ?? '-'}'),
        TextButton(
          onPressed: () async {
            final n = await Navigator.of(context).push<String>(
              MaterialPageRoute(
                builder: (_) => const GoodsReceiptRecordScreen(),
              ),
            );
            setState(() => recorded = n);
          },
          child: const Text('open'),
        ),
      ],
    ),
  );
}

Widget _app({Locale? locale}) => MaterialApp(
  localizationsDelegates: appLocalizationsDelegates,
  supportedLocales: AppLocalizations.supportedLocales,
  locale: locale,
  home: const _Host(),
);

Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 40 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

Future<void> _open(WidgetTester tester, {Locale? locale}) async {
  tester.view.physicalSize = const Size(1080, 3000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(_app(locale: locale));
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
  await _pumpUntil(tester, find.byKey(const Key('gr-line-qty-l1')));
}

String _fieldText(WidgetTester tester, String key) =>
    tester.widget<TextFormField>(find.byKey(Key(key))).controller!.text;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    GoodsReceiptStore.instance.reset();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  group('GoodsReceiptRecordScreen', () {
    testWidgets('lays out the picked PO with outstanding quantities pre-filled',
        (tester) async {
      ApiClient().debugConfigure(client: _backend());
      await _open(tester);

      expect(find.text('PO-1 — Acme Supply'), findsOneWidget);
      expect(find.textContaining('Widgets'), findsOneWidget);
      expect(find.text('Outstanding: 0.7'), findsOneWidget);
      expect(find.text('Ordered: 10'), findsOneWidget);
      expect(find.text('Received so far: 9.3'), findsOneWidget);
      // Outstanding pre-fills; a fully received line starts blank.
      expect(_fieldText(tester, 'gr-line-qty-l1'), '0.7');
      expect(_fieldText(tester, 'gr-line-qty-l2'), '');
      expect(find.byKey(const Key('gr-segregation-note')), findsOneWidget);

      // More than is outstanding is allowed, and said beside the line.
      await tester.enterText(find.byKey(const Key('gr-line-qty-l1')), '2');
      await tester.pump();
      expect(
        find.text(
          'More than is outstanding. Matching will flag this as an over-receipt.',
        ),
        findsOneWidget,
      );
    });

    testWidgets('submits quantities as strings with an idempotency key and '
        'pops with the receipt number', (tester) async {
      final posts = <http.Request>[];
      ApiClient().debugConfigure(client: _backend(posts: posts));
      await _open(tester);

      await tester.enterText(find.byKey(const Key('gr-line-qty-l1')), '0.25');
      await tester.enterText(find.byKey(const Key('gr-line-qty-l2')), '0');
      await tester.tap(find.byKey(const Key('gr-submit')));
      await tester.pumpAndSettle();

      expect(posts, hasLength(1));
      final body = jsonDecode(posts.single.body) as Map<String, dynamic>;
      expect(body['po_id'], '1');
      expect(body['received_date'], matches(RegExp(r'^\d{4}-\d{2}-\d{2}$')));
      expect(body.containsKey('gr_number'), isFalse);
      final lines = (body['lines'] as List).cast<Map<String, dynamic>>();
      expect(lines, [
        {'po_line_item_id': 'l1', 'quantity_received': '0.25'},
        {'po_line_item_id': 'l2', 'quantity_received': '0'},
      ]);
      // Strings on the wire, never JSON numbers.
      for (final l in lines) {
        expect(l['quantity_received'], isA<String>());
      }
      expect(posts.single.headers['Idempotency-Key'], isNotEmpty);
      expect(find.text('recorded:GR-PO-1-1'), findsOneWidget);
    });

    testWidgets('states a coded refusal in the reader\'s language and keeps '
        'the form open', (tester) async {
      ApiClient().debugConfigure(
        client: _backend(
          onPost: (_) => _json({
            'detail': {
              'code': 'goods_receipt_number_taken',
              'message': 'A goods receipt numbered DN-7 already exists.',
              'params': {'grNumber': 'DN-7'},
            },
          }, 409),
        ),
      );
      await _open(tester, locale: const Locale('de'));

      await tester.enterText(find.byKey(const Key('gr-number')), 'DN-7');
      await tester.tap(find.byKey(const Key('gr-submit')));
      await tester.pumpAndSettle();

      final de = AppLocalizationsDe();
      expect(
        find.text(de.codedRefusalGoodsReceiptNumberTaken('DN-7')),
        findsOneWidget,
      );
      expect(find.byKey(const Key('gr-submit-error')), findsOneWidget);
      expect(find.byType(GoodsReceiptRecordScreen), findsOneWidget);
    });

    testWidgets('a retry of the same form reuses its idempotency key',
        (tester) async {
      final posts = <http.Request>[];
      var first = true;
      ApiClient().debugConfigure(
        client: _backend(
          posts: posts,
          onPost: (_) {
            if (!first) return _json({'gr_number': 'GR-PO-1-1'}, 200);
            first = false;
            return _json({'detail': 'Service unavailable'}, 503);
          },
        ),
      );
      await _open(tester);

      await tester.tap(find.byKey(const Key('gr-submit')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('gr-submit-error')), findsOneWidget);
      await tester.tap(find.byKey(const Key('gr-submit')));
      await tester.pumpAndSettle();

      expect(posts, hasLength(2));
      expect(
        posts[0].headers['Idempotency-Key'],
        posts[1].headers['Idempotency-Key'],
      );
    });

    testWidgets('refuses to submit with nothing received', (tester) async {
      final posts = <http.Request>[];
      ApiClient().debugConfigure(client: _backend(posts: posts));
      await _open(tester);

      await tester.enterText(find.byKey(const Key('gr-line-qty-l1')), '');
      await tester.tap(find.byKey(const Key('gr-submit')));
      await tester.pumpAndSettle();

      expect(posts, isEmpty);
      expect(find.byKey(const Key('gr-lines-error')), findsOneWidget);
    });

    testWidgets('a cancelled PO says so and offers no submit', (tester) async {
      ApiClient().debugConfigure(client: _backend(poStatus: 'cancelled'));
      tester.view.physicalSize = const Size(1080, 3000);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
      await tester.pumpWidget(_app());
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await _pumpUntil(tester, find.byKey(const Key('gr-po-cancelled')));

      expect(find.byKey(const Key('gr-po-cancelled')), findsOneWidget);
      final submit = tester.widget<FilledButton>(
        find.byKey(const Key('gr-submit')),
      );
      expect(submit.onPressed, isNull);
    });

    testWidgets('a PO with no lines takes free-text lines', (tester) async {
      final posts = <http.Request>[];
      ApiClient().debugConfigure(
        client: _backend(lines: const [], posts: posts),
      );
      tester.view.physicalSize = const Size(1080, 3000);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
      await tester.pumpWidget(_app());
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await _pumpUntil(tester, find.byKey(const Key('gr-free-desc-0')));

      await tester.enterText(find.byKey(const Key('gr-free-desc-0')), 'Pallet');
      await tester.enterText(find.byKey(const Key('gr-free-qty-0')), '3');
      await tester.tap(find.byKey(const Key('gr-submit')));
      await tester.pumpAndSettle();

      final body = jsonDecode(posts.single.body) as Map<String, dynamic>;
      expect(body['lines'], [
        {'description': 'Pallet', 'quantity_received': '3'},
      ]);
    });
  });

  group('GoodsReceiptDraft', () {
    test('serializes quantities as the typed strings, never doubles', () {
      final json = GoodsReceiptDraft(
        poId: 'po1',
        receivedDate: DateTime(2026, 3, 5),
        grNumber: '  DN-1 ',
        lines: const [
          GoodsReceiptLineDraft(poLineItemId: 'l1', quantityReceived: '0.1'),
          GoodsReceiptLineDraft(
            poLineItemId: 'l2',
            quantityReceived: '12345678.1234',
          ),
        ],
      ).toJson();
      expect(json['received_date'], '2026-03-05');
      expect(json['gr_number'], 'DN-1');
      final encoded = jsonEncode(json);
      expect(encoded, contains('"quantity_received":"0.1"'));
      expect(encoded, contains('"quantity_received":"12345678.1234"'));
    });

    test('outstanding is exact in ten-thousandths', () {
      final line = ReceivablePOLine.fromJson({
        'id': 'l1',
        'quantity': 10.0,
        'quantity_received': 9.3,
      });
      expect(quantityString(line.outstandingScaled), '0.7');
      final over = ReceivablePOLine.fromJson({
        'id': 'l2',
        'quantity': 1.0,
        'quantity_received': 3.0,
      });
      expect(over.outstandingScaled, 0);
      final unordered = ReceivablePOLine.fromJson({
        'id': 'l3',
        'quantity': null,
        'quantity_received': 0,
      });
      expect(unordered.orderedScaled, isNull);
      expect(unordered.outstandingScaled, 0);
    });
  });

  group('record gate', () {
    Future<void> loginAs(List<String> roles) async {
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path == '/api/auth/login') {
            return _json({'access_token': 'tok'});
          }
          return _json({
            'id': 'u1',
            'email': 'demo@acme.com',
            'full_name': 'Demo',
            'organization_id': 'org1',
            'roles': roles,
          });
        }),
      );
      await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
    }

    test('admin, ap_manager and ap_clerk may record; cfo may not', () async {
      for (final role in ['admin', 'ap_manager', 'ap_clerk']) {
        await loginAs([role]);
        expect(AuthStore.instance.canRecordGoodsReceipt, isTrue, reason: role);
      }
      await loginAs(['cfo']);
      expect(AuthStore.instance.canRecordGoodsReceipt, isFalse);
    });
  });
}
