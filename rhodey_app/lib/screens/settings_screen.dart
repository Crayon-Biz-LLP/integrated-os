import 'package:flutter/material.dart';
import '../services/api_config.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';
import 'how_rhodey_works_screen.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  final _config = ApiConfig();
  final _api = ApiService();
  final _urlController = TextEditingController();
  final _keyController = TextEditingController();
  bool _loading = true;
  bool _saving = false;
  bool _keyVisible = false;
  String? _statusMessage;

  @override
  void initState() {
    super.initState();
    _loadConfig();
  }

  void _loadConfig() {
    _urlController.text = _config.baseUrl;
    _keyController.text = _config.apiKey;
    setState(() => _loading = false);
  }

  Future<void> _save() async {
    setState(() {
      _saving = true;
      _statusMessage = null;
    });

    final url = _urlController.text.trim();
    final key = _keyController.text.trim();

    if (url.isEmpty) {
      setState(() {
        _statusMessage = 'Base URL is required';
        _saving = false;
      });
      return;
    }

    await _config.setBaseUrl(url);
    if (key.isNotEmpty) {
      await _config.setApiKey(key);
    }

    setState(() {
      _saving = false;
      _statusMessage = '✅ Settings saved';
    });

    // Auto-clear status after 2s
    Future.delayed(const Duration(seconds: 2), () {
      if (!mounted) return;
      setState(() => _statusMessage = null);
    });
  }

  /// M10: clears the onboarding demo artifacts (idempotent server-side).
  Future<void> _clearDemoItems() async {
    setState(() {
      _saving = true;
      _statusMessage = null;
    });
    try {
      final r = await _api.cleanupDemo();
      if (!mounted) return;
      final removed = r.success && r.data is Map
          ? (r.data as Map)['removed']
          : null;
      final parts = <String>[];
      if (removed is Map) {
        for (final e in removed.entries) {
          final n = e.value is num ? e.value as num : 0;
          if (n != 0) parts.add('${e.key}: $n');
        }
      }
      setState(() {
        _saving = false;
        _statusMessage = parts.isEmpty
            ? '✅ No demo items to clear'
            : '✅ Cleared ${parts.join(', ')}';
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _saving = false;
        _statusMessage = 'Could not clear demo items ($e)';
      });
    }
    Future.delayed(const Duration(seconds: 3), () {
      if (!mounted) return;
      setState(() => _statusMessage = null);
    });
  }

  Future<void> _clear() async {
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppTheme.surface,
        content: const Text(
          'Clear all API settings? You will need to re-enter them to use the app.',
          style: TextStyle(color: AppTheme.textSecondary, fontSize: 13),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Clear',
                style: TextStyle(color: AppTheme.red)),
          ),
        ],
      ),
    );

    if (confirm == true) {
      await _config.clear();
      _loadConfig();
      setState(() => _statusMessage = 'Settings cleared');
    }
  }

  @override
  void dispose() {
    _urlController.dispose();
    _keyController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Settings'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: AppTheme.textSecondary),
          onPressed: () => Navigator.pop(context),
        ),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : ListView(
              padding: const EdgeInsets.all(16),
              children: [
                // ── Connection section ──
                _SectionHeader('Connection'),
                const SizedBox(height: 8),
                _ConfigField(
                  label: 'Backend URL',
                  controller: _urlController,
                  hint: 'https://your-backend.vercel.app',
                  keyboardType: TextInputType.url,
                ),
                const SizedBox(height: 16),

                // ── API Key section ──
                _SectionHeader('API Key'),
                const SizedBox(height: 8),
                Container(
                  decoration: BoxDecoration(
                    color: AppTheme.surface,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: AppTheme.border),
                  ),
                  child: TextField(
                    controller: _keyController,
                    obscureText: !_keyVisible,
                    decoration: InputDecoration(
                      hintText: 'Enter your API key',
                      border: InputBorder.none,
                      contentPadding: const EdgeInsets.symmetric(
                        horizontal: 14, vertical: 12,
                      ),
                      isDense: true,
                      suffixIcon: IconButton(
                        icon: Icon(
                          _keyVisible
                              ? Icons.visibility_off
                              : Icons.visibility,
                          size: 18,
                          color: AppTheme.textTertiary,
                        ),
                        onPressed: () =>
                            setState(() => _keyVisible = !_keyVisible),
                      ),
                    ),
                    style: AppTheme.body,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  'Required for API authentication. Found in your Rhodey dashboard.',
                  style: TextStyle(
                    color: AppTheme.textTertiary.withValues(alpha: 0.7),
                    fontSize: 10,
                  ),
                ),
                const SizedBox(height: 24),

                // ── Status message ──
                if (_statusMessage != null)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 16),
                    child: Text(
                      _statusMessage!,
                      style: TextStyle(
                        color: _statusMessage!.startsWith('✅')
                            ? AppTheme.green
                            : AppTheme.amber,
                        fontSize: 12,
                      ),
                    ),
                  ),

                // ── Save button ──
                SizedBox(
                  width: double.infinity,
                  child: Material(
                    color: AppTheme.accent,
                    borderRadius: BorderRadius.circular(10),
                    child: InkWell(
                      borderRadius: BorderRadius.circular(10),
                      onTap: _saving ? null : _save,
                      child: Container(
                        padding: const EdgeInsets.symmetric(vertical: 14),
                        alignment: Alignment.center,
                        child: _saving
                            ? const SizedBox(
                                width: 18, height: 18,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                  color: Colors.white,
                                ),
                              )
                            : const Text(
                                'Save Settings',
                                style: TextStyle(
                                  color: Colors.white,
                                  fontSize: 14,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 12),

                // ── Clear button ──
                SizedBox(
                  width: double.infinity,
                  child: Material(
                    color: Colors.transparent,
                    borderRadius: BorderRadius.circular(10),
                    child: InkWell(
                      borderRadius: BorderRadius.circular(10),
                      onTap: _clear,
                      child: Container(
                        padding: const EdgeInsets.symmetric(vertical: 12),
                        alignment: Alignment.center,
                        child: Text(
                          'Clear Settings',
                          style: TextStyle(
                            color: AppTheme.textTertiary.withValues(alpha: 0.7),
                            fontSize: 12,
                          ),
                        ),
                      ),
                    ),
                  ),
                ),

                const SizedBox(height: 32),

                // ── Demo cleanup (M10) ──
                _SectionHeader('Demo'),
                const SizedBox(height: 8),
                Container(
                  decoration: BoxDecoration(
                    color: AppTheme.surface,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: AppTheme.border),
                  ),
                  child: Material(
                    color: Colors.transparent,
                    child: InkWell(
                      borderRadius: BorderRadius.circular(10),
                      onTap: _saving ? null : _clearDemoItems,
                      child: Padding(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 14,
                          vertical: 13,
                        ),
                        child: Row(
                          children: [
                            const Icon(
                              Icons.cleaning_services_outlined,
                              size: 18,
                              color: AppTheme.textSecondary,
                            ),
                            const SizedBox(width: 12),
                            const Expanded(
                              child: Text(
                                'Clear onboarding demo items',
                                style: TextStyle(
                                  color: AppTheme.textPrimary,
                                  fontSize: 13,
                                ),
                              ),
                            ),
                            const Icon(
                              Icons.chevron_right,
                              size: 18,
                              color: AppTheme.textTertiary,
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 12),

                // ── Help ──
                _SectionHeader('Help'),
                const SizedBox(height: 8),
                Container(
                  decoration: BoxDecoration(
                    color: AppTheme.surface,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: AppTheme.border),
                  ),
                  child: Material(
                    color: Colors.transparent,
                    child: InkWell(
                      borderRadius: BorderRadius.circular(10),
                      onTap: () => Navigator.push(
                        context,
                        MaterialPageRoute(
                          builder: (_) => const HowRhodeyWorksScreen(),
                        ),
                      ),
                      child: Padding(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 14,
                          vertical: 13,
                        ),
                        child: Row(
                          children: [
                            const Icon(
                              Icons.help_outline,
                              size: 18,
                              color: AppTheme.textSecondary,
                            ),
                            const SizedBox(width: 12),
                            const Expanded(
                              child: Text(
                                'How Rhodey works',
                                style: TextStyle(
                                  color: AppTheme.textPrimary,
                                  fontSize: 13,
                                ),
                              ),
                            ),
                            const Icon(
                              Icons.chevron_right,
                              size: 18,
                              color: AppTheme.textTertiary,
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
                const SizedBox(height: 32),

                // ── App info ──
                _SectionHeader('About'),
                const SizedBox(height: 8),
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: AppTheme.surface,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: AppTheme.border),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      _infoRow('App', 'Rhodey OS'),
                      const SizedBox(height: 6),
                      _infoRow('Platform', 'Flutter'),
                      const SizedBox(height: 6),
                      _infoRow('Status', _config.isConfigured
                          ? '✅ Configured'
                          : '⚠️ Not configured'),
                    ],
                  ),
                ),
              ],
            ),
    );
  }

  Widget _infoRow(String label, String value) {
    return Row(
      children: [
        Text(
          label,
          style: TextStyle(
            color: AppTheme.textTertiary.withValues(alpha: 0.7),
            fontSize: 11,
          ),
        ),
        const Spacer(),
        Text(
          value,
          style: const TextStyle(
            color: AppTheme.textSecondary,
            fontSize: 12,
          ),
        ),
      ],
    );
  }
}

class _SectionHeader extends StatelessWidget {
  final String title;
  const _SectionHeader(this.title);

  @override
  Widget build(BuildContext context) {
    return Text(
      title.toUpperCase(),
      style: TextStyle(
        color: AppTheme.textTertiary.withValues(alpha: 0.6),
        fontSize: 10,
        letterSpacing: 0.8,
        fontWeight: FontWeight.w600,
      ),
    );
  }
}

class _ConfigField extends StatelessWidget {
  final String label;
  final TextEditingController controller;
  final String? hint;
  final TextInputType? keyboardType;

  const _ConfigField({
    required this.label,
    required this.controller,
    this.hint,
    this.keyboardType,
  });

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: TextStyle(
            color: AppTheme.textTertiary.withValues(alpha: 0.7),
            fontSize: 11,
          ),
        ),
        const SizedBox(height: 6),
        Container(
          decoration: BoxDecoration(
            color: AppTheme.surface,
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: AppTheme.border),
          ),
          child: TextField(
            controller: controller,
            keyboardType: keyboardType,
            decoration: InputDecoration(
              hintText: hint,
              border: InputBorder.none,
              contentPadding: const EdgeInsets.symmetric(
                horizontal: 14, vertical: 12,
              ),
              isDense: true,
            ),
            style: AppTheme.body,
          ),
        ),
      ],
    );
  }
}
