import 'package:flutter/material.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:home_widget/home_widget.dart';
import 'theme/app_theme.dart';
import 'screens/today_screen.dart';
import 'screens/inbox_screen.dart';
import 'screens/adaptive_home_screen.dart';
import 'screens/quick_capture_overlay.dart';
import 'services/api_service.dart';
import 'services/notification_service.dart';
import 'services/share_service.dart';
import 'services/update_service.dart';
import 'utils/route_observer.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  await ApiService().init();

  final uri = await HomeWidget.initiallyLaunchedFromHomeWidget();
  final isCapture = uri?.host == 'capture';

  runApp(RhodeyApp(initialCapture: isCapture));

  WidgetsBinding.instance.addPostFrameCallback((_) async {
    try {
      await Firebase.initializeApp();
    } catch (e) {
      debugPrint('[Firebase] Init failed: $e');
    }
    try {
      await NotificationService().init();
    } catch (e) {
      debugPrint('[FCM] Init failed (non-fatal): $e');
    }
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

class _MainShellState extends State<MainShell> with WidgetsBindingObserver {
  /// Set to true after the first post-frame callback fires.
  /// Prevents didChangeAppLifecycleState(resumed) from duplicating the
  /// startup check — the lifecycle fires on initial render, and we only
  /// want it to trigger on genuine foreground transitions (app backgrounded
  /// and reopened).
  bool _hasStarted = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _hasStarted = true;
      UpdateService().check(context);
    });

    HomeWidget.widgetClicked.listen((Uri? uri) {
      if (uri?.host == 'capture') {
        Navigator.of(context).push(PageRouteBuilder(
          opaque: false,
          pageBuilder: (_, __, ___) => const QuickCaptureOverlay(),
        ));
      } else if (uri?.host == 'inbox') {
        Navigator.push(context, MaterialPageRoute(builder: (_) => const InboxScreen()));
      } else if (uri?.host == 'today') {
        Navigator.push(context, MaterialPageRoute(builder: (_) => const TodayScreen()));
      }
    });
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
    return const AdaptiveHomeScreen();
  }
}
