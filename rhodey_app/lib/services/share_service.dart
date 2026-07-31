import 'package:flutter/foundation.dart';
import 'package:receive_sharing_intent/receive_sharing_intent.dart';

/// Handles content shared INTO Rhodey from other apps (browser → Share sheet
/// → Rhodey), mirroring the Telegram "send link to Rhodey Bot" flow.
///
/// When the user shares a URL from any app, Android hands the text to us via
/// an ACTION_SEND intent. This service:
///   1. Captures the shared text on cold start (`getInitialMedia`) and on
///      warm start (`getMediaStream`).
///   2. Delivers it to the registered screen callback — which forwards it to
///      Rhodey exactly like a typed message. The backend's `url_filter`
///      auto-saves bare URLs into `resources`, so "share a link" works the
///      same as Telegram today.
///
/// Callbacks mirror `NotificationService`: a static `onShared` the active
/// screen registers on mount, plus `pendingSharedText` for cold-start shares
/// that arrive before any screen is mounted.
class ShareService {
  static final ShareService _instance = ShareService._();
  factory ShareService() => _instance;
  ShareService._();

  /// Invoked when the app receives a shared text/URL. Screens register this
  /// callback on mount and unregister on dispose.
  static void Function(String text)? onShared;

  /// Holds a shared text received at cold start before any screen mounted.
  /// The screen reads this in initState, then clears it.
  static String? pendingSharedText;

  bool _initialized = false;

  /// Initialize share-intent capture. Call once at app startup.
  Future<void> init() async {
    if (_initialized) return;

    // Cold start: app launched directly from the share sheet.
    final initial = await ReceiveSharingIntent.instance.getInitialMedia();
    if (initial.isNotEmpty) {
      final text = initial.first.path.trim();
      if (text.isNotEmpty) _deliver(text);
    }
    // Reset so the initial intent doesn't re-fire on reload.
    ReceiveSharingIntent.instance.reset();

    // Warm start: app already running when a share arrives.
    ReceiveSharingIntent.instance.getMediaStream().listen((media) {
      if (media.isEmpty) return;
      final text = media.first.path.trim();
      if (text.isNotEmpty) _deliver(text);
    }, onError: (err) {
      debugPrint('[Share] Media stream error: $err');
    });

    _initialized = true;
  }

  void _deliver(String text) {
    if (onShared != null) {
      onShared!(text);
    } else {
      pendingSharedText = text;
    }
  }
}
