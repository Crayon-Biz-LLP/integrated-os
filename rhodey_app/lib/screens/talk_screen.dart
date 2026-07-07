import 'dart:async';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import '../models/message.dart';
import 'history_screen.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';
import '../widgets/chat_bubble.dart';
import '../widgets/voice_states.dart';

class TalkScreen extends StatefulWidget {
  const TalkScreen({super.key});

  @override
  State<TalkScreen> createState() => TalkScreenState();
}

class TalkScreenState extends State<TalkScreen> {
  final _controller = TextEditingController();
  final _scrollController = ScrollController();
  final _api = ApiService();
  int _msgCounter = 0;
  bool _loading = true;

  /// Whether the user has tapped the history pill to see past messages.
  bool _historyExpanded = false;

  /// Whether the user has sent a message this session.
  /// Resets to false on app restart (not persisted).
  bool _hasSentMessage = false;

  /// How many messages were loaded from the API history.
  int _historyCount = 0;

  /// Timer for polling new messages after sending.
  Timer? _pollTimer;

  /// Ids of messages we've already seen from the API (for poll dedup).
  final Set<String> _seenMessageIds = {};

  // ── Send pipeline ──
  //   pending → sending → sent → resolved
  //         ↘ failed → sending (retry)
  // All state is local + optimistic. The API only reports outcome.

  final _messages = <ChatMessage>[];

  // ── Voice state ──
  VoiceState _voiceState = VoiceState.idle;
  String? _transcribedText;
  final stt.SpeechToText _speech = stt.SpeechToText();
  bool _speechAvailable = false;

  @override
  void initState() {
    super.initState();
    _initSpeech();
    _loadHistory();
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    _controller.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  /// Initialize speech recognition on app start.
  Future<void> _initSpeech() async {
    _speechAvailable = await _speech.initialize();
    if (!_speechAvailable && mounted) {
      debugPrint('[Voice] Speech recognition not available on this device');
    }
  }

  /// Fetch real message history on app start and on pull-to-refresh.
  Future<void> _loadHistory() async {
    _messages.clear();

    final result = await _api.getMessages(limit: 30);
    if (!mounted) return;
    if (result.success && result.data!.isNotEmpty) {
      for (final m in result.data!) {
        final content = m['content'] as String? ?? '';
        if (content.isEmpty) continue;
        final direction = m['direction'] as String? ?? '';
        final role = direction == 'inbound'
            ? MessageRole.user
            : MessageRole.rhodey;
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
      setState(() => _loading = false);
    }
  }

  /// Poll for new messages after sending (live updates).
  void _startPolling() {
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(const Duration(seconds: 5), (_) => _pollForUpdates());
  }

  void _stopPolling() {
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
        id: 'p${_msgCounter++}', role: role, text: content,
        timestamp: ts,
        sendStatus: role == MessageRole.user ? SendStatus.sent : null,
      );
      setState(() {
        _messages.add(msg);
        _hasSentMessage = true;
      });
      _scrollToBottom();
    }
  }

  // ── Send pipeline ─────────────────────────────────────────────

