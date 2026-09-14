import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/utils/money.dart';

/// The formatter every money figure in the app goes through.
///
/// The behaviour under test is as much about what it REFUSES to do — invent a
/// symbol, round a figure it cannot represent — as about what it prints. Nine
/// call sites each used to hold their own `NumberFormat.currency(symbol: '\$')`.
void main() {
  group('normalizeCurrencyCode', () {
    test('accepts and upper-cases a 3-letter code', () {
      expect(normalizeCurrencyCode('usd'), 'USD');
      expect(normalizeCurrencyCode(' eur '), 'EUR');
      expect(normalizeCurrencyCode('ZAR'), 'ZAR');
    });

    test('refuses anything that is not provably a code', () {
      // A 2- or 4-character value in a currency field is a misconfiguration,
      // and formatting against it would put an invented symbol on a real
      // figure. Mirrors `frontend/src/lib/utils/reportingCurrency.ts`.
      expect(normalizeCurrencyCode(null), isNull);
      expect(normalizeCurrencyCode(''), isNull);
      expect(normalizeCurrencyCode('  '), isNull);
      expect(normalizeCurrencyCode('US'), isNull);
      expect(normalizeCurrencyCode('USDD'), isNull);
    });
  });

  group('formatMoney symbol, placement and minor units', () {
    test('USD renders the dollar symbol with two decimals', () {
      expect(formatMoney(1234.5, currency: 'USD', locale: 'en'), r'$1,234.50');
    });

    test('EUR renders the euro symbol, not a dollar', () {
      expect(formatMoney(1234.5, currency: 'EUR', locale: 'en'), '€1,234.50');
    });

    test('JPY renders ZERO decimal places, which is the trap', () {
      // The yen has no minor unit. A formatter that hardcodes two decimals
      // prints "¥1,234.50" — a figure that cannot exist — and one that
      // hardcodes `$` prints a US figure for a Japanese tenant. Both were
      // live before this change.
      expect(formatMoney(1234, currency: 'JPY', locale: 'en'), '¥1,234');
      expect(formatMoney(1234.5, currency: 'JPY', locale: 'en'), '¥1,235');
    });

    test('the locale drives grouping and placement, the code drives the symbol',
        () {
      // A EUR figure is "€1,234.50" to an en reader and "1.234,50 €" to a de
      // one; both are correct, and the distinction is why `locale` is a
      // separate parameter from `currency`.
      // `\u00a0` is the non-breaking space CLDR puts before the trailing
      // symbol in de — asserting a plain space here passes visual inspection
      // and fails the comparison.
      expect(
        formatMoney(1234.5, currency: 'EUR', locale: 'de'),
        '1.234,50\u00a0€',
      );
    });

    test('an unlisted code degrades to naming itself, never to a symbol', () {
      expect(formatMoney(10, currency: 'XTS', locale: 'en'), contains('XTS'));
    });
  });

  group('formatMoney with no provable currency', () {
    test('renders the grouped figure with NO symbol', () {
      // Not a fallback — the answer. `PaymentResponse.currency` states the
      // contract: a null code "is NOT a licence to substitute a default …
      // Render the bare figure rather than a code that cannot be proven".
      expect(formatMoney(1234.5, currency: null, locale: 'en'), '1,234.50');
      expect(formatMoney(1234.5, currency: 'US', locale: 'en'), '1,234.50');
    });

    test('keeps the digits identical to the formatted form', () {
      // The symbol is the only thing withheld: an unresolved currency must not
      // also change the number, or a figure would appear to move while a
      // second request was in flight.
      expect(
        formatMoney(1234.5, currency: 'USD', locale: 'en'),
        r'$' + formatMoney(1234.5, currency: null, locale: 'en'),
      );
    });
  });

  group('formatMoney with no figure', () {
    test('renders the no-figure glyph, not a zero', () {
      // Coercing an absent amount to 0 states a fact the server never sent.
      expect(formatMoney(null), moneyNoFigure);
      expect(formatMoney(null, currency: 'EUR'), moneyNoFigure);
      expect(formatMoney(null, placeholder: 'n/a'), 'n/a');
    });

    test('zero is a figure and renders as one', () {
      expect(formatMoney(0, currency: 'USD', locale: 'en'), r'$0.00');
    });
  });

  group('formatMoneyString keeps the server exact decimal', () {
    test('formats a decimal string against its currency', () {
      expect(
        formatMoneyString('1234.50', currency: 'EUR', locale: 'en'),
        '€1,234.50',
      );
      expect(
        formatMoneyString('1234', currency: 'JPY', locale: 'en'),
        '¥1,234',
      );
    });

    test('returns the string verbatim when there is no provable currency', () {
      // The server's own digit count says more about its precision than a
      // re-grouped guess would.
      expect(formatMoneyString('1234.50', currency: null), '1234.50');
    });

    test('returns a non-numeric value verbatim rather than dropping it', () {
      expect(formatMoneyString('n/a', currency: 'USD'), 'n/a');
    });

    test('passes through a figure too large to survive a double', () {
      // A silently rounded cent is worse than an unformatted figure. 16
      // significant digits cannot round-trip through the `double`
      // `NumberFormat` takes, so the exact string is what renders.
      const huge = '12345678901234567.89';
      expect(formatMoneyString(huge, currency: 'USD', locale: 'en'), huge);
      // Just inside the limit still formats.
      expect(
        formatMoneyString('123456789012.34', currency: 'USD', locale: 'en'),
        r'$123,456,789,012.34',
      );
    });

    test('leading zeros do not count against the precision budget', () {
      expect(
        formatMoneyString('000000000000000000012.34',
            currency: 'USD', locale: 'en'),
        r'$12.34',
      );
    });

    test('an empty or absent string is no figure', () {
      expect(formatMoneyString(null, currency: 'USD'), moneyNoFigure);
      expect(formatMoneyString('', currency: 'USD'), moneyNoFigure);
      expect(formatMoneyString('   ', currency: 'USD'), moneyNoFigure);
    });
  });

  group('formatMoneyCompact', () {
    test('abbreviates with the currency symbol', () {
      expect(
        formatMoneyCompact(1200000, currency: 'EUR', locale: 'en'),
        '€1.2M',
      );
    });

    test('abbreviates with no symbol when the currency is unproven', () {
      expect(formatMoneyCompact(1200000, currency: null, locale: 'en'), '1.2M');
    });

    test('renders the no-figure glyph for an absent amount', () {
      expect(formatMoneyCompact(null, currency: 'EUR'), moneyNoFigure);
    });
  });
}
