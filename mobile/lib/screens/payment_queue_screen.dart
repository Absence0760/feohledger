import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/coded_refusal_messages.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/load_error_messages.dart';
import 'package:feohledger_mobile/models/action_verdict.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/models/payment_queue.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/payment_queue_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/widgets/async_list_body.dart';
import 'package:feohledger_mobile/widgets/kpi_card.dart';
import 'package:feohledger_mobile/widgets/record_payment_dialog.dart';

/// Format a server-supplied money display string in [currency].
///
/// This screen shows figures in three different denominations and they must
/// not be run together: a queue row is in its own invoice's currency, the KPI
/// bar is in the org's reporting currency (which `/payments/summary` names
/// itself), and a run's total is in the one currency its legs agree on, which
/// the run response names — see [_runTotal]. Passing the wrong one relabels a
/// real figure, so the currency is a required argument rather than a
/// module-level default.
///
/// The parse inside [formatMoneyString] is for *rendering only* — money is
/// never summed on the device — and a figure it cannot format losslessly is
/// returned verbatim rather than rounded.
String _money(String display, String? currency) =>
    formatMoneyString(display, currency: currency);

/// A payment run's total, in the currency the run response names.
///
/// `payment_runs.total_amount` is a plain `SUM(Payment.amount)` with no
/// currency column, but a run cannot span two currencies — the builder 422s
/// that — so `services/payment_runs.py::one_currency` derives the code from the legs
/// and serves it on both the list and the detail. `PaymentRun.currency` is
/// `null` only where that proof fails (no payments, no invoice currency, a
/// legacy run whose legs disagree); [formatMoneyString] then renders the
/// digits bare, which for a sum denominated in nothing real is the honest
/// answer rather than a fallback (`docs/decisions.md` §160).
///
/// All four run figures go through here — the runs list, the sign-off row, the
/// detail sheet, and the execute confirmation — so none can end up labelled
/// differently from the others.
String _runTotal(PaymentRun run) =>
    formatMoneyString(run.totalAmountDisplay, currency: run.currency);

/// Localized label for a payment method (the model's `label` is English-only).
String _methodLabel(AppLocalizations l, PaymentMethod m) => switch (m) {
  PaymentMethod.ach => l.payMethodAch,
  PaymentMethod.wire => l.payMethodWire,
  PaymentMethod.check => l.payMethodCheck,
  PaymentMethod.virtualCard => l.payMethodVirtualCard,
};

/// Localized sentence for why a payment run would refuse — or constrain — a
/// queue row.
///
/// `blocked_reason` is a stable CODE from the backend's fixed, PII-free
/// vocabulary (a `PAYMENT_BLOCKING_EXCEPTION_TYPES` member, or one of
/// `services/payment_runs`' own reason constants) — deliberately NOT the
/// exception's description, which can carry vendor / bank / amount detail. An
/// unrecognised or absent code falls back to the generic sentence rather than
/// rendering a raw identifier at the operator. Mirrors the web page's
/// `blockedReason()`.
String _blockedReasonLabel(AppLocalizations l, String? code) => switch (code) {
  'duplicate' => l.payQueueBlockedDuplicate,
  'fraud_flag' => l.payQueueBlockedFraudFlag,
  'line_total_mismatch' => l.payQueueBlockedLineTotalMismatch,
  'payment_reconciliation' => l.payQueueBlockedPaymentReconciliation,
  'quality_hold' => l.payQueueBlockedQualityHold,
  'po_mismatch' => l.payQueueBlockedPoMismatch,
  'vendor_not_active' => l.payQueueBlockedVendorNotActive,
  // The invoice records no currency: no rail can pay it until one is set.
  'currency_unknown' => l.payQueueBlockedCurrencyUnknown,
  'applied_credit_mismatch' => l.payQueueBlockedAppliedCreditMismatch,
  'fully_credited' => l.payQueueBlockedFullyCredited,
  // Not a block — the row is pinned to the card rail. Its own sentence
  // says so, rather than the "can't be paid" generic.
  'live_virtual_card' => l.payQueueBlockedLiveVirtualCard,
  _ => l.payQueueBlockedGeneric,
};

