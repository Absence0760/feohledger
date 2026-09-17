import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/utils/format_locale.dart';
import 'package:feohledger_mobile/utils/money.dart';

/// German writes `1.234,50 €` with a no-break space before the symbol — real,
/// invisible in a diff, and not what a test author types.
String _plainSpaces(String s) =>
    s.replaceAll(' ', ' ').replaceAll(' ', ' ');

// The seam between the locale picker and the `intl` formatters. Before it, the
// picker changed every word on screen and not one digit: `money.dart` took a
// `locale` nothing passed, and every `DateFormat` was built without one.
void main() {
  tearDown(() => setActiveFormatLocale(null));

  group('setActiveFormatLocale', () {
    test('canonicalizes the tag and reports whether it changed', () {
      expect(setActiveFormatLocale('pt-BR'), isTrue);
      expect(activeFormatLocale, 'pt_BR');
      // A no-op write must not claim a change — the scope widget rebuilds on
      // every frame of its subtree.
      expect(setActiveFormatLocale('pt_BR'), isFalse);
    });

    test('empty and null both mean "intl\'s own default"', () {
      setActiveFormatLocale('de');
      expect(setActiveFormatLocale(null), isTrue);
      expect(activeFormatLocale, isNull);
      setActiveFormatLocale('de');
      expect(setActiveFormatLocale('   '), isTrue);
      expect(activeFormatLocale, isNull);
    });
  });

  group('money reads it by default', () {
    // The ISO code answers WHAT the figure is in; the locale answers how a
    // reader writes a number. The same EUR amount is two strings.
    test('grouping, separators and symbol placement follow the locale', () {
      setActiveFormatLocale('en');
      expect(formatMoney(1234.5, currency: 'EUR'), '€1,234.50');

      setActiveFormatLocale('de');
      expect(_plainSpaces(formatMoney(1234.5, currency: 'EUR')), '1.234,50 €');
    });

    test('a bare figure (no provable currency) is grouped for the locale too',
        () {
      setActiveFormatLocale('de');
      expect(formatMoney(1234.5), '1.234,50');
      expect(formatMoneyString('1234.50'), '1234.50',
          reason: 'an exact string with no code is still passed through');
    });

    test('the currency code still drives the symbol and the minor units', () {
      setActiveFormatLocale('de');
      // JPY has no minor unit anywhere — that is a property of the currency,
      // not of the reader.
      expect(_plainSpaces(formatMoney(1234.5, currency: 'JPY')), '1.235 ¥');
    });

    test('an explicit locale argument still wins', () {
      setActiveFormatLocale('de');
      expect(formatMoney(1234.5, currency: 'EUR', locale: 'en'), '€1,234.50');
    });
  });

  testWidgets('FormatLocaleScope tracks the locale MaterialApp resolved',
      (tester) async {
    // The scope reads the NEGOTIATED locale rather than `LocaleStore` itself,
    // so "follow the system locale" and a fallback from an unsupported device
    // locale both come out right — MaterialApp already decides them.
    Widget app(Locale? locale) => MaterialApp(
          locale: locale,
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          builder: (context, child) =>
              FormatLocaleScope(child: child ?? const SizedBox.shrink()),
          home: const SizedBox.shrink(),
        );

    await tester.pumpWidget(app(const Locale('de')));
    expect(activeFormatLocale, 'de');

    await tester.pumpWidget(app(const Locale('pt', 'BR')));
    expect(activeFormatLocale, 'pt_BR');

    // An unsupported device locale is resolved to a supported one by
    // MaterialApp; the formatters follow that decision rather than making
    // their own.
    await tester.pumpWidget(app(const Locale('is')));
    expect(AppLocalizations.supportedLocales.map((l) => l.toString()),
        contains(activeFormatLocale));
  });
}
