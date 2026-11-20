import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/exception.dart';

Map<String, dynamic> _json(Object? amount) => {
      'id': 'exc1',
      'invoice_id': 'inv1',
      'amount': amount,
      'currency': 'USD',
      'exception_type': 'duplicate',
      'severity': 'error',
      'status': 'open',
      'created_at': '2026-01-01T12:00:00',
    };

void main() {
  group('ApException.amount reads both wire shapes', () {
    test('the exact decimal string passes through verbatim', () {
      expect(ApException.fromJson(_json('1234.50')).amount, '1234.50');
    });

    test('a string past double precision is not rounded', () {
      // 17 significant digits — a double would turn this into ...234.56 ± 1c.
      expect(
        ApException.fromJson(_json('123456789012345.67')).amount,
        '123456789012345.67',
      );
    });

    test('a legacy JSON number reads as the string the server now sends', () {
      expect(ApException.fromJson(_json(250)).amount, '250.00');
      expect(ApException.fromJson(_json(1234.5)).amount, '1234.50');
    });

    test('no figure stays no figure', () {
      expect(ApException.fromJson(_json(null)).amount, isNull);
      expect(ApException.fromJson(_json('  ')).amount, isNull);
      expect(ApException.fromJson(_json(true)).amount, isNull);
    });
  });

  group('PoRematchClears reads only its own report', () {
    test('the PO number and calendar day come through', () {
      final r = PoRematchClears.fromErpReport({
        'kind': 'po_rematch_clears',
        'po_number': ' PO-7 ',
        'observed_on': '2026-10-26',
      })!;
      expect(r.poNumber, 'PO-7');
      expect(r.observedOn, DateTime(2026, 10, 26));
    });

    test('a missing or malformed field stays missing', () {
      final r = PoRematchClears.fromErpReport({
        'kind': 'po_rematch_clears',
        'po_number': '',
        'observed_on': '2026-10-26T00:00:00Z',
      })!;
      expect(r.poNumber, isNull);
      expect(r.observedOn, isNull);
    });

    test('another kind, or no report, is null', () {
      expect(
        PoRematchClears.fromErpReport({'kind': 'paid_not_recorded'}),
        isNull,
      );
      expect(PoRematchClears.fromErpReport(null), isNull);
      expect(PoRematchClears.fromErpReport('po_rematch_clears'), isNull);
    });
  });

  // `decision_refusals` (`api/exceptions.py::decision_refusals`): the queue
  // and detail offer Resolve / Dismiss / Escalate from these.
  group('ApException decision verdicts', () {
    ApException row(Object? decisions) =>
        ApException.fromJson({..._json(null), 'decision_refusals': decisions});

    test('a manager on an open row may take all three', () {
      final e = row({'resolve': null, 'dismiss': null, 'escalate': null});
      expect(e.resolveVerdict.allowed, isTrue);
      expect(e.dismissVerdict.allowed, isTrue);
      expect(e.escalateVerdict.allowed, isTrue);
    });

    test('an implicated manager is refused clearing but may escalate', () {
      const sod = {'code': 'segregation_implicated', 'message': 'm'};
      final e = row({'resolve': sod, 'dismiss': sod, 'escalate': null});
      expect(e.resolveVerdict.isOffered(hiddenCodes: kExceptionHiddenRefusals),
          isTrue);
      expect(e.resolveVerdict.allowed, isFalse);
      expect(e.escalateVerdict.allowed, isTrue);
    });

    test('a terminal row or a role refusal offers nothing', () {
      const state = {'code': 'exception_not_actionable', 'message': 'm'};
      const role = {'code': 'role_not_permitted', 'message': 'm'};
      for (final r in [state, role]) {
        final e = row({'resolve': r, 'dismiss': r, 'escalate': r});
        for (final v in [e.resolveVerdict, e.dismissVerdict, e.escalateVerdict]) {
          expect(v.isOffered(hiddenCodes: kExceptionHiddenRefusals), isFalse);
        }
      }
    });

    test('no verdict fails closed', () {
      expect(row(null).resolveVerdict.answered, isFalse);
      expect(row(<String, dynamic>{}).escalateVerdict.answered, isFalse);
    });
  });
}
