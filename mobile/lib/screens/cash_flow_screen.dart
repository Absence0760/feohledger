import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/cash_flow.dart';
import 'package:feohledger_mobile/stores/cash_flow_store.dart';
import 'package:feohledger_mobile/stores/org_currency_store.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/widgets/kpi_card.dart';

/// Predictive cash-flow forecast (CFO / admin). Shows a KPI summary (opening +
/// projected end balance, total committed / pending outflow over the horizon),
/// a low-balance alert when the cash-position threshold is breached, a
/// per-period forecast list (inflow context: committed vs pending outflow), and
/// the running cash-position balance per period. Pull-to-refresh; 30/60/90-day
/// horizon chips. Reached from the Dashboard app-bar (gated to CFO / admin,
/// matching the backend `_CFO_ROLES` gate on the analytics endpoints).
/// Format a server-supplied money display string. The string is the exact
/// figure the backend computed — we never do arithmetic on money on the device
/// — and [formatMoneyString] keeps every digit of it, returning it verbatim
/// rather than rounding when it cannot be formatted losslessly.
String _money(String display, String? currency) =>
    formatMoneyString(display, currency: currency);

class CashFlowScreen extends StatefulWidget {
  const CashFlowScreen({super.key});

  @override
  State<CashFlowScreen> createState() => _CashFlowScreenState();
}

