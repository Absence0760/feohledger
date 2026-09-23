import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/organization.dart';

void main() {
  group('OrgSettings.fromJson', () {
    test('projects the company + invoice_defaults subtrees', () {
      final s = OrgSettings.fromJson({
        'id': 'org1',
        'name': 'Acme',
        'slug': 'acme',
        'plan': 'pro',
        'settings': {
          'company': {
            'address': '1 Main',
            'phone': '555',
            'website': 'https://x',
            'tax_id': '12-3',
            'logo_url': 'https://x/l.png',
          },
          'invoice_defaults': {
            'currency': 'GBP',
            'payment_terms': 'Net 14',
            'number_prefix': 'AP-',
            'default_gl_account': '6000',
            'default_cost_center': 'CC-1',
          },
          // A sensitive key the model must ignore entirely.
          'erp': {'credentials': 'secret'},
        },
      });

      expect(s.name, 'Acme');
      expect(s.companyAddress, '1 Main');
      expect(s.companyLogoUrl, 'https://x/l.png');
      expect(s.defaultCurrency, 'GBP');
      expect(s.defaultGlAccount, '6000');
      expect(s.defaultCostCenter, 'CC-1');
    });

    test('falls back to sensible defaults when settings is empty', () {
      final s = OrgSettings.fromJson({
        'id': 'org1',
        'name': 'Acme',
        'slug': 'acme',
        'plan': 'free',
      });

      expect(s.companyAddress, '');
      expect(s.defaultCurrency, 'USD');
      expect(s.defaultPaymentTerms, 'Net 30');
      expect(s.invoiceNumberPrefix, 'INV-');
    });

    test('reads the top-level resolved_reporting_currency field', () {
      final s = OrgSettings.fromJson({
        'id': 'org1',
        'name': 'Acme',
        'slug': 'acme',
        'plan': 'pro',
        'resolved_reporting_currency': 'GBP',
        'settings': <String, dynamic>{},
      });

      expect(s.serverResolvedReportingCurrency, 'GBP');
    });
  });

  group('OrgSettingsUpdate.toJson', () {
    test('sends only name + the two safe subtrees', () {
      const update = OrgSettingsUpdate(
        name: 'Acme',
        companyAddress: 'A',
        companyPhone: 'P',
        companyWebsite: 'W',
        companyTaxId: 'T',
        companyLogoUrl: 'L',
        defaultCurrency: 'USD',
        defaultPaymentTerms: 'Net 30',
        invoiceNumberPrefix: 'INV-',
        defaultGlAccount: 'G',
        defaultCostCenter: 'C',
      );

      final json = update.toJson();
      final settings = json['settings'] as Map<String, dynamic>;

      expect(json['name'], 'Acme');
      expect(settings.keys, ['company', 'invoice_defaults']);
      expect((settings['company'] as Map)['logo_url'], 'L');
      expect((settings['invoice_defaults'] as Map)['default_cost_center'], 'C');
    });
  });
}
