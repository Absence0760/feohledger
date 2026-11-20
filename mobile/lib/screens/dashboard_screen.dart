import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/stores/dashboard_store.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/widgets/async_list_body.dart';
import 'package:feohledger_mobile/widgets/cash_flow_button.dart';
import 'package:feohledger_mobile/widgets/kpi_card.dart';
import 'package:feohledger_mobile/widgets/load_error_state.dart';
import 'package:feohledger_mobile/widgets/notification_bell.dart';
import 'package:feohledger_mobile/widgets/partial_conversion_note.dart';

class DashboardScreen extends StatefulWidget {
  /// Opens the Invoices tab on its Overdue quick view, the rows the Overdue
  /// card counts (`GET /api/invoices?overdue=true`). The home shell owns the
  /// tabs, so it supplies this; without it the card is not tappable.
  final VoidCallback? onShowOverdue;

  const DashboardScreen({super.key, this.onShowOverdue});

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

          // First load only: a refresh keeps the figures on screen.
          if (store.loading && store.data == null) {
            return Center(
              child: CircularProgressIndicator(semanticsLabel: l.commonLoading),
            );
          }

          // First load failed and there is no cached copy to fall back on.
          // The sentence is localized and says what to do next — never the
          // exception's `toString()`.
          if (store.error != null && store.data == null) {
            return LoadErrorState(
              message: l.dashboardLoadError,
              onRetry: store.fetch,
            );
          }

          final data = store.data;
          if (data == null) return const SizedBox.shrink();

