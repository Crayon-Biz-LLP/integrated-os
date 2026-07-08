import 'package:flutter/material.dart';
import 'package:firebase_core/firebase_core.dart';
import 'theme/app_theme.dart';
import 'screens/talk_screen.dart';
import 'screens/dump_screen.dart';
import 'screens/today_screen.dart';
import 'screens/inbox_screen.dart';
import 'screens/adaptive_home_screen.dart';
import 'services/api_service.dart';
import 'services/notification_service.dart';
import 'services/update_service.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Initialize Firebase (needed for App Distribution OTA updates).
  // Wrapped in try/catch so the app starts even if Google Play Services is missing.
  try {
    await Firebase.initializeApp();
  } catch (e) {
    debugPrint('[Firebase] Init failed: $e');
  }

  // Load persisted API config before anything renders.
  await ApiService().init();

  // Initialize push notifications via FCM.
  try {
    await NotificationService().init();
  } catch (e) {
    debugPrint('[FCM] Init failed (non-fatal): $e');
  }

  runApp(const RhodeyApp());
}

class RhodeyApp extends StatelessWidget {
  const RhodeyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Rhodey OS',
      theme: AppTheme.themeData,
      debugShowCheckedModeBanner: false,
      home: const MainShell(),
    );
  }
}

/// Feature flag: set to false at compile time to restore the old 4-tab shell.
///   flutter run --dart-define=USE_NEW_HOME=false
///   flutter build apk --dart-define=USE_NEW_HOME=false
const bool useNewHome = bool.fromEnvironment('USE_NEW_HOME', defaultValue: true);

class MainShell extends StatefulWidget {
  const MainShell({super.key});

  @override
  State<MainShell> createState() => _MainShellState();
}

class _MainShellState extends State<MainShell> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      UpdateService().check(context);
    });
  }

  @override
  Widget build(BuildContext context) {
    // Feature flag: new adaptive home or old 4-tab shell
    if (useNewHome) {
      return const AdaptiveHomeScreen();
    }

    // ── Legacy 4-tab shell (fully preserved for rollback) ──
    return _LegacyTabShell();
  }
}

/// The original 4-tab shell — kept intact for safe rollback.
class _LegacyTabShell extends StatefulWidget {
  @override
  State<_LegacyTabShell> createState() => _LegacyTabShellState();
}

class _LegacyTabShellState extends State<_LegacyTabShell> {
  int _selectedIndex = 0;

  final _screens = const [
    TalkScreen(),
    DumpScreen(),
    TodayScreen(),
    InboxScreen(),
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(
        index: _selectedIndex,
        children: _screens,
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _selectedIndex,
        onDestinationSelected: (i) => setState(() => _selectedIndex = i),
        indicatorColor: AppTheme.accentBg,
        backgroundColor: AppTheme.background,
        height: 64,
        destinations: const [
          NavigationDestination(icon: Icon(Icons.chat_bubble_outline), label: 'Talk'),
          NavigationDestination(icon: Icon(Icons.inbox_outlined), label: 'Captures'),
          NavigationDestination(icon: Icon(Icons.today_outlined), label: 'Today'),
          NavigationDestination(icon: Icon(Icons.checklist_outlined), label: 'Inbox'),
        ],
        labelBehavior: NavigationDestinationLabelBehavior.alwaysHide,
      ),
    );
  }
}
