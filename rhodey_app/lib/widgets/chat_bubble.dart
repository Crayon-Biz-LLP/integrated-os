import 'package:flutter/material.dart';
import '../models/message.dart';
import '../theme/app_theme.dart';

class ChatBubble extends StatelessWidget {
  final ChatMessage message;
  final bool isGroupStart;
  final VoidCallback? onRetry;
  final VoidCallback? onTap;

  const ChatBubble({
    super.key,
    required this.message,
    this.isGroupStart = true,
    this.onRetry,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final isUser = message.isUser;

    final screenWidth = MediaQuery.of(context).size.width;
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: isUser ? CrossAxisAlignment.end : CrossAxisAlignment.start,
        children: [
          if (isGroupStart && !isUser)
            Padding(
              padding: const EdgeInsets.only(left: 16, bottom: 4),
              child: Text(
                'Rhodey',
                style: AppTheme.caption.copyWith(
                  color: AppTheme.accent,
                  letterSpacing: 0.3,
                ),
              ),
            ),

          // Failed state — show message + retry button inline
          if (message.isFailed)
            _buildFailedBubble(isUser, screenWidth)
          else
            // Wrap normal Rhodey bubbles in a GestureDetector for TTS on tap
            // with a subtle speaker icon hint for discoverability
            (isUser || onTap == null)
                ? _buildNormalBubble(isUser, screenWidth)
                : GestureDetector(
                    onTap: onTap,
                    child: Stack(
                      children: [
                        _buildNormalBubble(isUser, screenWidth),
                        // Speaker icon hint — bottom-right of the bubble
                        Positioned(
                          right: 6,
                          bottom: 6,
                          child: Icon(
                            Icons.volume_up_outlined,
                            size: 10,
                            color: AppTheme.textTertiary.withValues(alpha: 0.4),
                          ),
                        ),
                      ],
                    ),
                  ),

          // Quick-reply chips
          if (message.quickReplies != null && message.quickReplies!.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8, left: 16),
              child: Wrap(
                spacing: 6, runSpacing: 6,
                children: message.quickReplies!.map((reply) {
                  return Material(
                    color: AppTheme.surfaceAlt,
                    borderRadius: BorderRadius.circular(8),
                    child: InkWell(
                      borderRadius: BorderRadius.circular(8),
                      onTap: () {},
                      child: Container(
                        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                        decoration: BoxDecoration(
                          borderRadius: BorderRadius.circular(8),
                          border: Border.all(color: AppTheme.borderLight, width: 1),
                        ),
                        child: Text(reply, style: AppTheme.bodySmall.copyWith(color: AppTheme.accent)),
                      ),
                    ),
                  );
                }).toList(),
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildNormalBubble(bool isUser, double screenWidth) {
    return Container(
      constraints: BoxConstraints(maxWidth: screenWidth * 0.72),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      decoration: BoxDecoration(
        color: isUser ? AppTheme.userBubble : AppTheme.botBubble,
        borderRadius: BorderRadius.only(
          topLeft: const Radius.circular(16),
          topRight: const Radius.circular(16),
          bottomLeft: Radius.circular(isUser ? 16 : 4),
          bottomRight: Radius.circular(isUser ? 4 : 16),
        ),
        border: Border.all(
          color: isUser ? AppTheme.accent.withValues(alpha: 0.15) : AppTheme.border,
          width: 1,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(message.text, style: AppTheme.body.copyWith(height: 1.5)),
          const SizedBox(height: 4),
          Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(message.timeString, style: AppTheme.caption.copyWith(fontSize: 10, color: AppTheme.textTertiary)),
              if (isUser) ...[
                const SizedBox(width: 4),
                _sendStatusIcon(),
              ],
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildFailedBubble(bool isUser, double screenWidth) {
    return Container(
      constraints: BoxConstraints(maxWidth: screenWidth * 0.72),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
      decoration: BoxDecoration(
        color: AppTheme.redBg,
        borderRadius: BorderRadius.only(
          topLeft: const Radius.circular(16),
          topRight: const Radius.circular(16),
          bottomLeft: Radius.circular(isUser ? 16 : 4),
          bottomRight: Radius.circular(isUser ? 4 : 16),
        ),
        border: Border.all(
          color: AppTheme.red.withValues(alpha: 0.3),
          width: 1,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(message.text, style: AppTheme.body.copyWith(height: 1.5)),
          const SizedBox(height: 8),
          Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.error_outline, size: 12, color: AppTheme.red),
              const SizedBox(width: 4),
              Text('Failed to send', style: AppTheme.caption.copyWith(
                  fontSize: 10, color: AppTheme.red)),
              const Spacer(),
              if (onRetry != null)
                Material(
                  color: AppTheme.red.withValues(alpha: 0.1),
                  borderRadius: BorderRadius.circular(6),
                  child: InkWell(
                    borderRadius: BorderRadius.circular(6),
                    onTap: onRetry,
                    child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(Icons.refresh, size: 12, color: AppTheme.red),
                          const SizedBox(width: 3),
                          Text('Retry', style: AppTheme.caption.copyWith(
                              fontSize: 10, color: AppTheme.red, fontWeight: FontWeight.w600)),
                        ],
                      ),
                    ),
                  ),
                ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _sendStatusIcon() {
    switch (message.sendStatus) {
      case SendStatus.pending:
      case SendStatus.sending:
        return SizedBox(
          width: 10, height: 10,
          child: CircularProgressIndicator(
            strokeWidth: 1.5, color: AppTheme.textTertiary,
          ),
        );
      case SendStatus.sent:
        return Icon(Icons.check, size: 12, color: AppTheme.textTertiary);
      case SendStatus.resolved:
        return Icon(Icons.check_circle, size: 12, color: AppTheme.green);
      case SendStatus.failed:
        return Icon(Icons.error_outline, size: 12, color: AppTheme.red);
      default:
        return const SizedBox.shrink();
    }
  }
}
