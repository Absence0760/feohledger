enum PaymentMethod {
  ach('ach'),
  wire('wire'),
  check('check'),
  virtualCard('virtual_card');

  const PaymentMethod(this.value);
  final String value;

  /// Strict parse — `null` for a rail code this build doesn't know.
  ///
  /// Exists because [fromString]'s ACH fallback is only right where the value
  /// is DISPLAYED. Where the rail is a decision (the payment queue's
  /// `required_method`, which names the ONE rail a run will accept for that
  /// invoice), silently reading an unknown code as `ach` would stage the run
  /// on a rail the backend refuses — the 409 the queue exists to prevent. Such
  /// a row is treated as unselectable instead; see
  /// `PaymentQueueItem.isSelectable`.
  static PaymentMethod? tryFromString(String s) {
    for (final e in PaymentMethod.values) {
      if (e.value == s) return e;
    }
    return null;
  }

  static PaymentMethod fromString(String s) =>
      tryFromString(s) ?? PaymentMethod.ach;

  String get label => switch (this) {
    PaymentMethod.ach => 'ACH',
    PaymentMethod.wire => 'Wire',
    PaymentMethod.check => 'Check',
    PaymentMethod.virtualCard => 'Virtual Card',
  };
}

enum PaymentStatus {
  pending('pending'),
  processing('processing'),
  completed('completed'),
  failed('failed'),
  cancelled('cancelled');

  const PaymentStatus(this.value);
  final String value;

  static PaymentStatus fromString(String s) {
    return PaymentStatus.values.firstWhere(
      (e) => e.value == s,
      orElse: () => PaymentStatus.pending,
    );
  }
}

class Payment {
  final String id;
  final String invoiceId;
  final double amount;

  /// What [amount] — the AUTHORIZED figure — is denominated in. `payments` has
  /// no currency column; a payment settles in its invoice's currency, and
  /// `PaymentResponse.currency` joins it through for exactly this reason.
  /// `null` means the invoice carries no code or none was joined, and is NOT
  /// a licence to substitute a default (`docs/decisions.md` §79/§82).
  final String? currency;

  final PaymentMethod method;
  final PaymentStatus status;
  final String? reference;
  final DateTime createdAt;

  Payment({
    required this.id,
    required this.invoiceId,
    required this.amount,
    this.currency,
    required this.method,
    required this.status,
    this.reference,
    required this.createdAt,
  });

  factory Payment.fromJson(Map<String, dynamic> json) {
    return Payment(
      id: json['id'] as String,
      invoiceId: json['invoice_id'] as String,
      amount: (json['amount'] as num).toDouble(),
      currency: json['currency'] as String?,
      method: PaymentMethod.fromString(json['method'] as String),
      status: PaymentStatus.fromString(json['status'] as String),
      reference: json['reference'] as String?,
      createdAt: DateTime.parse(json['created_at'] as String),
    );
  }
}

class DashboardData {
  final int totalInvoices;

  /// The whole book, rolled up into the org's reporting currency — the
  /// `reporting.total_amount` the backend computes from each row's rate-locked
  /// `reporting_amount`, NOT the sibling top-level `total_amount`, which is a
  /// naive `SUM(Invoice.amount)` across currencies.
  ///
  /// That distinction is what makes [reportingCurrency] sayable at all: a
  /// mixed-currency sum is a quantity in no currency, so labelling the naive
  /// figure with any code — a `$`, or the org's resolved one — would be a
  /// wrong number wearing a right-looking symbol. Falls back to the naive key
  /// only for a backend predating the `reporting` block, in which case
  /// [reportingCurrency] is null and the figure renders with no symbol.
  final double totalAmount;
  final Map<String, int> pipeline;
  final List<VendorSpend> topVendors;
  final AgingReport aging;
  final List<MonthlyTrend> trends;
  final UpcomingPayments upcoming;

