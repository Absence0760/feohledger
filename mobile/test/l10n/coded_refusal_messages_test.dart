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

  test('the recall refusals are stated in the reader\'s language', () {
    // `services/review.recall_refusal` (backend decisions §374).
    expect(
      localizeCodedRefusal(de, refusal('invoice_not_recallable', {'status': 'paid'})),
      de.codedRefusalInvoiceNotRecallable,
    );
    expect(
      localizeCodedRefusal(de, refusal('invoice_recall_in_erp')),
      de.codedRefusalInvoiceRecallInErp,
    );
    expect(
      localizeCodedRefusal(de, refusal('invoice_recall_claimed', {'claim': 'payment'})),
      de.codedRefusalPaymentBlocksRecall,
    );
    expect(
      localizeCodedRefusal(en, refusal('invoice_recall_claimed', {'claim': 'virtual_card'})),
      en.codedRefusalCardBlocksRecall,
    );
    // A claim this build does not know keeps the caller's generic failure.
    expect(localizeCodedRefusal(en, refusal('invoice_recall_claimed')), isNull);
    expect(
      localizeCodedRefusal(en, refusal('invoice_recall_claimed', {'claim': 'cheque'})),
      isNull,
    );
  });

  test('the fixed sentences are stated in the reader\'s language', () {
    expect(
      localizeCodedRefusal(de, refusal('approval_segregation')),
      de.codedRefusalApprovalSegregation,
    );
    expect(
      localizeCodedRefusal(de, refusal('approval_segregation_receiver')),
      de.codedRefusalApprovalSegregationReceiver,
    );
    expect(
      localizeCodedRefusal(de, refusal('approval_segregation_inspector')),
      de.codedRefusalApprovalSegregationInspector,
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
    expect(
      localizeCodedRefusal(de, refusal('segregation_approver')),
      de.codedRefusalExceptionSegregationApprover,
    );
    expect(
      localizeCodedRefusal(de, refusal('invoice_stale_approval')),
      de.codedRefusalInvoiceStaleApproval,
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

  // The review verdict's not-in-review code and the bulk-status skips
  // (`api/invoices.bulk_status_change`, backend decisions §387, §418).
  test('the review and bulk-status refusals are stated in the reader\'s '
      'language', () {
    expect(
      localizeCodedRefusal(de, refusal('invoice_not_in_review')),
      de.codedRefusalInvoiceNotInReview,
    );
    expect(
      localizeCodedRefusal(de, refusal('invoice_bulk_not_found')),
      de.codedRefusalInvoiceBulkNotFound,
    );
    expect(
      localizeCodedRefusal(de, refusal('invoice_recall_one_at_a_time')),
      de.codedRefusalInvoiceRecallOneAtATime,
    );
    expect(
      localizeCodedRefusal(de, refusal('invoice_once_approved_not_reread')),
      de.codedRefusalInvoiceOnceApprovedNotReread,
    );
  });

  test('a status is named by its label, and an unknown one falls back', () {
    expect(
      localizeCodedRefusal(
          en, refusal('invoice_status_immutable', {'status': 'paid'})),
      'This invoice is Paid, so it can no longer be changed.',
    );
    expect(
      localizeCodedRefusal(de,
          refusal('invoice_status_immutable', {'status': 'paid'})),
      de.codedRefusalInvoiceStatusImmutable(de.invoiceStatusPaid),
    );
    expect(
      localizeCodedRefusal(en, refusal('invoice_transition_invalid', {
        'status': 'ready_for_review',
        'target': 'pending',
      })),
      "This invoice can't move from Ready for Review to Pending.",
    );
    expect(
      localizeCodedRefusal(de, refusal('invoice_transition_recall_only', {
        'status': 'approved',
        'target': 'ready_for_review',
      })),
      de.codedRefusalInvoiceTransitionRecallOnly(
        de.invoiceStatusApproved,
        de.invoiceStatusReadyForReview,
      ),
    );
    // Never `InvoiceStatus.fromString`'s `new` fallback for a value this
    // build does not know: the server's sentence instead.
    expect(
      localizeCodedRefusal(
          en, refusal('invoice_status_immutable', {'status': 'archived'})),
      isNull,
    );
    expect(localizeCodedRefusal(en, refusal('invoice_status_immutable')),
        isNull);
    expect(
      localizeCodedRefusal(
          en, refusal('invoice_transition_invalid', {'status': 'new'})),
      isNull,
    );
  });

  test('the missing required fields are named by their labels', () {
    expect(
      localizeCodedRefusal(en, refusal('invoice_required_fields_missing', {
        'fields': ['vendor', 'amount'],
      })),
      'Required fields missing: Vendor, Amount',
    );
    expect(
      localizeCodedRefusal(de, refusal('invoice_required_fields_missing', {
        'fields': ['invoice_number'],
      })),
      de.codedRefusalInvoiceRequiredFieldsMissing(de.invoiceEditInvoiceNumber),
    );
    expect(
      localizeCodedRefusal(
          en, refusal('invoice_required_fields_missing', {'fields': []})),
      isNull,
    );
    expect(
      localizeCodedRefusal(
          en, refusal('invoice_required_fields_missing', {'fields': [1]})),
      isNull,
    );
  });
}
