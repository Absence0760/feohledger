// Every shared list tile, status badge and app-bar action reads its words
// through AppLocalizations — none renders an English enum `label` getter or a
// literal. Each surface is rendered under `en` and again under `ja`; a string
// that comes out identical in both is a literal nobody localized
// (`test/support/untranslated_text.dart`).
//
// Fixture data is UPPERCASE on purpose: the helper's second check treats any
// lowercase Latin word inside a Japanese string as leaked English, and a row's
// merged announcement embeds its data ("ACME, 請求書 INV-001, …"). Dates are
// left off the fixtures — their localization belongs to `utils/dates.dart`
// and its own tests, not to these widgets.
import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/contract.dart';
import 'package:feohledger_mobile/models/exception.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/models/notification.dart';
import 'package:feohledger_mobile/models/vendor.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/notification_store.dart';
import 'package:feohledger_mobile/widgets/cash_flow_button.dart';
import 'package:feohledger_mobile/widgets/contract_list_tile.dart';
import 'package:feohledger_mobile/widgets/exception_list_tile.dart';
import 'package:feohledger_mobile/widgets/inspection_list_tile.dart';
import 'package:feohledger_mobile/widgets/invoice_list_tile.dart';
import 'package:feohledger_mobile/widgets/notification_bell.dart';
import 'package:feohledger_mobile/widgets/notification_list_tile.dart';
import 'package:feohledger_mobile/widgets/vendor_list_tile.dart';

import '../support/untranslated_text.dart';

Widget _host(Locale locale, Widget child) => MaterialApp(
      locale: locale,
      localizationsDelegates: appLocalizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: SingleChildScrollView(child: child)),
    );

/// Renders [build] under `en` then `ja` and checks nothing leaked.
Future<void> _expectLocalized(
  WidgetTester tester,
  String surface,
  Widget Function() build, {
  Set<String> allowed = const {},
  Future<void> Function()? settle,
}) async {
  Future<Set<String>> render(Locale locale) async {
    await tester.pumpWidget(_host(locale, build()));
    await tester.pump();
    if (settle != null) await settle();
    return renderedStrings(tester);
  }

  final english = await render(const Locale('en'));
  final japanese = await render(const Locale('ja'));
  expectNoUntranslatedStrings(
    english: english,
    other: japanese,
    allowed: allowed,
    surface: surface,
  );
}

Invoice _invoice(InvoiceStatus status) => Invoice(
      id: 'inv1',
      invoiceNumber: 'INV-001',
      vendorName: 'ACME',
      amount: 1500,
      currency: 'USD',
      status: status,
      createdAt: DateTime(2026, 1, 1),
    );

ApException _exception(ApExceptionStatus status, ApExceptionSeverity sev) =>
    ApException(
      id: 'exc1',
      invoiceId: 'inv1',
      invoiceNumber: 'INV-001',
      vendorName: 'ACME',
      amount: '1500.00',
      exceptionType: 'duplicate',
      typeLabel: 'DUPLICATE',
      severity: sev,
      status: status,
      isOverdue: true,
      createdAt: DateTime(2026, 1, 1),
    );

http.Response _json(Object body) => http.Response(
      jsonEncode(body),
      200,
      headers: {'content-type': 'application/json'},
    );

