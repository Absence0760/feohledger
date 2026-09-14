import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/adaptive.dart';
import 'package:feohledger_mobile/stores/adaptive_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/org_currency_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/utils/money.dart';

/// Adaptive AI workflows — advisory suggestions, approval patterns and baseline
/// anomalies, over `/api/adaptive`.
///
/// **Read-first, deliberately.** The backend also offers two APPLY paths
/// (assign the top-ranked approver to an invoice; raise the org-wide
/// auto-approve threshold). Neither is here: both change live approval controls
/// — the second edits the active workflow definition and governs which invoices
/// skip human review entirely — and the threshold apply carries a stale-value
/// optimistic guard whose 409 needs a real "the recommendation changed, nothing
/// was applied" surface to land safely. A phone is the wrong place to take that
/// decision, so it stays on the web `/adaptive` page. The one write here is
/// dismissing a suggestion, which changes an advisory row's status and nothing
/// else.
///
/// Everything rendered is a deterministic statistic over the tenant's own
/// approval history — no model, nothing sent anywhere, recomputed on every read.
/// Reached from Settings, gated on `AuthStore.canViewAdaptive` (the backend
/// `_READ_ROLES`: admin / ap_manager / cfo).
class AdaptiveScreen extends StatefulWidget {
  const AdaptiveScreen({super.key});

  @override
  State<AdaptiveScreen> createState() => _AdaptiveScreenState();
}

