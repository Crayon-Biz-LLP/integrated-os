import 'package:flutter/material.dart';
import '../models/message.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';
import '../widgets/chat_bubble.dart';
import '../widgets/rich_card_content.dart';

/// Full user↔Rhodey conversation history.
///
/// Reads the thread-scoped `conversations` table via /api/conversation-history
/// (NOT raw_dumps — that's a noisy stream of pulses, briefings and system
/// noise). Renders the same rich-card interactions as the live conversation:
/// task cards with a working "Mark done" button, and quick-reply chips that
/// send their text to Rhodey.
class HistoryScreen extends StatefulWidget {
  const HistoryScreen({super.key});

  @override
  State<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends State<HistoryScreen> {
  final _api = ApiService();
  final _scrollController = ScrollController();
  List<ChatMessage> _messages = [];
  bool _loading = true;
  bool _loadingEarlier = false;
  bool _hasMore = true;
  String _error = '';

  /// Count of rows that actually came from the server. Local echoes inserted
  /// by _sendReply must NOT advance the pagination offset, or "Show earlier"
  /// would skip real conversation rows.
  int _serverRows = 0;

  @override
  void initState() {
    super.initState();
    _loadHistory();
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  Future<void> _loadHistory() async {
    setState(() => _loading = true);
    final result = await _api.getConversationHistory(limit: 60);
    if (!mounted) return;

    if (result.success) {
      _messages = _mapMessages(result.data ?? []);
      _serverRows = _messages.length;
      _hasMore = (result.data?.length ?? 0) >= 60;
      _error = '';
    } else {
      _error = result.error ?? 'Failed to load history';
    }

    if (mounted) {
      setState(() => _loading = false);
    }

    // reverse:true pins the viewport to offset 0 = the BOTTOM (most recent
    // message). A chat list never needs a jumpTo — it always opens on the
    // latest exchange, like a messaging app.
  }

  /// Load an older page of messages and prepend them (API returns newest-first).
  Future<void> _loadEarlier() async {
    if (_loadingEarlier || !_hasMore) return;
    setState(() => _loadingEarlier = true);
    final result = await _api.getConversationHistory(
        limit: 60, offset: _serverRows);
    if (!mounted) return;

    if (result.success && result.data!.isNotEmpty) {
      final older = _mapMessages(result.data!);
      setState(() {
        // Keep _messages globally newest-first: the build renders children[0]
        // at the BOTTOM (reverse:true), so the older page must be APPENDED at
        // the end to sit at the visual top — not prepended.
        _messages = [..._messages, ...older];
        _serverRows += older.length;
        _hasMore = result.data!.length >= 60;
      });
    } else {
      setState(() => _hasMore = false);
    }
    if (mounted) {
      setState(() => _loadingEarlier = false);
    }
  }

  List<ChatMessage> _mapMessages(List<Map<String, dynamic>> rows) {
    return rows.map((m) {
      final content = m['content'] as String? ?? '';
      final role = m['role'] as String? ?? 'bot';
      final createdAt = m['created_at'] as String? ?? '';
      final ts = createdAt.isNotEmpty
          ? (DateTime.tryParse(createdAt) ?? DateTime.now())
          : DateTime.now();
      return ChatMessage(
        id: m['id'].toString(),
        role: role == 'user' ? MessageRole.user : MessageRole.rhodey,
        text: content,
        timestamp: ts,
        intent: m['intent'] as String?,
        sendStatus: role == 'user' ? SendStatus.sent : null,
      );
    }).toList();
  }

  /// Group messages by date for section headers, preserving the iteration
  /// order of [msgs] (callers pass newest-first data).
  Map<String, List<ChatMessage>> _groupByDate(List<ChatMessage> msgs) {
    final groups = <String, List<ChatMessage>>{};
    for (final m in msgs) {
      final key = _dateLabel(m.timestamp);
      groups.putIfAbsent(key, () => []).add(m);
    }
    return groups;
  }

  String _dateLabel(DateTime dt) {
    final now = DateTime.now();
    if (dt.year == now.year && dt.month == now.month && dt.day == now.day) {
      return 'Today';
    }
    final yesterday = now.subtract(const Duration(days: 1));
    if (dt.year == yesterday.year && dt.month == yesterday.month && dt.day == yesterday.day) {
      return 'Yesterday';
    }
    final months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return '${months[dt.month - 1]} ${dt.day}, ${dt.year}';
  }

  /// Find a task ID from a card title by fetching open tasks — same logic as
  /// the live conversation's rich cards.
  Future<int?> _findTaskIdForCard(String title) async {
    final result = await _api.getTasks(status: 'todo');
    if (!result.success || result.data == null) return null;
    final lower = title.toLowerCase().trim();
    for (final t in result.data!) {
      final taskTitle = (t['title'] as String? ?? '').toLowerCase().trim();
      if (taskTitle == lower || taskTitle.contains(lower) || lower.contains(taskTitle)) {
        return t['id'] as int?;
      }
    }
    return null;
  }

  /// Send a message (used by quick-reply chips) and prepend the local echo.
  ///
  /// _messages is stored newest-first (the API returns desc, and the display
  /// reverses for chronological order), so new messages must go at index 0 —
  /// appending would drop them at the oldest position.
  Future<void> _sendReply(String text) async {
    if (text.trim().isEmpty) return;
    final id = 'h${DateTime.now().millisecondsSinceEpoch}';
    setState(() {
      _messages.insert(0, ChatMessage(
        id: id,
        role: MessageRole.user,
        text: text,
        timestamp: DateTime.now(),
        sendStatus: SendStatus.sent,
      ));
    });
    final result = await _api.sendMessage(text.trim());
    if (!mounted) return;
    if (!result.success) {
      // Flip the local echo to failed so it reads honestly (like the live
      // conversation's failed bubbles) and offer a retry.
      setState(() {
        final idx = _messages.indexWhere((m) => m.id == id);
        if (idx != -1) {
          _messages[idx] = ChatMessage(
            id: id,
            role: MessageRole.user,
            text: text,
            timestamp: _messages[idx].timestamp,
            sendStatus: SendStatus.failed,
          );
        }
      });
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Failed to send — check your connection.', style: TextStyle(fontSize: 12)),
          backgroundColor: AppTheme.red,
          duration: Duration(seconds: 2),
        ),
      );
      return;
    }
    final responseText = result.data is Map
        ? (result.data as Map)['response'] as String? ?? 'Got it.'
        : 'Got it.';
    setState(() {
      _messages.insert(0, ChatMessage(
        id: 'hr${DateTime.now().millisecondsSinceEpoch}',
        role: MessageRole.rhodey,
        text: responseText,
        timestamp: DateTime.now(),
      ));
    });
    // reverse:true → offset 0 IS the bottom. Glide the new exchange into view,
    // retrying until the ListView attaches (same hardening as the home screen).
    _retryScrollToBottom(0);
  }

