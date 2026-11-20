import 'package:feohledger_mobile/models/action_verdict.dart';

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

  /// The list row's `status_actions` as served (`api/vendors.py::
  /// vendor_status_actions` — `vendor.manage`, then the one status table
  /// `POST /{id}/verify` and `/reject` refuse from): `{verify, reject}`, each
  /// `null` (accepted) or a coded refusal. Kept raw so the offline cache
  /// round-trips it; read through [verifyVerdict] / [rejectVerdict]. `null`
  /// on a read that carries none (a single-vendor read, an older backend),
  /// which offers nothing.
  final Map<String, dynamic>? statusActions;

  /// The caller's verdict on `POST /api/vendors/{id}/verify`.
  ActionVerdict get verifyVerdict => statusActions == null
      ? ActionVerdict.unanswered
      : ActionVerdict.fromVerdicts(statusActions!, 'verify');

  /// The caller's verdict on `POST /api/vendors/{id}/reject`.
  ActionVerdict get rejectVerdict => statusActions == null
      ? ActionVerdict.unanswered
      : ActionVerdict.fromVerdicts(statusActions!, 'reject');

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
    this.statusActions,
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
      statusActions: json['status_actions'] is Map
          ? Map<String, dynamic>.from(json['status_actions'] as Map)
          : null,
    );
  }

  static int _listLength(Object? revocation, String key) {
    if (revocation is! Map<String, dynamic>) return 0;
    final list = revocation[key];
    return list is List ? list.length : 0;
  }
}
