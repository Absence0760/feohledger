import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/invoice_detail_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';

/// Wraps the screen in a MaterialApp carrying the localization delegates so
/// `AppLocalizations.of(context)` resolves (defaults to English).
Widget _localized() => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: const InvoiceDetailScreen(invoiceId: '1'),
    );

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _meBody(List<String> roles) => {
      'id': 'u1',
      'email': 'demo@acme.com',
      'full_name': 'Demo User',
      'organization_id': 'org1',
      'roles': roles,
    };

Map<String, dynamic> _invoiceJson(
  String id, {
  String status = 'ready_for_review',
  String? vendor = 'Acme Corp',
  num? amount = 1234.56,
  String? invoiceNumber = 'INV-001',
  String? poNumber,
  String? description,
  String? fileUrl,
  String? glAccount,
  List<Map<String, dynamic>>? warnings,
  Map<String, dynamic>? poMatch,
}) =>
    {
      'id': id,
      'invoice_number': invoiceNumber,
      'vendor_name': vendor,
      'amount': amount,
      'currency': 'USD',
      'status': status,
      'po_number': poNumber,
      'description': description,
      'file_url': fileUrl,
      'gl_account': glAccount,
      'warnings': warnings,
      'po_match': poMatch,
      'created_at': '2026-01-01T12:00:00',
    };

/// Pump bounded frames until [finder] is present (or attempts run out) so the
/// screen's fetch resolves and the loading spinner leaves the tree. Never use
/// pumpAndSettle here — the CircularProgressIndicator animates forever.
Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 30 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

/// Install [client] and drive a real login so the AuthStore is populated (the
/// roles come from the client's GET /api/auth/me handler). debugConfigure
/// resets the session, so login must run after the client is installed.
Future<void> _arrange(MockClient client) async {
  ApiClient().debugConfigure(client: client);
  final result =
      await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
  expect(result.isSuccess, isTrue, reason: 'login fixture should succeed');
}

/// A MockClient covering auth + a single invoice detail GET returning [invoice].
/// Optional [onApprove] / [onReject] / [onPatch] override those calls.
/// [roles] sets the logged-in user's roles (default admin). [audit] is the
/// audit-log array returned for `/audit-log` (default empty).
MockClient _detailClient(
  Map<String, dynamic> invoice, {
  http.Response Function()? onGet,
  http.Response Function(http.Request req)? onApprove,
  http.Response Function(http.Request req)? onReject,
  http.Response Function(http.Request req)? onPatch,
  List<String> roles = const ['admin'],
  List<Map<String, dynamic>> audit = const [],
}) {
  return MockClient((req) async {
    final path = req.url.path;
    if (req.method == 'POST' && path == '/api/auth/login') {
      return _json({'access_token': 'tok-123'});
    }
    if (req.method == 'GET' && path == '/api/auth/me') {
      return _json(_meBody(roles));
    }
    if (req.method == 'GET' && path.endsWith('/audit-log')) {
      return _json(audit);
    }
    if (req.method == 'PATCH' && path.startsWith('/api/invoices/')) {
      return onPatch?.call(req) ?? _json(invoice);
    }
    if (req.method == 'POST' && path.endsWith('/approve')) {
      return onApprove?.call(req) ?? _json(invoice);
    }
    if (req.method == 'POST' && path.endsWith('/reject')) {
      return onReject?.call(req) ?? _json(invoice);
    }
    // InvoiceStore.approve/reject/update refetch the list after the write.
    if (req.method == 'GET' && path == '/api/invoices') {
      return _json({'invoices': <Map<String, dynamic>>[]});
    }
    if (req.method == 'GET' && path.startsWith('/api/invoices/')) {
      return onGet?.call() ?? _json(invoice);
    }
    return http.Response('not found', 404);
  });
}