  /// What every money figure on this payload is denominated in, straight from
  /// `reporting.reporting_currency` — the server's own answer, so no client
  /// resolution is involved and there is nothing to disagree with it about.
  /// `null` against a backend with no `reporting` block.
  final String? reportingCurrency;

  /// How many invoices behind [totalAmount] had no exchange rate into
  /// [reportingCurrency] and were added at FACE value instead —
  /// `reporting.unconverted_count`. Non-zero means the total mixes currencies
  /// by that many rows, and the screen has to say so beside it: a fallback
  /// nobody reports is just a wrong number (`docs/decisions.md` §35). `0`
  /// against a backend with no `reporting` block, whose figures render bare.
  final int unconvertedCount;

  DashboardData({
    required this.totalInvoices,
    required this.totalAmount,
    required this.pipeline,
    required this.topVendors,
    required this.aging,
    required this.trends,
    required this.upcoming,
    this.reportingCurrency,
    this.unconvertedCount = 0,
  });

  factory DashboardData.fromJson(Map<String, dynamic> json) {
    // upcoming_payments is a list of invoices from the API. The total is
    // NOT folded from that list on-device (summing already-lossy per-item
    // floats client-side can produce visible rounding artifacts, e.g. a
    // total ending in `.99999`, and drifts further as more amounts
    // accumulate) — it's a separate server-computed aggregate
    // (`upcoming_total_amount`, summed in Decimal on the backend and
    // converted to float exactly once), mirroring the payment-queue and
    // cash-flow surfaces' "server-supplied total, never client float math"
    // convention.
    final upcomingRaw = json['upcoming_payments'];
    final upcomingList = upcomingRaw is List ? upcomingRaw : [];
    // Every money figure below comes from the reporting-currency counterpart
    // the backend serves beside the face-value one, because only those are in
    // the single currency `reporting_currency` names. `vendor_spend[].amount`
    // is already rolled up at source and needs no `_reporting` sibling.
    final reporting = (json['reporting'] as Map<String, dynamic>?) ?? const {};

    return DashboardData(
      totalInvoices: json['total_invoices'] as int? ?? 0,
      totalAmount: ((reporting['total_amount'] ?? json['total_amount']) as num?)
              ?.toDouble() ??
          0,
      pipeline: (json['pipeline'] as Map<String, dynamic>?)
              ?.map((k, v) => MapEntry(k, v as int)) ??
          {},
      topVendors: (json['vendor_spend'] as List<dynamic>?)
              ?.map((v) => VendorSpend.fromJson(v as Map<String, dynamic>))
              .toList() ??
          [],
      aging: AgingReport.fromJson(
        (json['aging_reporting'] ?? json['aging']) as Map<String, dynamic>? ??
            {},
      ),
      trends: (json['monthly_trend'] as List<dynamic>?)
              ?.map((t) => MonthlyTrend.fromJson(t as Map<String, dynamic>))
              .toList() ??
          [],
      upcoming: UpcomingPayments(
        count: upcomingList.length,
        totalAmount: ((json['upcoming_total_amount_reporting'] ??
                    json['upcoming_total_amount']) as num?)
                ?.toDouble() ??
            0,
        unconvertedCount: unconvertedCountOf(json['upcoming_unconverted_count']),
      ),
      reportingCurrency: reporting['reporting_currency'] as String?,
      unconvertedCount: unconvertedCountOf(reporting['unconverted_count']),
    );
  }
}

class VendorSpend {
  final String vendorName;
  final double totalAmount;
  final int invoiceCount;

  /// This vendor's invoices that had no exchange rate into the reporting
  /// currency and were added to [totalAmount] at FACE value
  /// (`vendor_spend[].unconverted_count`). Non-zero means this row is ranked
  /// on a figure that is not in the same currency as its neighbours'.
  final int unconvertedCount;

