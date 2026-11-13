import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/action_verdict.dart';

// `GET /api/invoices/{id}/review-actions` verdicts (backend decisions §374):
// an explicit null is "the server would accept", a coded map is a refusal, and
// anything else — a missing key above all — offers nothing.
void main() {
  Map<String, dynamic> refusal(String code) =>
      {'code': code, 'message': 'server english', 'params': <String, dynamic>{}};

  test('an explicit null is allowed and offered', () {
    final v = ActionVerdict.fromVerdicts({'recall_approval': null}, 'recall_approval');
    expect(v.answered, isTrue);
    expect(v.allowed, isTrue);
    expect(v.isOffered(), isTrue);
  });

  test('a missing key fails closed', () {
    final v = ActionVerdict.fromVerdicts({'approve': null}, 'recall_approval');
    expect(v.answered, isFalse);
    expect(v.allowed, isFalse);
    expect(v.isOffered(), isFalse);
  });

  test('a refusal the reader can act on is offered, disabled', () {
    final v = ActionVerdict.fromVerdicts(
      {'recall_approval': refusal('invoice_recall_claimed')},
      'recall_approval',
    );
    expect(v.allowed, isFalse);
    expect(v.code, 'invoice_recall_claimed');
    expect(v.isOffered(hiddenCodes: const {'invoice_not_recallable'}), isTrue);
  });

  test('a role refusal and a hidden status refusal are not offered', () {
    final role = ActionVerdict.fromVerdicts(
      {'recall_approval': refusal('role_not_permitted')},
      'recall_approval',
    );
    expect(role.isOffered(), isFalse);
    final status = ActionVerdict.fromVerdicts(
      {'recall_approval': refusal('invoice_not_recallable')},
      'recall_approval',
    );
    expect(status.isOffered(hiddenCodes: const {'invoice_not_recallable'}), isFalse);
  });

  test('a malformed value fails closed', () {
    final v = ActionVerdict.fromVerdicts({'recall_approval': 'yes'}, 'recall_approval');
    expect(v.answered, isFalse);
  });
}
