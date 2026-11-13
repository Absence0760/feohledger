import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/inspection.dart';

/// Localized label for an inspection outcome. Lives here because the badge, the
/// filter chips, the detail screen and the record form all need the same words,
/// and the model deliberately holds no English (unlike the older badges, whose
/// enum `label` maps predate the i18n pass).
String inspectionResultLabel(AppLocalizations l, InspectionResult result) =>
    switch (result) {
      InspectionResult.pass => l.inspectionsResultPass,
      InspectionResult.fail => l.inspectionsResultFail,
      InspectionResult.partial => l.inspectionsResultPartial,
      // A value outside the vocabulary (the column is free-form and a row can
      // arrive from a QMS sync) is named as unknown rather than coloured in as
      // one of the three — a mis-tinted `pass` is a wrong answer about whether
      // goods were accepted.
      InspectionResult.unknown => l.inspectionsResultUnknown,
    };

/// Outcome chip for a quality inspection. Mirrors the other mobile status
/// badges: a darkened foreground over a 0.15-alpha tint of the accent, so the
/// text clears WCAG 1.4.3 (≥4.5:1) — the full-saturation hue does not.
class InspectionResultBadge extends StatelessWidget {
  final InspectionResult result;

  const InspectionResultBadge({super.key, required this.result});

  Color get _color => switch (result) {
    InspectionResult.pass => Colors.green,
    InspectionResult.fail => Colors.red,
    InspectionResult.partial => Colors.orange,
    InspectionResult.unknown => Colors.blueGrey,
  };

  // Orange cannot reach 4.5:1 at 12px bold as a true orange, so `partial` uses
  // the dark brown that reads as deep amber — the same substitution the
  // exception badge makes for `open`.
  Color get _textColor => switch (result) {
    InspectionResult.pass => Colors.green.shade900,
    InspectionResult.fail => Colors.red.shade900,
    InspectionResult.partial => Colors.brown.shade800,
    InspectionResult.unknown => Colors.blueGrey.shade900,
  };

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final label = inspectionResultLabel(l, result);
    return Semantics(
      label: l.inspectionsResultAnnounce(label),
      excludeSemantics: true,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: _color.withValues(alpha: 0.15),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: _color.withValues(alpha: 0.4)),
        ),
        child: Text(
          label,
          style: TextStyle(
            color: _textColor,
            fontSize: 12,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
    );
  }
}
