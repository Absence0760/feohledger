import 'dart:async';
import 'dart:io' show Platform;

import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/screens/invoice_detail_screen.dart';

/// Push notification service — Firebase Cloud Messaging.
///
/// Requires Firebase project setup:
/// - iOS: GoogleService-Info.plist in ios/Runner/
/// - Android: google-services.json in android/app/
///
/// Until Firebase is configured, all methods are no-ops that log warnings.
class PushService {
  static final PushService instance = PushService._();
  PushService._();

  /// Root navigator key, wired into `MaterialApp.navigatorKey` in main.dart.
  /// `_handleMessageTap` runs as a top-level FCM callback outside the widget
  /// tree (no `BuildContext` of its own), so this is the only way it can
  /// push a route when a user taps a background/terminated notification.
  static final GlobalKey<NavigatorState> navigatorKey = GlobalKey<NavigatorState>();

  bool _initialized = false;
  String? _fcmToken;

  String? get fcmToken => _fcmToken;

  final _localNotifications = FlutterLocalNotificationsPlugin();

  /// Initialize FCM and request permissions.
  /// Call once from main.dart after Firebase.initializeApp().
  Future<void> init() async {
    if (_initialized) return;

    try {
      // Request permission (iOS prompts user, Android auto-grants)
      final settings = await FirebaseMessaging.instance.requestPermission(
        alert: true,
        badge: true,
        sound: true,
      );

      if (settings.authorizationStatus == AuthorizationStatus.denied) {
        debugPrint('[push] Notification permission denied');
        return;
      }

      // Get FCM token
      _fcmToken = await FirebaseMessaging.instance.getToken();
      debugPrint('[push] FCM token: $_fcmToken');
      if (_fcmToken != null) unawaited(_registerToken(_fcmToken!));

      // Listen for token refresh
      FirebaseMessaging.instance.onTokenRefresh.listen((token) {
        _fcmToken = token;
        debugPrint('[push] FCM token refreshed');
        unawaited(_registerToken(token));
      });

      // Initialize local notifications for foreground display. The small icon
      // is a white silhouette: Android draws it from alpha alone, so the
      // launcher tile would show as a solid white square.
      await _localNotifications.initialize(
        settings: const InitializationSettings(
          android: AndroidInitializationSettings('@drawable/ic_notification'),
          iOS: DarwinInitializationSettings(),
        ),
      );

      // Handle foreground messages
      FirebaseMessaging.onMessage.listen(_handleForegroundMessage);

      // Handle background message taps (app was in background)
      FirebaseMessaging.onMessageOpenedApp.listen(_handleMessageTap);

      _initialized = true;
      debugPrint('[push] Push notifications initialized');
    } catch (e) {
      debugPrint('[push] Init failed (Firebase not configured?): $e');
    }
  }

  void _handleForegroundMessage(RemoteMessage message) {
    debugPrint('[push] Foreground message: ${message.notification?.title}');

    final notification = message.notification;
    if (notification == null) return;

    // Show local notification since FCM doesn't auto-show in foreground
    _localNotifications.show(
      id: notification.hashCode,
      title: notification.title,
      body: notification.body,
      notificationDetails: const NotificationDetails(
        android: AndroidNotificationDetails(
          'feoh_approvals',
          'Approval Notifications',
          channelDescription: 'Invoice approval requests and status updates',
          importance: Importance.high,
          priority: Priority.high,
        ),
        iOS: DarwinNotificationDetails(),
      ),
    );
  }

  void _handleMessageTap(RemoteMessage message) {
    debugPrint('[push] Message tapped: ${message.data}');

    final invoiceId = message.data['invoice_id'] as String?;
    if (invoiceId == null || invoiceId.isEmpty) {
      debugPrint('[push] Tap carried no invoice_id — nothing to navigate to');
      return;
    }

    final navigator = navigatorKey.currentState;
    if (navigator == null) {
      // No live Navigator (e.g. a cold start still on the splash screen) —
      // this is a best-effort deep link, not a guaranteed one.
      debugPrint('[push] No navigator available yet — dropping deep link');
      return;
    }
    navigator.push(
      MaterialPageRoute(builder: (_) => InvoiceDetailScreen(invoiceId: invoiceId)),
    );
  }

  /// Best-effort registration of [token] for this device's [platform] with
  /// the backend (`POST /api/notifications/device-token`). Mirrors the
  /// try/catch-and-log discipline of the rest of this service — a failure
  /// here (no network, not yet authenticated) must never crash or block push
  /// setup. Silently no-ops before login (the caller has no JWT yet); the
  /// token is re-sent on the next `onTokenRefresh` fire, so a session that
  /// starts after this call still ends up registered.
  Future<void> _registerToken(String token) async {
    final platform = Platform.isIOS ? 'ios' : 'android';
    try {
      await NotificationApi.registerDeviceToken(token, platform);
      debugPrint('[push] Device token registered ($platform)');
    } catch (e) {
      debugPrint('[push] Device token registration failed (not logged in yet?): $e');
    }
  }
}
