import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/widgets/status_pill.dart';

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

/// Outcome chip for a quality inspection: the localized outcome in a
/// [StatusPill], announced as "Result: `<outcome>`".
class InspectionResultBadge extends StatelessWidget {
  final InspectionResult result;

  const InspectionResultBadge({super.key, required this.result});

  static StatusTone toneFor(InspectionResult result) => switch (result) {
    InspectionResult.pass => StatusTone.success,
    InspectionResult.fail => StatusTone.danger,
    InspectionResult.partial => StatusTone.warning,
    InspectionResult.unknown => StatusTone.neutral,
  };

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final label = inspectionResultLabel(l, result);
    return StatusPill(
      label: label,
      tone: toneFor(result),
      semanticsLabel: l.inspectionsResultAnnounce(label),
    );
  }
}
