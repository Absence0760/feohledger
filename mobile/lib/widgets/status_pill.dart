import 'package:material_ui/material_ui.dart';

/// The fixed set of colours a status pill may take. Each tone pairs a base
/// accent (tint + border) with a darkened text colour that clears WCAG 1.4.3
/// (≥4.5:1 — the pill's text is 12px, so the large-text 3:1 bar does not
/// apply) against the pill's own background. Light hues (orange, amber) can't
/// reach that as a true orange, so their text is a dark brown that reads as
/// deep amber. `test/widgets/status_pill_test.dart` computes every pair.
enum StatusTone {
  /// Neutral / not yet started (a draft, an unknown outcome, dismissed).
  neutral,

  /// New, informational.
  info,

  /// In flight to or in a downstream system (sending to / posted in ERP).
  progress,

  /// Scheduled for a future date.
  scheduled,

  /// Needs someone's attention soon (ready for review).
  attention,

  /// Waiting or at risk (pending, unverified, expired, open, partial).
  warning,

  /// Positive outcome (approved, active, resolved, pass).
  success,

  /// Closed out positively (paid, done).
  settled,

  /// Negative outcome (rejected, failed, terminated, escalated, fail).
  danger,

  /// Switched off (inactive, cancelled).
  muted;

  Color get accent => switch (this) {
    StatusTone.neutral => Colors.blueGrey,
    StatusTone.info => Colors.blue,
    StatusTone.progress => Colors.indigo,
    StatusTone.scheduled => Colors.teal,
    StatusTone.attention => Colors.amber,
    StatusTone.warning => Colors.orange,
    StatusTone.success => Colors.green,
    StatusTone.settled => Colors.green.shade800,
    StatusTone.danger => Colors.red,
    StatusTone.muted => Colors.grey,
  };

  Color get textColor => switch (this) {
    StatusTone.neutral => Colors.blueGrey.shade900,
    StatusTone.info => Colors.blue.shade800,
    StatusTone.progress => Colors.indigo.shade700,
    StatusTone.scheduled => Colors.teal.shade800,
    StatusTone.attention => Colors.brown.shade800,
    StatusTone.warning => Colors.brown.shade800,
    StatusTone.success => Colors.green.shade900,
    StatusTone.settled => Colors.green.shade900,
    StatusTone.danger => Colors.red.shade900,
    StatusTone.muted => Colors.grey.shade800,
  };

  /// The pill's fill: a 0.15-alpha tint of [accent] composited over white
  /// into an OPAQUE colour, so the contrast [textColor] was checked against is
  /// the contrast on screen whatever surface (light or dark) the pill sits on.
  Color get background =>
      Color.alphaBlend(accent.withValues(alpha: 0.15), Colors.white);
}

/// The one status chip every shared badge renders: [label] in a rounded pill
/// coloured by [tone], announced to assistive tech as [semanticsLabel] (for
/// example "Status: Approved") instead of the bare word.
class StatusPill extends StatelessWidget {
  final String label;
  final StatusTone tone;
  final String semanticsLabel;

  const StatusPill({
    super.key,
    required this.label,
    required this.tone,
    required this.semanticsLabel,
  });

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: semanticsLabel,
      excludeSemantics: true,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: tone.background,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: tone.accent.withValues(alpha: 0.4)),
        ),
        child: Text(
          label,
          style: TextStyle(
            color: tone.textColor,
            fontSize: 12,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
    );
  }
}
