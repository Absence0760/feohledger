import 'package:feohledger_mobile/models/action_verdict.dart';
import 'package:feohledger_mobile/models/payment.dart';

/// Convert a JSON money value (the backend emits these dict responses with
/// `float(...)`, so they arrive as JSON numbers) into a display string WITHOUT
/// doing any client-side float arithmetic on it. The string is what we render;
/// we never add / multiply money on the device — server-computed totals
/// (`total_amount`) are used for sums. Mirrors the web app's "money as
/// string-Decimal, never client float math" invariant.
String moneyToDisplay(Object? raw) {
  if (raw == null) return '0';
  if (raw is String) return raw;
  // num -> string verbatim; we only ever display this, never compute with it.
  return raw.toString();
}

/// One approved invoice awaiting payment, from `GET /api/payments/queue`.
class PaymentQueueItem {
  final String id;
  final String invoiceNumber;
  final String vendorName;

  /// Display string for the invoice amount. Never used in arithmetic.
  final String amountDisplay;

  /// What [amountDisplay] is denominated in — this invoice's OWN currency, not
  /// the org's reporting currency (the queue rollup below carries that one).
  /// `null` when the row carries no usable code, in which case the figure
  /// renders bare rather than wearing a substituted symbol.
  final String? currency;
  final DateTime? dueDate;
  final String? paymentTerms;
  final String status;
  final bool isOverdue;
  final bool discountEligible;
  final DateTime? discountDate;

  /// Display string for the discount amount, null when not eligible.
  final String? discountAmountDisplay;

  /// A payment run would refuse this invoice on EVERY rail — an unresolved
  /// payment-blocking exception, or credit memos covering it in full. Its
  /// checkbox is disabled: selecting it would 409 the whole batch.
  final bool blocked;

  /// Why a run would refuse — or constrain — this row. A stable, PII-free
  /// CODE from the backend's fixed vocabulary (a `PAYMENT_BLOCKING_EXCEPTION_TYPES`
  /// member, or one of `services/payment_runs`' own reason constants), never a
  /// row's description, which can carry vendor / bank / amount detail. Render
  /// it through the screen's label map, never raw.
  ///
  /// Populated for a [requiredMethodCode] row too, so the UI can say WHY the
  /// rail is pinned.
  final String? blockedReason;

  /// The RAW rail code a run would accept for this invoice, and only this one
  /// — today a live virtual card already claims it, and `virtual_card`
  /// converges onto that card instead of opening a second outflow. Not
  /// blocked: the row stays selectable, pinned to this rail.
  ///
  /// Kept as the raw string the backend sent; [requiredMethod] is the strict
  /// parse, because a code this build can't name must not be guessed.
  final String? requiredMethodCode;

  /// The coded refusal `POST /api/payments/record-outside` would answer for
  /// this row whoever records it (`external_payment_*`), or `null` when
  /// "Record as paid" may be offered. The server computes it from the gates
  /// that endpoint applies (`external_payment.record_outside_refusals`), so
  /// the app reads it rather than restating them — and deliberately NOT
  /// [blocked], which is the RUN builder's verdict (a run refuses an
  /// unverified vendor; recording money already sent does not, #566).
  /// Segregation of duties is not folded in: it depends on who records, so the
  /// server's 403 states it.
  final String? recordOutsideRefusal;

  /// What recording would book: the amount owed today, net of applied credit
  /// memos and an accepted early-payment discount still open
  /// (`payment_runs.payable_amounts`). Display only — the server binds the
  /// recorded figure itself. `null` from a backend that predates the field.
  final String? payableAmountDisplay;

  PaymentQueueItem({
    required this.id,
    required this.invoiceNumber,
    required this.vendorName,
    required this.amountDisplay,
    this.currency,
    this.dueDate,
    this.paymentTerms,
    required this.status,
    required this.isOverdue,
    required this.discountEligible,
    this.discountDate,
    this.discountAmountDisplay,
    this.blocked = false,
    this.blockedReason,
    this.requiredMethodCode,
    this.recordOutsideRefusal,
    this.payableAmountDisplay,
  });

  /// The single rail this row may be paid on, or `null` for "any".
  ///
  /// Strict: an unrecognised code resolves to `null` here and turns the row
  /// UNSELECTABLE via [isSelectable], rather than falling back to ACH — which
  /// would stage the run on a rail the backend has just said it refuses. This
  /// is the same fail-closed direction the backend takes when it can no longer
  /// name one converging rail (`CARD_CLAIM_ONLY_METHOD = None`, at which point
  /// it stops sending `required_method` and marks the row blocked outright).
  PaymentMethod? get requiredMethod {
    final code = requiredMethodCode;
    if (code == null || code.isEmpty) return null;
    return PaymentMethod.tryFromString(code);
  }

