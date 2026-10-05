import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/screens/capture_screen.dart';
import 'package:feohledger_mobile/screens/invoice_detail_screen.dart';
import 'package:feohledger_mobile/services/file_share.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/utils/debouncer.dart';
import 'package:feohledger_mobile/widgets/advanced_search_sheet.dart';
import 'package:feohledger_mobile/widgets/bulk_action_bar.dart';
import 'package:feohledger_mobile/widgets/invoice_list_tile.dart';
import 'package:feohledger_mobile/widgets/reject_reason_dialog.dart';

/// Bulk status-change targets offered on mobile. A deliberately small, safe
/// subset of the 12-state machine — the common manual moves an AP user makes in
/// a batch. The backend validates each row's transition and skips invalid ones,
/// so an option that doesn't apply to a given row is simply a no-op for it.
const _bulkStatusTargets = <InvoiceStatus>[
  InvoiceStatus.readyForReview,
  InvoiceStatus.approved,
  InvoiceStatus.rejected,
  InvoiceStatus.pending,
];

class InvoicesScreen extends StatefulWidget {
  const InvoicesScreen({super.key});

  @override
  State<InvoicesScreen> createState() => _InvoicesScreenState();
}

class _InvoicesScreenState extends State<InvoicesScreen> {
  final _searchController = TextEditingController();
  final _searchDebouncer = Debouncer();
  bool _busy = false;

