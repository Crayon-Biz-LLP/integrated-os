import 'dart:convert';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:package_info_plus/package_info_plus.dart';
import 'package:open_filex/open_filex.dart';
import 'api_config.dart';

/// Response from GET /api/app-version.
class _AppVersionResponse {
  final int versionCode;
  final String versionName;
  final String? downloadUrl;
  final String releaseNotes;
  final bool found;

  const _AppVersionResponse({
    required this.versionCode,
    required this.versionName,
    this.downloadUrl,
    this.releaseNotes = '',
    this.found = false,
  });

  factory _AppVersionResponse.fromJson(Map<String, dynamic> json) =>
      _AppVersionResponse(
        versionCode: (json['version_code'] as num?)?.toInt() ?? 0,
        versionName: json['version_name'] as String? ?? '',
        downloadUrl: json['download_url'] as String?,
        releaseNotes: json['release_notes'] as String? ?? '',
        found: json['found'] as bool? ?? false,
      );
}

/// In-app update checker.
///
/// Fetches latest version from /api/app-version on startup.
/// If the remote version_code > current version_code, shows a dialog.
/// On "Update Now", downloads the APK to internal storage and launches
/// the system package installer via OpenFilex.
class UpdateService {
  UpdateService._();
  static final UpdateService _instance = UpdateService._();
  factory UpdateService() => _instance;

  /// Tracks whether an update dialog has been shown this session.
  /// Prevents the dialog from appearing on every app resume after "Later".
  bool _dialogShownThisSession = false;

  /// Check for updates and show a dialog if one is available.
  /// Must be called after ApiService.init() so the base URL is loaded.
  ///
  /// [showFeedback] controls whether to surface non-update outcomes
  /// (errors, up-to-date) via snackbar. Set false for silent cold-start checks.
  Future<void> check(BuildContext context, {bool showFeedback = false}) async {
    // If we've already shown (or dismissed) the dialog this session, skip
    if (_dialogShownThisSession && !showFeedback) {
      return;
    }
    // 1. Read current app version
    PackageInfo info;
    try {
      info = await PackageInfo.fromPlatform();
    } catch (e) {
      debugPrint('[Update] Could not read package info: $e');
      if (showFeedback && context.mounted) {
        _showSnack(context, 'Could not check app version', isError: true);
      }
      return;
    }
    final currentCode = int.tryParse(info.buildNumber) ?? 0;

    // 2. Fetch latest version from backend
    final apiConfig = ApiConfig();
    await apiConfig.load();
    final url = '${apiConfig.baseUrl}/api/app-version';
    final headers = <String, String>{'Accept': 'application/json'};
    if (apiConfig.apiKey.isNotEmpty) {
      headers['X-API-Key'] = apiConfig.apiKey;
    }

    _AppVersionResponse remote;
    try {
      final resp = await http
          .get(Uri.parse(url), headers: headers)
          .timeout(const Duration(seconds: 10));
      if (resp.statusCode != 200) {
        debugPrint('[Update] Server returned ${resp.statusCode}');
        if (showFeedback && context.mounted) {
          _showSnack(context, 'Update check failed (server ${resp.statusCode})', isError: true);
        }
        return;
      }
      remote = _AppVersionResponse.fromJson(
          Map<String, dynamic>.from(jsonDecode(resp.body) as Map));
    } catch (e) {
      debugPrint('[Update] Failed to check for updates: $e');
      if (showFeedback && context.mounted) {
        _showSnack(context, 'Could not reach update server', isError: true);
      }
      return;
    }

    if (!remote.found || remote.downloadUrl == null) {
      debugPrint('[Update] No update found on server');
      if (showFeedback && context.mounted) {
        _showSnack(context, 'No update info available yet — push a build first');
      }
      return;
    }

    if (remote.versionCode <= currentCode) {
      debugPrint('[Update] Up to date ($currentCode >= ${remote.versionCode})');
      if (showFeedback && context.mounted) {
        _showSnack(context, '✓ Rhodey is up to date');
      }
      return;
    }

    debugPrint('[Update] Update available: v${remote.versionName} (code ${remote.versionCode})');

    if (!context.mounted) return;

    // Mark dialog as shown so it won't reappear on next foreground event
    _dialogShownThisSession = true;

    await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => _UpdateDialog(
        versionName: remote.versionName,
        releaseNotes: remote.releaseNotes,
      ),
    ).then((upgrade) {
      if (upgrade == true && context.mounted) {
        _downloadAndInstall(context, remote.downloadUrl!);
      }
    });
  }

  /// Check for updates showing full feedback (for manual "Check for updates" button).
  Future<void> checkNow(BuildContext context) => check(context, showFeedback: true);

  void _showSnack(BuildContext context, String message, {bool isError = false}) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(message, style: const TextStyle(fontSize: 13)),
        backgroundColor: isError ? const Color(0xFFEF5350) : const Color(0xFF34C759),
        duration: const Duration(seconds: 3),
        behavior: SnackBarBehavior.floating,
      ),
    );
  }

  Future<void> _downloadAndInstall(BuildContext context, String downloadUrl) async {
    // Show progress dialog
    if (!context.mounted) return;
    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (_) => const _DownloadDialog(),
    );

    try {
      final dir = Directory('/data/data/com.crayon.rhodey_app/files/downloads');
      if (!dir.existsSync()) dir.createSync(recursive: true);

      final filePath = '${dir.path}/rhodey-update.apk';
      final file = File(filePath);

      debugPrint('[Update] Downloading from $downloadUrl');
      final response = await http.get(Uri.parse(downloadUrl));

      if (response.statusCode != 200) {
        throw Exception('Download failed: ${response.statusCode}');
      }

      await file.writeAsBytes(response.bodyBytes);
      debugPrint('[Update] Downloaded ${response.bodyBytes.length} bytes to $filePath');

      if (context.mounted) {
        Navigator.of(context).pop(); // close progress dialog
      }

      // Launch system installer
      final result = await OpenFilex.open(filePath,
          type: 'application/vnd.android.package-archive');
      debugPrint('[Update] Installer result: $result');
    } catch (e) {
      debugPrint('[Update] Error: $e');
      if (context.mounted) {
        Navigator.of(context).pop(); // close progress dialog if still open
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Update failed: $e')),
        );
      }
    }
  }
}

