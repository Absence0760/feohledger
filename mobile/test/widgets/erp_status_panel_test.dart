import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/audit_entry.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/widgets/erp_status_panel.dart';

Widget _host(Widget child) => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: SingleChildScrollView(child: child)),
    );

Invoice _inv(InvoiceStatus status, {String? erpDocumentId}) => Invoice(
      id: 'inv1',
      invoiceNumber: 'INV-001',
      vendorName: 'Acme',
      amount: 100,
      currency: 'USD',
      status: status,
      createdAt: DateTime(2026, 1, 1),
      erpDocumentId: erpDocumentId,
    );

AuditEntry _entry(
  String action, {
  Map<String, dynamic>? details,
  DateTime? at,
}) =>
    AuditEntry(
      id: 'a-$action',
      actorId: 'u1',
      actorName: 'Demo User',
      action: action,
      entityType: 'invoice',
      entityId: 'inv1',
      details: details,
      createdAt: at ?? DateTime(2026, 1, 2, 10),
    );

void main() {
  group('ErpInfo.fromAuditLog', () {
    test('returns null when no ERP action is present', () {
      expect(
        ErpInfo.fromAuditLog([_entry('invoice.uploaded')]),
        isNull,
      );
    });

    test('picks the latest erp_* / completed entry (oldest-first list)', () {
      final info = ErpInfo.fromAuditLog([
        _entry('invoice.uploaded', at: DateTime(2026, 1, 1)),
        _entry('invoice.erp_submitted', at: DateTime(2026, 1, 2)),
        _entry(
          'invoice.erp_confirmed',
          details: {'erp_reference': 'ERP-555'},
          at: DateTime(2026, 1, 3),
        ),
      ]);
      expect(info, isNotNull);
      expect(info!.erpReference, 'ERP-555');
      // erp_confirmed has no friendly label → falls back to the raw verb.
      expect(info.actionLabel, 'invoice.erp_confirmed');
    });

    test('captures the error from a failed entry', () {
      final info = ErpInfo.fromAuditLog([
        _entry('invoice.erp_failed', details: {'error': 'Connection refused'}),
      ]);
      expect(info!.lastError, 'Connection refused');
    });
  });

  group('ErpStatusPanel', () {
    testWidgets('hides for a non-ERP status', (tester) async {
      await tester.pumpWidget(_host(
        ErpStatusPanel(invoice: _inv(InvoiceStatus.readyForReview), erpInfo: null),
      ));
      expect(find.text('ERP Status'), findsNothing);
    });

    testWidgets('shows the panel for an ERP-bound status', (tester) async {
      final info = ErpInfo.fromAuditLog([
        _entry('invoice.erp_confirmed', details: {'erp_reference': 'ERP-555'}),
      ]);
      await tester.pumpWidget(_host(
        ErpStatusPanel(
          invoice: _inv(InvoiceStatus.sentToErp, erpDocumentId: 'ERP-555'),
          erpInfo: info,
        ),
      ));
      expect(find.text('ERP Status'), findsOneWidget);
      expect(find.text('ERP Reference'), findsOneWidget);
      expect(find.text('ERP-555'), findsOneWidget);
    });

    testWidgets('a done invoice names its bill from the invoice row',
        (tester) async {
      // Found live: after a confirmed push the newest ERP audit entry is
      // `invoice.completed`, which carries no reference, and `done` is not an
      // ERP-bound status, so a posted invoice showed nothing at all.
      final info = ErpInfo.fromAuditLog([
        _entry('invoice.erp_confirmed', details: {'erp_reference': 'BILL-1'}),
        _entry('invoice.completed', at: DateTime(2026, 1, 2, 11)),
      ]);
      final parsed = Invoice.fromJson({
        'id': 'inv1',
        'status': 'done',
        'created_at': '2026-01-01T00:00:00',
        'erp_document_id': 'BILL-1',
      });
      expect(parsed.erpDocumentId, 'BILL-1');
      await tester.pumpWidget(_host(
        ErpStatusPanel(
          invoice: _inv(InvoiceStatus.done, erpDocumentId: 'BILL-1'),
          erpInfo: info,
        ),
      ));
      expect(find.text('ERP Status'), findsOneWidget);
      expect(find.text('BILL-1'), findsOneWidget);
    });

    testWidgets('a done invoice never posted shows no panel', (tester) async {
      await tester.pumpWidget(_host(
        ErpStatusPanel(invoice: _inv(InvoiceStatus.done), erpInfo: null),
      ));
      expect(find.text('ERP Status'), findsNothing);
    });

    testWidgets('shows on a failed status when ERP context exists',
        (tester) async {
      final info = ErpInfo.fromAuditLog([
        _entry('invoice.erp_failed', details: {'error': 'Timeout'}),
      ]);
      await tester.pumpWidget(_host(
        ErpStatusPanel(invoice: _inv(InvoiceStatus.failed), erpInfo: info),
      ));
      expect(find.text('ERP Status'), findsOneWidget);
      expect(find.text('Error'), findsOneWidget);
      expect(find.text('Timeout'), findsOneWidget);
    });

    testWidgets('shows a status fallback line when sending with no detail yet',
        (tester) async {
      await tester.pumpWidget(_host(
        ErpStatusPanel(invoice: _inv(InvoiceStatus.sendingToErp), erpInfo: null),
      ));
      expect(find.text('ERP Status'), findsOneWidget);
      expect(find.text('Sending to ERP'), findsOneWidget);
    });

    testWidgets('clears contrast guideline', (tester) async {
      final handle = tester.ensureSemantics();
      final info = ErpInfo.fromAuditLog([
        _entry('invoice.erp_failed', details: {'error': 'Timeout'}),
      ]);
      await tester.pumpWidget(_host(
        ErpStatusPanel(invoice: _inv(InvoiceStatus.failed), erpInfo: info),
      ));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });
  });
}
