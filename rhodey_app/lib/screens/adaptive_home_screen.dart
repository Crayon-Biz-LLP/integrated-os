import 'dart:async';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:flutter_tts/flutter_tts.dart';
import 'menu_sheet.dart';
import 'inbox_screen.dart';
import 'today_screen.dart';
import '../models/message.dart';
import '../services/notification_service.dart';
import '../models/decision_item.dart';
import '../models/briefing.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';
import '../widgets/chat_bubble.dart';
import '../widgets/voice_states.dart';
import '../widgets/rich_card_content.dart';
import '../utils/home_instrumentation.dart';

// ── Types ──────────────────────────────────────────────────────

enum _NowCardType { decision, task }

class _NowCard {
  final String id;
  final _NowCardType type;
  final String title;
  final String? subtitle;
  final String? description;
  final String actionLabel;
  final DecisionType? decisionType;
  final Map<String, dynamic> metadata;

  const _NowCard({
    required this.id,
    required this.type,
    required this.title,
    this.subtitle,
    this.description,
    this.actionLabel = 'Action',
    this.decisionType,
    this.metadata = const {},
  });
}

// ── AdaptiveHomeScreen ─────────────────────────────────────────

class AdaptiveHomeScreen extends StatefulWidget {
  const AdaptiveHomeScreen({super.key});

  @override
  State<AdaptiveHomeScreen> createState() => _AdaptiveHomeScreenState();
}

class _AdaptiveHomeScreenState extends State<AdaptiveHomeScreen> {
  final _api = ApiService();
  final _instrumentation = HomeInstrumentation();

  // ── Pulse intelligence ──
  BriefingResponse _briefing = BriefingResponse.empty();
  bool _briefingLoading = true;

  // ── NOW zone ──
  List<_NowCard> _nowCards = [];
  bool _nowLoading = true;
  bool _nowApiError = false;
  int _totalPendingCount = 0;

  // Mode switcher — local override of pulse's home_mode
  String? _overriddenMode;

  // Card removal animation tracking
  final Set<String> _removingCardIds = {};

  // Dedup: resolved card titles to filter from CONVERSATION
  final Set<String> _resolvedCardTitles = {};

  // Cached tasks for inline card Mark Done lookups
  List<Map<String, dynamic>> _allLoadedTasks = [];
  bool _tasksLoaded = false;

  // ── CONVERSATION ──
  final _messages = <ChatMessage>[];
  final _scrollController = ScrollController();
  final _textController = TextEditingController();
  bool _loadingHistory = true;
  bool _historyExpanded = false;
  int _historyCount = 0;
  int _msgCounter = 0;
  Timer? _pollTimer;
  final _seenMessageIds = <String>{};

  // Push-driven delivery: replaces aggressive 5s polling.
  // FCM push → onPushReceived → _pollForUpdates() → messages appear.
  // 60s backup poll only runs when a message is pending (response expected).
  static const _backupPollInterval = Duration(seconds: 60);
  bool _awaitingResponse = false;

  // ── Voice ──
  VoiceState _voiceState = VoiceState.idle;
  String? _transcribedText;
  String? _voiceError;
  final stt.SpeechToText _speech = stt.SpeechToText();
  bool _speechAvailable = false;
  final FlutterTts _tts = FlutterTts();

