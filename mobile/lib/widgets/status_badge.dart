import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/models/invoice.dart';

class StatusBadge extends StatelessWidget {
  final InvoiceStatus status;

  const StatusBadge({super.key, required this.status});

  // Base accent — drives the tint + border. The text uses a darkened variant
  // (`_textColor`) so the foreground clears WCAG 1.4.3 (≥4.5:1) against the
  // 0.15-alpha tint over white; the light hues (amber/orange/teal) fail at
  // full saturation.
  Color get _color => switch (status) {
    InvoiceStatus.newStatus => Colors.blue,
    InvoiceStatus.pending => Colors.orange,
    InvoiceStatus.readyForReview => Colors.amber,
    InvoiceStatus.approved => Colors.green,
    InvoiceStatus.rejected => Colors.red,
    InvoiceStatus.sendingToErp ||
    InvoiceStatus.sentToErp ||
    InvoiceStatus.postedInErp => Colors.indigo,
    InvoiceStatus.paymentScheduled => Colors.teal,
    InvoiceStatus.paid || InvoiceStatus.done => Colors.green.shade800,
    InvoiceStatus.failed => Colors.red.shade800,
  };

  // Darkened foreground for AA contrast (≥4.5:1) against the pale tint
  // background. The text is small (12px) bold, so the 4.5:1 (not 3:1) bar
  // applies — amber/orange can't reach it as a true amber, so those use a dark
  // brown that reads as deep amber. Ratios verified against the 0.15-alpha tint
  // over white (see Worker D a11y pass).
  Color get _textColor => switch (status) {
    InvoiceStatus.newStatus => Colors.blue.shade800, // 4.89
    InvoiceStatus.pending => Colors.brown.shade800, // 10.09
    InvoiceStatus.readyForReview => Colors.brown.shade800, // 10.49
    InvoiceStatus.approved => Colors.green.shade900, // 6.84
    InvoiceStatus.rejected => Colors.red.shade900, // 5.40
    InvoiceStatus.sendingToErp ||
    InvoiceStatus.sentToErp ||
    InvoiceStatus.postedInErp => Colors.indigo.shade700, // 7.17
    InvoiceStatus.paymentScheduled => Colors.teal.shade800, // 5.52
    InvoiceStatus.paid || InvoiceStatus.done => Colors.green.shade900,
    InvoiceStatus.failed => Colors.red.shade900,
  };

  @override
  Widget build(BuildContext context) {
    // Announce the status as a labelled value (e.g. "Status: Approved") rather
    // than letting the raw glyph string read as decorative text (WCAG 1.1.1).
    return Semantics(
      label: 'Status: ${status.label}',
      excludeSemantics: true,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: _color.withValues(alpha: 0.15),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: _color.withValues(alpha: 0.4)),
        ),
        child: Text(
          status.label,
          style: TextStyle(
            color: _textColor,
            fontSize: 12,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
    );
  }
}
