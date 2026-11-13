import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/widgets/advanced_search_sheet.dart';

/// Host that opens the sheet via [showAdvancedSearchSheet] so we exercise the
/// real Navigator.pop result contract.
Widget _host(
  InvoiceSearchFilters initial,
  void Function(InvoiceSearchFilters?) onResult,
) {
  return MaterialApp(
    localizationsDelegates: appLocalizationsDelegates,
    supportedLocales: AppLocalizations.supportedLocales,
    home: Scaffold(
      body: Builder(
        builder: (context) => Center(
          child: ElevatedButton(
            onPressed: () async {
              final r = await showAdvancedSearchSheet(context, initial);
              onResult(r);
            },
            child: const Text('open'),
          ),
        ),
      ),
    ),
  );
}

void main() {
  testWidgets('renders the four filter inputs seeded from the initial filters',
      (tester) async {
    await tester.pumpWidget(_host(
      const InvoiceSearchFilters(vendor: 'Acme', poNumber: 'PO-9'),
      (_) {},
    ));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('Advanced Search'), findsOneWidget);
    expect(find.widgetWithText(TextFormField, 'Vendor'), findsOneWidget);
    expect(find.widgetWithText(TextFormField, 'PO Number'), findsOneWidget);
    expect(find.widgetWithText(TextFormField, 'Min amount'), findsOneWidget);
    expect(find.widgetWithText(TextFormField, 'Max amount'), findsOneWidget);
    // Seeded values are present.
    expect(find.text('Acme'), findsOneWidget);
    expect(find.text('PO-9'), findsOneWidget);
  });

  testWidgets('Apply returns the entered filters', (tester) async {
    InvoiceSearchFilters? result;
    await tester.pumpWidget(_host(InvoiceSearchFilters.empty, (r) => result = r));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.enterText(
        find.widgetWithText(TextFormField, 'Vendor'), 'Globex');
    await tester.enterText(
        find.widgetWithText(TextFormField, 'Min amount'), '100');
    await tester.tap(find.text('Apply'));
    await tester.pumpAndSettle();

    expect(result, isNotNull);
    expect(result!.vendor, 'Globex');
    expect(result!.amountMin, 100);
    expect(result!.isEmpty, isFalse);
  });

  testWidgets('Clear returns the empty filters', (tester) async {
    InvoiceSearchFilters? result;
    await tester.pumpWidget(_host(
      const InvoiceSearchFilters(vendor: 'Acme'),
      (r) => result = r,
    ));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Clear'));
    await tester.pumpAndSettle();

    expect(result, isNotNull);
    expect(result!.isEmpty, isTrue);
  });

  testWidgets('blocks Apply when min amount exceeds max', (tester) async {
    InvoiceSearchFilters? result;
    await tester.pumpWidget(_host(InvoiceSearchFilters.empty, (r) => result = r));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.enterText(
        find.widgetWithText(TextFormField, 'Min amount'), '500');
    await tester.enterText(
        find.widgetWithText(TextFormField, 'Max amount'), '100');
    await tester.tap(find.text('Apply'));
    await tester.pumpAndSettle();

    // Validation failed → sheet stays open, no result delivered.
    expect(find.text('Advanced Search'), findsOneWidget);
    expect(find.text('Min must not exceed max'), findsOneWidget);
    expect(result, isNull);
  });

  testWidgets('meets a11y guidelines and labels the icon-only controls',
      (tester) async {
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(_host(InvoiceSearchFilters.empty, (_) {}));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
    await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
    await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
    await expectLater(tester, meetsGuideline(textContrastGuideline));
    expect(find.bySemanticsLabel('Close advanced search'), findsOneWidget);
    handle.dispose();
  });
}
