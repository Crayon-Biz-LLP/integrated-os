import 'package:flutter/material.dart';
import '../services/persona.dart';
import '../theme/app_theme.dart';
import 'today_screen.dart';
import 'entities_screen.dart';
import 'inbox_screen.dart';
import 'history_screen.dart';
import 'settings_screen.dart';

/// Shows the menu bottom sheet from the adaptive home screen.
///
/// Opens existing screens (Today, Captures, Inbox, History, Settings)
/// as full-screen routes via Navigator.push.
void showMenuSheet(BuildContext context) {
  debugPrint('[MenuSheet] opened');

  showModalBottomSheet(
    context: context,
    backgroundColor: AppTheme.surface,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
    ),
    builder: (ctx) => SafeArea(
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 36, height: 4,
              decoration: BoxDecoration(
                color: AppTheme.borderLight,
                borderRadius: BorderRadius.circular(2),
              ),
            ),
            const SizedBox(height: 20),

            _MenuTile(
              icon: Icons.today_outlined,
              label: PersonaStore.current.today,
              onTap: () {
                Navigator.pop(ctx);
                Navigator.push(context,
                    MaterialPageRoute(builder: (_) => const TodayScreen()));
              },
            ),
            _MenuTile(
              icon: Icons.account_tree_outlined,
              label: PersonaStore.current.entities,
              onTap: () {
                Navigator.pop(ctx);
                Navigator.push(context,
                    MaterialPageRoute(builder: (_) => const EntitiesScreen()));
              },
            ),
            _MenuTile(
              icon: Icons.checklist_outlined,
              label: PersonaStore.current.inbox,
              onTap: () {
                Navigator.pop(ctx);
                Navigator.push(context,
                    MaterialPageRoute(builder: (_) => const InboxScreen()));
              },
            ),
            _MenuTile(
              icon: Icons.history,
              label: PersonaStore.current.history,
              onTap: () {
                Navigator.pop(ctx);
                Navigator.push(context,
                    MaterialPageRoute(builder: (_) => const HistoryScreen()));
              },
            ),
            _MenuTile(
              icon: Icons.settings_outlined,
              label: 'Settings',
              onTap: () {
                Navigator.pop(ctx);
                Navigator.push(context,
                    MaterialPageRoute(builder: (_) => const SettingsScreen()));
              },
            ),
          ],
        ),
      ),
    ),
  );
}

class _MenuTile extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  const _MenuTile({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return ListTile(
      leading: Icon(icon, color: AppTheme.textSecondary, size: 22),
      title: Text(label, style: AppTheme.body),
      onTap: onTap,
    );
  }
}
