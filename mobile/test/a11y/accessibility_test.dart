import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/audit_entry.dart';
import 'package:feohledger_mobile/models/exception.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/models/notification.dart';
import 'package:feohledger_mobile/models/vendor.dart';
import 'package:feohledger_mobile/screens/adaptive_screen.dart';
import 'package:feohledger_mobile/screens/admin_users_screen.dart';
import 'package:feohledger_mobile/screens/approvals_screen.dart';
import 'package:feohledger_mobile/screens/cash_flow_screen.dart';
import 'package:feohledger_mobile/screens/org_settings_screen.dart';
import 'package:feohledger_mobile/screens/payment_queue_screen.dart';
import 'package:feohledger_mobile/screens/exception_detail_screen.dart';
import 'package:feohledger_mobile/screens/exceptions_screen.dart';
import 'package:feohledger_mobile/screens/inspection_detail_screen.dart';
import 'package:feohledger_mobile/screens/inspections_screen.dart';
import 'package:feohledger_mobile/screens/invoices_screen.dart';
import 'package:feohledger_mobile/screens/login_screen.dart';
import 'package:feohledger_mobile/screens/notifications_screen.dart';
import 'package:feohledger_mobile/screens/workflows_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/adaptive_store.dart';
import 'package:feohledger_mobile/stores/admin_user_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/cash_flow_store.dart';
import 'package:feohledger_mobile/stores/exception_store.dart';
import 'package:feohledger_mobile/stores/org_settings_store.dart';
import 'package:feohledger_mobile/stores/payment_queue_store.dart';
import 'package:feohledger_mobile/stores/inspection_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/stores/notification_store.dart';
import 'package:feohledger_mobile/stores/workflow_store.dart';
import 'package:feohledger_mobile/widgets/activity_timeline.dart';
import 'package:feohledger_mobile/widgets/advanced_search_sheet.dart';
import 'package:feohledger_mobile/widgets/bulk_action_bar.dart';
import 'package:feohledger_mobile/widgets/erp_status_panel.dart';
import 'package:feohledger_mobile/widgets/exception_list_tile.dart';
import 'package:feohledger_mobile/widgets/exception_status_badge.dart';
import 'package:feohledger_mobile/widgets/inspection_list_tile.dart';
import 'package:feohledger_mobile/widgets/inspection_result_badge.dart';
import 'package:feohledger_mobile/widgets/invoice_edit_sheet.dart';
import 'package:feohledger_mobile/widgets/invoice_list_tile.dart';
import 'package:feohledger_mobile/widgets/invoice_warnings_panel.dart';
import 'package:feohledger_mobile/widgets/kpi_card.dart';
import 'package:feohledger_mobile/widgets/notification_bell.dart';
import 'package:feohledger_mobile/widgets/notification_list_tile.dart';
import 'package:feohledger_mobile/widgets/partial_conversion_note.dart';
import 'package:feohledger_mobile/widgets/status_badge.dart';
import 'package:feohledger_mobile/widgets/vendor_list_tile.dart';
import 'package:feohledger_mobile/widgets/vendor_status_badge.dart';

// Regression guard mirroring the web axe pass: every key surface must clear
// Flutter's built-in accessibility guidelines — minimum tap-target size
// (WCAG 2.5.8), labelled tappables (WCAG 4.1.2 / 1.1.1) and text contrast
// (WCAG 1.4.3). Run with `flutter test test/a11y/`.

Widget _host(Widget child) => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: child),
    );

/// Like [_host] but mounts [screen] as the MaterialApp `home` directly (no extra
/// Scaffold), for full screens that bring their own. Carries the localization
/// delegates so a localized screen's `AppLocalizations.of(context)` resolves.
Widget _screenHost(Widget screen) => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: screen,
    );

Invoice _invoice() => Invoice(
      id: 'inv1',
      invoiceNumber: 'INV-001',
      vendorName: 'Acme Supplies',
      amount: 1500,
      currency: 'USD',
      status: InvoiceStatus.readyForReview,
      dueDate: DateTime(2026, 2, 1),
      createdAt: DateTime(2026, 1, 1),
    );

ApException _exception() => ApException(
      id: 'exc1',
      invoiceId: 'inv1',
      invoiceNumber: 'INV-001',
      vendorName: 'Acme Supplies',
      amount: 1500,
      exceptionType: 'duplicate',
      typeLabel: 'Duplicate Invoice',
      severity: ApExceptionSeverity.error,
      status: ApExceptionStatus.open,
      createdAt: DateTime(2026, 1, 1),
    );

http.Response _list(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'invoices': items}),
      200,
      headers: {'content-type': 'application/json'},
    );

