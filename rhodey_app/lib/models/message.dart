enum MessageRole { user, rhodey }

enum MessageType { text, taskResult, noteResult, decision, enrichment }

/// Outbound send states — the trust pipeline.
///
/// pending → sending → sent → resolved
///                ↘ failed → pending (retry)
enum SendStatus { pending, sending, sent, failed, resolved }

class ChatMessage {
  final String id;
  final MessageRole role;
  final MessageType type;
  final String text;
  final DateTime timestamp;
  final List<String>? quickReplies;

  /// Only meaningful for user messages.
  /// null = no send (inbound / Rhodey message).
  final SendStatus? sendStatus;

  const ChatMessage({
    required this.id,
    required this.role,
    this.type = MessageType.text,
    required this.text,
    required this.timestamp,
    this.quickReplies,
    this.sendStatus,
  });

  bool get isUser => role == MessageRole.user;

  bool get isSending => sendStatus == SendStatus.sending;

  bool get isFailed => sendStatus == SendStatus.failed;

  bool get isRhodeyTyping => text == '...' && role == MessageRole.rhodey;

  String get timeString {
    final now = DateTime.now();
    final diff = now.difference(timestamp);
    if (diff.inMinutes < 1) return 'now';
    if (diff.inHours < 1) return '${diff.inMinutes}m';
    if (timestamp.day == now.day && timestamp.month == now.month && timestamp.year == now.year) {
      return '${timestamp.hour.toString().padLeft(2, '0')}:${timestamp.minute.toString().padLeft(2, '0')}';
    }
    return '${timestamp.day}/${timestamp.month}';
  }
}
