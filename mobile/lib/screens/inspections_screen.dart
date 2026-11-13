import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/screens/inspection_detail_screen.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/inspection_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/widgets/inspection_list_tile.dart';
import 'package:feohledger_mobile/widgets/record_inspection_sheet.dart';

/// Quality inspections — the 4th leg of 4-way matching.
///
/// List (role-open, like the backend `GET /api/inspections`) with server-side
/// outcome chips, and a record-an-inspection write gated to admin / ap_manager
/// (`POST /api/inspections`' `require_roles`). A clerk sees every inspection and
/// no record affordance — the same split the web `/goods-receipts` Inspections
/// tab applies, and the reason the *list* is not hidden from them: a failed
/// inspection is what a clerk chasing a quality hold needs to read.
///
/// Why a phone: recording the outcome of a delivery inspection is work done at
/// the receiving dock, away from a desk. Syncing from a QMS is not — that needs
/// org-level configuration and stays on the web.
class InspectionsScreen extends StatefulWidget {
  const InspectionsScreen({super.key});

  @override
  State<InspectionsScreen> createState() => _InspectionsScreenState();
}

class _InspectionsScreenState extends State<InspectionsScreen> {
  /// True while the receipt page behind the record form is loading, so a second
  /// tap can't open two sheets.
  bool _opening = false;

  bool get _canRecord => AuthStore.instance.canRecordInspection;

  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      InspectionStore.instance.fetch();
    });
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.inspectionsTitle)),
      floatingActionButton: _canRecord
          ? FloatingActionButton.extended(
              onPressed: _opening ? null : _record,
              icon: _opening
                  ? const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.fact_check_outlined),
              label: Text(l.inspectionsRecord),
            )
          : null,
      body: Column(
        children: [
          SizedBox(
            height: 48,
            child: ListenableBuilder(
              listenable: InspectionStore.instance,
              builder: (context, _) {
                final current = InspectionStore.instance.resultFilter;
                return ListView(
                  scrollDirection: Axis.horizontal,
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  children: [
                    _filterChip(l.commonAll, null, current),
                    _filterChip(
                      l.inspectionsResultPass,
                      InspectionResult.pass,
                      current,
                    ),
                    _filterChip(
                      l.inspectionsResultFail,
                      InspectionResult.fail,
                      current,
                    ),
                    _filterChip(
                      l.inspectionsResultPartial,
                      InspectionResult.partial,
                      current,
                    ),
                  ],
                );
              },
            ),
          ),
          Expanded(
            child: ListenableBuilder(
              listenable: InspectionStore.instance,
              builder: (context, _) {
                final store = InspectionStore.instance;

                if (store.loading && store.inspections.isEmpty) {
                  return const Center(child: CircularProgressIndicator());
                }
                if (store.error != null && store.inspections.isEmpty) {
                  return _ErrorState(onRetry: store.fetch);
                }
                if (store.inspections.isEmpty) {
                  return Center(
                    child: Padding(
                      padding: const EdgeInsets.all(24),
                      child: Text(
                        store.resultFilter == null
                            ? l.inspectionsEmpty
                            : l.inspectionsEmptyFiltered,
                        textAlign: TextAlign.center,
                      ),
                    ),
                  );
                }

                return RefreshIndicator(
                  onRefresh: store.fetch,
                  child: ListView.separated(
                    // See the note in adaptive_screen: a one-row list cannot be
                    // overscrolled, so pull-to-refresh would do nothing.
                    physics: const AlwaysScrollableScrollPhysics(),
                    itemCount: store.inspections.length,
                    separatorBuilder: (_, _) => const Divider(height: 1),
                    itemBuilder: (context, index) {
                      final inspection = store.inspections[index];
                      return InspectionListTile(
                        inspection: inspection,
                        onTap: () => Navigator.of(context).push(
                          MaterialPageRoute(
                            builder: (_) => InspectionDetailScreen(
                              inspectionId: inspection.id,
                            ),
                          ),
                        ),
                      );
                    },
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  Widget _filterChip(
    String label,
    InspectionResult? value,
    InspectionResult? current,
  ) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      child: FilterChip(
        label: Text(label),
        selected: current == value,
        // The filter goes to the server (`?result=`), not to the loaded page:
        // the list is paginated, so narrowing on the device would hide every
        // matching row past the page boundary.
        onSelected: (_) => InspectionStore.instance.setResultFilter(value),
      ),
    );
  }

  /// Open the record form, then POST whatever it returns.
  ///
  /// The receipt page is loaded *before* the sheet opens, because an inspection
  /// has to name the delivery it covers — a sheet that opened first would have to
  /// render its own empty picker while fetching, and the one thing the form
  /// cannot be submitted without is the thing it would still be waiting for.
  Future<void> _record() async {
    final l = AppLocalizations.of(context);
    final store = InspectionStore.instance;
    setState(() => _opening = true);
    final receipts = await store.loadReceiptOptions();
    if (!mounted) return;
    setState(() => _opening = false);

    if (receipts == null) {
      final message = l.inspectionsReceiptsLoadFailed(store.error ?? '');
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(message)));
      A11y.announce(context, message);
      return;
    }

    final draft = await showModalBottomSheet<InspectionDraft>(
      context: context,
      isScrollControlled: true,
      builder: (_) => RecordInspectionSheet(
        receipts: receipts.items,
        totalReceipts: receipts.total,
      ),
    );
    if (draft == null || !mounted) return;

    final created = await store.record(draft);
    if (!mounted) return;
    final message = created != null
        ? l.inspectionsRecorded(created.inspectionNumber)
        : l.inspectionsRecordFailed(store.error ?? '');
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(SnackBar(content: Text(message)));
    A11y.announce(context, message);
  }
}

class _ErrorState extends StatelessWidget {
  final Future<void> Function() onRetry;

  const _ErrorState({required this.onRetry});

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.error_outline, size: 48, color: Colors.red.shade700),
          const SizedBox(height: 12),
          Text(l.inspectionsLoadError),
          const SizedBox(height: 12),
          FilledButton(onPressed: onRetry, child: Text(l.commonRetry)),
        ],
      ),
    );
  }
}
