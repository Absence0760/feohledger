import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/screens/payment_queue_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/payment_queue_store.dart';

/// Wraps a screen in a MaterialApp carrying the localization delegates so
/// `AppLocalizations.of(context)` resolves (defaults to English).
Widget _localized(Widget home) => MaterialApp(
      localizationsDelegates: appLocalizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: home,
    );

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

/// One row of `GET /api/payments/queue`, including the three refusal fields
/// `app/api/payments.py::payment_queue` stamps from
/// `services/payment_runs.run_refusal_reasons`.
Map<String, dynamic> _queueItem(
  String id, {
  double amount = 100,
  bool blocked = false,
  String? blockedReason,
  String? requiredMethod,
  String currency = 'USD',
}) =>
    {
      'id': id,
      'invoice_number': 'INV-$id',
      'vendor_name': 'Vendor $id',
      'amount': amount,
      'currency': currency,
      'due_date': '2026-02-01',
      'status': 'approved',
      'is_overdue': false,
      'discount_eligible': false,
      'blocked': blocked,
      'blocked_reason': blockedReason,
      'required_method': requiredMethod,
    };

/// `GET /api/payments/summary`. Its money keys are in the org's REPORTING
/// currency, which the response names itself (`"currency": reporting_currency`)
/// because none of these totals belongs to a single row — unlike a queue row,
/// which is in its own invoice's currency. One screen, two denominations.
Map<String, dynamic> _summaryJson({String currency = 'USD'}) => {
      'total_paid': '1000.00',
      'total_pending': '200.00',
      'payment_count': 5,
      'total_rebates': '12.00',
      'queue_count': 2,
      'currency': currency,
    };

Map<String, dynamic> _me(
  List<String> roles, {
  List<String> permissions = const [],
}) =>
    {
      'id': 'u1',
      'email': 'demo@acme.com',
      'full_name': 'Demo User',
      'organization_id': 'org1',
      'roles': roles,
      'permissions': permissions,
    };

http.Response _queueResponse(List<Map<String, dynamic>> items) => _json({
      'items': items,
      'total': items.length,
      'total_amount': 0,
      'total_savings': 0,
    });

/// One row of `GET /api/payments/runs/`, mirroring `PaymentRunResponse` in
/// `backend/app/schemas/payment.py` field for field. Hand-written fixtures
/// carrying only the keys the model reads are how the CFO-approval gate stayed
/// "covered" while the response schema declared no such field.
Map<String, dynamic> _runResponse({
  String id = 'run1',
  String status = 'draft',
  double totalAmount = 5000.0,
  String? currency = 'USD',
  int paymentCount = 2,
  bool requiresCfoApproval = false,
  String? cfoApprovedAt,
}) =>
    {
      'id': id,
      'status': status,
      // A STRING, like the endpoint sends (`str(run.total_amount)`) — the
      // backend never floats money across the boundary, and a numeric fixture
      // here renders `5000.0` where production renders `5000.00`.
      'total_amount': totalAmount.toStringAsFixed(2),
      // `_one_currency` over the run's legs. `null` is the server saying it
      // could not PROVE one, not that it omitted the field.
      'currency': currency,
      'initiated_by': 'u1',
      'executed_at': null,
      'created_at': '2026-01-10T12:00:00',
      'payment_count': paymentCount,
      'payments_completed': 0,
      'payments_failed': 0,
      'payments_in_flight': 0,
      'payments_pending': paymentCount,
      'requires_cfo_approval': requiresCfoApproval,
      'cfo_approved_at': cfoApprovedAt,
    };

/// `GET /api/payments/execution-mode`'s `mode`. Defaults to `processor` so the
/// screen's Execute tests keep reading a tenant FeohLedger sends payments for;
/// `null` makes the read fail, which must hide Execute (fail closed).
MockClient _screenClient({
  List<Map<String, dynamic>>? queue,
  List<Map<String, dynamic>>? runs,
  http.Response Function(http.Request req)? onPost,
  String summaryCurrency = 'USD',
  String? mode = 'processor',
  Map<String, dynamic>? runDetail,
}) {
  return MockClient((req) async {
    if (req.method == 'POST') {
      return onPost?.call(req) ?? _json({'message': 'ok'});
    }
    final path = req.url.path;
    if (path.endsWith('/payments/execution-mode')) {
      return mode == null
          ? _json({'detail': 'unavailable'}, 503)
          : _json({'mode': mode, 'reason': 'configured'});
    }
    if (runDetail != null && RegExp(r'/payments/runs/[^/]+$').hasMatch(path)) {
      return _json(runDetail);
    }
    if (path.endsWith('/payments/queue')) {
      return _queueResponse(queue ?? [_queueItem('1')]);
    }
    if (path.endsWith('/payments/summary')) {
      return _json(_summaryJson(currency: summaryCurrency));
    }
    if (path.contains('/payments/runs')) {
      final items = runs ?? <Map<String, dynamic>>[];
      return _json({'items': items, 'total': items.length});
    }
    return _json({});
  });
}

Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 25 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

