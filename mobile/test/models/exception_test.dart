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
}
