import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'api_service.dart';

/// Service for handling push notifications via Firebase Cloud Messaging.
///
/// On start: requests notification permissions, gets FCM token,
/// registers token with backend, and sets up message handlers.
class NotificationService {
  static final NotificationService _instance = NotificationService._();
  factory NotificationService() => _instance;
  NotificationService._();

  /// Callback invoked when the user taps a push notification.
  /// The [data] map contains the ``type`` key (briefing|decision|nudge|delegation)
  /// plus any additional payload. Screens register this callback on mount.
  static void Function(Map<String, dynamic> data)? onNotificationOpened;

  /// Callback invoked when ANY push notification is received while the app
  /// is in the foreground. Screens can use this to trigger an immediate
  /// briefing fetch instead of waiting for the poll cycle.
  static void Function()? onPushReceived;

  /// Holds notification data from cold-start (app launched via notification tap
  /// before any screen is mounted). The screen reads this in initState.
  static Map<String, dynamic>? pendingOpenData;

  final _localNotifications = FlutterLocalNotificationsPlugin();
  final _api = ApiService();
  String? _deviceToken;

  bool _initialized = false;

  /// Initialize FCM and local notifications.
  /// Call once at app startup after Firebase.initializeApp().
  Future<void> init() async {
    if (_initialized) return;

    // Initialize local notifications channel
    const androidSettings = AndroidInitializationSettings('@mipmap/ic_launcher');
    const iosSettings = DarwinInitializationSettings(
      requestAlertPermission: false,
      requestBadgePermission: false,
      requestSoundPermission: false,
    );
    await _localNotifications.initialize(
      settings: const InitializationSettings(android: androidSettings, iOS: iosSettings),
    );

    // Request notification permissions (Android 13+)
    final messaging = FirebaseMessaging.instance;
    final notiSettings = await messaging.requestPermission(
      alert: true,
      badge: true,
      sound: true,
    );

    if (notiSettings.authorizationStatus == AuthorizationStatus.authorized ||
        notiSettings.authorizationStatus == AuthorizationStatus.provisional) {
      debugPrint('[Notification] Permission granted');
    }

    // Get device token
    _deviceToken = await messaging.getToken();
    debugPrint('[Notification] FCM Token: $_deviceToken');

    // Register token with backend
    await _registerToken();

    // Listen for token refresh
    messaging.onTokenRefresh.listen((newToken) {
      _deviceToken = newToken;
      debugPrint('[Notification] Token refreshed: $newToken');
      _registerToken();
    });

    // Handle foreground messages — show local notification for visible pushes,
    // trigger silent refresh for data-only pushes (FCM-driven polling replacement).
    FirebaseMessaging.onMessage.listen(_onForegroundMessage);

    // Handle notification tap (app opened from background)
    FirebaseMessaging.onMessageOpenedApp.listen(_handleNotificationTap);

    // Handle notification that launched the app from terminated state
    final initialMessage = await messaging.getInitialMessage();
    if (initialMessage != null) {
      _handleNotificationTap(initialMessage);
    }

    _initialized = true;
  }

  /// Register the device token with the Rhodey backend.
  Future<void> _registerToken() async {
    if (_deviceToken == null) return;
    try {
      final result = await _api.post('/api/register-device', body: {
        'token': _deviceToken,
        'platform': 'android',
      });
      if (result.success) {
        debugPrint('[Notification] Token registered with backend');
      }
    } catch (e) {
      debugPrint('[Notification] Token registration failed: $e');
    }
  }

  /// Handle any FCM message received in the foreground.
  /// - If it has a ``notification`` payload: show local notification + refresh.
  /// - If it's data-only (``type: briefing_refresh``): trigger silent refresh.
  void _onForegroundMessage(RemoteMessage message) {
    final notification = message.notification;
    final data = message.data;

    if (notification != null) {
      // Visible push — show local notification
      final androidDetails = AndroidNotificationDetails(
        'rhodey_channel',
        'Rhodey Updates',
        channelDescription: 'Notifications from Rhodey OS',
        importance: Importance.high,
        priority: Priority.high,
        icon: '@mipmap/ic_launcher',
      );

      _localNotifications.show(
        id: DateTime.now().millisecondsSinceEpoch ~/ 1000,
        title: notification.title ?? 'Rhodey',
        body: notification.body ?? '',
        notificationDetails: NotificationDetails(android: androidDetails),
      );
    }

    // Trigger refresh for both visible and silent pushes
    // This replaces the 10s HTTP polling — the backend rings FCM's doorbell,
    // and we only fetch the briefing when there's actually new data.
    if (onPushReceived != null && data.isNotEmpty) {
      onPushReceived!();
    }
  }

  /// Handle user tapping on a notification.
  void _handleNotificationTap(RemoteMessage message) {
    final data = message.data;
    debugPrint('[Notification] Tapped: ${message.messageId} data=$data');

    if (data.isEmpty) return;

    if (onNotificationOpened != null) {
      // Screen is already mounted — navigate directly
      onNotificationOpened!(data);
    } else {
      // Screen not yet mounted (cold-start) — store for pickup
      pendingOpenData = data;
    }
  }
}
