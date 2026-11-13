import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/widgets/kpi_card.dart';

Widget _host(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  testWidgets('renders title and value', (tester) async {
    await tester.pumpWidget(_host(
      const KpiCard(
        title: 'Total Outstanding',
        value: '\$12,345',
        icon: Icons.payments,
      ),
    ));
    expect(find.text('Total Outstanding'), findsOneWidget);
    expect(find.text('\$12,345'), findsOneWidget);
  });

  testWidgets('renders the subtitle when provided', (tester) async {
    await tester.pumpWidget(_host(
      const KpiCard(
        title: 'Pending',
        value: '7',
        subtitle: '3 overdue',
        icon: Icons.timelapse,
      ),
    ));
    expect(find.text('3 overdue'), findsOneWidget);
  });

  testWidgets('omits the subtitle Text when not provided', (tester) async {
    await tester.pumpWidget(_host(
      const KpiCard(title: 'Paid', value: '42', icon: Icons.check),
    ));
    // Only title + value Text widgets, no third subtitle line.
    expect(find.byType(Text), findsNWidgets(2));
  });

  testWidgets('uses the supplied accent color for the icon', (tester) async {
    await tester.pumpWidget(_host(
      const KpiCard(
        title: 'Rebates',
        value: '\$99',
        icon: Icons.savings,
        color: Colors.purple,
      ),
    ));
    final icon = tester.widget<Icon>(find.byIcon(Icons.savings));
    expect(icon.color, Colors.purple);
  });
}
