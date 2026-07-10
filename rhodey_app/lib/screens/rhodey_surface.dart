import 'dart:async';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:flutter_tts/flutter_tts.dart';
import 'package:google_fonts/google_fonts.dart';
import '../services/api_service.dart';
import '../services/notification_service.dart';
import '../models/briefing.dart';
import 'menu_sheet.dart';
import 'today_screen.dart';
import 'inbox_screen.dart';

// ─────────────────────────────────────────────────────────────────────────────
//  Rhodey Surface — Horizon / Traces Edition
//  ─────────────────────────────────────────────────────────────────────────────
//  Design spine: Design 3 (Horizon/Traces)
//  Emotional tone: Design 2 (editorial serif, warm stone palette)
//  Interaction seasoning: Design 1 (proactive card)
//
//  Layout:
//    Presence strip (44px, fixed)
//    Editorial greeting (serif, large, italic)
//    Segmented control: HORIZON / TRACES
//    Content (scrollable):
//      HORIZON:
//        ─ Proactive card (conditional)
//        ─ Sections: UPCOMING, DECISIONS (conditional), RECENT (max 3)
//      TRACES:
//        ─ Trace cards (input → outcome pairs)
//    Response moment (transient, floating)
//    Bottom dock (menu, speak, type)
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
  bool _showTraces = false; // false = Horizon, true = Traces

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

  // ── Warm stone palette (self-contained, no theme dependency) ──
  static const Color _bg = Color(0xFF0C0C0B);
  static const Color _surface = Color(0xFF161618);
  static const Color _cardBg = Color(0xFF1E1E1D);
  static const Color _border = Color(0xFF2C2C30);
  static const Color _primaryText = Color(0xFFEDE9E4);
  static const Color _mutedText = Color(0xFF7A756E);
  static const Color _tertiaryText = Color(0xFF6B6863);
  static const Color _champagne = Color(0xFFDFCCA7);
  static const Color _green = Color(0xFF34C759);
  static const Color _amber = Color(0xFFFFD60A);
  static const Color _red = Color(0xFFEF5350);
  static const Color _amberLight = Color(0x1AFFD60A);

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

    // Register push notification handlers
    NotificationService.onNotificationOpened = _handlePushNotificationTap;
    NotificationService.onPushReceived = _onPushReceived;

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
    if (NotificationService.onPushReceived == _onPushReceived) {
      NotificationService.onPushReceived = null;
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
      _hasError = briefing.sections.isEmpty && briefing.traces.isEmpty;
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

      // Fade moment after min ~2s
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

    _showResponseMoment(
        '${action == 'approve' || action == 'accept' ? '✅' : '⏳'} $action...');

    final isApprove = action == 'approve' || action == 'accept';
    final pendingId = int.tryParse(item.decisionId ?? '');
    if (pendingId == null) {
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
        result = null;
        break;
      default:
        result = null;
    }

    if (!mounted) return;

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

  // ── Notification handling ─────────────────────────────────────────────────

  /// Instant briefing fetch when a push notification is received in foreground.
  void _onPushReceived() {
    debugPrint('[Surface] Push received — fetching fresh briefing');
    _stopPolling();
    _fetchBriefing().then((_) {
      if (mounted) _startPolling();
    });
  }

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
        backgroundColor: _bg,
        body: SafeArea(
          child: Column(
            children: [
              _buildPresenceStrip(),
              Expanded(child: _buildContent()),
              _buildTypeBar(),
            ],
          ),
        ),
      );
    }

    return Scaffold(
      backgroundColor: _bg,
      body: SafeArea(
        child: Stack(
          children: [
            Column(
              children: [
                _buildPresenceStrip(),
                Expanded(child: _buildContent()),
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

  // ── Content (loading / error / Horizon / Traces) ──────────────────────────

  Widget _buildContent() {
    if (_loading) {
      return const Center(
        child: SizedBox(
          width: 20,
          height: 20,
          child: CircularProgressIndicator(
            strokeWidth: 2,
            color: Color(0xFF6B6863),
          ),
        ),
      );
    }

    if (_hasError) {
      return _buildErrorOrEmpty();
    }

    return _buildScrollableContent();
  }

  // ── Error / Empty state ───────────────────────────────────────────────────

  Widget _buildErrorOrEmpty() {
    return ListView(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
      children: [
        const SizedBox(height: 40),
        Text(
          "Hey, I'm your companion.",
          style: GoogleFonts.instrumentSerif(
            fontSize: 28,
            fontWeight: FontWeight.w300,
            fontStyle: FontStyle.italic,
            color: _primaryText,
            height: 1.2,
          ),
        ),
        const SizedBox(height: 24),
        Text(
          "To start, just speak or type\nwhatever's on your mind.",
          style: GoogleFonts.plusJakartaSans(
            fontSize: 13,
            fontWeight: FontWeight.w300,
            color: _mutedText,
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
          _sendMessage(
              'Note down: explore AI-powered meeting summaries for Qhord');
        }),
        const SizedBox(height: 32),
        Text(
          '(nothing yet — your surface\nwill fill as we talk)',
          textAlign: TextAlign.center,
          style: GoogleFonts.plusJakartaSans(
            fontSize: 11,
            fontStyle: FontStyle.italic,
            fontWeight: FontWeight.w300,
            color: _mutedText.withValues(alpha: 0.6),
            height: 1.4,
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
              child: Text(
                'Retry',
                style: GoogleFonts.plusJakartaSans(
                    fontSize: 12, color: _champagne),
              ),
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
            border: Border.all(color: _border),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Text(
            label,
            style: GoogleFonts.plusJakartaSans(
              color: _mutedText,
              fontSize: 13,
            ),
          ),
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
          bottom: BorderSide(color: _border.withValues(alpha: 0.5)),
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
                  color: _green.withValues(alpha: _pulseAnimation.value),
                  shape: BoxShape.circle,
                ),
              );
            },
          ),
          const SizedBox(width: 8),
          Text(
            'Rhodey',
            style: GoogleFonts.plusJakartaSans(
              color: _mutedText,
              fontSize: 12,
              fontWeight: FontWeight.w500,
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

  // ── Scrollable content (greeting + segment control + horizon/traces) ──────

  Widget _buildScrollableContent() {
    return ListView(
      padding: const EdgeInsets.only(top: 8, bottom: 16),
      children: [
        // Editorial greeting
        _buildEditorialGreeting(),
        const SizedBox(height: 16),

        // Segmented control: HORIZON | TRACES
        _buildSegmentedControl(),
        const SizedBox(height: 20),

        // Content based on mode
        if (_showTraces)
          _buildTracesView()
        else
          _buildHorizonView(),
      ],
    );
  }

  // ── Editorial greeting ────────────────────────────────────────────────────

  Widget _buildEditorialGreeting() {
    final hasPending = _briefing.pendingCount > 0;
    final greeting = _briefing.greeting;

    // Extract the editorial greeting from the API response.
    // If the API returns "Good evening, Danny. Qhord sync at 19:30.",
    // we show "Good evening, Danny." as serif headline
    // and the rest as subtext.
    final dotIndex = greeting.indexOf('.');
    String headline = greeting;
    String subtext = '';

    if (dotIndex > 0 && dotIndex < greeting.length - 1) {
      headline = greeting.substring(0, dotIndex + 1);
      subtext = greeting.substring(dotIndex + 1).trim();
      // Remove leading period if present
      if (subtext.startsWith('.')) subtext = subtext.substring(1).trim();
    }

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Text(
                  headline,
                  style: GoogleFonts.instrumentSerif(
                    fontSize: 30,
                    fontWeight: FontWeight.w300,
                    fontStyle: FontStyle.italic,
                    color: _primaryText,
                    height: 1.2,
                  ),
                ),
              ),
              if (hasPending)
                Container(
                  margin: const EdgeInsets.only(top: 6),
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                  decoration: BoxDecoration(
                    color: _amberLight,
                    borderRadius: BorderRadius.circular(8),
                    border: Border.all(
                        color: _amber.withValues(alpha: 0.3)),
                  ),
                  child: Text(
                    '${_briefing.pendingCount}',
                    style: GoogleFonts.plusJakartaSans(
                      color: _amber,
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
            ],
          ),
          if (subtext.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              subtext,
              style: GoogleFonts.plusJakartaSans(
                fontSize: 13,
                fontWeight: FontWeight.w300,
                color: _mutedText,
                height: 1.5,
              ),
            ),
          ],
        ],
      ),
    );
  }

  // ── Segmented control ─────────────────────────────────────────────────────

  Widget _buildSegmentedControl() {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20),
      child: Container(
        height: 32,
        decoration: BoxDecoration(
          color: _cardBg,
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: _border),
        ),
        child: Row(
          children: [
            Expanded(
              child: GestureDetector(
                onTap: () => setState(() => _showTraces = false),
                child: Container(
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: _showTraces ? Colors.transparent : _surface,
                    borderRadius: const BorderRadius.only(
                      topLeft: Radius.circular(7),
                      bottomLeft: Radius.circular(7),
                    ),
                  ),
                  child: Text(
                    'HORIZON',
                    style: GoogleFonts.jetBrainsMono(
                      fontSize: 9,
                      fontWeight: FontWeight.w500,
                      letterSpacing: 1.5,
                      color: _showTraces ? _tertiaryText : _champagne,
                      height: 1.2,
                    ),
                  ),
                ),
              ),
            ),
            Expanded(
              child: GestureDetector(
                onTap: () => setState(() => _showTraces = true),
                child: Container(
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: _showTraces ? _surface : Colors.transparent,
                    borderRadius: const BorderRadius.only(
                      topRight: Radius.circular(7),
                      bottomRight: Radius.circular(7),
                    ),
                  ),
                  child: Text(
                    'TRACES',
                    style: GoogleFonts.jetBrainsMono(
                      fontSize: 9,
                      fontWeight: FontWeight.w500,
                      letterSpacing: 1.5,
                      color: _showTraces ? _champagne : _tertiaryText,
                      height: 1.2,
                    ),
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  // ── Horizon view ──────────────────────────────────────────────────────────

  Widget _buildHorizonView() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // Latest response card (conditional — shown when Rhodey last said something)
        _buildLatestResponse(),
        const SizedBox(height: 4),

        // Proactive card (conditional)
        _buildProactiveCard(),
        const SizedBox(height: 8),

        // Sections from briefing
        for (final section in _briefing.sections) ...[
          _buildSection(section),
          const SizedBox(height: 4),
        ],
      ],
    );
  }

  // ── Latest response card ──────────────────────────────────────────────────

  Widget _buildLatestResponse() {
    final text = _briefing.latestResponse;
    if (text == null || text.isEmpty) return const SizedBox.shrink();

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: _surface,
          borderRadius: BorderRadius.circular(14),
          border: Border.all(
            color: _green.withValues(alpha: 0.15),
          ),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 3,
              height: 32,
              decoration: BoxDecoration(
                color: _green,
                borderRadius: BorderRadius.circular(2),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Icon(Icons.check_circle_outline,
                          size: 10, color: _green),
                      const SizedBox(width: 4),
                      Text(
                        'RHODEY',
                        style: GoogleFonts.jetBrainsMono(
                          fontSize: 8,
                          fontWeight: FontWeight.w400,
                          color: _green,
                          letterSpacing: 1.5,
                          height: 1.2,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 6),
                  Text(
                    text,
                    style: GoogleFonts.plusJakartaSans(
                      color: _primaryText,
                      fontSize: 12,
                      fontWeight: FontWeight.w300,
                      height: 1.4,
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

  Widget _buildProactiveCard() {
    // Show when there's a next event AND the event has an urgent item
    // For v1: show a proactive suggestion card
    final hasProactive = _briefing.nextEvent != null &&
        _briefing.sections.isNotEmpty &&
        _briefing.sections.first.items.any((i) => i.isUrgent);

    if (!hasProactive) return const SizedBox.shrink();

    // Find the urgent item
    final urgentItem = _briefing.sections.first.items.firstWhere(
      (i) => i.isUrgent,
      orElse: () => _briefing.sections.first.items.first,
    );

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: _surface,
          borderRadius: BorderRadius.circular(14),
          border: Border.all(color: _champagne.withValues(alpha: 0.15)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Container(
                  width: 3,
                  height: 16,
                  decoration: BoxDecoration(
                    color: _champagne,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
                const SizedBox(width: 10),
                Text(
                  'SUGGESTION',
                  style: GoogleFonts.jetBrainsMono(
                    fontSize: 9,
                    fontWeight: FontWeight.w400,
                    color: _champagne,
                    letterSpacing: 1.5,
                    height: 1.2,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            Text(
              '${urgentItem.icon} ${urgentItem.text}',
              style: GoogleFonts.plusJakartaSans(
                fontSize: 12,
                fontWeight: FontWeight.w400,
                color: _primaryText,
                height: 1.4,
              ),
            ),
            if (urgentItem.isDecision) ...[
              const SizedBox(height: 12),
              Row(
                children: [
                  _smallChip('Dismiss', _mutedText, () {
                    _handleDecisionAction(urgentItem, 'dismiss');
                  }),
                  const SizedBox(width: 8),
                  _smallChip('Approve', _green, () {
                    _handleDecisionAction(urgentItem, 'approve');
                  }),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _smallChip(String label, Color color, VoidCallback onTap) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(6),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(6),
            border: Border.all(color: color.withValues(alpha: 0.4)),
            color: color.withValues(alpha: 0.08),
          ),
          child: Text(
            label,
            style: GoogleFonts.plusJakartaSans(
              color: color,
              fontSize: 10,
              fontWeight: FontWeight.w500,
            ),
          ),
        ),
      ),
    );
  }

  // ── Section ───────────────────────────────────────────────────────────────

  Widget _buildSection(BriefingSection section) {
    final isDecisions = section.id == 'decisions';

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // Section title
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 6),
          child: Text(
            section.title.toUpperCase(),
            style: GoogleFonts.jetBrainsMono(
              fontSize: 9,
              fontWeight: FontWeight.w400,
              color: _tertiaryText,
              letterSpacing: 2.0,
              height: 1.3,
            ),
          ),
        ),
        ...section.items.map((item) => _buildBriefingItem(item, isDecisions)),
      ],
    );
  }

  Widget _buildBriefingItem(BriefingItem item, bool isDecision) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: _surface.withValues(alpha: item.isUrgent ? 0.6 : 0.4),
          borderRadius: BorderRadius.circular(10),
          border: item.isUrgent
              ? Border.all(color: _red.withValues(alpha: 0.15))
              : null,
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.only(top: 1, right: 10),
              child: Text(item.icon, style: const TextStyle(fontSize: 14)),
            ),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    item.text,
                    style: GoogleFonts.plusJakartaSans(
                      color: item.isUrgent ? _red : _primaryText,
                      fontSize: 13,
                      fontWeight:
                          item.isUrgent ? FontWeight.w400 : FontWeight.w300,
                      height: 1.4,
                    ),
                  ),
                  if (isDecision) ...[
                    const SizedBox(height: 8),
                    _buildDecisionActions(item),
                  ],
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
          accent: _green,
          onTap: () =>
              _handleDecisionAction(item, approveLabel.toLowerCase()),
        ),
        _ActionChip(
          label: dismissLabel,
          accent: _mutedText,
          onTap: () =>
              _handleDecisionAction(item, dismissLabel.toLowerCase()),
        ),
      ],
    );
  }

  // ── Traces view ───────────────────────────────────────────────────────────

  Widget _buildTracesView() {
    final traces = _briefing.traces;

    if (traces.isEmpty) {
      return Padding(
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 32),
        child: Center(
          child: Text(
            'No activity yet.\nSpeak or type to get started.',
            textAlign: TextAlign.center,
            style: GoogleFonts.plusJakartaSans(
              fontSize: 11,
              fontStyle: FontStyle.italic,
              fontWeight: FontWeight.w300,
              color: _tertiaryText,
              height: 1.4,
            ),
          ),
        ),
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
          child: Text(
            'YOUR RECENT ACTIVITY',
            style: GoogleFonts.jetBrainsMono(
              fontSize: 9,
              fontWeight: FontWeight.w400,
              color: _tertiaryText,
              letterSpacing: 2.0,
              height: 1.3,
            ),
          ),
        ),
        ...traces.map((trace) => _buildTraceCard(trace)),
      ],
    );
  }

  Widget _buildTraceCard(TraceItem trace) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 3),
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: _surface,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: _border.withValues(alpha: 0.5)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Time label
            Text(
              trace.time,
              style: GoogleFonts.jetBrainsMono(
                fontSize: 9,
                fontWeight: FontWeight.w400,
                color: _tertiaryText,
                letterSpacing: 1.0,
                height: 1.2,
              ),
            ),
            const SizedBox(height: 8),
            // Input (what the user asked) — only if not auto
            if (trace.input != '(auto)') ...[
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.subdirectory_arrow_right,
                      size: 12, color: _tertiaryText),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      trace.input,
                      style: GoogleFonts.plusJakartaSans(
                        fontSize: 11,
                        fontStyle: FontStyle.italic,
                        fontWeight: FontWeight.w300,
                        color: _mutedText,
                        height: 1.4,
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 6),
            ],
            // Resolution (what happened)
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  '\u2192',
                  style: TextStyle(
                    fontSize: 12,
                    color: _champagne.withValues(alpha: 0.6),
                  ),
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    trace.resolution,
                    style: GoogleFonts.plusJakartaSans(
                      fontSize: 12,
                      fontWeight: FontWeight.w300,
                      color: _primaryText,
                      height: 1.4,
                    ),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
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
              color: _surface,
              borderRadius: BorderRadius.circular(14),
              border: Border.all(
                color: _green.withValues(alpha: 0.3),
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
                  width: 3,
                  height: 32,
                  decoration: BoxDecoration(
                    color: _green,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(
                    _momentText ?? '',
                    style: GoogleFonts.plusJakartaSans(
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

  // ── Bottom dock ───────────────────────────────────────────────────────────

  Widget _buildBottomDock() {
    return Container(
      height: 56,
      padding: const EdgeInsets.symmetric(horizontal: 12),
      decoration: BoxDecoration(
        color: _surface,
        border: Border(
          top: BorderSide(color: _border.withValues(alpha: 0.5)),
        ),
      ),
      child: Row(
        children: [
          // Menu (left)
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
                            color: _amber,
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
                ? _green.withValues(alpha: 0.15)
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
                        ? _green.withValues(alpha: 0.5)
                        : _border,
                  ),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      _isListening ? '\uD83C\uDFA4 Listening...' : '\uD83C\uDFA4  Speak',
                      style: GoogleFonts.plusJakartaSans(
                        color: _isListening ? _green : _mutedText,
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

          // Keyboard (right)
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

  // ── Type bar ──────────────────────────────────────────────────────────────

  Widget _buildTypeBar() {
    return Container(
      padding: const EdgeInsets.fromLTRB(12, 6, 12, 12),
      decoration: BoxDecoration(
        color: _surface,
        border: Border(
          top: BorderSide(color: _border.withValues(alpha: 0.5)),
        ),
      ),
      child: SafeArea(
        top: false,
        child: Row(
          children: [
            Expanded(
              child: Container(
                decoration: BoxDecoration(
                  color: _bg,
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: _border),
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
                  style: GoogleFonts.plusJakartaSans(
                      color: _primaryText, fontSize: 14),
                ),
              ),
            ),
            const SizedBox(width: 6),
            Material(
              color: _surface,
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
                  child: Icon(Icons.arrow_upward,
                      color: _champagne, size: 18),
                ),
              ),
            ),
            const SizedBox(width: 4),
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
            style: GoogleFonts.plusJakartaSans(
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
