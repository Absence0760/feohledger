/// Non-money numbers that arrive as exact decimal STRINGS — a variance, a
/// similarity score, a received quantity — formatted for the reader's locale.
///
/// The third of the formatting trio beside `utils/money.dart` and
/// `utils/dates.dart`, and separate from money on purpose: these carry no
/// currency, so `formatMoneyString`'s whole contract (a symbol, a minor-unit
/// count, a bare fallback when the code cannot be proven) says nothing about
/// them.
///
/// **The precision is data, not a formatting choice.** The backend measured
/// it: a duplicate similarity is `98` and a PO variance is `+20.0`, and
/// re-deciding the digit count here would either drop a digit the server
/// meant or invent one it did not. Same for the sign — `+20.0` is signed
/// because the direction matters, `98` is not. Both are read off the string
/// rather than guessed, exactly as the web `invoiceWarnings.ts::numberFormat`
/// does.
library;

import 'package:intl/intl.dart';

import 'package:feohledger_mobile/utils/format_locale.dart';

/// `1234.50` → `1,234.50` / `1.234,50`. Returns [raw] unchanged when it is not
/// a number (a wire-format change, a pre-migration backend) — the server's own
/// digits beat a blank.
String formatDecimalString(String raw, {String? locale}) =>
    _format(raw, locale: locale, percent: false);

/// `20.0` → `20.0%` / `20,0 %`, in the locale's own percent notation.
///
/// The value arrives as a percentage already (`20.0` means 20%), so it is
/// divided by 100 for `NumberFormat`, which multiplies it back.
String formatPercentString(String raw, {String? locale}) =>
    _format(raw, locale: locale, percent: true);

String _format(String raw, {required bool percent, String? locale}) {
  final trimmed = raw.trim();
  final n = num.tryParse(trimmed);
  if (n == null) return raw;
  final digits = _decimalsIn(trimmed);
  final f = percent
      ? NumberFormat.percentPattern(locale ?? activeFormatLocale)
      : NumberFormat.decimalPattern(locale ?? activeFormatLocale);
  f.minimumFractionDigits = digits;
  f.maximumFractionDigits = digits;
  final formatted = f.format(percent ? n / 100 : n);
  // `NumberFormat` writes a `-` but never a `+`; the backend's own `+` is a
  // statement about direction, so it is preserved rather than dropped.
  return trimmed.startsWith('+') && n > 0 ? '+$formatted' : formatted;
}

int _decimalsIn(String raw) {
  final dot = raw.indexOf('.');
  return dot < 0 ? 0 : raw.length - dot - 1;
}