/// The sentence for a row's refusal verdict, or `null` when there is nothing
/// to say. The ONE derivation — the chip and the screen-reader announcement
/// both read it, so they can never disagree about whether a row has a reason.
///
/// Non-null when the backend sent a code (it sends one for a PINNED row too,
/// which is how the UI can say why the rail is fixed) OR when we refuse the
/// row ourselves — an unnameable pin has to explain itself even though the
/// server sent no code for it.
String? _verdictReason(AppLocalizations l, PaymentQueueItem item) =>
    item.blockedReason != null || !item.isSelectable
    ? _blockedReasonLabel(l, item.blockedReason)
    : null;

/// The refusal verdict, as the tail of a queue row's single merged
/// announcement. Empty when a run would neither refuse nor constrain the row.
///
/// Split out because it has three distinct arms and an inline interpolation of
/// them once rendered the literal string "null" for a rail pinned with no
/// accompanying reason code.
String _verdictAnnounce(AppLocalizations l, PaymentQueueItem item) {
  final reason = _verdictReason(l, item);
  if (!item.isSelectable) return ', ${l.payQueueBlockedAnnounce(reason!)}';
  final pinned = item.requiredMethod;
  final parts = <String>[
    if (pinned != null) l.payQueuePinnedMethod(_methodLabel(l, pinned)),
    ?reason,
  ];
  return parts.isEmpty ? '' : ', ${parts.join(', ')}';
}

/// Localized label for a run's status. A status this build does not name
/// ([PaymentRunStatus.other]) falls back to the served code, capitalized.
String _runStatusLabel(AppLocalizations l, PaymentRun run) =>
    switch (run.status) {
      PaymentRunStatus.draft => l.payRunStatusDraft,
      PaymentRunStatus.exported => l.payRunStatusExported,
      PaymentRunStatus.completed => l.payRunStatusCompleted,
      PaymentRunStatus.submitted => l.payRunStatusSubmitted,
      PaymentRunStatus.partial => l.payRunStatusPartial,
      PaymentRunStatus.failed => l.payRunStatusFailed,
      PaymentRunStatus.cancelled => l.payRunStatusCancelled,
      PaymentRunStatus.other =>
        run.statusCode.isEmpty
            ? run.statusCode
            : run.statusCode[0].toUpperCase() + run.statusCode.substring(1),
    };

/// Payment queue + runs. Tab 1 lists approved invoices; the user ticks rows,
/// picks a method per row, and creates a draft run. Tab 2 lists the runs and
/// executes / cancels drafts. A KPI summary bar (total paid / pending /
/// rebates / queue) sits above both. Gated to admin / ap_manager / cfo.
///
/// **Record-only tenants** (the no-rail pilot, backend decisions §251): the
/// server's execution mode (`GET /api/payments/execution-mode`) decides
/// whether Execute is offered at all — failing closed while unknown — and a
/// confirmed record-only tenant gets a one-sentence note saying so, leading
/// each tab's list and scrolling away with it (so it does not hold height
/// above the rows at large text sizes). "Record as paid" is offered in BOTH
/// modes, as on the web: per queue row from the row's own
/// `record_outside_refusal`, and per run from the run detail's
/// `actions.record_outside` verdict.
///
/// Both tabs' bodies are [AsyncListBody], each worded from its own load error
/// ([PaymentQueueStore.queueLoadError] / [PaymentQueueStore.runsLoadError]);
/// a refused run action is [PaymentQueueStore.actionError], a snackbar.
class PaymentQueueScreen extends StatefulWidget {
  const PaymentQueueScreen({super.key});

  @override
  State<PaymentQueueScreen> createState() => _PaymentQueueScreenState();
}

