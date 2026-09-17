import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:intl/date_symbol_data_local.dart';
import 'package:intl/intl.dart';

import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/utils/format_locale.dart';

/// CLDR separates a time from its AM/PM marker with a narrow no-break space
/// (U+202F), which is invisible in a diff and not what a test author types.
String _plainSpaces(String s) => s.replaceAll(' ', ' ').replaceAll(' ', ' ');

// `utils/dates.dart` is the date half of what `utils/money.dart` is for money:
// one place that formats, reading the active locale rather than a pattern
// frozen at import time. These tests pin the two properties that were broken
// before it existed — the locale reaches the formatter, and the ORDER of the
// parts is the locale's to choose, not an `en` pattern's.
void main() {
  // A plain Dart test has no `flutter_localizations` delegate to load date
  // symbols for it, so load the full set here. The app gets them from the
  // delegate for whichever locale it resolves.
  setUpAll(() async {
    await initializeDateFormatting();
  });

  tearDown(() => setActiveFormatLocale(null));

  final march4 = DateTime(2026, 3, 4, 14, 30);

  group('the reader locale drives the format', () {
    test('formatDate renders the locale order, not an en pattern', () {
      setActiveFormatLocale('en');
      expect(formatDate(march4), 'Mar 4, 2026');

      // The bug this file exists for: `DateFormat('MMM d, yyyy', 'de')` would
      // render "März 4, 2026" — a German month inside English word order,
      // because a literal pattern pins the order. A skeleton lets the locale
      // decide, and German puts the day first with an ordinal dot.
      setActiveFormatLocale('de');
      expect(formatDate(march4), '4. März 2026');

      setActiveFormatLocale('ja');
      expect(formatDate(march4), '2026年3月4日');
    });

    test('formatLongDate / formatDayMonth / formatLongDayMonth follow too', () {
      setActiveFormatLocale('en');
      expect(formatLongDate(march4), 'March 4, 2026');
      expect(formatDayMonth(march4), 'Mar 4');
      expect(formatLongDayMonth(march4), 'March 4');

      setActiveFormatLocale('fr');
      expect(formatLongDate(march4), '4 mars 2026');
      expect(formatDayMonth(march4), '4 mars');
    });

    test('formatDateTime keeps the caller\'s separator and localizes the clock',
        () {
      setActiveFormatLocale('en');
      expect(_plainSpaces(formatDateTime(march4)), 'Mar 4, 2026 2:30 PM');
      expect(_plainSpaces(formatDateTime(march4, separator: ' • ')),
          'Mar 4, 2026 • 2:30 PM');

      // German reads a 24-hour clock; the separator is the caller's choice
      // either way (the activity timeline uses •, the exception detail ·).
      setActiveFormatLocale('de');
      expect(formatDateTime(march4, separator: ' · '), '4. März 2026 · 14:30');
    });

    test('the explicit locale argument overrides the active one', () {
      setActiveFormatLocale('de');
      expect(formatDate(march4, locale: 'en'), 'Mar 4, 2026');
    });
  });

  test('formatIsoDate is a wire format and ignores the locale', () {
    // A request body, never a rendered string: the backend parses ISO 8601 and
    // a German `04.03.2026` would be a bug, not a translation.
    setActiveFormatLocale('de');
    expect(formatIsoDate(march4), '2026-03-04');
    setActiveFormatLocale('ja');
    expect(formatIsoDate(march4), '2026-03-04');
  });

  test('a locale with no loaded date symbols falls back rather than throwing',
      () {
    // `dateFormatLocale` is a capability check, not a fallback for a bad
    // value: date symbols load per locale, and a `DateFormat` built against
    // one that has none throws. The alternative to formatting in intl's own
    // default is not formatting at all — which is also why
    // `setActiveFormatLocale` leaves `Intl.defaultLocale` alone: poisoning it
    // would make this fallback throw too.
    expect(DateFormat.localeExists('zz_ZZ'), isFalse);
    expect(dateFormatLocale('zz_ZZ'), isNull);

    final defaultLocale = Intl.defaultLocale;
    setActiveFormatLocale('zz_ZZ');
    expect(Intl.defaultLocale, defaultLocale, reason: 'left alone on purpose');
    expect(formatDate(march4), 'Mar 4, 2026');
  });

  test('no module outside utils/dates.dart constructs a DateFormat', () {
    // The drift guard for the whole arrangement. A `DateFormat` built anywhere
    // else is either frozen at import time (a module-level `final`, which
    // survives a picker change) or locale-less (which formats en_US at a
    // German reader) — the two halves of the bug this file closes. Same shape
    // for `NumberFormat` and `utils/money.dart`.
    final offenders = <String>[];
    for (final entity in Directory('lib').listSync(recursive: true)) {
      if (entity is! File || !entity.path.endsWith('.dart')) continue;
      // The generated AppLocalizations carries its own intl plumbing.
      if (entity.path.contains('/l10n/gen/')) continue;
      final source = entity.readAsStringSync();
      // A CONSTRUCTION — `DateFormat(...)` or a named skeleton. The static
      // `DateFormat.localeExists` is the capability check `format_locale.dart`
      // is built on, not a formatter.
      if (entity.path != 'lib/utils/dates.dart' &&
          RegExp(r'DateFormat(\(|\.(?!localeExists)\w+\()').hasMatch(source)) {
        offenders.add('${entity.path}: DateFormat');
      }
      if (entity.path != 'lib/utils/money.dart' &&
          RegExp(r'NumberFormat(\(|\.\w+\()').hasMatch(source)) {
        offenders.add('${entity.path}: NumberFormat');
      }
    }
    expect(offenders, isEmpty,
        reason: 'format through utils/dates.dart / utils/money.dart instead');
  });
}