          return RefreshIndicator(
            onRefresh: store.fetch,
            child: ListView(
              // Pull-to-refresh must work even when the content is shorter
              // than the viewport (an empty book, a large screen).
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.all(16),
              children: _sections(l, store, data),
            ),
          );
        },
      ),
    );
  }

  List<Widget> _sections(
    AppLocalizations l,
    DashboardStore store,
    DashboardData data,
  ) {
    // What the part-conversion disclosures name as the currency the
    // unconverted rows could not reach. The payload's own code whenever it
    // names one; the counts ride the same `reporting` era of the payload, so
    // the fallback is a guard, not a path anyone expects.
    final disclosedCurrency =
        data.reportingCurrency ?? l.partialConversionCurrencyFallback;
    // The rows ACTUALLY rendered below — the disclosure has to describe the
    // ranking the reader sees, not vendors the list never shows.
    final vendors = data.topVendors.take(5).toList();
    // A sum of row COUNTS, not money.
    final vendorsUnconverted = vendors.fold<int>(
      0,
      (n, v) => n + v.unconvertedCount,
    );
    final overdue = data.overdue;
    final dueSoon = data.dueSoon;

    return [
      // Status first: is the user looking at live figures?
      if (store.fromCache) ...[
        StaleDataBanner(
          icon: Icons.cloud_off,
          message: l.dashboardCachedBanner,
          onRetry: store.fetch,
        ),
        const SizedBox(height: 12),
      ] else if (store.error != null) ...[
        // A refresh failed with no cached copy behind it: the figures below
        // are the last ones loaded, and the user should know.
        StaleDataBanner(
          icon: Icons.sync_problem,
          message: l.dashboardRefreshFailed,
          onRetry: store.fetch,
        ),
        const SizedBox(height: 12),
      ],

      // An empty book is not an error and not a row of zeros to decode.
      if (data.totalInvoices == 0) ...[
        _EmptyNotice(message: l.dashboardEmpty),
        const SizedBox(height: 16),
      ],

      // 1. What is waiting on the user, and what is coming due. These lead
      // because they are the two figures an approver acts on today.
      _SectionHeader(l.dashboardNeedsAttention),
      const SizedBox(height: 8),
      Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: KpiCard(
              title: l.dashboardForReview,
              value: (data.pipeline['ready_for_review'] ?? 0).toString(),
              icon: Icons.rate_review,
              color: Colors.amber.shade700,
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            // Upcoming means due soon: today through seven days out, counted
            // over the whole book (`due_soon_*`). Only a payload predating
            // those fields falls back to the capped upcoming list, which also
            // holds the overdue rows.
            child: dueSoon != null
                ? KpiCard(
                    title: l.dashboardUpcoming,
                    value: dueSoon.count.toString(),
                    subtitle: _perCurrency(dueSoon),
                    icon: Icons.schedule,
                    color: Colors.orange,
                  )
                : KpiCard(
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
      // Owed now, over the whole book (`overdue_*`, the predicate the web's
      // /cfo overdue card reads). Absent, not `0`, when the payload predates
      // the field.
      if (overdue != null) ...[
        const SizedBox(height: 12),
        KpiCard(
          title: l.dashboardOverdue,
          value: overdue.count.toString(),
          subtitle: _perCurrency(overdue),
          icon: Icons.warning_amber_rounded,
          color: Colors.red.shade700,
          // The list behind the figure: the same predicate over the whole
          // book, so it holds exactly the invoices counted here.
          onTap: widget.onShowOverdue,
          semanticHint: l.dashboardOverdueOpen,
        ),
      ],
      // The legacy Upcoming figure is a reporting-currency rollup that keeps
      // an unconvertible row in at FACE value rather than drop it (dropping
      // would understate what is due), so it says so beside itself when it
      // happened. The due-soon figure is per currency and converts nothing.
      if (dueSoon == null && data.upcoming.unconvertedCount > 0)
        PartialConversionNote(
          l.dashboardKpiUnconverted(
            l.dashboardUpcoming,
            data.upcoming.unconvertedCount,
            disclosedCurrency,
          ),
        ),

      // 2. What is overdue, by how long.
      const SizedBox(height: 24),
      _SectionHeader(l.dashboardAging),
      const SizedBox(height: 8),
      // ONE note for the band set, matching the single
      // `aging_reporting.unconverted_count` the API serves: which band an
      // unconverted invoice fell into does not change what the reader should
      // do about it.
      if (data.aging.unconvertedCount > 0) ...[
        PartialConversionNote(
          l.dashboardAgingUnconverted(
            data.aging.unconvertedCount,
            disclosedCurrency,
          ),
        ),
        const SizedBox(height: 4),
      ],
      _AgingCard(
        currency: data.reportingCurrency,
        buckets: [
          (l.dashboardAgingCurrent, data.aging.current, Colors.green),
          (l.dashboardAgingDays30, data.aging.thirtyDays, Colors.amber),
          (l.dashboardAgingDays60, data.aging.sixtyDays, Colors.orange),
          (l.dashboardAgingDays90plus, data.aging.ninetyPlus, Colors.red),
        ],
      ),

      // 3. The book as a whole — context, not a to-do.
      const SizedBox(height: 24),
      _SectionHeader(l.dashboardOverview),
      const SizedBox(height: 8),
      Row(
        crossAxisAlignment: CrossAxisAlignment.start,
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
              title: l.dashboardApproved,
              value: (data.pipeline['approved'] ?? 0).toString(),
              icon: Icons.check_circle,
              color: Colors.green,
            ),
          ),
        ],
      ),
      if (data.unconvertedCount > 0)
        PartialConversionNote(
          l.dashboardKpiUnconverted(
            l.dashboardTotalInvoices,
            data.unconvertedCount,
            disclosedCurrency,
          ),
        ),

      // 4. Top vendors.
      const SizedBox(height: 24),
      _SectionHeader(l.dashboardTopVendors),
      const SizedBox(height: 8),
      // A vendor with an unconverted invoice is ranked on a total that is not
      // in the same currency as the rows around it. The section note says what
      // that means; the row's own subtitle says WHICH vendors.
      if (vendorsUnconverted > 0)
        PartialConversionNote(
          l.dashboardTopVendorsUnconverted(
            vendorsUnconverted,
            disclosedCurrency,
          ),
        ),
      if (vendors.isEmpty)
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 8),
          child: Text(
            l.dashboardTopVendorsEmpty,
            style: TextStyle(color: Colors.grey.shade700),
          ),
        ),
      ...vendors.map((v) => _vendorRow(l, v, data.reportingCurrency)),
    ];
  }

  /// A due rollup's money, one figure per currency in the invoices' own code
  /// (`€1.2K · $3.4K`), or `null` when nothing is due. Never one converted
  /// total: the server did not convert these, and no sum crosses currencies.
  String? _perCurrency(DueRollup rollup) {
    if (rollup.byCurrency.isEmpty) return null;
    return rollup.byCurrency
        .map((c) => formatMoneyCompact(c.amount, currency: c.currency))
        .join(' · ');
  }

  Widget _vendorRow(AppLocalizations l, VendorSpend v, String? currency) {
    final figure = formatMoneyCompact(v.totalAmount, currency: currency);
    final detail = v.unconvertedCount > 0
        ? '${l.dashboardInvoiceCount(v.invoiceCount)} · '
              '${l.dashboardVendorFaceValueCount(v.unconvertedCount)}'
        : l.dashboardInvoiceCount(v.invoiceCount);
    // One announcement per row ("Acme Corp, $20K, 4 invoices") instead of
    // three fragments in tile order.
    return Semantics(
      label: '${v.vendorName}, $figure, $detail',
      excludeSemantics: true,
      child: ListTile(
        contentPadding: EdgeInsets.zero,
        title: Text(v.vendorName),
        subtitle: Text(detail, style: TextStyle(color: Colors.grey.shade700)),
        trailing: Text(
          figure,
          style: const TextStyle(fontWeight: FontWeight.w600),
        ),
      ),
    );
  }
}

