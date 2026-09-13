/// Adaptive-workflow read models — approval patterns, baseline anomalies and
/// advisory suggestions.
///
/// Mirrors `backend/app/schemas/adaptive_workflows.py`. Three things about the
/// wire shape drive how this file is written:
///
/// * **Every statistic arrives as a STRING**, money and percentage alike — the
///   backend stringifies its `Decimal`s rather than floating them across the
///   boundary. They are carried as strings and rendered verbatim; the device
///   never parses or recomputes one. (The web page does the same.)
/// * **Per-vendor money figures carry no currency code.** They are denominated
///   in the org's reporting currency, which this payload does not name. The
///   screen therefore formats them against `OrgCurrencyStore`, which resolves
///   the SAME three settings rungs the backend used to denominate them, and
///   falls back to naming the denomination in a section note when the org
///   declares none usable — never to a `$`, which on a ZAR org's averages
///   would be a lie the API never told.
///   The anomaly rows are the exception: they carry their own `amount_currency`,
///   because `detect_invoice_anomaly` falls back to the BILLED figure when an
///   invoice has no usable rate lock, so each row labels itself.
/// * **Everything is advisory and deterministic** — statistics over the
///   tenant's own approval history, no model, recomputed on read. A suggestion
///   is a proposal; nothing here has changed a workflow.
library;

/// One approver's decision record. `approverName` is joined from the control
/// plane and may be null for a decision whose user row is gone.
class ApproverPattern {
  final String approverId;
  final String? approverName;
  final int approvedCount;
  final int rejectedCount;
  final String approvalRatePct;
  final String medianTimeToApproveDays;
  final String avgTimeToApproveDays;
  final int sampleSize;

  const ApproverPattern({
    required this.approverId,
    this.approverName,
    required this.approvedCount,
    required this.rejectedCount,
    required this.approvalRatePct,
    required this.medianTimeToApproveDays,
    required this.avgTimeToApproveDays,
    required this.sampleSize,
  });

  factory ApproverPattern.fromJson(Map<String, dynamic> json) {
    return ApproverPattern(
      approverId: json['approver_id'] as String? ?? '',
      approverName: json['approver_name'] as String?,
      approvedCount: (json['approved_count'] as num?)?.toInt() ?? 0,
      rejectedCount: (json['rejected_count'] as num?)?.toInt() ?? 0,
      approvalRatePct: json['approval_rate_pct']?.toString() ?? '0',
      medianTimeToApproveDays:
          json['median_time_to_approve_days']?.toString() ?? '0',
      avgTimeToApproveDays: json['avg_time_to_approve_days']?.toString() ?? '0',
      sampleSize: (json['sample_size'] as num?)?.toInt() ?? 0,
    );
  }
}

/// One vendor's approval record. The money figures are in the org's reporting
/// currency.
///
/// The response also carries `min_approved_amount` / `max_approved_amount`. The
/// phone row shows the median and the average — the two figures that describe
/// the vendor's norm, which is what this screen is for — and the range is left
/// to the web table rather than crammed into a list tile.
class VendorPattern {
  final String? vendorId;
  final String vendorName;
  final int approvedCount;
  final int rejectedCount;
  final String approvalRatePct;
  final int unmodifiedCount;
  final String consistencyPct;
  final String avgApprovedAmount;
  final String medianApprovedAmount;
  final int sampleSize;

  /// Approvals whose amount could NOT be expressed in the reporting currency.
  /// They are excluded from the money figures but counted in [sampleSize], so a
  /// reader handed an average beside a sample count is reading an average over
  /// fewer rows than the count claims unless this is disclosed. The backend
  /// emits it for exactly that disclosure; the screen renders it when non-zero.
  final int unconvertedCount;

  const VendorPattern({
    this.vendorId,
    required this.vendorName,
    required this.approvedCount,
    required this.rejectedCount,
    required this.approvalRatePct,
    required this.unmodifiedCount,
    required this.consistencyPct,
    required this.avgApprovedAmount,
    required this.medianApprovedAmount,
    required this.sampleSize,
    required this.unconvertedCount,
  });

  factory VendorPattern.fromJson(Map<String, dynamic> json) {
    return VendorPattern(
      vendorId: json['vendor_id'] as String?,
      vendorName: json['vendor_name'] as String? ?? '',
      approvedCount: (json['approved_count'] as num?)?.toInt() ?? 0,
      rejectedCount: (json['rejected_count'] as num?)?.toInt() ?? 0,
      approvalRatePct: json['approval_rate_pct']?.toString() ?? '0',
      unmodifiedCount: (json['unmodified_count'] as num?)?.toInt() ?? 0,
      consistencyPct: json['consistency_pct']?.toString() ?? '0',
      avgApprovedAmount: json['avg_approved_amount']?.toString() ?? '0',
      medianApprovedAmount: json['median_approved_amount']?.toString() ?? '0',
      sampleSize: (json['sample_size'] as num?)?.toInt() ?? 0,
      unconvertedCount: (json['unconverted_count'] as num?)?.toInt() ?? 0,
    );
  }
}

/// `GET /api/adaptive/approval-patterns`.
class ApprovalPatterns {
  final int lookbackDays;
  final List<ApproverPattern> approvers;
  final List<VendorPattern> vendors;

  const ApprovalPatterns({
    required this.lookbackDays,
    required this.approvers,
    required this.vendors,
  });

  factory ApprovalPatterns.fromJson(Map<String, dynamic> json) {
    return ApprovalPatterns(
      lookbackDays: (json['lookback_days'] as num?)?.toInt() ?? 0,
      approvers: ((json['approvers'] as List?) ?? const [])
          .map((e) => ApproverPattern.fromJson(e as Map<String, dynamic>))
          .toList(),
      vendors: ((json['vendors'] as List?) ?? const [])
          .map((e) => VendorPattern.fromJson(e as Map<String, dynamic>))
          .toList(),
    );
  }
}

