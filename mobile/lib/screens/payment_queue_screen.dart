import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:intl/intl.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/models/payment_queue.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/payment_queue_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/widgets/kpi_card.dart';

final _dateFormat = DateFormat('MMM d, yyyy');

/// Format a server-supplied money display string in [currency].
///
/// This screen shows figures in three different denominations and they must
/// not be run together: a queue row is in its own invoice's currency, the KPI
/// bar is in the org's reporting currency (which `/payments/summary` names
/// itself), and a run's total is in no single currency at all — see
/// [_runTotal]. Passing the wrong one relabels a real figure, so the currency
/// is a required argument rather than a module-level default.
///
/// The parse inside [formatMoneyString] is for *rendering only* — money is
/// never summed on the device — and a figure it cannot format losslessly is
/// returned verbatim rather than rounded.
String _money(String display, String? currency) =>
    formatMoneyString(display, currency: currency);

/// A payment run's total, deliberately bare.
///
/// `payment_runs.total_amount` is a plain `SUM(Payment.amount)` and each
/// payment is denominated in its own invoice's currency, so a run spanning a
/// USD and a EUR invoice holds a quantity in neither. The org's reporting
/// currency would be a guess at what a mixed sum "really" is, and a `$` was
/// simply wrong; the honest rendering is the digits with no symbol until the
/// endpoint rolls the sum up the way `/payments/queue` already does. Tracked
/// in `docs/followups.md`.
String _runTotal(PaymentRun run) =>
    formatMoneyString(run.totalAmountDisplay, currency: null);

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

/// Localized label for a payment-run status string. Unknown statuses fall back
/// to the server-supplied value capitalized (mirrors the old behaviour).
String _runStatusLabel(AppLocalizations l, String status) => switch (status) {
      'draft' => l.payRunStatusDraft,
      'completed' => l.payRunStatusCompleted,
      'submitted' => l.payRunStatusSubmitted,
      'partial' => l.payRunStatusPartial,
      'failed' => l.payRunStatusFailed,
      'cancelled' => l.payRunStatusCancelled,
      _ => status.isEmpty
          ? status
          : status[0].toUpperCase() + status.substring(1),
    };

