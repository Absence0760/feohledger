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
    expect(
      localizeCodedRefusal(de, refusal('mfa_code_invalid')),
      de.codedRefusalMfaCodeInvalid,
    );
    expect(
      localizeCodedRefusal(de, refusal('mfa_email_code_invalid')),
      de.codedRefusalMfaEmailCodeInvalid,
    );
    expect(
      localizeCodedRefusal(de, refusal('payment_run_awaiting_cfo')),
      de.codedRefusalPaymentRunAwaitingCfo,
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
    // A row refused without a code of its own names its target (§NEW-T1).
    expect(
      localizeCodedRefusal(
          en, refusal('invoice_bulk_refused', {'status': 'new'})),
      'This invoice could not be changed to New.',
    );
    expect(localizeCodedRefusal(en, refusal('invoice_bulk_refused')), isNull);
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

  // The no-rail pilot (backend decisions §251): the record doors' refusals
  // and the run verdicts the Pay tab's record dialogs read. Mirrors the web
  // `codedRefusals.ts` entries for the same codes.
  test('the record-outside refusals are stated in the reader\'s language', () {
    expect(localizeCodedRefusal(de, refusal('payments_record_only')),
        de.codedRefusalPaymentsRecordOnly);
    expect(localizeCodedRefusal(de, refusal('external_payment_segregation')),
        de.codedRefusalExternalPaymentSegregation);
    expect(
      localizeCodedRefusal(
          en, refusal('external_payment_not_payable', {'status': 'paid'})),
      en.codedRefusalExternalPaymentNotPayable('Paid'),
    );
    expect(
      localizeCodedRefusal(
          en, refusal('external_payment_payment_live', {'payment_status': 'processing'})),
      en.codedRefusalExternalPaymentPaymentLive(en.paymentStatusProcessing),
    );
    expect(
      localizeCodedRefusal(
          en, refusal('external_payment_amount_mismatch', {'amount': '95.00', 'currency': 'USD'})),
      en.codedRefusalExternalPaymentAmountMismatch('\$95.00'),
    );
    expect(
      localizeCodedRefusal(en, refusal('external_payment_in_run', {
        'payment_run_id': '0123456789abcdef',
      })),
      en.codedRefusalExternalPaymentInRun('01234567'),
    );
    expect(
      localizeCodedRefusal(en, refusal('external_payment_in_run', {
        'payment_run_id': '0123456789abcdef',
        'exception_id': 'e1',
      })),
      en.codedRefusalExternalPaymentErpSplitInRun('01234567'),
    );
    expect(localizeCodedRefusal(en, refusal('external_payment_erp_split')),
        en.codedRefusalExternalPaymentErpSplit);
  });

  test('a run-level record refusal names the invoice that stopped the run', () {
    expect(
      localizeCodedRefusal(en, refusal('external_payment_card_live', {
        'invoice_number': 'INV-7',
      })),
      en.codedRefusalExternalPaymentForInvoice(
          'INV-7', en.codedRefusalExternalPaymentCardLive),
    );
    // A malformed number states nothing rather than an unnamed invoice.
    expect(
      localizeCodedRefusal(
          en, refusal('external_payment_card_live', {'invoice_number': ''})),
      isNull,
    );
  });

  test('a record refusal missing what its sentence needs states nothing', () {
    expect(localizeCodedRefusal(en, refusal('external_payment_not_payable')),
        isNull);
    expect(
      localizeCodedRefusal(en,
          refusal('external_payment_payment_live', {'payment_status': 'mystery'})),
      isNull,
    );
    expect(
      localizeCodedRefusal(
          en, refusal('external_payment_amount_mismatch', {'amount': 'lots'})),
      isNull,
    );
    expect(localizeCodedRefusal(en, refusal('external_payment_in_run')), isNull);
    expect(localizeCodedRefusal(en, refusal('external_payment_unheard_of')),
        isNull);
  });

  test('the run verdicts name the step maker-checker refuses', () {
    expect(
      localizeCodedRefusal(
          de, refusal('payment_run_segregation', {'step': 'record_outside'})),
      de.codedRefusalPaymentRunSegregationRecord,
    );
    expect(
      localizeCodedRefusal(
          de, refusal('payment_run_segregation', {'step': 'nacha_export'})),
      de.codedRefusalPaymentRunSegregationExport,
    );
    expect(localizeCodedRefusal(de, refusal('payment_run_segregation')),
        de.codedRefusalPaymentRunSegregation);
    expect(localizeCodedRefusal(de, refusal('payment_run_awaiting_cfo')),
        de.codedRefusalPaymentRunAwaitingCfo);
    expect(localizeCodedRefusal(de, refusal('payment_run_not_recordable')),
        de.codedRefusalPaymentRunNotRecordable);
    expect(localizeCodedRefusal(de, refusal('payment_run_nothing_to_record')),
        de.codedRefusalPaymentRunNothingToRecord);
  });

  // `services/goods_receipts.py` — the mobile receipt form.
  test('the goods-receipt refusals are stated, and a missing param is null',
      () {
    expect(
      localizeCodedRefusal(
        de,
        refusal('goods_receipt_po_cancelled', {'poNumber': 'PO-9'}),
      ),
      de.codedRefusalGoodsReceiptPoCancelled('PO-9'),
    );
    expect(
      localizeCodedRefusal(
        en,
        refusal('goods_receipt_number_taken', {'grNumber': 'DN-7'}),
      ),
      en.codedRefusalGoodsReceiptNumberTaken('DN-7'),
    );
    expect(
      localizeCodedRefusal(en, refusal('goods_receipt_nothing_received')),
      en.codedRefusalGoodsReceiptNothingReceived,
    );
    expect(
      localizeCodedRefusal(en, refusal('goods_receipt_po_cancelled')),
      isNull,
    );
  });
}
