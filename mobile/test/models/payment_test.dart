import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/payment.dart';

void main() {
  group('PaymentMethod / PaymentStatus enums', () {
    test('fromString round-trips known values', () {
      for (final m in PaymentMethod.values) {
        expect(PaymentMethod.fromString(m.value), m);
      }
      for (final s in PaymentStatus.values) {
        expect(PaymentStatus.fromString(s.value), s);
      }
    });

    test('fromString falls back to a sane default for unknown values', () {
      expect(PaymentMethod.fromString('bitcoin'), PaymentMethod.ach);
      expect(PaymentStatus.fromString('???'), PaymentStatus.pending);
    });

    test('payment method labels are human-readable', () {
      expect(PaymentMethod.ach.label, 'ACH');
      expect(PaymentMethod.virtualCard.label, 'Virtual Card');
    });
  });

  group('Payment.fromJson', () {
    test('parses a payment and coerces amount to double', () {
      final payment = Payment.fromJson({
        'id': 'p1',
        'invoice_id': 'inv1',
        'amount': 500, // integer
        'method': 'wire',
        'status': 'completed',
        'reference': 'WIRE-1',
        'created_at': '2026-01-01T12:00:00',
      });
      expect(payment.id, 'p1');
      expect(payment.invoiceId, 'inv1');
      expect(payment.amount, 500.0);
      expect(payment.method, PaymentMethod.wire);
      expect(payment.status, PaymentStatus.completed);
      expect(payment.reference, 'WIRE-1');
    });

    test('reference is optional', () {
      final payment = Payment.fromJson({
        'id': 'p1',
        'invoice_id': 'inv1',
        'amount': 1.0,
        'method': 'ach',
        'status': 'pending',
        'created_at': '2026-01-01T12:00:00',
      });
      expect(payment.reference, isNull);
    });
  });

  group('DashboardData.fromJson', () {
    test('parses a full dashboard payload', () {
      final data = DashboardData.fromJson({
        'total_invoices': 12,
        'total_amount': 9876.54,
        'pipeline': {'pending': 3, 'approved': 2},
        'vendor_spend': [
          {'vendor': 'Acme', 'amount': 1000, 'invoice_count': 4},
        ],
        'aging': {
          'current': 100,
          'days_30': 200,
          'days_60': 300,
          'days_90_plus': 400,
        },
        'monthly_trend': [
          {'month': '2026-01', 'count': 5, 'amount': 500},
        ],
        'upcoming_payments': [
          {'amount': 250},
          {'amount': 250},
        ],
        'upcoming_total_amount': 500,
      });

      expect(data.totalInvoices, 12);
      expect(data.totalAmount, 9876.54);
      expect(data.pipeline, {'pending': 3, 'approved': 2});

      expect(data.topVendors, hasLength(1));
      expect(data.topVendors.first.vendorName, 'Acme');
      expect(data.topVendors.first.totalAmount, 1000.0);

      expect(data.aging.current, 100.0);
      expect(data.aging.thirtyDays, 200.0);
      expect(data.aging.sixtyDays, 300.0);
      expect(data.aging.ninetyPlus, 400.0);

      expect(data.trends, hasLength(1));
      expect(data.trends.first.month, '2026-01');

      // count comes from the list length; totalAmount is a separate
      // server-computed aggregate (upcoming_total_amount) — never folded
      // from the list on-device.
      expect(data.upcoming.count, 2);
      expect(data.upcoming.totalAmount, 500.0);
    });

    test('tolerates a completely empty payload with safe defaults', () {
      final data = DashboardData.fromJson({});
      expect(data.totalInvoices, 0);
      expect(data.totalAmount, 0);
      expect(data.pipeline, isEmpty);
      expect(data.topVendors, isEmpty);
      expect(data.trends, isEmpty);
      expect(data.aging.current, 0);
      expect(data.upcoming.count, 0);
      expect(data.upcoming.totalAmount, 0);
    });

    test('aging accepts the alternate key spellings', () {
      final data = DashboardData.fromJson({
        'aging': {
          'current': 1,
          '30_days': 2,
          '60_days': 3,
          '90_plus': 4,
        },
      });
      expect(data.aging.thirtyDays, 2.0);
      expect(data.aging.sixtyDays, 3.0);
      expect(data.aging.ninetyPlus, 4.0);
    });

    test('vendor_spend accepts vendor_name/total_amount spellings', () {
      final data = DashboardData.fromJson({
        'vendor_spend': [
          {'vendor_name': 'Beta', 'total_amount': 42, 'invoice_count': 1},
        ],
      });
      expect(data.topVendors.first.vendorName, 'Beta');
      expect(data.topVendors.first.totalAmount, 42.0);
    });
  });

  group('DashboardData unconverted counts', () {
    // Every reporting-currency rollup on `GET /dashboard` keeps a row it could
    // not convert in at FACE value and COUNTS it. The screen can only disclose
    // what the model parsed, so each count is pinned at its own key.
    Map<String, dynamic> payload({Object? counts = true}) => {
          'reporting': {
            'reporting_currency': 'USD',
            'total_amount': 100,
            if (counts == true) 'unconverted_count': 3,
            if (counts == null) 'unconverted_count': null,
          },
          'aging_reporting': {
            'current': 1,
            if (counts == true) 'unconverted_count': 2,
            if (counts == null) 'unconverted_count': null,
          },
          'vendor_spend': [
            {
              'vendor': 'Acme',
              'amount': 10,
              'invoice_count': 4,
              if (counts == true) 'unconverted_count': 1,
              if (counts == null) 'unconverted_count': null,
            },
          ],
          'upcoming_total_amount_reporting': 5,
          if (counts == true) 'upcoming_unconverted_count': 4,
          if (counts == null) 'upcoming_unconverted_count': null,
        };

    test('each rollup reads its own count', () {
      final data = DashboardData.fromJson(payload());
      expect(data.unconvertedCount, 3); // reporting.unconverted_count
      expect(data.aging.unconvertedCount, 2); // aging_reporting.unconverted_count
      expect(data.topVendors.single.unconvertedCount, 1);
      expect(data.upcoming.unconvertedCount, 4); // upcoming_unconverted_count
    });

    test('an older backend without the counts reads as nothing to disclose',
        () {
      final data = DashboardData.fromJson(payload(counts: false));
      expect(data.unconvertedCount, 0);
      expect(data.aging.unconvertedCount, 0);
      expect(data.topVendors.single.unconvertedCount, 0);
      expect(data.upcoming.unconvertedCount, 0);
    });

    test('an explicit null is also zero, not a crash', () {
      final data = DashboardData.fromJson(payload(counts: null));
      expect(data.unconvertedCount, 0);
      expect(data.aging.unconvertedCount, 0);
      expect(data.topVendors.single.unconvertedCount, 0);
      expect(data.upcoming.unconvertedCount, 0);
    });

    test('the aging count travels with the bands it describes', () {
      // The screen renders `aging_reporting` whenever the payload has it, so
      // the count must come from that same object — the face-value `aging`
      // beside it is a cross-currency sum in its entirety and carries none.
      final data = DashboardData.fromJson({
        'aging': {'current': 999},
        'aging_reporting': {'current': 1, 'unconverted_count': 2},
      });
      expect(data.aging.current, 1.0);
      expect(data.aging.unconvertedCount, 2);
    });
  });

  group('upcoming.totalAmount is server-supplied, never folded on-device', () {
    // Regression for #189: the model used to `.fold<double>` the per-item
    // `amount` values in `upcoming_payments` itself, which can accumulate
    // classic binary-float drift (e.g. 0.1 + 0.2 == 0.30000000000000004).
    // It must now read the backend's own Decimal-summed
    // `upcoming_total_amount` verbatim and ignore whatever the list folds to.
    test('reads upcoming_total_amount verbatim instead of summing the list', () {
      final data = DashboardData.fromJson({
        'upcoming_payments': [
          {'amount': 0.1},
          {'amount': 0.2},
        ],
        // Deliberately NOT 0.1 + 0.2 folded as double (0.30000000000000004) —
        // a clean, exact server total. If the model ever regresses to
        // folding the list client-side, this assertion fails.
        'upcoming_total_amount': 0.3,
      });
      expect(data.upcoming.count, 2);
      expect(data.upcoming.totalAmount, 0.3);
    });

    test('a naive client-side fold of the same list would have drifted', () {
      // Documents *why* the above matters: proves 0.1 + 0.2 really does
      // drift under double arithmetic, so the fixed assertion isn't
      // trivially true regardless of which path the code takes.
      final naiveFold = [0.1, 0.2].fold<double>(0, (sum, v) => sum + v);
      expect(naiveFold, isNot(0.3));
      expect(naiveFold, 0.30000000000000004);
    });

    test('defaults to 0 when upcoming_total_amount is absent', () {
      final data = DashboardData.fromJson({
        'upcoming_payments': [
          {'amount': 100},
        ],
      });
      expect(data.upcoming.totalAmount, 0);
    });

    test('accepts an integer upcoming_total_amount', () {
      final data = DashboardData.fromJson({
        'upcoming_total_amount': 500,
      });
      expect(data.upcoming.totalAmount, 500.0);
    });
  });

  group('PaymentMethod parsing', () {
    test('fromString keeps its ACH fallback for display paths', () {
      expect(PaymentMethod.fromString('wire'), PaymentMethod.wire);
      expect(PaymentMethod.fromString('virtual_card'), PaymentMethod.virtualCard);
      expect(PaymentMethod.fromString('rtp_instant'), PaymentMethod.ach);
    });

    test('tryFromString is strict — null for a rail this build cannot name',
        () {
      // The payment queue's `required_method` is a DECISION, not a label: an
      // unknown code read as ACH would stage a run on the one rail the backend
      // has just refused. `PaymentQueueItem.isSelectable` reads this null.
      expect(PaymentMethod.tryFromString('check'), PaymentMethod.check);
      expect(PaymentMethod.tryFromString('rtp_instant'), isNull);
      expect(PaymentMethod.tryFromString(''), isNull);
    });
  });
}