class _AdaptiveScreenState extends State<AdaptiveScreen>
    with SingleTickerProviderStateMixin {
  late final TabController _tabs;

  /// True while a dismiss POST is in flight — guards against a double-tap.
  bool _submitting = false;

  bool get _canDismiss => AuthStore.instance.canDismissSuggestion;

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: 3, vsync: this);
    SchedulerBinding.instance.addPostFrameCallback((_) {
      // Each section is its own request with its own state, so all three are
      // kicked off together rather than lazily per tab: the payloads are small
      // read models and a reader who opens the screen is looking for whichever
      // tab answers their question first.
      final store = AdaptiveStore.instance;
      store.fetchSuggestions();
      store.fetchPatterns();
      store.fetchAnomalies();
      // The patterns tab's per-vendor averages are in the org's reporting
      // currency and `approval-patterns` does not name it, so the code comes
      // from the settings rungs the server resolved to denominate them.
      OrgCurrencyStore.instance.ensureLoaded();
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
        title: Text(l.adaptiveTitle),
        bottom: TabBar(
          controller: _tabs,
          tabs: [
            Tab(text: l.adaptiveTabSuggestions),
            Tab(text: l.adaptiveTabPatterns),
            Tab(text: l.adaptiveTabAnomalies),
          ],
        ),
      ),
      body: TabBarView(
        controller: _tabs,
        children: [_suggestionsTab(), _patternsTab(), _anomaliesTab()],
      ),
    );
  }

  // ── Suggestions ────────────────────────────────────────────────────
  Widget _suggestionsTab() {
    return ListenableBuilder(
      listenable: AdaptiveStore.instance,
      builder: (context, _) {
        final l = AppLocalizations.of(context);
        final store = AdaptiveStore.instance;

        if (store.suggestionsLoading && store.suggestions.isEmpty) {
          return const Center(child: CircularProgressIndicator());
        }
        if (store.suggestionsError != null && store.suggestions.isEmpty) {
          return _ErrorState(
            message: l.adaptiveSuggestionsError,
            onRetry: store.fetchSuggestions,
          );
        }

        return RefreshIndicator(
          onRefresh: store.fetchSuggestions,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 24),
            // A short list (one card, or an empty state) is not scrollable
            // on its own, and a RefreshIndicator it cannot overscroll is a
            // gesture that silently does nothing — exactly when "has
            // anything changed yet?" is the question being asked.
            physics: const AlwaysScrollableScrollPhysics(),
            children: [
              _note(l.adaptiveAdvisoryNote),
              const SizedBox(height: 12),
              Row(
                children: [
                  _statusChip(l.adaptiveSuggestionsShowOpen, 'open'),
                  const SizedBox(width: 8),
                  _statusChip(l.adaptiveSuggestionsShowAll, 'all'),
                ],
              ),
              const SizedBox(height: 8),
              if (store.suggestions.isEmpty)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 32),
                  child: Text(
                    l.adaptiveSuggestionsEmpty,
                    textAlign: TextAlign.center,
                  ),
                )
              else
                for (final s in store.suggestions) _suggestionCard(l, s),
            ],
          ),
        );
      },
    );
  }

  Widget _statusChip(String label, String value) {
    final selected = AdaptiveStore.instance.suggestionStatus == value;
    return FilterChip(
      label: Text(label),
      selected: selected,
      onSelected: (_) => AdaptiveStore.instance.setSuggestionStatus(value),
    );
  }

  Widget _suggestionCard(AppLocalizations l, WorkflowSuggestion s) {
    // One merged announcement for the card; the Dismiss button keeps its own
    // (it sits outside this Semantics node).
    final announce = [
      s.title,
      l.adaptiveSuggestionsConfidence(s.confidencePct),
      _suggestionStatusLabel(l, s.status),
      ?s.rationale,
    ].join(', ');

    return Card(
      elevation: 0,
      margin: const EdgeInsets.only(bottom: 12),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: Colors.grey.shade300),
      ),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Semantics(
              label: announce,
              excludeSemantics: true,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  // `title` and `rationale` are sentences the server composes
                  // (numbers and currency code included) and are rendered
                  // verbatim — same as the web page does.
                  Text(
                    s.title,
                    style: const TextStyle(fontWeight: FontWeight.w600),
                  ),
                  if (s.rationale != null && s.rationale!.isNotEmpty) ...[
                    const SizedBox(height: 6),
                    Text(
                      s.rationale!,
                      style: TextStyle(
                        color: Colors.grey.shade700,
                        fontSize: 13,
                      ),
                    ),
                  ],
                  const SizedBox(height: 10),
                  Row(
                    children: [
                      _SuggestionStatusBadge(status: s.status),
                      const SizedBox(width: 8),
                      Text(
                        l.adaptiveSuggestionsConfidence(s.confidencePct),
                        style: TextStyle(
                          color: Colors.grey.shade700,
                          fontSize: 12,
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
            // Dismiss is offered only on an open row and only to the roles the
            // backend `_WRITE_ROLES` admits — a CFO reads and is not offered it.
            if (_canDismiss && s.isOpen)
              Align(
                alignment: Alignment.centerRight,
                child: TextButton.icon(
                  onPressed: _submitting ? null : () => _dismiss(s),
                  icon: const Icon(Icons.visibility_off_outlined),
                  label: Text(l.adaptiveSuggestionsDismiss),
                ),
              ),
          ],
        ),
      ),
    );
  }

  Future<void> _dismiss(WorkflowSuggestion s) async {
    final l = AppLocalizations.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(l.adaptiveSuggestionsDismissTitle),
        content: Text(l.adaptiveSuggestionsDismissBody),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: Text(l.commonCancel),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(l.adaptiveSuggestionsDismiss),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    setState(() => _submitting = true);
    final ok = await AdaptiveStore.instance.dismissSuggestion(s.id);
    if (!mounted) return;
    setState(() => _submitting = false);
    final message = ok
        ? l.adaptiveSuggestionsDismissed
        : l.adaptiveSuggestionsDismissFailed(
            AdaptiveStore.instance.suggestionsError ?? '',
          );
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(SnackBar(content: Text(message)));
    A11y.announce(context, message);
  }

  // ── Approval patterns ──────────────────────────────────────────────
  Widget _patternsTab() {
    return ListenableBuilder(
      // Both stores: the reporting currency is a separate request and can land
      // after the patterns do, at which point the bare figures must pick up
      // their symbol instead of staying bare for the life of the screen.
      listenable: Listenable.merge(
        [AdaptiveStore.instance, OrgCurrencyStore.instance],
      ),
      builder: (context, _) {
        final l = AppLocalizations.of(context);
        final store = AdaptiveStore.instance;
        final data = store.patterns;
        final currency = OrgCurrencyStore.instance.currency;

        if (store.patternsLoading && data == null) {
          return const Center(child: CircularProgressIndicator());
        }
        if (store.patternsError != null && data == null) {
          return _ErrorState(
            message: l.adaptivePatternsError,
            onRetry: store.fetchPatterns,
          );
        }
        if (data == null) {
          return Center(child: Text(l.adaptivePatternsEmptyApprovers));
        }

        return RefreshIndicator(
          onRefresh: store.fetchPatterns,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 24),
            // A short list (one card, or an empty state) is not scrollable
            // on its own, and a RefreshIndicator it cannot overscroll is a
            // gesture that silently does nothing — exactly when "has
            // anything changed yet?" is the question being asked.
            physics: const AlwaysScrollableScrollPhysics(),
            children: [
              _note(l.adaptivePatternsLookback(data.lookbackDays)),
              const SizedBox(height: 16),
              _sectionHeader(l.adaptivePatternsSectionApprovers),
              if (data.approvers.isEmpty)
                _emptyLine(l.adaptivePatternsEmptyApprovers)
              else
                for (final a in data.approvers) _approverRow(l, a),
              const SizedBox(height: 20),
              _sectionHeader(l.adaptivePatternsSectionVendors),
              // The money figures are in the org's reporting currency, which
              // this payload does not name. With a resolved code each figure
              // carries its own symbol and the note is redundant; with none
              // the figures stay bare and the note is the only thing that says
              // what they are in — so it renders exactly when it is load-
              // bearing, rather than permanently apologising for a `\$` that
              // is no longer there.
              if (currency == null)
                _emptyLine(l.adaptivePatternsCurrencyNote),
              if (data.vendors.isEmpty)
                _emptyLine(l.adaptivePatternsEmptyVendors)
              else
                for (final v in data.vendors) _vendorRow(l, v, currency),
            ],
          ),
        );
      },
    );
  }

  Widget _approverRow(AppLocalizations l, ApproverPattern a) {
    final name = a.approverName ?? l.adaptivePatternsUnknownApprover;
    final summary = l.adaptivePatternsApproverSummary(
      a.approvedCount,
      a.rejectedCount,
    );
    final timing = l.adaptivePatternsApproverTiming(
      a.approvalRatePct,
      a.medianTimeToApproveDays,
    );
    return Semantics(
      label: '$name, $summary, $timing',
      excludeSemantics: true,
      child: ListTile(
        contentPadding: EdgeInsets.zero,
        leading: const Icon(Icons.person_outline),
        title: Text(name, style: const TextStyle(fontWeight: FontWeight.w600)),
        subtitle: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              summary,
              style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
            ),
            Text(
              timing,
              style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
            ),
          ],
        ),
      ),
    );
  }

  Widget _vendorRow(AppLocalizations l, VendorPattern v, String? currency) {
    final consistency = l.adaptivePatternsVendorConsistency(v.consistencyPct);
    final money = l.adaptivePatternsVendorMoney(
      formatMoneyString(v.medianApprovedAmount, currency: currency),
      formatMoneyString(v.avgApprovedAmount, currency: currency),
    );
    final summary = l.adaptivePatternsApproverSummary(
      v.approvedCount,
      v.rejectedCount,
    );
    // Disclosed, not hidden: approvals excluded from the money figures are still
    // counted in the sample, so an average beside a count would otherwise be an
    // average over fewer rows than the count claims.
    final unconverted = v.unconvertedCount > 0
        ? l.adaptivePatternsUnconverted(v.unconvertedCount)
        : null;
    return Semantics(
      label: [v.vendorName, summary, consistency, money, ?unconverted].join(', '),
      excludeSemantics: true,
      child: ListTile(
        contentPadding: EdgeInsets.zero,
        leading: const Icon(Icons.store_outlined),
        title: Text(
          v.vendorName,
          style: const TextStyle(fontWeight: FontWeight.w600),
          overflow: TextOverflow.ellipsis,
        ),
        subtitle: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '$summary · $consistency',
              style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
            ),
            Text(
              money,
              style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
            ),
            if (unconverted != null)
              Text(
                unconverted,
                style: TextStyle(color: Colors.brown.shade800, fontSize: 12),
              ),
          ],
        ),
      ),
    );
  }

  // ── Anomalies ──────────────────────────────────────────────────────
  Widget _anomaliesTab() {
    return ListenableBuilder(
      listenable: AdaptiveStore.instance,
      builder: (context, _) {
        final l = AppLocalizations.of(context);
        final store = AdaptiveStore.instance;
        final batch = store.anomalies;

        if (store.anomaliesLoading && batch == null) {
          return const Center(child: CircularProgressIndicator());
        }
        if (store.anomaliesError != null && batch == null) {
          return _ErrorState(
            message: l.adaptiveAnomaliesError,
            onRetry: store.fetchAnomalies,
          );
        }
        if (batch == null) {
          return Center(child: Text(l.adaptiveAnomaliesEmpty));
        }

        return RefreshIndicator(
          onRefresh: store.fetchAnomalies,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 24),
            // A short list (one card, or an empty state) is not scrollable
            // on its own, and a RefreshIndicator it cannot overscroll is a
            // gesture that silently does nothing — exactly when "has
            // anything changed yet?" is the question being asked.
            physics: const AlwaysScrollableScrollPhysics(),
            children: [
              _note(l.adaptiveAnomaliesIntro),
              const SizedBox(height: 8),
              // The scanned count is what makes an empty list mean "nothing
              // stands out" rather than "nothing was looked at".
              _emptyLine(l.adaptiveAnomaliesScanned(batch.totalScanned)),
              const SizedBox(height: 8),
              if (batch.flagged.isEmpty)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 24),
                  child: Text(
                    l.adaptiveAnomaliesEmpty,
                    textAlign: TextAlign.center,
                  ),
                )
              else
                for (final a in batch.flagged) _anomalyCard(l, a),
            ],
          ),
        );
      },
    );
  }

  Widget _anomalyCard(AppLocalizations l, InvoiceAnomaly a) {
    // `amount_currency` labels the figure — the reporting currency normally, the
    // invoice's own when the backend had no usable rate lock and fell back to
    // the billed amount.
    final amount = a.amountCurrency.isEmpty
        ? a.amount
        : l.adaptiveAnomaliesAmount(a.amount, a.amountCurrency);
    final announce = [
      a.vendorName,
      amount,
      if (a.insufficientHistory) l.adaptiveAnomaliesInsufficient,
      ...a.flags.map((f) => f.message),
    ].join(', ');

    return Semantics(
      label: announce,
      excludeSemantics: true,
      child: Card(
        elevation: 0,
        margin: const EdgeInsets.only(bottom: 12),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(color: Colors.grey.shade300),
        ),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      a.vendorName,
                      style: const TextStyle(fontWeight: FontWeight.w600),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  Text(
                    amount,
                    style: const TextStyle(fontWeight: FontWeight.w600),
                  ),
                ],
              ),
              if (a.insufficientHistory) ...[
                const SizedBox(height: 6),
                Text(
                  l.adaptiveAnomaliesInsufficient,
                  style: TextStyle(color: Colors.grey.shade700, fontSize: 12),
                ),
              ],
              for (final flag in a.flags) ...[
                const SizedBox(height: 8),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    ExcludeSemantics(
                      child: Icon(
                        Icons.flag_outlined,
                        size: 16,
                        color: _severityColor(flag.severity),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        flag.message,
                        style: TextStyle(
                          color: _severityColor(flag.severity),
                          fontSize: 13,
                        ),
                      ),
                    ),
                  ],
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  /// Severity tints, each in the darkened variant that clears AA over white —
  /// `warning` uses the dark brown that reads as deep amber, as the rest of the
  /// app does for orange.
  Color _severityColor(String severity) => switch (severity) {
    'error' => Colors.red.shade900,
    'warning' => Colors.brown.shade800,
    _ => Colors.grey.shade700,
  };

  // ── Shared bits ────────────────────────────────────────────────────
  Widget _note(String text) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: scheme.secondaryContainer,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(
        text,
        style: TextStyle(color: scheme.onSecondaryContainer, fontSize: 13),
      ),
    );
  }

  Widget _sectionHeader(String text) => Padding(
    padding: const EdgeInsets.only(bottom: 4),
    child: Text(
      text,
      style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 15),
    ),
  );

  Widget _emptyLine(String text) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 6),
    child: Text(
      text,
      style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
    ),
  );
}

