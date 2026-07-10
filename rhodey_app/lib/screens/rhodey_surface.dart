import 'dart:async';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:flutter_tts/flutter_tts.dart';
import '../services/api_service.dart';
import '../services/notification_service.dart';
import '../models/briefing.dart';
import 'menu_sheet.dart';
import 'today_screen.dart';
import 'inbox_screen.dart';

// ─────────────────────────────────────────────────────────────────────────────
//  Rhodey Surface — Briefing Edition
//  ─────────────────────────────────────────────────────────────────────────────
//  Not a chat log. Not a dashboard. A living briefing surface.
//
//  Layout:
//    Presence strip (top)
//    Briefing view (scrollable):
//      ─ Greeting + next event
//      ─ Section "Your morning/afternoon/evening" (tasks + calendar)
//      ─ Section "Decisions" (conditional — hidden when empty)
//      ─ Section "Recent" (max 3 items — outcomes + activity)
//    Response moment (transient overlay)
//    Bottom dock (menu, mic, keyboard)
// ─────────────────────────────────────────────────────────────────────────────

class RhodeySurface extends StatefulWidget {
  const RhodeySurface({super.key});

  @override
  State<RhodeySurface> createState() => _RhodeySurfaceState();
}

class _RhodeySurfaceState extends State<RhodeySurface>
    with TickerProviderStateMixin {
  // ── Data ──
  BriefingResponse _briefing = BriefingResponse.empty();
  bool _loading = true;
  bool _hasError = false;

  // ── Response moment ──
  String? _momentText;
  bool _showMoment = false;

  // ── State ──
  bool _isListening = false;
  bool _isTyping = false;

  // ── Services ──
  final _api = ApiService();
  final _textController = TextEditingController();
  final _typeFocus = FocusNode();
  Timer? _pollTimer;
  Timer? _momentTimer;

  // ── Speech & TTS ──
  final stt.SpeechToText _speech = stt.SpeechToText();
  bool _speechAvailable = false;
  final FlutterTts _tts = FlutterTts();
  Timer? _voiceTimeout;

  // ── Animations ──
  late AnimationController _pulseController;
  late Animation<double> _pulseAnimation;
  late AnimationController _momentController;

  // ── Visual constants ──
  static const Color _surfaceBg = Color(0xFF0E0E10);
  static const Color _cardBg = Color(0xFF161618);
  static const Color _primaryText = Color(0xFFF2F2F2);
  static const Color _mutedText = Color(0xFF6B6B70);
  static const Color _accentGreen = Color(0xFF34C759);
  static const Color _accentAmber = Color(0xFFFFD60A);
  static const Color _accentBlue = Color(0xFF007AFF);
  static const Color _accentRed = Color(0xFFEF5350);
  static const Color _cardBorder = Color(0xFF2C2C30);
  static const Color _dockBg = Color(0xFF161618);
  static const Color _sectionTitleColor = Color(0xFF8E8E93);

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

    // Moment animation
    _momentController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 400),
    );

    // Init TTS
    _tts.setLanguage('en-US');
    _tts.setSpeechRate(0.5);

    // Init speech recognition
    _initSpeech();

    // Load initial briefing
    _fetchBriefing();

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
    _momentController.dispose();
    _pollTimer?.cancel();
    _momentTimer?.cancel();
    _voiceTimeout?.cancel();
    _textController.dispose();
    _typeFocus.dispose();
    if (NotificationService.onNotificationOpened == _handlePushNotificationTap) {
      NotificationService.onNotificationOpened = null;
    }
    super.dispose();
  }

  // ── Data loading ──────────────────────────────────────────────────────────

  Future<void> _fetchBriefing() async {
    final briefing = await _api.getBriefing();
    if (!mounted) return;
    setState(() {
      _briefing = briefing;
      _loading = false;
      _hasError = briefing.sections.isEmpty;
    });
  }

  // ── Polling ───────────────────────────────────────────────────────────────

  void _startPolling() {
    _pollTimer?.cancel();
    _pollTimer =
        Timer.periodic(const Duration(seconds: 10), (_) => _pollForUpdates());
  }

  void _stopPolling() {
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  Future<void> _pollForUpdates() async {
    final briefing = await _api.getBriefing();
    if (!mounted) return;

    setState(() {
      _briefing = briefing;
    });
  }

  // ── Send message ──────────────────────────────────────────────────────────

  void _sendMessage(String text) {
    if (text.trim().isEmpty) return;

    _textController.clear();
    _stopPolling();

    // Show brief response moment
    _showResponseMoment('Processing...');

    _api.sendMessage(text.trim()).then((result) {
      if (!mounted) return;

      String responseText;
      BriefingResponse? briefingUpdate;

      if (result.success && result.data is Map) {
        final data = result.data as Map<String, dynamic>;
        responseText = data['response'] as String? ?? 'Got it.';
        briefingUpdate = data['briefing_update'] != null
            ? BriefingResponse.fromJson(
                data['briefing_update'] as Map<String, dynamic>)
            : null;
      } else {
        responseText = result.error ?? 'Something went wrong.';
      }

      // Update briefing if we got one, otherwise re-fetch
      if (briefingUpdate != null) {
        setState(() {
          _briefing = briefingUpdate!;
        });
      } else {
        _fetchBriefing();
      }

      // Speak the response aloud
      _tts.stop();
      _tts.speak(responseText);

      // Show response moment
      _showResponseMoment(responseText);

      // Fade moment as soon as briefing is updated (with min ~2s display)
      Future.delayed(const Duration(seconds: 2), () {
        if (!mounted) return;
        _hideResponseMoment();
        _startPolling();
      });
    });
  }

  void _showResponseMoment(String text) {
    setState(() {
      _momentText = text;
      _showMoment = true;
    });
    _momentController.forward(from: 0.0);
  }

  void _hideResponseMoment() {
    _momentController.reverse().then((_) {
      if (!mounted) return;
      setState(() {
        _showMoment = false;
        _momentText = null;
      });
    });
  }

  // ── Decision actions ──────────────────────────────────────────────────────

  Future<void> _handleDecisionAction(
      BriefingItem item, String action) async {
    if (!item.isDecision) return;

    _showResponseMoment('${action == 'approve' || action == 'accept' ? '✅' : '⏳'} $action...');

    final isApprove = action == 'approve' || action == 'accept';
    final pendingId = int.tryParse(item.decisionId ?? '');
    if (pendingId == null) {
      // Can't parse ID — just re-fetch
      await Future.delayed(const Duration(milliseconds: 300));
      if (!mounted) return;
      await _fetchBriefing();
      _dismissMomentAfterDelay();
      return;
    }

    ApiResult<dynamic>? result;

    switch (item.decisionType) {
      case 'graph_node':
        result = isApprove
            ? await _api.approveGraphNode(pendingId)
            : await _api.rejectGraphNode(pendingId);
        break;
      case 'graph_edge':
        result = isApprove
            ? await _api.approveGraphEdge(pendingId)
            : await _api.rejectGraphEdge(pendingId);
        break;
      case 'email':
        result = isApprove
            ? await _api.approveEmail(pendingId)
            : await _api.rejectEmail(pendingId);
        break;
      case 'whatsapp':
        result = isApprove
            ? await _api.approveWhatsApp(pendingId)
            : await _api.rejectWhatsApp(pendingId);
        break;
      case 'call':
        result = isApprove
            ? await _api.approveCall(pendingId)
            : await _api.rejectCall(pendingId);
        break;
      case 'merge':
        // Merge actions need a different endpoint — re-fetch for now
        result = null;
        break;
      default:
        result = null;
    }

    if (!mounted) return;

    // Re-fetch briefing regardless, so state is fresh
    await _fetchBriefing();

    if (result != null && !result.success) {
      _showResponseMoment(result.error ?? 'Action failed');
      _dismissMomentAfterDelay();
    } else {
      _dismissMomentAfterDelay();
    }
  }

  void _dismissMomentAfterDelay() {
    _momentTimer?.cancel();
    _momentTimer = Timer(const Duration(seconds: 2), () {
      _hideResponseMoment();
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

  // TTS is triggered on each send-message response. No tap-to-speak on briefing
  // items because that would add chat-like interaction to the surface.
  // (Keep the method for future use — e.g. tapping the greeting to hear it.)

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

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    if (_isTyping) {
      return Scaffold(
        backgroundColor: _surfaceBg,
        body: SafeArea(
          child: Column(
            children: [
              _buildPresenceStrip(),
              Expanded(child: _buildBriefingView()),
              _buildTypeBar(),
            ],
          ),
        ),
      );
    }

    return Scaffold(
      backgroundColor: _surfaceBg,
      body: SafeArea(
        child: Stack(
          children: [
            Column(
              children: [
                _buildPresenceStrip(),
                Expanded(child: _buildBriefingView()),
                _buildBottomDock(),
              ],
            ),
            // Response moment overlay
            if (_showMoment && _momentText != null) _buildResponseMoment(),
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
              return Container(
                width: 8,
                height: 8,
                decoration: BoxDecoration(
                  color: _accentGreen
                      .withValues(alpha: _pulseAnimation.value),
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

  // ── Briefing view ─────────────────────────────────────────────────────────

  Widget _buildBriefingView() {
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

    if (_hasError || _briefing.sections.isEmpty) {
      return _buildErrorOrEmpty();
    }

    return _buildSections();
  }

  Widget _buildErrorOrEmpty() {
    return ListView(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
      children: [
        const SizedBox(height: 40),
        Text(
          "Hey, I'm your companion.",
          style: TextStyle(
            color: _primaryText,
            fontSize: 15,
            height: 1.5,
            fontWeight: FontWeight.w400,
          ),
        ),
        const SizedBox(height: 24),
        Text(
          "To start, just speak or type\nwhatever's on your mind.",
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
          '(nothing yet — your surface\nwill fill as we talk)',
          textAlign: TextAlign.center,
          style: TextStyle(
            color: _mutedText.withValues(alpha: 0.6),
            fontSize: 11,
            fontStyle: FontStyle.italic,
          ),
        ),
        if (_hasError) ...[
          const SizedBox(height: 24),
          Center(
            child: TextButton(
              onPressed: () {
                setState(() {
                  _loading = true;
                  _hasError = false;
                });
                _fetchBriefing();
              },
              child: const Text('Retry', style: TextStyle(fontSize: 12)),
            ),
          ),
        ],
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

  // ── Sectioned briefing ────────────────────────────────────────────────────

  Widget _buildSections() {
    return ListView(
      padding: const EdgeInsets.only(top: 16, bottom: 16),
      children: [
        // Greeting
        _buildGreetingHeader(),
        const SizedBox(height: 16),

        // Sections
        for (final section in _briefing.sections) ...[
          _buildSection(section),
          const SizedBox(height: 16),
        ],
      ],
    );
  }

  Widget _buildGreetingHeader() {
    final hasPending = _briefing.pendingCount > 0;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  _briefing.greeting,
                  style: const TextStyle(
                    color: _primaryText,
                    fontSize: 17,
                    fontWeight: FontWeight.w500,
                    height: 1.4,
                  ),
                ),
              ),
              if (hasPending)
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                  decoration: BoxDecoration(
                    color: _accentAmber.withValues(alpha: 0.15),
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(
                        color: _accentAmber.withValues(alpha: 0.3)),
                  ),
                  child: Text(
                    '${_briefing.pendingCount} pending',
                    style: TextStyle(
                      color: _accentAmber,
                      fontSize: 11,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ),
            ],
          ),
          if (_briefing.nextEvent != null) ...[
            const SizedBox(height: 4),
            Text(
              _briefing.nextEvent!,
              style: TextStyle(
                color: _accentBlue,
                fontSize: 13,
                fontWeight: FontWeight.w400,
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildSection(BriefingSection section) {
    final isDecisions = section.id == 'decisions';

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // Section title
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 4),
          child: Text(
            section.title.toUpperCase(),
            style: TextStyle(
              color: _sectionTitleColor,
              fontSize: 11,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.8,
            ),
          ),
        ),

        // Section items
        ...section.items.map((item) => _buildBriefingItem(item, isDecisions)),
      ],
    );
  }

  Widget _buildBriefingItem(BriefingItem item, bool isDecision) {
    final textColor = item.isUrgent ? _accentRed : _primaryText;
    final bgOpacity = item.isUrgent ? 0.05 : 0.0;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: _cardBg.withValues(alpha: 0.4 + bgOpacity),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Icon
            Padding(
              padding: const EdgeInsets.only(top: 1, right: 10),
              child: Text(
                item.icon,
                style: const TextStyle(fontSize: 14),
              ),
            ),
            // Text + actions
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    item.text,
                    style: TextStyle(
                      color: textColor,
                      fontSize: 13,
                      height: 1.4,
                      fontWeight: item.isUrgent ? FontWeight.w500 : FontWeight.w400,
                    ),
                  ),
                  // Decision action chips
                  if (isDecision) ...[const SizedBox(height: 8), _buildDecisionActions(item)],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildDecisionActions(BriefingItem item) {
    final decisionType = item.decisionType ?? '';

    String approveLabel;
    String dismissLabel;

    if (decisionType == 'merge') {
      approveLabel = 'Accept';
      dismissLabel = 'Reject';
    } else if (decisionType == 'graph_node') {
      approveLabel = 'Approve';
      dismissLabel = 'Dismiss';
    } else {
      approveLabel = 'Approve';
      dismissLabel = 'Dismiss';
    }

    return Wrap(
      spacing: 8,
      runSpacing: 4,
      children: [
        _ActionChip(
          label: approveLabel,
          accent: _accentGreen,
          onTap: () => _handleDecisionAction(item, approveLabel.toLowerCase()),
        ),
        _ActionChip(
          label: dismissLabel,
          accent: _mutedText,
          onTap: () => _handleDecisionAction(item, dismissLabel.toLowerCase()),
        ),
      ],
    );
  }

  // ── Response moment (floating overlay) ────────────────────────────────────

  Widget _buildResponseMoment() {
    return Positioned(
      left: 32,
      right: 32,
      bottom: 80,
      child: FadeTransition(
        opacity: _momentController,
        child: Material(
          color: Colors.transparent,
          child: Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: _cardBg,
              borderRadius: BorderRadius.circular(14),
              border: Border.all(
                color: _accentGreen.withValues(alpha: 0.3),
              ),
              boxShadow: [
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.4),
                  blurRadius: 20,
                  offset: const Offset(0, 4),
                ),
              ],
            ),
            child: Row(
              children: [
                Container(
                  width: 4,
                  height: 32,
                  decoration: BoxDecoration(
                    color: _accentGreen,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(
                    _momentText ?? '',
                    style: const TextStyle(
                      color: _primaryText,
                      fontSize: 13,
                      height: 1.4,
                    ),
                  ),
                ),
              ],
            ),
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
          // Menu (with optional pending dot)
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(8),
              onTap: _openMenu,
              child: Container(
                padding: const EdgeInsets.all(10),
                child: Stack(
                  children: [
                    Icon(Icons.menu, color: _mutedText, size: 20),
                    if (_briefing.pendingCount > 0)
                      Positioned(
                        right: 4,
                        top: 4,
                        child: Container(
                          width: 7,
                          height: 7,
                          decoration: const BoxDecoration(
                            color: _accentAmber,
                            shape: BoxShape.circle,
                          ),
                        ),
                      ),
                  ],
                ),
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
