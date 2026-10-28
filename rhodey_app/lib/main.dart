import 'dart:async';

import 'package:flutter/material.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:home_widget/home_widget.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'theme/app_theme.dart';
import 'screens/today_screen.dart';
import 'screens/inbox_screen.dart';
import 'screens/adaptive_home_screen.dart';
import 'screens/quick_capture_overlay.dart';
import 'screens/onboarding/onboarding_flow.dart';
import 'services/api_config.dart';
import 'services/api_service.dart';
import 'services/persona.dart';
import 'services/notification_service.dart';
import 'services/share_service.dart';
import 'services/update_service.dart';
import 'utils/route_observer.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  await ApiService().init();
  await PersonaStore.load();

  // Never let the platform channel delay the first frame. A cold start from
  // the home widget still resolves the capture route; anything slower than
  // 400ms is treated as a plain launch.
  Uri? uri;
  try {
    uri = await HomeWidget.initiallyLaunchedFromHomeWidget()
        .timeout(const Duration(milliseconds: 400));
  } catch (_) {
    uri = null;
  }
  final isCapture = uri?.host == 'capture';

  runApp(RhodeyApp(initialCapture: isCapture));

  WidgetsBinding.instance.addPostFrameCallback((_) async {
    try {
      await Firebase.initializeApp();
    } catch (e) {
      debugPrint('[Firebase] Init failed: $e');
    }
    // Notification setup (incl. the Android permission prompt) is deferred
    // to MainShell so it never overlaps the onboarding welcome screen — a
    // system permission dialog over the welcome step eats the first tap on
    // 'Set up my world'. MainShell fires it once the shell is actually shown
    // (or after onboarding completes).
    try {
      await ShareService().init();
    } catch (e) {
      debugPrint('[Share] Init failed (non-fatal): $e');
    }
  });
}

class RhodeyApp extends StatelessWidget {
  final bool initialCapture;
  const RhodeyApp({super.key, this.initialCapture = false});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Rhodey OS',
      theme: AppTheme.themeData.copyWith(
        scaffoldBackgroundColor: initialCapture ? Colors.transparent : AppTheme.background,
      ),
      debugShowCheckedModeBanner: false,
      navigatorObservers: [routeObserver],
      home: initialCapture ? const QuickCaptureOverlay() : const MainShell(),
    );
  }
}

class MainShell extends StatefulWidget {
  const MainShell({super.key});

  @override
  State<MainShell> createState() => _MainShellState();
}

/// Local mirror of "onboarding completed" so cold starts skip the status
/// round-trip. Verified against the server in the background on each launch.
const String _kOnboardingDoneKey = 'onboarding_done';

class _MainShellState extends State<MainShell> with WidgetsBindingObserver {
  /// Set to true after the first post-frame callback fires.
  /// Prevents didChangeAppLifecycleState(resumed) from duplicating the
  /// startup check — the lifecycle fires on initial render, and we only
  /// want it to trigger on genuine foreground transitions (app backgrounded
  /// and reopened).
  bool _hasStarted = false;

  /// First-run gate (M8): while loading we show a blank splash; if the user
  /// has no key or hasn't completed onboarding, we show the onboarding
  /// journey instead of the shell. Fail-open: any status error → straight to
  /// the shell (the home screen handles empty data gracefully).
  bool _gateLoading = true;
  bool _needsOnboarding = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _hasStarted = true;
      UpdateService().check(context);
    });
    _checkOnboarding();

    HomeWidget.widgetClicked.listen((Uri? uri) {
      if (!mounted) return;
      if (uri?.host == 'capture') {
        Navigator.of(context).push(PageRouteBuilder(
          opaque: false,
          pageBuilder: (_, _, _) => const QuickCaptureOverlay(),
        ));
      } else if (uri?.host == 'inbox') {
        Navigator.push(context, MaterialPageRoute(builder: (_) => const InboxScreen()));
      } else if (uri?.host == 'today') {
        Navigator.push(context, MaterialPageRoute(builder: (_) => const TodayScreen()));
      }
    });
  }

  /// Deferred notification setup (M10 fix): the FCM init + Android
  /// permission prompt run only once the onboarding gate has cleared —
  /// never over the welcome screen (which would swallow the first tap).
  bool _notificationsInitialized = false;

  Future<void> _initNotificationsAfterGate() async {
    if (_notificationsInitialized) return;
    _notificationsInitialized = true;
    try {
      await NotificationService().init();
    } catch (e) {
      debugPrint('[FCM] Init failed (non-fatal): $e');
    }
  }

  Future<void> _checkOnboarding() async {
    if (!ApiConfig().isConfigured) {
      if (!mounted) return;
      setState(() {
        _needsOnboarding = true;
        _gateLoading = false;
      });
      return;
    }

    // Fast path: a completed onboarding is cached locally, so a cold start
    // renders the shell immediately instead of blocking on a network call.
    // The status is still verified in the background and only flips to the
    // journey if the server says it was reset.
    final prefs = await SharedPreferences.getInstance();
    if (prefs.getBool(_kOnboardingDoneKey) ?? false) {
      if (!mounted) return;
      setState(() => _gateLoading = false);
      _initNotificationsAfterGate();
      unawaited(_verifyOnboarding(prefs));
      return;
    }
    await _verifyOnboarding(prefs);
  }

  /// Verifies the onboarding status with the server and persists the result.
  /// Fail-open on network errors: an existing user is never bounced back into
  /// the journey because the status call failed.
  Future<void> _verifyOnboarding(SharedPreferences prefs) async {
    final r = await ApiService().getOnboardingStatus();
    var needs = false;
    if (r.success && r.data is Map) {
      final status = (r.data as Map)['status'] as String?;
      needs = status == null || status == 'new' || status == 'in_progress';
    }
    if (r.success) {
      try {
        await prefs.setBool(_kOnboardingDoneKey, !needs);
      } catch (_) {
        // Non-fatal — the gate just pays the network call next launch.
      }
    }
    if (!mounted) return;
    setState(() {
      _needsOnboarding = needs;
      _gateLoading = false;
    });
    // Only reach the notification prompt when the journey is NOT showing.
    if (!needs) _initNotificationsAfterGate();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && _hasStarted) {
      // Quietly check for updates every time the app comes to foreground
      UpdateService().check(context);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_gateLoading) {
      return const Scaffold(
        backgroundColor: AppTheme.background,
        body: Center(
          child: SizedBox(
            width: 22,
            height: 22,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              color: AppTheme.accent,
            ),
          ),
        ),
      );
    }
    if (_needsOnboarding) {
      return OnboardingFlow(
        onDone: () async {
          setState(() => _needsOnboarding = false);
          try {
            final prefs = await SharedPreferences.getInstance();
            await prefs.setBool(_kOnboardingDoneKey, true);
          } catch (_) {
            // Non-fatal — the gate pays one extra status call next launch.
          }
          // Onboarding done — now the permission prompt is safe to show.
          _initNotificationsAfterGate();
        },
      );
    }
    return const AdaptiveHomeScreen();
  }
}
