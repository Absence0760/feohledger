import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/erp_refusal_messages.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_de.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_en.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_es.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_fr.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_ja.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_pt.dart';

// A coded ERP refusal (`{code, params}`, `backend/app/services/erp.py::
// erp_refusal_record`) is stated in the reader's language with what to do —
// the mobile mirror of the web `frontend/src/lib/api/erpRefusals.test.ts`.

/// The codes the web registry states: the keys of `KEYS` in
/// `frontend/src/lib/api/erpRefusals.ts`, a `[CONSTANT]` key resolved through
/// its `export const CONSTANT = '...'`. Read from source so the two registries
/// cannot drift silently (the web test pins its own to the backend's).
Set<String> _webCodes() {
  final source =
      File('../frontend/src/lib/api/erpRefusals.ts').readAsStringSync();
  final block = RegExp(
    r'const KEYS: Record<string, MessageKey> = \{([\s\S]*?)\n\};',
  ).firstMatch(source);
  expect(block, isNotNull, reason: 'KEYS not found in erpRefusals.ts');
  final constants = {
    for (final m in RegExp(r"export const (\w+) = '([a-z_]+)';")
        .allMatches(source))
      m.group(1)!: m.group(2)!,
  };
  final codes = <String>{};
  for (final m in RegExp(r'^\s*(?:([a-z_]+)|\[(\w+)\]):', multiLine: true)
      .allMatches(block!.group(1)!)) {
    final code = m.group(1) ?? constants[m.group(2)];
    expect(code, isNotNull, reason: 'unresolved key ${m.group(2)}');
    codes.add(code!);
  }
  return codes;
}

