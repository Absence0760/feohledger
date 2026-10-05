import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/exception.dart';
import 'package:feohledger_mobile/screens/exception_detail_screen.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/exception_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/widgets/bulk_action_bar.dart';
import 'package:feohledger_mobile/widgets/exception_list_tile.dart';

/// Exception queue — list flagged invoices with status filters and act on each
/// (resolve / escalate / dismiss). Swipe a row right to resolve, left to
/// dismiss; the trailing menu carries escalate (a third action doesn't map onto
/// a binary swipe). Mirrors `ApprovalsScreen` + `InvoicesScreen`.
class ExceptionsScreen extends StatefulWidget {
  const ExceptionsScreen({super.key});

  @override
  State<ExceptionsScreen> createState() => _ExceptionsScreenState();
}

class _ExceptionsScreenState extends State<ExceptionsScreen> {
  // Admin / AP manager can act on exceptions (mirrors the backend
  // require_roles(ROLE_ADMIN, ROLE_AP_MANAGER) on the assign / bulk routes).
  bool get _canBulk => AuthStore.instance.canApprove;

  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      ExceptionStore.instance.fetch();
    });
  }

  @override
  void dispose() {
    // Leave selection mode behind so re-entering the tab starts clean (the
    // store is a process-lifetime singleton).
    if (ExceptionStore.instance.selectionMode) {
      ExceptionStore.instance.exitSelectionMode();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return ListenableBuilder(
      listenable: ExceptionStore.instance,
      builder: (context, _) {
        final selecting = ExceptionStore.instance.selectionMode;
        return Scaffold(
          appBar: AppBar(
            title: Text(
              selecting
                  ? l.bulkSelectedCount(ExceptionStore.instance.selectedCount)
                  : l.exceptionsTitle,
            ),
            leading: selecting
                ? Semantics(
                    label: l.bulkCancelSelection,
                    button: true,
                    child: IconButton(
                      icon: const Icon(Icons.close),
                      tooltip: l.bulkCancelSelection,
                      onPressed: ExceptionStore.instance.exitSelectionMode,
                    ),
                  )
                : null,
            actions: [
              if (!selecting && _canBulk)
                Semantics(
                  label: l.exceptionsSelectMultiple,
                  button: true,
                  child: IconButton(
                    icon: const Icon(Icons.checklist),
                    tooltip: l.exceptionsSelectMultiple,
                    onPressed: () =>
                        ExceptionStore.instance.enterSelectionMode(),
                  ),
                ),
            ],
          ),
          bottomNavigationBar: selecting
              ? BulkActionBar(
                  selectedCount: ExceptionStore.instance.selectedCount,
                  busy: ExceptionStore.instance.loading,
                  // Reuse the shared bar's two slots for resolve / dismiss,
                  // named for what they do here rather than "Status" /
                  // "Delete".
                  onStatusChange: () => _bulkResolve('resolve'),
                  statusLabel: l.exceptionActionResolve,
                  onDelete: () => _bulkResolve('dismiss'),
                  deleteLabel: l.exceptionActionDismiss,
                )
              : null,
          body: _buildBody(l),
        );
      },
    );
  }

  Widget _buildBody(AppLocalizations l) {
    return Column(
      children: [
          // Status filter chips.
          SizedBox(
            height: 48,
            child: ListenableBuilder(
              listenable: ExceptionStore.instance,
              builder: (context, _) {
                final current = ExceptionStore.instance.statusFilter;
                return ListView(
                  scrollDirection: Axis.horizontal,
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  children: [
                    _filterChip(l.commonAll, null, current),
                    _filterChip(l.exceptionsFilterOpen, 'open', current),
                    _filterChip(l.exceptionsFilterEscalated, 'escalated', current),
                    _filterChip(l.exceptionsFilterResolved, 'resolved', current),
                    _filterChip(l.exceptionsFilterDismissed, 'dismissed', current),
                  ],
                );
              },
            ),
          ),

          // Exception list.
          Expanded(
            child: ListenableBuilder(
              listenable: ExceptionStore.instance,
              builder: (context, _) {
                final store = ExceptionStore.instance;

                if (store.loading && store.exceptions.isEmpty) {
                  return const Center(child: CircularProgressIndicator());
                }

                if (store.exceptions.isEmpty) {
                  return Center(
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        const Icon(
                          Icons.check_circle,
                          size: 64,
                          color: Colors.green,
                        ),
                        const SizedBox(height: 16),
                        Text(
                          l.exceptionsEmpty,
                          style: const TextStyle(
                            fontSize: 18,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        const SizedBox(height: 4),
                        Text(l.exceptionsQueueClear),
                      ],
                    ),
                  );
                }

                return RefreshIndicator(
                  onRefresh: store.fetch,
                  child: ListView.separated(
                    itemCount: store.exceptions.length,
                    separatorBuilder: (_, _) => const Divider(height: 1),
                    itemBuilder: (context, index) {
                      final exc = store.exceptions[index];
                      return _buildRow(exc);
                    },
                  ),
                );
              },
            ),
          ),
        ],
    );
  }

  Widget _buildRow(ApException exc) {
    final l = AppLocalizations.of(context);
    final store = ExceptionStore.instance;
    final selecting = store.selectionMode;

    final tile = ExceptionListTile(
      exception: exc,
      selected: selecting && store.isSelected(exc.id),
      onTap: selecting
          ? () => store.toggleSelected(exc.id)
          : () => _openDetail(exc),
      onLongPress: _canBulk && !selecting && exc.status.isActionable
          ? () => store.enterSelectionMode(exc.id)
          : null,
    );

    // In selection mode, or for terminal (read-only) rows, no swipe.
    if (selecting || !exc.status.isActionable) {
      return tile;
    }

    return Dismissible(
      key: ValueKey(exc.id),
      background: _swipeBackground(
        Colors.green.shade700,
        Icons.check,
        Alignment.centerLeft,
        l.exceptionActionResolve,
      ),
      secondaryBackground: _swipeBackground(
        Colors.blueGrey.shade700,
        Icons.block,
        Alignment.centerRight,
        l.exceptionActionDismiss,
      ),
      confirmDismiss: (direction) async {
        final resolve = direction == DismissDirection.startToEnd;
        final ok = resolve
            ? await ExceptionStore.instance.resolve(exc.id)
            : await ExceptionStore.instance.dismiss(exc.id);
        if (ok && mounted) {
          // The row vanishing isn't announced on its own (WCAG 4.1.3).
          A11y.announce(
            context,
            resolve ? l.exceptionResolved : l.exceptionDismissed,
          );
        }
        return ok;
      },
      child: tile,
    );
  }

  Widget _swipeBackground(
    Color color,
    IconData icon,
    Alignment alignment,
    String action,
  ) {
    // Label the swipe affordance so the icon isn't an unlabelled glyph
    // (WCAG 1.1.1); the action is also surfaced visually as text.
    return Container(
      color: color.withValues(alpha: 0.15),
      alignment: alignment,
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, color: color),
          const SizedBox(width: 8),
          Text(
            action,
            style: TextStyle(color: color, fontWeight: FontWeight.w600),
          ),
        ],
      ),
    );
  }

  /// Open the full detail screen for a row. On return (an action was taken
  /// there) the list refetches so the row reflects the new state.
  Future<void> _openDetail(ApException exc) async {
    final acted = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => ExceptionDetailScreen(exceptionId: exc.id),
      ),
    );
    if (acted == true && mounted) {
      await ExceptionStore.instance.fetch();
    }
  }

  /// Bulk resolve/dismiss the current selection → snackbar reports the
  /// updated/skipped counts (partial-success contract).
  Future<void> _bulkResolve(String action) async {
    final l = AppLocalizations.of(context);
    final result = await ExceptionStore.instance.bulkResolveSelected(
      action: action,
    );
    if (!mounted || result == null) {
      if (mounted) A11y.announce(context, l.exceptionActionFailed);
      return;
    }
    final base = action == 'dismiss'
        ? l.exceptionsBulkDismissed(result.updated)
        : l.exceptionsBulkResolved(result.updated);
    final message = result.skippedCount == 0
        ? base
        : l.bulkResultWithSkipped(base, result.skippedCount);
    A11y.announce(context, message);
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  Widget _filterChip(String label, String? value, String? current) {
    final selected = current == value;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      child: FilterChip(
        label: Text(label),
        selected: selected,
        onSelected: (_) => ExceptionStore.instance.setStatusFilter(value),
      ),
    );
  }
}
