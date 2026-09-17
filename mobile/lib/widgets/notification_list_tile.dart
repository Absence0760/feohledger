import 'package:flutter/material.dart';

import 'package:feohledger_mobile/models/notification.dart';
import 'package:feohledger_mobile/utils/dates.dart';

/// One row in the notification center: event label, title, optional body and a
/// short relative time. Unread rows carry a leading dot + bolder title.
/// Composes a single screen-reader announcement (WCAG 1.3.1 / 4.1.2).
class NotificationListTile extends StatelessWidget {
  final AppNotification notification;
  final VoidCallback? onTap;

  const NotificationListTile({
    super.key,
    required this.notification,
    this.onTap,
  });

  /// "5m ago" / "3h ago" / "2d ago" / "Mar 4, 2026" — friendly recency without
  /// pulling in a dependency, matching the list-tile date style elsewhere.
  String _relativeTime() {
    final delta = DateTime.now().difference(notification.createdAt);
    if (delta.inMinutes < 1) return 'just now';
    if (delta.inMinutes < 60) return '${delta.inMinutes}m ago';
    if (delta.inHours < 24) return '${delta.inHours}h ago';
    if (delta.inDays < 7) return '${delta.inDays}d ago';
    return formatDate(notification.createdAt.toLocal());
  }

  String get _semanticLabel {
    final parts = <String>[
      if (!notification.isRead) 'Unread',
      notification.eventLabel,
      notification.title,
      if (notification.body != null && notification.body!.isNotEmpty)
        notification.body!,
      _relativeTime(),
    ];
    return parts.join(', ');
  }

  @override
  Widget build(BuildContext context) {
    final unread = !notification.isRead;
    return Semantics(
      label: _semanticLabel,
      button: onTap != null,
      excludeSemantics: true,
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
        onTap: onTap,
        leading: Icon(
          unread ? Icons.circle : Icons.circle_outlined,
          size: 12,
          // Deep blue clears AA at this size; the outlined glyph marks read.
          color: unread ? Colors.blue.shade700 : Colors.grey.shade500,
        ),
        title: Text(
          notification.title,
          style: TextStyle(
            fontWeight: unread ? FontWeight.w700 : FontWeight.w500,
          ),
          overflow: TextOverflow.ellipsis,
        ),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (notification.body != null && notification.body!.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(bottom: 4),
                  child: Text(
                    notification.body!,
                    style: TextStyle(
                      color: Colors.grey.shade700,
                      fontSize: 13,
                    ),
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              Row(
                children: [
                  Text(
                    notification.eventLabel,
                    style: TextStyle(
                      color: Colors.grey.shade700,
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const Spacer(),
                  Text(
                    _relativeTime(),
                    style: TextStyle(
                      color: Colors.grey.shade700,
                      fontSize: 12,
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}