http.Response _wrappedList(List<Map<String, dynamic>> items) => http.Response(
      jsonEncode({'items': items, 'total': items.length, 'page': 1}),
      200,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _exceptionJson(String id) => {
      'id': id,
      'invoice_id': 'inv-$id',
      'invoice_number': 'INV-$id',
      'vendor_name': 'Acme Corp',
      'amount': 250,
      'exception_type': 'duplicate',
      'type_label': 'Duplicate Invoice',
      'severity': 'error',
      'status': 'open',
      'is_overdue': false,
      'created_at': '2026-01-01T12:00:00',
    };

AppNotification _notification({bool read = false}) => AppNotification(
      id: 'n1',
      eventType: 'invoice_approved',
      entityType: 'invoice',
      entityId: 'inv1',
      title: 'Invoice approved',
      body: 'INV-001 was approved by Demo User',
      readAt: read ? DateTime(2026, 1, 2) : null,
      createdAt: DateTime(2026, 1, 1),
    );

http.Response _notificationPage(List<Map<String, dynamic>> items) =>
    http.Response(
      jsonEncode({
        'items': items,
        'total': items.length,
        'unread': items.where((i) => i['read_at'] == null).length,
        'page': 1,
        'page_size': 20,
      }),
      200,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _notificationJson(String id) => {
      'id': id,
      'event_type': 'invoice_approved',
      'entity_type': 'invoice',
      'entity_id': 'inv-$id',
      'title': 'Invoice approved',
      'body': 'INV-$id was approved',
      'read_at': null,
      'created_at': '2026-01-01T12:00:00Z',
    };

Map<String, dynamic> _invoiceJson(String id) => {
      'id': id,
      'invoice_number': 'INV-$id',
      'vendor_name': 'Acme Corp',
      'amount': 100,
      'currency': 'USD',
      'status': 'ready_for_review',
      'created_at': '2026-01-01T12:00:00',
    };

Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 30 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  group('InvoiceListTile', () {
    testWidgets('meets tap-target, label and contrast guidelines',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        InvoiceListTile(invoice: _invoice(), onTap: () {}),
      ));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });

    testWidgets('announces a single composed label (vendor, amount, status)',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        InvoiceListTile(invoice: _invoice(), onTap: () {}),
      ));
      // The merged announcement leads with vendor + amount, not 5 fragments.
      expect(
        find.bySemanticsLabel(RegExp(r'Acme Supplies.*1,500.*Ready for Review')),
        findsOneWidget,
      );
      handle.dispose();
    });

    testWidgets('selection mode exposes a checked state + keeps tap target',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        InvoiceListTile(
          invoice: _invoice(),
          selectionMode: true,
          selected: true,
          onTap: () {},
        ),
      ));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      // The merged row label leads with the selection state for the reader.
      expect(
        find.bySemanticsLabel(RegExp(r'Selected.*Acme Supplies')),
        findsOneWidget,
      );
      handle.dispose();
    });
  });

  group('BulkActionBar', () {
    testWidgets('labels the count + actions and clears contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        BulkActionBar(
          selectedCount: 3,
          onExport: () {},
          onStatusChange: () {},
          onDelete: () {},
        ),
      ));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      expect(find.bySemanticsLabel('3 selected'), findsOneWidget);
      handle.dispose();
    });
  });

  group('KpiCard', () {
    testWidgets('meets contrast guideline and merges into one label',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        const KpiCard(
          title: 'For Review',
          value: '7',
          subtitle: '3 overdue',
          icon: Icons.rate_review,
        ),
      ));

      await expectLater(tester, meetsGuideline(textContrastGuideline));
      expect(find.bySemanticsLabel('For Review: 7, 3 overdue'), findsOneWidget);
      handle.dispose();
    });
  });

  group('PartialConversionNote', () {
    testWidgets('clears contrast and is announced as its sentence alone',
        (tester) async {
      // The dashboard and cash-flow part-conversion disclosures. Amber-reading
      // brown.shade800 on the scaffold, like the adaptive tab's note — a true
      // orange fails here — and the info glyph is decorative, so the
      // sentence is the whole announcement.
      final handle = tester.ensureSemantics();
      const message = 'Partial: 2 invoices with no exchange rate into EUR, '
          'counted at face value — the bands below mix currencies by that '
          'much.';
      await tester.pumpWidget(_host(const PartialConversionNote(message)));

      await expectLater(tester, meetsGuideline(textContrastGuideline));
      expect(find.bySemanticsLabel(message), findsOneWidget);
      handle.dispose();
    });
  });

  group('StatusBadge', () {
    testWidgets('exposes its status as a label and clears contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        const StatusBadge(status: InvoiceStatus.pending),
      ));

      await expectLater(tester, meetsGuideline(textContrastGuideline));
      expect(find.bySemanticsLabel('Status: Pending'), findsOneWidget);
      handle.dispose();
    });

    // The amber/orange/red hues are the worst-case for AA on the pale tint —
    // guard each so a future "make it amber again" regression is caught.
    for (final status in [
      InvoiceStatus.readyForReview, // amber tint
      InvoiceStatus.pending, // orange tint
      InvoiceStatus.rejected, // red tint
      InvoiceStatus.approved, // green tint
    ]) {
      testWidgets('clears contrast for ${status.value}', (tester) async {
        final handle = tester.ensureSemantics();
        await tester.pumpWidget(_host(StatusBadge(status: status)));
        await expectLater(tester, meetsGuideline(textContrastGuideline));
        handle.dispose();
      });
    }
  });

  group('VendorListTile', () {
    Vendor vendor() => Vendor(
          id: 'v1',
          name: 'Acme Supplies',
          code: 'ACME',
          email: 'ap@acme.com',
          status: VendorStatus.unverified,
          source: 'manual',
          invoiceCount: 3,
        );

    testWidgets('meets tap-target, label and contrast guidelines',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        VendorListTile(vendor: vendor(), onTap: () {}),
      ));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      // One merged announcement leads with the vendor name + status.
      expect(
        find.bySemanticsLabel(RegExp(r'Acme Supplies.*Unverified.*invoices')),
        findsOneWidget,
      );
      handle.dispose();
    });
  });

  group('VendorStatusBadge', () {
    // Amber/red are the worst case for AA on the pale tint — guard each.
    for (final status in VendorStatus.values) {
      testWidgets('exposes label and clears contrast for ${status.value}',
          (tester) async {
        final handle = tester.ensureSemantics();
        await tester.pumpWidget(_host(VendorStatusBadge(status: status)));
        await expectLater(tester, meetsGuideline(textContrastGuideline));
        expect(
          find.bySemanticsLabel('Status: ${status.label}'),
          findsOneWidget,
        );
        handle.dispose();
      });
    }
  });

  group('ActivityTimeline', () {
    AuditEntry auditEntry({
      String action = 'invoice.edited',
      Map<String, dynamic>? details = const {
        'changes': {
          'amount': {'old': '100.00', 'new': '250.00'},
        },
      },
    }) =>
        AuditEntry(
          id: 'a1',
          actorId: 'u1',
          actorName: 'Demo User',
          action: action,
          entityType: 'invoice',
          entityId: '1',
          details: details,
          createdAt: DateTime(2026, 1, 2, 10),
        );

    testWidgets('clears contrast and announces one phrase per entry',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        ActivityTimeline(entries: [auditEntry()]),
      ));

      await expectLater(tester, meetsGuideline(textContrastGuideline));
      // Each entry merges into a single sensible announcement.
      expect(
        find.bySemanticsLabel(
          RegExp(r'Edited fields by Demo User.*changed from 100.00 to 250.00'),
        ),
        findsOneWidget,
      );
      handle.dispose();
    });
  });

  group('InvoiceEditSheet', () {
    Invoice inv() => Invoice(
          id: '1',
          invoiceNumber: 'INV-001',
          vendorName: 'Acme Supplies',
          amount: 1500,
          currency: 'USD',
          status: InvoiceStatus.readyForReview,
          createdAt: DateTime(2026, 1, 1),
        );

    testWidgets('meets tap-target, label and contrast guidelines',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(InvoiceEditSheet(invoice: inv())));
      await tester.pump();

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      // The icon-only close + date-clear controls announce their purpose.
      expect(find.bySemanticsLabel('Close edit form'), findsOneWidget);
      handle.dispose();
    });

    testWidgets('the financially-locked variant stays labelled and legible',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(InvoiceEditSheet(
        invoice: Invoice(
          id: '1',
          invoiceNumber: 'INV-001',
          vendorName: 'Acme Supplies',
          amount: 1500,
          currency: 'USD',
          status: InvoiceStatus.approved,
          createdAt: DateTime(2026, 1, 1),
        ),
      )));
      await tester.pump();

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      // The lock notice + the per-field helper text must clear AA over the
      // secondary-container tint they sit on.
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      // A read-only field keeps its label (and Flutter marks it read-only for
      // assistive tech), so the reason is announced, not just colour-coded.
      expect(find.text('Frozen after approval'), findsNWidgets(2));
      handle.dispose();
    });
  });

  group('InvoiceWarningsPanel', () {
    testWidgets('clears contrast and merges each warning into one label',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        const InvoiceWarningsPanel(
          warnings: [
            InvoiceWarning(
              type: 'missing_field',
              severity: WarningSeverity.error,
              message: 'Missing vendor name',
            ),
            InvoiceWarning(
              type: 'duplicate',
              severity: WarningSeverity.warning,
              message: 'Duplicate invoice number',
            ),
          ],
          poMatch: PoMatch(
            matchType: '3-way',
            status: 'mismatch',
            variancePct: 12.5,
            withinTolerance: false,
            issues: ['Amount variance'],
          ),
        ),
      ));

      await expectLater(tester, meetsGuideline(textContrastGuideline));
      expect(find.bySemanticsLabel('Error: Missing vendor name'),
          findsOneWidget);
      handle.dispose();
    });
  });

  group('ErpStatusPanel', () {
    Invoice erpInvoice() => Invoice(
          id: 'inv1',
          invoiceNumber: 'INV-001',
          vendorName: 'Acme',
          amount: 100,
          currency: 'USD',
          status: InvoiceStatus.failed,
          createdAt: DateTime(2026, 1, 1),
        );

    testWidgets('clears contrast with an error row', (tester) async {
      final handle = tester.ensureSemantics();
      final info = ErpInfo.fromAuditLog([
        AuditEntry(
          id: 'a1',
          actorName: 'Demo User',
          action: 'invoice.erp_failed',
          details: const {'error': 'Connection refused'},
          createdAt: DateTime(2026, 1, 2),
        ),
      ]);
      await tester.pumpWidget(_host(
        ErpStatusPanel(invoice: erpInvoice(), erpInfo: info),
      ));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });
  });

  group('AdvancedSearchSheet', () {
    testWidgets('meets tap-target, label and contrast guidelines',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        const AdvancedSearchSheet(initial: InvoiceSearchFilters.empty),
      ));
      await tester.pump();

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      // The icon-only close control announces its purpose.
      expect(find.bySemanticsLabel('Close advanced search'), findsOneWidget);
      handle.dispose();
    });
  });

  group('LoginScreen', () {
    setUp(() {
      FlutterSecureStorage.setMockInitialValues({});
      ApiClient().debugConfigure();
    });

    testWidgets('meets tap-target, label and contrast guidelines',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(const LoginScreen()));
      await tester.pump();

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });

    testWidgets('the password show/hide toggle exposes an accessible label',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(const LoginScreen()));
      await tester.pump();

      // The icon-only visibility toggle must announce its purpose (WCAG 4.1.2).
      expect(find.bySemanticsLabel('Show password'), findsOneWidget);
      // Toggling flips the label so its state is conveyed too.
      await tester.tap(find.byTooltip('Show password'));
      await tester.pump();
      expect(find.bySemanticsLabel('Hide password'), findsOneWidget);
      handle.dispose();
    });
  });

  group('InvoicesScreen', () {
    setUpAll(() async {
      OfflineStore.instance.debugUseMemory();
    });

    setUp(() async {
      InvoiceStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      await OfflineStore.instance.clear();
      ApiClient().debugConfigure();
    });

    testWidgets('the capture-invoice app-bar action exposes a label',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([])),
      );

      await tester.pumpWidget(MaterialApp(
        localizationsDelegates: AppLocalizations.localizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: const InvoicesScreen(),
      ));
      await tester.pump();

      expect(find.bySemanticsLabel('Capture invoice'), findsOneWidget);
      handle.dispose();
    });
  });

  group('ApprovalsScreen', () {
    setUpAll(() async {
      OfflineStore.instance.debugUseMemory();
    });

    setUp(() async {
      InvoiceStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      await OfflineStore.instance.clear();
      ApiClient().debugConfigure();
    });

    testWidgets('approve/reject affordances meet tap-target + label guidelines',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async => _list([_invoiceJson('1')])),
      );

      await tester.pumpWidget(_screenHost(const ApprovalsScreen()));
      await _pumpUntil(tester, find.byType(InvoiceListTile));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });
  });

  group('ExceptionListTile', () {
    testWidgets('meets tap-target, label and contrast guidelines',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        ExceptionListTile(exception: _exception(), onTap: () {}),
      ));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });

    testWidgets('announces a single composed label (type, invoice, status)',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        ExceptionListTile(exception: _exception(), onTap: () {}),
      ));
      expect(
        find.bySemanticsLabel(
          RegExp(r'Duplicate Invoice.*INV-001.*Open'),
        ),
        findsOneWidget,
      );
      handle.dispose();
    });
  });

  group('ExceptionStatusBadge', () {
    // Open (deep-amber) / escalated (red) are the worst-case hues for AA on the
    // pale tint — guard each.
    for (final status in ApExceptionStatus.values) {
      testWidgets('exposes label and clears contrast for ${status.value}',
          (tester) async {
        final handle = tester.ensureSemantics();
        await tester.pumpWidget(_host(ExceptionStatusBadge(status: status)));
        await expectLater(tester, meetsGuideline(textContrastGuideline));
        expect(find.bySemanticsLabel('Status: ${status.label}'),
            findsOneWidget);
        handle.dispose();
      });
    }
  });

  group('ExceptionsScreen', () {
    setUpAll(() async {
      OfflineStore.instance.debugUseMemory();
    });

    setUp(() async {
      ExceptionStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      await OfflineStore.instance.clear();
      ApiClient().debugConfigure();
      ExceptionStore.instance.setStatusFilter(null);
    });

    testWidgets('the loaded queue meets tap-target + label + contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async => _wrappedList([_exceptionJson('1')])),
      );

      await tester.pumpWidget(_host(const ExceptionsScreen()));
      await _pumpUntil(tester, find.byType(ExceptionListTile));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });
  });

  group('ExceptionListTile (selection mode)', () {
    testWidgets('a selected row exposes a checked state + keeps its tap target',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        ExceptionListTile(
          exception: _exception(),
          selected: true,
          onTap: () {},
          onLongPress: () {},
        ),
      ));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      // The merged row label still announces the exception.
      expect(
        find.bySemanticsLabel(RegExp(r'Duplicate Invoice.*Open')),
        findsOneWidget,
      );
      handle.dispose();
    });
  });

  group('ExceptionDetailScreen', () {
    setUp(() async {
      ExceptionStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      ApiClient().debugConfigure();
    });

    testWidgets('the loaded detail meets tap-target + label + contrast',
        (tester) async {
      // Tall surface so the whole detail list (incl. action buttons) builds.
      tester.view.physicalSize = const Size(1200, 2400);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => http.Response(
            jsonEncode(_exceptionJson('1')),
            200,
            headers: {'content-type': 'application/json'},
          ),
        ),
      );

      await tester.pumpWidget(
        _host(const ExceptionDetailScreen(exceptionId: '1')),
      );
      await _pumpUntil(tester, find.text('Duplicate Invoice'));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });
  });

  group('NotificationListTile', () {
    testWidgets('meets tap-target, label and contrast guidelines',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        NotificationListTile(notification: _notification(), onTap: () {}),
      ));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });

    testWidgets('announces one merged label leading with unread + event',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        NotificationListTile(notification: _notification(), onTap: () {}),
      ));
      expect(
        find.bySemanticsLabel(
          RegExp(r'Unread.*Invoice approved.*was approved'),
        ),
        findsOneWidget,
      );
      handle.dispose();
    });

    // A read row must still clear contrast (greyed glyph + muted title).
    testWidgets('a read row clears contrast', (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        NotificationListTile(notification: _notification(read: true)),
      ));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });
  });

  group('NotificationBell', () {
    setUp(() {
      NotificationStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      ApiClient().debugConfigure();
    });

    testWidgets('exposes an accessible label including the unread count',
        (tester) async {
      final handle = tester.ensureSemantics();
      // Seed the store with 3 unread so the badge renders.
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response(
              jsonEncode({'unread': 3}),
              200,
              headers: {'content-type': 'application/json'},
            )),
      );
      await tester.pumpWidget(_host(const NotificationBell()));
      // Wait on the real rendered signal — the badge label appearing — not the
      // store field, so the ListenableBuilder rebuild that carries the count
      // into the Semantics label is guaranteed flushed before we assert.
      await _pumpUntil(tester, find.bySemanticsLabel('Notifications, 3 unread'));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      expect(find.bySemanticsLabel('Notifications, 3 unread'), findsOneWidget);
      handle.dispose();
    });
  });

  group('NotificationsScreen', () {
    setUpAll(() async {
      OfflineStore.instance.debugUseMemory();
    });

    setUp(() async {
      NotificationStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      await OfflineStore.instance.clear();
      ApiClient().debugConfigure();
    });

    testWidgets('the loaded center meets tap-target + label + contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _notificationPage([_notificationJson('1')]),
        ),
      );

      await tester.pumpWidget(_host(const NotificationsScreen()));
      await _pumpUntil(tester, find.byType(NotificationListTile));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      // The mark-all-read action announces its purpose (WCAG 4.1.2).
      expect(find.bySemanticsLabel('Mark all notifications read'),
          findsOneWidget);
      handle.dispose();
    });
  });

  group('CashFlowScreen', () {
    setUp(() {
      CashFlowStore.instance.reset();
      ApiClient().debugConfigure();
    });

    testWidgets('the loaded forecast (with a breach) meets tap-target + '
        'label + contrast', (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path.endsWith('/analytics/cashflow_forecast')) {
            return http.Response(
              jsonEncode({
                'granularity': 'week',
                'horizon_days': 90,
                'periods': [
                  {
                    'period': '2026-W26',
                    'scheduled_amount': 3000.0,
                    'committed_amount': 2000.0,
                    'pending_amount': 1000.0,
                    'discount_eligible_amount': 0.0,
                    'count': 4,
                  },
                ],
                'totals': {
                  'scheduled_amount': 3000.0,
                  'committed_amount': 2000.0,
                  'pending_amount': 1000.0,
                  'discount_eligible_amount': 0.0,
                  'count': 4,
                  // Non-zero, so the part-conversion note renders in the
                  // sweep below too.
                  'unconverted_count': 1,
                },
              }),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          // cash_position with a breached period (exercises the red contrast).
          return http.Response(
            jsonEncode({
              'granularity': 'week',
              'horizon_days': 90,
              'opening_balance': 6000.0,
              'opening_balance_source': 'settings',
              'unconverted_count': 1,
              'threshold': 5000.0,
              'periods': [
                {
                  'period': '2026-W26',
                  'opening': 6000.0,
                  'outflow': 4000.0,
                  'inflow': 0.0,
                  'closing': 2000.0,
                  'below_threshold': true,
                },
              ],
              'breaches': [
                {'period': '2026-W26', 'closing': 2000.0, 'shortfall': 3000.0},
              ],
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }),
      );

      await tester.pumpWidget(_host(const CashFlowScreen()));
      await _pumpUntil(tester, find.byType(KpiCard));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      // Catches sub-AA red/grey money + alert text (textContrastGuideline is
      // strict — it caught earlier muted-grey defects in this suite).
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      // The low-balance alert exposes one merged announcement (WCAG 1.3.1).
      expect(find.bySemanticsLabel(RegExp('^Low balance alert')),
          findsOneWidget);
      expect(find.byType(PartialConversionNote), findsWidgets);
      handle.dispose();
    });
  });

  group('PaymentQueueScreen', () {
    setUp(() {
      PaymentQueueStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      ApiClient().debugConfigure();
    });

    testWidgets(
        'a queue carrying a blocked row + a rail-pinned row meets tap-target + '
        'label + contrast', (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path == '/api/auth/login') {
            return http.Response(jsonEncode({'access_token': 'tok'}), 200,
                headers: {'content-type': 'application/json'});
          }
          if (req.url.path.endsWith('/auth/me')) {
            return http.Response(
                jsonEncode({
                  'id': 'u1',
                  'email': 'demo@acme.com',
                  'full_name': 'Demo User',
                  'organization_id': 'org1',
                  'roles': ['ap_manager'],
                }),
                200,
                headers: {'content-type': 'application/json'});
          }
          if (req.url.path.endsWith('/payments/queue')) {
            return http.Response(
                jsonEncode({
                  'items': [
                    {
                      'id': '1',
                      'invoice_number': 'INV-1',
                      'vendor_name': 'Acme Supplies',
                      'amount': 1500.0,
                      'currency': 'USD',
                      'due_date': '2026-02-01',
                      'status': 'approved',
                      'is_overdue': false,
                      'discount_eligible': false,
                      // Refused on every rail — exercises the red reason chip.
                      'blocked': true,
                      'blocked_reason': 'duplicate',
                      'required_method': null,
                    },
                    {
                      'id': '2',
                      'invoice_number': 'INV-2',
                      'vendor_name': 'Beta Ltd',
                      'amount': 900.0,
                      'currency': 'USD',
                      'due_date': '2026-02-05',
                      'status': 'approved',
                      'is_overdue': false,
                      'discount_eligible': false,
                      // Payable, pinned to one rail — exercises the blue
                      // pinned-rail chip + the muted reason chip.
                      'blocked': false,
                      'blocked_reason': 'live_virtual_card',
                      'required_method': 'virtual_card',
                    },
                  ],
                  'total': 2,
                  'total_amount': 2400.0,
                  'total_savings': 0.0,
                }),
                200,
                headers: {'content-type': 'application/json'});
          }
          if (req.url.path.endsWith('/payments/summary')) {
            return http.Response(
                jsonEncode({
                  'total_paid': 1000.0,
                  'total_pending': 200.0,
                  'payment_count': 5,
                  'total_rebates': 12.0,
                  'queue_count': 2,
                }),
                200,
                headers: {'content-type': 'application/json'});
          }
          return http.Response(jsonEncode({'items': [], 'total': 0}), 200,
              headers: {'content-type': 'application/json'});
        }),
      );
      await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

      await tester.pumpWidget(_screenHost(const PaymentQueueScreen()));
      await _pumpUntil(tester, find.text('Acme Supplies'));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      // The two new chips render darkened text over a pale tint; this is what
      // proves they clear AA rather than shipping a colour-only cue.
      await expectLater(tester, meetsGuideline(textContrastGuideline));

      // A disabled checkbox is a colour cue on its own, so the row's single
      // merged announcement has to carry the reason (WCAG 1.3.1 / 1.4.1).
      expect(
        find.bySemanticsLabel(
            RegExp('can’t be paid: Possible duplicate — unresolved')),
        findsOneWidget,
      );
      // …and the pinned row announces which rail it is fixed to.
      expect(
        find.bySemanticsLabel(RegExp('Pay by Virtual Card')),
        findsOneWidget,
      );
      handle.dispose();
    });
  });

  group('AdminUsersScreen', () {
    setUp(() {
      AdminUserStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      ApiClient().debugConfigure();
    });

    testWidgets('the loaded user list meets tap-target + label + contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path.endsWith('/admin/roles')) {
            return http.Response(
              jsonEncode([
                {'id': 'r1', 'name': 'admin', 'is_system': true},
              ]),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response(
            jsonEncode({
              'items': [
                {
                  'id': 'u1',
                  'email': 'alice@acme.com',
                  'full_name': 'Alice Admin',
                  'is_active': true,
                  'roles': [
                    {'id': 'r1', 'name': 'admin'},
                  ],
                  'created_at': '2026-01-01T00:00:00',
                },
                {
                  'id': 'u2',
                  'email': 'bob@acme.com',
                  'full_name': 'Bob Clerk',
                  'is_active': false,
                  'roles': [
                    {'id': 'r3', 'name': 'ap_clerk'},
                  ],
                  'created_at': '2026-01-01T00:00:00',
                },
              ],
              'total': 2,
              'page': 1,
            }),
            200,
            headers: {'content-type': 'application/json'},
          );
        }),
      );

      await tester.pumpWidget(_host(const AdminUsersScreen()));
      await _pumpUntil(tester, find.text('Alice Admin'));

      // Contrast covers the small Inactive badge + role chips + muted email.
      // (Tap-target isn't asserted at the screen level here: the Material
      // `SearchBar` in the app bar is a framework 24px field — the same one the
      // vendors/invoices screens use — so it's exercised via the tile, not the
      // whole-screen sweep, matching the existing InvoicesScreen a11y test.)
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      // The deactivated user's row merges into one announcement carrying
      // "inactive" (so the Inactive badge isn't an unlabelled colour cue).
      expect(
        find.bySemanticsLabel(RegExp(r'Bob Clerk.*inactive')),
        findsOneWidget,
      );
      handle.dispose();
    });

    testWidgets('the create-user sheet meets tap-target + label + contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path.endsWith('/admin/roles')) {
            return http.Response(
              jsonEncode([
                {'id': 'r1', 'name': 'admin', 'is_system': true},
                {'id': 'r3', 'name': 'ap_clerk', 'is_system': true},
              ]),
              200,
              headers: {'content-type': 'application/json'},
            );
          }
          return http.Response(
            jsonEncode({'items': [], 'total': 0, 'page': 1}),
            200,
            headers: {'content-type': 'application/json'},
          );
        }),
      );

      await tester.pumpWidget(_host(const AdminUsersScreen()));
      await _pumpUntil(tester, find.text('No users found'));

      await tester
          .tap(find.widgetWithText(FloatingActionButton, 'Create user'));
      await tester.pumpAndSettle();

      // The form fields + role checkboxes + Create/Cancel actions clear the
      // tap-target, label and contrast guidelines.
      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      expect(find.text('New user'), findsOneWidget);
      handle.dispose();
    });
  });

  group('OrgSettingsScreen', () {
    setUp(() {
      OrgSettingsStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      ApiClient().debugConfigure();
    });

    testWidgets('the settings form meets tap-target + label + contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response(
              jsonEncode({
                'id': 'org1',
                'name': 'Acme Corp',
                'slug': 'acme',
                'plan': 'pro',
                'created_at': '2026-01-01T00:00:00',
                'settings': {
                  'company': {'address': '1 Main St'},
                  'invoice_defaults': {'currency': 'USD'},
                },
              }),
              200,
              headers: {'content-type': 'application/json'},
            )),
      );

      await tester.pumpWidget(_host(const OrgSettingsScreen()));
      await _pumpUntil(tester, find.text('Acme Corp'));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });
  });

  group('WorkflowsScreen', () {
    setUp(() {
      WorkflowStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      ApiClient().debugConfigure();
    });

    testWidgets('the loaded list meets tap-target + label + contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response(
              jsonEncode({
                'items': [
                  {
                    'id': 'wf1',
                    'name': 'Default Workflow',
                    'is_active': true,
                    'is_default': true,
                    'steps_config': {
                      'steps': [
                        {
                          'number': 1,
                          'type': 'extraction',
                          'name': 'Extract',
                          'config': {},
                        },
                      ],
                    },
                    'created_at': '2026-01-01T00:00:00',
                  },
                  {
                    'id': 'wf2',
                    'name': 'Rush Approval',
                    'is_active': false,
                    'is_default': false,
                    'steps_config': {'steps': []},
                    'created_at': '2026-01-01T00:00:00',
                  },
                ],
                'total': 2,
                'page': 1,
              }),
              200,
              headers: {'content-type': 'application/json'},
            )),
      );

      await tester.pumpWidget(_host(const WorkflowsScreen()));
      await _pumpUntil(tester, find.text('Default Workflow'));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      // Covers the Active/Inactive + Default badges (darkened-accent text).
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      // The inactive row merges into one announcement carrying "Inactive" so
      // the badge isn't an unlabelled colour cue.
      expect(
        find.bySemanticsLabel(RegExp(r'Rush Approval.*Inactive')),
        findsOneWidget,
      );
      handle.dispose();
    });
  });

  group('InspectionResultBadge', () {
    // Every outcome tint has to clear AA on its own: the badge is the only
    // place the result is stated, so a colour that fails is a colour-only cue.
    for (final result in InspectionResult.values) {
      testWidgets('the ${result.name} badge clears contrast', (tester) async {
        final handle = tester.ensureSemantics();
        await tester.pumpWidget(_host(InspectionResultBadge(result: result)));
        await tester.pump();

        await expectLater(tester, meetsGuideline(textContrastGuideline));
        handle.dispose();
      });
    }

    testWidgets('the badge announces the outcome as a labelled value',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(
        _host(const InspectionResultBadge(result: InspectionResult.fail)),
      );
      await tester.pump();

      expect(find.bySemanticsLabel('Result: Fail'), findsOneWidget);
      handle.dispose();
    });
  });

  group('InspectionsScreen', () {
    setUp(() {
      InspectionStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      ApiClient().debugConfigure();
    });

    testWidgets('the loaded list meets tap-target + label + contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async {
          if (req.url.path == '/api/auth/login') {
            return http.Response(jsonEncode({'access_token': 'tok'}), 200,
                headers: {'content-type': 'application/json'});
          }
          if (req.url.path.endsWith('/auth/me')) {
            return http.Response(
                jsonEncode({
                  'id': 'u1',
                  'email': 'demo@acme.com',
                  'full_name': 'Demo User',
                  'organization_id': 'org1',
                  'roles': ['ap_manager'],
                }),
                200,
                headers: {'content-type': 'application/json'});
          }
          return http.Response(
              jsonEncode({
                'items': [
                  {
                    'id': '1',
                    'inspection_number': 'QI-FAIL',
                    'po_id': 'po1',
                    'gr_id': 'gr1',
                    'gr_number': 'GR-1',
                    'result': 'fail',
                    'inspected_date': '2026-03-04',
                    'inspector': 'Dana',
                    'accepted_quantity': 0.0,
                    'rejected_quantity': 10.0,
                    'deviation_notes': null,
                    'status': 'completed',
                    'created_at': '2026-03-04T09:00:00',
                  },
                  {
                    'id': '2',
                    'inspection_number': 'QI-ORPHAN',
                    'po_id': null,
                    'gr_id': null,
                    'gr_number': null,
                    'result': 'partial',
                    'inspected_date': null,
                    'inspector': null,
                    'accepted_quantity': 4.0,
                    'rejected_quantity': null,
                    'deviation_notes': null,
                    'status': 'completed',
                    'created_at': '2026-03-04T09:00:00',
                  },
                ],
                'total': 2,
                'page': 1,
              }),
              200,
              headers: {'content-type': 'application/json'});
        }),
      );
      await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

      await tester.pumpWidget(_screenHost(const InspectionsScreen()));
      await _pumpUntil(tester, find.byType(InspectionListTile));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      // Covers the outcome badges (darkened accent over a pale tint) and the
      // muted row subtitles.
      await expectLater(tester, meetsGuideline(textContrastGuideline));

      // One merged announcement per row, carrying the outcome — the badge is
      // otherwise a colour-only cue.
      expect(
        find.bySemanticsLabel(RegExp(r'QI-FAIL, Fail, GR-1')),
        findsOneWidget,
      );
      // An unlinked row says so in the same announcement.
      expect(
        find.bySemanticsLabel(RegExp(r'QI-ORPHAN, Partial acceptance, Not linked')),
        findsOneWidget,
      );
      handle.dispose();
    });
  });

  group('InspectionDetailScreen', () {
    setUp(() {
      InspectionStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      ApiClient().debugConfigure();
    });

    testWidgets('a failed, unlinked inspection meets label + contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(
        client: MockClient((req) async => http.Response(
              jsonEncode({
                'id': 'qi1',
                'inspection_number': 'QI-ORPHAN',
                'po_id': null,
                'gr_id': null,
                'gr_number': null,
                'result': 'fail',
                'inspected_date': '2026-03-04',
                'inspector': 'Dana',
                'accepted_quantity': 0.0,
                'rejected_quantity': 12.0,
                'deviation_notes': 'Two cartons crushed',
                'status': 'completed',
                'created_at': '2026-03-04T09:00:00',
              }),
              200,
              headers: {'content-type': 'application/json'},
            )),
      );

      await tester.pumpWidget(
        _screenHost(const InspectionDetailScreen(inspectionId: 'qi1')),
      );
      await _pumpUntil(tester, find.text('QI-ORPHAN'));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      // The consequence note and the amber "no match will read this" callout
      // both render tinted text — brown.shade800 over a pale orange, which is
      // what clears AA where a true orange does not.
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });
  });

  group('AdaptiveScreen', () {
    setUp(() {
      AdaptiveStore.instance.reset();
      FlutterSecureStorage.setMockInitialValues({});
      ApiClient().debugConfigure();
    });

    /// Serves the three adaptive reads plus the login pair.
    MockClient adaptiveClient() => MockClient((req) async {
          final path = req.url.path;
          http.Response ok(Object body) => http.Response(
                jsonEncode(body),
                200,
                headers: {'content-type': 'application/json'},
              );
          if (path == '/api/auth/login') return ok({'access_token': 'tok'});
          if (path.endsWith('/auth/me')) {
            return ok({
              'id': 'u1',
              'email': 'demo@acme.com',
              'full_name': 'Demo User',
              'organization_id': 'org1',
              'roles': ['ap_manager'],
            });
          }
          if (path.endsWith('/approval-patterns')) {
            return ok({
              'generated_at': '2026-03-04T10:00:00',
              'lookback_days': 180,
              'entity_id': null,
              'approvers': [
                {
                  'approver_id': 'u1',
                  'approver_name': 'Ada Lovelace',
                  'approved_count': 12,
                  'rejected_count': 1,
                  'approval_rate_pct': '92.31',
                  'median_time_to_approve_days': '1.50',
                  'avg_time_to_approve_days': '2.10',
                  'sample_size': 13,
                },
              ],
              'vendors': [
                {
                  'vendor_id': 'v1',
                  'vendor_name': 'Acme Supplies',
                  'approved_count': 14,
                  'rejected_count': 0,
                  'approval_rate_pct': '100.00',
                  'unmodified_count': 14,
                  'consistency_pct': '100.00',
                  'avg_approved_amount': '1200.00',
                  'median_approved_amount': '1150.00',
                  'min_approved_amount': '400.00',
                  'max_approved_amount': '1900.00',
                  'sample_size': 14,
                  // Non-zero, so the amber disclosure line renders too.
                  'unconverted_count': 2,
                },
              ],
            });
          }
          if (path.endsWith('/anomalies')) {
            return ok({
              'total_scanned': 7,
              'flagged': [
                {
                  'invoice_id': 'inv1',
                  'vendor_id': 'v1',
                  'vendor_name': 'Acme Supplies',
                  'amount': '9000.00',
                  'amount_currency': 'EUR',
                  'insufficient_history': false,
                  'flags': [
                    {
                      'code': 'amount_outlier',
                      'severity': 'warning',
                      'message': 'Amount is far above the usual range.',
                      'observed': '9000.00',
                      'expected': '1200.00',
                    },
                    {
                      'code': 'approver_unusual',
                      'severity': 'error',
                      'message': 'Proposed approver never approved this vendor.',
                      'observed': 'u9',
                      'expected': 'u1',
                    },
                  ],
                },
              ],
            });
          }
          return ok({
            'suggestions': [
              {
                'id': 's1',
                'kind': 'auto_approve_threshold',
                'vendor_id': 'v1',
                'vendor_name': 'Acme',
                'title': 'Vendor Acme: 14/14 invoices approved unmodified',
                'rationale': '14 invoices approved with no corrections.',
                'payload': {'suggested_threshold': '2000.00'},
                'confidence_pct': '95.00',
                'status': 'open',
                'created_at': '2026-03-01T10:00:00',
                'dismissed_at': null,
              },
            ],
          });
        });

    testWidgets('the suggestions tab meets tap-target + label + contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(client: adaptiveClient());
      await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

      await tester.pumpWidget(_screenHost(const AdaptiveScreen()));
      await _pumpUntil(tester, find.text('Dismiss'));

      await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      // Covers the status badge (darkened accent over a pale tint) and the
      // muted rationale / confidence text.
      await expectLater(tester, meetsGuideline(textContrastGuideline));

      // One merged announcement per card, carrying the status so the badge is
      // not a colour-only cue.
      expect(
        find.bySemanticsLabel(RegExp(r'Vendor Acme.*Confidence 95.00%, Open')),
        findsOneWidget,
      );
      handle.dispose();
    });

    testWidgets('the patterns and anomalies tabs meet label + contrast',
        (tester) async {
      final handle = tester.ensureSemantics();
      ApiClient().debugConfigure(client: adaptiveClient());
      await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');

      await tester.pumpWidget(_screenHost(const AdaptiveScreen()));
      await _pumpUntil(tester, find.text('Dismiss'));

      await tester.tap(find.widgetWithText(Tab, 'Approval patterns'));
      await tester.pumpAndSettle();
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      // The unconverted-approvals disclosure renders in the amber-reading
      // brown.shade800; a true orange would fail here.
      await expectLater(tester, meetsGuideline(textContrastGuideline));

      await tester.tap(find.widgetWithText(Tab, 'Anomalies'));
      await tester.pumpAndSettle();
      await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
      // Covers both severity tints (error red.shade900, warning brown.shade800).
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });
  });
}