  bool get _canBulk => AuthStore.instance.canBulkEditInvoices;

  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      InvoiceStore.instance.fetch();
    });
  }

  @override
  void dispose() {
    // Leave selection mode if the screen is torn down mid-selection so the
    // singleton store doesn't strand a stale selection.
    if (InvoiceStore.instance.selectionMode) {
      InvoiceStore.instance.exitSelectionMode();
    }
    _searchDebouncer.cancel();
    _searchController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return ListenableBuilder(
      listenable: InvoiceStore.instance,
      builder: (context, _) {
        final selecting = InvoiceStore.instance.selectionMode;
        return Scaffold(
          appBar: selecting ? _selectionAppBar(l) : _normalAppBar(l),
          body: Column(
            children: [
              // Status filter chips
              SizedBox(
                height: 48,
                child: ListenableBuilder(
                  listenable: InvoiceStore.instance,
                  builder: (context, _) {
                    final current = InvoiceStore.instance.statusFilter;
                    return ListView(
                      scrollDirection: Axis.horizontal,
                      padding: const EdgeInsets.symmetric(horizontal: 12),
                      children: [
                        _filterChip(l.invoicesFilterAll, null, current),
                        _filterChip(l.invoicesFilterNew, 'new', current),
                        _filterChip(l.invoicesFilterPending, 'pending', current),
                        _filterChip(
                            l.invoicesFilterReview, 'ready_for_review', current),
                        _filterChip(
                            l.invoicesFilterApproved, 'approved', current),
                        _filterChip(
                            l.invoicesFilterRejected, 'rejected', current),
                        _filterChip(l.invoicesFilterPaid, 'paid', current),
                      ],
                    );
                  },
                ),
              ),

              // Invoice list
              Expanded(
                child: ListenableBuilder(
                  listenable: InvoiceStore.instance,
                  builder: (context, _) {
                    final store = InvoiceStore.instance;

                    if (store.loading && store.invoices.isEmpty) {
                      return const Center(child: CircularProgressIndicator());
                    }

                    if (store.invoices.isEmpty) {
                      return Center(child: Text(l.invoicesEmpty));
                    }

                    return RefreshIndicator(
                      onRefresh: store.fetch,
                      child: ListView.separated(
                        itemCount: store.invoices.length,
                        separatorBuilder: (_, _) => const Divider(height: 1),
                        itemBuilder: (context, index) {
                          final invoice = store.invoices[index];
                          return InvoiceListTile(
                            invoice: invoice,
                            selectionMode: store.selectionMode,
                            selected: store.isSelected(invoice.id),
                            onTap: store.selectionMode
                                ? () => store.toggleSelected(invoice.id)
                                : () => _openDetail(invoice),
                            onLongPress: _canBulk && !store.selectionMode
                                ? () => store.enterSelectionMode(invoice.id)
                                : null,
                          );
                        },
                      ),
                    );
                  },
                ),
              ),
            ],
          ),
          bottomNavigationBar: selecting
              ? BulkActionBar(
                  selectedCount: InvoiceStore.instance.selectedCount,
                  busy: _busy,
                  onExport: _bulkExport,
                  onStatusChange: _bulkStatusChange,
                  onDelete: _bulkDelete,
                )
              : null,
        );
      },
    );
  }

  AppBar _normalAppBar(AppLocalizations l) {
    return AppBar(
      title: Text(l.invoicesTitle),
      actions: [
        // Multi-select toggle — gated to bulk-capable roles (admin/ap_manager
        // /cfo), mirroring the backend bulk endpoints. Long-press a row also
        // enters selection mode.
        if (_canBulk)
          Semantics(
            label: 'Select multiple',
            button: true,
            child: IconButton(
              tooltip: 'Select multiple',
              icon: const Icon(Icons.checklist),
              onPressed: () => InvoiceStore.instance.enterSelectionMode(),
            ),
          ),
        // Advanced search (vendor / PO / amount range / due-date range).
        // A dot badge marks an active advanced filter so it's never invisible.
        ListenableBuilder(
          listenable: InvoiceStore.instance,
          builder: (context, _) {
            final active = !InvoiceStore.instance.filters.isEmpty;
            return Semantics(
              label: active
                  ? l.invoicesAdvancedSearchActive
                  : l.invoicesAdvancedSearch,
              button: true,
              child: IconButton(
                tooltip: l.invoicesAdvancedSearch,
                icon: Badge(
                  isLabelVisible: active,
                  child: const Icon(Icons.tune),
                ),
                onPressed: _openAdvancedSearch,
              ),
            );
          },
        ),
        // Explicit label is the screen-reader name (tooltip alone isn't
        // exposed as a semantics label on all platforms — WCAG 4.1.2).
        Semantics(
          label: l.invoicesCaptureInvoiceLabel,
          button: true,
          child: IconButton(
            icon: const Icon(Icons.camera_alt),
            tooltip: l.invoicesCaptureInvoice,
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => const CaptureScreen()),
            ),
          ),
        ),
      ],
      bottom: PreferredSize(
        preferredSize: const Size.fromHeight(56),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
          child: SearchBar(
            controller: _searchController,
            hintText: l.invoicesSearchHint,
            leading: const Icon(Icons.search, size: 20),
            onChanged: (q) => _searchDebouncer.run(
              () => InvoiceStore.instance.setSearch(q.isEmpty ? null : q),
            ),
            elevation: WidgetStateProperty.all(0),
          ),
        ),
      ),
    );
  }

  AppBar _selectionAppBar(AppLocalizations l) {
    final store = InvoiceStore.instance;
    return AppBar(
      leading: Semantics(
        label: 'Cancel selection',
        button: true,
        child: IconButton(
          tooltip: 'Cancel selection',
          icon: const Icon(Icons.close),
          onPressed: store.exitSelectionMode,
        ),
      ),
      title: Text('${store.selectedCount} selected'),
      actions: [
        Semantics(
          label: 'Select all',
          button: true,
          child: IconButton(
            tooltip: 'Select all',
            icon: const Icon(Icons.select_all),
            onPressed: store.selectAll,
          ),
        ),
      ],
    );
  }

  Widget _filterChip(String label, String? value, String? current) {
    final selected = current == value;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      child: FilterChip(
        label: Text(label),
        selected: selected,
        onSelected: (_) => InvoiceStore.instance.setStatusFilter(value),
      ),
    );
  }

  Future<void> _openAdvancedSearch() async {
    final result = await showAdvancedSearchSheet(
      context,
      InvoiceStore.instance.filters,
    );
    // null = dismissed (no change); empty = Clear; otherwise Apply.
    if (result != null) {
      InvoiceStore.instance.setFilters(result);
    }
  }

  void _openDetail(Invoice invoice) {
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => InvoiceDetailScreen(invoiceId: invoice.id),
      ),
    );
  }

  Future<void> _bulkDelete() async {
    final count = InvoiceStore.instance.selectedCount;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('Delete invoices?'),
        content: Text(
          'Permanently delete $count selected '
          '${count == 1 ? 'invoice' : 'invoices'}? '
          'Invoices already paid or sent to the ERP are skipped.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(false),
            child: Text(AppLocalizations.of(dialogContext).commonCancel),
          ),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Colors.red.shade700),
            onPressed: () => Navigator.of(dialogContext).pop(true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;

    setState(() => _busy = true);
    final result = await InvoiceStore.instance.bulkDeleteSelected();
    if (!mounted) return;
    setState(() => _busy = false);

    final message = result == null
        ? 'Bulk delete failed: ${InvoiceStore.instance.error ?? ''}'
        : _resultMessage('Deleted', result.count, result.skipped.length);
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
    A11y.announce(context, message);
  }

  Future<void> _bulkStatusChange() async {
    final target = await showModalBottomSheet<InvoiceStatus>(
      context: context,
      builder: (sheetContext) {
        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const ListTile(
                title: Text(
                  'Change status to…',
                  style: TextStyle(fontWeight: FontWeight.w600),
                ),
              ),
              const Divider(height: 1),
              for (final status in _bulkStatusTargets)
                ListTile(
                  title: Text(status.label),
                  onTap: () => Navigator.of(sheetContext).pop(status),
                ),
            ],
          ),
        );
      },
    );
    if (target == null || !mounted) return;

    // A rejection needs a reason: the backend 422s a reasonless bulk reject,
    // and the reason is what the supplier corrects from.
    String? reason;
    if (target == InvoiceStatus.rejected) {
      reason = await showRejectReasonDialog(
        context,
        title: AppLocalizations.of(context)
            .invoicesBulkRejectTitle(InvoiceStore.instance.selectedCount),
      );
      if (reason == null || !mounted) return;
    }

    setState(() => _busy = true);
    final result = await InvoiceStore.instance
        .bulkStatusSelected(target.value, reason: reason);
    if (!mounted) return;
    setState(() => _busy = false);

    final message = result == null
        ? 'Bulk status change failed: ${InvoiceStore.instance.error ?? ''}'
        : _resultMessage(
            'Moved to ${target.label}:',
            result.count,
            result.skipped.length,
          );
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
    A11y.announce(context, message);
  }

  Future<void> _bulkExport() async {
    // Offer the two file formats the backend renders (CSV is the safe default;
    // XML for systems that want structured data). JSON is omitted — it's not a
    // natural "share a file" format on a phone.
    final format = await showModalBottomSheet<String>(
      context: context,
      builder: (sheetContext) {
        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const ListTile(
                title: Text(
                  'Export as…',
                  style: TextStyle(fontWeight: FontWeight.w600),
                ),
              ),
              const Divider(height: 1),
              ListTile(
                leading: const Icon(Icons.table_chart_outlined),
                title: const Text('CSV'),
                onTap: () => Navigator.of(sheetContext).pop('csv'),
              ),
              ListTile(
                leading: const Icon(Icons.code),
                title: const Text('XML'),
                onTap: () => Navigator.of(sheetContext).pop('xml'),
              ),
            ],
          ),
        );
      },
    );
    if (format == null || !mounted) return;

    final count = InvoiceStore.instance.selectedCount;
    setState(() => _busy = true);
    final result = await InvoiceStore.instance.exportSelected(format);
    if (!mounted) return;

    if (result == null) {
      setState(() => _busy = false);
      final message =
          'Export failed: ${InvoiceStore.instance.error ?? 'unknown error'}';
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(message)));
      A11y.announce(context, message);
      return;
    }

    try {
      await FileShare.instance.shareBytes(
        bytes: result.bytes,
        filename: result.filename,
        mimeType: format == 'xml' ? 'application/xml' : 'text/csv',
      );
    } catch (e) {
      if (!mounted) return;
      final message = 'Could not open the share sheet: $e';
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(message)));
      A11y.announce(context, message);
      return;
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    if (!mounted) return;
    final noun = count == 1 ? 'invoice' : 'invoices';
    A11y.announce(
      context,
      'Exported $count $noun as ${format.toUpperCase()}',
    );
  }

  /// Compose a "verb N invoice(s) (M skipped)" result line shared by both
  /// bulk actions. M is omitted when nothing was skipped.
  String _resultMessage(String verb, int count, int skipped) {
    final noun = count == 1 ? 'invoice' : 'invoices';
    final base = '$verb $count $noun';
    return skipped == 0 ? base : '$base ($skipped skipped)';
  }
}
