import 'dart:async';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:flutter_tts/flutter_tts.dart';
import '../services/api_service.dart';
import '../services/notification_service.dart';
import 'menu_sheet.dart';
import 'today_screen.dart';
import 'inbox_screen.dart';

// ─────────────────────────────────────────────────────────────────────────────
//  Rhodey Surface — Production
//  ─────────────────────────────────────────────────────────────────────────────
//  Layout:
//    Fixed presence strip (top) → ● Rhodey
//    Scrollable surface (middle) → items with 3 visual weights
//    Fixed bottom dock (bottom) → [≡]  [🎤 Tap to speak]  [⌨︎]
//
//  Data sources:
//    - ApiService.getMessages()     → user captures + Rhodey responses
//    - ApiService.getPendingDecisions() → structured decision cards
//    - ApiService.getCalendarEvents() + getTasks() → greeting context
//
//  Actions:
//    - ApiService.sendMessage()     → send text/voice to Rhodey
//    - ApiService.approve*()        → approve decisions inline
//    - ApiService.reject*()         → reject decisions inline
// ─────────────────────────────────────────────────────────────────────────────

// ── Item types that can appear on the surface ────────────────────────────────

enum SurfaceItemType {
  greeting,           // Rhodey's headline greeting (always at bottom)
  userCapture,        // Your message — muted, icon-led
  rhodeyResponse,     // Rhodey's reply — primary weight, no icon
  structuredDecision, // Decision card — thin card with accent + chips
  chronology,         // Time marker — faint, centered
  historyHint,        // "scroll up for older" — disappears after first scroll
  starterChips,       // Blank-state suggestion chips
}

// ── Internal surface item model ─────────────────────────────────────────────

class _SurfaceItem {
  final String id;
  SurfaceItemType type;
  String text;
  String? icon;
  String? subtitle;
  List<String>? chips;
  bool isUrgent;
  DateTime timestamp;
  PendingDecision? decision; // Reference for structured decisions

  _SurfaceItem({
    required this.id,
    required this.type,
    required this.text,
    this.icon,
    this.subtitle,
    this.chips,
    this.isUrgent = false,
    required this.timestamp,
    this.decision,
  });
}

// ── Main widget ─────────────────────────────────────────────────────────────

class RhodeySurface extends StatefulWidget {
  const RhodeySurface({super.key});

  @override
  State<RhodeySurface> createState() => _RhodeySurfaceState();
}

