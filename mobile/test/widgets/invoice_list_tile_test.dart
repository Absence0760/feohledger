import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/widgets/invoice_list_tile.dart';

Widget _host(Widget child) => MaterialApp(home: Scaffold(body: child));

Invoice _invoice({
  String? vendorName = 'Acme Supplies',
  String? invoiceNumber = 'INV-001',
  double? amount = 1500,
  String? currency = 'USD',
  DateTime? dueDate,
}) =>
    Invoice(
      id: 'inv1',
      invoiceNumber: invoiceNumber,
      vendorName: vendorName,
      amount: amount,
      currency: currency,
      status: InvoiceStatus.readyForReview,
      dueDate: dueDate,
      createdAt: DateTime(2026, 1, 1),
    );

void main() {
  testWidgets('shows vendor, invoice number, formatted amount and status',
      (tester) async {
    await tester.pumpWidget(_host(InvoiceListTile(invoice: _invoice())));

    expect(find.text('Acme Supplies'), findsOneWidget);
    expect(find.text('INV-001'), findsOneWidget);
    expect(find.text('\$1,500.00'), findsOneWidget);
    // StatusBadge label
    expect(find.text('Ready for Review'), findsOneWidget);
  });

  testWidgets('formats the amount in the INVOICE\'s own currency',
      (tester) async {
    // A multi-currency tenant's list holds rows in several currencies, so the
    // org's reporting currency is not the answer for a row — the row's own
    // code is. This tile used to hold a module-level dollar formatter, so
    // every row read as dollars whatever the invoice said.
    await tester.pumpWidget(
      _host(InvoiceListTile(invoice: _invoice(currency: 'EUR'))),
    );

    expect(find.text('€1,500.00'), findsOneWidget);
    expect(find.textContaining(r'$'), findsNothing);
  });

  testWidgets('a zero-decimal currency loses its decimal places',
      (tester) async {
    // JPY has no minor unit. `¥1,500.00` is a figure that cannot exist, and it
    // is what a two-decimal formatter prints.
    await tester.pumpWidget(
      _host(InvoiceListTile(invoice: _invoice(currency: 'JPY'))),
    );

    expect(find.text('¥1,500'), findsOneWidget);
  });

  testWidgets('an invoice with no currency renders the bare figure',
      (tester) async {
    await tester.pumpWidget(
      _host(InvoiceListTile(invoice: _invoice(currency: null))),
    );

    expect(find.text('1,500.00'), findsOneWidget);
    expect(find.textContaining(r'$'), findsNothing);
  });

  testWidgets('falls back to "Unknown Vendor" when vendor name is null',
      (tester) async {
    await tester.pumpWidget(_host(
      InvoiceListTile(invoice: _invoice(vendorName: null)),
    ));
    expect(find.text('Unknown Vendor'), findsOneWidget);
  });

  testWidgets('hides the amount when it is null', (tester) async {
    await tester.pumpWidget(_host(
      InvoiceListTile(invoice: _invoice(amount: null)),
    ));
    expect(find.textContaining('\$'), findsNothing);
  });

  testWidgets('fires onTap when the row is tapped', (tester) async {
    var tapped = false;
    await tester.pumpWidget(_host(
      InvoiceListTile(invoice: _invoice(), onTap: () => tapped = true),
    ));
    await tester.tap(find.byType(InvoiceListTile));
    expect(tapped, isTrue);
  });

  testWidgets('paints a past due date in red', (tester) async {
    final overdue = DateTime.now().subtract(const Duration(days: 5));
    await tester.pumpWidget(_host(
      InvoiceListTile(invoice: _invoice(dueDate: overdue)),
    ));
    // The due-date label is the only Text rendered with a red color.
    final reds = tester.widgetList<Text>(find.byType(Text)).where(
          (t) => t.style?.color == Colors.red.shade700,
        );
    expect(reds, isNotEmpty);
  });

  testWidgets('does not paint a future due date in red', (tester) async {
    final future = DateTime.now().add(const Duration(days: 30));
    await tester.pumpWidget(_host(
      InvoiceListTile(invoice: _invoice(dueDate: future)),
    ));
    final reds = tester.widgetList<Text>(find.byType(Text)).where(
          (t) => t.style?.color == Colors.red.shade700,
        );
    expect(reds, isEmpty);
  });
}
