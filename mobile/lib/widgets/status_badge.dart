import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/enum_labels.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/widgets/status_pill.dart';

/// Status chip for an invoice: the localized state name in a [StatusPill].
class StatusBadge extends StatelessWidget {
  final InvoiceStatus status;

  const StatusBadge({super.key, required this.status});

  static StatusTone toneFor(InvoiceStatus status) => switch (status) {
    InvoiceStatus.newStatus => StatusTone.info,
    InvoiceStatus.pending => StatusTone.warning,
    InvoiceStatus.readyForReview => StatusTone.attention,
    InvoiceStatus.approved => StatusTone.success,
    InvoiceStatus.rejected || InvoiceStatus.failed => StatusTone.danger,
    InvoiceStatus.sendingToErp ||
    InvoiceStatus.sentToErp ||
    InvoiceStatus.postedInErp => StatusTone.progress,
    InvoiceStatus.paymentScheduled => StatusTone.scheduled,
    InvoiceStatus.paid || InvoiceStatus.done => StatusTone.settled,
  };

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final label = invoiceStatusLabel(l, status);
    return StatusPill(
      label: label,
      tone: toneFor(status),
      semanticsLabel: l.statusPillAnnounce(label),
    );
  }
}