class _PaymentQueueScreenState extends State<PaymentQueueScreen>
    with SingleTickerProviderStateMixin {
  late final TabController _tabs;
  bool _busy = false;

  bool get _canManage => AuthStore.instance.canManagePayments;

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: 2, vsync: this);
    SchedulerBinding.instance.addPostFrameCallback((_) {
      PaymentQueueStore.instance.fetch();
      PaymentQueueStore.instance.fetchRuns();
    });
  }

  @override
  void dispose() {
    _tabs.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(l.payTitle),
        bottom: TabBar(
          controller: _tabs,
          tabs: [
            Tab(text: l.payTabQueue),
            Tab(text: l.payTabRuns),
          ],
        ),
      ),
      body: Column(
        children: [
          _summaryBar(),
          Expanded(
            child: TabBarView(
              controller: _tabs,
              children: [_queueTab(), _runsTab()],
            ),
          ),
        ],
      ),
    );
  }

  // ── Summary KPI bar ────────────────────────────────────────────────
  Widget _summaryBar() {
    return ListenableBuilder(
      listenable: PaymentQueueStore.instance,
      builder: (context, _) {
        final l = AppLocalizations.of(context);
        final s = PaymentQueueStore.instance.summary;
        if (s == null) return const SizedBox.shrink();
        return Padding(
          padding: const EdgeInsets.fromLTRB(12, 12, 12, 4),
          child: GridView.count(
            crossAxisCount: 2,
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            mainAxisSpacing: 8,
            crossAxisSpacing: 8,
            childAspectRatio: 2.4,
            children: [
              KpiCard(
                title: l.paySummaryTotalPaid,
                value: _money(s.totalPaidDisplay, s.currency),
                icon: Icons.check_circle,
                color: Colors.green,
              ),
              KpiCard(
                title: l.paySummaryPending,
                value: _money(s.totalPendingDisplay, s.currency),
                icon: Icons.hourglass_bottom,
                color: Colors.orange,
              ),
              KpiCard(
                title: l.paySummaryInQueue,
                value: s.queueCount.toString(),
                subtitle: l.paySummaryPaymentsSubtitle(s.paymentCount),
                icon: Icons.payments,
                color: Colors.blue,
              ),
              KpiCard(
                title: l.paySummaryCardRebates,
                value: _money(s.totalRebatesDisplay, s.currency),
                icon: Icons.savings,
                color: Colors.purple,
              ),
            ],
          ),
        );
      },
    );
  }

  /// Shown only on a CONFIRMED record-only tenant: an unresolved mode hides
  /// the send controls (fail closed) but is not evidence of this, so it says
  /// nothing. A status, not an alert — it describes the page. `null` when
  /// there is nothing to say; each tab's list leads with it otherwise, so it
  /// scrolls with the rows. Its one sentence points at the web app for a
  /// run's bank file, which mobile does not download.
  Widget? _recordOnlyBanner() {
    if (!PaymentQueueStore.instance.isRecordOnly) return null;
    final l = AppLocalizations.of(context);
    return Container(
      key: const Key('record-only-banner'),
      width: double.infinity,
      margin: const EdgeInsets.fromLTRB(12, 4, 12, 4),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.blueGrey.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ExcludeSemantics(
            child: Icon(
              Icons.info_outline,
              size: 20,
              color: Colors.blueGrey.shade800,
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              l.payRecordOnlyBanner,
              // Darkened for AA contrast over the pale tint.
              style: TextStyle(color: Colors.blueGrey.shade900, fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }

  /// [body] with the record-only note (when there is one) as a header that
  /// scrolls away with the list rather than holding height above it.
  Widget _withLeadingBanner(Widget body) {
    final banner = _recordOnlyBanner();
    if (banner == null) return body;
    return NestedScrollView(
      headerSliverBuilder: (context, _) => [SliverToBoxAdapter(child: banner)],
      body: body,
    );
  }

  // ── Queue tab ──────────────────────────────────────────────────────
  Widget _queueTab() {
    return ListenableBuilder(
      listenable: PaymentQueueStore.instance,
      builder: (context, _) {
        final l = AppLocalizations.of(context);
        final store = PaymentQueueStore.instance;
        final loadError = store.queueLoadError;
        return Column(
          children: [
            Expanded(
              child: _withLeadingBanner(
                AsyncListBody<PaymentQueueItem>(
                  items: store.queue,
                  loading: store.loading,
                  // The queue read's failure only: a refused run action is
                  // actionError and leaves the list live; a failed runs read is
                  // the Runs tab's.
                  hasError: loadError != null,
                  fromCache: store.fromCache,
                  errorMessage: loadError == null
                      ? ''
                      : describeLoadError(
                          l,
                          loadError,
                          l.payQueueLoadErrorPrefix,
                        ),
                  onRefresh: store.fetch,
                  empty: Text(l.payQueueEmpty, textAlign: TextAlign.center),
                  itemBuilder: (context, item) => _queueRow(item),
                ),
              ),
            ),
            if (_canManage && store.hasSelection) _createRunBar(store),
          ],
        );
      },
    );
  }

  Widget _queueRow(PaymentQueueItem item) {
    final l = AppLocalizations.of(context);
    final store = PaymentQueueStore.instance;
    final selected = store.isSelected(item.id);
    // A row a payment run would refuse on every rail. Its checkbox is disabled
    // and it can't be tapped into the selection — one such invoice in a batch
    // 409s the WHOLE draft run, taking every other invoice down with it.
    // `hasUnknownRequiredMethod` joins it: the backend pinned a rail this build
    // can't name, so we can't honour the pin and must not offer the row.
    final selectable = item.isSelectable;
    final pinnedMethod = item.requiredMethod;
    final reason = _verdictReason(l, item);
    final dueText = item.dueDate != null
        ? l.payQueueDue(formatDate(item.dueDate!))
        : l.payQueueNoDueDate;

    final subtitleParts = <String>[
      item.invoiceNumber,
      dueText,
      if (item.discountEligible && item.discountAmountDisplay != null)
        l.payQueueDiscount(_money(item.discountAmountDisplay!, item.currency)),
    ];

    final row = Semantics(
      label:
          '${item.vendorName}, '
          '${_money(item.amountDisplay, item.currency)}, '
          '${subtitleParts.join(', ')}'
          '${item.isOverdue ? ', ${l.payQueueOverdue}' : ''}'
          // The verdict is spoken, not just coloured — a screen-reader user
          // meeting a disabled checkbox has to be told why it is disabled, and
          // one whose rail was chosen for them has to be told which.
          '${_verdictAnnounce(l, item)}'
          '${selected ? ', ${l.payQueueSelected}' : ''}',
      excludeSemantics: true,
      child: ListTile(
        leading: _canManage
            ? Checkbox(
                value: selected,
                // `null` renders the Material disabled state AND makes the
                // control untappable; the store refuses the same row anyway,
                // so the guard holds even if this ever regresses.
                onChanged: selectable
                    ? (_) => store.toggleSelection(item)
                    : null,
              )
            : null,
        title: Row(
          children: [
            Expanded(
              child: Text(
                item.vendorName,
                style: const TextStyle(fontWeight: FontWeight.w600),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            Text(
              _money(item.amountDisplay, item.currency),
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
          ],
        ),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Flexible(
                    child: Text(
                      item.invoiceNumber,
                      style: TextStyle(
                        color: Colors.grey.shade700,
                        fontSize: 13,
                      ),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  const SizedBox(width: 12),
                  Text(
                    dueText,
                    style: TextStyle(
                      color: item.isOverdue
                          ? Colors.red.shade700
                          : Colors.grey.shade700,
                      fontSize: 12,
                    ),
                  ),
                  const Spacer(),
                  // A pinned row shows the rail it is fixed to instead of a
                  // picker — offering four options where the backend accepts
                  // one is offering a choice that doesn't exist.
                  if (_canManage && selectable && pinnedMethod != null)
                    _pinnedMethodChip(pinnedMethod)
                  else if (_canManage && selectable && selected)
                    _methodDropdown(item),
                ],
              ),
              if (reason != null)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: _reasonChip(reason, blocked: !selectable),
                ),
            ],
          ),
        ),
        // Blocked rows stay visible and readable — an operator has to see WHAT
        // is blocked to go and clear it — they just aren't selectable.
        onTap: _canManage && selectable
            ? () => store.toggleSelection(item)
            : null,
      ),
    );
    // The record action sits OUTSIDE the row's merged announcement: inside an
    // `excludeSemantics` it would be unreachable to a screen reader.
    if (!AuthStore.instance.canRecordExternalPayment) return row;
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [row, _recordAction(item)],
    );
  }

  /// "Record as paid" for one row — for `payment.record_external`, in either
  /// execution mode. Gated on the row's `record_outside_refusal` (the gates
  /// `POST /record-outside` itself applies), deliberately NOT on `blocked`,
  /// the run builder's verdict, which also refuses an unverified vendor that
  /// recording does not (#566). A refused row shows the action disabled with
  /// its reason, never hidden, so the operator learns what to clear.
  Widget _recordAction(PaymentQueueItem item) {
    final l = AppLocalizations.of(context);
    final code = item.recordOutsideRefusal;
    final String? why = code == null
        ? null
        : localizeCodedRefusal(l, {
                'code': code,
                'params': {'status': item.status},
              }) ??
              l.payRecordUnavailable;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.center,
        children: [
          TextButton.icon(
            key: Key('record-outside-${item.id}'),
            onPressed: why == null ? () => _recordInvoice(item) : null,
            icon: const Icon(Icons.task_alt, size: 18),
            label: Text(
              l.payRecordAction,
              semanticsLabel: l.payRecordActionLabel(item.invoiceNumber),
            ),
          ),
          if (why != null) ...[
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                why,
                style: TextStyle(color: Colors.grey.shade800, fontSize: 12),
              ),
            ),
          ],
        ],
      ),
    );
  }

  Future<void> _recordInvoice(PaymentQueueItem item) async {
    final l = AppLocalizations.of(context);
    final recorded = await showRecordInvoicePaymentDialog(context, item);
    if (!mounted || recorded != true) return;
    final text = l.payRecordRecorded(item.invoiceNumber);
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
    A11y.announce(context, text);
  }

  /// The rail a pinned row will be paid on. Read-only by design: it is the one
  /// rail `POST /api/payments/runs` accepts for this invoice. The row's own
  /// merged announcement already speaks it (see [_verdictAnnounce]), so the
  /// chip is excluded from the semantics tree rather than repeating itself.
  Widget _pinnedMethodChip(PaymentMethod method) {
    final l = AppLocalizations.of(context);
    final label = l.payQueuePinnedMethod(_methodLabel(l, method));
    return ExcludeSemantics(
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: Colors.blue.withValues(alpha: 0.12),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Text(
          label,
          // Darkened for AA contrast over the pale tint (>=4.5:1), the same
          // rule the run-status chips follow.
          style: TextStyle(
            color: Colors.blue.shade900,
            fontSize: 12,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
    );
  }

  /// Why a run would refuse — or pin — this row. The row's Semantics already
  /// speaks the sentence, so the chip itself is excluded from the tree.
  Widget _reasonChip(String reason, {required bool blocked}) {
    final (tint, textColor) = blocked
        ? (Colors.red, Colors.red.shade900)
        : (Colors.blueGrey, Colors.blueGrey.shade800);
    return ExcludeSemantics(
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: tint.withValues(alpha: 0.12),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Text(
          reason,
          style: TextStyle(
            color: textColor,
            fontSize: 12,
            fontWeight: FontWeight.w500,
          ),
        ),
      ),
    );
  }

  Widget _methodDropdown(PaymentQueueItem item) {
    final l = AppLocalizations.of(context);
    final store = PaymentQueueStore.instance;
    return Semantics(
      label: l.payMethodLabel(item.invoiceNumber),
      child: DropdownButton<PaymentMethod>(
        value: store.methodFor(item),
        isDense: true,
        underline: const SizedBox.shrink(),
        items: PaymentMethod.values
            .map(
              (m) => DropdownMenuItem(
                value: m,
                child: Text(
                  _methodLabel(l, m),
                  style: const TextStyle(fontSize: 13),
                ),
              ),
            )
            .toList(),
        onChanged: (m) {
          if (m != null) store.setMethod(item, m);
        },
      ),
    );
  }

  Widget _createRunBar(PaymentQueueStore store) {
    final l = AppLocalizations.of(context);
    final count = store.selectedCount;
    return Material(
      elevation: 8,
      child: SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  l.paySelectedCount(count),
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
              ),
              TextButton(
                onPressed: _busy ? null : store.clearSelection,
                child: Text(l.payClear),
              ),
              const SizedBox(width: 8),
              FilledButton.icon(
                onPressed: _busy ? null : _createRun,
                icon: const Icon(Icons.playlist_add_check),
                label: Text(l.payCreateRun),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _createRun() async {
    final l = AppLocalizations.of(context);
    setState(() => _busy = true);
    final message = await PaymentQueueStore.instance.createRunFromSelection();
    if (!mounted) return;
    setState(() => _busy = false);
    final text =
        message ??
        l.payCreateRunFailed(PaymentQueueStore.instance.actionError ?? '');
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
    A11y.announce(context, text);
    if (message != null) _tabs.animateTo(1); // jump to Runs
  }

  // ── Runs tab ───────────────────────────────────────────────────────
  Widget _runsTab() {
    return ListenableBuilder(
      listenable: PaymentQueueStore.instance,
      builder: (context, _) {
        final l = AppLocalizations.of(context);
        final store = PaymentQueueStore.instance;
        final loadError = store.runsLoadError;
        return _withLeadingBanner(
          AsyncListBody<PaymentRun>(
            items: store.runs,
            loading: store.runsLoading,
            hasError: loadError != null,
            // Runs are never served from the cache (a stale status would
            // offer money controls), so there is no saved copy to label.
            fromCache: false,
            errorMessage: loadError == null
                ? ''
                : describeLoadError(l, loadError, l.payRunsLoadErrorPrefix),
            onRefresh: store.fetchRuns,
            empty: Text(l.payRunsEmpty, textAlign: TextAlign.center),
            itemBuilder: (context, run) => _runRow(run),
          ),
        );
      },
    );
  }

  Widget _runRow(PaymentRun run) {
    final l = AppLocalizations.of(context);
    final subtitle =
        l.payRunSubtitle(run.paymentCount, formatDate(run.createdAt)) +
        (run.awaitsCfoApproval ? l.payRunCfoRequiredSuffix : '');
    // One merged announcement for the title + subtitle only: wrapping the
    // whole ListTile in `excludeSemantics` also swallowed `trailing`, so a
    // screen-reader user could never reach the "Run actions" menu.
    return ListTile(
      title: Semantics(
        label: l.payRunAnnounce(
          _runTotal(run),
          _runStatusLabel(l, run),
          subtitle,
        ),
        excludeSemantics: true,
        child: Row(
          children: [
            Expanded(
              child: Text(
                _runTotal(run),
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
            ),
            _runStatusChip(run),
          ],
        ),
      ),
      subtitle: ExcludeSemantics(
        child: Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Text(
            subtitle,
            style: TextStyle(
              color: Theme.of(context).colorScheme.onSurfaceVariant,
              fontSize: 12,
            ),
          ),
        ),
      ),
      trailing: _runMenu(run),
    );
  }

  /// The run's action menu, or `null` when it would hold nothing.
  Widget? _runMenu(PaymentRun run) {
    final l = AppLocalizations.of(context);
    final store = PaymentQueueStore.instance;
    final approve = _approveItem(l, run);
    // Execute only where FeohLedger SENDS payments — the server's execution
    // mode, failing closed while it is unknown. On a record-only tenant the
    // endpoint answers `payments_record_only`, so the control could only
    // ever be refused; the record path below replaces it. Who may execute
    // THIS run (`payment.execute`, maker-checker, CFO sign-off) is the run
    // detail's `actions.execute`, never a role or status check here.
    final execute = store.canDispatch
        ? _verdictItem(
            'execute',
            l.payRunActionExecute,
            store.runExecuteVerdict(run.id),
            runId: run.id,
          )
        : null;
    final cancel = _verdictItem(
      'cancel',
      l.payRunActionCancel,
      store.runCancelVerdict(run.id),
      runId: run.id,
    );
    final items = <PopupMenuEntry<String>>[
      ?approve,
      ?execute,
      ?cancel,
      // Record the run as paid outside FeohLedger, in either mode. The menu
      // entry appears where [PaymentRun.mayRecordOutside] says a run can hold
      // an open record decision; whether THIS reader may record it is the
      // run detail's `actions.record_outside` verdict, which the dialog reads
      // before it offers a form.
      if (AuthStore.instance.canRecordExternalPayment && run.mayRecordOutside)
        PopupMenuItem(value: 'record', child: Text(l.payRunActionRecord)),
    ];
    if (items.isEmpty) return null;
    return Semantics(
      label: l.payRunActions,
      button: true,
      child: PopupMenuButton<String>(
        onSelected: (v) => _runMenuAction(run, v),
        itemBuilder: (_) => items,
      ),
    );
  }

  /// CFO sign-off, from the server's verdict ([PaymentQueueStore.
  /// runApproveVerdict] — the run detail's `actions.approve`), never a role or
  /// maker-checker check here. [PaymentRun.canTakeCfoApproval] decides only
  /// whether the run HAS a CFO step to render a control for (the web run
  /// dialog's `pendingCfo`). A role refusal (`role_not_permitted`: anyone not
  /// a CFO, admins included) hides it; any other refusal — the CFO who
  /// created the run (maker-checker) — renders it disabled with the reason,
  /// as the web does, instead of an Approve that can only 403.
  PopupMenuEntry<String>? _approveItem(AppLocalizations l, PaymentRun run) {
    if (!run.canTakeCfoApproval) return null;
    return _verdictItem(
      'approve',
      l.payRunActionApprove,
      PaymentQueueStore.instance.runApproveVerdict(run.id),
      runId: run.id,
    );
  }

  /// A run menu item offered from the server's [verdict]: none while it is
  /// unanswered (fail closed) or refused for the reader's role
  /// (`role_not_permitted`) or the run's status (`payment_run_not_draft` —
  /// a run the list showed as a draft that has since moved on); enabled when
  /// accepted; otherwise disabled with the refusal in the reader's language
  /// (maker-checker, the CFO wait), instead of a control that can only 403.
  ///
  /// A run whose verdict read failed ([PaymentQueueStore.runVerdictsFailed])
  /// renders the item disabled with "couldn't check — pull to refresh":
  /// still nothing to tap, but the reader learns why there is no control
  /// (a CFO otherwise saw "awaiting CFO approval" with nothing to press).
  PopupMenuEntry<String>? _verdictItem(
    String value,
    String label,
    ActionVerdict verdict, {
    required String runId,
  }) {
    final l = AppLocalizations.of(context);
    final String reason;
    if (PaymentQueueStore.instance.runVerdictsFailed(runId)) {
      reason = l.reviewVerdictsUnavailable;
    } else {
      if (!verdict.isOffered(hiddenCodes: const {'payment_run_not_draft'})) {
        return null;
      }
      if (verdict.allowed) {
        return PopupMenuItem(value: value, child: Text(label));
      }
      reason = localizeCodedRefusal(l, verdict.refusal) ??
          (verdict.refusal?['message'] as String? ?? '');
    }
    return PopupMenuItem(
      value: value,
      enabled: false,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(label),
          if (reason.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Text(
                reason,
                style: TextStyle(
                  fontSize: 12,
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ),
            ),
        ],
      ),
    );
  }

  Future<void> _runMenuAction(PaymentRun run, String action) async {
    final l = AppLocalizations.of(context);
    if (action == 'approve') {
      // Authorizing a payment run is a money-path decision, so it confirms
      // first and the dialog names the run and its total.
      final confirmed = await _confirm(
        l.payRunApproveTitle,
        l.payRunApproveBody(
          formatDate(run.createdAt),
          run.paymentCount,
          _runTotal(run),
        ),
        confirmLabel: l.payRunApproveConfirm,
      );
      if (confirmed != true) return;
      final message = await PaymentQueueStore.instance.approveRun(run.id);
      if (!mounted) return;
      final text = message ?? l.payRunApproveFailed(_runRefusal(l));
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
      A11y.announce(context, text);
    } else if (action == 'record') {
      final count = await showRecordRunPaymentDialog(context, run);
      if (!mounted || count == null) return;
      final text = l.payRunRecordRecorded(count);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
      A11y.announce(context, text);
    } else if (action == 'execute') {
      final confirmed = await _confirm(
        l.payRunExecuteTitle,
        l.payRunExecuteBody(_runTotal(run)),
      );
      if (confirmed != true) return;
      final message = await PaymentQueueStore.instance.executeRun(run.id);
      if (!mounted) return;
      final text = message ?? l.payRunExecuteFailed(_runRefusal(l));
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
      A11y.announce(context, text);
    } else {
      final message = await PaymentQueueStore.instance.cancelRun(run.id);
      if (!mounted) return;
      final text =
          message ??
          l.payRunCancelFailed(PaymentQueueStore.instance.actionError ?? '');
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
      A11y.announce(context, text);
    }
  }

  /// The last refused approve / execute in the reader's language when the
  /// server coded it (maker-checker, the CFO wait), else its own sentence.
  String _runRefusal(AppLocalizations l) {
    final store = PaymentQueueStore.instance;
    return localizeCodedRefusal(l, store.runActionErrorDetail) ??
        store.actionError ??
        '';
  }

  Future<bool?> _confirm(String title, String body, {String? confirmLabel}) {
    final l = AppLocalizations.of(context);
    return showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: Text(body),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: Text(l.payConfirmCancel),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: Text(confirmLabel ?? l.payConfirmExecute),
          ),
        ],
      ),
    );
  }

  Widget _runStatusChip(PaymentRun run) {
    final l = AppLocalizations.of(context);
    // Darkened foreground for AA contrast over the pale tint (≥4.5:1).
    final (color, textColor) = switch (run.status) {
      PaymentRunStatus.draft => (Colors.blueGrey, Colors.blueGrey.shade800),
      PaymentRunStatus.exported => (Colors.indigo, Colors.indigo.shade800),
      PaymentRunStatus.completed => (Colors.green, Colors.green.shade900),
      PaymentRunStatus.submitted => (Colors.blue, Colors.blue.shade800),
      PaymentRunStatus.partial => (Colors.orange, Colors.brown.shade800),
      PaymentRunStatus.failed => (Colors.red, Colors.red.shade900),
      PaymentRunStatus.cancelled ||
      PaymentRunStatus.other => (Colors.grey, Colors.grey.shade800),
    };
    final label = _runStatusLabel(l, run);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: textColor,
          fontSize: 12,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}
