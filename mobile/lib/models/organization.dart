import 'package:feohledger_mobile/utils/money.dart';

/// The safe, editable subset of organization settings the mobile app exposes —
/// the company profile and invoice defaults (mirrors the web Org Settings page's
/// least-sensitive tabs). ERP credentials, payment/webhook secrets, extraction
/// keys and SSO live in the same `settings` JSONB but are deliberately NOT
/// surfaced or sent from mobile.
class OrgSettings {
  final String id;
  final String name;
  final String slug;
  final String plan;

  // company.*
  final String companyAddress;
  final String companyPhone;
  final String companyWebsite;
  final String companyTaxId;
  // Carried through (not edited on mobile) so a company-subtree replace on
  // save doesn't drop the white-label logo the web app set.
  final String companyLogoUrl;

  /// The org's REPORTING (base) currency — `settings.reporting_currency`, a
  /// bare top-level string rather than a block. Every cross-currency rollup
  /// the API serves is denominated in the code
  /// `currency_conversion.resolve_reporting_currency` picks, and this is its
  /// first candidate. `null` when the org has not set one.
  final String? reportingCurrency;

  /// `settings.payments.home_currency` — the SECOND candidate in that same
  /// order, and the one the follow-up this work closed omitted. The whole
  /// `payments` block is admin-only credentials EXCEPT this key, which
  /// `org_settings_view.NON_ADMIN_SETTINGS` admits by name for exactly this
  /// read. `null` when unset.
  final String? paymentsHomeCurrency;

  // invoice_defaults.*

  /// `settings.invoice_defaults.currency` as the org actually set it — `null`
  /// when it did not. Distinct from [defaultCurrency], which substitutes a
  /// platform default for the edit form. The distinction is the whole point:
  /// a rung that always answers makes the rungs after it unreachable
  /// (`docs/decisions.md` §119), and rung 3 is the last one a client can see.
  final String? configuredInvoiceCurrency;

  final String defaultPaymentTerms;
  final String invoiceNumberPrefix;
  final String defaultGlAccount;
  final String defaultCostCenter;

  const OrgSettings({
    required this.id,
    required this.name,
    required this.slug,
    required this.plan,
    required this.companyAddress,
    required this.companyPhone,
    required this.companyWebsite,
    required this.companyTaxId,
    required this.companyLogoUrl,
    this.reportingCurrency,
    this.paymentsHomeCurrency,
    this.configuredInvoiceCurrency,
    required this.defaultPaymentTerms,
    required this.invoiceNumberPrefix,
    required this.defaultGlAccount,
    required this.defaultCostCenter,
  });

  /// What the org's aggregate figures are denominated in, or `null` when it
  /// declares nothing usable.
  ///
  /// The first three rungs of
  /// `backend/app/services/currency_conversion.py::resolve_reporting_currency`,
  /// in its order — and the direct mirror of the web
  /// `utils/reportingCurrency.ts::resolveReportingCurrency`, which exists so
  /// the two surfaces cannot drift. The fourth rung is the server-side
  /// `settings.reporting_currency_default`, which no client can read; a client
  /// that substituted `USD` in its place would be guessing at a value the
  /// operator may have changed.
  ///
  /// Returns `null` rather than a default for the reason §119 records: this is
  /// the layer that must be able to abstain. The caller decides what an
  /// unproven currency renders as — and in this app it renders as no symbol
  /// at all, never as a dollar sign.
  String? get resolvedReportingCurrency =>
      normalizeCurrencyCode(reportingCurrency) ??
      normalizeCurrencyCode(paymentsHomeCurrency) ??
      normalizeCurrencyCode(configuredInvoiceCurrency);

  /// The invoice-default currency for the EDIT FORM, which needs a value in
  /// its field. Never use it to label a figure — see
  /// [resolvedReportingCurrency].
  String get defaultCurrency => configuredInvoiceCurrency ?? 'USD';

  factory OrgSettings.fromJson(Map<String, dynamic> json) {
    final settings = (json['settings'] as Map<String, dynamic>?) ?? const {};
    final company = (settings['company'] as Map<String, dynamic>?) ?? const {};
    final defaults =
        (settings['invoice_defaults'] as Map<String, dynamic>?) ?? const {};
    final payments = (settings['payments'] as Map<String, dynamic>?) ?? const {};

    String s(Map<String, dynamic> m, String k) => (m[k] as String?) ?? '';

    return OrgSettings(
      id: json['id'] as String? ?? '',
      name: json['name'] as String? ?? '',
      slug: json['slug'] as String? ?? '',
      plan: json['plan'] as String? ?? '',
      companyAddress: s(company, 'address'),
      companyPhone: s(company, 'phone'),
      companyWebsite: s(company, 'website'),
      companyTaxId: s(company, 'tax_id'),
      companyLogoUrl: s(company, 'logo_url'),
      reportingCurrency: settings['reporting_currency'] as String?,
      paymentsHomeCurrency: payments['home_currency'] as String?,
      configuredInvoiceCurrency: defaults['currency'] as String?,
      defaultPaymentTerms: (defaults['payment_terms'] as String?) ?? 'Net 30',
      invoiceNumberPrefix: (defaults['number_prefix'] as String?) ?? 'INV-',
      defaultGlAccount: s(defaults, 'default_gl_account'),
      defaultCostCenter: s(defaults, 'default_cost_center'),
    );
  }
}

/// The partial PATCH body for an org-settings edit. Sends `name` plus a
/// `settings` patch carrying ONLY the `company` + `invoice_defaults` sub-trees
/// (the backend merges top-level keys, so untouched keys like `erp` survive).
class OrgSettingsUpdate {
  final String name;
  final String companyAddress;
  final String companyPhone;
  final String companyWebsite;
  final String companyTaxId;
  final String companyLogoUrl;
  final String defaultCurrency;
  final String defaultPaymentTerms;
  final String invoiceNumberPrefix;
  final String defaultGlAccount;
  final String defaultCostCenter;

  const OrgSettingsUpdate({
    required this.name,
    required this.companyAddress,
    required this.companyPhone,
    required this.companyWebsite,
    required this.companyTaxId,
    required this.companyLogoUrl,
    required this.defaultCurrency,
    required this.defaultPaymentTerms,
    required this.invoiceNumberPrefix,
    required this.defaultGlAccount,
    required this.defaultCostCenter,
  });

  Map<String, dynamic> toJson() => {
        'name': name,
        // The backend's UpdateOrganizationRequest shallow-merges these top-level
        // settings keys into the existing dict, so we send the whole `company`
        // and `invoice_defaults` sub-objects (their own fields are replaced).
        'settings': {
          'company': {
            'address': companyAddress,
            'phone': companyPhone,
            'website': companyWebsite,
            'tax_id': companyTaxId,
            'logo_url': companyLogoUrl,
          },
          'invoice_defaults': {
            'currency': defaultCurrency,
            'payment_terms': defaultPaymentTerms,
            'number_prefix': invoiceNumberPrefix,
            'default_gl_account': defaultGlAccount,
            'default_cost_center': defaultCostCenter,
          },
        },
      };
}
