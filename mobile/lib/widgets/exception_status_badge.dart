import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/enum_labels.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/exception.dart';
import 'package:feohledger_mobile/widgets/status_pill.dart';

/// Status chip for an exception: the localized queue state in a [StatusPill].
class ExceptionStatusBadge extends StatelessWidget {
  final ApExceptionStatus status;

  const ExceptionStatusBadge({super.key, required this.status});

  static StatusTone toneFor(ApExceptionStatus status) => switch (status) {
    ApExceptionStatus.open => StatusTone.warning,
    ApExceptionStatus.escalated => StatusTone.danger,
    ApExceptionStatus.resolved => StatusTone.success,
    ApExceptionStatus.dismissed => StatusTone.neutral,
  };

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final label = exceptionStatusLabel(l, status);
    return StatusPill(
      label: label,
      tone: toneFor(status),
      semanticsLabel: l.statusPillAnnounce(label),
    );
  }
}
