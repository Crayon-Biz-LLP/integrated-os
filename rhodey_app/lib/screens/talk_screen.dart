import 'package:flutter/material.dart';
import '../models/message.dart';
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
  int _msgCounter = 5;
  bool _loading = true;

  // ── Send pipeline ──
  //   pending → sending → sent → resolved
  //         ↘ failed → sending (retry)
  // All state is local + optimistic. The API only reports outcome.

  final _messages = <ChatMessage>[
    ChatMessage(
      id: '1', role: MessageRole.rhodey,
      text: 'Good morning. You have a busy day ahead — Equisoft at 10, Qhord pricing review at 2. I\'d start with the deck.',
      timestamp: DateTime.now().subtract(const Duration(hours: 3)),
    ),
    ChatMessage(
      id: '2', role: MessageRole.user,
      text: "What's the status on Qhord?",
      timestamp: DateTime.now().subtract(const Duration(hours: 2, minutes: 45)),
      sendStatus: SendStatus.sent,
    ),
    ChatMessage(
      id: '3', role: MessageRole.rhodey,
      text: 'Qhord pricing is at ₹2.4L. Marcus approved the quote. Blocked on Anil\'s signature.',
      timestamp: DateTime.now().subtract(const Duration(hours: 2, minutes: 43)),
    ),
    ChatMessage(
      id: '4', role: MessageRole.user,
      text: 'Need to email Marcus about the Qhord pricing update',
      timestamp: DateTime.now().subtract(const Duration(hours: 1)),
      sendStatus: SendStatus.sent,
    ),
    ChatMessage(
      id: '5', role: MessageRole.rhodey, type: MessageType.taskResult,
      text: '✅ Logged. Task created: Follow up with Marcus re: Equisoft contract.',
      timestamp: DateTime.now().subtract(const Duration(hours: 1)),
    ),
  ];

  // ── Voice state ──
  VoiceState _voiceState = VoiceState.idle;
  String? _transcribedText;

  @override
  void initState() {
    super.initState();
    _loadHistory();
  }

  /// Fetch real message history on app start and on pull-to-refresh.
  /// Strips previously loaded API messages, then re-fetches fresh.
  Future<void> _loadHistory() async {
    // Remove previously loaded API messages (ids starting with 'h')
    _messages.removeWhere((m) => m.id.startsWith('h'));

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
    if (mounted) {
      setState(() => _loading = false);
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  // ── Send pipeline ─────────────────────────────────────────────

  void _sendMessage(String text) {
    if (text.trim().isEmpty) return;
    final id = 'u${++_msgCounter}';
    final msg = ChatMessage(
      id: id, role: MessageRole.user, text: text.trim(),
      timestamp: DateTime.now(), sendStatus: SendStatus.pending,
    );

    setState(() => _messages.add(msg));
    _controller.clear();
    _scrollToBottom();

    _updateMessage(id, sendStatus: SendStatus.sending);

    _api.sendMessage(text.trim()).then((result) {
      if (!mounted) return;
      if (result.success) {
        _updateMessage(id, sendStatus: SendStatus.sent);
        // Try to extract Rhodey's response from the API body
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
    final id = 'r${++_msgCounter}';
    setState(() {
      _messages.add(ChatMessage(
        id: id, role: MessageRole.rhodey, text: text,
        timestamp: DateTime.now(),
      ));
    });
    _scrollToBottom();
  }

  // ── Voice pipeline ────────────────────────────────────────────

  void _toggleVoice() {
    if (_voiceState == VoiceState.idle) {
      setState(() => _voiceState = VoiceState.listening);
      Future.delayed(const Duration(seconds: 2), () {
        if (!mounted) return;
        setState(() {
          _voiceState = VoiceState.transcribing;
          _transcribedText = 'Equisoft wants phase 2 at 2.4L. Follow up with Marcus.';
        });
        Future.delayed(const Duration(seconds: 1), () {
          if (!mounted) return;
          setState(() => _voiceState = VoiceState.confirm);
        });
      });
    } else {
      setState(() {
        _voiceState = VoiceState.idle;
        _transcribedText = null;
      });
    }
  }

  void _confirmVoice(String type) {
    if (_transcribedText != null) {
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
            child: _loading
                ? const Center(child: CircularProgressIndicator())
                : RefreshIndicator(
                    onRefresh: _loadHistory,
                    color: AppTheme.accent,
                    child: ListView.builder(
                      controller: _scrollController,
                      padding: const EdgeInsets.only(top: 8, bottom: 8),
                      itemCount: _messages.length,
                      itemBuilder: (context, index) {
                        final msg = _messages[index];
                        final isGroupStart = index == 0 ||
                            _messages[index - 1].role != msg.role;
                        return ChatBubble(
                          message: msg,
                          isGroupStart: isGroupStart,
                          onRetry: msg.isFailed ? () => _retryMessage(msg.id) : null,
                        );
                      },
                    ),
                  ),
          ),
          if (_voiceState != VoiceState.idle)
            VoiceStateMachine(
              state: _voiceState,
              transcribedText: _transcribedText,
              onCancel: _toggleVoice,
              onTaskConfirm: () => _confirmVoice('task'),
              onNoteConfirm: () => _confirmVoice('note'),
              onRetry: () => _toggleVoice(),
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
                  onTap: () => Navigator.pop(context)),
              _MenuTile(icon: Icons.memory_outlined, label: 'Memories',
                  onTap: () => Navigator.pop(context)),
              _MenuTile(icon: Icons.settings_outlined, label: 'API Settings',
                  onTap: () {
                    Navigator.pop(context);
                    Navigator.push(context,
                      MaterialPageRoute(builder: (_) => const _SettingsScreen()));
                  }),
              _MenuTile(icon: Icons.person_outline, label: 'Profile',
                  onTap: () => Navigator.pop(context)),
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
          // Status indicator
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
