import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/audit_entry.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/widgets/erp_status_panel.dart';
import 'package:feohledger_mobile/widgets/external_link.dart';

Widget _host(Widget child) => MaterialApp(
      localizationsDelegates: appLocalizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: SingleChildScrollView(child: child)),
    );

Invoice _inv(
  InvoiceStatus status, {
  String? erpDocumentId,
  String? erpDocumentUrl,
}) =>
    Invoice(
      id: 'inv1',
      invoiceNumber: 'INV-001',
      vendorName: 'Acme',
      amount: 100,
      currency: 'USD',
      status: status,
      createdAt: DateTime(2026, 1, 1),
      erpDocumentId: erpDocumentId,
      erpDocumentUrl: erpDocumentUrl,
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

    group('the bill link (decisions §354)', () {
      const bill = '8f1c2a3b-0000-4000-8000-1234567890ab';
      const url = 'https://go.xero.com/organisationlogin/default.aspx'
          '?shortcode=!mBdtL'
          '&redirecturl=/AccountsPayable/View.aspx?InvoiceID=$bill';

      testWidgets('parses erp_document_url and opens it when tapped',
          (tester) async {
        final parsed = Invoice.fromJson({
          'id': 'inv1',
          'status': 'done',
          'created_at': '2026-01-01T00:00:00',
          'erp_document_id': bill,
          'erp_document_url': url,
        });
        expect(parsed.erpDocumentUrl, url);

        final opened = <Uri>[];
        await tester.pumpWidget(_host(
          ErpStatusPanel(
            invoice: parsed,
            erpInfo: null,
            opener: (uri) async {
              opened.add(uri);
              return true;
            },
          ),
        ));
        final link = find.byKey(const ValueKey('erp-reference-link'));
        expect(link, findsOneWidget);
        expect(
          find.descendant(of: link, matching: find.text(bill)),
          findsOneWidget,
        );

        await tester.tap(link);
        await tester.pump();
        expect(opened, [Uri.parse(url)]);
        // The organisation survives the round trip into the launched URL.
        expect(opened.single.queryParameters['shortcode'], '!mBdtL');
        expect(find.byType(SnackBar), findsNothing);
      });

      testWidgets('is one actionable link node naming the bill',
          (tester) async {
        final handle = tester.ensureSemantics();
        await tester.pumpWidget(_host(
          ErpStatusPanel(
            invoice: _inv(InvoiceStatus.done,
                erpDocumentId: bill, erpDocumentUrl: url),
            erpInfo: null,
            opener: (_) async => true,
          ),
        ));
        final node = tester.getSemantics(
          find.byKey(const ValueKey('erp-reference-link')),
        );
        expect(
          node,
          matchesSemantics(
            label: '$bill, opens the bill in the ERP',
            isLink: true,
            hasTapAction: true,
          ),
        );
        await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
        await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
        await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
        await expectLater(tester, meetsGuideline(textContrastGuideline));
        handle.dispose();
      });

      testWidgets('reads in screen order: title, reference, then details',
          (tester) async {
        // The details used to ride the title's merged label, so a screen
        // reader heard them before the reference that sits above them.
        final handle = tester.ensureSemantics();
        final info = ErpInfo.fromAuditLog([
          _entry('invoice.erp_failed', details: {'error': 'Timeout'}),
        ]);
        await tester.pumpWidget(_host(
          ErpStatusPanel(
            invoice: _inv(InvoiceStatus.failed,
                erpDocumentId: bill, erpDocumentUrl: url),
            erpInfo: info,
            opener: (_) async => true,
          ),
        ));

        expect(find.bySemanticsLabel('ERP Status'), findsOneWidget);
        final details = tester.getSemantics(
          find.byKey(const ValueKey('erp-status-details')),
        );
        expect(details.label, startsWith('Error Timeout, Last update '));
        expect(details.label, contains('by Demo User'));

        // Vertical order on screen is the reading order.
        final title = tester.getTopLeft(find.text('ERP Status')).dy;
        final link = tester
            .getTopLeft(find.byKey(const ValueKey('erp-reference-link')))
            .dy;
        final detailRow = tester
            .getTopLeft(find.byKey(const ValueKey('erp-status-details')))
            .dy;
        expect(title, lessThan(link));
        expect(link, lessThan(detailRow));
        handle.dispose();
      });

      testWidgets('says so when no app can open it', (tester) async {
        await tester.pumpWidget(_host(
          ErpStatusPanel(
            invoice: _inv(InvoiceStatus.done,
                erpDocumentId: bill, erpDocumentUrl: url),
            erpInfo: null,
            opener: (_) async => false,
          ),
        ));
        await tester.tap(find.byKey(const ValueKey('erp-reference-link')));
        await tester.pump();
        expect(find.text("Couldn't open the link"), findsOneWidget);
      });

      for (final bad in [
        null,
        '',
        'http://go.xero.com/x',
        'javascript:alert(1)',
        '/AccountsPayable/View.aspx',
      ]) {
        testWidgets('a non-https URL ($bad) stays plain text', (tester) async {
          await tester.pumpWidget(_host(
            ErpStatusPanel(
              invoice: _inv(InvoiceStatus.done,
                  erpDocumentId: bill, erpDocumentUrl: bad),
              erpInfo: null,
              opener: (_) async => fail('must not launch'),
            ),
          ));
          expect(find.byKey(const ValueKey('erp-reference-link')), findsNothing);
          expect(find.byType(ExternalLink), findsNothing);
          expect(find.text(bill), findsOneWidget);
        });
      }
    });

    testWidgets('the last update names its actor in the reader\'s language',
        (tester) async {
      final info = ErpInfo.fromAuditLog([
        _entry('invoice.erp_failed', details: {'error': 'Timeout'}),
      ]);
      await tester.pumpWidget(MaterialApp(
        locale: const Locale('fr'),
        localizationsDelegates: appLocalizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        home: Scaffold(
          body: ErpStatusPanel(
            invoice: _inv(InvoiceStatus.failed),
            erpInfo: info,
          ),
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.textContaining('par Demo User'), findsOneWidget);
      expect(find.textContaining(' by '), findsNothing);
    });

    testWidgets('an unlinked panel announces every row, last update included',
        (tester) async {
      final handle = tester.ensureSemantics();
      final info = ErpInfo.fromAuditLog([
        _entry('invoice.erp_failed', details: {'error': 'Timeout'}),
      ]);
      await tester.pumpWidget(_host(
        ErpStatusPanel(invoice: _inv(InvoiceStatus.failed), erpInfo: info),
      ));
      expect(
        find.bySemanticsLabel(
          RegExp(r'^ERP Status, Error Timeout, Last update .* by Demo User'),
        ),
        findsOneWidget,
      );
      handle.dispose();
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
