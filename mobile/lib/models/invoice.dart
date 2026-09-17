enum InvoiceStatus {
  newStatus('new'),
  pending('pending'),
  readyForReview('ready_for_review'),
  approved('approved'),
  rejected('rejected'),
  sendingToErp('sending_to_erp'),
  sentToErp('sent_to_erp'),
  postedInErp('posted_in_erp'),
  paymentScheduled('payment_scheduled'),
  paid('paid'),
  done('done'),
  failed('failed');

  const InvoiceStatus(this.value);
  final String value;

  static InvoiceStatus fromString(String s) {
    return InvoiceStatus.values.firstWhere(
      (e) => e.value == s,
      orElse: () => InvoiceStatus.newStatus,
    );
  }

  String get label => switch (this) {
    InvoiceStatus.newStatus => 'New',
    InvoiceStatus.pending => 'Pending',
    InvoiceStatus.readyForReview => 'Ready for Review',
    InvoiceStatus.approved => 'Approved',
    InvoiceStatus.rejected => 'Rejected',
    InvoiceStatus.sendingToErp => 'Sending to ERP',
    InvoiceStatus.sentToErp => 'Sent to ERP',
    InvoiceStatus.postedInErp => 'Posted in ERP',
    InvoiceStatus.paymentScheduled => 'Payment Scheduled',
    InvoiceStatus.paid => 'Paid',
    InvoiceStatus.done => 'Done',
    InvoiceStatus.failed => 'Failed',
  };

  bool get isActionable =>
      this == InvoiceStatus.readyForReview;

  /// Whether the invoice's fields may be edited via `PATCH /api/invoices/{id}`.
  /// Mirrors the backend `IMMUTABLE_STATUSES` gate: once an invoice is en route
  /// to / posted in the ERP, scheduled, paid, or done, edits are rejected with
  /// 409, so the edit affordance is hidden in those states.
  bool get isEditable => switch (this) {
    InvoiceStatus.sendingToErp ||
    InvoiceStatus.sentToErp ||
    InvoiceStatus.postedInErp ||
    InvoiceStatus.paymentScheduled ||
    InvoiceStatus.paid ||
    InvoiceStatus.done =>
      false,
    _ => true,
  };

  /// Whether the invoice's *financial* content — how much, and to whom — is
  /// frozen. Mirrors the backend `_FINANCIALLY_LOCKED_STATUSES`
  /// (`backend/app/api/invoices.py`), which is literally
  /// `{approved} | IMMUTABLE_STATUSES` — written the same way here so the two
  /// halves can't drift apart: `!isEditable` IS this file's mirror of
  /// `IMMUTABLE_STATUSES`, and `approved` is the extra state it adds.
  ///
  /// The approval signature (`services/approval_signature.py`) is computed over
  /// the exact amount and the payment run reads `Invoice.amount` /
  /// `Invoice.vendor_id` straight off the row — so editing money after sign-off
  /// would pay a figure nobody approved, and re-saving the vendor would
  /// re-point the payee at another supplier's bank details (the BEC redirect
  /// the dual-control `VendorChangeRequest` gate exists to stop). A PATCH
  /// carrying any of [kFinancialInvoiceFields] in one of these states is
  /// refused with 409; reject → correct → re-approve is the way through.
  ///
  /// Note this is *narrower* than [isEditable]: an `approved` invoice is still
  /// editable (GL coding, notes, addresses) — only the money and the payee are
  /// frozen.
  bool get isFinanciallyLocked => this == InvoiceStatus.approved || !isEditable;
}

/// The invoice fields frozen once the invoice is financially locked — a mirror
/// of `_FINANCIAL_FIELDS` in `backend/app/api/invoices.py`.
///
/// These are *wire* names (PATCH body keys), which is why both `vendor` and its
/// DB spelling `vendor_name` appear: the backend runs its check before remapping
/// one to the other, and this client sends `vendor`. The set is deliberately the
/// full backend list rather than only the fields today's edit sheet renders, so
/// adding, say, a currency field to the sheet later is covered for free.
const Set<String> kFinancialInvoiceFields = {
  'amount',
  'currency',
  'subtotal',
  'tax_amount',
  'discount_amount',
  'shipping_amount',
  'tax_rate',
  'vendor',
  'vendor_name',
  'remit_to_address',
};

/// Drop every frozen financial field from a PATCH diff, preserving the rest.
///
/// The backend rejects the *whole* request with a 409 when a locked invoice's
/// PATCH carries even one financial field — so a combined edit (description +
/// amount) used to lose the description too. Omitting the frozen fields lets
/// the legitimate half through instead of failing the write.
Map<String, dynamic> stripFinancialFields(Map<String, dynamic> changes) => {
      for (final entry in changes.entries)
        if (!kFinancialInvoiceFields.contains(entry.key)) entry.key: entry.value,
    };

/// Severity of an invoice warning / fraud flag, mirroring the backend
/// `invoice_warnings` severities (`error` | `warning` | `info`).
enum WarningSeverity {
  error('error'),
  warning('warning'),
  info('info');

  const WarningSeverity(this.value);
  final String value;

  static WarningSeverity fromString(String? s) =>
      WarningSeverity.values.firstWhere(
        (e) => e.value == s,
        orElse: () => WarningSeverity.info,
      );
}

