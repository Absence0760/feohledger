import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/invoice.dart';

void main() {
  group('InvoiceStatus', () {
    test('fromString maps every known wire value', () {
      for (final status in InvoiceStatus.values) {
        expect(InvoiceStatus.fromString(status.value), status);
      }
    });

    test('fromString falls back to newStatus for unknown values', () {
      expect(InvoiceStatus.fromString('not_a_status'), InvoiceStatus.newStatus);
      expect(InvoiceStatus.fromString(''), InvoiceStatus.newStatus);
    });

    test('every status has a human label', () {
      for (final status in InvoiceStatus.values) {
        expect(status.label, isNotEmpty);
      }
      expect(InvoiceStatus.readyForReview.label, 'Ready for Review');
      expect(InvoiceStatus.paymentScheduled.label, 'Payment Scheduled');
    });

    test('isActionable is true only for ready_for_review', () {
      expect(InvoiceStatus.readyForReview.isActionable, isTrue);
      for (final status in InvoiceStatus.values) {
        if (status != InvoiceStatus.readyForReview) {
          expect(status.isActionable, isFalse, reason: status.value);
        }
      }
    });
  });

  group('financial freeze', () {
    // The backend's `_FINANCIALLY_LOCKED_STATUSES` is `{approved} |
    // IMMUTABLE_STATUSES` (backend/app/api/invoices.py). Spelled out here so a
    // change to either side of the mirror has to be deliberate.
    const lockedStatuses = {
      InvoiceStatus.approved,
      InvoiceStatus.sendingToErp,
      InvoiceStatus.sentToErp,
      InvoiceStatus.postedInErp,
      InvoiceStatus.paymentScheduled,
      InvoiceStatus.paid,
      InvoiceStatus.done,
    };

    test('isFinanciallyLocked matches {approved} | IMMUTABLE_STATUSES', () {
      for (final status in InvoiceStatus.values) {
        expect(
          status.isFinanciallyLocked,
          lockedStatuses.contains(status),
          reason: status.value,
        );
      }
    });

    test('approved is still editable, but financially locked', () {
      // The narrower gate is the whole point: AP may keep fixing GL coding /
      // notes on an approved invoice; only money + payee are frozen.
      expect(InvoiceStatus.approved.isEditable, isTrue);
      expect(InvoiceStatus.approved.isFinanciallyLocked, isTrue);
    });

    test('a pre-approval status is neither locked nor immutable', () {
      for (final status in [
        InvoiceStatus.newStatus,
        InvoiceStatus.pending,
        InvoiceStatus.readyForReview,
        InvoiceStatus.rejected,
        InvoiceStatus.failed,
      ]) {
        expect(status.isFinanciallyLocked, isFalse, reason: status.value);
        expect(status.isEditable, isTrue, reason: status.value);
      }
    });

    test('kFinancialInvoiceFields mirrors the backend _FINANCIAL_FIELDS', () {
      expect(kFinancialInvoiceFields, {
        'amount',
        'currency',
        'subtotal',
        'tax_amount',
        'discount_amount',
        'shipping_amount',
        'tax_rate',
        'vendor',
        'vendor_name',
        'remit_to_address',
      });
    });

    test('stripFinancialFields drops every frozen field, keeps the rest', () {
      final stripped = stripFinancialFields({
        'amount': '1500.00',
        'currency': 'EUR',
        'subtotal': '1400.00',
        'tax_amount': '100.00',
        'discount_amount': '0',
        'shipping_amount': '0',
        'tax_rate': '7',
        'vendor': 'Other Supplier',
        'vendor_name': 'Other Supplier',
        'remit_to_address': '1 New Street',
        'description': 'Q3 retainer',
        'gl_account': '6000',
        'po_number': 'PO-9',
        'invoice_number': 'INV-001',
        'due_date': '2026-02-01',
      });
      // The non-financial half of a mixed edit survives — that is the data-loss
      // half of the bug: the backend 409s the whole PATCH otherwise.
      expect(stripped, {
        'description': 'Q3 retainer',
        'gl_account': '6000',
        'po_number': 'PO-9',
        'invoice_number': 'INV-001',
        'due_date': '2026-02-01',
      });
      for (final field in kFinancialInvoiceFields) {
        expect(stripped.containsKey(field), isFalse, reason: field);
      }
    });

    test('stripFinancialFields leaves a purely non-financial diff untouched',
        () {
      final diff = {'description': 'x', 'gl_account': '6000'};
      expect(stripFinancialFields(diff), diff);
      expect(stripFinancialFields(const {}), isEmpty);
    });
  });

  group('Invoice.fromJson', () {
    test('parses a fully-populated invoice', () {
      final invoice = Invoice.fromJson({
        'id': 'inv1',
        'invoice_number': 'INV-001',
        'vendor_name': 'Acme Supplies',
        'amount': 1234.56,
        'currency': 'EUR',
        'status': 'approved',
        'invoice_date': '2026-01-01T00:00:00',
        'due_date': '2026-02-01T00:00:00',
        'description': 'Widgets',
        'po_number': 'PO-9',
        'file_url': 'https://x/inv1.pdf',
        'created_at': '2026-01-01T12:00:00',
      });

      expect(invoice.id, 'inv1');
      expect(invoice.invoiceNumber, 'INV-001');
      expect(invoice.vendorName, 'Acme Supplies');
      expect(invoice.amount, 1234.56);
      expect(invoice.currency, 'EUR');
      expect(invoice.status, InvoiceStatus.approved);
      expect(invoice.invoiceDate, DateTime.parse('2026-01-01T00:00:00'));
      expect(invoice.dueDate, DateTime.parse('2026-02-01T00:00:00'));
      expect(invoice.description, 'Widgets');
      expect(invoice.poNumber, 'PO-9');
      expect(invoice.fileUrl, 'https://x/inv1.pdf');
    });

    test('falls back to the "vendor" key when "vendor_name" is absent', () {
      final invoice = Invoice.fromJson({
        'id': 'inv1',
        'vendor': 'Legacy Vendor',
        'status': 'new',
        'created_at': '2026-01-01T12:00:00',
      });
      expect(invoice.vendorName, 'Legacy Vendor');
    });

    test('leaves an absent currency NULL and coerces integer amount to double',
        () {
      final invoice = Invoice.fromJson({
        'id': 'inv1',
        'amount': 100, // integer from JSON
        'status': 'pending',
        'created_at': '2026-01-01T12:00:00',
      });
      // It used to substitute 'USD', which put a `$` on the amount of an
      // invoice whose currency the payload never named — on the same screen
      // that prints the currency code as its own detail row. `null` renders
      // the figure bare; see `docs/decisions.md` §160.
      expect(invoice.currency, isNull);
      expect(invoice.amount, 100.0);
      expect(invoice.amount, isA<double>());
    });

    test('leaves optional fields null when missing', () {
      final invoice = Invoice.fromJson({
        'id': 'inv1',
        'status': 'pending',
        'created_at': '2026-01-01T12:00:00',
      });
      expect(invoice.invoiceNumber, isNull);
      expect(invoice.vendorName, isNull);
      expect(invoice.amount, isNull);
      expect(invoice.invoiceDate, isNull);
      expect(invoice.dueDate, isNull);
      expect(invoice.poNumber, isNull);
      expect(invoice.fileUrl, isNull);
    });

    test('unknown status string degrades to newStatus rather than throwing', () {
      final invoice = Invoice.fromJson({
        'id': 'inv1',
        'status': 'bogus',
        'created_at': '2026-01-01T12:00:00',
      });
      expect(invoice.status, InvoiceStatus.newStatus);
    });

    test('parses warnings into typed InvoiceWarning list', () {
      final invoice = Invoice.fromJson({
        'id': 'inv1',
        'status': 'ready_for_review',
        'created_at': '2026-01-01T12:00:00',
        'warnings': [
          {'type': 'duplicate', 'severity': 'warning', 'message': 'Dup'},
          {'type': 'missing_field', 'severity': 'error', 'message': 'No amount'},
          {'type': 'fraud_round_amount', 'severity': 'info', 'message': 'Round'},
        ],
      });
      expect(invoice.warnings, hasLength(3));
      expect(invoice.warnings[0].type, 'duplicate');
      expect(invoice.warnings[0].severity, WarningSeverity.warning);
      expect(invoice.warnings[1].severity, WarningSeverity.error);
      expect(invoice.warnings[2].severity, WarningSeverity.info);
      expect(invoice.warnings[1].message, 'No amount');
    });

    test('defaults to an empty warnings list when absent or not a list', () {
      expect(
        Invoice.fromJson({
          'id': 'i',
          'status': 'new',
          'created_at': '2026-01-01T12:00:00',
        }).warnings,
        isEmpty,
      );
      expect(
        Invoice.fromJson({
          'id': 'i',
          'status': 'new',
          'created_at': '2026-01-01T12:00:00',
          'warnings': 'not-a-list',
        }).warnings,
        isEmpty,
      );
    });

    test('parses the localization pair — code + params as exact strings', () {
      // `message` is the English fallback; `code` + `params` is what makes the
      // finding translatable. Every value is kept as the string the wire
      // carried: money is `Decimal` server-side, and a JSON number that went
      // through a `double` here would be a rounded cent inside a warning.
      final w = InvoiceWarning.fromJson({
        'type': 'fraud_flag',
        'severity': 'warning',
        'message': 'Round amount: 5000.00 EUR',
        'code': 'round_amount',
        'params': {'amount': '5000.00', 'currency': 'EUR', 'days': 3},
      });
      expect(w.code, 'round_amount');
      expect(w.params,
          {'amount': '5000.00', 'currency': 'EUR', 'days': '3'});
    });

    test('a warning with no code parses to the English-only shape', () {
      // The normal path for every row persisted before the backend catalogue
      // existed — nothing backfills them.
      final w = InvoiceWarning.fromJson(
        {'type': 'duplicate', 'severity': 'error', 'message': 'Dup'},
      );
      expect(w.code, isNull);
      expect(w.params, isEmpty);
      // A null-valued param is dropped rather than rendered as "null".
      expect(
        InvoiceWarning.fromJson({
          'type': 'x',
          'severity': 'info',
          'message': 'm',
          'code': 'po_not_found',
          'params': {'poNumber': null},
        }).params,
        isEmpty,
      );
    });

    test('warning severity falls back to info on an unknown value', () {
      final w = InvoiceWarning.fromJson(
        {'type': 'x', 'severity': 'bogus', 'message': 'm'},
      );
      expect(w.severity, WarningSeverity.info);
    });

    test('parses po_match into a typed PoMatch', () {
      final invoice = Invoice.fromJson({
        'id': 'inv1',
        'status': 'ready_for_review',
        'created_at': '2026-01-01T12:00:00',
        'po_match': {
          'match_type': '3-way',
          'status': 'mismatch',
          'amount_variance_pct': 12.5,
          'within_tolerance': false,
          'issues': ['Amount variance', 'Quantity mismatch'],
        },
      });
      final m = invoice.poMatch!;
      expect(m.matchType, '3-way');
      expect(m.status, 'mismatch');
      expect(m.statusLabel, 'Mismatch');
      expect(m.variancePct, 12.5);
      expect(m.withinTolerance, isFalse);
      expect(m.issues, ['Amount variance', 'Quantity mismatch']);
      expect(m.isNoPo, isFalse);
    });

    test('reads the variance off the key the matcher writes', () {
      // `MatchResult.to_json_dict` writes `amount_variance_pct` at the top
      // level; `variance_pct` exists only inside `details`. Reading the latter
      // left the variance permanently blank.
      final m = Invoice.fromJson({
        'id': 'inv1',
        'status': 'ready_for_review',
        'created_at': '2026-01-01T12:00:00',
        'po_match': {
          'match_type': '2-way',
          'status': 'mismatch',
          'amount_variance_pct': 20.0,
          'details': {'variance_pct': 20.0},
        },
      }).poMatch!;
      expect(m.variancePct, 20.0);
    });

    test('a currency mismatch carries no variance', () {
      // Invoice and PO in different currencies: the matcher compares nothing
      // and sends `amount_variance_pct: null` (decisions §197).
      final m = Invoice.fromJson({
        'id': 'inv1',
        'status': 'ready_for_review',
        'created_at': '2026-01-01T12:00:00',
        'po_match': {
          'match_type': '2-way',
          'status': 'mismatch',
          'amount_variance_pct': null,
          'currency_check': 'different',
          'po_currency': 'USD',
          'issues': ['Currency mismatch: invoice in EUR, PO in USD — amounts not compared'],
        },
      }).poMatch!;
      expect(m.status, 'mismatch');
      expect(m.variancePct, isNull);
    });

    test('po_match is null when absent, isNoPo when status=no_po', () {
      expect(
        Invoice.fromJson({
          'id': 'i',
          'status': 'new',
          'created_at': '2026-01-01T12:00:00',
        }).poMatch,
        isNull,
      );
      final noPo = Invoice.fromJson({
        'id': 'i',
        'status': 'new',
        'created_at': '2026-01-01T12:00:00',
        'po_match': {'match_type': 'none', 'status': 'no_po', 'issues': []},
      }).poMatch!;
      expect(noPo.isNoPo, isTrue);
    });
  });
}
