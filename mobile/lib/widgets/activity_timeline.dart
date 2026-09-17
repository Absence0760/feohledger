import 'package:flutter/material.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/audit_entry.dart';
import 'package:feohledger_mobile/utils/dates.dart';

/// Vertical activity timeline rendering an invoice's audit-log entries
/// (`GET /api/invoices/{id}/audit-log`). Each entry shows the action label, the
/// actor (when known), the timestamp, and — for edit / approve-with-corrections
/// events — the per-field before → after diff from `details.changes`.
///
/// The host screen owns fetching + the loading / error states; this widget only
/// renders a resolved [entries] list (or its own empty state when the list is
/// empty). Each entry merges into a single screen-reader announcement.
class ActivityTimeline extends StatelessWidget {
  final List<AuditEntry> entries;

  const ActivityTimeline({super.key, required this.entries});

  @override
  Widget build(BuildContext context) {
    if (entries.isEmpty) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 16),
        child: Row(
          children: [
            // Decorative — the adjacent text carries the meaning.
            ExcludeSemantics(
              child: Icon(Icons.history, size: 20, color: Colors.grey.shade700),
            ),
            const SizedBox(width: 8),
            Text(
              AppLocalizations.of(context).timelineNoActivity,
              style: TextStyle(color: Colors.grey.shade700),
            ),
          ],
        ),
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (var i = 0; i < entries.length; i++)
          _TimelineRow(
            entry: entries[i],
            isLast: i == entries.length - 1,
          ),
      ],
    );
  }
}

class _TimelineRow extends StatelessWidget {
  final AuditEntry entry;
  final bool isLast;

  const _TimelineRow({required this.entry, required this.isLast});

  @override
  Widget build(BuildContext context) {
    final when = formatDateTime(entry.createdAt.toLocal(), separator: ' • ');
    final actor = entry.actorName;
    final changes = entry.changes;
    final note = entry.detailNote;

    // One composed phrase for assistive tech instead of disjoint fragments.
    final semanticLabel = StringBuffer(entry.actionLabel);
    if (actor != null && actor.isNotEmpty) semanticLabel.write(' by $actor');
    semanticLabel.write(', $when');
    if (note != null) semanticLabel.write('. $note');
    for (final c in changes) {
      semanticLabel.write(
        '. ${_fieldLabel(c.field)} changed from ${c.oldDisplay} to ${c.newDisplay}',
      );
    }

    return Semantics(
      label: semanticLabel.toString(),
      excludeSemantics: true,
      child: IntrinsicHeight(
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Dot + connecting rail (decorative).
            Column(
              children: [
                Container(
                  width: 12,
                  height: 12,
                  margin: const EdgeInsets.only(top: 4),
                  decoration: BoxDecoration(
                    color: Theme.of(context).colorScheme.primary,
                    shape: BoxShape.circle,
                  ),
                ),
                if (!isLast)
                  Expanded(
                    child: Container(
                      width: 2,
                      color: Colors.grey.shade300,
                    ),
                  ),
              ],
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Padding(
                padding: EdgeInsets.only(bottom: isLast ? 0 : 16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      entry.actionLabel,
                      style: const TextStyle(fontWeight: FontWeight.w600),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      actor != null && actor.isNotEmpty
                          ? '$actor • $when'
                          : when,
                      // shade700 keeps muted text at AA contrast (per a11y conventions).
                      style: TextStyle(
                        fontSize: 12,
                        color: Colors.grey.shade700,
                      ),
                    ),
                    if (note != null) ...[
                      const SizedBox(height: 4),
                      Text(note, style: const TextStyle(fontSize: 13)),
                    ],
                    if (changes.isNotEmpty) ...[
                      const SizedBox(height: 6),
                      for (final c in changes) _ChangeLine(change: c),
                    ],
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ChangeLine extends StatelessWidget {
  final AuditFieldChange change;

  const _ChangeLine({required this.change});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 2),
      child: RichText(
        text: TextSpan(
          style: DefaultTextStyle.of(context).style.copyWith(fontSize: 13),
          children: [
            TextSpan(
              text: '${_fieldLabel(change.field)}: ',
              style: TextStyle(color: Colors.grey.shade700),
            ),
            TextSpan(
              text: change.oldDisplay,
              style: const TextStyle(decoration: TextDecoration.lineThrough),
            ),
            const TextSpan(text: ' → '),
            TextSpan(
              text: change.newDisplay,
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
          ],
        ),
      ),
    );
  }
}

/// Turn a snake_case audit field key into a human label ("vendor_name" →
/// "Vendor name"). Keeps unknown keys readable without a hardcoded map.
String _fieldLabel(String field) {
  final words = field.replaceAll('_', ' ').trim();
  if (words.isEmpty) return field;
  return words[0].toUpperCase() + words.substring(1);
}
