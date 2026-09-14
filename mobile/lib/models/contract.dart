enum ContractStatus {
  draft('draft'),
  active('active'),
  expired('expired'),
  terminated('terminated'),
  cancelled('cancelled');

  const ContractStatus(this.value);
  final String value;

  static ContractStatus fromString(String s) {
    return ContractStatus.values.firstWhere(
      (e) => e.value == s,
      orElse: () => ContractStatus.draft,
    );
  }

  String get label => switch (this) {
    ContractStatus.draft => 'Draft',
    ContractStatus.active => 'Active',
    ContractStatus.expired => 'Expired',
    ContractStatus.terminated => 'Terminated',
    ContractStatus.cancelled => 'Cancelled',
  };

  /// Lifecycle actions are only offered while the contract can still change
  /// state from the field. Terminal states (expired / terminated / cancelled)
  /// expose no actions.
  bool get isActionable =>
      this == ContractStatus.draft || this == ContractStatus.active;
}

enum ContractType {
  purchase('purchase'),
  service('service'),
  subscription('subscription'),
  lease('lease'),
  sla('sla'),
  msa('msa'),
  sow('sow'),
  other('other');

  const ContractType(this.value);
  final String value;

  static ContractType fromString(String s) {
    return ContractType.values.firstWhere(
      (e) => e.value == s,
      orElse: () => ContractType.other,
    );
  }

  String get label => switch (this) {
    ContractType.purchase => 'Purchase',
    ContractType.service => 'Service',
    ContractType.subscription => 'Subscription',
    ContractType.lease => 'Lease',
    ContractType.sla => 'SLA',
    ContractType.msa => 'MSA',
    ContractType.sow => 'SOW',
    ContractType.other => 'Other',
  };
}

class ContractLineItem {
  final String id;
  final int? lineNumber;
  final String? itemCode;
  final String? description;
  final double? quantity;
  final double? unitPrice;
  final double? total;
  final String? glAccount;

  ContractLineItem({
    required this.id,
    this.lineNumber,
    this.itemCode,
    this.description,
    this.quantity,
    this.unitPrice,
    this.total,
    this.glAccount,
  });

  factory ContractLineItem.fromJson(Map<String, dynamic> json) {
    return ContractLineItem(
      id: json['id'] as String,
      lineNumber: (json['line_number'] as num?)?.toInt(),
      itemCode: json['item_code'] as String?,
      description: json['description'] as String?,
      quantity: (json['quantity'] as num?)?.toDouble(),
      unitPrice: (json['unit_price'] as num?)?.toDouble(),
      total: (json['total'] as num?)?.toDouble(),
      glAccount: json['gl_account'] as String?,
    );
  }
}

/// Spend rolled up against a contract's spend limit. Null when the backend
/// doesn't compute one (e.g. on the list endpoint).
class ContractSpend {
  final double invoicedTotal;
  final int invoiceCount;
  final double? spendLimit;
  final double? remaining;
  final bool overLimit;

  ContractSpend({
    required this.invoicedTotal,
    required this.invoiceCount,
    this.spendLimit,
    this.remaining,
    required this.overLimit,
  });

  factory ContractSpend.fromJson(Map<String, dynamic> json) {
    return ContractSpend(
      invoicedTotal: (json['invoiced_total'] as num?)?.toDouble() ?? 0,
      invoiceCount: (json['invoice_count'] as num?)?.toInt() ?? 0,
      spendLimit: (json['spend_limit'] as num?)?.toDouble(),
      remaining: (json['remaining'] as num?)?.toDouble(),
      overLimit: json['over_limit'] as bool? ?? false,
    );
  }
}

class Contract {
  final String id;
  final String? contractNumber;
  final String? title;
  final String? description;
  final ContractType contractType;
  final ContractStatus status;
  final String? vendorId;
  final String? vendorName;
  final String? currency;
  final double? totalValue;
  final double? spendLimit;
  final bool notToExceed;
  final DateTime? startDate;
  final DateTime? endDate;
  final DateTime? signedDate;
  final bool autoRenew;
  final int? renewalTermMonths;
  final int? renewalNoticeDays;
  final String? paymentTerms;
  final List<ContractLineItem> lineItems;
  final ContractSpend? spend;
  final DateTime createdAt;
  final DateTime? updatedAt;

  Contract({
    required this.id,
    this.contractNumber,
    this.title,
    this.description,
    required this.contractType,
    required this.status,
    this.vendorId,
    this.vendorName,
    this.currency,
    this.totalValue,
    this.spendLimit,
    this.notToExceed = false,
    this.startDate,
    this.endDate,
    this.signedDate,
    this.autoRenew = false,
    this.renewalTermMonths,
    this.renewalNoticeDays,
    this.paymentTerms,
    this.lineItems = const [],
    this.spend,
    required this.createdAt,
    this.updatedAt,
  });

  factory Contract.fromJson(Map<String, dynamic> json) {
    return Contract(
      id: json['id'] as String,
      contractNumber: json['contract_number'] as String?,
      title: json['title'] as String?,
      description: json['description'] as String?,
      contractType: ContractType.fromString(json['contract_type'] as String),
      status: ContractStatus.fromString(json['status'] as String),
      vendorId: json['vendor_id'] as String?,
      vendorName: json['vendor_name'] as String?,
      currency: json['currency'] as String?,
      totalValue: (json['total_value'] as num?)?.toDouble(),
      spendLimit: (json['spend_limit'] as num?)?.toDouble(),
      notToExceed: json['not_to_exceed'] as bool? ?? false,
      startDate: json['start_date'] != null
          ? DateTime.parse(json['start_date'] as String)
          : null,
      endDate: json['end_date'] != null
          ? DateTime.parse(json['end_date'] as String)
          : null,
      signedDate: json['signed_date'] != null
          ? DateTime.parse(json['signed_date'] as String)
          : null,
      autoRenew: json['auto_renew'] as bool? ?? false,
      renewalTermMonths: (json['renewal_term_months'] as num?)?.toInt(),
      renewalNoticeDays: (json['renewal_notice_days'] as num?)?.toInt(),
      paymentTerms: json['payment_terms'] as String?,
      lineItems: (json['line_items'] as List?)
              ?.map((e) =>
                  ContractLineItem.fromJson(e as Map<String, dynamic>))
              .toList() ??
          const [],
      spend: json['spend'] != null
          ? ContractSpend.fromJson(json['spend'] as Map<String, dynamic>)
          : null,
      createdAt: DateTime.parse(json['created_at'] as String),
      updatedAt: json['updated_at'] != null
          ? DateTime.parse(json['updated_at'] as String)
          : null,
    );
  }
}