Future<void> _pumpUntilTrue(WidgetTester tester, bool Function() c) async {
  for (var i = 0; i < 25 && !c(); i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    PaymentQueueStore.instance.reset();
    await OfflineStore.instance.clear();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  Future<void> loginThen(
    List<String> roles,
    MockClient screenClient, {
    List<String> permissions = const [],
  }) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.url.path == '/api/auth/login') {
          return _json({'access_token': 'tok'});
        }
        return _json(_me(roles, permissions: permissions));
      }),
    );
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
    ApiClient().debugConfigure(client: screenClient);
  }

  testWidgets('renders the summary KPI cards + queue rows once loaded',
      (tester) async {
    await loginThen(
      ['ap_manager'],
      _screenClient(queue: [_queueItem('1'), _queueItem('2')]),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Total Paid'));

    expect(find.text('Total Paid'), findsOneWidget);
    expect(find.text('Card Rebates'), findsOneWidget);
    // Two queue rows -> two vendor names rendered.
    expect(find.text('Vendor 1'), findsOneWidget);
    expect(find.text('Vendor 2'), findsOneWidget);
  });

  testWidgets('selecting a row reveals the Create Run bar', (tester) async {
    await loginThen(['ap_manager'], _screenClient(queue: [_queueItem('1')]));

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Vendor 1'));

    expect(find.text('Create Run'), findsNothing);

    await tester.tap(find.byType(Checkbox).first);
    await tester.pump();

    expect(find.text('Create Run'), findsOneWidget);
    expect(find.textContaining('1 invoice selected'), findsOneWidget);
  });

  testWidgets('Create Run POSTs the selection to /payments/runs',
      (tester) async {
    http.Request? captured;
    await loginThen(
      ['ap_manager'],
      _screenClient(
        queue: [_queueItem('1')],
        onPost: (req) {
          if (req.url.path.endsWith('/payments/runs')) {
            captured = req;
            return _json({
              'id': 'run1',
              'status': 'draft',
              'requires_cfo_approval': false,
              'message': 'Payment run created',
            });
          }
          return _json({'items': [], 'total': 0});
        },
      ),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Vendor 1'));

    await tester.tap(find.byType(Checkbox).first);
    await tester.pump();
    await tester.tap(find.text('Create Run'));
    await _pumpUntilTrue(tester, () => captured != null);

    expect(captured, isNotNull);
    final body = jsonDecode(captured!.body) as Map<String, dynamic>;
    expect((body['items'] as List).first['invoice_id'], '1');
  });

  testWidgets('cfo read-only context: no checkbox column when role cannot '
      'manage payments is still managed (cfo CAN manage)', (tester) async {
    // CFO can manage payments per the backend gate, so the checkbox IS shown.
    await loginThen(['cfo'], _screenClient(queue: [_queueItem('1')]));

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Vendor 1'));

    expect(find.byType(Checkbox), findsOneWidget);
  });

  testWidgets('empty queue renders the empty state', (tester) async {
    await loginThen(['ap_manager'], _screenClient(queue: []));

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('No invoices awaiting payment'));

    expect(find.text('No invoices awaiting payment'), findsOneWidget);
  });

  // Both tabs are AsyncListBody, each with its own load error: one shared
  // `error` used to put the Queue tab into its error state when only the runs
  // read failed, and the queue's error was a hand-built column.
  testWidgets('a failed queue load says why and offers Retry, which reloads',
      (tester) async {
    var fail = true;
    final ok = _screenClient(queue: [_queueItem('1')]);
    await loginThen(
      ['ap_manager'],
      MockClient((req) async {
        if (fail && req.url.path.endsWith('/payments/queue')) {
          return _json({'detail': 'Queue unavailable'}, 503);
        }
        return ok.send(req).then(http.Response.fromStream);
      }),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Retry'));

    expect(
      find.text("Couldn't load the payment queue: Queue unavailable"),
      findsOneWidget,
    );
    // Never the empty state over a failed read.
    expect(find.text('No invoices awaiting payment'), findsNothing);

    fail = false;
    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await _pumpUntil(tester, find.text('Vendor 1'));

    expect(find.text('Vendor 1'), findsOneWidget);
    expect(find.text('Retry'), findsNothing);
  });

  testWidgets('a failed runs load is the Runs tab\'s error, not the queue\'s',
      (tester) async {
    final ok = _screenClient(queue: [_queueItem('1')]);
    await loginThen(
      ['ap_manager'],
      MockClient((req) async {
        if (req.url.path.contains('/payments/runs')) {
          return _json({'detail': 'Runs unavailable'}, 503);
        }
        return ok.send(req).then(http.Response.fromStream);
      }),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Vendor 1'));

    // The queue is live: no error state, no "couldn't refresh" banner.
    expect(find.text('Vendor 1'), findsOneWidget);
    expect(find.text('Retry'), findsNothing);

    await tester.tap(find.text('Runs'));
    await tester.pumpAndSettle();

    expect(
      find.text("Couldn't load payment runs: Runs unavailable"),
      findsOneWidget,
    );
    expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
    expect(find.text('No payment runs'), findsNothing);
  });

  // The CFO-approval gate. `GET /api/payments/runs/` must declare
  // `requires_cfo_approval` / `cfo_approved_at` on `PaymentRunResponse` for
  // this to be reachable at all — without them FastAPI strips the keys, both
  // parse false for every run, and Execute goes to the server only to come
  // back 403.
  group('CFO-approval gate', () {
    Future<void> openRunsTab(WidgetTester tester) async {
      await _pumpUntil(tester, find.text('Runs'));
      await tester.tap(find.text('Runs'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
    }

    testWidgets('a run awaiting sign-off is flagged and Execute never reaches '
        'the server', (tester) async {
      var executeCalls = 0;
      await loginThen(
        ['ap_manager'],
        _screenClient(
          queue: [],
          runs: [_runResponse(requiresCfoApproval: true)],
          onPost: (req) {
            if (req.url.path.endsWith('/execute')) executeCalls++;
            return _json({'message': 'ok'});
          },
        ),
      );

      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await openRunsTab(tester);

      expect(find.textContaining('CFO approval required'), findsOneWidget);

      await tester.tap(find.byType(PopupMenuButton<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Execute'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));

      expect(executeCalls, 0, reason: 'the pre-flight gate must short-circuit');
      expect(
        find.text('This run needs CFO approval before it can be executed.'),
        findsOneWidget,
      );
    });

    testWidgets('a signed-off run executes normally', (tester) async {
      var executeCalls = 0;
      await loginThen(
        ['cfo'],
        _screenClient(
          queue: [],
          runs: [
            _runResponse(
              requiresCfoApproval: true,
              cfoApprovedAt: '2026-01-11T09:00:00',
            ),
          ],
          onPost: (req) {
            if (req.url.path.endsWith('/execute')) executeCalls++;
            return _json({'message': 'Payment run executed'});
          },
        ),
      );

      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await openRunsTab(tester);

      expect(find.textContaining('CFO approval required'), findsNothing);

      await tester.tap(find.byType(PopupMenuButton<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Execute'));
      await tester.pumpAndSettle();
      // Confirm the "Execute payment run?" dialog.
      await tester.tap(find.widgetWithText(FilledButton, 'Execute'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));

      expect(executeCalls, 1);
    });

    testWidgets('a 403 from the server surfaces its sentence, not raw JSON',
        (tester) async {
      await loginThen(
        ['ap_manager'],
        _screenClient(
          queue: [],
          // The server says sign-off is not required (e.g. the threshold moved
          // after the run was drafted), so the pre-flight gate lets it through
          // and the refusal comes back over the wire.
          runs: [_runResponse()],
          onPost: (req) {
            if (req.url.path.endsWith('/execute')) {
              return _json(
                {'detail': 'This run exceeds the CFO-approval threshold.'},
                403,
              );
            }
            return _json({'message': 'ok'});
          },
        ),
      );

      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await openRunsTab(tester);

      await tester.tap(find.byType(PopupMenuButton<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Execute'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Execute'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));

      expect(
        find.text(
          'Failed to execute: This run exceeds the CFO-approval threshold.',
        ),
        findsOneWidget,
      );
      expect(find.textContaining('{"detail"'), findsNothing);
    });
  });

  // CFO sign-off on an over-threshold run. The Pay tab has always been visible
  // to a CFO and has always rendered "This run needs CFO approval before it can
  // be executed" — but the one person who could clear that message had no
  // control to do it with, because the app never called `/approve`.
  group('CFO run approval', () {
    Future<void> openRunsTab(WidgetTester tester) async {
      await _pumpUntil(tester, find.text('Runs'));
      await tester.tap(find.text('Runs'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
    }

    Future<void> openRunMenu(WidgetTester tester) async {
      await tester.tap(find.byType(PopupMenuButton<String>));
      await tester.pumpAndSettle();
    }

    testWidgets('a CFO can sign a pending run off — confirm names the run and '
        'its total, then POSTs /approve', (tester) async {
      final approvePaths = <String>[];
      await loginThen(
        ['cfo'],
        _screenClient(
          queue: [],
          runs: [_runResponse(requiresCfoApproval: true)],
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
            return _json({'message': 'ok'});
          },
        ),
      );

      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await openRunsTab(tester);
      await openRunMenu(tester);

      expect(find.text('Approve as CFO'), findsOneWidget);
      await tester.tap(find.text('Approve as CFO'));
      await tester.pumpAndSettle();

      // Money-path authorization: confirm-then-act, and the dialog names the
      // run (created date + payment count) and its total.
      //
      // The total wears the run's OWN currency, the one `_one_currency`
      // derived from its legs and the response names. It used to read
      // `\$5,000.00` from a module-level dollar formatter — a `\$` nothing
      // supported on the one dialog that authorizes execution — and then
      // nothing at all, because the model dropped the field. Both are wrong
      // for a run the server can denominate. See `docs/decisions.md` §160.
      expect(find.text('Approve payment run?'), findsOneWidget);
      expect(
        find.text(
          'Sign off the run created Jan 10, 2026 — 2 payments totalling '
          '\$5,000.00. This authorizes execution; it moves no money.',
        ),
        findsOneWidget,
      );
      expect(approvePaths, isEmpty, reason: 'nothing sent before confirming');

      await tester.tap(find.widgetWithText(FilledButton, 'Approve'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));

      expect(approvePaths, ['/api/payments/runs/run1/approve']);
      expect(find.text('Run approved by CFO'), findsOneWidget);
    });

    testWidgets('dismissing the confirm dialog sends nothing', (tester) async {
      var approveCalls = 0;
      await loginThen(
        ['cfo'],
        _screenClient(
          queue: [],
          runs: [_runResponse(requiresCfoApproval: true)],
          onPost: (req) {
            if (req.url.path.endsWith('/approve')) approveCalls++;
            return _json({'message': 'ok'});
          },
        ),
      );

      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await openRunsTab(tester);
      await openRunMenu(tester);
      await tester.tap(find.text('Approve as CFO'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));

      expect(approveCalls, 0);
    });

    testWidgets('an ap_manager never sees the sign-off action', (tester) async {
      await loginThen(
        ['ap_manager'],
        _screenClient(
          queue: [],
          runs: [_runResponse(requiresCfoApproval: true)],
        ),
      );

      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await openRunsTab(tester);
      await openRunMenu(tester);

      expect(find.text('Approve as CFO'), findsNothing);
      // The other two run actions are unaffected.
      expect(find.text('Execute'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
    });

    testWidgets('an admin without the cfo role never sees it either',
        (tester) async {
      // The backend gate is `require_roles(ROLE_CFO)`, which does NOT
      // special-case admin — showing the item to an admin would 403 every tap.
      await loginThen(
        ['admin'],
        _screenClient(
          queue: [],
          runs: [_runResponse(requiresCfoApproval: true)],
        ),
      );

      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await openRunsTab(tester);
      await openRunMenu(tester);

      expect(find.text('Approve as CFO'), findsNothing);
    });

    testWidgets('an already signed-off run offers no second sign-off',
        (tester) async {
      await loginThen(
        ['cfo'],
        _screenClient(
          queue: [],
          runs: [
            _runResponse(
              requiresCfoApproval: true,
              cfoApprovedAt: '2026-01-11T09:00:00',
            ),
          ],
        ),
      );

      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await openRunsTab(tester);
      await openRunMenu(tester);

      expect(find.text('Approve as CFO'), findsNothing);
    });

    testWidgets('a run that needs no sign-off offers none', (tester) async {
      await loginThen(
        ['cfo'],
        _screenClient(queue: [], runs: [_runResponse()]),
      );

      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await openRunsTab(tester);
      await openRunMenu(tester);

      expect(find.text('Approve as CFO'), findsNothing);
    });

    testWidgets('a refusal surfaces the server sentence, not raw JSON',
        (tester) async {
      await loginThen(
        ['cfo'],
        _screenClient(
          queue: [],
          runs: [_runResponse(requiresCfoApproval: true)],
          onPost: (req) {
            if (req.url.path.endsWith('/approve')) {
              return _json(
                {'detail': 'You cannot approve a run you created.'},
                403,
              );
            }
            return _json({'message': 'ok'});
          },
        ),
      );

      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await openRunsTab(tester);
      await openRunMenu(tester);
      await tester.tap(find.text('Approve as CFO'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Approve'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));

      expect(
        find.text('Failed to approve: You cannot approve a run you created.'),
        findsOneWidget,
      );
      expect(find.textContaining('{"detail"'), findsNothing);
    });
  });

  // A row `POST /api/payments/runs` would refuse must be impossible to select:
  // it 409s the WHOLE batch, so one held invoice used to take every other
  // invoice in the draft down with it. It still RENDERS — an operator has to
  // see what to go and clear — with the reason spelled out.
  testWidgets('a credit-mismatch row names its own reason', (tester) async {
    await loginThen(
      ['ap_manager'],
      _screenClient(queue: [
        _queueItem('1', blocked: true, blockedReason: 'applied_credit_mismatch'),
      ]),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Vendor 1'));

    expect(
      find.text("An applied credit memo no longer matches this invoice's vendor "
          'or currency — correct the invoice first'),
      findsOneWidget,
    );
    expect(find.textContaining('applied_credit_mismatch'), findsNothing);
  });

  testWidgets('a blocked row is unselectable and says why', (tester) async {
    await loginThen(
      ['ap_manager'],
      _screenClient(queue: [
        _queueItem('1', blocked: true, blockedReason: 'fully_credited'),
      ]),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Vendor 1'));

    // The reason is rendered from the app's own label map, never the raw code.
    expect(find.text('Fully covered by credit memos — nothing to pay'),
        findsOneWidget);
    expect(find.textContaining('fully_credited'), findsNothing);

    final checkbox = tester.widget<Checkbox>(find.byType(Checkbox));
    expect(checkbox.onChanged, isNull, reason: 'blocked checkbox is disabled');

    // Tapping the row itself is inert too — the Create Run bar never appears.
    await tester.tap(find.text('Vendor 1'));
    await tester.pump();
    expect(PaymentQueueStore.instance.hasSelection, isFalse);
    expect(find.text('Create Run'), findsNothing);
  });

  testWidgets('a row with no recorded currency names that, not the generic',
      (tester) async {
    await loginThen(
      ['ap_manager'],
      _screenClient(queue: [
        _queueItem('1', blocked: true, blockedReason: 'currency_unknown'),
      ]),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Vendor 1'));

    expect(
      find.text('No currency recorded on this invoice — set its currency first'),
      findsOneWidget,
    );
    expect(find.textContaining('currency_unknown'), findsNothing);
    final checkbox = tester.widget<Checkbox>(find.byType(Checkbox));
    expect(checkbox.onChanged, isNull, reason: 'blocked checkbox is disabled');
  });

  testWidgets('an unresolved-exception row renders its own reason',
      (tester) async {
    await loginThen(
      ['ap_manager'],
      _screenClient(queue: [
        _queueItem('1', blocked: true, blockedReason: 'payment_reconciliation'),
      ]),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Vendor 1'));

    expect(
      find.text('Earlier payment unreconciled — may still be in flight'),
      findsOneWidget,
    );
  });

  // A live virtual card already claims the invoice: every rail BUT
  // `virtual_card` is refused, because that one converges onto the existing
  // card instead of opening a second outflow. The row stays selectable, pinned.
  testWidgets('a rail-pinned row offers no method picker and pays on the pin',
      (tester) async {
    http.Request? captured;
    await loginThen(
      ['ap_manager'],
      _screenClient(
        queue: [
          _queueItem('1',
              blockedReason: 'live_virtual_card',
              requiredMethod: 'virtual_card'),
        ],
        onPost: (req) {
          if (req.url.path.endsWith('/payments/runs')) {
            captured = req;
            return _json({
              'id': 'run1',
              'status': 'draft',
              'requires_cfo_approval': false,
              'message': 'Payment run created',
            });
          }
          return _json({'items': [], 'total': 0});
        },
      ),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Vendor 1'));

    // Selectable, and the pinned rail is stated rather than offered as one of
    // four options the backend would refuse three of.
    expect(find.text('Pay by Virtual Card'), findsOneWidget);
    expect(
      find.text('A live virtual card claims this invoice — pay it by card'),
      findsOneWidget,
    );

    await tester.tap(find.byType(Checkbox));
    await tester.pump();
    expect(find.byType(DropdownButton<PaymentMethod>), findsNothing,
        reason: 'a pinned row must not offer a rail choice');

    await tester.tap(find.text('Create Run'));
    await _pumpUntilTrue(tester, () => captured != null);

    final items =
        (jsonDecode(captured!.body) as Map<String, dynamic>)['items'] as List;
    expect(items.single['method'], 'virtual_card');
  });

  testWidgets('a rail this build cannot name is treated as unselectable',
      (tester) async {
    await loginThen(
      ['ap_manager'],
      _screenClient(queue: [
        _queueItem('1',
            blockedReason: 'live_virtual_card',
            requiredMethod: 'rtp_instant'),
      ]),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Vendor 1'));

    final checkbox = tester.widget<Checkbox>(find.byType(Checkbox));
    expect(checkbox.onChanged, isNull);
    expect(find.textContaining('rtp_instant'), findsNothing);
  });

  testWidgets('a pinned row with no reason code renders the rail, not "null"',
      (tester) async {
    // The reason and the pin are separate fields; nothing guarantees a future
    // backend sends both. Interpolating an absent reason once put the literal
    // string "null" in the row's screen-reader announcement.
    await loginThen(
      ['ap_manager'],
      _screenClient(queue: [
        _queueItem('1', requiredMethod: 'virtual_card'),
      ]),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Vendor 1'));

    expect(find.text('Pay by Virtual Card'), findsOneWidget);
    expect(find.textContaining('null'), findsNothing);
    expect(find.bySemanticsLabel(RegExp('null')), findsNothing);
  });

  testWidgets('the three money denominations on this screen stay separate',
      (tester) async {
    // One screen, three answers to "what currency is this in", and a single
    // module-level `NumberFormat.currency(symbol: '\$')` used to give all
    // three the same wrong one:
    //   * the KPI bar is in the org's REPORTING currency, named by
    //     `/payments/summary` itself;
    //   * a queue row is in its own INVOICE's currency;
    //   * a run total is in the ONE currency its legs agree on, which
    //     `_one_currency` derives and the run response names.
    await loginThen(
      ['cfo'],
      _screenClient(
        summaryCurrency: 'ZAR',
        queue: [_queueItem('1', amount: 100, currency: 'EUR')],
        runs: [_runResponse(totalAmount: 5000, currency: 'GBP')],
      ),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Total paid'));

    // Reporting currency on the KPI bar…
    expect(find.text('R1,000.00'), findsOneWidget);
    expect(find.text('R200.00'), findsOneWidget);
    // …the row's own currency on the row…
    expect(find.text('€100.00'), findsOneWidget);
    // …and the run's own on the run total.
    await tester.tap(find.widgetWithText(Tab, 'Runs'));
    await tester.pumpAndSettle();
    expect(find.text('£5,000.00'), findsWidgets);

    expect(find.textContaining(r'$'), findsNothing);
  });

  testWidgets('a run whose currency the server could not prove renders bare, '
      'including on the execute confirmation', (tester) async {
    // `_one_currency` returns `null` for a run with no payments, one whose
    // invoices carry no currency, and a legacy run whose legs disagree. That
    // total is denominated in nothing real, so the digits stand alone — a
    // substituted symbol would be a wrong number that looks right
    // (`docs/decisions.md` §79/§82, §160).
    await loginThen(
      ['admin'],
      _screenClient(
        queue: [],
        runs: [_runResponse(totalAmount: 5000, currency: null)],
      ),
    );

    await tester.pumpWidget(_localized(const PaymentQueueScreen()));
    await _pumpUntil(tester, find.text('Runs'));
    await tester.tap(find.text('Runs'));
    await tester.pumpAndSettle();

    // `find.text` is an exact match, so a `$5,000.00` would not satisfy it.
    expect(find.text('5000.00'), findsWidgets);

    await tester.tap(find.byType(PopupMenuButton<String>));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Execute'));
    await tester.pumpAndSettle();
    expect(
      find.text('This sends 5000.00 via the configured payment processor.'),
      findsOneWidget,
    );
  });

  // ── Record-only mode (the no-rail pilot, issue #517, decisions §251) ────
  // The web hides Execute on a tenant whose payments FeohLedger only records
  // and offers "Record as paid" instead; mobile reads the same server signals:
  // the execution mode, the queue row's `record_outside_refusal`, and the run
  // detail's `actions.record_outside` verdict.
  group('record-only mode', () {
    const recorder = ['payment.record_external', 'payment.execute'];

    // The banner, the KPI bar, the queue rows with their record actions and
    // the record dialog's form do not all fit the default 800x600 test
    // surface; a phone-height-and-more surface keeps every control on screen.
    Future<void> tall(WidgetTester tester) async {
      await tester.binding.setSurfaceSize(const Size(800, 1400));
      addTearDown(() => tester.binding.setSurfaceSize(null));
    }

    Future<void> openRunMenu(WidgetTester tester) async {
      await tester.tap(find.text('Runs'));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(PopupMenuButton<String>));
      await tester.pumpAndSettle();
    }

    testWidgets('a record-only tenant sees the banner and no Execute',
        (tester) async {
      await tall(tester);
      await loginThen(
        ['ap_manager'],
        _screenClient(mode: 'record_only', runs: [_runResponse()]),
        permissions: recorder,
      );
      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await _pumpUntil(tester, find.text('Vendor 1'));

      final banner = find.byKey(const Key('record-only-banner'));
      expect(banner, findsOneWidget);
      // It is the header of the tab's own scroll view, so it scrolls away
      // with the rows rather than holding height above them at a large text
      // size, and it sends the reader to the web for a run's bank file.
      expect(
        find.ancestor(of: banner, matching: find.byType(NestedScrollView)),
        findsOneWidget,
      );
      expect(
        find.text('FeohLedger records payments but never sends them: pay '
            "from your bank or ERP, then record each one here (a run's bank "
            'file is on the web app).'),
        findsOneWidget,
      );
      expect(find.text('Record as paid'), findsOneWidget);

      await openRunMenu(tester);
      // The Runs tab leads with it too.
      expect(banner, findsOneWidget);
      expect(
        find.ancestor(of: banner, matching: find.byType(NestedScrollView)),
        findsOneWidget,
      );
      expect(find.text('Execute'), findsNothing);
      expect(find.text('Record run as paid…'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
    });

    testWidgets('a processor tenant keeps Execute and shows no banner',
        (tester) async {
      await tall(tester);
      await loginThen(
        ['ap_manager'],
        _screenClient(runs: [_runResponse()]),
        permissions: recorder,
      );
      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await _pumpUntil(tester, find.text('Vendor 1'));

      expect(find.byKey(const Key('record-only-banner')), findsNothing);
      // Recording a cheque is offered in both modes, as on the web.
      expect(find.text('Record as paid'), findsOneWidget);

      await openRunMenu(tester);
      expect(find.text('Execute'), findsOneWidget);
      expect(find.text('Record run as paid…'), findsOneWidget);
    });

    testWidgets('an unknown mode hides Execute but claims nothing',
        (tester) async {
      await tall(tester);
      await loginThen(
        ['ap_manager'],
        _screenClient(mode: null, runs: [_runResponse()]),
        permissions: recorder,
      );
      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await _pumpUntil(tester, find.text('Vendor 1'));

      expect(find.byKey(const Key('record-only-banner')), findsNothing);
      await openRunMenu(tester);
      expect(find.text('Execute'), findsNothing);
    });

    testWidgets('without payment.record_external there is no record action',
        (tester) async {
      await tall(tester);
      await loginThen(
        ['ap_manager'],
        _screenClient(mode: 'record_only', runs: [_runResponse()]),
        permissions: const ['payment.execute'],
      );
      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await _pumpUntil(tester, find.text('Vendor 1'));

      expect(find.text('Record as paid'), findsNothing);
      await openRunMenu(tester);
      expect(find.text('Record run as paid…'), findsNothing);
    });

    testWidgets('a refused row shows the action disabled with its reason',
        (tester) async {
      await tall(tester);
      await loginThen(
        ['ap_manager'],
        _screenClient(mode: 'record_only', queue: [
          {
            ..._queueItem('1'),
            'record_outside_refusal': 'external_payment_card_live',
          },
        ]),
        permissions: recorder,
      );
      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await _pumpUntil(tester, find.text('Vendor 1'));

      final button = tester.widget<TextButton>(
        find.ancestor(
          of: find.text('Record as paid'),
          matching: find.byWidgetPredicate((w) => w is TextButton),
        ),
      );
      expect(button.onPressed, isNull);
      expect(
        find.text('A live virtual card is issued against this invoice. '
            'Cancel the card before recording a payment made another way.'),
        findsOneWidget,
      );
    });

    testWidgets('an unverified-vendor row the RUN refuses can still be recorded',
        (tester) async {
      await tall(tester);
      // `blocked` is the run builder's verdict; recording reads its own.
      await loginThen(
        ['ap_manager'],
        _screenClient(mode: 'record_only', queue: [
          {
            ..._queueItem('1', blocked: true, blockedReason: 'vendor_not_active'),
            'record_outside_refusal': null,
          },
        ]),
        permissions: recorder,
      );
      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await _pumpUntil(tester, find.text('Vendor 1'));

      final button = tester.widget<TextButton>(
        find.ancestor(
          of: find.text('Record as paid'),
          matching: find.byWidgetPredicate((w) => w is TextButton),
        ),
      );
      expect(button.onPressed, isNotNull);
    });

    testWidgets('recording a row posts to /record-outside and confirms',
        (tester) async {
      await tall(tester);
      http.Request? posted;
      await loginThen(
        ['ap_manager'],
        _screenClient(
          mode: 'record_only',
          queue: [
            {..._queueItem('1'), 'payable_amount': '100.00'},
          ],
          onPost: (req) {
            posted = req;
            return _json({'id': 'p1', 'status': 'completed'}, 201);
          },
        ),
        permissions: recorder,
      );
      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await _pumpUntil(tester, find.text('Vendor 1'));

      await tester.tap(find.text('Record as paid'));
      await tester.pumpAndSettle();
      expect(find.text('Record a payment made outside FeohLedger'),
          findsOneWidget);
      expect(find.text('Amount owed: \$100.00'), findsOneWidget);

      // Submit stays disabled until a reference is entered.
      final submit = find.widgetWithText(FilledButton, 'Record as paid');
      expect(tester.widget<FilledButton>(submit).onPressed, isNull);
      await tester.enterText(
          find.widgetWithText(TextField, 'Reference'), 'CHK-1001');
      await tester.pump();
      await tester.tap(submit);
      await tester.pumpAndSettle();

      expect(posted!.url.path, '/api/payments/record-outside');
      final body = jsonDecode(posted!.body) as Map<String, dynamic>;
      expect(body['invoice_id'], '1');
      expect(body['method'], 'ach');
      expect(body['reference'], 'CHK-1001');
      expect(body.containsKey('amount'), isFalse);
      expect(find.text('Invoice INV-1 recorded as paid'), findsOneWidget);
    });

    testWidgets('a refusal from the server is stated beside the form',
        (tester) async {
      await tall(tester);
      await loginThen(
        ['ap_manager'],
        _screenClient(
          mode: 'record_only',
          onPost: (req) => _json({
            'detail': {
              'code': 'external_payment_segregation',
              'message': 'server english',
              'params': null,
            },
          }, 403),
        ),
        permissions: recorder,
      );
      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await _pumpUntil(tester, find.text('Vendor 1'));

      await tester.tap(find.text('Record as paid'));
      await tester.pumpAndSettle();
      await tester.enterText(find.widgetWithText(TextField, 'Reference'), 'R');
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, 'Record as paid'));
      await tester.pumpAndSettle();

      expect(
        find.text('Segregation of duties: you created or shaped this invoice, '
            'so a different user must record its payment.'),
        findsOneWidget,
      );
      // The dialog stays open on the refusal.
      expect(find.text('Record a payment made outside FeohLedger'),
          findsOneWidget);
    });

    Map<String, dynamic> detail(Object? recordVerdict, {String status = 'draft'}) => {
          ..._runResponse(status: status),
          'actions': {'record_outside': recordVerdict},
        };

    testWidgets('a run the server refuses this reader states why, with no form',
        (tester) async {
      await tall(tester);
      await loginThen(
        ['ap_manager'],
        _screenClient(
          mode: 'record_only',
          runs: [_runResponse()],
          runDetail: detail({
            'code': 'payment_run_segregation',
            'message': 'server english',
            'params': {'step': 'record_outside'},
          }),
        ),
        permissions: recorder,
      );
      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await _pumpUntil(tester, find.text('Vendor 1'));
      await openRunMenu(tester);
      await tester.tap(find.text('Record run as paid…'));
      await tester.pumpAndSettle();

      expect(
        find.text('Segregation of duties: the user who created this payment '
            'run cannot also record it as paid. A different user must.'),
        findsOneWidget,
      );
      expect(find.byType(TextField), findsNothing);
      expect(find.byType(FilledButton), findsNothing);
    });

    testWidgets('an exported run is recorded from the verdict', (tester) async {
      await tall(tester);
      http.Request? posted;
      await loginThen(
        ['ap_manager'],
        _screenClient(
          mode: 'record_only',
          runs: [_runResponse(status: 'exported')],
          runDetail: detail(null, status: 'exported'),
          onPost: (req) {
            posted = req;
            return _json({
              'id': 'run1',
              'status': 'completed',
              'payment_count': 2,
              'total_amount': '5000.00',
            });
          },
        ),
        permissions: recorder,
      );
      await tester.pumpWidget(_localized(const PaymentQueueScreen()));
      await _pumpUntil(tester, find.text('Vendor 1'));
      await openRunMenu(tester);
      // An exported run is claimed: no Execute, no Cancel — only recording.
      expect(find.text('Execute'), findsNothing);
      expect(find.text('Cancel'), findsNothing);
      await tester.tap(find.text('Record run as paid…'));
      await tester.pumpAndSettle();

      await tester.enterText(
          find.widgetWithText(TextField, 'Bank batch reference'), 'BATCH-9');
      await tester.pump();
      await tester.tap(
          find.widgetWithText(FilledButton, 'Record as paid · \$5,000.00'));
      await tester.pumpAndSettle();

      expect(posted!.url.path, '/api/payments/runs/run1/record-outside');
      final body = jsonDecode(posted!.body) as Map<String, dynamic>;
      expect(body['reference'], 'BATCH-9');
      expect(body['paid_on'], matches(RegExp(r'^\d{4}-\d{2}-\d{2}$')));
      expect(find.text('2 payments recorded as paid'), findsOneWidget);
    });
  });
}
