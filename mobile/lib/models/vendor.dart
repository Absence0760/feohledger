/// Vendor lifecycle status. Mirrors the backend `Vendor.status` column
/// (`active` / `unverified` / `inactive` / `rejected`).
enum VendorStatus {
  active('active'),
  unverified('unverified'),
  inactive('inactive'),
  rejected('rejected');

  const VendorStatus(this.value);
  final String value;

  static VendorStatus fromString(String s) {
    return VendorStatus.values.firstWhere(
      (e) => e.value == s,
      orElse: () => VendorStatus.unverified,
    );
  }

  String get label => switch (this) {
    VendorStatus.active => 'Active',
    VendorStatus.unverified => 'Unverified',
    VendorStatus.inactive => 'Inactive',
    VendorStatus.rejected => 'Rejected',
  };

  /// Verify/reject are only offered while the vendor is awaiting review —
  /// the backend `verify` endpoint 409s from any status other than
  /// `unverified`. (Reject also accepts `active` server-side, but the
  /// mobile review flow only surfaces actions on the unverified queue.)
  bool get isUnverified => this == VendorStatus.unverified;
}

class Vendor {
  final String id;
  final String name;
  final String? code;
  final String? email;
  final String? phone;
  final VendorStatus status;
  final String source;
  final String? paymentTerms;
  final String? verifiedBy;
  final String? erpVendorId;
  final int invoiceCount;

  /// Set only on the response to a write that left the vendor un-payable
  /// (reject / deactivate / block / sanctions match): its virtual cards that
  /// are STILL LIVE after the write. `cardsNotClosed` — the card provider did
  /// not confirm the cancel; `cardsRequireVoid` — the card backs a live
  /// payment, closed only by voiding it. Both 0 on every list read.
  /// Backend: `services/vendor_card_revocation.py`.
  final int cardsNotClosed;
  final int cardsRequireVoid;

  Vendor({
    required this.id,
    required this.name,
    this.code,
    this.email,
    this.phone,
    required this.status,
    required this.source,
    this.paymentTerms,
    this.verifiedBy,
    this.erpVendorId,
    this.invoiceCount = 0,
    this.cardsNotClosed = 0,
    this.cardsRequireVoid = 0,
  });

  factory Vendor.fromJson(Map<String, dynamic> json) {
    return Vendor(
      id: json['id'] as String,
      name: json['name'] as String? ?? 'Unknown',
      code: json['code'] as String?,
      email: json['email'] as String?,
      phone: json['phone'] as String?,
      status: VendorStatus.fromString(
        json['status'] as String? ?? 'unverified',
      ),
      source: json['source'] as String? ?? 'manual',
      paymentTerms: json['payment_terms'] as String?,
      verifiedBy: json['verified_by'] as String?,
      erpVendorId: json['erp_vendor_id'] as String?,
      invoiceCount: json['invoice_count'] as int? ?? 0,
      cardsNotClosed: _listLength(json['card_revocation'], 'not_closed'),
      cardsRequireVoid: _listLength(
        json['card_revocation'],
        'requires_payment_void',
      ),
    );
  }

  static int _listLength(Object? revocation, String key) {
    if (revocation is! Map<String, dynamic>) return 0;
    final list = revocation[key];
    return list is List ? list.length : 0;
  }

  /// `manual` / `erp_sync` / `ai_extracted` — display label for the source.
  String get sourceLabel => switch (source) {
    'erp_sync' => 'ERP',
    'ai_extracted' => 'AI',
    'manual' => 'Manual',
    _ => source,
  };
}
