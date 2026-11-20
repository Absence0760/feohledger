import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/enum_labels.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/contract.dart';
import 'package:feohledger_mobile/widgets/status_pill.dart';

/// Status chip for a contract: the localized lifecycle state in a
/// [StatusPill].
class ContractStatusBadge extends StatelessWidget {
  final ContractStatus status;

  const ContractStatusBadge({super.key, required this.status});

  static StatusTone toneFor(ContractStatus status) => switch (status) {
    ContractStatus.draft => StatusTone.neutral,
    ContractStatus.active => StatusTone.success,
    ContractStatus.expired => StatusTone.warning,
    ContractStatus.terminated => StatusTone.danger,
    ContractStatus.cancelled => StatusTone.muted,
  };

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final label = contractStatusLabel(l, status);
    return StatusPill(
      label: label,
      tone: toneFor(status),
      semanticsLabel: l.statusPillAnnounce(label),
    );
  }
}
