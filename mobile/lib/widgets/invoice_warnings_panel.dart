import 'package:flutter/material.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/invoice_warning_messages.dart';
import 'package:feohledger_mobile/models/invoice.dart';

/// Detail-screen panel that surfaces an invoice's warnings / fraud flags and
/// (when present) its latest PO-match result — the same signals the web invoice
/// modal renders. Pure presentation over `Invoice.warnings` + `Invoice.poMatch`;
/// renders nothing when there's nothing to show.
class InvoiceWarningsPanel extends StatelessWidget {
  final List<InvoiceWarning> warnings;
  final PoMatch? poMatch;

  const InvoiceWarningsPanel({
    super.key,
    required this.warnings,
    this.poMatch,
  });

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final hasPoMatch = poMatch != null && !poMatch!.isNoPo;
    if (warnings.isEmpty && !hasPoMatch) return const SizedBox.shrink();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (warnings.isNotEmpty) ...[
          _SectionTitle(l.warningsSectionTitle),
          const SizedBox(height: 8),
          for (final w in warnings) ...[
            _WarningTile(warning: w),
            const SizedBox(height: 8),
          ],
        ],
        if (hasPoMatch) ...[
          if (warnings.isNotEmpty) const SizedBox(height: 8),
          _SectionTitle(l.warningsPoMatchTitle),
          const SizedBox(height: 8),
          _PoMatchTile(match: poMatch!),
        ],
      ],
    );
  }
}

class _SectionTitle extends StatelessWidget {
  final String text;
  const _SectionTitle(this.text);

  @override
  Widget build(BuildContext context) {
    return Text(
      text,
      style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
    );
  }
}

/// Colour + foreground for a severity, calibrated like `StatusBadge` so the
/// text clears WCAG 1.4.3 (≥4.5:1) over the 0.12-alpha tint.
({Color tint, Color fg, IconData icon}) _severityStyle(
  WarningSeverity s,
) {
  return switch (s) {
    WarningSeverity.error => (
        tint: Colors.red,
        fg: Colors.red.shade900,
        icon: Icons.error_outline,
      ),
    WarningSeverity.warning => (
        tint: Colors.orange,
        // brown.shade800 reads as deep amber and clears AA at the small size.
        fg: Colors.brown.shade800,
        icon: Icons.warning_amber_outlined,
      ),
    WarningSeverity.info => (
        tint: Colors.blue,
        fg: Colors.blue.shade800,
        icon: Icons.info_outline,
      ),
  };
}

/// Localized label for a warning severity (drives the merged announcement).
String _severityLabel(AppLocalizations l, WarningSeverity s) => switch (s) {
      WarningSeverity.error => l.warningsSeverityError,
      WarningSeverity.warning => l.warningsSeverityWarning,
      WarningSeverity.info => l.warningsSeverityInfo,
    };

class _WarningTile extends StatelessWidget {
  final InvoiceWarning warning;
  const _WarningTile({required this.warning});

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final style = _severityStyle(warning.severity);
    // The finding in the reader's language, or the backend's English sentence
    // when this build cannot state it — resolved ONCE, so the announcement and
    // the visible text can never end up in two different languages.
    final text = invoiceWarningText(l, warning);
    // One merged announcement per warning ("Error: Missing vendor name")
    // instead of an icon glyph + two disjoint text spans (WCAG 1.3.1 / 4.1.2).
    return Semantics(
      label: '${_severityLabel(l, warning.severity)}: $text',
      excludeSemantics: true,
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: style.tint.withValues(alpha: 0.12),
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: style.tint.withValues(alpha: 0.4)),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(style.icon, size: 18, color: style.fg),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                text,
                style: TextStyle(
                  color: style.fg,
                  fontSize: 13,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _PoMatchTile extends StatelessWidget {
  final PoMatch match;
  const _PoMatchTile({required this.match});

  ({Color tint, Color fg}) get _style {
    // matched within tolerance → green; mismatch → red; partial → amber.
    if (match.status == 'matched' && (match.withinTolerance ?? true)) {
      return (tint: Colors.green, fg: Colors.green.shade900);
    }
    if (match.status == 'partial') {
      return (tint: Colors.orange, fg: Colors.brown.shade800);
    }
    if (match.status == 'mismatch' || match.withinTolerance == false) {
      return (tint: Colors.red, fg: Colors.red.shade900);
    }
    return (tint: Colors.blue, fg: Colors.blue.shade800);
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final style = _style;
    final variance = match.variancePct;
    final varianceText = variance != null
        ? l.warningsVarianceLabel(
            '${variance >= 0 ? '+' : ''}${variance.toStringAsFixed(1)}')
        : null;

    final summary = [
      l.warningsMatchLabel(match.matchType),
      match.statusLabel,
      ?varianceText,
    ].join(', ');

    return Semantics(
      label: 'PO match: $summary'
          '${match.issues.isNotEmpty ? '. ${match.issues.join('. ')}' : ''}',
      excludeSemantics: true,
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: style.tint.withValues(alpha: 0.12),
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: style.tint.withValues(alpha: 0.4)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Text(
                  match.matchType == 'none'
                      ? l.warningsPoLabel
                      : l.warningsMatchLabel(match.matchType),
                  style: TextStyle(
                    color: style.fg,
                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  match.statusLabel,
                  style: TextStyle(color: style.fg, fontSize: 13),
                ),
                const Spacer(),
                if (varianceText != null)
                  Text(
                    varianceText,
                    style: TextStyle(
                      color: style.fg,
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
              ],
            ),
            if (match.issues.isNotEmpty) ...[
              const SizedBox(height: 6),
              for (final issue in match.issues)
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text(
                    '• $issue',
                    style: TextStyle(color: style.fg, fontSize: 12),
                  ),
                ),
            ],
          ],
        ),
      ),
    );
  }
}
