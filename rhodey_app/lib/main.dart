import 'package:flutter/material.dart';
import 'package:firebase_core/firebase_core.dart';
import 'theme/app_theme.dart';
import 'screens/talk_screen.dart';
import 'screens/dump_screen.dart';
import 'screens/today_screen.dart';
import 'screens/inbox_screen.dart';
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

class MainShell extends StatefulWidget {
  const MainShell({super.key});

  @override
  State<MainShell> createState() => _MainShellState();
}

class _MainShellState extends State<MainShell> {
  int _selectedIndex = 0;

  // All 4 screens are mounted once and preserved via IndexedStack.
  final _screens = const [
    TalkScreen(),
    DumpScreen(),
    TodayScreen(),
    InboxScreen(),
  ];

  @override
  void initState() {
    super.initState();
    // Check for app updates after the first frame
    WidgetsBinding.instance.addPostFrameCallback((_) {
      UpdateService().check(context);
    });
  }

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