/// One reason an invoice stands out from its vendor's baseline. `message` is a
/// server-composed English sentence (the same one the web renders) — the screen
/// shows it verbatim rather than rebuilding it from `observed` / `expected`.
class AnomalyFlag {
  final String code;
  final String severity;
  final String message;
  final String observed;
  final String expected;

  const AnomalyFlag({
    required this.code,
    required this.severity,
    required this.message,
    required this.observed,
    required this.expected,
  });

  factory AnomalyFlag.fromJson(Map<String, dynamic> json) {
    return AnomalyFlag(
      code: json['code'] as String? ?? '',
      severity: json['severity'] as String? ?? 'info',
      message: json['message'] as String? ?? '',
      observed: json['observed']?.toString() ?? '',
      expected: json['expected']?.toString() ?? '',
    );
  }
}

/// One flagged in-review invoice.
///
/// The response also carries the per-vendor `baseline` block the web's
/// explainability panel expands. It is deliberately not parsed here: the phone
/// screen shows the flags (each of which already states observed-vs-expected in
/// its own sentence) and links through to the invoice, so a baseline field no
/// surface renders would be a shape to keep in sync for nothing.
class InvoiceAnomaly {
  final String invoiceId;
  final String? vendorId;
  final String vendorName;
  final String amount;

  /// What [amount] is denominated in — normally the org's reporting currency,
  /// but the invoice's OWN currency when it carried no usable rate lock and the
  /// backend fell back to the billed figure. Without it a client would stamp the
  /// reporting currency onto a number that is not in it.
  final String amountCurrency;
  final bool insufficientHistory;
  final List<AnomalyFlag> flags;

  const InvoiceAnomaly({
    required this.invoiceId,
    this.vendorId,
    required this.vendorName,
    required this.amount,
    required this.amountCurrency,
    required this.insufficientHistory,
    required this.flags,
  });

  factory InvoiceAnomaly.fromJson(Map<String, dynamic> json) {
    return InvoiceAnomaly(
      invoiceId: json['invoice_id'] as String? ?? '',
      vendorId: json['vendor_id'] as String?,
      vendorName: json['vendor_name'] as String? ?? '',
      amount: json['amount']?.toString() ?? '0',
      amountCurrency: json['amount_currency'] as String? ?? '',
      insufficientHistory: json['insufficient_history'] as bool? ?? false,
      flags: ((json['flags'] as List?) ?? const [])
          .map((e) => AnomalyFlag.fromJson(e as Map<String, dynamic>))
          .toList(),
    );
  }
}

/// `GET /api/adaptive/anomalies` in batch mode. [totalScanned] is how many
/// in-review invoices were examined, which is what makes an empty [flagged]
/// list mean "nothing stands out" rather than "nothing was looked at".
class AnomalyBatch {
  final int totalScanned;
  final List<InvoiceAnomaly> flagged;

  const AnomalyBatch({required this.totalScanned, required this.flagged});

  factory AnomalyBatch.fromJson(Map<String, dynamic> json) {
    return AnomalyBatch(
      totalScanned: (json['total_scanned'] as num?)?.toInt() ?? 0,
      flagged: ((json['flagged'] as List?) ?? const [])
          .map((e) => InvoiceAnomaly.fromJson(e as Map<String, dynamic>))
          .toList(),
    );
  }
}

/// An advisory suggestion's lifecycle state.
///
/// `open` is live, `dismissed` survives recomputation (that is the whole point
/// of persisting the row), `stale` is one whose condition stopped holding, and
/// `applied` is reserved for the audited apply path — which mobile does not
/// offer. [unknown] keeps a future status from being mis-rendered as open.
enum SuggestionStatus {
  open('open'),
  dismissed('dismissed'),
  applied('applied'),
  stale('stale'),
  unknown('');

  const SuggestionStatus(this.value);

  final String value;

  static SuggestionStatus fromString(String? raw) {
    for (final s in SuggestionStatus.values) {
      if (s != unknown && s.value == raw) return s;
    }
    return unknown;
  }
}

/// One advisory suggestion.
///
/// [title] and [rationale] are sentences the backend composes, numbers and
/// currency code included — they are rendered verbatim (and are therefore
/// English, on mobile exactly as on the web). The `payload` block behind them
/// (`suggested_threshold`, `based_on_n`, …) is what an *apply* would act on, and
/// mobile never applies, so it is not parsed.
class WorkflowSuggestion {
  final String id;
  final String kind;
  final String? vendorId;
  final String vendorName;
  final String title;
  final String? rationale;
  final String confidencePct;
  final SuggestionStatus status;
  final DateTime? createdAt;

  const WorkflowSuggestion({
    required this.id,
    required this.kind,
    this.vendorId,
    required this.vendorName,
    required this.title,
    this.rationale,
    required this.confidencePct,
    required this.status,
    this.createdAt,
  });

  bool get isOpen => status == SuggestionStatus.open;

  factory WorkflowSuggestion.fromJson(Map<String, dynamic> json) {
    return WorkflowSuggestion(
      id: json['id'] as String? ?? '',
      kind: json['kind'] as String? ?? '',
      vendorId: json['vendor_id'] as String?,
      vendorName: json['vendor_name'] as String? ?? '',
      title: json['title'] as String? ?? '',
      rationale: json['rationale'] as String?,
      confidencePct: json['confidence_pct']?.toString() ?? '0',
      status: SuggestionStatus.fromString(json['status'] as String?),
      createdAt: DateTime.tryParse(json['created_at'] as String? ?? ''),
    );
  }
}
