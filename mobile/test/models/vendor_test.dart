import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/vendor.dart';

void main() {
  group('VendorStatus enum', () {
    test('fromString round-trips known values', () {
      for (final s in VendorStatus.values) {
        expect(VendorStatus.fromString(s.value), s);
      }
    });

    test('fromString falls back to unverified for unknown values', () {
      expect(VendorStatus.fromString('???'), VendorStatus.unverified);
    });

    test('isUnverified is true only for unverified', () {
      expect(VendorStatus.unverified.isUnverified, isTrue);
      expect(VendorStatus.active.isUnverified, isFalse);
      expect(VendorStatus.rejected.isUnverified, isFalse);
    });

    test('labels are human-readable', () {
      expect(VendorStatus.active.label, 'Active');
      expect(VendorStatus.unverified.label, 'Unverified');
    });
  });

  group('Vendor.fromJson card revocation', () {
    test('counts the still-live cards a reject left behind', () {
      final v = Vendor.fromJson({
        'id': 'v1',
        'name': 'Gone Co',
        'status': 'rejected',
        'source': 'manual',
        'card_revocation': {
          'vendor_id': 'v1',
          'cancelled': 1,
          'not_closed': [
            {
              'card_id': 'c1',
              'last_four': '4242',
              'outcome': 'card_cancel_rejected',
            },
            {
              'card_id': 'c2',
              'last_four': '1111',
              'outcome': 'cards_not_configured',
            },
          ],
          'requires_payment_void': [
            {
              'card_id': 'c3',
              'last_four': '0000',
              'outcome': 'payment_live',
              'payment_id': 'p1',
            },
          ],
        },
      });
      expect(v.cardsNotClosed, 2);
      expect(v.cardsRequireVoid, 1);
    });

    test('is zero when the field is absent or null', () {
      for (final rev in [null, 'garbage']) {
        final v = Vendor.fromJson({
          'id': 'v1',
          'name': 'Plain Co',
          'status': 'active',
          'source': 'manual',
          'card_revocation': rev,
        });
        expect(v.cardsNotClosed, 0);
        expect(v.cardsRequireVoid, 0);
      }
    });
  });

  group('Vendor.fromJson', () {
    test('parses a full vendor row', () {
      final v = Vendor.fromJson({
        'id': 'v1',
        'name': 'Acme Supplies',
        'code': 'ACME',
        'email': 'ap@acme.com',
        'phone': '555-1234',
        'status': 'active',
        'source': 'erp_sync',
        'payment_terms': 'Net 30',
        'verified_by': 'Jane',
        'erp_vendor_id': 'ERP-1',
        'invoice_count': 7,
      });
      expect(v.id, 'v1');
      expect(v.name, 'Acme Supplies');
      expect(v.status, VendorStatus.active);
      expect(v.source, 'erp_sync');
      expect(v.sourceLabel, 'ERP');
      expect(v.invoiceCount, 7);
    });

    test('tolerates missing optional fields', () {
      final v = Vendor.fromJson({
        'id': 'v2',
        'name': 'Bare Vendor',
        'status': 'unverified',
        'source': 'manual',
        'created_at': '2026-01-01T00:00:00',
      });
      expect(v.code, isNull);
      expect(v.email, isNull);
      expect(v.invoiceCount, 0);
      expect(v.status, VendorStatus.unverified);
      expect(v.sourceLabel, 'Manual');
    });
  });
}
