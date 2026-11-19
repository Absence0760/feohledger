import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';

/// The full-screen "first load failed, nothing to show" state: an icon, a
/// localized sentence saying what happened and what to do, and a Retry button.
///
/// For the first load only. Once a screen holds data, a failed refresh keeps
/// the rows on screen and says so inline — blanking them reads as a crash.
///
/// [message] is a localized sentence, never an exception's `toString()`: a
/// raw `SocketException: …` names neither what happened nor the next action.
class LoadErrorState extends StatelessWidget {
  final String message;
  final Future<void> Function() onRetry;

  const LoadErrorState({
    super.key,
    required this.message,
    required this.onRetry,
  });

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    // Scrollable so the message and button stay reachable at a 200% text
    // scale on a short screen rather than overflowing the viewport.
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            // Decorative: the sentence below carries the meaning.
            ExcludeSemantics(
              child: Icon(
                Icons.error_outline,
                size: 48,
                color: Colors.red.shade700,
              ),
            ),
            const SizedBox(height: 12),
            // A live region, so a screen-reader user hears why the screen
            // went empty instead of landing on silence (WCAG 4.1.3).
            Semantics(
              container: true,
              liveRegion: true,
              child: Text(message, textAlign: TextAlign.center),
            ),
            const SizedBox(height: 16),
            FilledButton(onPressed: onRetry, child: Text(l.commonRetry)),
          ],
        ),
      ),
    );
  }
}
