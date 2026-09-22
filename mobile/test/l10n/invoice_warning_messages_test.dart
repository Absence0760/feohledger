import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_de.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_en.dart';
import 'package:feohledger_mobile/l10n/invoice_warning_messages.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/utils/format_locale.dart';

// Invoice warnings are composed per row from that row's own data, so a label
// per `type` could never state one — the backend emits a stable `code` plus
// typed `params` beside the English `message`, and this is the mobile half
// that turns the pair into a sentence in the reader's language.
//
// Parity with the web is NOT asserted here any more, deliberately. The code
// map and the parameter kinds are generated — `pnpm gen:warning-messages`
// writes `lib/l10n/invoice_warning_messages.generated.dart` in the same run,
// from the same `backend/app/services/invoice_warning_catalog.py`, as the
// web's `invoiceWarningMessages.generated.ts` — and
// `pnpm check:warning-messages` fails CI's Backend lint job when either file
// is stale. This test used to parse that TypeScript to catch a hand
// transcription drifting from it; with no transcription left, the only thing
// it could catch is a hand edit to a generated file, which the drift check
// already refuses. What a generated file cannot prove about itself is that
// every arm it emits reaches a real sentence, and that is the first group.
void main() {
  final en = AppLocalizationsEn();
  final de = AppLocalizationsDe();

  InvoiceWarning warning(
    String code, {
    Map<String, String> params = const {},
    String message = 'server english',
  }) =>
      InvoiceWarning(
        type: 'fraud_flag',
        severity: WarningSeverity.warning,
        message: message,
        code: code,
        params: params,
      );

  /// A well-formed sample value for every parameter [code] declares.
  Map<String, String> sampleParams(String code) => {
        for (final e in invoiceWarningParamKinds[code]!.entries)
          e.key: switch (e.value) {
            'money' => '1234.50',
            'currency' => 'EUR',
            'percent' => '20.0',
            'number' => '3',
            'count' => '2',
            'date' => '2026-03-04',
            _ => 'x',
          },
      };

  group('the generated catalogue', () {
    test('declares only the parameter kinds this library can format', () {
      // A kind the formatters here don't know would render verbatim at best;
      // the generator reads kinds straight off the backend catalogue, so this
      // is where a new backend kind first has to be taught to mobile.
      const known = {
        'text',
        'currency',
        'money',
        'number',
        'percent',
        'count',
        'date',
      };
      expect(invoiceWarningParamKinds, isNotEmpty);
      for (final entry in invoiceWarningParamKinds.entries) {
        expect(known, containsAll(entry.value.values.toSet()), reason: entry.key);
      }
    });

    test('every code resolves to a real sentence in every locale', () {
      // Proves each generated arm reaches its `AppLocalizations` method with
      // every argument its kind map promises — in all six catalogues, since a
      // placeholder the translator dropped is where a locale would fall back.
      final locales = [
        for (final locale in AppLocalizations.supportedLocales)
          lookupAppLocalizations(locale),
      ];
      expect(locales, isNotEmpty);
      for (final l in locales) {
        for (final code in invoiceWarningParamKinds.keys) {
          final text = localizeInvoiceWarning(
            l,
            warning(code, params: sampleParams(code)),
          );
          expect(text, isNotNull, reason: '${l.localeName}: $code');
          expect(text, isNot(contains('{')), reason: '${l.localeName}: $code');
        }
      }
    });

    test('every declared parameter is required, not decorative', () {
      // Dropping any one non-currency parameter must fall the whole finding
      // back to the server's English — the generated arm reads every
      // parameter the sentence embeds, and none of them is optional.
      for (final entry in invoiceWarningParamKinds.entries) {
        final code = entry.key;
        for (final name in entry.value.keys) {
          if (entry.value[name] == 'currency') continue;
          final params = sampleParams(code)..remove(name);
          expect(
            localizeInvoiceWarning(en, warning(code, params: params)),
            isNull,
            reason: '$code still renders without `$name`',
          );
        }
      }
    });
  });

  group('resolution', () {
    tearDown(() => setActiveFormatLocale(null));

    test('a known code renders in the reader\'s language', () {
      final w = warning('po_not_found', params: {'poNumber': 'PO-4412'});
      expect(invoiceWarningText(en, w), 'PO PO-4412 not found');
      expect(invoiceWarningText(de, w), 'Bestellung PO-4412 nicht gefunden');
    });

    test('a money parameter carries the invoice\'s own currency, formatted '
        'for the reader', () {
      final w = warning('round_amount', params: {
        'amount': '5000.00',
        'currency': 'EUR',
      });
      setActiveFormatLocale('en');
      expect(invoiceWarningText(en, w), 'Round amount: €5,000.00');

      // The German sentence AND the German figure — the pair this closes.
      setActiveFormatLocale('de');
      expect(invoiceWarningText(de, w), startsWith('Runder Betrag: 5.000,00'));
      expect(invoiceWarningText(de, w), contains('€'));
    });

    test('a percentage and a date are formatted, not passed through raw', () {
      setActiveFormatLocale('en');
      expect(
        invoiceWarningText(
            en,
            warning('contract_expired', params: {
              'invoiceDate': '2026-03-04',
              'contractNumber': 'C-1',
              'endDate': '2026-01-31',
            })),
        'Invoice dated Mar 4, 2026 is after contract C-1 expired (Jan 31, 2026)',
      );
      expect(
        invoiceWarningText(
            en,
            warning('po_amount_variance', params: {
              'variancePct': '+20.0',
              'poNumber': 'PO-1',
              'invoiceAmount': '120.00',
              'poTotal': '100.00',
              'currency': 'USD',
            })),
        'Amount variance +20.0% vs PO PO-1 '
        '(invoice \$120.00 vs PO \$100.00)',
      );
    });

    test('a plural parameter selects its arm, including the empty =0 one', () {
      String duplicate(String count) => invoiceWarningText(
            en,
            warning('duplicate_similar', params: {
              'similarity': '98',
              'invoiceNumber': 'INV-1',
              'vendorName': 'Acme',
              'crossEntityCount': count,
            }),
          );

      expect(duplicate('0'), 'Potential duplicate: 98% match to INV-1 from Acme');
      expect(duplicate('1'), endsWith('(plus 1 near-identical invoice under '
          'another entity)'));
      expect(duplicate('3'), endsWith('(plus 3 near-identical invoices under '
          'another entity)'));
    });
  });

  group('the English message is the fallback, and it is a normal path', () {
    test('a warning with no code at all falls back', () {
      // Everything persisted before the catalogue existed: `refresh_warnings`
      // re-derives a code on the invoice's next write and nothing backfills,
      // so an untouched row has none.
      const w = InvoiceWarning(
        type: 'duplicate',
        severity: WarningSeverity.error,
        message: 'Duplicate invoice number for this vendor',
      );
      expect(localizeInvoiceWarning(de, w), isNull);
      expect(invoiceWarningText(de, w), w.message);
    });

    test('a code this build does not know falls back', () {
      final w = warning('a_code_from_a_newer_backend');
      expect(invoiceWarningText(de, w), 'server english');
    });

    test('a known code with a missing parameter falls back whole', () {
      // Half a translated sentence — with a `{poNumber}` showing through — is
      // worse than the server's complete English one.
      final w = warning('po_not_found', message: 'PO 9 not found');
      expect(localizeInvoiceWarning(en, w), isNull);
      expect(invoiceWarningText(en, w), 'PO 9 not found');
    });

    test('a plural parameter that is not an integer falls back', () {
      // A selector chooses an ARM of the message rather than filling a slot,
      // so there is no sentence to render at all without it.
      final w = warning('rush_payment', params: {'days': 'soon'});
      expect(localizeInvoiceWarning(en, w), isNull);
    });

    test('a non-selector parameter of the wrong shape renders verbatim', () {
      // NOT a fallback trigger, deliberately: the value is the server's own
      // characters either way, so the only question is which language
      // surrounds them — and the English fallback would print the identical
      // fragment. Same rule `formatMoneyString` and `utils/numbers.dart`
      // follow for a figure they cannot format. Matches the web module.
      setActiveFormatLocale('en');
      expect(
        invoiceWarningText(
            en,
            warning('duplicate_similar_cross_entity',
                params: {'similarity': 'n/a'})),
        'Potential duplicate: n/a match to a near-identical invoice '
        'under another entity',
      );
      expect(
        invoiceWarningText(
            en,
            warning('round_amount',
                params: {'amount': 'lots', 'currency': 'EUR'})),
        'Round amount: lots',
      );
      expect(
        invoiceWarningText(
            en,
            warning('self_correction_date_ordering', params: {
              'dueDate': 'yesterday',
              'invoiceDate': '2026-03-04',
            })),
        'Due date (yesterday) is before invoice date (Mar 4, 2026).',
      );
    });
  });

  test('every locale can state every code', () {
    // The ARB parity test proves the keys exist in all six; this proves they
    // are reachable through the resolver, which is where a placeholder-order
    // mistake would surface.
    for (final l in [
      for (final locale in AppLocalizations.supportedLocales)
        AppLocalizations.delegate.isSupported(locale)
            ? lookupAppLocalizations(locale)
            : null,
    ]) {
      expect(l, isNotNull);
      final text = invoiceWarningText(
        l!,
        warning('po_not_found', params: {'poNumber': 'PO-1'}),
      );
      expect(text, isNot('server english'), reason: l.localeName);
    }
  });
}
