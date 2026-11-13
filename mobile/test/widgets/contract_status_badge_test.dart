import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/contract.dart';
import 'package:feohledger_mobile/widgets/contract_status_badge.dart';

Widget _host(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  testWidgets('renders the status label text', (tester) async {
    await tester.pumpWidget(_host(
      const ContractStatusBadge(status: ContractStatus.active),
    ));
    expect(find.text('Active'), findsOneWidget);
  });

  testWidgets('renders a label for every status without throwing',
      (tester) async {
    for (final status in ContractStatus.values) {
      await tester.pumpWidget(_host(ContractStatusBadge(status: status)));
      expect(find.text(status.label), findsOneWidget,
          reason: 'missing label for ${status.value}');
    }
  });

  testWidgets('tints the text by status color (active = darkened green)',
      (tester) async {
    await tester.pumpWidget(_host(
      const ContractStatusBadge(status: ContractStatus.active),
    ));
    final text = tester.widget<Text>(find.text('Active'));
    // Darkened to shade900 for WCAG AA contrast (≥4.5:1) against the pale tint.
    expect(text.style?.color, Colors.green.shade900);
  });

  testWidgets('passes the text-contrast accessibility guideline',
      (tester) async {
    await tester.pumpWidget(_host(
      const ContractStatusBadge(status: ContractStatus.expired),
    ));
    await expectLater(tester, meetsGuideline(textContrastGuideline));
  });
}
