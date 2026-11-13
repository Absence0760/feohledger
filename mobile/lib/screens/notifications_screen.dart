import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/notification.dart';
import 'package:feohledger_mobile/screens/invoice_detail_screen.dart';
import 'package:feohledger_mobile/stores/notification_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/widgets/notification_list_tile.dart';

/// In-app notification center — lists the current user's notifications
/// (`GET /api/notifications`), with an All / Unread filter, mark-all-read, and
/// per-row tap → mark read + deep-link to the invoice detail when the row links
/// to one. Mirrors `ExceptionsScreen` (ListenableBuilder, filter chips,
/// RefreshIndicator, empty / loading / error states).
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

                if (store.loading && store.notifications.isEmpty) {
                  return const Center(child: CircularProgressIndicator());
                }

                if (store.error != null && store.notifications.isEmpty) {
                  return _ErrorState(onRetry: store.fetch);
                }

                if (store.notifications.isEmpty) {
                  return _EmptyState(unreadOnly: store.unreadOnly);
                }

                return RefreshIndicator(
                  onRefresh: store.fetch,
                  child: ListView.separated(
                    itemCount: store.notifications.length,
                    separatorBuilder: (_, _) => const Divider(height: 1),
                    itemBuilder: (context, index) {
                      final n = store.notifications[index];
                      return NotificationListTile(
                        notification: n,
                        onTap: () => _onTap(n),
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
  final bool unreadOnly;
  const _EmptyState({required this.unreadOnly});

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            Icons.notifications_none,
            size: 64,
            color: Colors.grey.shade500,
          ),
          const SizedBox(height: 16),
          Text(
            unreadOnly ? l.notificationsEmptyUnread : l.notificationsEmpty,
            style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 4),
          Text(unreadOnly ? l.notificationsCaughtUp : l.notificationsNothingYet),
        ],
      ),
    );
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
          Icon(Icons.cloud_off, size: 64, color: Colors.grey.shade700),
          const SizedBox(height: 16),
          Text(
            l.notificationsLoadError,
            style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 12),
          FilledButton.tonal(
            onPressed: onRetry,
            child: Text(l.commonRetry),
          ),
        ],
      ),
    );
  }
}