String _suggestionStatusLabel(AppLocalizations l, SuggestionStatus status) =>
    switch (status) {
      SuggestionStatus.open => l.adaptiveSuggestionStatusOpen,
      SuggestionStatus.dismissed => l.adaptiveSuggestionStatusDismissed,
      SuggestionStatus.applied => l.adaptiveSuggestionStatusApplied,
      SuggestionStatus.stale => l.adaptiveSuggestionStatusStale,
      SuggestionStatus.unknown => l.adaptiveSuggestionStatusUnknown,
    };

/// Status chip for an advisory suggestion — same darkened-text-over-pale-tint
/// construction as the other mobile badges, for AA contrast.
class _SuggestionStatusBadge extends StatelessWidget {
  final SuggestionStatus status;

  const _SuggestionStatusBadge({required this.status});

  Color get _color => switch (status) {
    SuggestionStatus.open => Colors.blue,
    SuggestionStatus.applied => Colors.green,
    SuggestionStatus.dismissed => Colors.blueGrey,
    SuggestionStatus.stale => Colors.grey,
    SuggestionStatus.unknown => Colors.grey,
  };

  Color get _textColor => switch (status) {
    SuggestionStatus.open => Colors.blue.shade900,
    SuggestionStatus.applied => Colors.green.shade900,
    SuggestionStatus.dismissed => Colors.blueGrey.shade900,
    SuggestionStatus.stale => Colors.grey.shade800,
    SuggestionStatus.unknown => Colors.grey.shade800,
  };

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final label = _suggestionStatusLabel(l, status);
    return Semantics(
      label: l.adaptiveSuggestionStatusAnnounce(label),
      excludeSemantics: true,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: _color.withValues(alpha: 0.15),
          borderRadius: BorderRadius.circular(12),
        ),
        child: Text(
          label,
          style: TextStyle(
            color: _textColor,
            fontSize: 12,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
    );
  }
}

class _ErrorState extends StatelessWidget {
  final String message;
  final Future<void> Function() onRetry;

  const _ErrorState({required this.message, required this.onRetry});

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.error_outline, size: 48, color: Colors.red.shade700),
            const SizedBox(height: 12),
            Text(message, textAlign: TextAlign.center),
            const SizedBox(height: 12),
            FilledButton(onPressed: onRetry, child: Text(l.commonRetry)),
          ],
        ),
      ),
    );
  }
}
