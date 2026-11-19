// The five detail screens state a failed first load through the shared
// `LoadErrorState`: a localized sentence and a Retry that refetches. The
// sentence carries the server's own `detail` when there is one, and the
// localized connection sentence when no server answered — never an
// exception's `toString()` (`ApiException(500): …`, `ClientException: …`).
//
// Also pins the `_detailRow` label colour on the invoice and contract detail
// screens at AA against the app's real theme surface (it was grey.shade600,
// ~4.4:1 there).
import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/main.dart' show buildAppTheme;
import 'package:feohledger_mobile/screens/contract_detail_screen.dart';
import 'package:feohledger_mobile/screens/exception_detail_screen.dart';
import 'package:feohledger_mobile/screens/inspection_detail_screen.dart';
import 'package:feohledger_mobile/screens/invoice_detail_screen.dart';
import 'package:feohledger_mobile/screens/workflow_detail_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/widgets/load_error_state.dart';

Widget _host(Widget home) => MaterialApp(
      theme: buildAppTheme(),
      localizationsDelegates: appLocalizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: home,
    );

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 40 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

/// One detail screen: the GET it loads from, a body that loads, and a text
/// the loaded screen shows.
class _Case {
  final String name;
  final Widget screen;
  final String path;
  final Map<String, dynamic> body;
  final String loadedText;
  final String serverPrefix;

  const _Case(
    this.name,
    this.screen,
    this.path,
    this.body,
    this.loadedText,
    this.serverPrefix,
  );
}

final _cases = <_Case>[
  const _Case(
    'InvoiceDetailScreen',
    InvoiceDetailScreen(invoiceId: '1'),
    '/api/invoices/1',
    {
      'id': '1',
      'invoice_number': 'INV-LOADED',
      'vendor_name': 'Acme Corp',
      'amount': 100,
      'currency': 'USD',
      'status': 'approved',
      'created_at': '2026-01-01T12:00:00',
    },
    'INV-LOADED',
    'Error: ',
  ),
  const _Case(
    'ContractDetailScreen',
    ContractDetailScreen(contractId: 'c1'),
    '/api/contracts/c1',
    {
      'id': 'c1',
      'contract_number': 'CTR-LOADED',
      'title': 'Cleaning services',
      'contract_type': 'service',
      'status': 'active',
      'vendor_name': 'Acme Corp',
      'currency': 'USD',
      'total_value': 1000,
      'created_at': '2026-01-01T12:00:00',
      'line_items': <Object>[],
    },
    'CTR-LOADED',
    'Error: ',
  ),
  const _Case(
    'InspectionDetailScreen',
    InspectionDetailScreen(inspectionId: 'qi1'),
    '/api/inspections/qi1',
    {
      'id': 'qi1',
      'inspection_number': 'QI-LOADED',
      'po_id': 'po1',
      'gr_id': 'gr1',
      'gr_number': 'GR-1',
      'result': 'pass',
      'inspected_date': '2026-03-04',
      'inspector': 'Dana',
      'accepted_quantity': 9.0,
      'rejected_quantity': 1.0,
      'status': 'completed',
      'created_at': '2026-03-04T09:00:00',
    },
    'QI-LOADED',
    'Could not load the inspection: ',
  ),
  const _Case(
    'WorkflowDetailScreen',
    WorkflowDetailScreen(workflowId: 'w1'),
    '/api/workflows/w1',
    {
      'id': 'w1',
      'name': 'Loaded Workflow',
      'is_active': true,
      'is_default': true,
      'steps_config': {
        'steps': [
          {'number': 1, 'type': 'extraction', 'name': 'Extract', 'config': {}},
        ],
      },
      'created_at': '2026-01-01T12:00:00',
    },
    'Loaded Workflow',
    'Could not load the workflow: ',
  ),
  const _Case(
    'ExceptionDetailScreen',
    ExceptionDetailScreen(exceptionId: '1'),
    '/api/exceptions/1',
    {
      'id': '1',
      'invoice_id': 'inv-1',
      'invoice_number': 'INV-EXC-LOADED',
      'vendor_name': 'Acme Corp',
      'amount': 250,
      'currency': 'USD',
      'exception_type': 'po_mismatch',
      'type_label': 'PO Mismatch',
      'severity': 'warning',
      'description': 'Amount differs from PO',
      'status': 'open',
      'is_overdue': false,
      'created_at': '2026-01-01T12:00:00',
    },
    'INV-EXC-LOADED',
    'Could not load the exception: ',
  ),
];