  /// Glide the chat to the newest message (offset 0 = bottom with reverse:true),
  /// retrying until the ListView attaches — the list may not have clients yet
  /// when the reply is added (e.g. still mid history load).
  void _retryScrollToBottom(int attempt) {
    if (!mounted) return;
    if (!_scrollController.hasClients) {
      if (attempt >= 10) return; // give up after ~1.2s
      Future.delayed(const Duration(milliseconds: 120), () {
        if (mounted) _retryScrollToBottom(attempt + 1);
      });
      return;
    }
    // Measure after the frame so the just-inserted exchange's layout is
    // included before gliding to the bottom.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_scrollController.hasClients) return;
      _scrollController.animateTo(
        0,
        duration: const Duration(milliseconds: 250),
        curve: Curves.easeOut,
      );
    });
  }

  /// Render a Rhodey message — rich card when the text parses to one,
  /// otherwise a plain bubble. Mirrors the live conversation.
  Widget _buildMessageItem(ChatMessage msg) {
    if (msg.role == MessageRole.rhodey) {
      final cardData = resolveCardData(
        intent: msg.intent,
        text: msg.text,
        timestamp: msg.timestamp,
      );
      if (cardData != null) {
        return _buildRichCard(msg, cardData);
      }
    }
    return ChatBubble(
      message: msg,
      isGroupStart: true,
      onQuickReply: msg.quickReplies?.isNotEmpty == true
          ? (reply) => _sendReply(reply)
          : null,
    );
  }

  Widget _buildRichCard(ChatMessage msg, CardData cardData) {
    VoidCallback? onMarkDone;
    if (cardData.type == CardType.task) {
      onMarkDone = () async {
        final taskId = await _findTaskIdForCard(cardData.title);
        if (taskId != null && mounted) {
          await _api.updateTaskStatus(taskId, 'done');
        } else if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text('Task already completed or not found.', style: TextStyle(fontSize: 12)),
              backgroundColor: AppTheme.amber,
              duration: Duration(seconds: 2),
            ),
          );
        }
      };
    }

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.only(left: 16, bottom: 4),
            child: Text('Rhodey', style: AppTheme.caption.copyWith(
              color: AppTheme.accent, letterSpacing: 0.3,
            )),
          ),
          RichCardContent(
            cardData: cardData,
            onMarkDone: onMarkDone,
            quickReplies: msg.quickReplies,
            onQuickReply: msg.quickReplies?.isNotEmpty == true
                ? (reply) => _sendReply(reply)
                : null,
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('History'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: AppTheme.textSecondary),
          onPressed: () => Navigator.pop(context),
        ),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _messages.isEmpty
              ? Center(
                  child: Text(
                    _error.isNotEmpty ? '⚠️ $_error' : 'No conversation yet',
                    style: AppTheme.body.copyWith(color: AppTheme.textTertiary),
                  ),
                )
              : RefreshIndicator(
                  onRefresh: _loadHistory,
                  // reverse:true → child 0 renders at the BOTTOM, so the list
                  // opens on the most recent message with zero jumping.
                  child: ListView(
                    reverse: true,
                    controller: _scrollController,
                    padding: const EdgeInsets.only(bottom: 24),
                    children: () {
                      // _messages is newest-first; with reverse:true the first
                      // child sits at the bottom, so iterate newest-first:
                      // newest date group at the bottom, oldest at the top.
                      final groups = _groupByDate(_messages);
                      final entries = <Widget>[];

                      for (final date in groups.keys) {
                        // With reverse:true the LAST child of a group renders
                        // ABOVE it — so the date header goes AFTER the group's
                        // messages to sit on top of them (newest date group is
                        // first in the list → bottom of the screen).
                        for (final msg in groups[date]!) {
                          entries.add(_buildMessageItem(msg));
                        }
                        entries.add(
                          Padding(
                            padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
                            child: Text(
                              date,
                              style: AppTheme.label.copyWith(
                                color: AppTheme.textTertiary,
                                fontSize: 12,
                              ),
                            ),
                          ),
                        );
                      }

                      // 'Show earlier' sits at the very TOP — with reverse:true
                      // that means it must be the LAST child.
                      if (_hasMore) {
                        entries.add(Padding(
                          padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
                          child: Center(
                            child: TextButton.icon(
                              onPressed: _loadingEarlier ? null : _loadEarlier,
                              icon: _loadingEarlier
                                  ? const SizedBox(
                                      width: 12, height: 12,
                                      child: CircularProgressIndicator(strokeWidth: 1.5))
                                  : const Icon(Icons.expand_less, size: 16),
                              label: const Text('Show earlier',
                                  style: TextStyle(fontSize: 12, color: AppTheme.textSecondary)),
                            ),
                          ),
                        ));
                      }
                      return entries;
                    }(),
                  ),
                ),
    );
  }
}
