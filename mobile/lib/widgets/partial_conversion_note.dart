import 'package:flutter/material.dart';

/// The line that says a rendered rollup is only PARTLY converted: some of the
/// rows behind it had no exchange rate into the reporting currency and were
/// added at face value, so the figure it sits beside mixes currencies by that
/// many rows.
///
/// The mobile counterpart of the web dashboard's `.dashboard-skipped` and the
/// cash-position card's `.cf-skipped` notices. A fallback nobody reports is
/// just a wrong number (`docs/decisions.md` §35), and the figure is correctly
/// NAMED either way — it is the part-conversion the reader cannot see.
///
/// Call sites render it only for a non-zero count and pass the finished,
/// localized sentence: each surface says what the unconverted rows do to ITS
/// figure (a total, a band set, a ranking, a running balance), which is not
/// one sentence.
///
/// Brown.shade800 is the amber-reading ink the adaptive tab's disclosure
/// already uses; a true orange fails `textContrastGuideline` on white.
class PartialConversionNote extends StatelessWidget {
  final String message;

  const PartialConversionNote(this.message, {super.key});

  @override
  Widget build(BuildContext context) {
    final ink = Colors.brown.shade800;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Decorative: the sentence carries the whole meaning, so the icon
          // is not a second, wordless announcement.
          ExcludeSemantics(
            child: Padding(
              padding: const EdgeInsets.only(top: 1),
              child: Icon(Icons.info_outline, size: 16, color: ink),
            ),
          ),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              message,
              style: TextStyle(
                color: ink,
                fontSize: 13,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