/// Answers [c]'s detail GET from [detail] (counting each call) and everything
/// else with an empty 404, which every screen's secondary reads tolerate.
MockClient _client(_Case c, Future<http.Response> Function() detail,
    void Function() onDetail) {
  return MockClient((req) async {
    if (req.method == 'GET' && req.url.path == c.path) {
      onDetail();
      return detail();
    }
    if (req.url.path.endsWith('/audit-log')) return _json(<Object>[]);
    return _json({'detail': 'Not found'}, 404);
  });
}

void main() {
  setUpAll(() {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    FlutterSecureStorage.setMockInitialValues({});
    await OfflineStore.instance.clear();
    ApiClient().debugConfigure();
  });

  for (final c in _cases) {
    group(c.name, () {
      testWidgets(
          'a failed load shows LoadErrorState with the server reason, and '
          'Retry refetches', (tester) async {
        var calls = 0;
        var fail = true;
        ApiClient().debugConfigure(
          client: _client(
            c,
            () async => fail
                ? _json({'detail': 'The server said no'}, 500)
                : _json(c.body),
            () => calls++,
          ),
        );

        await tester.pumpWidget(_host(c.screen));
        await _pumpUntil(tester, find.byType(LoadErrorState));

        expect(find.byType(LoadErrorState), findsOneWidget);
        expect(find.text('${c.serverPrefix}The server said no'),
            findsOneWidget);
        // Never the exception's log line.
        expect(find.textContaining('ApiException'), findsNothing);
        expect(calls, 1);

        fail = false;
        await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
        await _pumpUntil(tester, find.text(c.loadedText));

        expect(calls, 2);
        expect(find.byType(LoadErrorState), findsNothing);
        expect(find.text(c.loadedText), findsWidgets);
      });

      testWidgets(
          'a transport failure shows the localized connection sentence, '
          'not the exception', (tester) async {
        ApiClient().debugConfigure(
          client: _client(
            c,
            () async => throw http.ClientException('Connection refused'),
            () {},
          ),
        );

        await tester.pumpWidget(_host(c.screen));
        await _pumpUntil(tester, find.byType(LoadErrorState));

        expect(
          find.text(
            "Couldn't reach FeohLedger. Check your connection and tap Retry.",
          ),
          findsOneWidget,
        );
        expect(find.textContaining('ClientException'), findsNothing);
        expect(find.textContaining('Connection refused'), findsNothing);
        expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
      });
    });
  }

  // `_detailRow`'s label colour, measured against the theme surface the
  // screen actually paints on.
  for (final (c, label) in [
    (_cases[0], 'Invoice #'),
    (_cases[1], 'Contract #'),
  ]) {
    testWidgets('${c.name} detail-row labels clear AA contrast',
        (tester) async {
      tester.view.physicalSize = const Size(1200, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: _client(c, () async => _json(c.body), () {}),
      );

      await tester.pumpWidget(_host(c.screen));
      await _pumpUntil(tester, find.text(label));

      final text = tester.widget<Text>(find.text(label));
      final fg = text.style!.color!;
      final bg = Theme.of(tester.element(find.text(label))).colorScheme.surface;
      final hi = fg.computeLuminance() > bg.computeLuminance() ? fg : bg;
      final lo = identical(hi, fg) ? bg : fg;
      final ratio = (hi.computeLuminance() + 0.05) /
          (lo.computeLuminance() + 0.05);
      expect(ratio, greaterThanOrEqualTo(4.5),
          reason: '$label is ${ratio.toStringAsFixed(2)}:1 on the surface');

      // The whole-screen sweep runs on the invoice only: the contract's spend
      // KPI tints are their own widget's concern (kpi_card_test.dart).
      if (c.screen is InvoiceDetailScreen) {
        await expectLater(tester, meetsGuideline(textContrastGuideline));
      }
      handle.dispose();
    });
  }
}
