import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/load_error_messages.dart';
import 'package:feohledger_mobile/models/notification.dart';
import 'package:feohledger_mobile/screens/invoice_detail_screen.dart';
import 'package:feohledger_mobile/stores/notification_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/widgets/async_list_body.dart';
import 'package:feohledger_mobile/widgets/notification_list_tile.dart';

/// In-app notification center — lists the current user's notifications
/// (`GET /api/notifications`), with an All / Unread filter, mark-all-read, and
/// per-row tap → mark read + deep-link to the invoice detail when the row links
/// to one. Mirrors `ExceptionsScreen` (ListenableBuilder, filter chips, and
/// the shared `AsyncListBody` for the loading / error / empty / stale states).
class NotificationsScreen extends StatefulWidget {
  const NotificationsScreen({super.key});

  @override
  State<NotificationsScreen> createState() => _NotificationsScreenState();
}

class _NotificationsScreenState extends State<NotificationsScreen> {
  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      NotificationStore.instance.fetch();
    });
  }

  Future<void> _markAllRead() async {
    final l = AppLocalizations.of(context);
    final ok = await NotificationStore.instance.markAllRead();
    if (!mounted) return;
    A11y.announce(
      context,
      ok ? l.notificationsAllMarkedRead : l.notificationsCouldNotMarkAll,
    );
  }

  Future<void> _onTap(AppNotification n) async {
    await NotificationStore.instance.markRead(n.id);
    if (!mounted) return;
    if (n.linksToInvoice) {
      Navigator.of(context).push(
        MaterialPageRoute(
          builder: (_) => InvoiceDetailScreen(invoiceId: n.entityId!),
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(l.notificationsTitle),
        actions: [
          ListenableBuilder(
            listenable: NotificationStore.instance,
            builder: (context, _) {
              final store = NotificationStore.instance;
              // Only offer mark-all-read when there's something to clear.
              if (store.unread == 0) return const SizedBox.shrink();
              return Semantics(
                label: l.notificationsMarkAllReadLabel,
                button: true,
                child: IconButton(
                  tooltip: l.notificationsMarkAllRead,
                  icon: const Icon(Icons.done_all),
                  onPressed: _markAllRead,
                ),
              );
            },
          ),
        ],
      ),
      body: Column(
        children: [
          SizedBox(
            height: 48,
            child: ListenableBuilder(
              listenable: NotificationStore.instance,
              builder: (context, _) {
                final unreadOnly = NotificationStore.instance.unreadOnly;
                return ListView(
                  scrollDirection: Axis.horizontal,
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  children: [
                    _filterChip(l.commonAll, false, unreadOnly),
                    _filterChip(l.notificationsFilterUnread, true, unreadOnly),
                  ],
                );
              },
            ),
          ),
          Expanded(
            child: ListenableBuilder(
              listenable: NotificationStore.instance,
              builder: (context, _) {
                final store = NotificationStore.instance;
                final loadError = store.loadError;

                return AsyncListBody<AppNotification>(
                  items: store.notifications,
                  loading: store.loading,
                  // The fetch's failure only — a refused mark-read leaves the
                  // list live (actionError), so it raises no banner.
                  hasError: loadError != null,
                  fromCache: store.fromCache,
                  errorMessage: loadError == null
                      ? ''
                      : describeLoadError(
                          l,
                          loadError,
                          l.notificationsLoadErrorPrefix,
                        ),
                  onRefresh: store.fetch,
                  empty: const _EmptyState(),
                  // Under the Unread chip an empty list says only that
                  // nothing is unread, with the way back to every
                  // notification.
                  filteredEmpty: store.unreadOnly
                      ? FilteredEmptyState(
                          message: l.notificationsEmptyUnread,
                          onShowAll: () => store.setUnreadOnly(false),
                        )
                      : null,
                  itemBuilder: (context, n) => NotificationListTile(
                    notification: n,
                    onTap: () => _onTap(n),
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  Widget _filterChip(String label, bool value, bool current) {
    final selected = current == value;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      child: FilterChip(
        label: Text(label),
        selected: selected,
        onSelected: (_) => NotificationStore.instance.setUnreadOnly(value),
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState();

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        ExcludeSemantics(
          child: Icon(
            Icons.notifications_none,
            size: 64,
            color: Colors.grey.shade500,
          ),
        ),
        const SizedBox(height: 16),
        Text(
          l.notificationsEmpty,
          textAlign: TextAlign.center,
          style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 4),
        Text(l.notificationsNothingYet, textAlign: TextAlign.center),
      ],
    );
  }
}
