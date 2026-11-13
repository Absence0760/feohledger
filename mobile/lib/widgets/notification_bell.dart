import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/screens/notifications_screen.dart';
import 'package:feohledger_mobile/stores/notification_store.dart';

/// App-bar bell that opens the notification center and shows the live unread
/// count as a [Badge]. Drop it into any screen's `AppBar.actions`. Refreshes
/// the cheap unread count on mount (and when popped back to) so the badge stays
/// current without a full list fetch.
class NotificationBell extends StatefulWidget {
  const NotificationBell({super.key});

  @override
  State<NotificationBell> createState() => _NotificationBellState();
}

class _NotificationBellState extends State<NotificationBell> {
  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      NotificationStore.instance.refreshUnreadCount();
    });
  }

  Future<void> _open() async {
    await Navigator.of(context).push(
      MaterialPageRoute(builder: (_) => const NotificationsScreen()),
    );
    // Reconcile the badge after the center may have marked rows read.
    await NotificationStore.instance.refreshUnreadCount();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: NotificationStore.instance,
      builder: (context, _) {
        final unread = NotificationStore.instance.unread;
        final label = unread > 0
            ? 'Notifications, $unread unread'
            : 'Notifications';
        // Merge into one node and exclude the inner IconButton/Badge semantics
        // (the Badge count + the IconButton tooltip would otherwise produce
        // competing labels) so screen readers announce one sensible phrase that
        // carries the live unread count.
        return Semantics(
          label: label,
          button: true,
          container: true,
          excludeSemantics: true,
          child: IconButton(
            tooltip: 'Notifications',
            onPressed: _open,
            icon: unread > 0
                ? Badge(
                    label: Text(unread > 99 ? '99+' : '$unread'),
                    child: const Icon(Icons.notifications),
                  )
                : const Icon(Icons.notifications_none),
          ),
        );
      },
    );
  }
}