// ── Dialog widgets ─────────────────────────────────────────────

class _UpdateDialog extends StatelessWidget {
  final String versionName;
  final String releaseNotes;

  const _UpdateDialog({
    required this.versionName,
    required this.releaseNotes,
  });

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: const Color(0xFF1A1A2E),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      title: Row(
        children: [
          const Icon(Icons.system_update, color: Color(0xFF7C83FD), size: 24),
          const SizedBox(width: 10),
          const Text('Update Available',
              style: TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.w600)),
        ],
      ),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Rhodey v$versionName is available.',
            style: const TextStyle(color: Color(0xFFB0B0C3), fontSize: 14),
          ),
          if (releaseNotes.isNotEmpty) ...[
            const SizedBox(height: 12),
            const Text("What's new:",
                style: TextStyle(color: Colors.white, fontSize: 13, fontWeight: FontWeight.w500)),
            const SizedBox(height: 4),
            Text(releaseNotes,
                style: const TextStyle(color: Color(0xFFB0B0C3), fontSize: 13)),
          ],
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(false),
          child: const Text('Later', style: TextStyle(color: Color(0xFF7C83FD))),
        ),
        ElevatedButton(
          onPressed: () => Navigator.of(context).pop(true),
          style: ElevatedButton.styleFrom(
            backgroundColor: const Color(0xFF7C83FD),
            foregroundColor: Colors.white,
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
          ),
          child: const Text('Update Now'),
        ),
      ],
    );
  }
}

class _DownloadDialog extends StatelessWidget {
  const _DownloadDialog();

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: const Color(0xFF1A1A2E),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      content: Row(
        children: [
          const SizedBox(
            width: 20, height: 20,
            child: CircularProgressIndicator(strokeWidth: 2, color: Color(0xFF7C83FD)),
          ),
          const SizedBox(width: 16),
          const Text('Downloading update...',
              style: TextStyle(color: Colors.white, fontSize: 14)),
        ],
      ),
    );
  }
}
