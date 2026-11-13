import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/audit_entry.dart';
import 'package:feohledger_mobile/widgets/activity_timeline.dart';

Widget _host(Widget child) => MaterialApp(
      localizationsDelegates: appLocalizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Scaffold(body: child),
    );

AuditEntry _entry({
  String id = 'a1',
  String action = 'invoice.uploaded',
  String? actorName = 'Demo User',
  Map<String, dynamic>? details,
  String createdAt = '2026-01-01T10:00:00',
}) =>
    AuditEntry(
      id: id,
      actorId: 'u1',
      actorName: actorName,
      action: action,
      entityType: 'invoice',
      entityId: '1',
      details: details,
      createdAt: DateTime.parse(createdAt),
    );

void main() {
  group('ActivityTimeline', () {
    testWidgets('renders an empty state when there are no entries',
        (tester) async {
      await tester.pumpWidget(_host(const ActivityTimeline(entries: [])));

      expect(find.text('No activity yet'), findsOneWidget);
    });

    testWidgets('renders one row per entry with its friendly action label',
        (tester) async {
      await tester.pumpWidget(_host(
        ActivityTimeline(entries: [
          _entry(action: 'invoice.uploaded'),
          _entry(id: 'a2', action: 'invoice.approved'),
        ]),
      ));

      expect(find.text('Uploaded invoice'), findsOneWidget);
      expect(find.text('Approved'), findsOneWidget);
      expect(find.text('No activity yet'), findsNothing);
    });

    testWidgets('renders the per-field before/after diff for an edit event',
        (tester) async {
      await tester.pumpWidget(_host(
        ActivityTimeline(entries: [
          _entry(
            action: 'invoice.edited',
            details: {
              'changes': {
                'amount': {'old': '100.00', 'new': '250.00'},
                'vendor_name': {'old': 'Acme', 'new': 'Globex'},
              },
            },
          ),
        ]),
      ));

      expect(find.text('Edited fields'), findsOneWidget);
      // The before/after values render as RichText spans (humanised labels).
      expect(find.textContaining('100.00', findRichText: true), findsOneWidget);
      expect(find.textContaining('250.00', findRichText: true), findsOneWidget);
      expect(find.textContaining('Globex', findRichText: true), findsOneWidget);
    });

    testWidgets('shows a reject reason note', (tester) async {
      await tester.pumpWidget(_host(
        ActivityTimeline(entries: [
          _entry(
            action: 'invoice.rejected',
            details: {'reason': 'Wrong amount'},
          ),
        ]),
      ));

      expect(find.text('Rejected'), findsOneWidget);
      expect(find.text('Wrong amount'), findsOneWidget);
    });

    testWidgets('each entry exposes one composed semantics label',
        (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        ActivityTimeline(entries: [
          _entry(
            action: 'invoice.edited',
            details: {
              'changes': {
                'amount': {'old': '100.00', 'new': '250.00'},
              },
            },
          ),
        ]),
      ));

      // One sensible phrase: action + actor + change, not disjoint fragments.
      expect(
        find.bySemanticsLabel(
          RegExp(r'Edited fields by Demo User.*Amount changed from 100.00 to 250.00'),
        ),
        findsOneWidget,
      );
      handle.dispose();
    });

    testWidgets('clears the text-contrast guideline', (tester) async {
      final handle = tester.ensureSemantics();
      await tester.pumpWidget(_host(
        ActivityTimeline(entries: [_entry(action: 'invoice.approved')]),
      ));

      await expectLater(tester, meetsGuideline(textContrastGuideline));
      handle.dispose();
    });
  });
}
