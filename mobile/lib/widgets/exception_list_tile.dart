import 'package:flutter/material.dart';

import 'package:feohledger_mobile/models/exception.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/widgets/exception_status_badge.dart';

/// One row in the exception queue: type, related invoice + vendor + amount,
/// severity and status. Composes a single screen-reader announcement.
class ExceptionListTile extends StatelessWidget {
  final ApException exception;
  final VoidCallback? onTap;
  final VoidCallback? onLongPress;

  /// In multi-select mode, whether this row is in the selection — renders a
  /// leading checkbox and exposes a `checked` Semantics state.
  final bool selected;

  const ExceptionListTile({
    super.key,
    required this.exception,
    this.onTap,
    this.onLongPress,
    this.selected = false,
  });

  // Map severity to a darkened accent that clears AA contrast at 12px on white.
  Color _severityColor() => switch (exception.severity) {
    ApExceptionSeverity.error => Colors.red.shade700,
    ApExceptionSeverity.warning => Colors.orange.shade900,
    ApExceptionSeverity.info => Colors.grey.shade700,
  };

  // A single, sensible screen-reader announcement for the whole row instead of
  // letting the reader walk several disjoint Text spans (WCAG 1.3.1 / 4.1.2).
  String get _semanticLabel {
    final parts = <String>[
      exception.typeLabel,
      '${exception.severity.label} severity',
      if (exception.vendorName != null) exception.vendorName!,
      if (exception.invoiceNumber != null)
        'invoice ${exception.invoiceNumber}',
      if (exception.amount != null) _amount,
      exception.status.label,
      if (exception.isOverdue) 'overdue',
    ];
    return parts.join(', ');
  }

  /// The related invoice's amount, in the related invoice's currency —
  /// `/api/exceptions` joins the code through for this row.
  String get _amount =>
      formatMoney(exception.amount, currency: exception.currency);

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: _semanticLabel,
      button: onTap != null,
      selected: selected,
      excludeSemantics: true,
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
        onTap: onTap,
        onLongPress: onLongPress,
        selected: selected,
        leading: selected
            ? const Icon(Icons.check_circle, color: Colors.blue)
            : null,
        title: Row(
          children: [
            Expanded(
              child: Text(
                exception.typeLabel,
                style: const TextStyle(fontWeight: FontWeight.w600),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            if (exception.amount != null)
              Text(
                _amount,
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
          ],
        ),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (exception.vendorName != null ||
                  exception.invoiceNumber != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 4),
                  child: Text(
                    [
                      if (exception.vendorName != null) exception.vendorName!,
                      if (exception.invoiceNumber != null)
                        exception.invoiceNumber!,
                    ].join(' · '),
                    style: TextStyle(
                      color: Colors.grey.shade700,
                      fontSize: 13,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              Row(
                children: [
                  Text(
                    exception.severity.label,
                    style: TextStyle(
                      color: _severityColor(),
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(width: 12),
                  ExceptionStatusBadge(status: exception.status),
                  const Spacer(),
                  if (exception.isOverdue)
                    Text(
                      'Overdue',
                      style: TextStyle(
                        color: Colors.red.shade700,
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}