void main() {
  testWidgets('InvoiceListTile + StatusBadge, every status, both modes',
      (tester) async {
    await _expectLocalized(
      tester,
      'InvoiceListTile',
      () => Column(
        children: [
          for (final s in InvoiceStatus.values) ...[
            InvoiceListTile(invoice: _invoice(s), onTap: () {}),
            InvoiceListTile(
              invoice: _invoice(s),
              onTap: () {},
              selectionMode: true,
              selected: s.index.isEven,
            ),
          ],
          InvoiceListTile(
            invoice: Invoice(
              id: 'inv2',
              status: InvoiceStatus.pending,
              createdAt: DateTime(2026, 1, 1),
            ),
          ),
        ],
      ),
      allowed: {'ACME', 'INV-001'},
    );
  });

  testWidgets('ExceptionListTile + ExceptionStatusBadge, every status/severity',
      (tester) async {
    await _expectLocalized(
      tester,
      'ExceptionListTile',
      () => Column(
        children: [
          for (final s in ApExceptionStatus.values)
            for (final sev in ApExceptionSeverity.values)
              ExceptionListTile(exception: _exception(s, sev), onTap: () {}),
        ],
      ),
      allowed: {'DUPLICATE', 'ACME · INV-001'},
    );
  });

  testWidgets('ContractListTile + ContractStatusBadge, every status',
      (tester) async {
    await _expectLocalized(
      tester,
      'ContractListTile',
      () => Column(
        children: [
          for (final s in ContractStatus.values)
            ContractListTile(
              contract: Contract(
                id: 'c-${s.value}',
                contractNumber: 'C-001',
                contractType: ContractType.msa,
                status: s,
                currency: 'USD',
                totalValue: 1000,
                createdAt: DateTime(2026, 1, 1),
              ),
              onTap: () {},
            ),
          // No title, no vendor: the untitled fallback.
          ContractListTile(
            contract: Contract(
              id: 'c-untitled',
              contractType: ContractType.other,
              status: ContractStatus.draft,
              createdAt: DateTime(2026, 1, 1),
            ),
          ),
        ],
      ),
      allowed: {'C-001'},
    );
  });

  testWidgets('VendorListTile + VendorStatusBadge, every status and source',
      (tester) async {
    const sources = ['manual', 'erp_sync', 'ai_extracted', 'csv_import'];
    await _expectLocalized(
      tester,
      'VendorListTile',
      () => Column(
        children: [
          for (final s in VendorStatus.values)
            for (final source in sources)
              VendorListTile(
                vendor: Vendor(
                  id: 'v-${s.value}-$source',
                  name: 'ACME',
                  code: 'AC-1',
                  status: s,
                  source: source,
                  invoiceCount: s.index,
                ),
                onTap: () {},
              ),
        ],
      ),
      // ERP and AI are acronyms the Japanese catalogue keeps as written
      // (`vendorSourceErp` / `vendorSourceAi`), like CSV elsewhere.
      allowed: {'ACME', 'AC-1', 'ERP', 'AI'},
    );
  });

  testWidgets('InspectionListTile + InspectionResultBadge, every outcome',
      (tester) async {
    await _expectLocalized(
      tester,
      'InspectionListTile',
      () => Column(
        children: [
          for (final r in InspectionResult.values)
            InspectionListTile(
              inspection: Inspection(
                id: 'qi-${r.name}',
                inspectionNumber: 'QI-001',
                grNumber: 'GR-001',
                grId: 'gr1',
                result: r,
                status: 'completed',
              ),
              onTap: () {},
            ),
        ],
      ),
      allowed: {'QI-001', 'GR-001'},
    );
  });

  testWidgets('NotificationListTile: every event, read state and recency',
      (tester) async {
    const events = [
      'invoice_assigned',
      'invoice_approved',
      'invoice_rejected',
      'invoice_paid',
      'contract_renewal_due',
      'chat_message',
      'some_future_event',
    ];
    const ages = [
      Duration(seconds: 10),
      Duration(minutes: 1),
      Duration(minutes: 5),
      Duration(hours: 1),
      Duration(hours: 3),
      Duration(days: 1),
      Duration(days: 2),
    ];
    final now = DateTime.now();
    await _expectLocalized(
      tester,
      'NotificationListTile',
      () => Column(
        children: [
          for (var i = 0; i < events.length; i++)
            NotificationListTile(
              notification: AppNotification(
                id: 'n$i',
                eventType: events[i],
                entityType: 'invoice',
                entityId: 'inv1',
                title: 'INV-001',
                body: 'ACME',
                readAt: i.isEven ? null : now,
                createdAt: now.subtract(ages[i]),
              ),
              onTap: () {},
            ),
        ],
      ),
      allowed: {'INV-001', 'ACME'},
    );
  });

  group('app-bar actions', () {
    setUp(() {
      FlutterSecureStorage.setMockInitialValues({});
      NotificationStore.instance.reset();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          final path = req.url.path;
          if (path == '/api/auth/login') {
            return _json({'access_token': 'tok-123'});
          }
          if (path == '/api/auth/me') {
            return _json({
              'id': 'u1',
              'email': 'demo@acme.com',
              'full_name': 'Demo User',
              'organization_id': 'org1',
              'roles': ['cfo'],
            });
          }
          return _json({'unread': 3});
        }),
      );
    });

    testWidgets('NotificationBell names itself and its unread count',
        (tester) async {
      await _expectLocalized(
        tester,
        'NotificationBell',
        () => const NotificationBell(),
        settle: () async {
          for (var i = 0;
              i < 30 && NotificationStore.instance.unread == 0;
              i++) {
            await tester.pump(const Duration(milliseconds: 50));
          }
          expect(NotificationStore.instance.unread, 3);
          await tester.pump();
        },
      );
    });

    testWidgets('CashFlowButton names its action', (tester) async {
      final result =
          await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
      expect(result.isSuccess, isTrue);
      await _expectLocalized(
        tester,
        'CashFlowButton',
        () => const Row(children: [CashFlowButton()]),
      );
    });
  });
}