void main() {
  setUpAll(() async {
    // InvoiceStore.approve/reject touch the OfflineStore via a refetch.
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    InvoiceStore.instance.reset();
    FlutterSecureStorage.setMockInitialValues({});
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  testWidgets('shows a loading spinner before the fetch resolves',
      (tester) async {
    // Gate the detail GET on a Completer so we can assert the spinner while the
    // fetch is in flight, then resolve it so no async work outlives the test.
    final gate = Completer<http.Response>();
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.url.path == '/api/auth/login') {
          return _json({'access_token': 'tok-123'});
        }
        if (req.url.path == '/api/auth/me') {
          return _json(_meBody(['admin']));
        }
        return gate.future; // detail fetch blocks here
      }),
    );
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await tester.pump();

    expect(find.byType(CircularProgressIndicator), findsOneWidget);

    // Release the fetch and let the screen reach its loaded state so nothing is
    // left mid-load at teardown.
    gate.complete(_json(_invoiceJson('1')));
    await _pumpUntil(tester, find.text('Acme Corp'));
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('renders invoice detail fields once the fetch resolves',
      (tester) async {
    await _arrange(
      _detailClient(_invoiceJson(
        '1',
        vendor: 'Globex LLC',
        poNumber: 'PO-42',
        description: 'Quarterly cloud bill',
      )),
    );

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Globex LLC'));

    expect(find.text('Globex LLC'), findsOneWidget);
    expect(find.text('INV-001'), findsOneWidget);
    expect(find.text('PO-42'), findsOneWidget);
    expect(find.text('Quarterly cloud bill'), findsOneWidget);
    expect(find.text(r'$1,234.56'), findsOneWidget);
    // No spinner remains.
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('falls back to "Unknown Vendor" when vendor_name is null',
      (tester) async {
    await _arrange(
      _detailClient(_invoiceJson('1', vendor: null)),
    );

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Unknown Vendor'));

    expect(find.text('Unknown Vendor'), findsOneWidget);
  });

  testWidgets('renders an error message when the fetch fails', (tester) async {
    final client = MockClient((req) async {
      final path = req.url.path;
      if (path == '/api/auth/login') return _json({'access_token': 'tok-123'});
      if (path == '/api/auth/me') return _json(_meBody(['admin']));
      return http.Response('boom', 500);
    });
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.textContaining('Error:'));

    expect(find.textContaining('Error:'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
    // The error state offers a way back without leaving the screen.
    expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
  });

  testWidgets('Retry on the error state reloads the invoice', (tester) async {
    var failNext = true;
    final client = MockClient((req) async {
      final path = req.url.path;
      if (path == '/api/auth/login') return _json({'access_token': 'tok-123'});
      if (path == '/api/auth/me') return _json(_meBody(['admin']));
      if (req.method == 'GET' && path.startsWith('/api/invoices/')) {
        if (failNext) return http.Response('boom', 500);
        return _json(_invoiceJson('1', vendor: 'Recovered Vendor'));
      }
      return http.Response('not found', 404);
    });
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.widgetWithText(FilledButton, 'Retry'));

    failNext = false;
    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await _pumpUntil(tester, find.text('Recovered Vendor'));

    expect(find.text('Recovered Vendor'), findsOneWidget);
    expect(find.textContaining('Error:'), findsNothing);
  });

  testWidgets('shows Approve/Reject actions for an actionable invoice when the '
      'user can approve', (tester) async {
    await _arrange(
      _detailClient(_invoiceJson('1', status: 'ready_for_review')),
    );

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Approve'));

    expect(find.widgetWithText(FilledButton, 'Approve'), findsOneWidget);
    expect(find.widgetWithText(OutlinedButton, 'Reject'), findsOneWidget);
  });

  testWidgets('hides the action bar when the invoice is not actionable',
      (tester) async {
    await _arrange(
      _detailClient(_invoiceJson('1', status: 'approved')),
    );

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Acme Corp'));

    expect(find.widgetWithText(FilledButton, 'Approve'), findsNothing);
    expect(find.widgetWithText(OutlinedButton, 'Reject'), findsNothing);
  });

  testWidgets('hides the action bar for a clerk even on an actionable invoice',
      (tester) async {
    // ap_clerk cannot approve -> action bar suppressed by canApprove gate.
    final client = MockClient((req) async {
      final path = req.url.path;
      if (path == '/api/auth/login') return _json({'access_token': 'tok-123'});
      if (path == '/api/auth/me') return _json(_meBody(['ap_clerk']));
      if (req.method == 'GET' && path.startsWith('/api/invoices/')) {
        return _json(_invoiceJson('1', status: 'ready_for_review'));
      }
      return http.Response('not found', 404);
    });
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Acme Corp'));

    expect(find.text('Acme Corp'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Approve'), findsNothing);
    expect(find.widgetWithText(OutlinedButton, 'Reject'), findsNothing);
  });

  testWidgets('tapping Approve posts approval and shows a confirmation snackbar',
      (tester) async {
    var approveCalls = 0;
    final client = _detailClient(
      _invoiceJson('1', status: 'ready_for_review'),
      onApprove: (req) {
        approveCalls++;
        return _json(_invoiceJson('1', status: 'approved'));
      },
      onGet: () => approveCalls == 0
          ? _json(_invoiceJson('1', status: 'ready_for_review'))
          : _json(_invoiceJson('1', status: 'approved')),
    );
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Approve'));

    await tester.tap(find.widgetWithText(FilledButton, 'Approve'));
    await _pumpUntil(tester, find.text('Invoice approved'));

    expect(approveCalls, 1);
    expect(find.text('Invoice approved'), findsOneWidget);
  });

  testWidgets('a double-tap on Approve only fires one approval POST',
      (tester) async {
    var approveCalls = 0;
    final gate = Completer<http.Response>();
    final client = MockClient((req) async {
      final path = req.url.path;
      if (req.method == 'POST' && path == '/api/auth/login') {
        return _json({'access_token': 'tok-123'});
      }
      if (req.method == 'GET' && path == '/api/auth/me') {
        return _json(_meBody(['admin']));
      }
      if (req.method == 'POST' && path.endsWith('/approve')) {
        approveCalls++;
        return gate.future; // hold the approval in flight
      }
      if (req.method == 'GET' && path == '/api/invoices') {
        return _json({'invoices': <Map<String, dynamic>>[]});
      }
      if (req.method == 'GET' && path.startsWith('/api/invoices/')) {
        return _json(_invoiceJson('1',
            status: approveCalls == 0 ? 'ready_for_review' : 'approved'));
      }
      return http.Response('not found', 404);
    });
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Approve'));

    final approveBtn = find.widgetWithText(FilledButton, 'Approve');
    await tester.tap(approveBtn);
    await tester.pump(); // _submitting=true → button disabled
    // Second tap while the first is in flight must be ignored.
    await tester.tap(approveBtn, warnIfMissed: false);
    await tester.pump();

    expect(approveCalls, 1, reason: 'double-tap must not double-POST');

    // Release the held approval so nothing outlives the test.
    gate.complete(_json(_invoiceJson('1', status: 'approved')));
    await _pumpUntil(tester, find.text('Invoice approved'));
  });

  testWidgets('shows an error snackbar when the approval fails',
      (tester) async {
    final client = _detailClient(
      _invoiceJson('1', status: 'ready_for_review'),
      onApprove: (req) => http.Response('boom', 500),
    );
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Approve'));

    await tester.tap(find.widgetWithText(FilledButton, 'Approve'));
    await _pumpUntil(tester, find.textContaining('Could not approve'));

    // The user gets explicit failure feedback instead of a silent no-op.
    expect(find.textContaining('Could not approve'), findsOneWidget);
  });

  testWidgets('tapping Reject opens the reason dialog with Cancel/Reject',
      (tester) async {
    await _arrange(
      _detailClient(_invoiceJson('1', status: 'ready_for_review')),
    );

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Reject'));

    await tester.tap(find.widgetWithText(OutlinedButton, 'Reject'));
    await tester.pumpAndSettle();

    expect(find.text('Reject Invoice'), findsOneWidget);
    expect(find.byType(TextField), findsOneWidget);
    // The reason field's labelText renders as a Text node inside the dialog.
    expect(find.text('Reason'), findsOneWidget);
    expect(find.widgetWithText(TextButton, 'Cancel'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Reject'), findsOneWidget);
  });

  testWidgets('submitting a reject reason posts it and shows a snackbar',
      (tester) async {
    String? sentReason;
    var rejectCalls = 0;
    final client = _detailClient(
      _invoiceJson('1', status: 'ready_for_review'),
      onReject: (req) {
        rejectCalls++;
        sentReason = (jsonDecode(req.body) as Map)['reason'] as String?;
        return _json(_invoiceJson('1', status: 'rejected'));
      },
      onGet: () => rejectCalls == 0
          ? _json(_invoiceJson('1', status: 'ready_for_review'))
          : _json(_invoiceJson('1', status: 'rejected')),
    );
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Reject'));

    await tester.tap(find.widgetWithText(OutlinedButton, 'Reject'));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), 'Wrong amount');
    await tester.tap(find.widgetWithText(FilledButton, 'Reject'));
    await _pumpUntil(tester, find.text('Invoice rejected'));

    expect(rejectCalls, 1);
    expect(sentReason, 'Wrong amount');
    expect(find.text('Invoice rejected'), findsOneWidget);
  });

  testWidgets('shows the Edit action for an editable invoice when the user can '
      'edit', (tester) async {
    await _arrange(_detailClient(_invoiceJson('1', status: 'ready_for_review')));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Acme Corp'));

    expect(find.byTooltip('Edit'), findsOneWidget);
  });

  testWidgets('hides the Edit action for a clerk', (tester) async {
    await _arrange(_detailClient(
      _invoiceJson('1', status: 'ready_for_review'),
      roles: ['ap_clerk'],
    ));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Acme Corp'));

    expect(find.byTooltip('Edit'), findsNothing);
  });

  testWidgets('hides the Edit action for an immutable-status invoice',
      (tester) async {
    // `paid` is in the backend IMMUTABLE_STATUSES set -> PATCH would 409, so
    // the affordance is hidden.
    await _arrange(_detailClient(_invoiceJson('1', status: 'paid')));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Acme Corp'));

    expect(find.byTooltip('Edit'), findsNothing);
  });

  testWidgets('editing a field PATCHes the change and confirms', (tester) async {
    Map<String, dynamic>? sentBody;
    var patched = false;
    final client = _detailClient(
      _invoiceJson('1', status: 'ready_for_review', vendor: 'Acme Corp'),
      onPatch: (req) {
        sentBody = jsonDecode(req.body) as Map<String, dynamic>;
        patched = true;
        return _json(_invoiceJson('1', status: 'ready_for_review', vendor: 'Globex'));
      },
      onGet: () => patched
          ? _json(_invoiceJson('1', status: 'ready_for_review', vendor: 'Globex'))
          : _json(_invoiceJson('1', status: 'ready_for_review', vendor: 'Acme Corp')),
    );
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.byTooltip('Edit'));

    await tester.tap(find.byTooltip('Edit'));
    await tester.pumpAndSettle();

    // The edit sheet is open.
    expect(find.text('Edit Invoice'), findsOneWidget);

    // Change the vendor field and save.
    await tester.enterText(find.widgetWithText(TextFormField, 'Acme Corp'), 'Globex');
    await tester.ensureVisible(find.widgetWithText(FilledButton, 'Save'));
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await _pumpUntil(tester, find.text('Invoice updated'));

    expect(sentBody, isNotNull);
    expect(sentBody!['vendor'], 'Globex');
    expect(find.text('Invoice updated'), findsOneWidget);
  });

  testWidgets('a refused edit shows the server\'s sentence, not "try again"',
      (tester) async {
    // The server refuses a GL code outside the invoice's chart (decisions
    // §199). The sheet's GL field is free text, so this is where a mistyped
    // code lands — and the user needs to be told WHICH code and why, since
    // retrying the same save can only be refused again.
    const refusal = "GL account '9999' is not in this invoice's chart of "
        "accounts. Choose an active code from the invoice's own chart — the "
        "shared accounts plus its entity's own.";
    Map<String, dynamic>? sentBody;
    final client = _detailClient(
      _invoiceJson('1', status: 'ready_for_review', glAccount: '6100'),
      onPatch: (req) {
        sentBody = jsonDecode(req.body) as Map<String, dynamic>;
        return http.Response(
          jsonEncode({'detail': refusal}),
          422,
          headers: {'content-type': 'application/json'},
        );
      },
    );
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(_localized());
    await _pumpUntil(tester, find.byTooltip('Edit'));
    await tester.tap(find.byTooltip('Edit'));
    await tester.pumpAndSettle();

    await tester.enterText(find.widgetWithText(TextFormField, '6100'), '9999');
    await tester.ensureVisible(find.widgetWithText(FilledButton, 'Save'));
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await _pumpUntil(tester, find.textContaining("GL account '9999'"));

    expect(
      sentBody,
      {'gl_account': '9999'},
      reason: 'only the changed field is sent',
    );
    expect(find.text('Could not save changes: $refusal'), findsOneWidget);
    expect(find.textContaining('ApiException'), findsNothing);
  });

  testWidgets('the edit sheet sends the amount as a string-Decimal',
      (tester) async {
    Map<String, dynamic>? sentBody;
    var patched = false;
    final client = _detailClient(
      _invoiceJson('1', status: 'ready_for_review', amount: 1234.56),
      onPatch: (req) {
        sentBody = jsonDecode(req.body) as Map<String, dynamic>;
        patched = true;
        return _json(_invoiceJson('1', status: 'ready_for_review', amount: 999.99));
      },
      onGet: () => patched
          ? _json(_invoiceJson('1', status: 'ready_for_review', amount: 999.99))
          : _json(_invoiceJson('1', status: 'ready_for_review', amount: 1234.56)),
    );
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.byTooltip('Edit'));

    await tester.tap(find.byTooltip('Edit'));
    await tester.pumpAndSettle();

    // The amount field is seeded with the plain decimal text.
    await tester.enterText(find.widgetWithText(TextFormField, '1234.56'), '999.99');
    await tester.ensureVisible(find.widgetWithText(FilledButton, 'Save'));
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await _pumpUntil(tester, find.text('Invoice updated'));

    expect(sentBody!['amount'], '999.99');
    expect(sentBody!['amount'], isA<String>(),
        reason: 'money must travel as string-Decimal, never a float');
  });

  testWidgets('an invalid amount blocks the save (validation)', (tester) async {
    var patched = false;
    final client = _detailClient(
      _invoiceJson('1', status: 'ready_for_review', amount: 1234.56),
      onPatch: (req) {
        patched = true;
        return _json(_invoiceJson('1', status: 'ready_for_review'));
      },
    );
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.byTooltip('Edit'));

    await tester.tap(find.byTooltip('Edit'));
    await tester.pumpAndSettle();

    // A bare dot is not a valid decimal — the input filter strips letters, but
    // the validator catches the malformed remainder.
    await tester.enterText(find.widgetWithText(TextFormField, '1234.56'), '.');
    await tester.ensureVisible(find.widgetWithText(FilledButton, 'Save'));
    await tester.tap(find.widgetWithText(FilledButton, 'Save'));
    await tester.pumpAndSettle();

    // Still on the sheet, validation message shown, no PATCH fired.
    expect(find.text('Edit Invoice'), findsOneWidget);
    expect(find.textContaining('valid amount'), findsOneWidget);
    expect(patched, isFalse);
  });

  testWidgets('renders the activity timeline from the audit log',
      (tester) async {
    await _arrange(_detailClient(
      _invoiceJson('1', status: 'ready_for_review'),
      audit: [
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
      ],
    ));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Uploaded invoice'));

    expect(find.text('Activity'), findsOneWidget);
    expect(find.text('Uploaded invoice'), findsOneWidget);
    expect(find.text('Edited fields'), findsOneWidget);
    // The before/after value (rendered as a RichText span) is present.
    expect(find.textContaining('250.00', findRichText: true), findsOneWidget);
  });

  testWidgets('shows the empty activity state when there is no audit history',
      (tester) async {
    await _arrange(_detailClient(_invoiceJson('1', status: 'ready_for_review')));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('No activity yet'));

    expect(find.text('No activity yet'), findsOneWidget);
  });

  testWidgets('shows a PDF preview card when the file is a PDF', (tester) async {
    await _arrange(_detailClient(_invoiceJson(
      '1',
      fileUrl: '/api/invoices/file/k/scan.pdf',
    )));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Tap to view PDF'));

    // The PDF can't render as a bitmap inline — a labelled card invites the
    // full viewer instead of an Image.network attempt that would error.
    expect(find.text('Tap to view PDF'), findsOneWidget);
    expect(find.byIcon(Icons.picture_as_pdf), findsOneWidget);
  });

  testWidgets('shows an image preview (not the PDF card) for an image file',
      (tester) async {
    await _arrange(_detailClient(_invoiceJson(
      '1',
      fileUrl: '/api/invoices/file/k/photo.png',
    )));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Acme Corp'));

    expect(find.text('Tap to view PDF'), findsNothing);
    // The image path renders an Image (network) widget in the preview tile.
    expect(find.byType(Image), findsWidgets);
  });

  testWidgets('renders no file preview when the invoice has no file',
      (tester) async {
    await _arrange(_detailClient(_invoiceJson('1', fileUrl: null)));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Acme Corp'));

    expect(find.text('Tap to view PDF'), findsNothing);
    expect(find.text('Tap to view file'), findsNothing);
  });

  testWidgets('cancelling the reject dialog posts nothing', (tester) async {
    var rejectCalls = 0;
    final client = _detailClient(
      _invoiceJson('1', status: 'ready_for_review'),
      onReject: (req) {
        rejectCalls++;
        return _json(_invoiceJson('1', status: 'rejected'));
      },
    );
    ApiClient().debugConfigure(client: client);
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Reject'));

    await tester.tap(find.widgetWithText(OutlinedButton, 'Reject'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
    await tester.pumpAndSettle();

    expect(find.text('Reject Invoice'), findsNothing);
    expect(rejectCalls, 0);
  });

  testWidgets('renders warnings / fraud flags from the invoice', (tester) async {
    await _arrange(_detailClient(_invoiceJson(
      '1',
      warnings: [
        {
          'type': 'duplicate',
          'severity': 'warning',
          'message': 'Duplicate invoice number for this vendor',
        },
        {
          'type': 'fraud_bank_change',
          'severity': 'error',
          'message': 'Vendor bank details recently changed',
        },
      ],
    )));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Acme Corp'));

    expect(find.text('Warnings & fraud flags'), findsOneWidget);
    expect(find.text('Duplicate invoice number for this vendor'),
        findsOneWidget);
    expect(find.text('Vendor bank details recently changed'), findsOneWidget);
  });

  testWidgets('renders the PO match panel when present', (tester) async {
    await _arrange(_detailClient(_invoiceJson(
      '1',
      poNumber: 'PO-9',
      poMatch: {
        'match_type': '3-way',
        'status': 'mismatch',
        'variance_pct': 8.0,
        'within_tolerance': false,
        'issues': ['Amount variance of 8.0%'],
      },
    )));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Acme Corp'));

    expect(find.text('PO Match'), findsOneWidget);
    expect(find.text('3-way match'), findsOneWidget);
    expect(find.text('Mismatch'), findsOneWidget);
  });

  testWidgets('shows ERP status derived from the audit log', (tester) async {
    await _arrange(_detailClient(
      _invoiceJson('1', status: 'sent_to_erp'),
      audit: [
        {
          'id': 'a1',
          'actor_id': 'u1',
          'actor_name': 'Demo User',
          'action': 'invoice.erp_confirmed',
          'entity_type': 'invoice',
          'entity_id': '1',
          'details': {'erp_reference': 'ERP-12345'},
          'created_at': '2026-01-02T10:00:00',
        },
      ],
    ));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('ERP Status'));

    expect(find.text('ERP Status'), findsOneWidget);
    expect(find.text('ERP Reference'), findsOneWidget);
    expect(find.text('ERP-12345'), findsOneWidget);
  });

  testWidgets('hides the ERP panel for a non-ERP status', (tester) async {
    await _arrange(_detailClient(_invoiceJson('1', status: 'ready_for_review')));

    await tester.pumpWidget(
      _localized(),
    );
    await _pumpUntil(tester, find.text('Acme Corp'));

    expect(find.text('ERP Status'), findsNothing);
  });

  // `_load()` awaited the detail GET and then called setState in BOTH branches
  // with no `mounted` check (while the sibling `_loadActivity` had one), so
  // backing out of an invoice before a slow GET — or its 10s timeout — resolved
  // threw "setState() called after dispose()".
  group('route popped mid-load', () {
    Future<void> pushPopComplete(
      WidgetTester tester,
      Completer<void> gate,
    ) async {
      final navigator = GlobalKey<NavigatorState>();
      await tester.pumpWidget(MaterialApp(
        navigatorKey: navigator,
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: const Scaffold(body: Text('list')),
      ));

      navigator.currentState!.push(
        MaterialPageRoute(
          builder: (_) => const InvoiceDetailScreen(invoiceId: '1'),
        ),
      );
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));

      expect(find.byType(InvoiceDetailScreen), findsOneWidget,
          reason: 'the detail route must be mounted before we pop it');

      // Back out while the detail GET is still in flight, and wait on the real
      // signal — the route leaving the tree — rather than a fixed delay.
      navigator.currentState!.pop();
      await tester.pump();
      final gone = find.byType(InvoiceDetailScreen);
      for (var i = 0; i < 40 && gone.evaluate().isNotEmpty; i++) {
        await tester.pump(const Duration(milliseconds: 50));
      }
      expect(gone, findsNothing,
          reason: 'the detail route must be gone (its State disposed)');

      gate.complete();
      for (var i = 0; i < 10; i++) {
        await tester.pump(const Duration(milliseconds: 50));
      }
    }

    testWidgets('a late success response does not setState after dispose',
        (tester) async {
      final gate = Completer<void>();
      // The invoice GET hangs until the test releases it.
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'GET' &&
              req.url.path.startsWith('/api/invoices/') &&
              !req.url.path.endsWith('/audit-log')) {
            await gate.future;
            return _json(_invoiceJson('1'));
          }
          return _json(<String, dynamic>{});
        }),
      );

      await pushPopComplete(tester, gate);

      expect(tester.takeException(), isNull);
    });

    testWidgets('a late failure response does not setState after dispose',
        (tester) async {
      final gate = Completer<void>();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.method == 'GET' &&
              req.url.path.startsWith('/api/invoices/') &&
              !req.url.path.endsWith('/audit-log')) {
            await gate.future;
            return http.Response('boom', 500);
          }
          return _json(<String, dynamic>{});
        }),
      );

      await pushPopComplete(tester, gate);

      expect(tester.takeException(), isNull);
    });
  });
}
