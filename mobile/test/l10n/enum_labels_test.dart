// The localized enum names in `lib/l10n/enum_labels.dart` — the only place a
// model enum becomes reader-facing text, now that the models' English `label`
// getters are gone. The switches are exhaustive, so a missing value fails to
// compile; these pin the English wording the getters used to carry and prove
// each name is translated rather than echoed.
import 'package:flutter/widgets.dart' show Locale;
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/enum_labels.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/contract.dart';
import 'package:feohledger_mobile/models/exception.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/models/vendor.dart';

void main() {
  final en = lookupAppLocalizations(const Locale('en'));
  final ja = lookupAppLocalizations(const Locale('ja'));

  test('English names keep the wording the model getters carried', () {
    expect(invoiceStatusLabel(en, InvoiceStatus.readyForReview),
        'Ready for Review');
    expect(invoiceStatusLabel(en, InvoiceStatus.paymentScheduled),
        'Payment Scheduled');
    expect(paymentMethodLabel(en, PaymentMethod.ach), 'ACH');
    expect(paymentMethodLabel(en, PaymentMethod.virtualCard), 'Virtual Card');
    expect(vendorStatusLabel(en, VendorStatus.active), 'Active');
    expect(vendorStatusLabel(en, VendorStatus.unverified), 'Unverified');
    expect(vendorSourceLabel(en, 'erp_sync'), 'ERP');
    expect(vendorSourceLabel(en, 'manual'), 'Manual');
  });

  test('a vendor source or notification event this build does not know is '
      'named generically, never echoed raw', () {
    expect(vendorSourceLabel(en, 'csv_import'), 'Other source');
    expect(notificationEventLabel(en, 'brand_new_event'),
        isNot(contains('brand_new_event')));
  });

  test('every status, type and severity is translated, not echoed', () {
    final pairs = <(String, String)>[
      for (final s in InvoiceStatus.values)
        (invoiceStatusLabel(en, s), invoiceStatusLabel(ja, s)),
      for (final s in VendorStatus.values)
        (vendorStatusLabel(en, s), vendorStatusLabel(ja, s)),
      for (final s in ContractStatus.values)
        (contractStatusLabel(en, s), contractStatusLabel(ja, s)),
      // SLA is the one acronym the Japanese catalogue keeps as written.
      for (final t in ContractType.values)
        if (t != ContractType.sla)
          (contractTypeLabel(en, t), contractTypeLabel(ja, t)),
      for (final s in ApExceptionStatus.values)
        (exceptionStatusLabel(en, s), exceptionStatusLabel(ja, s)),
      for (final s in ApExceptionSeverity.values)
        (exceptionSeverityLabel(en, s), exceptionSeverityLabel(ja, s)),
      for (final s in PaymentStatus.values)
        (paymentStatusLabel(en, s), paymentStatusLabel(ja, s)),
    ];
    for (final (english, japanese) in pairs) {
      expect(english, isNotEmpty);
      expect(japanese, isNot(english), reason: '"$english" is untranslated');
    }
  });
}
