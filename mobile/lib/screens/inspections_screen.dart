import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/load_error_messages.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/screens/inspection_detail_screen.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/inspection_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/widgets/async_list_body.dart';
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
                final loadError = store.loadError;

                return AsyncListBody<Inspection>(
                  items: store.inspections,
                  loading: store.loading,
                  // The fetch's failure only — a refused record leaves the
                  // list live (actionError), so it raises no banner.
                  hasError: loadError != null,
                  // Never offline-cached (see InspectionStore): a stale
                  // pass/fail is a wrong answer about payability.
                  fromCache: false,
                  errorMessage: loadError == null
                      ? ''
                      : describeLoadError(
                          l,
                          loadError,
                          l.inspectionsLoadErrorPrefix,
                        ),
                  onRefresh: store.fetch,
                  empty: Text(l.inspectionsEmpty, textAlign: TextAlign.center),
                  filteredEmpty: store.resultFilter == null
                      ? null
                      : FilteredEmptyState(
                          message: l.inspectionsEmptyFiltered,
                          onShowAll: () => store.setResultFilter(null),
                        ),
                  itemBuilder: (context, inspection) => InspectionListTile(
                    inspection: inspection,
                    onTap: () => Navigator.of(context).push(
                      MaterialPageRoute(
                        builder: (_) =>
                            InspectionDetailScreen(inspectionId: inspection.id),
                      ),
                    ),
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
      final message = l.inspectionsReceiptsLoadFailed(store.actionError ?? '');
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(message)));
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
        : l.inspectionsRecordFailed(store.actionError ?? '');
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
    A11y.announce(context, message);
  }
}