/// A section title a screen-reader user can jump between (headings rotor /
/// TalkBack heading navigation).
class _SectionHeader extends StatelessWidget {
  final String text;

  const _SectionHeader(this.text);

  @override
  Widget build(BuildContext context) {
    return Semantics(
      header: true,
      child: Text(text, style: Theme.of(context).textTheme.titleMedium),
    );
  }
}

class _EmptyNotice extends StatelessWidget {
  final String message;

  const _EmptyNotice({required this.message});

  @override
  Widget build(BuildContext context) {
    return Card(
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: Colors.grey.shade200),
      ),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            ExcludeSemantics(
              child: Icon(Icons.inbox_outlined, color: Colors.grey.shade700),
            ),
            const SizedBox(width: 12),
            Expanded(child: Text(message)),
          ],
        ),
      ),
    );
  }
}

/// The four aging bands. Four across at a normal text size; two across once
/// the user's text scale would squeeze a compact figure ("R45.3K") into a
/// quarter of a 390dp screen and wrap it mid-number.
class _AgingCard extends StatelessWidget {
  final List<(String, double, Color)> buckets;
  final String? currency;

  const _AgingCard({required this.buckets, required this.currency});

  @override
  Widget build(BuildContext context) {
    final scaled = MediaQuery.textScalerOf(context).scale(14) / 14 > 1.3;
    final perRow = scaled ? 2 : 4;
    return Card(
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: Colors.grey.shade200),
      ),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            for (var i = 0; i < buckets.length; i += perRow) ...[
              if (i > 0) const SizedBox(height: 12),
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  for (final b in buckets.skip(i).take(perRow))
                    Expanded(child: _bucket(b.$1, b.$2, b.$3)),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _bucket(String label, double amount, Color color) {
    final figure = formatMoneyCompact(amount, currency: currency);
    // One announcement per bucket ("Current: $1,234") rather than the dot +
    // amount + label read as three fragments (WCAG 1.3.1). The dot is a
    // decorative cue; the label carries the meaning.
    return Semantics(
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
            textAlign: TextAlign.center,
            style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13),
          ),
          Text(
            label,
            textAlign: TextAlign.center,
            // shade700 keeps the small (11px) label at AA contrast.
            style: TextStyle(color: Colors.grey.shade700, fontSize: 11),
          ),
        ],
      ),
    );
  }
}
