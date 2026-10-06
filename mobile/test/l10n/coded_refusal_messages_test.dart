import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/coded_refusal_messages.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_de.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_en.dart';

// The coded approval / exception-queue refusals
// (`backend/app/api/refusals.py::coded_refusal`) are stated in the reader's
// language from their code and params; anything this build cannot state
// completely is null, so the caller keeps its own generic failure. Mirrors the
// web `frontend/src/lib/api/codedRefusals.test.ts`.
void main() {
  final en = AppLocalizationsEn();
  final de = AppLocalizationsDe();

  Map<String, dynamic> refusal(
    String code, [
    Map<String, dynamic> params = const {},
  ]) => {'code': code, 'message': 'server english', 'params': params};

  Map<String, dynamic> gate([Map<String, dynamic> overrides = const {}]) => {
    'amount': '12000.00',
    'currency': 'USD',
    'limit': '10000.00',
    'limit_currency': 'USD',
    'recent_spend': null,
    'aggregate_amount': null,
    'window_days': null,
    'expressible': true,
    'measured_amount': null,
    ...overrides,
  };

  test('every code it states is a literal the backend emits', () {
    // Read the backend source rather than re-typing the list, so a code
    // renamed on the server fails here naming it.
    final source = Directory('../backend/app')
        .listSync(recursive: true)
        .whereType<File>()
        .where((f) => f.path.endsWith('.py'))
        .map((f) => f.readAsStringSync())
        .join('\n');
    for (final code in codedRefusalCodes) {
      expect(source.contains('"$code"'), isTrue, reason: code);
    }
  });

  test('the fixed sentences are stated in the reader\'s language', () {
    expect(
      localizeCodedRefusal(de, refusal('approval_segregation')),
      de.codedRefusalApprovalSegregation,
    );
    expect(
      localizeCodedRefusal(de, refusal('approval_not_named_approver')),
      de.codedRefusalApprovalNotNamedApprover,
    );
    expect(
      localizeCodedRefusal(de, refusal('approval_level_reuse')),
      de.codedRefusalApprovalLevelReuse,
    );
    expect(
      localizeCodedRefusal(de, refusal('segregation_raiser')),
      de.codedRefusalExceptionSegregationRaiser,
    );
    expect(
      localizeCodedRefusal(de, refusal('segregation_implicated')),
      de.codedRefusalExceptionSegregationImplicated,
    );
  });

  test('the CFO gate names both figures and what was measured', () {
    final out = localizeCodedRefusal(
      en,
      refusal(
        'approval_cfo_required',
        gate({
          'amount': '9000.00',
          'currency': 'GBP',
          'measured_amount': '11403.00',
        }),
      ),
    )!;
    expect(out, contains('£9,000.00'));
    expect(out, contains(r'$10,000.00'));
    expect(out, contains(r'Measured as $11,403.00'));
    expect(out, isNot(contains('server english')));
  });

  test('the structuring note is pluralised and inexpressible says so', () {
    final out = localizeCodedRefusal(
      en,
      refusal(
        'approval_cfo_required',
        gate({
          'amount': '4000.00',
          'recent_spend': '7000.00',
          'aggregate_amount': '11000.00',
          'window_days': 30,
          'expressible': false,
        }),
      ),
    )!;
    expect(out, contains('(last 30 days)'));
    expect(out, contains('could not be expressed in USD'));
  });

  test('a code-less invoice is stated bare, never in a borrowed currency', () {
    final out = localizeCodedRefusal(
      en,
      refusal(
        'approval_cfo_required',
        gate({
          'amount': '4000.00',
          'currency': null,
          'recent_spend': '7000.00',
          'aggregate_amount': '11000.00',
          'window_days': 30,
        }),
      ),
    )!;
    expect(out, contains('4000.00'));
    expect(out, contains('7000.00'));
    expect(out, contains('11000.00'));
    expect(out, contains(r'$10,000.00'));
    expect(out, isNot(contains(r'$4')));
    expect(out, isNot(contains('server english')));
  });

  test('a malformed CFO threshold names the configured limit', () {
    expect(
      localizeCodedRefusal(
        en,
        refusal('approval_cfo_required', gate({'limit': null})),
      ),
      contains('the configured limit'),
    );
  });

  test('anything it cannot state completely is null', () {
    expect(localizeCodedRefusal(en, 'a string detail'), isNull);
    expect(localizeCodedRefusal(en, refusal('from_a_newer_backend')), isNull);
    expect(
      localizeCodedRefusal(
        en,
        refusal('approval_max_amount_exceeded', gate({'limit': null})),
      ),
      isNull,
    );
    expect(
      localizeCodedRefusal(
        en,
        refusal('approval_cfo_required', gate({'amount': 9000})),
      ),
      isNull,
    );
    expect(
      localizeCodedRefusal(
        en,
        refusal(
          'approval_cfo_required',
          gate({'recent_spend': '1.00', 'aggregate_amount': '2.00'}),
        ),
      ),
      isNull,
    );
  });
}
