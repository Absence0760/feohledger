import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/contract.dart';

void main() {
  group('ContractStatus', () {
    test('fromString maps every known wire value', () {
      for (final status in ContractStatus.values) {
        expect(ContractStatus.fromString(status.value), status);
      }
    });

    test('fromString falls back to draft for unknown values', () {
      expect(ContractStatus.fromString('not_a_status'), ContractStatus.draft);
      expect(ContractStatus.fromString(''), ContractStatus.draft);
    });

    test('every status has a human label', () {
      for (final status in ContractStatus.values) {
        expect(status.label, isNotEmpty);
      }
    });

    test('isActionable is true only for draft and active', () {
      expect(ContractStatus.draft.isActionable, isTrue);
      expect(ContractStatus.active.isActionable, isTrue);
      expect(ContractStatus.expired.isActionable, isFalse);
      expect(ContractStatus.terminated.isActionable, isFalse);
      expect(ContractStatus.cancelled.isActionable, isFalse);
    });
  });

  group('ContractType', () {
    test('fromString maps every known wire value', () {
      for (final type in ContractType.values) {
        expect(ContractType.fromString(type.value), type);
      }
    });

    test('fromString falls back to other for unknown values', () {
      expect(ContractType.fromString('bogus'), ContractType.other);
    });
  });

  group('Contract.fromJson', () {
    test('parses a fully-populated contract with line items and spend', () {
      final contract = Contract.fromJson({
        'id': 'c1',
        'contract_number': 'CTR-001',
        'title': 'Cloud Hosting',
        'description': 'Annual hosting agreement',
        'contract_type': 'subscription',
        'status': 'active',
        'vendor_id': 'v1',
        'vendor_name': 'Acme Cloud',
        'currency': 'EUR',
        'total_value': 120000,
        'spend_limit': 100000,
        'not_to_exceed': true,
        'start_date': '2026-01-01',
        'end_date': '2026-12-31',
        'signed_date': '2025-12-15',
        'auto_renew': true,
        'renewal_term_months': 12,
        'renewal_notice_days': 60,
        'payment_terms': 'Net 30',
        'line_items': [
          {
            'id': 'li1',
            'line_number': 1,
            'item_code': 'SVC-1',
            'description': 'Compute',
            'quantity': 10,
            'unit_price': 1000,
            'total': 10000,
            'gl_account': '6000',
          },
        ],
        'spend': {
          'invoiced_total': 25000,
          'invoice_count': 3,
          'spend_limit': 100000,
          'remaining': 75000,
          'over_limit': false,
        },
        'created_at': '2025-12-01T12:00:00',
        'updated_at': '2026-01-05T08:00:00',
      });

      expect(contract.id, 'c1');
      expect(contract.contractNumber, 'CTR-001');
      expect(contract.title, 'Cloud Hosting');
      expect(contract.contractType, ContractType.subscription);
      expect(contract.status, ContractStatus.active);
      expect(contract.vendorName, 'Acme Cloud');
      expect(contract.currency, 'EUR');
      expect(contract.totalValue, 120000);
      expect(contract.spendLimit, 100000);
      expect(contract.notToExceed, isTrue);
      expect(contract.startDate, DateTime.parse('2026-01-01'));
      expect(contract.endDate, DateTime.parse('2026-12-31'));
      expect(contract.signedDate, DateTime.parse('2025-12-15'));
      expect(contract.autoRenew, isTrue);
      expect(contract.renewalTermMonths, 12);
      expect(contract.renewalNoticeDays, 60);
      expect(contract.paymentTerms, 'Net 30');
      expect(contract.lineItems, hasLength(1));
      expect(contract.lineItems.first.description, 'Compute');
      expect(contract.lineItems.first.total, 10000);
      expect(contract.spend, isNotNull);
      expect(contract.spend!.invoicedTotal, 25000);
      expect(contract.spend!.invoiceCount, 3);
      expect(contract.spend!.remaining, 75000);
      expect(contract.spend!.overLimit, isFalse);
    });

    test('leaves an absent currency NULL and defaults bool flags to false', () {
      final contract = Contract.fromJson({
        'id': 'c1',
        'contract_type': 'service',
        'status': 'draft',
        'created_at': '2026-01-01T12:00:00',
      });
      // It used to substitute 'USD'. A contract whose currency the payload
      // never named would then have every figure on its detail screen — value,
      // spend limit, line-item prices — labelled in a currency nobody
      // established. `null` renders those bare instead; see
      // `utils/money.dart` and `docs/decisions.md` §161.
      expect(contract.currency, isNull);
      expect(contract.notToExceed, isFalse);
      expect(contract.autoRenew, isFalse);
    });

    test('leaves optional fields null and line_items empty when missing', () {
      final contract = Contract.fromJson({
        'id': 'c1',
        'contract_type': 'other',
        'status': 'draft',
        'created_at': '2026-01-01T12:00:00',
      });
      expect(contract.contractNumber, isNull);
      expect(contract.title, isNull);
      expect(contract.totalValue, isNull);
      expect(contract.spendLimit, isNull);
      expect(contract.startDate, isNull);
      expect(contract.endDate, isNull);
      expect(contract.signedDate, isNull);
      expect(contract.spend, isNull);
      expect(contract.updatedAt, isNull);
      expect(contract.lineItems, isEmpty);
    });

    test('coerces integer numerics to double', () {
      final contract = Contract.fromJson({
        'id': 'c1',
        'contract_type': 'purchase',
        'status': 'active',
        'total_value': 5000, // integer from JSON
        'created_at': '2026-01-01T12:00:00',
      });
      expect(contract.totalValue, 5000.0);
      expect(contract.totalValue, isA<double>());
    });

    test('unknown status/type strings degrade rather than throwing', () {
      final contract = Contract.fromJson({
        'id': 'c1',
        'contract_type': 'bogus',
        'status': 'bogus',
        'created_at': '2026-01-01T12:00:00',
      });
      expect(contract.status, ContractStatus.draft);
      expect(contract.contractType, ContractType.other);
    });
  });
}
