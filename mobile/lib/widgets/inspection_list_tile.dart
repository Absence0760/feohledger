import 'package:flutter/material.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/widgets/inspection_result_badge.dart';

/// One quality-inspection row: inspection number + outcome badge, with the
/// goods receipt it covers and when it was inspected underneath.
class InspectionListTile extends StatelessWidget {
  final Inspection inspection;
  final VoidCallback? onTap;

  const InspectionListTile({
    super.key,
    required this.inspection,
    this.onTap,
  });

  /// The receipt this inspection covers. "Not linked" is a real state, not a
  /// missing value: an inspection naming no receipt and no PO is one the 4-way
  /// match will never read, so the row says so instead of rendering a blank.
  String _receiptLabel(AppLocalizations l) {
    if (inspection.grNumber != null) return inspection.grNumber!;
    if (inspection.isUnlinked) return l.inspectionsNotLinked;
    // Linked to a receipt the join could not name — a different fact again.
    return l.inspectionsReceiptUnnamed;
  }

  // One merged announcement per row instead of walking each disjoint span
  // (WCAG 1.3.1 / 4.1.2).
  String _semanticLabel(AppLocalizations l) {
    final parts = <String>[
      inspection.inspectionNumber,
      inspectionResultLabel(l, inspection.result),
      _receiptLabel(l),
      if (inspection.inspectedDate != null)
        formatDate(inspection.inspectedDate!),
      if (inspection.inspector != null && inspection.inspector!.isNotEmpty)
        inspection.inspector!,
    ];
    return parts.join(', ');
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final date = inspection.inspectedDate;
    return Semantics(
      label: _semanticLabel(l),
      button: onTap != null,
      excludeSemantics: true,
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
        onTap: onTap,
        title: Row(
          children: [
            Expanded(
              child: Text(
                inspection.inspectionNumber,
                style: const TextStyle(fontWeight: FontWeight.w600),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            InspectionResultBadge(result: inspection.result),
          ],
        ),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Row(
            children: [
              Flexible(
                child: Text(
                  _receiptLabel(l),
                  style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              const Spacer(),
              if (date != null)
                Text(
                  formatDate(date),
                  style: TextStyle(color: Colors.grey.shade700, fontSize: 12),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
