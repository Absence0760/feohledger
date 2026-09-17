import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/utils/format_locale.dart';
import 'package:feohledger_mobile/widgets/invoice_warnings_panel.dart';

/// Mirrors `APApp`: the delegates localize the copy and `FormatLocaleScope`
/// points the money/date formatters at the same locale, so a figure inside a
/// warning is formatted the way the app really formats it.
Widget _host(Widget child, {Locale? locale}) => MaterialApp(
      locale: locale,
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      builder: (context, inner) =>
          FormatLocaleScope(child: inner ?? const SizedBox.shrink()),
      home: Scaffold(body: SingleChildScrollView(child: child)),
    );

void main() {
  testWidgets('renders nothing when there are no warnings and no PO match',
      (tester) async {
    await tester.pumpWidget(_host(
      const InvoiceWarningsPanel(warnings: [], poMatch: null),
    ));
    expect(find.byType(SizedBox), findsWidgets); // the shrink placeholder
    expect(find.text('Warnings & fraud flags'), findsNothing);
    expect(find.text('PO Match'), findsNothing);
  });

  testWidgets('renders nothing when the only PO match is no_po', (tester) async {
    await tester.pumpWidget(_host(
      const InvoiceWarningsPanel(
        warnings: [],
        poMatch: PoMatch(matchType: 'none', status: 'no_po'),
      ),
    ));
    expect(find.text('PO Match'), findsNothing);
  });

  testWidgets('lists each warning message under the section header',
      (tester) async {
    await tester.pumpWidget(_host(
      const InvoiceWarningsPanel(
        warnings: [
          InvoiceWarning(
            type: 'duplicate',
            severity: WarningSeverity.warning,
            message: 'Duplicate invoice number for this vendor',
          ),
          InvoiceWarning(
            type: 'missing_field',
            severity: WarningSeverity.error,
            message: 'Missing vendor name',
          ),
        ],
        poMatch: null,
      ),
    ));

    expect(find.text('Warnings & fraud flags'), findsOneWidget);
    expect(find.text('Duplicate invoice number for this vendor'),
        findsOneWidget);
    expect(find.text('Missing vendor name'), findsOneWidget);
  });

  testWidgets('renders the PO match panel with variance and issues',
      (tester) async {
    await tester.pumpWidget(_host(
      const InvoiceWarningsPanel(
        warnings: [],
        poMatch: PoMatch(
          matchType: '3-way',
          status: 'mismatch',
          variancePct: 12.5,
          withinTolerance: false,
          issues: ['Amount variance of 12.5%'],
        ),
      ),
    ));

    expect(find.text('PO Match'), findsOneWidget);
    expect(find.text('3-way match'), findsOneWidget);
    expect(find.text('Mismatch'), findsOneWidget);
    expect(find.text('+12.5% variance'), findsOneWidget);
    expect(find.text('• Amount variance of 12.5%'), findsOneWidget);
  });

  testWidgets('exposes one merged semantics label per warning', (tester) async {
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(_host(
      const InvoiceWarningsPanel(
        warnings: [
          InvoiceWarning(
            type: 'missing_field',
            severity: WarningSeverity.error,
            message: 'Missing vendor name',
          ),
        ],
        poMatch: null,
      ),
    ));
    expect(
      find.bySemanticsLabel('Error: Missing vendor name'),
      findsOneWidget,
    );
    await expectLater(tester, meetsGuideline(textContrastGuideline));
    handle.dispose();
  });

  // The panel used to render `warning.message`, which the backend composes in
  // English from the row's own data — so a German reviewer read German chrome
  // around an English finding. `code` + `params` is what makes the sentence
  // translatable (`backend/docs/invoice-warnings.md`).
  group('a coded warning is stated in the reader\'s language', () {
    const coded = InvoiceWarning(
      type: 'po_mismatch',
      severity: WarningSeverity.error,
      message: 'PO PO-4412 not found',
      code: 'po_not_found',
      params: {'poNumber': 'PO-4412'},
    );

    testWidgets('German copy and a German figure', (tester) async {
      await tester.pumpWidget(_host(
        const InvoiceWarningsPanel(
          warnings: [
            coded,
            InvoiceWarning(
              type: 'fraud_flag',
              severity: WarningSeverity.warning,
              message: 'Round amount: 5000.00 EUR',
              code: 'round_amount',
              params: {'amount': '5000.00', 'currency': 'EUR'},
            ),
          ],
          poMatch: null,
        ),
        locale: const Locale('de'),
      ));

      expect(find.text('Bestellung PO-4412 nicht gefunden'), findsOneWidget);
      expect(find.text('PO PO-4412 not found'), findsNothing);
      // The money inside the sentence is German too — grouped with dots, the
      // decimal comma, symbol last.
      expect(find.textContaining('Runder Betrag: 5.000,00'), findsOneWidget);
    });

    testWidgets('the merged announcement carries the localized sentence',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        const InvoiceWarningsPanel(warnings: [coded], poMatch: null),
        locale: const Locale('de'),
      ));
      // One label, not the German visible text beside an English announcement.
      expect(
        find.bySemanticsLabel('Fehler: Bestellung PO-4412 nicht gefunden'),
        findsOneWidget,
      );
      handle.dispose();
    });

    testWidgets('an uncoded warning still shows the server sentence',
        (tester) async {
      // The normal path for every row persisted before the catalogue existed.
      await tester.pumpWidget(_host(
        const InvoiceWarningsPanel(
          warnings: [
            InvoiceWarning(
              type: 'duplicate',
              severity: WarningSeverity.warning,
              message: 'Duplicate invoice number for this vendor',
            ),
          ],
          poMatch: null,
        ),
        locale: const Locale('de'),
      ));
      expect(
        find.text('Duplicate invoice number for this vendor'),
        findsOneWidget,
      );
    });
  });
}
