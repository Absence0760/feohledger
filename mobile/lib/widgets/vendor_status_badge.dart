import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/enum_labels.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/vendor.dart';
import 'package:feohledger_mobile/widgets/status_pill.dart';

/// Status chip for a vendor: the localized lifecycle state in a [StatusPill].
class VendorStatusBadge extends StatelessWidget {
  final VendorStatus status;

  const VendorStatusBadge({super.key, required this.status});

  static StatusTone toneFor(VendorStatus status) => switch (status) {
    VendorStatus.active => StatusTone.success,
    VendorStatus.unverified => StatusTone.warning,
    VendorStatus.inactive => StatusTone.muted,
    VendorStatus.rejected => StatusTone.danger,
  };

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final label = vendorStatusLabel(l, status);
    return StatusPill(
      label: label,
      tone: toneFor(status),
      semanticsLabel: l.statusPillAnnounce(label),
    );
  }
}
