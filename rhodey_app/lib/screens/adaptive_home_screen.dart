import 'dart:async';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:flutter_tts/flutter_tts.dart';
import 'menu_sheet.dart';
import 'inbox_screen.dart';
import 'today_screen.dart';
import '../models/message.dart';
import '../services/notification_service.dart';
import '../models/briefing.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';
import '../services/widget_data_provider.dart';
import '../widgets/chat_bubble.dart';
import '../widgets/voice_states.dart';
import '../widgets/rich_card_content.dart';
import '../utils/home_instrumentation.dart';

// ── Types ──────────────────────────────────────────────────────

/// A focal item — the top-priority thing Rhodey surfaces.
class _FocalItem {
  final String id;
  final String title;
  final String? subtitle;
  final String? description;
  final String actionLabel;
  final String? source; // "task", "graph_node", "graph_edge", "email", etc.
  final Map<String, dynamic> metadata;

  const _FocalItem({
    required this.id,
    required this.title,
    this.subtitle,
    this.description,
    this.actionLabel = 'Action',
    this.source,
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

  // ── Focal zone (Phase 2 v2: LLM-chosen top item + three-button model) ──
  List<_FocalItem> _focalItems = [];
  bool _focalLoading = true;
  /// ID of the focal item currently animating to "done" state
  String? _completingCardId;

  // ── CONVERSATION ──
  final _messages = <ChatMessage>[];
  final _scrollController = ScrollController();
  final _textController = TextEditingController();
  int _msgCounter = 0;
  Timer? _pollTimer;
  final _seenMessageIds = <String>{};

  // Push-driven delivery: replaces aggressive 5s polling.
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
    _loadFocalItems();
    _initSpeech();
    _tts.setLanguage("en-US");
    _tts.setSpeechRate(0.5);

    NotificationService.onNotificationOpened = _handlePushNotificationTap;
    NotificationService.onPushReceived = _handlePushReceived;

    final pendingData = NotificationService.pendingOpenData;
    if (pendingData != null) {
      NotificationService.pendingOpenData = null;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _handlePushNotificationTap(pendingData);
      });
    }
  }

  @override
  void dispose() {
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

          // If briefing has an LLM-chosen top_focal_item, promote it as #1
          final focal = briefing.topFocalItem;
          if (focal != null && focal.isNotEmpty && focal['title'] != null) {
            final existingIdx = _focalItems.indexWhere(
              (f) => f.id == 'focal_${focal['item_id']}',
            );
            final itemType = focal['type'] as String? ?? 'task';
            final llmLabel = focal['action_label'] as String?;
            final newItem = _FocalItem(
              id: 'focal_${focal['item_id'] ?? focal['title']}',
              title: focal['title'] as String? ?? '',
              description: focal['reason'] as String?,
              actionLabel: _actionLabelForType(itemType, llmLabel),
              source: itemType,
              metadata: {
                'focal_item_id': focal['item_id'],
                'focal_type': focal['type'],
                'reason': focal['reason'],
                'urgency': focal['urgency'],
                'from_briefing': true,
              },
            );
            if (existingIdx >= 0) {
              _focalItems[existingIdx] = newItem;
            } else {
              _focalItems.insert(0, newItem);
            }
            // Remove any duplicate from _loadFocalItems that has the same natural ID
            final naturalId = focal['item_id']?.toString() ?? '';
            if (naturalId.isNotEmpty) {
              _focalItems.removeWhere((f) =>
                f.id == naturalId && f.id != newItem.id
              );
            }
          }
        });
        WidgetDataProvider().updatePulseWidget(briefing);
      }
    } catch (_) {
      if (mounted) {
        setState(() => _briefingLoading = false);
      }
    }
  }

  // ── Focal items ─────────────────────────────────────────────────

  Future<void> _loadFocalItems() async {
    try {
      final decResult = await _api.getPendingDecisions();
      final taskResult = await _api.getTasks(status: 'todo');
      if (!mounted) return;

      final items = <_FocalItem>[];

      // Decisions first (graph nodes, edges, channel items)
      if (decResult.success && decResult.data != null) {
        for (final pd in decResult.data!) {
          final source = pd.source;
          items.add(_FocalItem(
            id: pd.id,
            title: pd.title,
            description: pd.description,
            actionLabel: _sourceActionLabel(source),
            source: source,
            metadata: {
              'source': source,
              'api_id': pd.id,
              'pending_id': int.tryParse(pd.id),
            },
          ));
        }
      }

      // Due/overdue tasks fill remaining slots
      if (taskResult.success && taskResult.data != null) {
        for (final t in taskResult.data!) {
          final deadline = t['deadline'] as String?;
          if (deadline == null || !_isUrgent(deadline)) continue;
          items.add(_FocalItem(
            id: t['id'].toString(),
            title: t['title'] as String? ?? 'Untitled',
            subtitle: _formatDeadline(deadline),
            source: 'task',
            actionLabel: 'Done',
            metadata: {'task_id': t['id']},
          ));
        }
      }

      if (mounted) {
        setState(() {
          _focalItems = items;
          _focalLoading = false;
          _instrumentation.nowCardsShown = items.length;
        });
      }
    } catch (_) {
      // Network or parse error — show empty state
    } finally {
      if (mounted) {
        setState(() => _focalLoading = false);
      }
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

  String _sourceActionLabel(String source) {
    switch (source) {
      case 'graph_node': return 'Approve';
      case 'graph_edge': return 'Review';
      case 'email': return 'Create';
      case 'whatsapp': return 'Create';
      case 'call': return 'Create';
      default: return 'View';
    }
  }

  /// Derive the first-button label from the item's type field.
  /// This is the authoritative source — overrides the LLM's `action_label`
  /// to ensure the button text always makes sense for the item type.
  String _actionLabelForType(String type, [String? llmFallback]) {
    switch (type) {
      case 'task':
        return llmFallback ?? "I'll do it";
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
        return llmFallback ?? "I'll do it";
    }
  }

  // ── Focal card action + promotion ─────────────────────────

  /// Complete the current focal card via /api/focal-action, then promote next.
  Future<void> _completeFocalItem(_FocalItem item) async {
    // 1. Start done animation (green flash, 300ms)
    setState(() => _completingCardId = item.id);
    await Future.delayed(const Duration(milliseconds: 300));
    if (!mounted) return;

    // 2. Execute via /api/focal-action (Phase 2 v2 unified endpoint)
    final fromBriefing = item.metadata['from_briefing'] == true;
    if (fromBriefing && item.metadata['focal_item_id'] != null) {
      // LLM-chosen item from briefing — use unified endpoint
      await _api.focalAction(
        action: 'done',
        itemType: item.metadata['focal_type'] as String? ?? item.source ?? 'task',
        itemId: item.metadata['focal_item_id'].toString(),
        title: item.title,
      );
    } else {
      // Legacy fallback: direct API calls
      if (item.source == 'task') {
        final taskId = item.metadata['task_id'];
        if (taskId != null) {
          await _api.updateTaskStatus(taskId as int, 'done');
        }
      } else {
        final source = item.metadata['source'] as String?;
        final pendingId = item.metadata['pending_id'] as int?;
        if (source != null && pendingId != null) {
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
          if (result != null && !result.success && mounted) {
            ScaffoldMessenger.of(context).showSnackBar(
              SnackBar(
                content: Text(result.error ?? 'Action failed', style: const TextStyle(fontSize: 12)),
                backgroundColor: AppTheme.red,
                duration: const Duration(seconds: 2),
              ),
            );
          }
        }
      }
    }
    _instrumentation.homeActionsCompleted++;
    if (!mounted) return;

    // 3. Remove completed item — the next item promotes immediately
    setState(() {
      _focalItems.removeWhere((c) => c.id == item.id);
      _completingCardId = null;
    });

    // 4. If no items remain, show "all clear" from Rhodey
    if (_focalItems.isEmpty && mounted) {
      _addRhodeyResponse(
        "All clear for now, Danny. You have some deep work time before your next sync — I'll keep tracking the board.",
      );
    }

    // 5. Background refresh for fresh counts (deferred 2s to avoid flicker)
    Future.delayed(const Duration(seconds: 2), () {
      if (mounted) _loadFocalItems();
    });
  }

  /// Skip/dismiss the current focal item — snooze (no learning signal).
  Future<void> _snoozeFocalItem(_FocalItem item) async {
    _instrumentation.itemsDismissed++;

    // Send snooze signal to API (no learning — just dismisses until next pulse)
    final fromBriefing = item.metadata['from_briefing'] == true;
    if (fromBriefing && item.metadata['focal_item_id'] != null) {
      await _api.focalAction(
        action: 'snooze',
        itemType: item.metadata['focal_type'] as String? ?? item.source ?? 'task',
        itemId: item.metadata['focal_item_id'].toString(),
        title: item.title,
      );
    }

    setState(() {
      _focalItems.removeWhere((c) => c.id == item.id);
    });
    if (_focalItems.isEmpty && mounted) {
      _addRhodeyResponse(
        "Noted. I'll bring it back if it's still relevant next time.",
      );
    }
  }

  /// Correct the focal item — sends correction signal to learning loop.
  Future<void> _correctFocalItem(_FocalItem item) async {
    final fromBriefing = item.metadata['from_briefing'] == true;
    if (fromBriefing && item.metadata['focal_item_id'] != null) {
      await _api.focalAction(
        action: 'correct',
        itemType: item.metadata['focal_type'] as String? ?? item.source ?? 'task',
        itemId: item.metadata['focal_item_id'].toString(),
        title: item.title,
        reason: item.metadata['reason'] as String? ?? '',
      );
    }

    setState(() {
      _focalItems.removeWhere((c) => c.id == item.id);
    });
    if (_focalItems.isEmpty && mounted) {
      _addRhodeyResponse(
        "Got it — I've noted that wasn't the right priority. I'll adjust.",
      );
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
        final role = direction == 'inbound' ? MessageRole.user : MessageRole.rhodey;
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
  }

  void _handlePushReceived(Map<String, dynamic> data) {
    debugPrint('[PushDelivery] Push received: type=${data['type']}');
    final type = data['type'];
    if (type == 'briefing' || type == 'briefing_refresh') {
      _loadBriefing();
    }
    _pollForUpdates();
  }

  void _startBackupPoll() {
    _awaitingResponse = true;
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(_backupPollInterval, (_) {
      if (!_awaitingResponse) { _stopBackupPoll(); return; }
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

      final direction = m['direction'] as String? ?? '';
      final role = direction == 'inbound' ? MessageRole.user : MessageRole.rhodey;
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
      setState(() { _messages.add(msg); });
      _scrollToBottom();
    }
  }

  void _sendMessage(String text) {
    if (text.trim().isEmpty) return;
    final id = 'u${++_msgCounter}';
    final msg = ChatMessage(
      id: id, role: MessageRole.user, text: text.trim(),
      timestamp: DateTime.now(), sendStatus: SendStatus.pending,
    );

    setState(() { _messages.add(msg); });
    _textController.clear();
    _scrollToBottom();
    _startBackupPoll();

    _api.sendMessage(text.trim()).then((result) {
      if (!mounted) return;
      if (result.success) {
        _updateMessage(id, sendStatus: SendStatus.sent);
        String responseText;
        if (result.data is Map) {
          responseText = (result.data as Map)['response'] as String? ?? 'Got it. Processing...';
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

  void _updateMessage(String id, {String? text, SendStatus? sendStatus}) {
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
        id: id, role: MessageRole.rhodey, text: text, timestamp: DateTime.now(),
      ));
    });
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
          setState(() { _voiceState = VoiceState.idle; _voiceError = null; });
          return;
        }
        setState(() { _voiceState = VoiceState.error; _voiceError = error.errorMsg; });
      },
      onStatus: (status) { debugPrint('[Voice] Status: $status'); },
    );
    if (!_speechAvailable && mounted) {
      setState(() { _voiceState = VoiceState.error; _voiceError = 'Speech recognition not available'; });
    }
  }

  void _toggleVoice() {
    if (_voiceState == VoiceState.idle) { _startListening(); }
    else if (_voiceState == VoiceState.listening) { _stopListening(); }
    else { setState(() { _voiceState = VoiceState.idle; _transcribedText = null; _voiceError = null; }); }
  }

  Future<void> _startListening() async {
    _voiceError = null;
    if (!_speechAvailable) {
      _speechAvailable = await _speech.initialize();
      if (!_speechAvailable) {
        if (mounted) { setState(() { _voiceState = VoiceState.error; _voiceError = 'Speech recognition not available'; }); }
        return;
      }
    }
    setState(() => _voiceState = VoiceState.listening);
    await _speech.listen(
      onResult: (result) {
        if (!mounted) return;
        final text = result.recognizedWords;
        if (text.isNotEmpty) { _transcribedText = text; }
        if (result.finalResult) {
          final words = result.recognizedWords;
          if (words.isNotEmpty) { _sendMessage(words); }
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
    setState(() { _voiceState = VoiceState.idle; _transcribedText = null; });
  }

  void _retryVoice() {
    setState(() { _voiceState = VoiceState.idle; _transcribedText = null; });
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
    Navigator.push(context, MaterialPageRoute(builder: (_) => const InboxScreen()));
  }

  void _handlePushNotificationTap(Map<String, dynamic> data) {
    final type = data['type'];
    debugPrint('[PushNav] Notification type=$type data=$data');
    switch (type) {
      case 'decision':
      case 'delegation':
        _openInbox();
        break;
      case 'nudge':
        Navigator.push(context, MaterialPageRoute(builder: (_) => const TodayScreen()));
        break;
      case 'briefing':
      default:
        break;
    }
  }

  // ── Scroll & TTS ─────────────────────────────────────────────

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

  void _speakMessage(String text) {
    if (text.trim().isEmpty) return;
    _tts.stop();
    _tts.speak(text.trim());
  }

  // ── Pulse mode helpers ──────────────────────────────────────

  String _pulseLabel() {
    final mode = _briefing.pulseMode ?? '';
    switch (mode.toLowerCase()) {
      case 'morning': return 'MORNING';
      case 'afternoon': return 'AFTERNOON';
      case 'closing_loop': return 'CLOSING';
      case 'weekend': return 'WEEKEND';
      case 'pre_monday': return 'PRE-MONDAY';
      case 'intel': return 'INTEL';
      default: return '';
    }
  }

  Color _pulseColor() {
    final mode = _briefing.pulseMode ?? '';
    switch (mode.toLowerCase()) {
      case 'morning': return AppTheme.accent;
      case 'afternoon': return AppTheme.amber;
      case 'closing_loop': return AppTheme.red;
      case 'intel': return AppTheme.textTertiary;
      default: return AppTheme.accent;
    }
  }

  // ═══════════════════════════════════════════════════════════
  // BUILD
  // ═══════════════════════════════════════════════════════════

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.background,
      body: SafeArea(
        child: Column(
          children: [
            _buildAmbientHeader(),
            _buildFocalArea(),
            Expanded(child: _buildConversationArea()),
            if (_voiceState == VoiceState.listening || _voiceState == VoiceState.error)
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

  // ── Ambient Header ─────────────────────────────────────────

  Widget _buildAmbientHeader() {
    final pulseLabel = _pulseLabel();
    final pulseColor = _pulseColor();
    final vaultTotal = _briefing.vaultedCount;

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: AppTheme.border, width: 1)),
      ),
      child: Row(
        children: [
          // Menu
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(10),
              onTap: _showMenu,
              child: Container(
                padding: const EdgeInsets.all(8),
                child: const Icon(Icons.menu, color: AppTheme.textSecondary, size: 22),
              ),
            ),
          ),

          const SizedBox(width: 8),

          // Rhodey dot + pulse mode
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(
              color: pulseColor,
              shape: BoxShape.circle,
            ),
          ),
          const SizedBox(width: 6),
          Text(
            'Rhodey${pulseLabel.isNotEmpty ? ' · $pulseLabel' : ''}',
            style: AppTheme.body.copyWith(
              fontWeight: FontWeight.w600,
              fontSize: 13,
              letterSpacing: 0.3,
            ),
          ),

          const Spacer(),

          // Vault badge
          if (vaultTotal > 0)
            Material(
              color: Colors.transparent,
              child: InkWell(
                borderRadius: BorderRadius.circular(10),
                onTap: () {
                  _addRhodeyResponse('📦 $vaultTotal items vaulted behind the pulse. Want me to pull any forward?');
                },
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                  decoration: BoxDecoration(
                    color: AppTheme.surfaceAlt,
                    borderRadius: BorderRadius.circular(6),
                    border: Border.all(color: AppTheme.border, width: 1),
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Text('📦', style: TextStyle(fontSize: 10)),
                      const SizedBox(width: 3),
                      Text(
                        '$vaultTotal',
                        style: AppTheme.caption.copyWith(
                          color: AppTheme.textTertiary,
                          fontSize: 11,
                          fontWeight: FontWeight.w600,
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

  // ── Focal Area (Chief's Stage) ─────────────────────────────

  Widget _buildFocalArea() {
    if (_briefingLoading || _focalLoading) {
      return Container(
        padding: const EdgeInsets.all(16),
        child: const Center(child: SizedBox(
          width: 14, height: 14,
          child: CircularProgressIndicator(strokeWidth: 2, color: AppTheme.textTertiary),
        )),
      );
    }

    return Container(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 6),
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: AppTheme.border, width: 1)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          // Rhodey's voice line as greeting
          _buildRhodeyStage(),

          // Focal card (Top-1 item)
          if (_focalItems.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: _buildFocalCard(_focalItems.first),
            ),

          // Decision digest
          if (!_briefingLoading && _briefing.pendingCount > 0)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: _buildDecisionDigest(),
            ),

          // All-clear state
          if (_focalItems.isEmpty && !_focalLoading)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: _buildAllClear(),
            ),

          // Sunday weekly transparency report
          if (_briefing.transparencyReport != null && _briefing.transparencyReport!.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: _buildTransparencyReport(),
            ),

          // Quick-reply chips
          if (_focalItems.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: _buildQuickReplyChips(),
            ),
        ],
      ),
    );
  }

  // ── Rhodey's Stage ─────────────────────────────────────────

  Widget _buildRhodeyStage() {
    final voice = _briefing.voiceLine ?? '';
    final greeting = _briefing.greeting;

    String rhodeySays;
    if (voice.isNotEmpty) {
      rhodeySays = voice;
    } else if (greeting.isNotEmpty) {
      rhodeySays = greeting;
    } else {
      final h = DateTime.now().hour;
      final prefix = h < 12 ? 'morning' : h < 17 ? 'afternoon' : 'evening';
      rhodeySays = 'Good $prefix, Danny.';
    }

    return Padding(
      padding: const EdgeInsets.only(bottom: 2),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: const EdgeInsets.only(top: 2, right: 6),
                child: Text(
                  '\u201c',
                  style: TextStyle(
                    fontSize: 22,
                    fontWeight: FontWeight.w300,
                    color: AppTheme.accent.withValues(alpha: 0.4),
                    height: 1.0,
                  ),
                ),
              ),
              Expanded(
                child: Text(
                  rhodeySays,
                  style: const TextStyle(
                    fontFamily: 'InstrumentSerif',
                    fontSize: 22,
                    fontWeight: FontWeight.w300,
                    color: AppTheme.textPrimary,
                    height: 1.25,
                  ),
                ),
              ),
            ],
          ),
          Padding(
            padding: const EdgeInsets.only(left: 28, top: 2),
            child: Text(
              'Rhodey',
              style: AppTheme.caption.copyWith(
                color: AppTheme.accent,
                fontSize: 9,
                letterSpacing: 0.5,
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ── Focal Card (Phase 2 v2: three-button model with LLM reason) ──

  Widget _buildFocalCard(_FocalItem item) {
    final isCompleting = _completingCardId == item.id;
    final isFromBriefing = item.metadata['from_briefing'] == true;
    final focalReason = item.metadata['reason'] as String?;
    final urgency = item.metadata['urgency'] as String?;

    // Urgency color
    Color urgencyColor;
    switch (urgency) {
      case 'critical':
        urgencyColor = AppTheme.red;
        break;
      case 'important':
        urgencyColor = AppTheme.amber;
        break;
      default:
        urgencyColor = AppTheme.accent;
    }

    return AnimatedOpacity(
      opacity: isCompleting ? 0.0 : 1.0,
      duration: const Duration(milliseconds: 350),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 350),
        width: double.infinity,
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: isCompleting ? AppTheme.green.withValues(alpha: 0.08) : AppTheme.accentBg,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
            color: isCompleting
                ? AppTheme.green.withValues(alpha: 0.3)
                : urgencyColor.withValues(alpha: 0.18),
            width: 1,
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(
              children: [
                Container(
                  width: 3,
                  height: 14,
                  decoration: BoxDecoration(
                    color: isCompleting ? AppTheme.green : urgencyColor,
                    borderRadius: BorderRadius.circular(1),
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  isCompleting ? 'DONE' : 'FOCUS',
                  style: AppTheme.label.copyWith(
                    color: isCompleting ? AppTheme.green : urgencyColor,
                    fontSize: 9,
                    letterSpacing: 1.5,
                  ),
                ),
                if (isFromBriefing && !isCompleting)
                  Padding(
                    padding: const EdgeInsets.only(left: 6),
                    child: Text(
                      'RHODEY',
                      style: AppTheme.caption.copyWith(
                        color: AppTheme.textTertiary,
                        fontSize: 7,
                        letterSpacing: 1.0,
                      ),
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 10),

            // Title
            Text(
              item.title,
              style: AppTheme.body.copyWith(
                fontSize: 14,
                fontWeight: FontWeight.w600,
                height: 1.3,
              ),
            ),

            // LLM reason — the "why" behind this choice
            if (focalReason != null && focalReason.isNotEmpty && !isCompleting)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  focalReason,
                  style: AppTheme.bodySmall.copyWith(
                    color: AppTheme.textSecondary,
                    fontSize: 11,
                    height: 1.4,
                    fontStyle: FontStyle.italic,
                  ),
                ),
              ),

            // Deadline subtitle (legacy)
            if (item.subtitle != null && item.subtitle!.isNotEmpty && focalReason == null)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  item.subtitle!,
                  style: AppTheme.caption.copyWith(
                    color: AppTheme.textTertiary,
                    fontSize: 10,
                  ),
                ),
              ),

            // Next event (contextual)
            if (_briefing.nextEvent != null && _briefing.nextEvent!.isNotEmpty && !isFromBriefing)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  '📅 ${_briefing.nextEvent}',
                  style: AppTheme.caption.copyWith(
                    color: AppTheme.textTertiary,
                    fontSize: 10,
                  ),
                ),
              ),

            const SizedBox(height: 12),

            // Three-button model
            Row(
              children: [
                // "I'll do it" — confirm and complete
                Expanded(
                  child: _MiniButton(
                    label: isFromBriefing ? (item.actionLabel) : 'Done',
                    color: AppTheme.green,
                    onTap: () => _completeFocalItem(item),
                  ),
                ),
                const SizedBox(width: 6),
                // "Not now" — snooze (no learning signal)
                Expanded(
                  child: _MiniButton(
                    label: 'Not now',
                    color: AppTheme.amber,
                    onTap: () => _snoozeFocalItem(item),
                  ),
                ),
                // "Not right" — correction signal (only for LLM-chosen items)
                if (isFromBriefing) ...[
                  const SizedBox(width: 6),
                  Expanded(
                    child: _MiniButton(
                      label: 'Not right',
                      color: AppTheme.red,
                      onTap: () => _correctFocalItem(item),
                    ),
                  ),
                ],
              ],
            ),
          ],
        ),
      ),
    );
  }

  // ── Decision Digest ─────────────────────────────────────────

  Widget _buildDecisionDigest() {
    final count = _briefing.pendingCount;

    return Container(
      padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 12),
      decoration: BoxDecoration(
        color: AppTheme.green.withValues(alpha: 0.06),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(
          color: AppTheme.green.withValues(alpha: 0.15),
          width: 1,
        ),
      ),
      child: Row(
        children: [
          const Icon(Icons.check_circle_outline, size: 14, color: AppTheme.green),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              '$count item${count != 1 ? 's' : ''} in Inbox awaiting your decision.',
              style: AppTheme.bodySmall.copyWith(
                fontSize: 11,
                color: AppTheme.textSecondary,
                height: 1.3,
              ),
            ),
          ),
          const SizedBox(width: 8),
          GestureDetector(
            onTap: _openInbox,
            child: Text(
              'View',
              style: AppTheme.caption.copyWith(
                color: AppTheme.accent,
                fontSize: 11,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ── Transparency Report ──────────────────────────────────

  Widget _buildTransparencyReport() {
    final report = _briefing.transparencyReport ?? '';
    if (report.isEmpty) return const SizedBox.shrink();

    // Simplify for app display: extract the first few lines
    final lines = report.split('\n').where((l) => l.trim().isNotEmpty).toList();
    final displayLines = lines.length > 8 ? lines.take(8).toList() : lines;

    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: AppTheme.surfaceAlt,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: AppTheme.border, width: 1),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              const Text('🧠', style: TextStyle(fontSize: 14)),
              const SizedBox(width: 6),
              Text(
                "What I Learned This Week",
                style: AppTheme.label.copyWith(
                  fontSize: 11,
                  letterSpacing: 0.5,
                  color: AppTheme.textPrimary,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          for (final line in displayLines) ...[
            Padding(
              padding: const EdgeInsets.only(bottom: 3),
              child: Text(
                line,
                style: AppTheme.caption.copyWith(
                  fontSize: 10,
                  color: AppTheme.textSecondary,
                  height: 1.4,
                ),
              ),
            ),
          ],
          if (lines.length > 8)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: GestureDetector(
                onTap: () {
                  _addRhodeyResponse(report);
                },
                child: Text(
                  '+ See full report',
                  style: AppTheme.caption.copyWith(
                    color: AppTheme.accent,
                    fontSize: 10,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }

  // ── All Clear ──────────────────────────────────────────────

  Widget _buildAllClear() {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(
        children: [
          Container(
            width: 6,
            height: 6,
            decoration: const BoxDecoration(color: AppTheme.green, shape: BoxShape.circle),
          ),
          const SizedBox(width: 8),
          const Flexible(
            child: Text(
              'Nothing needs your attention right now.',
              style: TextStyle(
                fontSize: 12,
                color: AppTheme.textTertiary,
                fontWeight: FontWeight.w300,
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ── Quick-Reply Chips ──────────────────────────────────────

  Widget _buildQuickReplyChips() {
    // Show chips matching the three-button model
    final chips = <String>[
      "I'll do it",
      'Not now',
      'Not right',
    ];

    return Wrap(
      spacing: 6,
      runSpacing: 4,
      children: chips.map((chip) {
        Color chipColor;
        Color chipBg;
        switch (chip) {
          case "I'll do it":
            chipColor = AppTheme.green;
            chipBg = AppTheme.green.withValues(alpha: 0.08);
            break;
          case 'Not now':
            chipColor = AppTheme.amber;
            chipBg = AppTheme.amber.withValues(alpha: 0.08);
            break;
          case 'Not right':
            chipColor = AppTheme.red;
            chipBg = AppTheme.red.withValues(alpha: 0.08);
            break;
          default:
            chipColor = AppTheme.textSecondary;
            chipBg = AppTheme.surfaceAlt;
        }

        return GestureDetector(
          onTap: () {
            if (_focalItems.isEmpty) return;
            switch (chip) {
              case "I'll do it":
                _completeFocalItem(_focalItems.first);
                break;
              case 'Not now':
                _snoozeFocalItem(_focalItems.first);
                break;
              case 'Not right':
                _correctFocalItem(_focalItems.first);
                break;
            }
          },
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            decoration: BoxDecoration(
              color: chipBg,
              borderRadius: BorderRadius.circular(16),
              border: Border.all(
                color: chipColor.withValues(alpha: 0.25),
                width: 1,
              ),
            ),
            child: Text(
              chip,
              style: AppTheme.caption.copyWith(
                color: chipColor,
                fontSize: 11,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
        );
      }).toList(),
    );
  }

  // ── Conversation area ────────────────────────────────────────

  Widget _buildConversationArea() {
    if (_messages.isEmpty) {
      return _buildEmptyConversation();
    }

    return RefreshIndicator(
      onRefresh: _loadHistory,
      color: AppTheme.accent,
      child: ListView(
        controller: _scrollController,
        padding: const EdgeInsets.only(top: 4, bottom: 8),
        children: [
          // History pill
          GestureDetector(
            onTap: _loadHistory,
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
                  const Icon(Icons.history, size: 14, color: AppTheme.textTertiary),
                  const SizedBox(width: 6),
                  Text(
                    'Show earlier activity',
                    style: AppTheme.caption.copyWith(
                      color: AppTheme.textTertiary,
                      fontSize: 12,
                    ),
                  ),
                ],
              ),
            ),
          ),

          // Messages
          ..._messages.asMap().entries.map((entry) {
            final index = entry.key;
            final msg = entry.value;

            if (msg.role == MessageRole.rhodey && !msg.isRhodeyTyping) {
              final cardData = parseMessageToCardData(msg.text);
              if (cardData != null) {
                return _buildRichCard(msg, cardData);
              }
            }

            final isGroupStart = index == 0 || _messages[index - 1].role != msg.role;
            return ChatBubble(
              message: msg,
              isGroupStart: isGroupStart,
              onRetry: msg.isFailed ? () => _retryMessage(msg.id) : null,
              onTap: !msg.isUser && msg.text.isNotEmpty ? () => _speakMessage(msg.text) : null,
            );
          }),

          const SizedBox(height: 16),
        ],
      ),
    );
  }

  Widget _buildEmptyConversation() {
    return Center(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.chat_bubble_outline, size: 28, color: AppTheme.textMuted),
            const SizedBox(height: 12),
            const Text(
              'Speak or type to get started',
              style: TextStyle(
                fontSize: 14,
                color: AppTheme.textTertiary,
                fontWeight: FontWeight.w300,
              ),
              textAlign: TextAlign.center,
            ),
          ],
        ),
      ),
    );
  }

  /// Find a task ID from a card title by fetching open tasks.
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

  Widget _buildRichCard(ChatMessage msg, CardData cardData) {
    VoidCallback? onMarkDone;
    if (cardData.type == CardType.task) {
      onMarkDone = () async {
        _instrumentation.homeActionsCompleted++;
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
            onTap: () => _speakMessage(msg.text),
          ),
        ],
      ),
    );
  }

  void _retryMessage(String id) {
    final idx = _messages.indexWhere((m) => m.id == id);
    if (idx == -1) return;
    final text = _messages[idx].text;
    setState(() {
      _messages.removeAt(idx);
    });
    _sendMessage(text);
  }

  // ── Input bar ──────────────────────────────────────────────

  Widget _buildInputBar() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: const BoxDecoration(
        border: Border(top: BorderSide(color: AppTheme.border, width: 1)),
      ),
      child: Row(
        children: [
          // Voice button
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(20),
              onTap: _toggleVoice,
              child: Container(
                padding: const EdgeInsets.all(8),
                child: Icon(
                  _voiceState == VoiceState.listening ? Icons.mic : Icons.mic_none,
                  color: _voiceState == VoiceState.listening ? AppTheme.accent : AppTheme.textTertiary,
                  size: 20,
                ),
              ),
            ),
          ),

          const SizedBox(width: 4),

          // Text field
          Expanded(
            child: TextField(
              controller: _textController,
              style: AppTheme.body.copyWith(fontSize: 14),
              decoration: InputDecoration(
                hintText: 'Ask Rhodey...',
                hintStyle: AppTheme.caption.copyWith(
                  color: AppTheme.textTertiary,
                  fontSize: 14,
                ),
                border: InputBorder.none,
                contentPadding: const EdgeInsets.symmetric(vertical: 8),
                isDense: true,
              ),
              textInputAction: TextInputAction.send,
              onSubmitted: (text) {
                if (text.trim().isNotEmpty) {
                  _sendMessage(text.trim());
                }
              },
            ),
          ),

          // Send button
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(20),
              onTap: () {
                final text = _textController.text;
                if (text.trim().isNotEmpty) {
                  _sendMessage(text.trim());
                }
              },
              child: Container(
                padding: const EdgeInsets.all(8),
                child: const Icon(Icons.arrow_upward, color: AppTheme.accent, size: 20),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ── Mini Button Widget ─────────────────────────────────────────

class _MiniButton extends StatelessWidget {
  final String label;
  final Color color;
  final VoidCallback onTap;

  const _MiniButton({
    required this.label,
    required this.color,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 8),
        decoration: BoxDecoration(
          color: color.withValues(alpha: 0.1),
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: color.withValues(alpha: 0.25), width: 1),
        ),
        child: Center(
          child: Text(
            label,
            style: AppTheme.caption.copyWith(
              color: color,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ),
    );
  }
}