/// Payment queue + runs. Tab 1 lists approved invoices; the user ticks rows,
/// picks a method per row, and creates a draft run. Tab 2 lists the runs and
/// executes / cancels drafts. A KPI summary bar (total paid / pending /
/// rebates / queue) sits above both. Gated to admin / ap_manager / cfo.
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
              children: [
                _queueTab(),
                _runsTab(),
              ],
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

  // ── Queue tab ──────────────────────────────────────────────────────
  Widget _queueTab() {
    return ListenableBuilder(
      listenable: PaymentQueueStore.instance,
      builder: (context, _) {
        final l = AppLocalizations.of(context);
        final store = PaymentQueueStore.instance;

        if (store.loading && store.queue.isEmpty) {
          return const Center(child: CircularProgressIndicator());
        }
        if (store.error != null && store.queue.isEmpty) {
          return _errorState(store.error!, store.fetch);
        }
        if (store.queue.isEmpty) {
          return Center(child: Text(l.payQueueEmpty));
        }

        return Column(
          children: [
            Expanded(
              child: RefreshIndicator(
                onRefresh: store.fetch,
                child: ListView.separated(
                  itemCount: store.queue.length,
                  separatorBuilder: (_, _) => const Divider(height: 1),
                  itemBuilder: (context, index) =>
                      _queueRow(store.queue[index]),
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
        ? l.payQueueDue(_dateFormat.format(item.dueDate!))
        : l.payQueueNoDueDate;

    final subtitleParts = <String>[
      item.invoiceNumber,
      dueText,
      if (item.discountEligible && item.discountAmountDisplay != null)
        l.payQueueDiscount(_money(item.discountAmountDisplay!, item.currency)),
    ];

    return Semantics(
      label: '${item.vendorName}, '
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
                onChanged:
                    selectable ? (_) => store.toggleSelection(item) : null,
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
                      style:
                          TextStyle(color: Colors.grey.shade700, fontSize: 13),
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
                child: Text(_methodLabel(l, m),
                    style: const TextStyle(fontSize: 13)),
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
    final message =
        await PaymentQueueStore.instance.createRunFromSelection();
    if (!mounted) return;
    setState(() => _busy = false);
    final text = message ??
        l.payCreateRunFailed('${PaymentQueueStore.instance.error}');
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
        if (store.runs.isEmpty) {
          return RefreshIndicator(
            onRefresh: store.fetchRuns,
            child: ListView(
              children: [
                const SizedBox(height: 120),
                Center(child: Text(l.payRunsEmpty)),
              ],
            ),
          );
        }
        return RefreshIndicator(
          onRefresh: store.fetchRuns,
          child: ListView.separated(
            itemCount: store.runs.length,
            separatorBuilder: (_, _) => const Divider(height: 1),
            itemBuilder: (context, index) => _runRow(store.runs[index]),
          ),
        );
      },
    );
  }

  Widget _runRow(PaymentRun run) {
    final l = AppLocalizations.of(context);
    final subtitle =
        l.payRunSubtitle(run.paymentCount, _dateFormat.format(run.createdAt)) +
            (run.requiresCfoApproval && !run.cfoApproved
                ? l.payRunCfoRequiredSuffix
                : '');
    return Semantics(
      label: l.payRunAnnounce(
        _runTotal(run),
        _runStatusLabel(l, run.status),
        subtitle,
      ),
      excludeSemantics: true,
      child: ListTile(
        title: Row(
          children: [
            Expanded(
              child: Text(
                _runTotal(run),
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
            ),
            _runStatusChip(run.status),
          ],
        ),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Text(
            subtitle,
            style: TextStyle(color: Colors.grey.shade700, fontSize: 12),
          ),
        ),
        trailing: _canManage && run.isExecutable
            ? Semantics(
                label: l.payRunActions,
                button: true,
                child: PopupMenuButton<String>(
                  onSelected: (v) => _runMenuAction(run, v),
                  itemBuilder: (_) => [
                    // CFO sign-off. Offered only to the one role the backend
                    // accepts (`require_roles(ROLE_CFO)`) and only while the
                    // run is actually waiting on it — otherwise the person
                    // reading "needs CFO approval" is the person who can't
                    // act on it, which is exactly the gap this closes.
                    if (_canApproveRun(run))
                      PopupMenuItem(
                          value: 'approve', child: Text(l.payRunActionApprove)),
                    PopupMenuItem(
                        value: 'execute', child: Text(l.payRunActionExecute)),
                    PopupMenuItem(
                        value: 'cancel', child: Text(l.payRunActionCancel)),
                  ],
                ),
              )
            : null,
      ),
    );
  }

  /// Whether the signed-in user can sign this run off. Mirrors the backend
  /// `require_roles(ROLE_CFO)` on `POST /api/payments/runs/{id}/approve` plus
  /// its two state guards (draft — already implied by [PaymentRun.isExecutable]
  /// at the call site — requires sign-off, not yet signed off).
  bool _canApproveRun(PaymentRun run) =>
      AuthStore.instance.canApprovePaymentRun &&
      run.requiresCfoApproval &&
      !run.cfoApproved;

  Future<void> _runMenuAction(PaymentRun run, String action) async {
    final l = AppLocalizations.of(context);
    if (action == 'approve') {
      // Authorizing a payment run is a money-path decision, so it confirms
      // first and the dialog names the run and its total.
      final confirmed = await _confirm(
        l.payRunApproveTitle,
        l.payRunApproveBody(
          _dateFormat.format(run.createdAt),
          run.paymentCount,
          _runTotal(run),
        ),
        confirmLabel: l.payRunApproveConfirm,
      );
      if (confirmed != true) return;
      final message = await PaymentQueueStore.instance.approveRun(run.id);
      if (!mounted) return;
      final text = message ??
          l.payRunApproveFailed('${PaymentQueueStore.instance.error}');
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
      A11y.announce(context, text);
    } else if (action == 'execute') {
      // CFO approval is a server-side gate; surface it before attempting.
      if (run.requiresCfoApproval && !run.cfoApproved) {
        final text = l.payRunCfoBlocked;
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(text)));
        A11y.announce(context, text);
        return;
      }
      final confirmed = await _confirm(
        l.payRunExecuteTitle,
        l.payRunExecuteBody(_runTotal(run)),
      );
      if (confirmed != true) return;
      final message = await PaymentQueueStore.instance.executeRun(run.id);
      if (!mounted) return;
      final text = message ??
          l.payRunExecuteFailed('${PaymentQueueStore.instance.error}');
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
      A11y.announce(context, text);
    } else {
      final message = await PaymentQueueStore.instance.cancelRun(run.id);
      if (!mounted) return;
      final text = message ??
          l.payRunCancelFailed('${PaymentQueueStore.instance.error}');
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
      A11y.announce(context, text);
    }
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

  Widget _runStatusChip(String status) {
    final l = AppLocalizations.of(context);
    // Darkened foreground for AA contrast over the pale tint (≥4.5:1).
    final (color, textColor) = switch (status) {
      'draft' => (Colors.blueGrey, Colors.blueGrey.shade800),
      'completed' => (Colors.green, Colors.green.shade900),
      'submitted' => (Colors.blue, Colors.blue.shade800),
      'partial' => (Colors.orange, Colors.brown.shade800),
      'failed' => (Colors.red, Colors.red.shade900),
      'cancelled' => (Colors.grey, Colors.grey.shade800),
      _ => (Colors.grey, Colors.grey.shade800),
    };
    final label = _runStatusLabel(l, status);
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

  Widget _errorState(String message, Future<void> Function() onRetry) {
    final l = AppLocalizations.of(context);
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.error_outline, size: 48, color: Colors.red.shade700),
          const SizedBox(height: 12),
          Text(l.payQueueError),
          const SizedBox(height: 12),
          FilledButton(onPressed: onRetry, child: Text(l.payQueueRetry)),
        ],
      ),
    );
  }
}
