import 'package:flutter/material.dart';
import '../models/message.dart';
import '../services/api_service.dart';
import '../services/persona.dart';
import '../theme/app_theme.dart';
import '../voice/rhodey_voice.dart';
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

class _HistoryScreenState extends State<HistoryScreen>
    with WidgetsBindingObserver {
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

  /// Memoized card resolution keyed by message id — resolveCardData (regex
  /// parsing) runs at most once per message instead of on every rebuild.
  final _cardDataCache = <String, CardData?>{};

  /// Title→id index of todo tasks, fetched once on the first mark-done tap —
  /// no per-tap network round-trip, and fail-closed matching never completes
  /// the wrong task.
  final Map<String, int> _taskIdByTitle = {};
  bool _taskIndexLoaded = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _loadHistory();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _scrollController.dispose();
    super.dispose();
  }

  /// Refresh when the app returns to foreground — new messages may have
  /// arrived while backgrounded. Silent: no full-screen spinner flash.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      _loadHistory(silent: true);
    }
  }

  Future<void> _loadHistory({bool silent = false}) async {
    if (!silent) setState(() => _loading = true);
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
      limit: 60,
      offset: _serverRows,
    );
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
      // Server timestamps are UTC (timestamptz) — convert to device-local so
      // the bubble clock and Today/date labels match the phone's clock.
      final ts = createdAt.isNotEmpty
          ? ((DateTime.tryParse(createdAt) ?? DateTime.now()).toLocal())
          : DateTime.now();
      return ChatMessage(
        id: m['id'].toString(),
        role: role == 'user' ? MessageRole.user : MessageRole.rhodey,
        text: content,
        timestamp: ts,
        intent: m['intent'] as String?,
        ackTitle: m['title'] as String?,
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
    if (dt.year == yesterday.year &&
        dt.month == yesterday.month &&
        dt.day == yesterday.day) {
      return 'Yesterday';
    }
    final months = [
      'Jan',
      'Feb',
      'Mar',
      'Apr',
      'May',
      'Jun',
      'Jul',
      'Aug',
      'Sep',
      'Oct',
      'Nov',
      'Dec',
    ];
    return '${months[dt.month - 1]} ${dt.day}, ${dt.year}';
  }

  /// Resolves a task id from a card title — one /api/tasks fetch per screen
  /// visit (cached in an index), never one per tap. Fail-closed: an exact
  /// normalized match wins; a containment match is only accepted when exactly
  /// one task qualifies, so a "Fix login bug" card can't complete "Fix login
  /// bug on Android". Null → the existing "not found" snackbar.
  Future<int?> _taskIdForTitle(String title) async {
    if (!_taskIndexLoaded) {
      // Include committed (in_progress) tasks so completing a card still
      // resolves a task the user has already taken on.
      final result = await _api.getTasks(status: 'todo,in_progress');
      if (result.success && result.data != null) {
        // Only mark loaded on success — a transient failure retries on the
        // next tap instead of disabling lookup for the whole screen visit.
        _taskIndexLoaded = true;
        for (final t in result.data!) {
          final id = t['id'] as int?;
          final taskTitle = (t['title'] as String? ?? '').toLowerCase().trim();
          if (id != null && taskTitle.isNotEmpty) {
            // putIfAbsent: first match wins for duplicate titles (same
            // semantics as the old first-match loop).
            _taskIdByTitle.putIfAbsent(taskTitle, () => id);
          }
        }
      }
    }
    final lower = title.toLowerCase().trim();
    if (lower.isEmpty) return null;
    final exact = _taskIdByTitle[lower];
    if (exact != null) return exact;
    final candidates = <int>{};
    for (final e in _taskIdByTitle.entries) {
      if (e.key.contains(lower) || lower.contains(e.key)) {
        candidates.add(e.value);
      }
    }
    return candidates.length == 1 ? candidates.first : null;
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
      _messages.insert(
        0,
        ChatMessage(
          id: id,
          role: MessageRole.user,
          text: text,
          timestamp: DateTime.now(),
          sendStatus: SendStatus.sent,
        ),
      );
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
        SnackBar(
          content: Text(
            RhodeyVoice.failedToSend(),
            style: const TextStyle(fontSize: 12),
          ),
          backgroundColor: AppTheme.red,
          duration: const Duration(seconds: 2),
        ),
      );
      return;
    }
    final responseText = result.data is Map
        ? (result.data as Map)['response'] as String? ?? 'Got it.'
        : 'Got it.';
    setState(() {
      _messages.insert(
        0,
        ChatMessage(
          id: 'hr${DateTime.now().millisecondsSinceEpoch}',
          role: MessageRole.rhodey,
          text: responseText,
          timestamp: DateTime.now(),
        ),
      );
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

  /// resolveCardData, memoized by message id — the regex parser runs at most
  /// once per message instead of on every rebuild.
  CardData? _cachedCardData(ChatMessage msg) {
    if (!_cardDataCache.containsKey(msg.id)) {
      _cardDataCache[msg.id] = resolveCardData(
        intent: msg.intent,
        text: msg.text,
        title: msg.ackTitle,
        timestamp: msg.timestamp,
      );
    }
    return _cardDataCache[msg.id];
  }

  /// Render a Rhodey message — rich card when the text parses to one,
  /// otherwise a plain bubble. Mirrors the live conversation.
  Widget _buildMessageItem(ChatMessage msg) {
    if (msg.role == MessageRole.rhodey) {
      final cardData = _cachedCardData(msg);
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
        final taskId = await _taskIdForTitle(cardData.title);
        if (taskId != null && mounted) {
          await _api.updateTaskStatus(taskId, 'done');
        } else if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text(
                'Task already completed or not found.',
                style: TextStyle(fontSize: 12),
              ),
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
            child: Text(
              'Rhodey',
              style: AppTheme.caption.copyWith(
                color: AppTheme.accent,
                letterSpacing: 0.3,
              ),
            ),
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
        title: Text(PersonaStore.current.history),
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
                    entries.add(
                      Padding(
                        padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
                        child: Center(
                          child: TextButton.icon(
                            onPressed: _loadingEarlier ? null : _loadEarlier,
                            icon: _loadingEarlier
                                ? const SizedBox(
                                    width: 12,
                                    height: 12,
                                    child: CircularProgressIndicator(
                                      strokeWidth: 1.5,
                                    ),
                                  )
                                : const Icon(Icons.expand_less, size: 16),
                            label: const Text(
                              'Show earlier',
                              style: TextStyle(
                                fontSize: 12,
                                color: AppTheme.textSecondary,
                              ),
                            ),
                          ),
                        ),
                      ),
                    );
                  }
                  return entries;
                }(),
              ),
            ),
    );
  }
}
