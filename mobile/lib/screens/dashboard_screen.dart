import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/stores/dashboard_store.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/widgets/cash_flow_button.dart';
import 'package:feohledger_mobile/widgets/kpi_card.dart';
import 'package:feohledger_mobile/widgets/notification_bell.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key});

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      DashboardStore.instance.fetch();
    });
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(l.dashboardTitle),
        actions: const [CashFlowButton(), NotificationBell()],
      ),
      body: ListenableBuilder(
        listenable: DashboardStore.instance,
        builder: (context, _) {
          final store = DashboardStore.instance;

          if (store.loading && store.data == null) {
            return const Center(child: CircularProgressIndicator());
          }

          if (store.error != null && store.data == null) {
            return Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(l.dashboardErrorPrefix('${store.error}')),
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

          return RefreshIndicator(
            onRefresh: store.fetch,
            child: ListView(
              padding: const EdgeInsets.all(16),
              children: [
                // Offline indicator — data is being served from the local
                // cache because the last refresh couldn't reach the server.
                if (store.fromCache) ...[
                  _cacheBanner(l),
                  const SizedBox(height: 12),
                ],

                // KPI row
                Row(
                  children: [
                    Expanded(
                      child: KpiCard(
                        title: l.dashboardTotalInvoices,
                        value: data.totalInvoices.toString(),
                        subtitle: formatMoneyCompact(
                          data.totalAmount,
                          currency: data.reportingCurrency,
                        ),
                        icon: Icons.receipt_long,
                        color: Colors.blue,
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: KpiCard(
                        title: l.dashboardUpcoming,
                        value: data.upcoming.count.toString(),
                        subtitle: formatMoneyCompact(
                          data.upcoming.totalAmount,
                          currency: data.reportingCurrency,
                        ),
                        icon: Icons.schedule,
                        color: Colors.orange,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: KpiCard(
                        title: l.dashboardForReview,
                        value: (data.pipeline['ready_for_review'] ?? 0)
                            .toString(),
                        icon: Icons.rate_review,
                        color: Colors.amber.shade700,
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: KpiCard(
                        title: l.dashboardApproved,
                        value: (data.pipeline['approved'] ?? 0).toString(),
                        icon: Icons.check_circle,
                        color: Colors.green,
                      ),
                    ),
                  ],
                ),

                // Aging
                const SizedBox(height: 24),
                Text(
                  l.dashboardAging,
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                const SizedBox(height: 8),
                Card(
                  elevation: 0,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                    side: BorderSide(color: Colors.grey.shade200),
                  ),
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Row(
                      children: [
                        _agingBucket(
                          l.dashboardAgingCurrent,
                          data.aging.current,
                          Colors.green,
                          currency: data.reportingCurrency,
                        ),
                        _agingBucket(
                          l.dashboardAgingDays30,
                          data.aging.thirtyDays,
                          Colors.amber,
                          currency: data.reportingCurrency,
                        ),
                        _agingBucket(
                          l.dashboardAgingDays60,
                          data.aging.sixtyDays,
                          Colors.orange,
                          currency: data.reportingCurrency,
                        ),
                        _agingBucket(
                          l.dashboardAgingDays90plus,
                          data.aging.ninetyPlus,
                          Colors.red,
                          currency: data.reportingCurrency,
                        ),
                      ],
                    ),
                  ),
                ),

                // Top vendors
                const SizedBox(height: 24),
                Text(
                  l.dashboardTopVendors,
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                const SizedBox(height: 8),
                ...data.topVendors.take(5).map(
                      (v) => ListTile(
                        dense: true,
                        title: Text(v.vendorName),
                        trailing: Text(
                          formatMoneyCompact(
                            v.totalAmount,
                            currency: data.reportingCurrency,
                          ),
                          style: const TextStyle(fontWeight: FontWeight.w600),
                        ),
                        subtitle: Text(l.dashboardInvoiceCount(v.invoiceCount)),
                      ),
                    ),
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _cacheBanner(AppLocalizations l) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: Colors.amber.shade50,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: Colors.amber.shade200),
      ),
      child: Row(
        children: [
          Icon(Icons.cloud_off, size: 18, color: Colors.amber.shade800),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              l.dashboardCachedBanner,
              style: TextStyle(color: Colors.amber.shade900, fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }

  Widget _agingBucket(
    String label,
    double amount,
    Color color, {
    required String? currency,
  }) {
    final figure = formatMoneyCompact(amount, currency: currency);
    // One announcement per bucket ("Current: $1,234") rather than the dot +
    // amount + label read as three fragments (WCAG 1.3.1).
    return Expanded(
      child: Semantics(
        label: '$label: $figure',
        excludeSemantics: true,
        child: Column(
          children: [
            Container(
              width: 8,
              height: 8,
              decoration: BoxDecoration(color: color, shape: BoxShape.circle),
            ),
            const SizedBox(height: 4),
            Text(
              figure,
              style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13),
            ),
            Text(
              label,
              // shade700 keeps the small (11px) label at AA contrast.
              style: TextStyle(color: Colors.grey.shade700, fontSize: 11),
            ),
          ],
        ),
      ),
    );
  }
}
