enum MessageRole { user, rhodey }

enum MessageType { text, taskResult, noteResult, decision, enrichment, taskList, documentReview }

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

  /// Rows for a Rhodey task-ledger message (type == taskList).
  final List<Map<String, dynamic>>? taskList;

  /// Structured intent label from the backend (e.g. BRIEFING, RESPONSE,
  /// QUERY, CLARIFICATION, WORKFLOW_RESOLUTION). Drives which card renders:
  /// briefings become Moment cards, clarifications become prompt cards, etc.
  final String? intent;

  /// Bare entity title (task/note/event name) for ack messages. Rides along
  /// with [intent] on the message row so the card shows the task name and
  /// Mark-Done works — the ack text itself is voice-rendered and unparsed.
  final String? ackTitle;

  /// Only meaningful for user messages.
  /// null = no send (inbound / Rhodey message).
  final SendStatus? sendStatus;

  /// Structured document breakdown for document review cards.
  /// Populated when MessageType == documentReview.
  final Map<String, dynamic>? documentBreakdown;

  const ChatMessage({
    required this.id,
    required this.role,
    this.type = MessageType.text,
    required this.text,
    required this.timestamp,
    this.quickReplies,
    this.taskList,
    this.intent,
    this.ackTitle,
    this.sendStatus,
    this.documentBreakdown,
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