void main() {
  final en = AppLocalizationsEn();
  final de = AppLocalizationsDe();

  Map<String, dynamic> refusal(
    String code, [
    Map<String, dynamic> params = const {},
  ]) => {'code': code, 'params': params};

  const qbo = 'QuickBooks Online';

  group('the registry pins the web one', () {
    test('every code the web states has a sentence here, and no other', () {
      final web = _webCodes();
      expect(web, isNotEmpty);
      expect(
        web.difference(erpRefusalCodes),
        isEmpty,
        reason: 'add a sentence in lib/l10n/erp_refusal_messages.dart',
      );
      expect(erpRefusalCodes.difference(web), isEmpty);
    });

    test('every code renders a full sentence in every locale', () {
      final locales = <String, AppLocalizations>{
        'en': en,
        'de': de,
        'es': AppLocalizationsEs(),
        'fr': AppLocalizationsFr(),
        'ja': AppLocalizationsJa(),
        'pt': AppLocalizationsPt(),
        'pt_BR': AppLocalizationsPtBr(),
      };
      const params = {
        'provider': qbo,
        'accounts': ['6100'],
        'intuit_code': '6000',
        'exception_type': 'duplicate',
        'claim': 'payment',
        'hold': 'payments_blocked',
        'reason': 'tenant_budget',
      };
      for (final MapEntry(key: locale, value: l) in locales.entries) {
        for (final code in erpRefusalCodes) {
          final text = erpRefusalText(l, refusal(code, params));
          expect(text, isNotNull, reason: '$locale $code');
          expect(text, isNot(contains(code)), reason: '$locale $code');
          expect(
            RegExp(r'\{\w+\}').hasMatch(text!),
            isFalse,
            reason: '$locale $code left a placeholder',
          );
        }
      }
    });
  });

  group('erpRefusalText', () {
    test('names the provider, in the reader\'s language', () {
      expect(
        erpRefusalText(en, refusal('bill_not_in_erp', {'provider': qbo})),
        en.erpRefusalBillNotInErp(qbo),
      );
      expect(
        erpRefusalText(de, refusal('payment_exceeds_balance', {'provider': qbo})),
        de.erpRefusalPaymentExceedsBalance(qbo),
      );
    });

    test('the providerless refusals need no provider', () {
      expect(
        erpRefusalText(en, refusal('not_connected')),
        en.erpRefusalNotConnected,
      );
      expect(
        erpRefusalText(en, refusal('invoice_due_before_date')),
        en.erpRefusalInvoiceDueBeforeDate,
      );
    });

    test('a provider-naming code without a provider is not stated', () {
      expect(erpRefusalText(en, refusal('bill_not_in_erp')), isNull);
      expect(
        erpRefusalText(en, refusal('bill_not_in_erp', {'provider': '  '})),
        isNull,
      );
    });

    test('accounts are listed; none on account_not_linked is the uncoded line',
        () {
      expect(
        erpRefusalText(
          en,
          refusal('account_not_linked', {
            'provider': qbo,
            'accounts': ['6100', '6200'],
          }),
        ),
        en.erpRefusalAccountNotLinked(qbo, '6100, 6200'),
      );
      expect(
        erpRefusalText(
          en,
          refusal('account_not_linked', {'provider': qbo, 'accounts': []}),
        ),
        en.erpRefusalAccountNotLinkedUncoded(qbo),
      );
      expect(
        erpRefusalText(
          en,
          refusal('account_inactive', {'provider': qbo, 'accounts': []}),
        ),
        isNull,
      );
      expect(
        erpRefusalText(
          en,
          refusal('account_not_linked', {
            'provider': qbo,
            'accounts': [6100],
          }),
        ),
        isNull,
      );
    });

    test('an Intuit fault names its numeric code, and only a numeric one', () {
      expect(
        erpRefusalText(
          en,
          refusal('intuit_fault', {'provider': qbo, 'intuit_code': '6000'}),
        ),
        en.erpRefusalIntuitFault(qbo, '6000'),
      );
      expect(
        erpRefusalText(
          en,
          refusal('reference_invalid', {'provider': qbo, 'intuit_code': 'x1'}),
        ),
        isNull,
      );
    });

    test('the export gates state their claim, hold and possibly-posted', () {
      expect(
        erpRefusalText(
          en,
          refusal('payment_blocks_erp_export', {
            'claim': 'virtual_card',
            'possibly_posted': true,
          }),
        ),
        en.erpRefusalCardBlocksExportPossiblyPosted,
      );
      // Only a real `true` means possibly posted.
      expect(
        erpRefusalText(
          en,
          refusal('payment_blocks_erp_export', {
            'claim': 'payment',
            'possibly_posted': 'true',
          }),
        ),
        en.erpRefusalPaymentBlocksExport,
      );
      expect(
        erpRefusalText(
          en,
          refusal('vendor_blocks_erp_export', {
            'hold': 'sanctions_review',
            'possibly_posted': true,
          }),
        ),
        '${en.erpRefusalVendorSanctionsReview} '
        '${en.erpRefusalVendorHoldPossiblyPosted}',
      );
      expect(
        erpRefusalText(
          de,
          refusal('exception_blocks_erp_export', {'exception_type': 'fraud_flag'}),
        ),
        de.erpRefusalExceptionBlocksExport(de.erpRefusalTypeFraudFlag),
      );
      expect(
        erpRefusalText(
          en,
          refusal('exception_blocks_erp_export', {'exception_type': 'other'}),
        ),
        isNull,
      );
      expect(
        erpRefusalText(en, refusal('vendor_blocks_erp_export', {'hold': 'x'})),
        isNull,
      );
    });

    test('the call budget states whose allowance ran out', () {
      expect(
        erpRefusalText(
          en,
          refusal('erp_call_budget_exhausted', {
            'provider': qbo,
            'reason': 'rate_limited',
          }),
        ),
        en.erpRefusalCallBudgetRateLimited(qbo),
      );
      expect(
        erpRefusalText(
          en,
          refusal('erp_call_budget_exhausted', {'provider': qbo}),
        ),
        isNull,
      );
    });

    test('an unknown code or a malformed refusal is not stated', () {
      expect(erpRefusalText(en, refusal('newer_code', {'provider': qbo})), isNull);
      expect(erpRefusalText(en, null), isNull);
      expect(erpRefusalText(en, 'bill_not_in_erp'), isNull);
      expect(erpRefusalText(en, {'code': ''}), isNull);
    });
  });

  group('erpRefusalNote', () {
    test('states a write-back refusal', () {
      for (final kind in erpWritebackRefusalKinds) {
        expect(
          erpRefusalNote(
            en,
            kind,
            refusal('bill_payment_voided', {'provider': 'Xero'}),
            'Xero',
          ),
          en.erpRefusalBillPaymentVoided('Xero'),
        );
      }
    });

    test('a code this build cannot state reads as the generic sentence', () {
      final note = erpRefusalNote(
        en,
        'payment_writeback_failed',
        refusal('newer_code', {'provider': 'Xero'}),
        'Xero',
      );
      expect(note, en.erpRefusalUnknown('Xero'));
      expect(note, isNot(contains('newer_code')));
    });

    test('nothing for another row, or a failure the ERP gave no code for', () {
      final r = refusal('bill_not_in_erp', {'provider': 'Xero'});
      expect(erpRefusalNote(en, 'paid_not_recorded', r, 'Xero'), isNull);
      expect(erpRefusalNote(en, null, r, 'Xero'), isNull);
      expect(
        erpRefusalNote(en, 'payment_writeback_failed', null, 'Xero'),
        isNull,
      );
      expect(
        erpRefusalNote(en, 'payment_writeback_failed', {'code': null}, 'Xero'),
        isNull,
      );
    });
  });
}