  /// The backend pinned a rail this build has no name for. Not blocked
  /// server-side, but this client cannot honour the pin, so it must not offer
  /// the row.
  bool get hasUnknownRequiredMethod =>
      requiredMethodCode != null &&
      requiredMethodCode!.isNotEmpty &&
      requiredMethod == null;

  /// May this row be put into a draft run? The one predicate the selection
  /// path reads — so "blocked" and "pinned to a rail we can't name" can never
  /// diverge between the store's guard and the screen's checkbox.
  bool get isSelectable => !blocked && !hasUnknownRequiredMethod;

  factory PaymentQueueItem.fromJson(Map<String, dynamic> json) {
    DateTime? parseDate(Object? v) {
      if (v is String && v.isNotEmpty) return DateTime.tryParse(v);
      return null;
    }

    return PaymentQueueItem(
      id: json['id'] as String,
      invoiceNumber: json['invoice_number'] as String? ?? '',
      vendorName: json['vendor_name'] as String? ?? 'Unknown',
      amountDisplay: moneyToDisplay(json['amount']),
      currency: json['currency'] as String?,
      dueDate: parseDate(json['due_date']),
      paymentTerms: json['payment_terms'] as String?,
      status: json['status'] as String? ?? 'approved',
      isOverdue: json['is_overdue'] as bool? ?? false,
      discountEligible: json['discount_eligible'] as bool? ?? false,
      discountDate: parseDate(json['discount_date']),
      discountAmountDisplay: json['discount_amount'] == null
          ? null
          : moneyToDisplay(json['discount_amount']),
      // Absent / non-`true` reads as NOT blocked — byte-for-byte the behaviour
      // before these fields existed, so an older backend degrades to the
      // server-side gate (a 409) rather than to an unusable queue.
      blocked: json['blocked'] == true,
      blockedReason: json['blocked_reason'] as String?,
      requiredMethodCode: json['required_method'] as String?,
      recordOutsideRefusal: json['record_outside_refusal'] as String?,
      payableAmountDisplay: json['payable_amount'] == null
          ? null
          : moneyToDisplay(json['payable_amount']),
    );
  }
}

/// Payment-page KPI bar, from `GET /api/payments/summary`. Each value is a
/// server-computed display string; the device never sums money.
class PaymentSummary {
  final String totalPaidDisplay;
  final String totalPendingDisplay;
  final int paymentCount;
  final String totalRebatesDisplay;
  final int queueCount;

  /// What the three money figures above are denominated in — the org's
  /// REPORTING currency, which `/payments/summary` resolves and then names in
  /// its own response (`"currency": reporting_currency`) because none of these
  /// totals belongs to a single row. This is the authoritative answer for this
  /// KPI bar and outranks `OrgCurrencyStore`, which resolves the same rungs
  /// client-side for the payloads that do NOT name one.
  final String? currency;

  PaymentSummary({
    required this.totalPaidDisplay,
    required this.totalPendingDisplay,
    required this.paymentCount,
    required this.totalRebatesDisplay,
    required this.queueCount,
    this.currency,
  });

  factory PaymentSummary.fromJson(Map<String, dynamic> json) {
    return PaymentSummary(
      totalPaidDisplay: moneyToDisplay(json['total_paid']),
      totalPendingDisplay: moneyToDisplay(json['total_pending']),
      paymentCount: json['payment_count'] as int? ?? 0,
      totalRebatesDisplay: moneyToDisplay(json['total_rebates']),
      queueCount: json['queue_count'] as int? ?? 0,
      currency: json['currency'] as String?,
    );
  }
}

/// A batch from `GET /api/payments/runs/`.
///
/// **[totalAmountDisplay] is denominated in [currency], and the server is what
/// proves it.** `payment_runs.total_amount` is a plain `SUM(Payment.amount)`
/// with no currency column beside it — but `services/payment_runs` refuses to
/// create a run spanning more than one currency, so the legs agree, and
/// `services/payment_runs.py::one_currency` derives the code from them for the runs
/// list and the run detail alike.
///
/// A `null` therefore means the code could not be PROVEN — a run with no
/// payments, one whose invoices carry no currency, or a legacy run predating
/// that guard whose legs disagree — never that the client did not ask. Such a
/// total is denominated in nothing real, so it renders bare rather than
/// wearing the org's reporting currency, which would dress a mixed sum up as a
/// genuine figure (`docs/decisions.md` §79/§82, §160).
class PaymentRun {
  final String id;
  final String status;
  final String totalAmountDisplay;

