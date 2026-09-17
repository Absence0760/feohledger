import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/utils/format_locale.dart';
import 'package:feohledger_mobile/utils/numbers.dart';

/// German writes `20,0 %` with a no-break space before the sign.
String _plainSpaces(String s) =>
    s.replaceAll(' ', ' ').replaceAll(' ', ' ');

void main() {
  tearDown(() => setActiveFormatLocale(null));

  group('the precision is the backend\'s, read off its own digits', () {
    test('a measured decimal place is kept; an absent one is not invented', () {
      setActiveFormatLocale('en');
      // A duplicate similarity is whole; a PO variance carries one decimal.
      expect(formatPercentString('98'), '98%');
      expect(formatPercentString('20.0'), '20.0%');
      expect(formatDecimalString('3'), '3');
      expect(formatDecimalString('1234.500'), '1,234.500');
    });

    test('a signed figure keeps its sign — the direction is the point', () {
      setActiveFormatLocale('en');
      expect(formatPercentString('+20.0'), '+20.0%');
      expect(formatPercentString('-20.0'), '-20.0%');
      // Unsigned stays unsigned rather than gaining a `+`.
      expect(formatDecimalString('20.0'), '20.0');
    });
  });

  test('the reader\'s locale drives separators and percent notation', () {
    setActiveFormatLocale('de');
    expect(formatDecimalString('1234.50'), '1.234,50');
    expect(_plainSpaces(formatPercentString('20.0')), '20,0 %');

    setActiveFormatLocale('fr');
    expect(_plainSpaces(formatDecimalString('1234.50')), '1 234,50');
  });

  test('something that is not a number is returned verbatim', () {
    // A wire-format change or a pre-migration backend: the server's own
    // characters are more informative than a blank or a zero.
    setActiveFormatLocale('de');
    expect(formatDecimalString('n/a'), 'n/a');
    expect(formatPercentString(''), '');
  });
}
