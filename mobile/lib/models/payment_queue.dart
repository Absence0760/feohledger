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

  /// What the ERP reported paying on this invoice, when it holds a live
  /// `paid_not_recorded` exception (`api/payments.py::
  /// _queue_erp_reported_payments`) — the record dialog starts from it, as the
  /// web's does. `null` otherwise.
  final QueueErpReportedPayment? erpReportedPayment;

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
    this.erpReportedPayment,
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
      erpReportedPayment:
          QueueErpReportedPayment.fromJson(json['erp_reported_payment']),
    );
  }
}

/// One part of the ERP's breakdown of a reported payment: a rail a payment
/// made outside FeohLedger can be recorded on, or `null` for "not stated",
/// and its exact amount string (never parsed).
class ErpRailPart {
  final String? rail;
  final String amount;
  const ErpRailPart({required this.rail, required this.amount});
}

/// What a "record this payment" form starts from. [method] is `null` when the
/// ERP named no rail a payment made outside FeohLedger can be recorded on, so
/// the person must choose — never a guessed default.
class RecordPrefill {
  final String? method;
  final String reference;
  final DateTime paidOn;
  const RecordPrefill({
    required this.method,
    required this.reference,
    required this.paidOn,
  });
}

/// A queue row's `erp_reported_payment`: the ERP's report on a live
/// `paid_not_recorded` exception, flattened for the record dialog. The mobile
/// mirror of the web's `QueueErpReportedPayment` + `erpPrefill` /
/// `erpPaymentParts` / `spansSeveralRails` (`frontend/src/lib/types/
/// erpReport.ts`), reading the same server fields.
class QueueErpReportedPayment {
  /// The exception whose one-click (`POST /api/exceptions/{id}/
  /// record-erp-payment`) books a split that stands.
  final String exceptionId;

  /// The ERP's product name, or `null` when the adapter has none.
  final String? erpName;
  final String? method;
  final String? reference;

  /// As the server sent it (`YYYY-MM-DD`); [prefill] validates it.
  final String? paidOn;

  /// The cash the ERP states and the code it stated it in. Display only.
  final String? amount;
  final String? amountCurrency;

  /// The breakdown as the server keeps it; empty when there is none or it is
  /// malformed (never a partial list that would look like the whole).
  final List<ErpRailPart> parts;

  /// The server's verdict on [parts] (`erp_status_sync.kept_split_stands`):
  /// true stands, false dropped, null not judged.
  final bool? splitStands;

  const QueueErpReportedPayment({
    required this.exceptionId,
    this.erpName,
    this.method,
    this.reference,
    this.paidOn,
    this.amount,
    this.amountCurrency,
    this.parts = const [],
    this.splitStands,
  });

  static final _isoDay = RegExp(r'^\d{4}-\d{2}-\d{2}$');

  static String? _str(Object? v) => v is String ? v : null;

  /// `null` for an absent or malformed report: with no exception id there is
  /// nothing the dialog could book a split through.
  static QueueErpReportedPayment? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final id = raw['exception_id'];
    if (id is! String || id.isEmpty) return null;
    final amount = raw['amount'];
    return QueueErpReportedPayment(
      exceptionId: id,
      erpName: _str(raw['erp_name']),
      method: _str(raw['method']),
      reference: _str(raw['reference']),
      paidOn: _str(raw['paid_on']),
      amount: amount == null ? null : moneyToDisplay(amount),
      amountCurrency: _str(raw['amount_currency']),
      parts: _parts(raw['payment_parts']),
      splitStands: raw['split_stands'] is bool ? raw['split_stands'] as bool : null,
    );
  }

  /// The breakdown part by part, as the server books it: a rail that cannot be
  /// recorded outside FeohLedger (`virtual_card`, an unknown string) reads as
  /// not stated; a part with no exact amount string makes the whole breakdown
  /// read as none (`payment_methods.rail_parts_from_json`).
  static List<ErpRailPart> _parts(Object? raw) {
    if (raw is! List) return const [];
    final out = <ErpRailPart>[];
    for (final part in raw) {
      if (part is! Map) return const [];
      final amount = part['amount'];
      if (amount is! String || amount.trim().isEmpty) return const [];
      final rail = part['method'] is String
          ? (part['method'] as String).trim().toLowerCase()
          : '';
      out.add(ErpRailPart(
        rail: kExternalPaymentMethods.contains(rail) ? rail : null,
        amount: amount,
      ));
    }
    return out;
  }

  /// Does the breakdown span more than one rail? A part with no rail counts
  /// as one of its own, as the server counts it. Only then is it shown: one
  /// rail is just the method the form pre-fills.
  bool get showsSplit => parts.map((p) => p.rail).toSet().length > 1;

  /// The split is the record whatever rail is picked: `record-outside` would
  /// refuse it (`external_payment_erp_split`), so the dialog books through
  /// the exception's one-click and names no rail (decisions §426).
  bool get splitIsTheRecord => showsSplit && splitStands == true;

  /// The form pre-filled from the report. A rail it did not state, or one
  /// that cannot be recorded outside FeohLedger, is left unchosen; a missing,
  /// malformed or future date falls back to [today].
  RecordPrefill prefill(DateTime today) {
    final m = (method ?? '').trim().toLowerCase();
    final day = (paidOn ?? '').length >= 10 ? paidOn!.substring(0, 10) : '';
    final parsed = _isoDay.hasMatch(day) ? DateTime.tryParse(day) : null;
    return RecordPrefill(
      method: kExternalPaymentMethods.contains(m) ? m : null,
      reference: (reference ?? '').trim(),
      paidOn: parsed != null && !parsed.isAfter(today) ? parsed : today,
    );
  }

  /// For the offline cache round-trip (`PaymentQueueStore._queueItemToJson`).
  Map<String, dynamic> toJson() => {
        'exception_id': exceptionId,
        'erp_name': erpName,
        'method': method,
        'reference': reference,
        'paid_on': paidOn,
        'amount': amount,
        'amount_currency': amountCurrency,
        'payment_parts': [
          for (final p in parts) {'method': p.rail, 'amount': p.amount},
        ],
        'split_stands': splitStands,
      };
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