  /// The ISO code [totalAmountDisplay] is in, or `null` when the server could
  /// not prove one — see the class note. Not a licence to substitute a default.
  final String? currency;
  final int paymentCount;
  final bool requiresCfoApproval;
  final bool cfoApproved;
  final DateTime createdAt;
  final DateTime? executedAt;

  PaymentRun({
    required this.id,
    required this.status,
    required this.totalAmountDisplay,
    this.currency,
    required this.paymentCount,
    required this.requiresCfoApproval,
    required this.cfoApproved,
    required this.createdAt,
    this.executedAt,
  });

  factory PaymentRun.fromJson(Map<String, dynamic> json) {
    DateTime parseRequired(Object? v) =>
        v is String ? DateTime.parse(v) : DateTime.now();
    DateTime? parseOptional(Object? v) =>
        v is String && v.isNotEmpty ? DateTime.tryParse(v) : null;

    return PaymentRun(
      id: json['id'] as String,
      status: json['status'] as String? ?? 'draft',
      totalAmountDisplay: moneyToDisplay(json['total_amount']),
      currency: json['currency'] as String?,
      paymentCount: (json['payment_count'] as int?) ??
          (json['payment_count'] as num?)?.toInt() ??
          0,
      requiresCfoApproval: json['requires_cfo_approval'] as bool? ?? false,
      cfoApproved: json['cfo_approved_at'] != null,
      createdAt: parseRequired(json['created_at']),
      executedAt: parseOptional(json['executed_at']),
    );
  }

  /// Only `draft` runs can be executed (or cancelled) from the app.
  bool get isExecutable => status == 'draft';
}

/// `GET /api/payments/execution-mode`'s `mode`: whether FeohLedger SENDS this
/// tenant's payments (`processor`) or only RECORDS payments the customer made
/// from its own bank or ERP (`record_only`) — the no-rail pilot, backend
/// decisions §251. The server resolves it (`services/payment_execution_mode`)
/// and every dispatching endpoint enforces it; the app only mirrors it so a
/// record-only tenant is never offered a control that can only 409.
const kExecutionModeProcessor = 'processor';
const kExecutionModeRecordOnly = 'record_only';

/// Whether the app may offer the controls that SEND money (Execute).
///
/// **Fails closed**, like the web `canDispatchPayments`: an unknown mode —
/// still loading, the read failed, offline, or a value this build predates —
/// reads as record-only, so the worst a slow or failed read costs is a hidden
/// button, never a send control on a tenant whose server would refuse it.
/// Recording is offered in both modes, so nothing is stranded.
bool canDispatchPayments(String? mode) => mode == kExecutionModeProcessor;

/// The rails a payment made outside FeohLedger can be recorded on — every
/// rail except `virtual_card`, which is FeohLedger's own card programme and
/// never a way to pay around it (`schemas/payment.py::
/// RecordErpReportedPaymentRequest._not_card`). Raw codes, in the web
/// dialog's order (`EXTERNAL_PAYMENT_METHODS`); [PaymentMethod] is the run
/// builder's set and deliberately not widened, so the run dropdown is
/// unchanged.
const kExternalPaymentMethods = <String>[
  'ach',
  'wire',
  'check',
  'bacs',
  'faster_payments',
  'chaps',
  'card',
];

/// A run's detail, as far as recording it outside FeohLedger needs:
/// `GET /api/payments/runs/{id}`. [recordOutside] is the CALLER's verdict on
/// `POST /runs/{id}/record-outside` (`run_action_refusals`, from the gate the
/// endpoint raises from — issue #575), so the sheet offers the control from it
/// and never restates maker-checker, CFO sign-off or the run's status.
class PaymentRunRecordDetail {
  final PaymentRun run;
  final ActionVerdict recordOutside;

  const PaymentRunRecordDetail({required this.run, required this.recordOutside});

  factory PaymentRunRecordDetail.fromJson(Map<String, dynamic> json) {
    final actions = json['actions'];
    return PaymentRunRecordDetail(
      run: PaymentRun.fromJson(json),
      recordOutside: actions is Map
          ? ActionVerdict.fromVerdicts(
              Map<String, dynamic>.from(actions), 'record_outside')
          : ActionVerdict.unanswered,
    );
  }
}

/// Selection passed to `POST /api/payments/runs` — one invoice + chosen method.
class PaymentRunSelection {
  final String invoiceId;
  final PaymentMethod method;

  const PaymentRunSelection({required this.invoiceId, required this.method});

  Map<String, dynamic> toJson() => {
        'invoice_id': invoiceId,
        'method': method.value,
      };
}
