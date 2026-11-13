import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/models/exception.dart';

/// Status chip for an exception. Mirrors [StatusBadge] (the invoice one) but is
/// typed to [ApExceptionStatus]. Text uses a darkened variant of the accent so
/// the foreground clears WCAG 1.4.3 (≥4.5:1) over the 0.15-alpha tint.
class ExceptionStatusBadge extends StatelessWidget {
  final ApExceptionStatus status;

  const ExceptionStatusBadge({super.key, required this.status});

  Color get _color => switch (status) {
    ApExceptionStatus.open => Colors.orange,
    ApExceptionStatus.escalated => Colors.red,
    ApExceptionStatus.resolved => Colors.green,
    ApExceptionStatus.dismissed => Colors.blueGrey,
  };

  // Darkened foreground for AA contrast against the pale tint. Orange can't
  // reach 4.5:1 at 12px bold as a true orange, so `open` uses a dark brown that
  // reads as deep amber (same trick the invoice badge uses for pending).
  Color get _textColor => switch (status) {
    ApExceptionStatus.open => Colors.brown.shade800,
    ApExceptionStatus.escalated => Colors.red.shade900,
    ApExceptionStatus.resolved => Colors.green.shade900,
    ApExceptionStatus.dismissed => Colors.blueGrey.shade900,
  };

  @override
  Widget build(BuildContext context) {
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
