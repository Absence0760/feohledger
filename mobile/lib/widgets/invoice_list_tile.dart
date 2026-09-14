import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/widgets/status_badge.dart';

class InvoiceListTile extends StatelessWidget {
  final Invoice invoice;
  final VoidCallback? onTap;
  final VoidCallback? onLongPress;

  /// Multi-select mode — when true a leading checkbox is shown and the row
  /// tint reflects [selected]. Defaults off so existing call sites are
  /// unaffected.
  final bool selectionMode;
  final bool selected;

  const InvoiceListTile({
    super.key,
    required this.invoice,
    this.onTap,
    this.onLongPress,
    this.selectionMode = false,
    this.selected = false,
  });

  // A single, sensible screen-reader announcement for the whole row, instead
  // of letting the reader walk 4-5 disjoint Text spans (WCAG 1.3.1 / 4.1.2).
  String get _semanticLabel {
    final parts = <String>[
      if (selectionMode) selected ? 'Selected' : 'Not selected',
      invoice.vendorName ?? 'Unknown Vendor',
      if (invoice.amount != null) _amount,
      if (invoice.invoiceNumber != null) 'invoice ${invoice.invoiceNumber}',
      invoice.status.label,
      if (invoice.dueDate != null)
        '${invoice.dueDate!.isBefore(DateTime.now()) ? 'past due' : 'due'} '
            '${DateFormat('MMMM d').format(invoice.dueDate!)}',
    ];
    return parts.join(', ');
  }

  /// The row's amount in the INVOICE's own currency — a multi-currency tenant
  /// has rows in several, so the org's reporting currency is not the answer
  /// here. A row carrying no code renders its digits with no symbol.
  String get _amount =>
      formatMoney(invoice.amount, currency: invoice.currency);

  @override
  Widget build(BuildContext context) {
    // Merge the inner spans into one announcement; the row stays a single
    // focusable button for assistive tech when it's tappable. In selection
    // mode it reads as a checkbox toggle.
    return Semantics(
      label: _semanticLabel,
      button: onTap != null && !selectionMode,
      checked: selectionMode ? selected : null,
      excludeSemantics: true,
      child: _buildTile(),
    );
  }

  Widget _buildTile() {
    return ListTile(
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      onTap: onTap,
      onLongPress: onLongPress,
      selected: selectionMode && selected,
      selectedTileColor: Colors.blue.withValues(alpha: 0.08),
      leading: selectionMode
          ? Checkbox(
              value: selected,
              // The whole row handles the toggle (onTap); keep the box in sync
              // but route the gesture through the row so the tap target is full
              // width (≥48dp) and one announcement covers it.
              onChanged: onTap == null ? null : (_) => onTap!(),
            )
          : null,
      title: Row(
        children: [
          Expanded(
            child: Text(
              invoice.vendorName ?? 'Unknown Vendor',
              style: const TextStyle(fontWeight: FontWeight.w600),
              overflow: TextOverflow.ellipsis,
            ),
          ),
          if (invoice.amount != null)
            Text(
              _amount,
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
        ],
      ),
      subtitle: Padding(
        padding: const EdgeInsets.only(top: 4),
        child: Row(
          children: [
            if (invoice.invoiceNumber != null) ...[
              Text(
                invoice.invoiceNumber!,
                style: TextStyle(
                  color: Colors.grey.shade600,
                  fontSize: 13,
                ),
              ),
              const SizedBox(width: 12),
            ],
            StatusBadge(status: invoice.status),
            const Spacer(),
            if (invoice.dueDate != null)
              Text(
                DateFormat('MMM d').format(invoice.dueDate!),
                style: TextStyle(
                  // Darkened so the due-date label clears AA contrast at 12px
                  // against white (plain Colors.red / grey.shade500 fail).
                  color: invoice.dueDate!.isBefore(DateTime.now())
                      ? Colors.red.shade700
                      : Colors.grey.shade700,
                  fontSize: 12,
                ),
              ),
          ],
        ),
      ),
    );
  }
}