class _RhodeySurfaceState extends State<RhodeySurface>
    with TickerProviderStateMixin {
  // ── Data ──
  final List<_SurfaceItem> _items = [];
  final _scrollController = ScrollController();
  int _idSeq = 0;

  // ── State ──
  bool _loading = true;
  bool _hasScrolledOnce = false;
  bool _showHistoryHint = true;
  bool _isListening = false;
  bool _isTyping = false;
  String? _expandedDecisionId;
  String? _selectedDecisionChip;

  // ── Services ──
  final _api = ApiService();
  final _textController = TextEditingController();
  final _typeFocus = FocusNode();
  Timer? _pollTimer;
  final _seenMessageIds = <String>{};

  // ── Speech & TTS ──
  final stt.SpeechToText _speech = stt.SpeechToText();
  bool _speechAvailable = false;
  final FlutterTts _tts = FlutterTts();
  Timer? _voiceTimeout;

  // ── Animations ──
  late AnimationController _pulseController;
  late Animation<double> _pulseAnimation;

  // ── Visual constants ──
  static const Color _surfaceBg = Color(0xFF0E0E10);
  static const Color _primaryText = Color(0xFFF2F2F2);
  static const Color _mutedText = Color(0xFF6B6B70);
  static const Color _accentGreen = Color(0xFF34C759);
  static const Color _accentAmber = Color(0xFFFFD60A);
  static const Color _accentBlue = Color(0xFF007AFF);
  static const Color _cardBorder = Color(0xFF2C2C30);
  static const Color _dockBg = Color(0xFF161618);

  @override
  void initState() {
    super.initState();

    // Pulse animation for the presence dot
    _pulseController = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 2),
    )..repeat(reverse: true);
    _pulseAnimation = Tween<double>(begin: 0.4, end: 1.0).animate(
      CurvedAnimation(parent: _pulseController, curve: Curves.easeInOut),
    );

    // Init TTS
    _tts.setLanguage('en-US');
    _tts.setSpeechRate(0.5);

    // Init speech recognition
    _initSpeech();

    // Load initial data
    _loadInitialData();

    // Start polling for updates
    _startPolling();

    // Register push notification tap handler
    NotificationService.onNotificationOpened = _handlePushNotificationTap;

    // Cold-start: check if app was launched via notification tap
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
    _pulseController.dispose();
    _pollTimer?.cancel();
    _voiceTimeout?.cancel();
    _textController.dispose();
    _typeFocus.dispose();
    _scrollController.dispose();
    if (NotificationService.onNotificationOpened == _handlePushNotificationTap) {
      NotificationService.onNotificationOpened = null;
    }
    super.dispose();
  }

  // ── Data loading ──────────────────────────────────────────────────────────

  Future<void> _loadInitialData() async {
    // Load all data in parallel
    final messagesFut = _api.getMessages(limit: 50);
    final decisionsFut = _api.getPendingDecisions();
    final eventsFut = _api.getCalendarEvents();
    final tasksFut = _api.getTasks();

    final messagesResult = await messagesFut;
    final decisionsResult = await decisionsFut;
    final eventsResult = await eventsFut;
    final tasksResult = await tasksFut;

    if (!mounted) return;

    final now = DateTime.now();
    final items = <_SurfaceItem>[];
    String? lastDecisionId;

    // ── Decisions first (they appear at their timestamp or near the bottom) ──
    if (decisionsResult.success) {
      for (final pd in decisionsResult.data!) {
        final rawCreated = pd.raw['created_at'] as String?;
        final ts = rawCreated != null
            ? (DateTime.tryParse(rawCreated) ?? now)
            : now;

        final itemId = 'dec_${pd.id}';
        lastDecisionId = itemId; // Track the last (most recent) decision

        items.add(_SurfaceItem(
          id: itemId,
          type: SurfaceItemType.structuredDecision,
          text: pd.title,
          subtitle: pd.description,
          chips: _chipsForSource(pd.source),
          isUrgent: _isUrgentDecision(pd),
          timestamp: ts,
          decision: pd,
        ));
      }
    }

    // ── Messages ────────────────────────────────────────────────────────────
    if (messagesResult.success && messagesResult.data!.isNotEmpty) {
      for (final m in messagesResult.data!) {
        final id = m['id'].toString();
        final content = m['content'] as String? ?? '';
        if (content.isEmpty) continue;

        _seenMessageIds.add(id);

        final direction = m['direction'] as String? ?? '';
        final createdAt = m['created_at'] as String? ?? '';
        final ts = createdAt.isNotEmpty
            ? (DateTime.tryParse(createdAt) ?? now)
            : now;
        final isUser = direction == 'inbound';

        items.add(_SurfaceItem(
          id: 'msg_${id}_${_idSeq++}',
          type: isUser ? SurfaceItemType.userCapture : SurfaceItemType.rhodeyResponse,
          text: content,
          icon: isUser ? '🗣️' : null,
          timestamp: ts,
        ));
      }
    }

    // ── Sort by timestamp (oldest first) ────────────────────────────────────
    items.sort((a, b) => a.timestamp.compareTo(b.timestamp));

    // ── Insert chronology markers ──────────────────────────────────────────
    final sortedItems = <_SurfaceItem>[];
    String? lastDateLabel;
    for (final item in items) {
      final dateLabel = _dateLabel(item.timestamp);
      if (dateLabel != lastDateLabel) {
        sortedItems.add(_SurfaceItem(
          id: 'chrono_${_idSeq++}',
          type: SurfaceItemType.chronology,
          text: dateLabel,
          timestamp: item.timestamp,
        ));
        lastDateLabel = dateLabel;
      }
      sortedItems.add(item);
    }

    // ── Show history hint if we have any items above the greeting ──────────
    if (sortedItems.isNotEmpty) {
      _showHistoryHint = true;
    }

    _items.addAll(sortedItems);

    // ── Generate greeting from context ──────────────────────────────────────
    final greeting = await _generateGreeting(
      decisionsResult.data ?? [],
      eventsResult,
      tasksResult,
    );

    if (!mounted) return;

    _items.add(_SurfaceItem(
      id: 'greeting_${_idSeq++}',
      type: SurfaceItemType.greeting,
      text: greeting,
      timestamp: DateTime.now(),
    ));

    setState(() {
      _loading = false;
      // Expand the most recent decision card (last item added before sorting)
      if (lastDecisionId != null) {
        _expandedDecisionId = lastDecisionId;
      }
    });

    // Scroll to bottom on next frame
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _scrollToBottom();
    });
  }

  String _dateLabel(DateTime dt) {
    final now = DateTime.now();
    if (dt.year == now.year && dt.month == now.month && dt.day == now.day) {
      return '─ ${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')} ──';
    }
    if (dt.year == now.year && dt.month == now.month && dt.day == now.day - 1) {
      return '─ yesterday ──';
    }
    final months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return '─ ${months[dt.month - 1]} ${dt.day} ──';
  }

  Future<String> _generateGreeting(
    List<PendingDecision> decisions,
    ApiResult<List<CalendarEventItem>> eventsResult,
    ApiResult<List<Map<String, dynamic>>> tasksResult,
  ) async {
    final isEvening = DateTime.now().hour >= 17;
    final greeting = isEvening ? 'Good evening' : 'Good morning';

    final pendingCount = decisions.length;
    final parts = <String>[greeting];

    if (pendingCount > 0) {
      parts.add('$pendingCount thing${pendingCount != 1 ? 's' : ''} need${pendingCount == 1 ? 's' : ''} your attention.');
    }

    // Next calendar event
    if (eventsResult.success && eventsResult.data!.isNotEmpty) {
      final next = eventsResult.data!.first;
      parts.add('Next: ${next.title} at ${next.timeRange}.');
    }

    // Urgent tasks
    if (tasksResult.success) {
      final urgent = tasksResult.data!
          .where((t) => t['deadline'] != null && _isDeadlineUrgent(t['deadline'] as String))
          .take(2)
          .toList();
      if (urgent.isNotEmpty) {
        final titles = urgent.map((t) => t['title'] as String).join(', ');
        parts.add('${urgent.length == 1 ? 'Task due' : 'Tasks due'}: $titles.');
      }
    }

    return parts.join(' ');
  }

  bool _isDeadlineUrgent(String deadline) {
    try {
      return DateTime.parse(deadline).toLocal().isBefore(
          DateTime.now().add(const Duration(hours: 24)));
    } catch (_) {
      return false;
    }
  }

  bool _isUrgentDecision(PendingDecision d) {
    return d.source == 'graph_edge' || d.source == 'graph_node';
  }

  List<String>? _chipsForSource(String source) {
    switch (source) {
      case 'graph_node':
        return ['Approve', 'Edit name', 'Dismiss'];
      case 'graph_edge':
        return ['Approve', 'Edit', 'Dismiss'];
      case 'email':
        return ['Create task', 'Dismiss'];
      case 'whatsapp':
        return ['Create task', 'Dismiss'];
      case 'call':
        return ['Create task', 'Dismiss'];
      default:
        return ['Approve', 'Dismiss'];
    }
  }

  // ── Polling ───────────────────────────────────────────────────────────────

  void _startPolling() {
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(
        const Duration(seconds: 8), (_) => _pollForUpdates());
  }

  void _stopPolling() {
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  Future<void> _pollForUpdates() async {
    final messagesResult = await _api.getMessages(limit: 5);
    final decisionsResult = await _api.getPendingDecisions();
    if (!mounted) return;

    bool changed = false;

    // Check for new messages
    if (messagesResult.success) {
      for (final m in messagesResult.data!) {
        final id = m['id'].toString();
        if (_seenMessageIds.contains(id)) continue;
        _seenMessageIds.add(id);

        final content = m['content'] as String? ?? '';
        if (content.isEmpty) continue;

        final direction = m['direction'] as String? ?? '';
        final createdAt = m['created_at'] as String? ?? '';
        final ts = createdAt.isNotEmpty
            ? (DateTime.tryParse(createdAt) ?? DateTime.now())
            : DateTime.now();
        final isUser = direction == 'inbound';

        setState(() {
          _items.insert(
            _items.length - 1, // Insert before the greeting (which is always last)
            _SurfaceItem(
              id: 'msg_${id}_${_idSeq++}',
              type: isUser
                  ? SurfaceItemType.userCapture
                  : SurfaceItemType.rhodeyResponse,
              text: content,
              icon: isUser ? '🗣️' : null,
              timestamp: ts,
            ),
          );
        });

        // Speak Rhodey responses aloud
        if (!isUser && content.isNotEmpty) {
          _tts.stop();
          _tts.speak(content);
        }

        changed = true;

        // Also add chronology marker if enough time has passed
        _insertChronologyForNewItem(ts);
      }
    }

    // Check for new decisions
    if (decisionsResult.success) {
      final existingDecisionIds =
          _items.where((i) => i.decision != null).map((i) => i.decision!.id).toSet();
      for (final pd in decisionsResult.data!) {
        if (existingDecisionIds.contains(pd.id)) continue;

        final rawCreated = pd.raw['created_at'] as String?;
        final ts = rawCreated != null
            ? (DateTime.tryParse(rawCreated) ?? DateTime.now())
            : DateTime.now();

        final itemId = 'dec_${pd.id}';

        setState(() {
          _items.insert(
            _items.length - 1,
            _SurfaceItem(
              id: itemId,
              type: SurfaceItemType.structuredDecision,
              text: pd.title,
              subtitle: pd.description,
              chips: _chipsForSource(pd.source),
              isUrgent: _isUrgentDecision(pd),
              timestamp: ts,
              decision: pd,
            ),
          );
          // Expand the newly arrived decision card
          _expandedDecisionId = itemId;
        });

        changed = true;
      }
    }

    if (changed) {
      Future.delayed(const Duration(milliseconds: 50), _scrollToBottom);
    }
  }

  void _insertChronologyForNewItem(DateTime ts) {
    // Check if the last item already has a chronology marker
    final lastChronoIdx = _items.lastIndexWhere(
        (i) => i.type == SurfaceItemType.chronology && i != _items.last);
    if (lastChronoIdx >= 0) {
      final lastChrono = _items[lastChronoIdx];
      final diff = ts.difference(lastChrono.timestamp);
      if (diff.inMinutes < 30) return; // No marker if within 30 min
    }

    final dateLabel = _dateLabel(ts);
    setState(() {
      _items.insert(
        _items.length - 1,
        _SurfaceItem(
          id: 'chrono_${_idSeq++}',
          type: SurfaceItemType.chronology,
          text: dateLabel,
          timestamp: ts,
        ),
      );
    });
  }

  // ── Send message ──────────────────────────────────────────────────────────

  void _sendMessage(String text) {
    if (text.trim().isEmpty) return;

    // Add user message to surface immediately
    setState(() {
      _items.insert(
        _items.length - 1, // Before greeting
        _SurfaceItem(
          id: 'user_${_idSeq++}',
          type: SurfaceItemType.userCapture,
          text: text.trim(),
          icon: '🗣️',
          timestamp: DateTime.now(),
        ),
      );
    });
    _textController.clear();
    _scrollToBottom();
    _startPolling();

    // Send to API
    _api.sendMessage(text.trim()).then((result) {
      if (!mounted) return;
      if (result.success) {
        String responseText;
        if (result.data is Map) {
          responseText = (result.data as Map)['response'] as String? ??
              'Got it, working on it.';
        } else {
          responseText = 'Got it, working on it.';
        }

        Future.delayed(const Duration(milliseconds: 500), () {
          if (!mounted) return;
          _addRhodeyResponse(responseText);
        });
      }
    });
  }

  void _addRhodeyResponse(String text) {
    _stopPolling();

    setState(() {
      _items.insert(
        _items.length - 1,
        _SurfaceItem(
          id: 'rhodey_${_idSeq++}',
          type: SurfaceItemType.rhodeyResponse,
          text: text,
          timestamp: DateTime.now(),
        ),
      );
    });

    _scrollToBottom();

    // Speak it
    _tts.stop();
    _tts.speak(text);
  }

  // ── Decision actions ──────────────────────────────────────────────────────

  Future<void> _handleDecisionAction(_SurfaceItem item, String action) async {
    final decision = item.decision;
    if (decision == null) return;

    setState(() {
      _selectedDecisionChip = action;
      _expandedDecisionId = null;
    });

    final source = decision.source;
    final pendingId = int.tryParse(decision.id);

    if (pendingId == null) {
      setState(() => _items.remove(item));
      return;
    }

    ApiResult<dynamic>? result;

    if (action == 'Approve' || action == 'Create task') {
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
    } else if (action == 'Dismiss') {
      switch (source) {
        case 'graph_node':
          result = await _api.rejectGraphNode(pendingId);
          break;
        case 'graph_edge':
          result = await _api.rejectGraphEdge(pendingId);
          break;
        case 'email':
          result = await _api.rejectEmail(pendingId);
          break;
        case 'whatsapp':
          result = await _api.rejectWhatsApp(pendingId);
          break;
        case 'call':
          result = await _api.rejectCall(pendingId);
          break;
      }
    }

    if (!mounted) return;

    Future.delayed(const Duration(milliseconds: 350), () {
      if (!mounted) return;

      if (action == 'Dismiss') {
        // Remove the item entirely
        setState(() {
          _items.remove(item);
          _selectedDecisionChip = null;
        });
      } else if (result != null && result.success) {
        // Replace with confirmation
        setState(() {
          item.type = SurfaceItemType.rhodeyResponse;
          item.text = '✅ ${action == 'Approve' ? 'Approved' : 'Created'} — ${decision.title}';
          item.chips = null;
          item.isUrgent = false;
          item.decision = null;
          _selectedDecisionChip = null;
        });
      } else {
        // Failed — show the card again
        setState(() {
          _expandedDecisionId = item.id;
          _selectedDecisionChip = null;
        });
        if (result != null) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Text(
                result.error ?? 'Action failed',
                style: const TextStyle(fontSize: 12),
              ),
              backgroundColor: const Color(0xFFEF5350),
              duration: const Duration(seconds: 2),
            ),
          );
        }
      }
    });
  }

  // ── Voice ─────────────────────────────────────────────────────────────────

  Future<void> _initSpeech() async {
    _speechAvailable = await _speech.initialize(
      onError: (error) {
        if (!mounted) return;
        debugPrint('[Voice] Error: ${error.errorMsg}');
        if (error.errorMsg == 'error_speech_timeout') {
          setState(() => _isListening = false);
          return;
        }
        setState(() => _isListening = false);
      },
      onStatus: (status) {
        debugPrint('[Voice] Status: $status');
      },
    );
  }

  void _onMicTap() {
    if (_isListening) {
      _stopListening();
      return;
    }
    _startListening();
  }

  Future<void> _startListening() async {
    if (!_speechAvailable) {
      _speechAvailable = await _speech.initialize();
      if (!_speechAvailable) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text('Speech recognition not available',
                  style: TextStyle(fontSize: 12)),
              duration: Duration(seconds: 2),
            ),
          );
        }
        return;
      }
    }

    if (!mounted) return;
    setState(() => _isListening = true);

    // Auto-stop after 15s if no result
    _voiceTimeout?.cancel();
    _voiceTimeout = Timer(const Duration(seconds: 15), () {
      _stopListening();
    });

    await _speech.listen(
      onResult: (result) {
        if (!mounted) return;
        if (result.finalResult) {
          final words = result.recognizedWords;
          if (words.isNotEmpty) {
            _voiceTimeout?.cancel();
            setState(() => _isListening = false);
            _sendMessage(words);
          }
        }
      },
      listenOptions: stt.SpeechListenOptions(
        listenFor: const Duration(seconds: 30),
        pauseFor: const Duration(seconds: 3),
        partialResults: true,
        cancelOnError: false,
      ),
    );
  }

  Future<void> _stopListening() async {
    _voiceTimeout?.cancel();
    await _speech.stop();
    if (!mounted) return;
    setState(() => _isListening = false);
  }

  // ── TTS on tap ────────────────────────────────────────────────────────────

  void _speakMessage(String text) {
    if (text.trim().isEmpty) return;
    _tts.stop();
    _tts.speak(text.trim());
  }

  // ── Notification handling ─────────────────────────────────────────────────

  void _handlePushNotificationTap(Map<String, dynamic> data) {
    final type = data['type'];
    debugPrint('[PushNav] Notification type=$type');

    switch (type) {
      case 'decision':
      case 'delegation':
        Navigator.push(
          context,
          MaterialPageRoute(builder: (_) => const InboxScreen()),
        );
        break;
      case 'nudge':
        Navigator.push(
          context,
          MaterialPageRoute(builder: (_) => const TodayScreen()),
        );
        break;
      case 'briefing':
      default:
        break;
    }
  }

  // ── Menu ──────────────────────────────────────────────────────────────────

  void _openMenu() {
    showMenuSheet(context);
  }

  // ── Scroll ────────────────────────────────────────────────────────────────

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

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    // Keyboard handling
    if (_isTyping) {
      return Scaffold(
        backgroundColor: _surfaceBg,
        body: SafeArea(
          child: Column(
            children: [
              _buildPresenceStrip(),
              Expanded(child: _buildSurfaceList()),
              _buildTypeBar(),
            ],
          ),
        ),
      );
    }

    return Scaffold(
      backgroundColor: _surfaceBg,
      body: SafeArea(
        child: Column(
          children: [
            _buildPresenceStrip(),
            Expanded(child: _buildSurfaceList()),
            _buildBottomDock(),
          ],
        ),
      ),
    );
  }

  // ── Presence strip ────────────────────────────────────────────────────────

  Widget _buildPresenceStrip() {
    return Container(
      height: 44,
      padding: const EdgeInsets.symmetric(horizontal: 16),
      alignment: Alignment.centerLeft,
      decoration: BoxDecoration(
        border: Border(
          bottom: BorderSide(color: _cardBorder.withValues(alpha: 0.5)),
        ),
      ),
      child: Row(
        children: [
          AnimatedBuilder(
            animation: _pulseAnimation,
            builder: (_, child) {
              final isActive = _isListening || _selectedDecisionChip != null;
              return Container(
                width: 8,
                height: 8,
                decoration: BoxDecoration(
                  color: isActive
                      ? _accentGreen
                      : _accentGreen.withValues(alpha: _pulseAnimation.value),
                  shape: BoxShape.circle,
                ),
              );
            },
          ),
          const SizedBox(width: 8),
          Text(
            'Rhodey',
            style: TextStyle(
              color: _mutedText,
              fontSize: 12,
              fontWeight: FontWeight.w500,
              letterSpacing: 0.3,
            ),
          ),
          if (_isListening) ...[
            const SizedBox(width: 8),
            const _ListeningIndicator(),
          ],
        ],
      ),
    );
  }

  // ── Surface list ──────────────────────────────────────────────────────────

  Widget _buildSurfaceList() {
    if (_loading) {
      return const Center(
        child: SizedBox(
          width: 20,
          height: 20,
          child: CircularProgressIndicator(
            strokeWidth: 2,
            color: Color(0xFF6B6B70),
          ),
        ),
      );
    }

    if (_items.isEmpty) {
      return _buildBlankState();
    }

    return NotificationListener<ScrollNotification>(
      onNotification: (notification) {
        if (notification is ScrollUpdateNotification && !_hasScrolledOnce &&
            notification.metrics.pixels > 20) {
          setState(() {
            _hasScrolledOnce = true;
            _showHistoryHint = false;
          });
        }
        return false;
      },
      child: ListView.builder(
        controller: _scrollController,
        padding: const EdgeInsets.only(top: 8, bottom: 16),
        itemCount: _items.length + (_showHistoryHint ? 1 : 0),
        itemBuilder: (context, index) {
          // History hint at the top (item 0)
          if (_showHistoryHint && index == 0) {
            return _buildHistoryHint();
          }
          final itemIndex = _showHistoryHint ? index - 1 : index;
          if (itemIndex >= _items.length) return const SizedBox();
          return _buildItem(_items[itemIndex]);
        },
      ),
    );
  }

  // ── Blank state ───────────────────────────────────────────────────────────

  Widget _buildBlankState() {
    return ListView(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
      children: [
        const SizedBox(height: 40),
        Text(
          'Hey, I\'m your companion.\n'
          'I\'ll keep track of tasks, people, projects,\n'
          'and anything you throw at me.',
          style: TextStyle(
            color: _primaryText,
            fontSize: 15,
            height: 1.5,
            fontWeight: FontWeight.w400,
          ),
        ),
        const SizedBox(height: 24),
        Text(
          'To start, just speak or type\nwhatever\'s on your mind.',
          style: TextStyle(
            color: _mutedText,
            fontSize: 13,
            height: 1.4,
          ),
        ),
        const SizedBox(height: 20),
        _starterChip('📝  "Remind me to call Sunju"', () {
          _sendMessage('Remind me to call Sunju about school');
        }),
        const SizedBox(height: 8),
        _starterChip('🗣️  "What\'s new today?"', () {
          _sendMessage("What's new today?");
        }),
        const SizedBox(height: 8),
        _starterChip('📝  "Note down an idea"', () {
          _sendMessage('Note down: explore AI-powered meeting summaries for Qhord');
        }),
        const SizedBox(height: 32),
        Text(
          '(nothing yet — your surface\n will fill as we talk)',
          textAlign: TextAlign.center,
          style: TextStyle(
            color: _mutedText.withValues(alpha: 0.6),
            fontSize: 11,
            fontStyle: FontStyle.italic,
          ),
        ),
      ],
    );
  }

  Widget _starterChip(String label, VoidCallback onTap) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: onTap,
        child: Container(
          width: double.infinity,
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
          decoration: BoxDecoration(
            border: Border.all(color: _cardBorder),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Text(
            label,
            style: TextStyle(color: _mutedText, fontSize: 13),
          ),
        ),
      ),
    );
  }

  // ── History hint ──────────────────────────────────────────────────────────

  Widget _buildHistoryHint() {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 12),
      child: Center(
        child: Text(
          'scroll up for older',
          style: TextStyle(
            color: _mutedText.withValues(alpha: 0.45),
            fontSize: 11,
          ),
        ),
      ),
    );
  }

  // ── Item renderer ─────────────────────────────────────────────────────────

  Widget _buildItem(_SurfaceItem item) {
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 0.0, end: 1.0),
      duration: const Duration(milliseconds: 250),
      curve: Curves.easeOut,
      builder: (context, value, child) {
        return Opacity(
          opacity: value,
          child: Transform.translate(
            offset: Offset(0, 16 * (1.0 - value)),
            child: child,
          ),
        );
      },
      child: _buildItemContent(item),
    );
  }

  Widget _buildItemContent(_SurfaceItem item) {
    switch (item.type) {
      case SurfaceItemType.greeting:
        return _buildGreeting(item);
      case SurfaceItemType.userCapture:
        return _buildUserCapture(item);
      case SurfaceItemType.rhodeyResponse:
        return _buildRhodeyResponse(item);
      case SurfaceItemType.structuredDecision:
        return _buildDecisionCard(item);
      case SurfaceItemType.chronology:
        return _buildChronology(item);
      case SurfaceItemType.historyHint:
      case SurfaceItemType.starterChips:
        return const SizedBox();
    }
  }

  // ── Greeting ──────────────────────────────────────────────────────────────

  Widget _buildGreeting(_SurfaceItem item) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 16, 20, 8),
      child: Text(
        item.text,
        style: const TextStyle(
          color: _primaryText,
          fontSize: 15,
          height: 1.5,
          fontWeight: FontWeight.w400,
        ),
      ),
    );
  }

  // ── User capture — muted, icon-led ────────────────────────────────────────

  Widget _buildUserCapture(_SurfaceItem item) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 8, 48, 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            item.icon ?? '📝',
            style: const TextStyle(fontSize: 12),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              item.text,
              style: TextStyle(
                color: _mutedText,
                fontSize: 13,
                height: 1.4,
                fontWeight: FontWeight.w300,
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ── Rhodey response — primary weight, no icon ─────────────────────────────

  Widget _buildRhodeyResponse(_SurfaceItem item) {
    // Wrapped in GestureDetector for TTS on tap
    return GestureDetector(
      onTap: () => _speakMessage(item.text),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 6, 20, 6),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Text(
                item.text,
                style: const TextStyle(
                  color: _primaryText,
                  fontSize: 14,
                  height: 1.5,
                  fontWeight: FontWeight.w400,
                ),
              ),
            ),
            const SizedBox(width: 4),
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Icon(
                Icons.volume_up_outlined,
                size: 10,
                color: _mutedText.withValues(alpha: 0.35),
              ),
            ),
          ],
        ),
      ),
    );
  }

  // ── Decision card ─────────────────────────────────────────────────────────

  Widget _buildDecisionCard(_SurfaceItem item) {
    final accentColor = item.isUrgent ? _accentAmber : _accentBlue;
    final isExpanded = _expandedDecisionId == item.id;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: AnimatedSize(
        duration: const Duration(milliseconds: 300),
        curve: Curves.easeOut,
        alignment: Alignment.topCenter,
        child: AnimatedOpacity(
          duration: const Duration(milliseconds: 200),
          opacity: isExpanded ? 1.0 : 0.0,
          child: isExpanded
              ? Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    border: Border.all(
                        color: _cardBorder.withValues(alpha: 0.7)),
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      // Left accent bar indicator
                      Container(
                        width: 3,
                        height: 16,
                        margin: const EdgeInsets.only(bottom: 8),
                        decoration: BoxDecoration(
                          color: accentColor,
                          borderRadius: BorderRadius.circular(2),
                        ),
                      ),
                      // Icon + text
                      Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            item.isUrgent ? '⚠️' : '🔗',
                            style: const TextStyle(fontSize: 14),
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  item.text,
                                  style: const TextStyle(
                                    color: _primaryText,
                                    fontSize: 13,
                                    height: 1.4,
                                    fontWeight: FontWeight.w500,
                                  ),
                                ),
                                if (item.subtitle != null) ...[
                                  const SizedBox(height: 4),
                                  Text(
                                    item.subtitle!,
                                    style: TextStyle(
                                      color: _mutedText,
                                      fontSize: 12,
                                      height: 1.3,
                                    ),
                                  ),
                                ],
                              ],
                            ),
                          ),
                        ],
                      ),
                      // Action chips
                      if (item.chips != null) ...[
                        const SizedBox(height: 12),
                        Wrap(
                          spacing: 8,
                          runSpacing: 6,
                          children: item.chips!.map((chip) {
                            return _ActionChip(
                              label: chip,
                              accent: chip == 'Approve' || chip == 'Create task'
                                  ? _accentGreen
                                  : chip == 'Dismiss' || chip == 'Skip'
                                      ? _mutedText
                                      : _accentBlue,
                              onTap: () => _handleDecisionAction(item, chip),
                            );
                          }).toList(),
                        ),
                      ],
                    ],
                  ),
                )
              : const SizedBox(height: 0),
        ),
      ),
    );
  }

  // ── Chronology marker ─────────────────────────────────────────────────────

  Widget _buildChronology(_SurfaceItem item) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Center(
        child: Text(
          item.text,
          style: TextStyle(
            color: _mutedText.withValues(alpha: 0.35),
            fontSize: 10,
            letterSpacing: 0.5,
          ),
        ),
      ),
    );
  }

  // ── Bottom dock (default) ─────────────────────────────────────────────────

  Widget _buildBottomDock() {
    return Container(
      height: 56,
      padding: const EdgeInsets.symmetric(horizontal: 12),
      decoration: BoxDecoration(
        color: _dockBg,
        border: Border(
          top: BorderSide(color: _cardBorder.withValues(alpha: 0.5)),
        ),
      ),
      child: Row(
        children: [
          // Menu
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(8),
              onTap: _openMenu,
              child: Container(
                padding: const EdgeInsets.all(10),
                child: Icon(Icons.menu, color: _mutedText, size: 20),
              ),
            ),
          ),

          const Spacer(),

          // Primary: Tap to speak
          Material(
            color: _isListening
                ? _accentGreen.withValues(alpha: 0.15)
                : Colors.transparent,
            borderRadius: BorderRadius.circular(20),
            child: InkWell(
              borderRadius: BorderRadius.circular(20),
              onTap: _onMicTap,
              child: Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(20),
                  border: Border.all(
                    color: _isListening
                        ? _accentGreen.withValues(alpha: 0.5)
                        : _cardBorder,
                  ),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      _isListening ? '🎤 Listening...' : '🎤  Tap to speak',
                      style: TextStyle(
                        color: _isListening ? _accentGreen : _mutedText,
                        fontSize: 13,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),

          const Spacer(),

          // Keyboard
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(8),
              onTap: () => setState(() => _isTyping = true),
              child: Container(
                padding: const EdgeInsets.all(10),
                child: Icon(Icons.keyboard_outlined,
                    color: _mutedText, size: 20),
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ── Type bar (when keyboard is active) ────────────────────────────────────

  Widget _buildTypeBar() {
    return Container(
      padding: const EdgeInsets.fromLTRB(12, 6, 12, 12),
      decoration: BoxDecoration(
        color: _dockBg,
        border: Border(
          top: BorderSide(color: _cardBorder.withValues(alpha: 0.5)),
        ),
      ),
      child: SafeArea(
        top: false,
        child: Row(
          children: [
            Expanded(
              child: Container(
                decoration: BoxDecoration(
                  color: _surfaceBg,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: _cardBorder),
                ),
                child: TextField(
                  controller: _textController,
                  focusNode: _typeFocus,
                  autofocus: true,
                  textInputAction: TextInputAction.send,
                  onSubmitted: (value) {
                    if (value.trim().isEmpty) return;
                    _sendMessage(value.trim());
                    setState(() => _isTyping = false);
                  },
                  decoration: const InputDecoration(
                    hintText: 'Type a message...',
                    border: InputBorder.none,
                    contentPadding:
                        EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                    isDense: true,
                  ),
                  style: const TextStyle(
                      color: Color(0xFFF2F2F2), fontSize: 14),
                ),
              ),
            ),
            const SizedBox(width: 6),
            // Send
            Material(
              color: _accentBlue,
              borderRadius: BorderRadius.circular(10),
              child: InkWell(
                borderRadius: BorderRadius.circular(10),
                onTap: () {
                  final value = _textController.text.trim();
                  if (value.isEmpty) return;
                  _sendMessage(value);
                  setState(() => _isTyping = false);
                },
                child: Container(
                  width: 36,
                  height: 36,
                  alignment: Alignment.center,
                  child: const Icon(Icons.arrow_upward,
                      color: Colors.white, size: 18),
                ),
              ),
            ),
            const SizedBox(width: 4),
            // Close keyboard
            Material(
              color: Colors.transparent,
              child: InkWell(
                borderRadius: BorderRadius.circular(8),
                onTap: () {
                  setState(() => _isTyping = false);
                },
                child: Container(
                  padding: const EdgeInsets.all(8),
                  child: Icon(Icons.close, color: _mutedText, size: 18),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Supporting widgets ──────────────────────────────────────────────────────

class _ListeningIndicator extends StatefulWidget {
  const _ListeningIndicator();

  @override
  State<_ListeningIndicator> createState() => _ListeningIndicatorState();
}

class _ListeningIndicatorState extends State<_ListeningIndicator>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    )..repeat();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (_, child) {
        return Row(
          mainAxisSize: MainAxisSize.min,
          children: List.generate(3, (i) {
            final phase = (_controller.value + i * 0.33) % 1.0;
            final height = 4.0 + 8.0 * (1.0 - (phase * 2 - 1).abs());
            return Padding(
              padding: const EdgeInsets.symmetric(horizontal: 1.5),
              child: Container(
                width: 3,
                height: height,
                decoration: BoxDecoration(
                  color: const Color(0xFF34C759)
                      .withValues(alpha: 0.6 + 0.4 * (1.0 - phase)),
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            );
          }),
        );
      },
    );
  }
}

class _ActionChip extends StatelessWidget {
  final String label;
  final Color accent;
  final VoidCallback onTap;

  const _ActionChip({
    required this.label,
    required this.accent,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(8),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 7),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(8),
            border: Border.all(
              color: accent.withValues(alpha: 0.4),
            ),
            color: accent.withValues(alpha: 0.08),
          ),
          child: Text(
            label,
            style: TextStyle(
              color: accent,
              fontSize: 12,
              fontWeight: FontWeight.w500,
            ),
          ),
        ),
      ),
    );
  }
}
