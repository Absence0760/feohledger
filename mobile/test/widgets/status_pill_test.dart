import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/widgets/status_pill.dart';

/// WCAG 2.x contrast ratio between two opaque colours.
double _contrast(Color a, Color b) {
  final la = a.computeLuminance();
  final lb = b.computeLuminance();
  final hi = la > lb ? la : lb;
  final lo = la > lb ? lb : la;
  return (hi + 0.05) / (lo + 0.05);
}

void main() {
  // The pill's text is 12px, so WCAG 1.4.3's normal-text bar (4.5:1) applies.
  // Computed, not eyeballed: a new tone, or a lighter shade on an old one,
  // fails here before it ships.
  for (final tone in StatusTone.values) {
    test('${tone.name} text clears 4.5:1 against the pill background', () {
      expect(
        tone.background.a,
        1.0,
        reason: 'the background must be opaque, or the checked contrast '
            'depends on whatever surface the pill sits on',
      );
      final ratio = _contrast(tone.textColor, tone.background);
      expect(
        ratio,
        greaterThanOrEqualTo(4.5),
        reason: '${tone.name}: ${ratio.toStringAsFixed(2)}:1',
      );
    });
  }

  testWidgets('every tone passes the text-contrast guideline, in light and '
      'dark themes', (tester) async {
    for (final theme in [ThemeData.light(), ThemeData.dark()]) {
      await tester.pumpWidget(MaterialApp(
        theme: theme,
        home: Scaffold(
          body: Wrap(
            children: [
              for (final tone in StatusTone.values)
                StatusPill(
                  label: tone.name,
                  tone: tone,
                  semanticsLabel: 'Status: ${tone.name}',
                ),
            ],
          ),
        ),
      ));
      await expectLater(tester, meetsGuideline(textContrastGuideline));
    }
  });

  testWidgets('announces the semantics label, not the bare word',
      (tester) async {
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(const MaterialApp(
      home: Scaffold(
        body: StatusPill(
          label: 'Approved',
          tone: StatusTone.success,
          semanticsLabel: 'Status: Approved',
        ),
      ),
    ));
    expect(find.bySemanticsLabel('Status: Approved'), findsOneWidget);
    expect(find.bySemanticsLabel('Approved'), findsNothing);
    handle.dispose();
  });
}