  @override
  void initState() {
    super.initState();
    _loadBriefing();
    _loadNowCards();
    // Conversation starts empty — no history fetch.
    // The home screen is a clean companion, not an archive browser.
    // Messages appear as you send/receive them in the current session.
    _loadingHistory = false;
    _initSpeech();
    _tts.setLanguage("en-US");
    _tts.setSpeechRate(0.5);

    // Register push notification tap handler for navigation
    NotificationService.onNotificationOpened = _handlePushNotificationTap;

    // Push-driven delivery: when a push arrives (new message, briefing, etc.),
    // fetch new messages immediately instead of polling every 5 seconds.
    // This replaces the aggressive polling that was draining the battery.
    NotificationService.onPushReceived = _handlePushReceived;

    // Cold-start: check if app was launched via notification tap
    final pendingData = NotificationService.pendingOpenData;
    if (pendingData != null) {
      NotificationService.pendingOpenData = null;
      // Use a post-frame callback so the screen is fully built before navigation
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _handlePushNotificationTap(pendingData);
      });
    }
  }

  @override
  void dispose() {
    // Unregister notification tap handler
    if (NotificationService.onNotificationOpened == _handlePushNotificationTap) {
      NotificationService.onNotificationOpened = null;
    }
    if (NotificationService.onPushReceived == _handlePushReceived) {
      NotificationService.onPushReceived = null;
    }
    _pollTimer?.cancel();
    _textController.dispose();
    _scrollController.dispose();
    _instrumentation.log();
    super.dispose();
  }

  // ── Pulse intelligence ───────────────────────────────────────────

  Future<void> _loadBriefing() async {
    try {
      final briefing = await _api.getBriefing();
      if (mounted) {
        setState(() {
          _briefing = briefing;
          _briefingLoading = false;
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() => _briefingLoading = false);
      }
    }
  }

  // ── Now zone ─────────────────────────────────────────────────

  Future<void> _loadNowCards() async {
    final decResult = await _api.getPendingDecisions();
    final taskResult = await _api.getTasks(status: 'todo');
    if (!mounted) return;

    final cards = <_NowCard>[];
    int totalVisible = 0;
    int totalDecisions = 0;
    int urgentTaskCount = 0;
    bool hadError = false;

    // Decisions first (graph nodes, edges, channel items)
    if (decResult.success) {
      totalDecisions = decResult.data!.length;
      for (final pd in decResult.data!) {
        if (totalVisible >= 3) break;
        cards.add(_NowCard(
          id: pd.id,
          type: _NowCardType.decision,
          title: pd.title,
          description: pd.description,
          actionLabel: _sourceActionLabel(pd.source),
          decisionType: _sourceToDecisionType(pd.source),
          metadata: {
            'source': pd.source,
            'api_id': pd.id,
            'pending_id': int.tryParse(pd.id),
          },
        ));
        totalVisible++;
      }
    } else {
      hadError = true;
    }

    // Due/overdue tasks fill remaining slots (count all urgent, show up to 3)
    if (taskResult.success && totalVisible < 3) {
      for (final t in taskResult.data!) {
        final deadline = t['deadline'] as String?;
        if (deadline != null && _isUrgent(deadline)) {
          urgentTaskCount++;
          if (totalVisible >= 3) continue;
          cards.add(_NowCard(
            id: t['id'].toString(),
            type: _NowCardType.task,
            title: t['title'] as String? ?? 'Untitled',
            subtitle: _formatDeadline(deadline),
            description: t['project_name'] as String?,
            actionLabel: 'Done',
            metadata: {'task_id': t['id']},
          ));
          totalVisible++;
        }
      }
    } else {
      hadError = true;
    }

    if (mounted) {
      setState(() {
        _nowCards = cards;
        _nowLoading = false;
        _nowApiError = hadError;
        // Badge: decisions + urgent tasks only (not all tasks)
        _totalPendingCount = totalDecisions + urgentTaskCount;
        _instrumentation.nowCardsShown = cards.length;
      });
    }
  }

  bool _isUrgent(String deadline) {
    try {
      final dt = DateTime.parse(deadline).toLocal();
      return dt.isBefore(DateTime.now().add(const Duration(hours: 24)));
    } catch (_) {
      return false;
    }
  }

  String _formatDeadline(String dt) {
    try {
      final parsed = DateTime.parse(dt).toLocal();
      final diff = parsed.difference(DateTime.now());
      if (diff.inDays < 0) return '${diff.inDays.abs()}d overdue';
      if (diff.inDays == 0) return 'Today';
      return '${diff.inDays}d left';
    } catch (_) {
      return '';
    }
  }

  DecisionType _sourceToDecisionType(String source) {
    switch (source) {
      case 'email':
        return DecisionType.email;
      case 'whatsapp':
        return DecisionType.whatsapp;
      case 'call':
        return DecisionType.call;
      case 'graph_node':
        return DecisionType.person;
      case 'graph_edge':
        return DecisionType.edge;
      default:
        return DecisionType.clarification;
    }
  }

  String _sourceActionLabel(String source) {
    switch (source) {
      case 'graph_node':
        return 'Approve';
      case 'graph_edge':
        return 'Review';
      case 'email':
        return 'Create';
      case 'whatsapp':
        return 'Create';
      case 'call':
        return 'Create';
      default:
        return 'View';
    }
  }

  // ── Handle NOW card actions ──────────────────────────────────

  /// Removes a card with fade-out animation, then refreshes.
  Future<void> _removeNowCardAnimated(String cardId, {bool resolved = false}) async {
    // Start fade-out
    setState(() => _removingCardIds.add(cardId));
    // Wait for animation to complete
    await Future.delayed(const Duration(milliseconds: 350));
    if (!mounted) return;
    // Actually remove
    setState(() {
      _nowCards.removeWhere((c) => c.id == cardId);
      _removingCardIds.remove(cardId);
    });
  }

  Future<void> _handleNowAction(_NowCard card) async {
    if (card.type == _NowCardType.task) {
      final taskId = card.metadata['task_id'];
      if (taskId != null) {
        await _api.updateTaskStatus(taskId as int, 'done');
        _instrumentation.homeActionsCompleted++;      _addResolvedTitle(card.title);
          await _removeNowCardAnimated(card.id, resolved: true);
        _loadNowCards(); // refresh counts
      }
      return;
    }

    // ── Inline decision action ──
    final source = card.metadata['source'] as String?;
    final pendingId = card.metadata['pending_id'] as int?;
    if (source == null || pendingId == null) return;

    ApiResult<dynamic>? result;
    switch (source) {
      case 'graph_node':
        result = await _api.approveGraphNode(pendingId);
        break;
      case 'graph_edge':
        result = await _api.approveGraphEdge(pendingId);
        break;
      case 'email':
        result = await _api.approveEmail(pendingId);
        break;
      case 'whatsapp':
        result = await _api.approveWhatsApp(pendingId);
        break;
      case 'call':
        result = await _api.approveCall(pendingId);
        break;
    }

    if (!mounted) return;

    if (result != null && result.success) {
      _instrumentation.homeActionsCompleted++;
      _addResolvedTitle(card.title);
      await _removeNowCardAnimated(card.id, resolved: true);
      _loadNowCards(); // refresh counts
    } else if (result != null) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              result.error ?? 'Action failed',
              style: const TextStyle(fontSize: 12),
            ),
            backgroundColor: AppTheme.red,
            duration: const Duration(seconds: 2),
          ),
        );
      }
    }
  }

  Future<void> _dismissNowCard(_NowCard card) async {
    _instrumentation.itemsDismissed++;
    await _removeNowCardAnimated(card.id);
  }

  /// Track a resolved card title for dedup, capped at 50 entries.
  void _addResolvedTitle(String title) {
    _resolvedCardTitles.add(title.toLowerCase());
    if (_resolvedCardTitles.length > 50) {
      _resolvedCardTitles.clear();
    }
  }

  // ── Conversation ─────────────────────────────────────────────

  Future<void> _loadHistory() async {
    final result = await _api.getMessages(limit: 30);
    if (!mounted) return;

    if (result.success && result.data!.isNotEmpty) {
      for (final m in result.data!) {
        final content = m['content'] as String? ?? '';
        if (content.isEmpty) continue;
        final direction = m['direction'] as String? ?? '';
        final role =
            direction == 'inbound' ? MessageRole.user : MessageRole.rhodey;
        final createdAt = m['created_at'] as String? ?? '';
        final ts = createdAt.isNotEmpty
            ? (DateTime.tryParse(createdAt) ?? DateTime.now())
            : DateTime.now();
        _messages.add(ChatMessage(
          id: 'h${_msgCounter++}',
          role: role,
          text: content,
          timestamp: ts,
          sendStatus: role == MessageRole.user ? SendStatus.sent : null,
        ));
      }
    }
    _historyCount = _messages.length;
    if (mounted) {
      setState(() => _loadingHistory = false);
    }
    // Pre-load tasks for inline card Mark Done lookups
    if (!_tasksLoaded) {
      _loadTasksForCards();
    }
  }

  /// Load tasks for inline card lookups (Mark Done button).
  Future<void> _loadTasksForCards() async {
    final result = await _api.getTasks();
    if (result.success && mounted) {
      setState(() {
        _allLoadedTasks = result.data!;
        _tasksLoaded = true;
      });
    }
  }

  /// Find a task by title (case-insensitive) for inline Mark Done.
  int? _findTaskIdByTitle(String title) {
    final lowerTitle = title.toLowerCase().trim();
    for (final t in _allLoadedTasks) {
      final taskTitle = (t['title'] as String? ?? '').toLowerCase().trim();
      if (taskTitle == lowerTitle || taskTitle.contains(lowerTitle) || lowerTitle.contains(taskTitle)) {
        return t['id'] as int?;
      }
    }
    return null;
  }

  /// Called when an FCM push arrives (new message, briefing, decision).
  /// Triggers a message fetch immediately — no need to wait for the next poll cycle.
  void _handlePushReceived(Map<String, dynamic> data) {
    debugPrint('[PushDelivery] Push received: type=${data['type']}');
    final type = data['type'];
    // Refresh pulse intelligence on briefing pushes
    if (type == 'briefing' || type == 'briefing_refresh') {
      _loadBriefing();
    }
    _pollForUpdates();
  }

  /// Starts a backup poll timer at a low frequency (60s).
  /// Only active when a response is expected — stops automatically once Rhodey replies.
  /// Primary delivery is via FCM push -> _handlePushReceived.
  void _startBackupPoll() {
    _awaitingResponse = true;
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(_backupPollInterval, (_) {
      if (!_awaitingResponse) {
        _stopBackupPoll();
        return;
      }
      _pollForUpdates();
    });
  }

  void _stopBackupPoll() {
    _awaitingResponse = false;
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  Future<void> _pollForUpdates() async {
    final result = await _api.getMessages(limit: 5);
    if (!mounted || !result.success) return;
    for (final m in result.data!) {
      final id = m['id'].toString();
      if (_seenMessageIds.contains(id)) continue;
      _seenMessageIds.add(id);
      final content = m['content'] as String? ?? '';
      if (content.isEmpty) continue;

      // Dedup: skip messages whose content matches a recently-resolved NOW card
      final contentLower = content.toLowerCase();
      bool isDuplicate = false;
      for (final resolvedTitle in _resolvedCardTitles) {
        if (contentLower.contains(resolvedTitle)) {
          isDuplicate = true;
          _instrumentation.dedupSuppressions++;
          debugPrint('[Dedup] Suppressed CONVERSATION message matching "$resolvedTitle"');
          break;
        }
      }
      if (isDuplicate) continue;

      final direction = m['direction'] as String? ?? '';
      final role =
          direction == 'inbound' ? MessageRole.user : MessageRole.rhodey;
      final createdAt = m['created_at'] as String? ?? '';
      final ts = createdAt.isNotEmpty
          ? (DateTime.tryParse(createdAt) ?? DateTime.now())
          : DateTime.now();
      final msg = ChatMessage(
        id: 'p${_msgCounter++}',
        role: role,
        text: content,
        timestamp: ts,
        sendStatus: role == MessageRole.user ? SendStatus.sent : null,
      );
      setState(() {
        _messages.add(msg);
      });
      _scrollToBottom();
    }
  }

  void _sendMessage(String text) {
    if (text.trim().isEmpty) return;
    final id = 'u${++_msgCounter}';
    final msg = ChatMessage(
      id: id,
      role: MessageRole.user,
      text: text.trim(),
      timestamp: DateTime.now(),
      sendStatus: SendStatus.pending,
    );

    setState(() {
      _messages.add(msg);
    });
    _textController.clear();
    _scrollToBottom();
    // Start backup polling (60s) — primary delivery is via FCM push.
    // Push arrives -> _handlePushReceived -> _pollForUpdates() -> messages appear.
    _startBackupPoll();

    _api.sendMessage(text.trim()).then((result) {
      if (!mounted) return;
      if (result.success) {
        _updateMessage(id, sendStatus: SendStatus.sent);
        String responseText;
        if (result.data is Map) {
          final serverResponse =
              (result.data as Map)['response'] as String?;
          responseText = serverResponse ?? 'Got it. Processing...';
        } else {
          responseText = 'Got it. Processing...';
        }

        // Response is already in the API body — Rhodey's reply appears instantly.
        Future.delayed(const Duration(milliseconds: 400), () {
          if (!mounted) return;
          _addRhodeyResponse(responseText);
          _updateMessage(id, sendStatus: SendStatus.resolved);
        });
      } else {
        _updateMessage(id, sendStatus: SendStatus.failed);
        // Show a visible toast for failed sends (S3#9)
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: const Text('Failed to send message. Check your connection.',
                  style: TextStyle(fontSize: 12)),
              backgroundColor: AppTheme.red,
              duration: const Duration(seconds: 3),
            ),
          );
        }
      }
    });
  }

  void _updateMessage(String id,
      {String? text, SendStatus? sendStatus}) {
    setState(() {
      final idx = _messages.indexWhere((m) => m.id == id);
      if (idx == -1) return;
      _messages[idx] = ChatMessage(
        id: _messages[idx].id,
        role: _messages[idx].role,
        type: _messages[idx].type,
        text: text ?? _messages[idx].text,
        timestamp: _messages[idx].timestamp,
        quickReplies: _messages[idx].quickReplies,
        sendStatus: sendStatus ?? _messages[idx].sendStatus,
      );
    });
  }

  void _addRhodeyResponse(String text) {
    _stopBackupPoll();
    final id = 'r${++_msgCounter}';
    setState(() {
      _messages.add(ChatMessage(
        id: id,
        role: MessageRole.rhodey,
        text: text,
        timestamp: DateTime.now(),
      ));
    });
    // Load tasks for inline card lookups if we haven't yet
    if (!_tasksLoaded) {
      _loadTasksForCards();
    }
    _scrollToBottom();
    _tts.speak(text);
  }

  // ── Voice ────────────────────────────────────────────────────

  Future<void> _initSpeech() async {
    _speechAvailable = await _speech.initialize(
      onError: (error) {
        if (!mounted) return;
        debugPrint('[Voice] Error: ${error.errorMsg}');
        if (error.errorMsg == 'error_speech_timeout') {
          setState(() {
            _voiceState = VoiceState.idle;
            _voiceError = null;
          });
          return;
        }
        setState(() {
          _voiceState = VoiceState.error;
          _voiceError = error.errorMsg;
        });
      },
      onStatus: (status) {
        debugPrint('[Voice] Status: $status');
      },
    );
    if (!_speechAvailable && mounted) {
      setState(() {
        _voiceState = VoiceState.error;
        _voiceError = 'Speech recognition not available';
      });
    }
  }

  void _toggleVoice() {
    if (_voiceState == VoiceState.idle) {
      _startListening();
    } else if (_voiceState == VoiceState.listening) {
      _stopListening();
    } else {
      setState(() {
        _voiceState = VoiceState.idle;
        _transcribedText = null;
        _voiceError = null;
      });
    }
  }

  Future<void> _startListening() async {
    _voiceError = null;
    if (!_speechAvailable) {
      _speechAvailable = await _speech.initialize();
      if (!_speechAvailable) {
        if (mounted) {
          setState(() {
            _voiceState = VoiceState.error;
            _voiceError = 'Speech recognition not available';
          });
        }
        return;
      }
    }

    setState(() => _voiceState = VoiceState.listening);

    await _speech.listen(
      onResult: (result) {
        if (!mounted) return;
        final text = result.recognizedWords;
        if (text.isNotEmpty) {
          _transcribedText = text;
        }
        if (result.finalResult) {
          final words = result.recognizedWords;
          if (words.isNotEmpty) {
            _sendMessage(words);
          }
          setState(() => _voiceState = VoiceState.idle);
          _transcribedText = null;
        }
      },
      listenOptions: stt.SpeechListenOptions(
        listenFor: const Duration(seconds: 30),
        pauseFor: const Duration(seconds: 5),
        partialResults: true,
        cancelOnError: false,
      ),
    );
  }

  Future<void> _stopListening() async {
    await _speech.stop();
    if (!mounted) return;
    if (_transcribedText != null && _transcribedText!.isNotEmpty) {
      _sendMessage(_transcribedText!);
      setState(() => _voiceState = VoiceState.idle);
      _transcribedText = null;
      return;
    }
    setState(() {
      _voiceState = VoiceState.idle;
      _transcribedText = null;
    });
  }

  void _retryVoice() {
    setState(() {
      _voiceState = VoiceState.idle;
      _transcribedText = null;
    });
    Future.delayed(const Duration(milliseconds: 300), () {
      if (!mounted) return;
      _startListening();
    });
  }

  // ── Navigation ───────────────────────────────────────────────

  void _showMenu() {
    _instrumentation.menuOpens++;
    showMenuSheet(context);
  }

  void _openInbox() {
    _instrumentation.inboxBadgeTaps++;
    Navigator.push(
      context,
      MaterialPageRoute(builder: (_) => const InboxScreen()),
    );
  }

  /// Handle push notification tap — navigate to the appropriate screen.
  void _handlePushNotificationTap(Map<String, dynamic> data) {
    final type = data['type'];
    debugPrint('[PushNav] Notification type=$type data=$data');

    switch (type) {
      case 'decision':
      case 'delegation':
        _openInbox();
        break;
      case 'nudge':
        Navigator.push(
          context,
          MaterialPageRoute(builder: (_) => const TodayScreen()),
        );
        break;
      case 'briefing':
      default:
        // Stay on the home screen — briefing context is already in CONVERSATION
        break;
    }
  }

  // ── Scroll ───────────────────────────────────────────────────

  void _scrollToBottom() {
    Future.delayed(const Duration(milliseconds: 100), () {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 300),
          curve: Curves.easeOut,
        );
      }
    });
  }

  // ── TTS on tap ──────────────────────────────────────────────

  /// Speak a message text aloud via TTS. Stops any in-progress speech first.
  void _speakMessage(String text) {
    if (text.trim().isEmpty) return;
    _tts.stop();
    _tts.speak(text.trim());
  }

  // ── Mode helpers ────────────────────────────────────────────

  String _modeIcon(String mode) {
    switch (mode) {
      case 'proceed': return '▶';
      case 'decide': return '📋';
      case 'sprint': return '⏱';
      case 'catch_up': return '🔄';
      case 'wrap': return '✅';
      default: return '•';
    }
  }

  void _openModeSwitcher() {
    final modes = ['proceed', 'decide', 'sprint', 'catch_up', 'wrap'];
    final labels = ['Act', 'Decide', 'Sprint', 'Catch Up', 'Wrap'];
    final current = _overriddenMode ?? _briefing.homeMode;

    showModalBottomSheet(
      context: context,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (ctx) {
        return SafeArea(
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text(
                  'Not what you need?',
                  style: TextStyle(
                    color: AppTheme.textTertiary,
                    fontSize: 11,
                    letterSpacing: 0.5,
                  ),
                ),
                const SizedBox(height: 12),
                ...modes.asMap().entries.map((entry) {
                  final i = entry.key;
                  final mode = entry.value;
                  final isCurrent = mode == current;
                  return ListTile(
                    dense: true,
                    leading: Text(_modeIcon(mode), style: const TextStyle(fontSize: 16)),
                    title: Text(
                      labels[i],
                      style: TextStyle(
                        color: isCurrent ? AppTheme.accent : AppTheme.textPrimary,
                        fontWeight: isCurrent ? FontWeight.w600 : FontWeight.w400,
                        fontSize: 14,
                      ),
                    ),
                    trailing: isCurrent
                        ? const Icon(Icons.check, size: 16, color: AppTheme.accent)
                        : null,
                    onTap: () {
                      Navigator.pop(ctx);
                      _switchMode(mode);
                    },
                  );
                }),
              ],
            ),
          ),
        );
      },
    );
  }

  void _switchMode(String newMode) {
    // Only switch if different from current effective mode
    final current = _overriddenMode ?? _briefing.homeMode;
    if (newMode == current) return;
    
    final previousMode = current;
    setState(() {
      _overriddenMode = newMode;
    });
  
    // Fire-and-forget correction signal to train Rhodey
    // This logs to subsystem_telemetry + classifier_corrections
    _api.switchHomeMode(previousMode, newMode);
  }

  // ── Time formatting ─────────────────────────────────────────

  String _formattedTime() {
    final now = DateTime.now();
    final h = now.hour > 12 ? now.hour - 12 : (now.hour == 0 ? 12 : now.hour);
    final m = now.minute.toString().padLeft(2, '0');
    final ampm = now.hour >= 12 ? 'PM' : 'AM';
    return '$h:$m $ampm';
  }

  String _formattedDate() {
    final now = DateTime.now();
    final months = [
      'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'
    ];
    final days = [
      'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'
    ];
    return '${days[now.weekday % 7]}, ${months[now.month - 1]} ${now.day}';
  }

  // ── Build ────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.background,
      body: SafeArea(
        child: Column(
          children: [
            _buildHeader(),
            _buildNowZone(),
            _buildDivider('CONVERSATION'),
            Expanded(child: _buildConversationArea()),
            if (_voiceState == VoiceState.listening ||
                _voiceState == VoiceState.error)
              VoiceStateMachine(
                state: _voiceState,
                transcribedText: _transcribedText,
                errorMessage: _voiceError,
                onCancel: _toggleVoice,
                onRetry: _retryVoice,
              ),
            _buildInputBar(),
          ],
        ),
      ),
    );
  }

  // ── Header ──────────────────────────────────────────────────

  Widget _buildHeader() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: const BoxDecoration(
        border: Border(
          bottom: BorderSide(color: AppTheme.border, width: 1),
        ),
      ),
      child: Row(
        children: [
          // Menu button
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(10),
              onTap: _showMenu,
              child: Container(
                padding: const EdgeInsets.all(8),
                child: const Icon(
                  Icons.menu,
                  color: AppTheme.textSecondary,
                  size: 22,
                ),
              ),
            ),
          ),

          const SizedBox(width: 8),

          // Date + time
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                _formattedDate(),
                style: AppTheme.caption.copyWith(
                  color: AppTheme.textTertiary,
                  fontSize: 10,
                  letterSpacing: 0.3,
                ),
              ),
              Text(
                _formattedTime(),
                style: AppTheme.body.copyWith(
                  fontWeight: FontWeight.w600,
                  fontSize: 13,
                ),
              ),
            ],
          ),

          const Spacer(),

          // Inbox badge
          if (_totalPendingCount > 0)
            Material(
              color: Colors.transparent,
              child: InkWell(
                borderRadius: BorderRadius.circular(10),
                onTap: _openInbox,
                child: Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                  decoration: BoxDecoration(
                    color: AppTheme.accentBg,
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(
                      color: AppTheme.accent.withValues(alpha: 0.3),
                      width: 1,
                    ),
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(
                        Icons.inbox_outlined,
                        size: 14,
                        color: AppTheme.accent,
                      ),
                      const SizedBox(width: 4),
                      Text(
                        '$_totalPendingCount',
                        style: AppTheme.caption.copyWith(
                          color: AppTheme.accent,
                          fontWeight: FontWeight.w700,
                          fontSize: 12,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
          ),
        ],
      ),
    );
  }

  // ── NOW zone ─────────────────────────────────────────────────

  Widget _buildNowZone() {
    final greeting = _briefing.greeting;
    final voice = _briefing.voiceLine ?? '';
    final hasVoice = voice.isNotEmpty;
    final homeMode = _overriddenMode ?? _briefing.homeMode;
    final vaultTotal = _briefing.vaultedCount;

    return Container(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 6),
      decoration: const BoxDecoration(
        border: Border(
          bottom: BorderSide(color: AppTheme.border, width: 1),
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          // ── Greeting (serif, large) ──
          if (!_briefingLoading && greeting.isNotEmpty)
            _buildGreeting(greeting),

          // ── Rhodey voice line ──
          if (!_briefingLoading && hasVoice)
            _buildVoiceLine(),

          const SizedBox(height: 6),

          // ── Mode-driven section ──
          if (!_briefingLoading && !_nowLoading)
            _buildModeSection(homeMode)
          else
            _buildEmptyNow(),

          // ── Vault stat line ──
          if (!_briefingLoading && vaultTotal > 0)
            _buildVaultStat(),

          // ── Overflow link ──
          if (!_nowLoading && _totalPendingCount > _nowCards.length)
            _buildOverflowLink(),
        ],
      ),
    );
  }

  // ── Greeting ──────────────────────────────────────────────────

  Widget _buildGreeting(String greeting) {
    // Split greeting into headline + subtext at the period
    final dotIndex = greeting.indexOf('.');
    String headline = greeting;
    String subtext = '';
    if (dotIndex > 0 && dotIndex < greeting.length - 1) {
      headline = greeting.substring(0, dotIndex + 1);
      subtext = greeting.substring(dotIndex + 1).trim();
      if (subtext.startsWith('.')) subtext = subtext.substring(1).trim();
    }

    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            headline,
            style: const TextStyle(
              fontFamily: 'InstrumentSerif',
              fontSize: 30,
              fontWeight: FontWeight.w300,
              color: AppTheme.textPrimary,
              height: 1.1,
            ),
          ),
          if (subtext.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Text(
                subtext,
                style: AppTheme.subGreetingStyle,
              ),
            ),
        ],
      ),
    );
  }

  // ── Mode section (switches on homeMode) ─────────────────────────

  Widget _buildModeSection(String mode) {
    switch (mode) {
      case 'proceed':
        return _buildProceedSection();
      case 'decide':
        return _buildDecideSection();
      case 'sprint':
        return _buildSprintSection();
      case 'catch_up':
        return _buildCatchUpSection();
      case 'wrap':
        return _buildWrapSection();
      default:
        return _buildProceedSection();
    }
  }

  // ── PROCEED: priority task cards ─────────────────────────────

  Widget _buildProceedSection() {
    // Show top 2-3 priority items from _nowCards
    final actCards = _nowCards.take(3).toList();

    if (actCards.isEmpty) {
      return _buildEmptyNow();
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        _buildModeHeader('Act'),
        const SizedBox(height: 6),
        ...actCards.map((card) => Padding(
          padding: const EdgeInsets.only(bottom: 4),
          child: _NowCardWidget(
            card: card,
            onAction: () => _handleNowAction(card),
            onDismiss: () => _dismissNowCard(card),
          ),
        )),
      ],
    );
  }

  // ── DECIDE: decision cards ───────────────────────────────────

  Widget _buildDecideSection() {
    // Show only decision-type cards
    final decisionCards = _nowCards
        .where((c) => c.type == _NowCardType.decision)
        .take(3)
        .toList();

    if (decisionCards.isEmpty) {
      return _buildEmptyNow();
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        _buildModeHeader('Decide'),
        const SizedBox(height: 6),
        ...decisionCards.map((card) => Padding(
          padding: const EdgeInsets.only(bottom: 4),
          child: _NowCardWidget(
            card: card,
            onAction: () => _handleNowAction(card),
            onDismiss: () => _dismissNowCard(card),
          ),
        )),
      ],
    );
  }

  // ── SPRINT: focus tasks + radar ──────────────────────────────

  Widget _buildSprintSection() {
    // Show urgent tasks first, then high priority
    final urgentCards = _nowCards
        .where((c) => c.type == _NowCardType.task)
        .take(3)
        .toList();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        _buildModeHeader('Sprint'),
        const SizedBox(height: 6),
        if (urgentCards.isEmpty)
          _buildEmptyNow()
        else
          ...urgentCards.map((card) => Padding(
            padding: const EdgeInsets.only(bottom: 4),
            child: _NowCardWidget(
              card: card,
              onAction: () => _handleNowAction(card),
              onDismiss: () => _dismissNowCard(card),
            ),
          )),
        // Radar: next calendar event
        if (_briefing.nextEvent != null && _briefing.nextEvent!.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Row(
              children: [
                const Icon(Icons.calendar_today, size: 10, color: AppTheme.textTertiary),
                const SizedBox(width: 4),
                Text(
                  '📅 ${_briefing.nextEvent}',
                  style: AppTheme.caption.copyWith(
                    color: AppTheme.textTertiary,
                    fontSize: 10,
                  ),
                ),
              ],
            ),
          ),
      ],
    );
  }

  // ── CATCH UP: delta items ────────────────────────────────────

  Widget _buildCatchUpSection() {
    final deltas = _briefing.deltaItems;

    if (deltas.isEmpty) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          _buildModeHeader('Since You Were Away'),
          const SizedBox(height: 6),
          _buildEmptyNow('Everything is current — no changes to report.'),
        ],
      );
    }

    // Show up to 10 delta items, newest first (they're already ordered by the API)
    final items = deltas.take(10).toList();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        _buildModeHeader('Since You Were Away'),
        const SizedBox(height: 6),
        ...items.map((item) => _buildDeltaRow(item)),
      ],
    );
  }

  Widget _buildDeltaRow(DeltaItem item) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 20,
            child: Text(
              item.icon,
              style: const TextStyle(fontSize: 12),
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  item.text,
                  style: AppTheme.body.copyWith(
                    fontSize: 12,
                    fontWeight: FontWeight.w400,
                  ),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                if (item.time.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 1),
                    child: Text(
                      item.time,
                      style: AppTheme.caption.copyWith(
                        color: AppTheme.textTertiary,
                        fontSize: 9,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  // ── WRAP: done today + rolling ───────────────────────────────

  Widget _buildWrapSection() {
    final doneToday = _briefing.wrapDoneToday;
    final rolling = _briefing.wrapRolling;

    final hasDone = doneToday.isNotEmpty;
    final hasRolling = rolling.isNotEmpty;

    if (!hasDone && !hasRolling) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          _buildModeHeader('Done Today'),
          const SizedBox(height: 6),
          _buildEmptyNow('Clear board — nothing closed yet today.'),
        ],
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        // Always show mode header when there's any content
        _buildModeHeader('Done Today'),
        const SizedBox(height: 6),

        // Done Today section
        if (hasDone)
          ...doneToday.take(8).map((item) => _buildWrapRow(item)),

        if (hasDone && hasRolling) const SizedBox(height: 10),

        // Rolling to Tomorrow section
        if (hasRolling) _buildRollingHeader(),
        if (hasRolling)
          ...rolling.take(5).map((item) => _buildWrapRow(item)),
      ],
    );
  }

  Widget _buildRollingHeader() {
    return Row(
      children: [
        Text(
          'ROLLING TO TOMORROW',
          style: AppTheme.label.copyWith(
            color: AppTheme.amber,
            fontSize: 10,
            letterSpacing: 1.2,
          ),
        ),
        const SizedBox(width: 4),
        const Icon(
          Icons.unfold_more,
          size: 12,
          color: AppTheme.amber,
        ),
      ],
    );
  }

  Widget _buildWrapRow(WrapItem item) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 20,
            child: Text(
              item.icon,
              style: const TextStyle(fontSize: 12),
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  item.text,
                  style: AppTheme.body.copyWith(
                    fontSize: 12,
                    fontWeight: FontWeight.w400,
                  ),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                if (item.detail.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 1),
                    child: Text(
                      item.detail,
                      style: AppTheme.caption.copyWith(
                        color: AppTheme.textTertiary,
                        fontSize: 9,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  // ── Mode header with switcher ────────────────────────────────

  Widget _buildModeHeader(String label) {
    return GestureDetector(
      onTap: _openModeSwitcher,
      child: Row(
        children: [
          Text(
            label.toUpperCase(),
            style: AppTheme.label.copyWith(
              color: AppTheme.accent,
              fontSize: 10,
              letterSpacing: 1.2,
            ),
          ),
          const SizedBox(width: 4),
          Icon(
            Icons.unfold_more,
            size: 12,
            color: AppTheme.accent.withValues(alpha: 0.5),
          ),
          if (_nowLoading)
            const Padding(
              padding: EdgeInsets.only(left: 6),
              child: SizedBox(
                width: 10,
                height: 10,
                child: CircularProgressIndicator(
                  strokeWidth: 1.5,
                  color: AppTheme.textTertiary,
                ),
              ),
            ),
          if (!_nowLoading && _nowApiError)
            const Padding(
              padding: EdgeInsets.only(left: 4),
              child: Icon(
                Icons.warning_amber_rounded,
                size: 12,
                color: AppTheme.amber,
              ),
            ),
        ],
      ),
    );
  }

  // ── Vault stat line ──────────────────────────────────────────

  Widget _buildVaultStat() {
    final total = _briefing.vaultedCount;
    final urgent = _briefing.vaultedUrgentCount;
    final high = _briefing.vaultedHighCount;

    if (total <= 0) return const SizedBox.shrink();

    // Build segmented parts
    final parts = <Widget>[];
    parts.add(const Text('📦', style: TextStyle(fontSize: 10)));
    parts.add(const SizedBox(width: 4));
    parts.add(Text(
      '$total vaulted',
      style: AppTheme.caption.copyWith(
        color: AppTheme.textTertiary,
        fontSize: 10,
      ),
    ));

    if (urgent > 0) {
      parts.add(const SizedBox(width: 4));
      parts.add(Container(
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
        decoration: BoxDecoration(
          color: AppTheme.red.withValues(alpha: 0.1),
          borderRadius: BorderRadius.circular(3),
        ),
        child: Text(
          '🔴 $urgent urgent',
          style: AppTheme.caption.copyWith(
            color: AppTheme.red,
            fontSize: 9,
            fontWeight: FontWeight.w600,
          ),
        ),
      ));
    }
    if (high > 0) {
      parts.add(const SizedBox(width: 4));
      parts.add(Container(
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
        decoration: BoxDecoration(
          color: AppTheme.amber.withValues(alpha: 0.1),
          borderRadius: BorderRadius.circular(3),
        ),
        child: Text(
          '🟡 $high high',
          style: AppTheme.caption.copyWith(
            color: AppTheme.amber,
            fontSize: 9,
            fontWeight: FontWeight.w600,
          ),
        ),
      ));
    }

    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: GestureDetector(
        onTap: () => Navigator.push(
          context,
          MaterialPageRoute(builder: (_) => const TodayScreen()),
        ),
        child: Row(
          children: parts,
        ),
      ),
    );
  }

  // ── Empty now ─────────────────────────────────────────────────

  Widget _buildEmptyNow([String message = 'Nothing needs your attention right now.']) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(
        children: [
          Container(
            width: 6,
            height: 6,
            decoration: const BoxDecoration(
              color: AppTheme.green,
              shape: BoxShape.circle,
            ),
          ),
          const SizedBox(width: 8),
          Flexible(
            child: Text(
              message,
              style: AppTheme.bodySmall.copyWith(
                color: AppTheme.textTertiary,
                fontSize: 12,
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ── NOW cards (used by mode sections) ─────────────────────────

  Widget _buildOverflowLink() {
    return Padding(
      padding: const EdgeInsets.only(top: 4, bottom: 2),
      child: GestureDetector(
        onTap: _openInbox,
        child: Row(
          children: [
            const Icon(Icons.arrow_forward_ios,
                size: 8, color: AppTheme.accent),
            const SizedBox(width: 4),
            Text(
              '+${_totalPendingCount - _nowCards.length} more in Inbox',
              style: AppTheme.caption.copyWith(
                color: AppTheme.accent,
                fontSize: 11,
              ),
            ),
          ],
        ),
      ),
    );
  }

  // ── Voice line ───────────────────────────────────────────────

  Widget _buildVoiceLine() {
    final voice = _briefing.voiceLine ?? '';
    if (voice.isEmpty) return const SizedBox.shrink();

    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          color: AppTheme.accent.withValues(alpha: 0.04),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(
            color: AppTheme.accent.withValues(alpha: 0.12),
            width: 1,
          ),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Decorative quote mark
            Padding(
              padding: const EdgeInsets.only(top: 1, right: 8),
              child: Text(
                '\u201c',
                style: TextStyle(
                  fontSize: 20,
                  fontWeight: FontWeight.w300,
                  color: AppTheme.accent.withValues(alpha: 0.5),
                  height: 1.0,
                ),
              ),
            ),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    voice,
                    style: TextStyle(
                      fontSize: 13,
                      fontWeight: FontWeight.w400,
                      color: AppTheme.textPrimary,
                      height: 1.4,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    'Rhodey',
                    style: AppTheme.caption.copyWith(
                      color: AppTheme.accent,
                      fontSize: 9,
                      letterSpacing: 0.5,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  // ── Conversation area ────────────────────────────────────────

  Widget _buildConversationArea() {
    if (_loadingHistory) {
      return const Center(child: CircularProgressIndicator());
    }

    final displayMessages = _historyExpanded
        ? _messages
        : _messages.skip(_historyCount).toList();

    return RefreshIndicator(
      onRefresh: _loadHistory,
      color: AppTheme.accent,
      child: ListView(
        controller: _scrollController,
        padding: const EdgeInsets.only(top: 4, bottom: 8),
        children: [
          // History pill (collapsible banner)
          if (!_historyExpanded && _historyCount > 0)
            _buildHistoryPill(),

          // Messages — rich cards for Rhodey responses, ChatBubble for everything else
          if (displayMessages.isEmpty && !_loadingHistory)
            _buildEmptyConversation()
          else
            ...displayMessages.asMap().entries.map((entry) {
              final index = entry.key;
              final msg = entry.value;

              // Check if this is a Rhodey message that should render as a rich card
              if (msg.role == MessageRole.rhodey && !msg.isRhodeyTyping) {
                final cardData = parseMessageToCardData(msg.text);
                if (cardData != null) {
                  return _buildRichCard(msg, cardData);
                }
              }

              // Fall back to normal ChatBubble — provide onTap for TTS on Rhodey messages
              final isGroupStart = index == 0 ||
                  displayMessages[index - 1].role != msg.role;
              return ChatBubble(
                message: msg,
                isGroupStart: isGroupStart,
                onRetry: msg.isFailed
                    ? () => _retryMessage(msg.id)
                    : null,
                onTap: !msg.isUser && msg.text.isNotEmpty
                    ? () => _speakMessage(msg.text)
                    : null,
              );
            }),

          // Spacer so last message doesn't hide behind input bar
          const SizedBox(height: 16),
        ],
      ),
    );
  }

  Widget _buildEmptyConversation() {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 32),
      child: Column(
        children: [
          const Icon(Icons.chat_bubble_outline,
              size: 28, color: AppTheme.textMuted),
          const SizedBox(height: 12),
          Text(
            'Capture a thought, task, or idea',
            style: AppTheme.body.copyWith(
              color: AppTheme.textTertiary,
              fontSize: 14,
            ),
            textAlign: TextAlign.center,
          ),
        ],
      ),
    );
  }

  /// Build a rich card for a Rhodey message that matches a card pattern.
  Widget _buildRichCard(ChatMessage msg, CardData cardData) {
    VoidCallback? onMarkDone;
    VoidCallback? onUndo;

    if (cardData.type == CardType.task) {
      onMarkDone = () async {
        final taskId = _findTaskIdByTitle(cardData.title);
        if (taskId != null) {
          await _api.updateTaskStatus(taskId, 'done');
          _instrumentation.homeActionsCompleted++;
          _addResolvedTitle(cardData.title);
        } else {
          // Refresh tasks and try again
          await _loadTasksForCards();
          final retryId = _findTaskIdByTitle(cardData.title);
          if (retryId != null) {
            await _api.updateTaskStatus(retryId, 'done');
            _instrumentation.homeActionsCompleted++;
            _addResolvedTitle(cardData.title);
          } else if (mounted) {
            ScaffoldMessenger.of(context).showSnackBar(
              SnackBar(
                content: const Text('Could not find task. Open Inbox to complete.',
                    style: TextStyle(fontSize: 12)),
                backgroundColor: AppTheme.amber,
                duration: const Duration(seconds: 2),
              ),
            );
          }
        }
      };
    }

    if (cardData.type == CardType.approval) {
      onUndo = () {
        _openInbox();
      };
    }

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          // Rhodey label (matches ChatBubble's group-start style)
          Padding(
            padding: const EdgeInsets.only(left: 16, bottom: 4),
            child: Text('Rhodey', style: AppTheme.caption.copyWith(
              color: AppTheme.accent,
              letterSpacing: 0.3,
            )),
          ),
          RichCardContent(
            cardData: cardData,
            onMarkDone: onMarkDone,
            onUndo: onUndo,
            onTap: () => _speakMessage(msg.text),
          ),
        ],
      ),
    );
  }

  Widget _buildHistoryPill() {
    return GestureDetector(
      onTap: () {
        setState(() => _historyExpanded = true);
      },
      child: Container(
        margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
        decoration: BoxDecoration(
          color: AppTheme.surfaceAlt,
          borderRadius: BorderRadius.circular(20),
          border: Border.all(color: AppTheme.border, width: 1),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.history,
                size: 14, color: AppTheme.textTertiary),
            const SizedBox(width: 6),
            Text(
              'Show $_historyCount earlier messages ▴',
              style: AppTheme.caption.copyWith(
                color: AppTheme.textTertiary,
                fontSize: 12,
              ),
            ),
          ],
        ),
      ),
    );
  }

  void _retryMessage(String id) {
    _startBackupPoll();
    final idx = _messages.indexWhere((m) => m.id == id);
    if (idx == -1) return;
    final text = _messages[idx].text;

    _api.sendMessage(text).then((result) {
      if (!mounted) return;
      if (result.success) {
        String responseText;
        if (result.data is Map) {
          final serverResponse =
              (result.data as Map)['response'] as String?;
          responseText = serverResponse ?? 'Got it. Processing...';
        } else {
          responseText = 'Got it. Processing...';
        }
        Future.delayed(const Duration(milliseconds: 400), () {
          if (!mounted) return;
          _addRhodeyResponse(responseText);
          _updateMessage(id, sendStatus: SendStatus.resolved);
        });
      } else {
        _updateMessage(id, sendStatus: SendStatus.failed);
      }
    });
  }

  // ── Input bar ────────────────────────────────────────────────

  Widget _buildInputBar() {
    return Container(
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 12),
      decoration: const BoxDecoration(
        border: Border(
          top: BorderSide(color: AppTheme.border, width: 1),
        ),
      ),
      child: SafeArea(
        top: false,
        child: Row(
          children: [
            Expanded(
              child: Container(
                decoration: BoxDecoration(
                  color: AppTheme.surfaceAlt,
                  borderRadius: BorderRadius.circular(14),
                  border: Border.all(
                    color: _voiceState == VoiceState.listening
                        ? AppTheme.accent
                        : AppTheme.border,
                    width: 1,
                  ),
                ),
                child: TextField(
                  controller: _textController,
                  textInputAction: TextInputAction.send,
                  onSubmitted: _sendMessage,
                  decoration: const InputDecoration(
                    hintText: 'Type a message — or tap the mic',
                    border: InputBorder.none,
                    contentPadding: EdgeInsets.symmetric(
                        horizontal: 16, vertical: 12),
                    isDense: true,
                  ),
                  style: AppTheme.body,
                ),
              ),
            ),
            const SizedBox(width: 8),
            Material(
              color: _voiceState == VoiceState.listening
                  ? AppTheme.red
                  : AppTheme.accent,
              borderRadius: BorderRadius.circular(18),
              child: InkWell(
                borderRadius: BorderRadius.circular(18),
                onTap: _toggleVoice,
                child: Container(
                  width: 64,
                  height: 64,
                  alignment: Alignment.center,
                  child: Icon(
                    _voiceState == VoiceState.listening
                        ? Icons.stop
                        : Icons.mic,
                    color: Colors.white,
                    size: 28,
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  // ── Divider ──────────────────────────────────────────────────

  Widget _buildDivider(String label) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: Row(
        children: [
          Text(
            label,
            style: AppTheme.label.copyWith(
              color: AppTheme.textMuted,
              fontSize: 9,
              letterSpacing: 1.2,
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Container(
              height: 1,
              color: AppTheme.border.withValues(alpha: 0.5),
            ),
          ),
        ],
      ),
    );
  }
}

// ── NowCardWidget ──────────────────────────────────────────────

class _NowCardWidget extends StatelessWidget {
  final _NowCard card;
  final VoidCallback onAction;
  final VoidCallback onDismiss;

  const _NowCardWidget({
    required this.card,
    required this.onAction,
    required this.onDismiss,
  });

  @override
  Widget build(BuildContext context) {
    final isDecision = card.type == _NowCardType.decision;
    return Container(
      margin: const EdgeInsets.only(bottom: 4),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(
        color: isDecision ? AppTheme.accentBg : AppTheme.surfaceAlt,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(
          color: isDecision
              ? AppTheme.accent.withValues(alpha: 0.2)
              : AppTheme.border,
          width: 1,
        ),
      ),
      child: Row(
        children: [
          // Icon
          Container(
            width: 28,
            height: 28,
            decoration: BoxDecoration(
              color: isDecision
                  ? AppTheme.accent.withValues(alpha: 0.15)
                  : AppTheme.amberBg,
              borderRadius: BorderRadius.circular(6),
            ),
            child: Center(
              child: Text(
                isDecision ? _decisionIcon() : '📋',
                style: const TextStyle(fontSize: 14),
              ),
            ),
          ),
          const SizedBox(width: 10),

          // Content
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  card.title,
                  style: AppTheme.body.copyWith(
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
                if (card.subtitle != null)
                  Text(
                    card.subtitle!,
                    style: AppTheme.caption.copyWith(
                      color: AppTheme.amber,
                      fontSize: 10,
                    ),
                  ),
              ],
            ),
          ),

          // Action button
          if (isDecision)
            _TinyButton(
              label: card.actionLabel,
              color: AppTheme.accent,
              onTap: onAction,
            )
          else
            _TinyButton(
              label: 'Done',
              color: AppTheme.green,
              onTap: onAction,
            ),

          const SizedBox(width: 4),

          // Dismiss
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(6),
              onTap: onDismiss,
              child: const Padding(
                padding: EdgeInsets.all(4),
                child: Icon(
                  Icons.close,
                  size: 14,
                  color: AppTheme.textMuted,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  String _decisionIcon() {
    switch (card.decisionType) {
      case DecisionType.person:
        return '👤';
      case DecisionType.edge:
        return '🔗';
      case DecisionType.email:
        return '📧';
      case DecisionType.whatsapp:
        return '💬';
      case DecisionType.call:
        return '📞';
      default:
        return '📌';
    }
  }
}

class _TinyButton extends StatelessWidget {
  final String label;
  final Color color;
  final VoidCallback onTap;

  const _TinyButton({
    required this.label,
    required this.color,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(6),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(6),
            border: Border.all(
              color: color.withValues(alpha: 0.4),
              width: 1,
            ),
          ),
          child: Text(
            label,
            style: AppTheme.caption.copyWith(
              color: color,
              fontSize: 10,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ),
    );
  }
}
