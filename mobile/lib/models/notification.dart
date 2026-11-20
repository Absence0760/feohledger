/// In-app notification-center entity. Mirrors the backend
/// `NotificationResponse` shape (see `backend/app/schemas/notification.py` +
/// `backend/app/api/notifications.py`).
///
/// Named `AppNotification` to avoid clashing with the `flutter_local_notifications`
/// / FCM `Notification` types pulled in by `push_service`.
class AppNotification {
  final String id;
  final String eventType;

  /// `invoice` | `contract` (backend default `invoice`). Drives tap-navigation:
  /// only an `invoice` row with an [entityId] deep-links to a detail screen.
  final String entityType;
  final String? entityId;
  final String title;
  final String? body;

  /// `null` == unread. Set to a timestamp once the recipient marks it read.
  final DateTime? readAt;
  final DateTime createdAt;

  AppNotification({
    required this.id,
    required this.eventType,
    required this.entityType,
    this.entityId,
    required this.title,
    this.body,
    this.readAt,
    required this.createdAt,
  });

  bool get isRead => readAt != null;

  /// Can this row deep-link to an invoice detail screen on tap? (Other entity
  /// types — e.g. `contract` — have no mobile detail screen yet, so they just
  /// mark read.)
  bool get linksToInvoice => entityType == 'invoice' && entityId != null;

  /// Human-readable label for the originating event.
  @Deprecated('use enum_labels')
  String get eventLabel => switch (eventType) {
        'invoice_assigned' => 'Invoice assigned',
        'invoice_approved' => 'Invoice approved',
        'invoice_rejected' => 'Invoice rejected',
        'invoice_paid' => 'Invoice paid',
        'contract_renewal_due' => 'Contract renewal due',
        'chat_message' => 'New message',
        _ => eventType.replaceAll('_', ' '),
      };

  factory AppNotification.fromJson(Map<String, dynamic> json) {
    return AppNotification(
      id: json['id'] as String,
      eventType: json['event_type'] as String? ?? 'unknown',
      entityType: json['entity_type'] as String? ?? 'invoice',
      entityId: json['entity_id'] as String?,
      title: json['title'] as String? ?? '',
      body: json['body'] as String?,
      readAt: _parseDate(json['read_at']),
      createdAt: _parseDate(json['created_at']) ??
          DateTime.fromMillisecondsSinceEpoch(0),
    );
  }

  /// A read copy of this row — used for optimistic in-memory updates so the UI
  /// reflects a mark-read without a refetch.
  AppNotification copyMarkedRead(DateTime when) => AppNotification(
        id: id,
        eventType: eventType,
        entityType: entityType,
        entityId: entityId,
        title: title,
        body: body,
        readAt: readAt ?? when,
        createdAt: createdAt,
      );

  Map<String, dynamic> toJson() => {
        'id': id,
        'event_type': eventType,
        'entity_type': entityType,
        'entity_id': entityId,
        'title': title,
        'body': body,
        'read_at': readAt?.toIso8601String(),
        'created_at': createdAt.toIso8601String(),
      };

  static DateTime? _parseDate(Object? v) {
    if (v is! String || v.isEmpty) return null;
    return DateTime.tryParse(v);
  }
}
