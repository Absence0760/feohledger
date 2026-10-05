import 'package:flutter/material.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';

/// Bottom action bar shown while the invoice list is in multi-select mode.
/// Surfaces the selected-count plus the bulk actions (export / status-change /
/// delete). Pure presentation — the parent owns the store, the selection and
/// the confirmations; this just renders enabled/disabled buttons and forwards
/// taps. Reused shape so any future bulk surface (e.g. vendors) can adopt it.
/// An action whose callback is null is omitted, so a surface can opt in to only
/// the actions it supports.
///
/// Every label is localized. A surface whose two slots mean something other
/// than "change status" / "delete" (the exception queue maps them to resolve /
/// dismiss) passes its own already-localized [statusLabel] / [deleteLabel]
/// rather than letting the generic word misname the action.
class BulkActionBar extends StatelessWidget {
  final int selectedCount;
  final bool busy;
  final VoidCallback? onExport;
  final VoidCallback? onStatusChange;
  final VoidCallback? onDelete;
  final String? statusLabel;
  final String? deleteLabel;

  const BulkActionBar({
    super.key,
    required this.selectedCount,
    this.busy = false,
    this.onExport,
    this.onStatusChange,
    this.onDelete,
    this.statusLabel,
    this.deleteLabel,
  });

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final hasSelection = selectedCount > 0 && !busy;
    return Material(
      elevation: 8,
      child: SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          child: Row(
            children: [
              // The count yields the row's spare width to the actions: a
              // translated count ("3 éléments sélectionnés") is several times
              // the English one, and a fixed-width Text beside three buttons
              // overflowed the bar. Ellipsized, it still announces in full.
              Expanded(
                child: Text(
                  l.bulkSelectedCount(selectedCount),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
              ),
              if (busy)
                const Padding(
                  padding: EdgeInsets.only(right: 12),
                  child: SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  ),
                ),
              if (onExport != null) ...[
                TextButton.icon(
                  onPressed: hasSelection ? onExport : null,
                  icon: const Icon(Icons.ios_share),
                  label: Text(l.bulkActionExport),
                ),
                const SizedBox(width: 4),
              ],
              if (onStatusChange != null) ...[
                TextButton.icon(
                  onPressed: hasSelection ? onStatusChange : null,
                  icon: const Icon(Icons.swap_horiz),
                  label: Text(statusLabel ?? l.bulkActionStatus),
                ),
                const SizedBox(width: 4),
              ],
              if (onDelete != null)
                TextButton.icon(
                  onPressed: hasSelection ? onDelete : null,
                  icon: Icon(Icons.delete_outline, color: Colors.red.shade700),
                  label: Text(
                    deleteLabel ?? l.commonDelete,
                    // shade700 keeps the destructive label at AA contrast.
                    style: TextStyle(color: Colors.red.shade700),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
