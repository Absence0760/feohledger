import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/widgets/invoice_edit_sheet.dart';

// Regression guard for the financial freeze on the invoice edit sheet.
//
// The backend refuses a PATCH that carries ANY of `_FINANCIAL_FIELDS` once the
// invoice is financially locked (`models/invoice_status.financially_locked`:
// `approved` or later, or a failed ERP push after approval — decisions §370) —
// and it refuses the WHOLE request, so a combined description + amount edit
// used to 409 and silently lose the description as well. The sheet renders the
// frozen fields read-only and omits them from the diff, from the server's own
// `financially_locked` verdict rather than a status list.

Invoice _invoice(InvoiceStatus status, {required bool locked}) => Invoice(
      id: 'inv-1',
      invoiceNumber: 'INV-001',
      vendorName: 'Acme Supplies',
      amount: 1500,
      currency: 'USD',
      status: status,
      description: 'Original description',
      poNumber: 'PO-1',
      glAccount: '6000',
      createdAt: DateTime(2026, 1, 1),
      financiallyLocked: locked,
    );

/// Opens the sheet the way the detail screen does. Returns a reader for
/// whatever `showInvoiceEditSheet` pops, so the assertions run against the real
/// result rather than a re-implementation of the diff.
Future<InvoiceEditResult? Function()> _openSheet(
  WidgetTester tester,
  Invoice inv,
) async {
  InvoiceEditResult? popped;
  await tester.pumpWidget(MaterialApp(
    localizationsDelegates: appLocalizationsDelegates,
    supportedLocales: AppLocalizations.supportedLocales,
    home: Builder(
      builder: (context) => Scaffold(
        body: Center(
          child: ElevatedButton(
            onPressed: () async {
              popped = await showInvoiceEditSheet(context, inv);
            },
            child: const Text('open'),
          ),
        ),
      ),
    ),
  ));
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
  return () => popped;
}

/// The `TextField` behind the `TextFormField` carrying [label].
TextField _fieldFor(WidgetTester tester, String label) =>
    tester.widget<TextField>(
      find.ancestor(of: find.text(label), matching: find.byType(TextField)),
    );

Future<void> _enterInto(
  WidgetTester tester,
  String label,
  String value,
) async {
  final field = find.ancestor(
    of: find.text(label),
    matching: find.byType(TextFormField),
  );
  await tester.ensureVisible(field);
  await tester.enterText(field, value);
  await tester.pump();
}

Future<void> _tapButton(WidgetTester tester, String label) async {
  final button = find.text(label);
  await tester.ensureVisible(button);
  await tester.tap(button);
  await tester.pumpAndSettle();
}

void main() {
  group('unlocked invoice', () {
    testWidgets('amount and vendor stay editable and ride the diff',
        (tester) async {
      final result = await _openSheet(
        tester,
        _invoice(InvoiceStatus.readyForReview, locked: false),
      );

      expect(_fieldFor(tester, 'Amount').readOnly, isFalse);
      expect(_fieldFor(tester, 'Vendor').readOnly, isFalse);
      expect(find.text('Frozen after approval'), findsNothing);

      await _enterInto(tester, 'Amount', '1600.50');
      await _enterInto(tester, 'Description', 'Updated description');
      await _tapButton(tester, 'Save');

      expect(result(), {
        // Money crosses the wire as a string-Decimal, never a float.
        'amount': '1600.50',
        'description': 'Updated description',
      });
    });
  });

  group('financially locked invoice', () {
    testWidgets('renders the money + payee fields read-only with a reason',
        (tester) async {
      await _openSheet(tester, _invoice(InvoiceStatus.approved, locked: true));

      expect(_fieldFor(tester, 'Amount').readOnly, isTrue);
      expect(_fieldFor(tester, 'Vendor').readOnly, isTrue);
      // Non-financial fields are untouched — an approved invoice may still have
      // its GL coding / notes corrected.
      expect(_fieldFor(tester, 'Description').readOnly, isFalse);
      expect(_fieldFor(tester, 'GL Account').readOnly, isFalse);
      expect(_fieldFor(tester, 'Invoice #').readOnly, isFalse);

      // The reviewer is told why, and how to change it: the exit that exists
      // (recall the approval, backend decisions §374), not a rejection the
      // server refuses on an approved invoice.
      expect(
        find.textContaining('recall the approval, correct the invoice'),
        findsOneWidget,
      );
      expect(find.textContaining('reject'), findsNothing);
      expect(find.text('Frozen after approval'), findsNWidgets(2));
      // The frozen value stays legible — read-only, not hidden.
      expect(find.text('1500'), findsOneWidget);
      expect(find.text('Acme Supplies'), findsOneWidget);
    });

    testWidgets('a non-financial edit still goes through', (tester) async {
      final result = await _openSheet(tester, _invoice(InvoiceStatus.approved, locked: true));

      await _enterInto(tester, 'Description', 'Corrected coding note');
      await _enterInto(tester, 'GL Account', '6100');
      await _tapButton(tester, 'Save');

      final changes = result();
      expect(changes, {
        'description': 'Corrected coding note',
        'gl_account': '6100',
      });
      for (final field in kFinancialInvoiceFields) {
        expect(changes!.containsKey(field), isFalse, reason: field);
      }
    });

    testWidgets('every post-approval status locks the same way',
        (tester) async {
      for (final status in [
        InvoiceStatus.approved,
        InvoiceStatus.sendingToErp,
        InvoiceStatus.paid,
        InvoiceStatus.done,
        // An approved invoice whose ERP push failed (decisions §370).
        InvoiceStatus.failed,
      ]) {
        await _openSheet(tester, _invoice(status, locked: true));
        expect(
          _fieldFor(tester, 'Amount').readOnly,
          isTrue,
          reason: status.value,
        );
        expect(
          _fieldFor(tester, 'Vendor').readOnly,
          isTrue,
          reason: status.value,
        );
        // Close it before the next iteration re-opens over the same navigator.
        await _tapButton(tester, 'Cancel');
      }
    });
  });

  group('the verdict, not the status, decides', () {
    testWidgets('a failed extraction (never approved) stays editable',
        (tester) async {
      final result = await _openSheet(
        tester,
        _invoice(InvoiceStatus.failed, locked: false),
      );

      expect(_fieldFor(tester, 'Amount').readOnly, isFalse);
      expect(find.text('Frozen after approval'), findsNothing);
      await _enterInto(tester, 'Amount', '1499.99');
      await _tapButton(tester, 'Save');
      expect(result(), {'amount': '1499.99'});
    });

    testWidgets('a failed ERP push after approval strips the money fields',
        (tester) async {
      final result = await _openSheet(
        tester,
        _invoice(InvoiceStatus.failed, locked: true),
      );

      expect(_fieldFor(tester, 'Amount').readOnly, isTrue);
      await _enterInto(tester, 'Description', 'Retry note');
      await _tapButton(tester, 'Save');
      expect(result(), {'description': 'Retry note'});
    });
  });
}