/// A payment run's stored status, as `GET /api/payments/runs/` reports it.
///
/// The rules a screen used to apply to raw strings (`run.status == 'draft'`)
/// live on [PaymentRun] as predicates over this, so a typo in a comparison
/// cannot silently hide a control. [other] is any code this build does not
/// name (`executing`, or one added later): it gets no action and the screen
/// labels it from [PaymentRun.statusCode].
enum PaymentRunStatus {
  draft('draft'),
  exported('exported'),
  submitted('submitted'),
  completed('completed'),
  partial('partial'),
  failed('failed'),
  cancelled('cancelled'),
  other('');

  const PaymentRunStatus(this.value);
  final String value;

  static PaymentRunStatus fromString(String s) {
    for (final e in PaymentRunStatus.values) {
      if (e != other && e.value == s) return e;
    }
    return other;
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

  /// The parsed status. Read the predicates below rather than comparing it.
  final PaymentRunStatus status;

  /// The raw status code as served — only for labelling an [PaymentRunStatus.other].
  final String statusCode;
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
    required this.statusCode,
    required this.totalAmountDisplay,
    this.currency,
    required this.paymentCount,
    required this.requiresCfoApproval,
    required this.cfoApproved,
    required this.createdAt,
    this.executedAt,
  }) : status = PaymentRunStatus.fromString(statusCode);

  factory PaymentRun.fromJson(Map<String, dynamic> json) {
    DateTime parseRequired(Object? v) =>
        v is String ? DateTime.parse(v) : DateTime.now();
    DateTime? parseOptional(Object? v) =>
        v is String && v.isNotEmpty ? DateTime.tryParse(v) : null;

    return PaymentRun(
      id: json['id'] as String,
      statusCode: json['status'] as String? ?? 'draft',
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

  bool get isDraft => status == PaymentRunStatus.draft;

  /// Only `draft` runs can be executed (or cancelled) from the app.
  bool get isExecutable => isDraft;

  /// Cancel is the draft's other exit (`POST /runs/{id}/cancel` refuses
  /// anything else).
  bool get isCancellable => isDraft;

  /// The run is over the org's CFO threshold and nobody has signed it off —
  /// execute will be refused until someone does.
  bool get awaitsCfoApproval => requiresCfoApproval && !cfoApproved;

  /// Whether a sign-off is something this run can take now: a draft still
  /// waiting on one. Mirrors the state guards on
  /// `POST /api/payments/runs/{id}/approve` (draft, requires it, not yet
  /// given); WHO may sign is the caller's role, checked beside this.
  bool get canTakeCfoApproval => isDraft && awaitsCfoApproval;

  /// The two stored statuses a run can hold an open record-as-paid decision
  /// in: a draft, or one whose bank file was exported on the web. Whether
  /// THIS reader may record it is the run detail's `actions.record_outside`
  /// verdict, which the dialog reads before it offers a form.
  bool get mayRecordOutside =>
      status == PaymentRunStatus.draft || status == PaymentRunStatus.exported;
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