  void _sendMessage(String text) {
    if (text.trim().isEmpty) return;
    final id = 'u${++_msgCounter}';
    final msg = ChatMessage(
      id: id, role: MessageRole.user, text: text.trim(),
      timestamp: DateTime.now(), sendStatus: SendStatus.pending,
    );

    setState(() {
      _messages.add(msg);
      _hasSentMessage = true;
    });
    _controller.clear();
    _scrollToBottom();
    _startPolling();

    _updateMessage(id, sendStatus: SendStatus.sending);

    _api.sendMessage(text.trim()).then((result) {
      if (!mounted) return;
      if (result.success) {
        _updateMessage(id, sendStatus: SendStatus.sent);
        String responseText = 'Got it. Processing...';
        if (result.data is Map) {
          final msg = (result.data as Map)['message'] as String?;
          final briefing = (result.data as Map)['briefing'] as String?;
          if (msg != null && msg != 'Message processed') {
            responseText = msg;
          } else if (briefing != null) {
            responseText = briefing;
          }
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

  void _retryMessage(String id) {
    _startPolling();
    final idx = _messages.indexWhere((m) => m.id == id);
    if (idx == -1) return;
    final text = _messages[idx].text;

    _updateMessage(id, sendStatus: SendStatus.sending);

    _api.sendMessage(text).then((result) {
      if (!mounted) return;
      if (result.success) {
        _updateMessage(id, sendStatus: SendStatus.sent);
        String responseText = 'Got it. Processing...';
        if (result.data is Map) {
          final msg = (result.data as Map)['message'] as String?;
          final briefing = (result.data as Map)['briefing'] as String?;
          if (msg != null && msg != 'Message processed') {
            responseText = msg;
          } else if (briefing != null) {
            responseText = briefing;
          }
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
    _stopPolling();
    final id = 'r${++_msgCounter}';
    setState(() {
      _messages.add(ChatMessage(
        id: id, role: MessageRole.rhodey, text: text,
        timestamp: DateTime.now(),
      ));
    });
    _scrollToBottom();
  }

  // ── Real Voice Pipeline (speech_to_text) ─────────────────────

  void _toggleVoice() {
    if (_voiceState == VoiceState.idle) {
      _startListening();
    } else if (_voiceState == VoiceState.listening) {
      _stopListening();
    } else {
      setState(() {
        _voiceState = VoiceState.idle;
        _transcribedText = null;
      });
    }
  }

  Future<void> _startListening() async {
    if (!_speechAvailable) {
      _speechAvailable = await _speech.initialize();
      if (!_speechAvailable) {
        if (mounted) {
          setState(() {
            _voiceState = VoiceState.error;
            _transcribedText = 'Speech recognition not available on this device.';
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
          setState(() => _voiceState = VoiceState.transcribing);
          Future.delayed(const Duration(milliseconds: 300), () {
            if (!mounted) return;
            setState(() => _voiceState = VoiceState.confirm);
          });
        }
      },
      listenOptions: stt.SpeechListenOptions(
        listenFor: const Duration(seconds: 30),
        pauseFor: const Duration(seconds: 2),
        partialResults: true,
        cancelOnError: true,
        localeId: 'en_IN',
      ),
    );
  }

  Future<void> _stopListening() async {
    await _speech.stop();
    if (!mounted) return;
    if (_transcribedText != null && _transcribedText!.isNotEmpty) {
      setState(() => _voiceState = VoiceState.transcribing);
      Future.delayed(const Duration(milliseconds: 300), () {
        if (!mounted) return;
        setState(() => _voiceState = VoiceState.confirm);
      });
    } else {
      setState(() {
        _voiceState = VoiceState.idle;
        _transcribedText = null;
      });
    }
  }

  void _confirmVoice(String type) {
    if (_transcribedText != null && _transcribedText!.isNotEmpty) {
      _sendMessage(_transcribedText!);
    }
    setState(() => _voiceState = VoiceState.done);
    Future.delayed(const Duration(seconds: 2), () {
      if (!mounted) return;
      setState(() {
        _voiceState = VoiceState.idle;
        _transcribedText = null;
      });
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

  /// Called when the user taps the "Show N previous" pill.
  void _expandHistory() {
    setState(() => _historyExpanded = true);
    _scrollToBottom();
  }

  /// Builds the main content area based on current state.
  ///
  /// - Clean default: nothing shown (just input bar at bottom)
  /// - After sending: current conversation visible
  /// - History pill: shown when past messages exist but not expanded
  /// - Full history: shown when expanded or when there's a current conversation
  bool get _showConversation => _historyExpanded || _hasSentMessage;

  // ── Build ─────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            const Text('Talk'),
            if (_api.config.isConfigured)
              Container(
                margin: const EdgeInsets.only(left: 8),
                width: 6, height: 6,
                decoration: const BoxDecoration(
                  color: AppTheme.green, shape: BoxShape.circle,
                ),
              ),
          ],
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.more_horiz, color: AppTheme.textSecondary),
            onPressed: () => _showOverflow(context),
          ),
        ],
      ),
      body: Column(
        children: [
          if (!_api.config.isConfigured)
            Container(
              width: double.infinity,
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              color: AppTheme.redBg,
              child: const Text(
                '⚠️ API not configured. Tap ⋮ → Settings to set your API key.',
                style: TextStyle(color: AppTheme.red, fontSize: 11),
              ),
            ),
          Expanded(
            child: _buildMainArea(),
          ),
          if (_voiceState != VoiceState.idle)
            VoiceStateMachine(
              state: _voiceState,
              transcribedText: _transcribedText,
              onCancel: _toggleVoice,
              onTaskConfirm: () => _confirmVoice('task'),
              onNoteConfirm: () => _confirmVoice('note'),
              onRetry: () => _retryVoice(),
            ),
          _InputBar(
            controller: _controller,
            onSend: _sendMessage,
            onMicTap: _toggleVoice,
            isListening: _voiceState == VoiceState.listening,
          ),
        ],
      ),
    );
  }

  Widget _buildMainArea() {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }

    if (!_showConversation) {
      // ── Clean default: no conversation visible ──
      return Column(
        children: [
          const Spacer(),
          // Subtle prompt text
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 32),
            child: Text(
              'Capture a thought, task, or idea',
              style: AppTheme.body.copyWith(
                color: AppTheme.textTertiary,
                fontSize: 14,
              ),
              textAlign: TextAlign.center,
            ),
          ),
          const SizedBox(height: 8),
          // History pill (if previous messages exist)
          if (_historyCount > 0)
            GestureDetector(
              onTap: _expandHistory,
              child: Container(
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
                      '$_historyCount previous messages ▴',
                      style: AppTheme.caption.copyWith(
                        color: AppTheme.textTertiary,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          const Spacer(),
        ],
      );
    }

    // ── Conversation visible ──
    // Determine which messages to show:
    // If history is expanded OR this is the first send (no history yet), show all.
    // If not expanded but hasSentMessage, show only the current-session messages.
    final displayMessages = _historyExpanded
        ? _messages
        : _messages.skip(_historyCount).toList();

    return RefreshIndicator(
      onRefresh: _loadHistory,
      color: AppTheme.accent,
      child: Column(
        children: [
          // History pill (collapsible banner at top when not expanded)
          if (!_historyExpanded && _historyCount > 0)
            GestureDetector(
              onTap: _expandHistory,
              child: Container(
                width: double.infinity,
                padding: const EdgeInsets.symmetric(vertical: 8),
                color: AppTheme.surfaceAlt.withValues(alpha: 0.5),
                child: Center(
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(Icons.history, size: 14, color: AppTheme.textTertiary),
                      const SizedBox(width: 6),
                      Text(
                        '$_historyCount previous messages ▴',
                        style: AppTheme.caption.copyWith(
                          color: AppTheme.textTertiary,
                          fontSize: 12,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          Expanded(
            child: ListView.builder(
              controller: _scrollController,
              padding: const EdgeInsets.only(top: 8, bottom: 8),
              itemCount: displayMessages.length,
              itemBuilder: (context, index) {
                final msg = displayMessages[index];
                final isGroupStart = index == 0 ||
                    displayMessages[index - 1].role != msg.role;
                return ChatBubble(
                  message: msg,
                  isGroupStart: isGroupStart,
                  onRetry: msg.isFailed ? () => _retryMessage(msg.id) : null,
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  void _showOverflow(BuildContext context) {
    showModalBottomSheet(
      context: context,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (context) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 36, height: 4,
                decoration: BoxDecoration(
                  color: AppTheme.borderLight,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              const SizedBox(height: 20),
              _MenuTile(icon: Icons.history, label: 'History',
                  onTap: () {
                    Navigator.pop(context);
                    Navigator.push(context,
                      MaterialPageRoute(builder: (_) => const HistoryScreen()));
                  }),
              _MenuTile(icon: Icons.memory_outlined, label: 'Memories',
                  onTap: () {
                    Navigator.pop(context);
                    ScaffoldMessenger.of(context).showSnackBar(
                      const SnackBar(
                        content: Text('Memories coming soon — browse knowledge graph', style: TextStyle(fontSize: 12)),
                        duration: Duration(seconds: 2),
                      ),
                    );
                  }),
              _MenuTile(icon: Icons.settings_outlined, label: 'API Settings',
                  onTap: () {
                    Navigator.pop(context);
                    Navigator.push(context,
                      MaterialPageRoute(builder: (_) => const _SettingsScreen()));
                  }),
              _MenuTile(icon: Icons.person_outline, label: 'Profile',
                  onTap: () {
                    Navigator.pop(context);
                    ScaffoldMessenger.of(context).showSnackBar(
                      const SnackBar(
                        content: Text('Coming soon — contact management', style: TextStyle(fontSize: 12)),
                        duration: Duration(seconds: 2),
                      ),
                    );
                  }),
            ],
          ),
        ),
      ),
    );
  }
}

// ── Input bar ──────────────────────────────────────────────────

class _InputBar extends StatelessWidget {
  final TextEditingController controller;
  final ValueChanged<String> onSend;
  final VoidCallback onMicTap;
  final bool isListening;

  const _InputBar({
    required this.controller, required this.onSend,
    required this.onMicTap, required this.isListening,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 12),
      decoration: const BoxDecoration(
        border: Border(top: BorderSide(color: AppTheme.border, width: 1)),
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
                    color: isListening ? AppTheme.accent : AppTheme.border,
                    width: 1,
                  ),
                ),
                child: TextField(
                  controller: controller,
                  textInputAction: TextInputAction.send,
                  onSubmitted: onSend,
                  decoration: const InputDecoration(
                    hintText: 'Type a message — or tap the mic',
                    border: InputBorder.none,
                    contentPadding: EdgeInsets.symmetric(horizontal: 16, vertical: 12),
                    isDense: true,
                  ),
                  style: AppTheme.body,
                ),
              ),
            ),
            const SizedBox(width: 8),
            Material(
              color: isListening ? AppTheme.red : AppTheme.accent,
              borderRadius: BorderRadius.circular(14),
              child: InkWell(
                borderRadius: BorderRadius.circular(14),
                onTap: onMicTap,
                child: Container(
                  width: 44, height: 44,
                  alignment: Alignment.center,
                  child: Icon(
                    isListening ? Icons.stop : Icons.mic,
                    color: Colors.white, size: 20,
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Menu tile ─────────────────────────────────────────────────

class _MenuTile extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  const _MenuTile({
    required this.icon, required this.label, required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return ListTile(
      leading: Icon(icon, color: AppTheme.textSecondary, size: 22),
      title: Text(label, style: AppTheme.body),
      onTap: onTap,
    );
  }
}

// ── API Settings screen ────────────────────────────────────────

class _SettingsScreen extends StatefulWidget {
  const _SettingsScreen();

  @override
  State<_SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<_SettingsScreen> {
  final _api = ApiService();
  late final TextEditingController _urlCtrl;
  late final TextEditingController _keyCtrl;
  bool _saving = false;
  bool _saved = false;

  @override
  void initState() {
    super.initState();
    _urlCtrl = TextEditingController(text: _api.config.baseUrl);
    _keyCtrl = TextEditingController(text: _api.config.apiKey);
  }

  @override
  void dispose() {
    _urlCtrl.dispose();
    _keyCtrl.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    await _api.config.setBaseUrl(_urlCtrl.text.trim());
    await _api.config.setApiKey(_keyCtrl.text.trim());
    if (mounted) {
      setState(() {
        _saving = false;
        _saved = true;
      });
      Future.delayed(const Duration(seconds: 2), () {
        if (mounted) setState(() => _saved = false);
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('API Settings'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: AppTheme.textSecondary),
          onPressed: () => Navigator.pop(context),
        ),
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: _api.config.isConfigured ? AppTheme.greenBg : AppTheme.redBg,
              borderRadius: BorderRadius.circular(12),
              border: Border.all(
                color: _api.config.isConfigured
                    ? AppTheme.green.withValues(alpha: 0.3)
                    : AppTheme.red.withValues(alpha: 0.3),
              ),
            ),
            child: Row(
              children: [
                Icon(
                  _api.config.isConfigured ? Icons.check_circle : Icons.warning,
                  color: _api.config.isConfigured ? AppTheme.green : AppTheme.red,
                  size: 20,
                ),
                const SizedBox(width: 12),
                Text(
                  _api.config.isConfigured
                      ? 'Connected to Rhodey API'
                      : 'Not configured — set your API key below',
                  style: AppTheme.body.copyWith(fontSize: 13),
                ),
              ],
            ),
          ),
          const SizedBox(height: 24),
          const Text('Base URL', style: AppTheme.label),
          const SizedBox(height: 8),
          Container(
            decoration: BoxDecoration(
              color: AppTheme.surfaceAlt,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: AppTheme.border),
            ),
            child: TextField(
              controller: _urlCtrl,
              style: AppTheme.body.copyWith(fontSize: 13),
              decoration: const InputDecoration(
                hintText: 'https://integrated-os.vercel.app',
                border: InputBorder.none,
                contentPadding: EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                isDense: true,
              ),
            ),
          ),
          const SizedBox(height: 16),
          const Text('API Key (X-API-Key)', style: AppTheme.label),
          const SizedBox(height: 8),
          Container(
            decoration: BoxDecoration(
              color: AppTheme.surfaceAlt,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: AppTheme.border),
            ),
            child: TextField(
              controller: _keyCtrl,
              obscureText: true,
              style: AppTheme.body.copyWith(fontSize: 13),
              decoration: const InputDecoration(
                hintText: 'Paste your API key here',
                border: InputBorder.none,
                contentPadding: EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                isDense: true,
              ),
            ),
          ),
          const SizedBox(height: 24),
          SizedBox(
            width: double.infinity,
            height: 48,
            child: ElevatedButton(
              onPressed: _saving ? null : _save,
              style: ElevatedButton.styleFrom(
                backgroundColor: AppTheme.accent,
                foregroundColor: Colors.white,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              child: _saving
                  ? const SizedBox(
                      width: 20, height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white),
                    )
                  : Text(_saved ? '✓ Saved' : 'Save'),
            ),
          ),
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: AppTheme.accentBg,
              borderRadius: BorderRadius.circular(10),
            ),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Icon(Icons.info_outline, color: AppTheme.accent, size: 16),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'Your API key is stored securely on-device and never shared. '
                    'Get it from your Rhodey deployment settings.',
                    style: AppTheme.caption.copyWith(fontSize: 11, color: AppTheme.textSecondary),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
