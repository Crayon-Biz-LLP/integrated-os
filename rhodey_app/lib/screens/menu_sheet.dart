import 'package:flutter/material.dart';
import '../theme/app_theme.dart';
import 'today_screen.dart';
import 'dump_screen.dart';
import 'inbox_screen.dart';
import 'history_screen.dart';
import '../services/api_service.dart';

/// Shows the menu bottom sheet from the adaptive home screen.
///
/// Opens existing screens (Today, Captures, Inbox, History, Settings)
/// as full-screen routes via Navigator.push.
void showMenuSheet(BuildContext context) {
  // Track menu open for instrumentation
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
              width: 36,
              height: 4,
              decoration: BoxDecoration(
                color: AppTheme.borderLight,
                borderRadius: BorderRadius.circular(2),
              ),
            ),
            const SizedBox(height: 20),

            _MenuTile(
              icon: Icons.today_outlined,
              label: 'Today',
              onTap: () {
                Navigator.pop(ctx);
                Navigator.push(
                  context,
                  MaterialPageRoute(builder: (_) => const TodayScreen()),
                );
              },
            ),
            _MenuTile(
              icon: Icons.inbox_outlined,
              label: 'Captures',
              onTap: () {
                Navigator.pop(ctx);
                Navigator.push(
                  context,
                  MaterialPageRoute(builder: (_) => const DumpScreen()),
                );
              },
            ),
            _MenuTile(
              icon: Icons.checklist_outlined,
              label: 'Inbox',
              onTap: () {
                Navigator.pop(ctx);
                Navigator.push(
                  context,
                  MaterialPageRoute(builder: (_) => const InboxScreen()),
                );
              },
            ),
            _MenuTile(
              icon: Icons.history,
              label: 'History',
              onTap: () {
                Navigator.pop(ctx);
                Navigator.push(
                  context,
                  MaterialPageRoute(builder: (_) => const HistoryScreen()),
                );
              },
            ),
            _MenuTile(
              icon: Icons.settings_outlined,
              label: 'Settings',
              onTap: () {
                Navigator.pop(ctx);
                Navigator.push(
                  context,
                  MaterialPageRoute(
                    builder: (_) => const _SettingsScreen(),
                  ),
                );
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

/// Minimal API settings screen (moved from talk_screen.dart to be reusable).
class _SettingsScreen extends StatefulWidget {
  const _SettingsScreen();

  @override
  State<_SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<_SettingsScreen> {
  final _api = ApiService();
  late final TextEditingController _urlCtrl;
  late final TextEditingController _keyCtrl;
  bool _saving = false;
  bool _saved = false;

  @override
  void initState() {
    super.initState();
    _urlCtrl = TextEditingController(text: _api.config.baseUrl);
    _keyCtrl = TextEditingController(text: _api.config.apiKey);
  }

  @override
  void dispose() {
    _urlCtrl.dispose();
    _keyCtrl.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    await _api.config.setBaseUrl(_urlCtrl.text.trim());
    await _api.config.setApiKey(_keyCtrl.text.trim());
    if (mounted) {
      setState(() {
        _saving = false;
        _saved = true;
      });
      Future.delayed(const Duration(seconds: 2), () {
        if (mounted) setState(() => _saved = false);
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('API Settings'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: AppTheme.textSecondary),
          onPressed: () => Navigator.pop(context),
        ),
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: _api.config.isConfigured
                  ? AppTheme.greenBg
                  : AppTheme.redBg,
              borderRadius: BorderRadius.circular(12),
              border: Border.all(
                color: _api.config.isConfigured
                    ? AppTheme.green.withValues(alpha: 0.3)
                    : AppTheme.red.withValues(alpha: 0.3),
              ),
            ),
            child: Row(
              children: [
                Icon(
                  _api.config.isConfigured
                      ? Icons.check_circle
                      : Icons.warning,
                  color: _api.config.isConfigured
                      ? AppTheme.green
                      : AppTheme.red,
                  size: 20,
                ),
                const SizedBox(width: 12),
                Text(
                  _api.config.isConfigured
                      ? 'Connected to Rhodey API'
                      : 'Not configured — set your API key below',
                  style: AppTheme.body.copyWith(fontSize: 13),
                ),
              ],
            ),
          ),
          const SizedBox(height: 24),
          const Text('Base URL', style: AppTheme.label),
          const SizedBox(height: 8),
          Container(
            decoration: BoxDecoration(
              color: AppTheme.surfaceAlt,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: AppTheme.border),
            ),
            child: TextField(
              controller: _urlCtrl,
              style: AppTheme.body.copyWith(fontSize: 13),
              decoration: const InputDecoration(
                hintText: 'https://integrated-os.vercel.app',
                border: InputBorder.none,
                contentPadding: EdgeInsets.symmetric(
                    horizontal: 14, vertical: 12),
                isDense: true,
              ),
            ),
          ),
          const SizedBox(height: 16),
          const Text('API Key (X-API-Key)', style: AppTheme.label),
          const SizedBox(height: 8),
          Container(
            decoration: BoxDecoration(
              color: AppTheme.surfaceAlt,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: AppTheme.border),
            ),
            child: TextField(
              controller: _keyCtrl,
              obscureText: true,
              style: AppTheme.body.copyWith(fontSize: 13),
              decoration: const InputDecoration(
                hintText: 'Paste your API key here',
                border: InputBorder.none,
                contentPadding: EdgeInsets.symmetric(
                    horizontal: 14, vertical: 12),
                isDense: true,
              ),
            ),
          ),
          const SizedBox(height: 24),
          SizedBox(
            width: double.infinity,
            height: 48,
            child: ElevatedButton(
              onPressed: _saving ? null : _save,
              style: ElevatedButton.styleFrom(
                backgroundColor: AppTheme.accent,
                foregroundColor: Colors.white,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: _saving
                  ? const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(
                          strokeWidth: 2, color: Colors.white),
                    )
                  : Text(_saved ? '✓ Saved' : 'Save'),
            ),
          ),
        ],
      ),
    );
  }
}
