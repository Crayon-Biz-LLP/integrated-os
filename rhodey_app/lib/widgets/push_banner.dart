import 'package:flutter/material.dart';
import '../theme/app_theme.dart';

/// Instant-content banner for screens opened from a notification tap.
///
/// WhatsApp-style delivery: the pushed `content` rides inside the FCM payload,
/// so the target screen (Inbox, Today) can render it the moment the app opens —
/// no waiting on the screen's own data fetch. The banner sits above the loading
/// spinner / real list and disappears once fresh data arrives.
class PushBanner extends StatelessWidget {
  final String content;
  final String? title;

  const PushBanner({super.key, required this.content, this.title});

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 12, 16, 4),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [AppTheme.accentBg, AppTheme.surface],
        ),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppTheme.accent.withValues(alpha: 0.25)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 3,
            height: 40,
            decoration: BoxDecoration(
              color: AppTheme.accent,
              borderRadius: BorderRadius.circular(2),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (title != null && title!.isNotEmpty) ...[
                  Text(
                    title!,
                    style: AppTheme.caption.copyWith(
                      color: AppTheme.accent,
                      fontSize: 10,
                      letterSpacing: 1.2,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 3),
                ],
                Text(
                  content,
                  style: AppTheme.body.copyWith(fontSize: 13, height: 1.4),
                ),
              ],
            ),
          ),
          const Icon(Icons.bolt, size: 14, color: AppTheme.accent),
        ],
      ),
    );
  }
}
