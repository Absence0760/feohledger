import 'dart:io';

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
// The parity group is the drift guard. The catalogue's authority is
// `backend/app/services/invoice_warning_catalog.py`, which
// `pnpm gen:warning-messages` renders into the frontend's generated module;
// this file reads THAT and fails when mobile falls behind it, so a new
// `WarningSpec` cannot ship web-only.
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

  group('parity with the generated backend catalogue', () {
    final source =
        File('../frontend/src/lib/api/invoiceWarningMessages.generated.ts')
            .readAsStringSync();

    String section(String name) =>
        source.split('$name = {')[1].split('} as const')[0];

    test('the generated module is where it is expected to be', () {
      // A guard that silently passes when its input moves is not a guard.
      expect(source, contains('INVOICE_WARNING_MESSAGE_KEYS'));
      expect(source, contains('INVOICE_WARNING_PARAM_KINDS'));
    });

    test('mobile states every code the backend can emit — and only those', () {
      final codes = RegExp(r"^\t'([a-z0-9_]+)':", multiLine: true)
          .allMatches(section('INVOICE_WARNING_MESSAGE_KEYS'))
          .map((m) => m.group(1)!)
          .toSet();

      expect(codes, isNotEmpty);
      expect(invoiceWarningParamKinds.keys.toSet(), codes);
      // Every code resolves to a real sentence, not just to a map entry.
      for (final code in codes) {
        final params = {
          for (final e in invoiceWarningParamKinds[code]!.entries)
            e.key: switch (e.value) {
              'money' => '1234.50',
              'percent' => '20.0',
              'number' => '3',
              'count' => '2',
              'date' => '2026-03-04',
              _ => 'x',
            },
        };
        expect(localizeInvoiceWarning(en, warning(code, params: params)),
            isNotNull,
            reason: '$code has no localized sentence');
      }
    });

    test('each parameter means the same thing on both surfaces', () {
      // The kind is what decides whether `1234.50` renders as money in the
      // row's currency or as a bare number — a drift here is a mislabelled
      // figure, not a missing string.
      final web = <String, Map<String, String>>{};
      for (final m in RegExp(r"^\t'([a-z0-9_]+)': \{(.*)\},$", multiLine: true)
          .allMatches(section('INVOICE_WARNING_PARAM_KINDS'))) {
        web[m.group(1)!] = {
          for (final p
              in RegExp(r"(\w+): '(\w+)'").allMatches(m.group(2)!))
            p.group(1)!: p.group(2)!,
        };
      }
      expect(web, isNotEmpty);
      expect(invoiceWarningParamKinds, web);
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
