/// Date formatting for the whole app — the counterpart of `utils/money.dart`,
/// and the reason no screen constructs a `DateFormat` of its own any more.
///
/// **A date is formatted for the reader's locale, and the pattern is a
/// SKELETON, not a literal.** Ten files each held a module-level
/// `DateFormat('MMM d, yyyy')`, which is an *en* pattern: handing it a German
/// locale produces "März 4, 2026", not the "4. März 2026" a German reader
/// expects, because the pattern pins the order. `DateFormat.yMMMd(locale)` asks
/// for "year, abbreviated month, day" and lets the locale decide how to say it.
///
/// A module-level `final` was the second half of the bug: it captures the
/// locale at first use, so a picker change would have re-localized the words
/// around a date that never moved. Every helper here builds its formatter per
/// call, against [activeFormatLocale] at that moment.
///
/// [formatIsoDate] is the deliberate exception — a wire format, never shown.
library;

import 'package:intl/intl.dart';

import 'package:feohledger_mobile/utils/format_locale.dart';

/// `Mar 4, 2026` / `4. März 2026` — the app's default date.
String formatDate(DateTime date, {String? locale}) =>
    DateFormat.yMMMd(dateFormatLocale(locale)).format(date);

/// `March 4, 2026` / `4. März 2026` — the long form a contract end date uses.
String formatLongDate(DateTime date, {String? locale}) =>
    DateFormat.yMMMMd(dateFormatLocale(locale)).format(date);

/// `Mar 4` — a due date on a list tile, where the year is noise.
String formatDayMonth(DateTime date, {String? locale}) =>
    DateFormat.MMMd(dateFormatLocale(locale)).format(date);

/// `March 4` — [formatDayMonth] spelled out, for a screen-reader label.
String formatLongDayMonth(DateTime date, {String? locale}) =>
    DateFormat.MMMMd(dateFormatLocale(locale)).format(date);

/// `Mar 4, 2026 <separator> 2:30 PM`, with the time in the locale's own clock
/// (`14:30` where that is the convention).
///
/// [separator] is the visual join the caller already uses — a space, a `·`, a
/// `•`. The two halves are formatted apart rather than through `add_jm()`
/// precisely so those stay the caller's choice.
String formatDateTime(
  DateTime date, {
  String separator = ' ',
  String? locale,
}) {
  final tag = dateFormatLocale(locale);
  return '${DateFormat.yMMMd(tag).format(date)}$separator'
      '${DateFormat.jm(tag).format(date)}';
}

/// `2026-03-04` — the **wire** format, for a request body or a query
/// parameter.
///
/// Deliberately locale-independent: the backend parses ISO 8601 and a German
/// `04.03.2026` would be a bug, not a translation. Nothing renders this to a
/// user.
String formatIsoDate(DateTime date) => DateFormat('yyyy-MM-dd').format(date);