/// One invoice warning / fraud flag, as produced by
/// `services.invoice_warnings.refresh_warnings` and carried on the invoice
/// JSON as `warnings: [{type, severity, message, code, params}]`. The detail
/// screen renders these so a reviewer sees the same fraud/duplicate/past-due
/// signals the web modal shows.
///
/// **[message] is English, and it is the FALLBACK.** The backend composes each
/// finding from the row's own data, so a label per [type] could never state
/// one — a single type is up to five different sentences. [code] plus [params]
/// is what makes it translatable: `l10n/invoice_warning_messages.dart` resolves
/// the pair to an ARB key and locale-formatted parameters, and falls back to
/// [message] when it cannot (`backend/docs/invoice-warnings.md`).
class InvoiceWarning {
  final String type;
  final WarningSeverity severity;
  final String message;

  /// Stable identifier for the finding (`po_not_found`, `duplicate_similar`)
  /// from the backend's fixed catalogue. `null` for a row persisted before the
  /// catalogue existed — nothing backfills those, so the English [message] is
  /// the normal path for them rather than an edge case.
  final String? code;

  /// The finding's parameters, keyed as its message names them. Kept as the
  /// exact STRINGS the wire carried: money is `Decimal` server-side and must
  /// not round-trip through a `double`. What each one holds — money, a
  /// percentage, a date — is `invoiceWarningParamKinds[code]`.
  final Map<String, String> params;

  const InvoiceWarning({
    required this.type,
    required this.severity,
    required this.message,
    this.code,
    this.params = const {},
  });

  factory InvoiceWarning.fromJson(Map<String, dynamic> json) {
    final params = json['params'];
    return InvoiceWarning(
      type: json['type'] as String? ?? 'warning',
      severity: WarningSeverity.fromString(json['severity'] as String?),
      message: json['message'] as String? ?? '',
      code: json['code'] as String?,
      params: params is Map
          ? {
              for (final e in params.entries)
                if (e.value != null) '${e.key}': '${e.value}',
            }
          : const {},
    );
  }
}

/// Latest 2/3/4-way PO match result, carried on the invoice JSON as the
/// `po_match` dict (populated by `services.invoice_warnings.refresh_warnings`).
/// Null when the invoice has no `po_number`. Money/variance fields arrive as
/// plain numbers from the backend (display-only — never used for client math).
class PoMatch {
  /// `none` | `2-way` | `3-way` | `4-way`.
  final String matchType;

  /// `no_po` | `matched` | `mismatch` | `partial`.
  final String status;
  final double? variancePct;
  final bool? withinTolerance;
  final List<String> issues;

  const PoMatch({
    required this.matchType,
    required this.status,
    this.variancePct,
    this.withinTolerance,
    this.issues = const [],
  });

  factory PoMatch.fromJson(Map<String, dynamic> json) {
    final issues = json['issues'];
    return PoMatch(
      matchType: json['match_type'] as String? ?? 'none',
      status: json['status'] as String? ?? 'no_po',
      variancePct: (json['variance_pct'] as num?)?.toDouble(),
      withinTolerance: json['within_tolerance'] as bool?,
      issues: issues is List
          ? issues.map((e) => e.toString()).toList()
          : const [],
    );
  }

  /// True when there's nothing useful to show (no PO on the invoice).
  bool get isNoPo => status == 'no_po';

  String get statusLabel => switch (status) {
        'matched' => 'Matched',
        'mismatch' => 'Mismatch',
        'partial' => 'Partial',
        _ => 'No PO',
      };
}

class Invoice {
  final String id;
  final String? invoiceNumber;
  final String? vendorName;
  final double? amount;
  final String? currency;
  final InvoiceStatus status;
  final DateTime? invoiceDate;
  final DateTime? dueDate;
  final String? description;
  final String? poNumber;
  final String? glAccount;
  final String? fileUrl;
  final DateTime createdAt;
  final List<InvoiceWarning> warnings;
  final PoMatch? poMatch;

  Invoice({
    required this.id,
    this.invoiceNumber,
    this.vendorName,
    this.amount,
    this.currency,
    required this.status,
    this.invoiceDate,
    this.dueDate,
    this.description,
    this.poNumber,
    this.glAccount,
    this.fileUrl,
    required this.createdAt,
    this.warnings = const [],
    this.poMatch,
  });

  factory Invoice.fromJson(Map<String, dynamic> json) {
    final rawWarnings = json['warnings'];
    final rawPoMatch = json['po_match'];
    return Invoice(
      id: json['id'] as String,
      invoiceNumber: json['invoice_number'] as String?,
      vendorName: (json['vendor_name'] ?? json['vendor']) as String?,
      amount: (json['amount'] as num?)?.toDouble(),
      currency: json['currency'] as String?,
      status: InvoiceStatus.fromString(json['status'] as String),
      invoiceDate: json['invoice_date'] != null
          ? DateTime.parse(json['invoice_date'] as String)
          : null,
      dueDate: json['due_date'] != null
          ? DateTime.parse(json['due_date'] as String)
          : null,
      description: json['description'] as String?,
      poNumber: json['po_number'] as String?,
      glAccount: json['gl_account'] as String?,
      fileUrl: json['file_url'] as String?,
      createdAt: DateTime.parse(json['created_at'] as String),
      warnings: rawWarnings is List
          ? rawWarnings
              .whereType<Map<String, dynamic>>()
              .map(InvoiceWarning.fromJson)
              .toList()
          : const [],
      poMatch: rawPoMatch is Map<String, dynamic>
          ? PoMatch.fromJson(rawPoMatch)
          : null,
    );
  }
}