  VendorSpend({
    required this.vendorName,
    required this.totalAmount,
    required this.invoiceCount,
    this.unconvertedCount = 0,
  });

  factory VendorSpend.fromJson(Map<String, dynamic> json) {
    return VendorSpend(
      vendorName: (json['vendor'] ?? json['vendor_name']) as String? ?? 'Unknown',
      totalAmount: ((json['amount'] ?? json['total_amount']) as num?)?.toDouble() ?? 0,
      invoiceCount: json['invoice_count'] as int? ?? 0,
      unconvertedCount: unconvertedCountOf(json['unconverted_count']),
    );
  }
}

class AgingReport {
  final double current;
  final double thirtyDays;
  final double sixtyDays;
  final double ninetyPlus;

  /// ONE count for the whole band set (`aging_reporting.unconverted_count`):
  /// open invoices with no exchange rate into the reporting currency, added to
  /// whichever band they fell in at FACE value. The backend deliberately
  /// serves one figure rather than five — "some of these bands mix currencies"
  /// is the actionable fact either way. The legacy face-value `aging` carries
  /// none (it is a cross-currency sum in its entirety), so it parses as `0`.
  final int unconvertedCount;

  AgingReport({
    required this.current,
    required this.thirtyDays,
    required this.sixtyDays,
    required this.ninetyPlus,
    this.unconvertedCount = 0,
  });

  factory AgingReport.fromJson(Map<String, dynamic> json) {
    return AgingReport(
      current: (json['current'] as num?)?.toDouble() ?? 0,
      thirtyDays: ((json['days_30'] ?? json['30_days']) as num?)?.toDouble() ?? 0,
      sixtyDays: ((json['days_60'] ?? json['60_days']) as num?)?.toDouble() ?? 0,
      ninetyPlus: ((json['days_90_plus'] ?? json['90_plus']) as num?)?.toDouble() ?? 0,
      unconvertedCount: unconvertedCountOf(json['unconverted_count']),
    );
  }
}

class MonthlyTrend {
  final String month;
  final int count;
  final double amount;

  MonthlyTrend({
    required this.month,
    required this.count,
    required this.amount,
  });

  factory MonthlyTrend.fromJson(Map<String, dynamic> json) {
    return MonthlyTrend(
      month: json['month'] as String? ?? '',
      count: json['count'] as int? ?? 0,
      // Reporting-currency counterpart, for the reason `DashboardData.
      // totalAmount` records: the bare `amount` is a per-month naive sum
      // across currencies, so the trend's own bars would not be comparable
      // with each other, let alone with the `reporting_currency` label.
      amount: ((json['reporting_amount'] ?? json['amount']) as num?)
              ?.toDouble() ??
          0,
    );
  }
}

class UpcomingPayments {
  final int count;
  final double totalAmount;

  /// Of the invoices behind [totalAmount], how many had no exchange rate into
  /// the reporting currency and were added at FACE value
  /// (`upcoming_unconverted_count`) — dropping them would understate what is
  /// due, so the backend keeps them in and counts them instead.
  final int unconvertedCount;

  UpcomingPayments({
    required this.count,
    required this.totalAmount,
    this.unconvertedCount = 0,
  });

  factory UpcomingPayments.fromJson(Map<String, dynamic> json) {
    return UpcomingPayments(
      count: json['count'] as int? ?? 0,
      totalAmount: (json['total_amount'] as num?)?.toDouble() ?? 0,
      unconvertedCount: unconvertedCountOf(json['unconverted_count']),
    );
  }
}

/// A row COUNT off a rollup payload's `unconverted_count`, never money.
///
/// Absent or `null` reads as `0` — a backend predating the field has nothing
/// to disclose, and a disclosure that renders on a missing key would claim a
/// part-converted figure nobody reported. A JSON number of either width is
/// accepted; anything else is not a count and also reads as `0`.
int unconvertedCountOf(Object? raw) => raw is num ? raw.toInt() : 0;