class _CashFlowScreenState extends State<CashFlowScreen> {
  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      CashFlowStore.instance.fetch();
      // The forecast leg's amounts are in the reporting currency and its
      // payload does not name one; this is the fallback for a request whose
      // cash-position sibling landed nothing to read it from.
      OrgCurrencyStore.instance.ensureLoaded();
    });
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.cashFlowTitle)),
      body: ListenableBuilder(
        // Both stores: the currency can land after the figures, and a figure
        // must pick up its symbol when it does rather than stay bare.
        listenable: Listenable.merge(
          [CashFlowStore.instance, OrgCurrencyStore.instance],
        ),
        builder: (context, _) {
          final store = CashFlowStore.instance;

          if (store.loading && store.data == null) {
            return const Center(child: CircularProgressIndicator());
          }

          if (store.error != null && store.data == null) {
            return Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(l.cashFlowErrorPrefix(store.error.toString())),
                  const SizedBox(height: 16),
                  FilledButton(
                    onPressed: store.fetch,
                    child: Text(l.commonRetry),
                  ),
                ],
              ),
            );
          }

          final data = store.data;
          if (data == null) return const SizedBox.shrink();

          // One currency for the whole screen — the server denominated both
          // legs of this request in it. `null` (unnamed payload and an
          // unresolved org setting) renders every figure bare rather than
          // stamping a symbol nothing established.
          final currency =
              data.openingBalanceCurrency ?? OrgCurrencyStore.instance.currency;

          return RefreshIndicator(
            onRefresh: store.fetch,
            child: ListView(
              padding: const EdgeInsets.all(16),
              children: [
                _horizonChips(l, store),
                const SizedBox(height: 16),
                if (data.hasBreach) ...[
                  _lowBalanceAlert(l, data, currency),
                  const SizedBox(height: 16),
                ],
                _kpiSummary(l, data, currency),
                const SizedBox(height: 24),
                _forecastSection(context, l, data, currency),
                const SizedBox(height: 24),
                _positionSection(context, l, data, currency),
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _horizonChips(AppLocalizations l, CashFlowStore store) {
    return Row(
      children: [
        for (final days in CashFlowStore.horizonOptions) ...[
          ChoiceChip(
            label: Text(l.cashFlowHorizonDays(days)),
            selected: store.horizonDays == days,
            onSelected: (_) => store.setHorizon(days),
          ),
          const SizedBox(width: 8),
        ],
      ],
    );
  }

  Widget _lowBalanceAlert(
      AppLocalizations l, CashFlowData data, String? currency) {
    final count = data.breaches.length;
    final worst = data.breaches.reduce((a, b) {
      final av = num.tryParse(a.shortfallDisplay) ?? 0;
      final bv = num.tryParse(b.shortfallDisplay) ?? 0;
      return av >= bv ? a : b;
    });
    final message = count == 1
        ? l.cashFlowBreachSingle(
            data.thresholdDisplay != null
                ? _money(data.thresholdDisplay!, currency)
                : l.cashFlowMinimum,
            worst.period,
            _money(worst.shortfallDisplay, currency),
          )
        : l.cashFlowBreachMultiple(
            count,
            worst.period,
            _money(worst.shortfallDisplay, currency),
          );
    return Semantics(
      label: l.cashFlowLowBalanceAlertLabel(message),
      excludeSemantics: true,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
        decoration: BoxDecoration(
          color: Colors.red.shade50,
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: Colors.red.shade200),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.warning_amber_rounded,
                size: 20, color: Colors.red.shade800),
            const SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    l.cashFlowLowBalanceAlert,
                    style: TextStyle(
                      fontWeight: FontWeight.w700,
                      // shade900 clears AA contrast on the 0.x-alpha tint.
                      color: Colors.red.shade900,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    message,
                    style: TextStyle(color: Colors.red.shade900, fontSize: 13),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _kpiSummary(
      AppLocalizations l, CashFlowData data, String? currency) {
    final endColor = data.hasBreach ? Colors.red : Colors.green;
    return Column(
      children: [
        Row(
          children: [
            Expanded(
              child: KpiCard(
                title: l.cashFlowOpeningBalance,
                value: _money(data.openingBalanceDisplay, currency),
                subtitle: _openingSourceLabel(l, data.openingBalanceSource),
                icon: Icons.account_balance,
                color: Colors.blue,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: KpiCard(
                title: l.cashFlowProjectedEnd,
                value: _money(data.projectedEndBalanceDisplay, currency),
                subtitle: l.cashFlowProjectedEndSubtitle(data.horizonDays),
                icon: Icons.trending_up,
                color: endColor,
              ),
            ),
          ],
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            Expanded(
              child: KpiCard(
                title: l.cashFlowCommittedOut,
                value: _money(data.totals.committedAmountDisplay, currency),
                subtitle: l.cashFlowCommittedSubtitle,
                icon: Icons.lock_clock,
                color: Colors.deepOrange,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: KpiCard(
                title: l.cashFlowPendingOut,
                value: _money(data.totals.pendingAmountDisplay, currency),
                subtitle: l.cashFlowPendingSubtitle,
                icon: Icons.pending_actions,
                color: Colors.amber.shade700,
              ),
            ),
          ],
        ),
      ],
    );
  }

  String _openingSourceLabel(AppLocalizations l, String source) =>
      switch (source) {
        'provider' => l.cashFlowOpeningSourceProvider,
        'settings' => l.cashFlowOpeningSourceSettings,
        'query' => l.cashFlowOpeningSourceQuery,
        _ => l.cashFlowOpeningSourceUnset,
      };

  Widget _forecastSection(BuildContext context, AppLocalizations l,
      CashFlowData data, String? currency) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          l.cashFlowProjectedOutflows,
          style: Theme.of(context).textTheme.titleMedium,
        ),
        const SizedBox(height: 8),
        if (data.forecastPeriods.isEmpty)
          _emptyCard(l.cashFlowNoOutflows)
        else
          ...data.forecastPeriods.map((p) => _forecastRow(l, p, currency)),
      ],
    );
  }

  Widget _forecastRow(
      AppLocalizations l, CashFlowForecastPeriod p, String? currency) {
    // One announcement per row instead of period + four money fragments.
    return Semantics(
      label: l.cashFlowForecastRowLabel(
        p.period,
        _money(p.scheduledAmountDisplay, currency),
        _money(p.committedAmountDisplay, currency),
        _money(p.pendingAmountDisplay, currency),
        p.count,
      ),
      excludeSemantics: true,
      child: Card(
        elevation: 0,
        margin: const EdgeInsets.only(bottom: 8),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(color: Colors.grey.shade200),
        ),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      p.period,
                      style: const TextStyle(fontWeight: FontWeight.w600),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      l.cashFlowInvoiceCount(p.count),
                      style:
                          TextStyle(color: Colors.grey.shade700, fontSize: 12),
                    ),
                  ],
                ),
              ),
              Column(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Text(
                    _money(p.scheduledAmountDisplay, currency),
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    l.cashFlowCommittedAmount(_money(p.committedAmountDisplay, currency)),
                    style:
                        TextStyle(color: Colors.grey.shade700, fontSize: 11),
                  ),
                  Text(
                    l.cashFlowPendingAmount(_money(p.pendingAmountDisplay, currency)),
                    style:
                        TextStyle(color: Colors.grey.shade700, fontSize: 11),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _positionSection(BuildContext context, AppLocalizations l,
      CashFlowData data, String? currency) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          l.cashFlowPosition,
          style: Theme.of(context).textTheme.titleMedium,
        ),
        const SizedBox(height: 8),
        if (data.positionPeriods.isEmpty)
          _emptyCard(l.cashFlowNoPosition)
        else
          ...data.positionPeriods.map((p) => _positionRow(l, p, currency)),
      ],
    );
  }

  Widget _positionRow(
      AppLocalizations l, CashPositionPeriod p, String? currency) {
    final breach = p.belowThreshold;
    // shade900 keeps the red closing balance legible at AA on white.
    final closingColor = breach ? Colors.red.shade900 : Colors.black87;
    return Semantics(
      label: l.cashFlowPositionRowLabel(
            p.period,
            _money(p.openingDisplay, currency),
            _money(p.outflowDisplay, currency),
            _money(p.closingDisplay, currency),
          ) +
          (breach ? l.cashFlowBelowThresholdSuffix : ''),
      excludeSemantics: true,
      child: Card(
        elevation: 0,
        margin: const EdgeInsets.only(bottom: 8),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(
            color: breach ? Colors.red.shade200 : Colors.grey.shade200,
          ),
        ),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Row(
            children: [
              if (breach) ...[
                Icon(Icons.warning_amber_rounded,
                    size: 18, color: Colors.red.shade800),
                const SizedBox(width: 8),
              ],
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      p.period,
                      style: const TextStyle(fontWeight: FontWeight.w600),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      l.cashFlowOutAmount(_money(p.outflowDisplay, currency)),
                      style:
                          TextStyle(color: Colors.grey.shade700, fontSize: 12),
                    ),
                  ],
                ),
              ),
              Text(
                _money(p.closingDisplay, currency),
                style: TextStyle(
                  fontWeight: FontWeight.w700,
                  color: closingColor,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _emptyCard(String message) {
    return Card(
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: Colors.grey.shade200),
      ),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Text(
          message,
          // shade700 keeps the empty-state copy at AA contrast.
          style: TextStyle(color: Colors.grey.shade700),
        ),
      ),
    );
  }
}
