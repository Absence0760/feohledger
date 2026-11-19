import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/notification.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/utils/sequenced_fetch.dart';

/// In-app notification center state. Mirrors [ExceptionStore]: a process-lifetime
/// `ChangeNotifier` singleton over `GET /api/notifications` + mark-read /
/// read-all, with an offline-cached list and a separately-tracked unread count
/// (the badge) so the app-bar bell can render without forcing a full fetch.
class NotificationStore extends ChangeNotifier with SequencedFetch {
  static final NotificationStore instance = NotificationStore._();
  NotificationStore._();

  List<AppNotification> _notifications = [];
  int _unread = 0;
  bool _loading = false;

  /// The last **fetch** failure, as caught — the list screen words it with
  /// `describeLoadError` and it alone drives the list's error state and
  /// "couldn't refresh" banner. Mark-read actions never write it.
  Object? _loadError;

  /// The last failed **action** (mark read / mark all read), as a sentence
  /// ([describeApiError]). Kept apart from [loadError] so a refused mark-read
  /// over a loaded list is not reported as a failed refresh.
  String? _actionError;
  bool _unreadOnly = false;
  bool _fromCache = false;

  List<AppNotification> get notifications => _notifications;
  int get unread => _unread;
  bool get loading => _loading;
  Object? get loadError => _loadError;
  String? get actionError => _actionError;
  bool get unreadOnly => _unreadOnly;
  bool get fromCache => _fromCache;

  /// Drop all in-memory state. Called on logout / forced logout through
  /// `SessionManager.endSession` — these are process-lifetime singletons, so
  /// without this a signed-out user's data would still be in memory for the
  /// next account on the device. Tests use it to decouple from run order.
  void reset() {
    _notifications = [];
    _unread = 0;
    _loading = false;
    _loadError = null;
    _actionError = null;
    _unreadOnly = false;
    _fromCache = false;
    debugResetSequence();
  }

  void setUnreadOnly(bool value) {
    _unreadOnly = value;
    fetch();
  }

  /// Cheap badge refresh — no list reload. Best-effort: a failure leaves the
  /// last known count in place (the bell never blocks the app).
  Future<void> refreshUnreadCount() async {
    try {
      _unread = await NotificationApi.unreadCount();
      notifyListeners();
    } catch (_) {
      // Keep the stale count; the next full fetch will reconcile it.
    }
  }

  Future<void> fetch() async {
    // See SequencedFetch — discards a response superseded by a newer fetch()
    // (e.g. a rapid All/Unread filter toggle racing itself).
    final token = nextRequestToken();
    _loading = true;
    _loadError = null;
    notifyListeners();

    final cacheKey = 'notifications_${_unreadOnly ? 'unread' : 'all'}';

    try {
      final pageData = await NotificationApi.list(unreadOnly: _unreadOnly);
      if (!isCurrentRequest(token)) return;
      _notifications = pageData.items;
      _unread = pageData.unread;
      _fromCache = false;
      _loading = false;

      await OfflineStore.instance.put(
        cacheKey,
        _notifications.map((n) => n.toJson()).toList(),
      );

      notifyListeners();
    } catch (e) {
      if (!isCurrentRequest(token)) return;
      // Try cache on failure (offline mode — same pattern as ExceptionStore).
      try {
        final cached = await OfflineStore.instance.get(cacheKey);
        if (cached != null) {
          if (!isCurrentRequest(token)) return;
          _notifications = (cached as List)
              .map((j) => AppNotification.fromJson(j as Map<String, dynamic>))
              .toList();
          _unread = _notifications.where((n) => !n.isRead).length;
          _fromCache = true;
          _loading = false;
          notifyListeners();
          return;
        }
      } catch (_) {}
      if (!isCurrentRequest(token)) return;
      _fromCache = false;
      _loading = false;
      _loadError = e;
      notifyListeners();
    }
  }

  /// Mark one notification read. Optimistically flips the row + decrements the
  /// badge before the request so the UI reacts instantly; on failure we surface
  /// the error and reconcile via a fetch. Idempotent — re-marking a read row is
  /// a no-op locally and server-side.
  Future<bool> markRead(String id) async {
    final idx = _notifications.indexWhere((n) => n.id == id);
    if (idx == -1) return false;
    final existing = _notifications[idx];
    if (existing.isRead) return true; // already read — nothing to do

    _actionError = null;
    _notifications[idx] = existing.copyMarkedRead(DateTime.now());
    if (_unread > 0) _unread--;
    notifyListeners();

    try {
      await NotificationApi.markRead(id);
      // If we're filtered to unread-only, the now-read row should drop out.
      if (_unreadOnly) {
        _notifications.removeWhere((n) => n.id == id);
        notifyListeners();
      }
      return true;
    } catch (e) {
      // Reconcile real state — undo the optimistic flip via a refetch. The
      // refetch's own outcome lands in loadError; the mark-read failure is the
      // action's, so it never reads as a failed refresh of the list.
      _actionError = describeApiError(e);
      await fetch();
      return false;
    }
  }

  /// Mark every unread notification read.
  Future<bool> markAllRead() async {
    _actionError = null;
    try {
      await NotificationApi.markAllRead();
      await fetch();
      return true;
    } catch (e) {
      _actionError = describeApiError(e);
      notifyListeners();
      return false;
    }
  }
}
