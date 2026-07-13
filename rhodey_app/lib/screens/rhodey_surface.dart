import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:flutter_tts/flutter_tts.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:image_picker/image_picker.dart';
import 'package:file_picker/file_picker.dart';
import 'package:shared_preferences/shared_preferences.dart';
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
  bool _apiConfigured = false;
  bool _showTraces = false; // false = Horizon, true = Traces

  // ── Conversation feed ──
  final List<Map<String, String>> _conversation = [];
  final _conversationScroll = ScrollController();
  bool _conversationLoaded = false;

  // ── Response moment ──
  String? _momentText;
  bool _showMoment = false;
  bool _isProcessing = false;

  // ── State ──
  bool _isListening = false;
  bool _isTyping = false;
  String? _sessionId; // For thread continuity across messages
  String _tracesSearchQuery = ''; // Client-side search for Traces view

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
  String _voiceText = '';        // Live partial transcript while speaking
  int _voiceDuration = 0;        // Seconds since recording started
  Timer? _voiceTimer;            // Ticks every second for duration

  // ── Animations ──
  late AnimationController _pulseController;
  late Animation<double> _pulseAnimation;
  late AnimationController _momentController;
  // ── Warm stone palette (self-contained, no theme dependency) ──
  static const Color _bg = Color(0xFF030302);
  static const Color _surface = Color(0xFF090908);
  static const Color _cardBg = Color(0xFF0E0E0D);
  static const Color _border = Color(0xFF1A1A19);
  static const Color _primaryText = Color(0xFFF5F4F0);
  static const Color _mutedText = Color(0xFFA8A29E);
  static const Color _tertiaryText = Color(0xFF57534E);
  static const Color _champagne = Color(0xFFDFCCA7);
  static const Color _accentGold = Color(0xFFDFCCA7);
  static const Color _amber = Color(0xFFDFCCA7);
  static const Color _red = Color(0xFFEF5350);
  static const Color _accentGoldLight = Color(0x1ADFCCA7);

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

    // Check API config & load initial briefing
    _apiConfigured = _api.config.isConfigured;
    // Load cached briefing instantly (if available), then refresh in background
    _loadCachedAndRefresh();

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
    _voiceTimer?.cancel();
    _textController.dispose();
    _typeFocus.dispose();
    _conversationScroll.dispose();
    if (NotificationService.onNotificationOpened == _handlePushNotificationTap) {
      NotificationService.onNotificationOpened = null;
    }
    if (NotificationService.onPushReceived == _onPushReceived) {
      NotificationService.onPushReceived = null;
    }
    super.dispose();
  }

  // ── Data loading ──────────────────────────────────────────────────────────

  Future<void> _fetchBriefing({bool isBackground = false}) async {
    final briefing = await _api.getBriefing();
    if (!mounted) return;
    final wasInitialLoad = _loading;
    setState(() {
      _briefing = briefing;
      _loading = false;
      _apiConfigured = _api.config.isConfigured;
      _hasError = briefing.sections.isEmpty && briefing.traces.isEmpty;
    });
    // On initial load only, populate conversation from API history
    if (wasInitialLoad && !_conversationLoaded) {
      _loadFromTraces(briefing.traces);
    }
    // Cache briefing for instant cold-start load
    _cacheBriefing();
  }

  /// Populate conversation feed from API traces (persisted chat history).
  void _loadFromTraces(List<TraceItem> traces) {
    if (traces.isEmpty) return;
    final entries = <Map<String, String>>[];
    // Traces are sorted most-recent-first; reverse to show oldest first
    for (final trace in traces.reversed) {
      // Skip auto-completions without user input
      if (trace.input != '(auto)') {
        entries.add({'role': 'user', 'text': trace.input});
      }
      entries.add({'role': 'assistant', 'text': trace.resolution});
    }
    if (!mounted) return;
    setState(() {
      _conversation.addAll(entries.take(50));
      _conversationLoaded = true;
    });
  }

  // ── Caching ───────────────────────────────────────────────────────────────

  Future<void> _loadCachedAndRefresh() async {
    // Load previously cached briefing for instant display
    try {
      final prefs = await SharedPreferences.getInstance();
      final cached = prefs.getString('cached_briefing');
      if (cached != null && cached.isNotEmpty && mounted) {
        final json = jsonDecode(cached) as Map<String, dynamic>;
        setState(() {
          _briefing = BriefingResponse.fromJson(json);
          _loading = false;
          _apiConfigured = _api.config.isConfigured;
        });
      }
    } catch (_) {
      // Silently fall through — cached data is optional
    }
    // Always fetch fresh data in background
    try {
      await _fetchBriefing(isBackground: true);
    } catch (_) {
      // Background refresh failures are non-fatal
    }
    // Resume polling after initial load regardless of fetch outcome
    _startPolling();
  }

  /// Save current briefing to SharedPreferences for instant cold-start loads.
  Future<void> _cacheBriefing() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString('cached_briefing', jsonEncode(_briefing.toJson()));
    } catch (_) {
      // Cache failures are non-critical
    }
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
    // Lightweight background cache update on poll
    _cacheBriefing();
  }

  // ── Send message ──────────────────────────────────────────────────────────

  void _sendMessage(String text) {
    if (text.trim().isEmpty) return;

    _textController.clear();
    _stopPolling();

    // Add user message to conversation feed immediately
    setState(() {
      _conversation.add({'role': 'user', 'text': text.trim()});
    });
    _scrollToBottom();

    // Show golden wave processing animation
    _showProcessingWave();

    _api.sendMessage(text.trim(), sessionId: _sessionId).then((result) {
      if (!mounted) return;

      String responseText;
      BriefingResponse? briefingUpdate;

      if (result.success && result.data is Map) {
        final data = result.data as Map<String, dynamic>;
        responseText = data['response'] as String? ?? 'Got it.';
        // Preserve session_id for thread continuity
        final newSessionId = data['session_id'] as String?;
        if (newSessionId != null && newSessionId.isNotEmpty) {
          _sessionId = newSessionId;
        }
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

      // Add Rhodey's response to conversation feed (permanent)
      final isSuccess = result.success;
      setState(() {
        _conversation.add({
          'role': 'assistant',
          'text': responseText,
          'status': isSuccess ? '' : 'error',
        });
      });
      _scrollToBottom();

      // Show brief response moment — error text visible in overlay, not spoken
      _showResponseMoment(isSuccess ? '✅' : responseText);

      // Speak only successful responses
      if (isSuccess) {
        _tts.stop();
        _tts.speak(responseText);
      }

      // Fade feedback moment after ~1.5s
      Future.delayed(const Duration(seconds: 1), () {
        if (!mounted) return;
        _hideResponseMoment();
        _startPolling();
      });
    });
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_conversationScroll.hasClients) {
        _conversationScroll.animateTo(
          _conversationScroll.position.maxScrollExtent,
          duration: const Duration(milliseconds: 300),
          curve: Curves.easeOut,
        );
      }
    });
  }

  void _showProcessingWave() {
    setState(() {
      _isProcessing = true;
      _momentText = null;
      _showMoment = true;
    });
    _momentController.forward(from: 0.0);
  }

  void _showResponseMoment(String text) {
    setState(() {
      _isProcessing = false;
      _momentText = text;
      _showMoment = true;
    });
    _momentController.forward(from: 0.0);
  }

  void _hideResponseMoment() {
    setState(() => _isProcessing = false);
    _momentController.reverse().then((_) {
      if (!mounted) return;
      setState(() {
        _showMoment = false;
        _momentText = null;
      });
    });
  }

  // ── Decision actions ──────────────────────────────────────────────────────



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
        _stopListening();
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
    _stopPolling();

    // Reset live transcript and duration
    _voiceText = '';
    _voiceDuration = 0;

    _voiceTimeout?.cancel();
    _voiceTimeout = Timer(const Duration(seconds: 15), () {
      _stopListening();
    });

    // Start duration timer
    _voiceTimer?.cancel();
    _voiceTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) {
        setState(() => _voiceDuration++);
      }
    });

    await _speech.listen(
      onResult: (result) {
        if (!mounted) return;
        // Show live partial transcript as user speaks
        final words = result.recognizedWords;
        if (words.isNotEmpty && words != _voiceText) {
          setState(() => _voiceText = words);
        }
        if (result.finalResult) {
          if (words.isNotEmpty) {
            _voiceTimer?.cancel();
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
    _voiceTimer?.cancel();
    await _speech.stop();
    if (!mounted) return;
    setState(() {
      _isListening = false;
      _voiceText = '';
      _voiceDuration = 0;
    });
    _startPolling();
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

  // ── Attachment picker ──────────────────────────────────────────────────────

  void _showAttachmentSheet() {
    showModalBottomSheet(
      context: context,
      backgroundColor: _surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (ctx) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 36, height: 4,
                decoration: BoxDecoration(
                  color: _border.withValues(alpha: 0.5),
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              const SizedBox(height: 16),
              Text(
                'Add attachment',
                style: GoogleFonts.plusJakartaSans(
                  fontSize: 13,
                  fontWeight: FontWeight.w500,
                  color: _primaryText,
                ),
              ),
              const SizedBox(height: 16),
              _attachmentTile(Icons.camera_alt_outlined, 'Camera', () {
                Navigator.pop(ctx);
                _pickFromCamera();
              }),
              _attachmentTile(Icons.photo_outlined, 'Gallery', () {
                Navigator.pop(ctx);
                _pickFromGallery();
              }),
              _attachmentTile(Icons.description_outlined, 'Document', () {
                Navigator.pop(ctx);
                _pickDocument();
              }),
            ],
          ),
        ),
      ),
    );
  }

  Widget _attachmentTile(IconData icon, String label, VoidCallback onTap) {
    return ListTile(
      leading: Icon(icon, color: _mutedText, size: 22),
      title: Text(
        label,
        style: GoogleFonts.plusJakartaSans(
          color: _primaryText,
          fontSize: 14,
        ),
      ),
      onTap: onTap,
    );
  }

  Future<void> _pickFromCamera() async {
    final picker = ImagePicker();
    final file = await picker.pickImage(source: ImageSource.camera);
    if (file != null) {
      await _uploadFile(file.path);
    }
  }

  Future<void> _pickFromGallery() async {
    final picker = ImagePicker();
    final file = await picker.pickImage(source: ImageSource.gallery);
    if (file != null) {
      await _uploadFile(file.path);
    }
  }

  Future<void> _pickDocument() async {
    final result = await FilePicker.platform.pickFiles(
      type: FileType.custom,
      allowedExtensions: ['pdf', 'docx', 'txt', 'jpg', 'jpeg', 'png', 'mp3', 'ogg', 'wav', 'mp4'],
    );
    if (result != null && result.files.single.path != null) {
      await _uploadFile(result.files.single.path!);
    }
  }

  Future<void> _uploadFile(String filePath) async {
    if (!File(filePath).existsSync()) return;

    _stopPolling();
    _showResponseMoment('Uploading...');

    // Add to conversation feed
    _addConversationEntry('user', '📎 File attached');

    final result = await _api.sendMultimodal(filePath);
    if (!mounted) return;

    if (result.success && result.data is Map) {
      final data = result.data as Map<String, dynamic>;
      final responseText = data['response'] as String? ?? 'Got it.';
      final briefingMap = data['briefing_update'] as Map<String, dynamic>?;
      if (briefingMap != null) {
        setState(() {
          _briefing = BriefingResponse.fromJson(briefingMap);
        });
      } else {
        _fetchBriefing();
      }
      // Add response to conversation feed
      _addConversationEntry('assistant', responseText);
      // Speak response aloud
      _tts.stop();
      _tts.speak(responseText);
      _showResponseMoment('✅');
    } else {
      final errorText = result.error ?? 'Upload failed';
      _addConversationEntry('assistant', errorText, isError: true);
      _showResponseMoment('⚠️');
    }

    _dismissMomentAfterDelay();
    _startPolling();
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
            // Full-screen recording overlay
            if (_isListening) _buildRecordingOverlay(),
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
      return _buildSkeletonLoading();
    }

    if (_hasError) {
      return _buildErrorOrEmpty();
    }

    return _buildScrollableContent();
  }

  // ── Error / Empty state ───────────────────────────────────────────────────

  Widget _buildErrorOrEmpty() {
    final isFirstLaunch = !_apiConfigured;

    return ListView(
      padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
      children: [
        const SizedBox(height: 40),
        // ── API not configured banner ──
        if (isFirstLaunch) ...[
          Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: _accentGoldLight,
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: _amber.withValues(alpha: 0.3)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(Icons.settings, color: _amber, size: 18),
                    const SizedBox(width: 8),
                    Text(
                      'ALMOST THERE',
                      style: GoogleFonts.jetBrainsMono(
                        fontSize: 9,
                        fontWeight: FontWeight.w500,
                        color: _amber,
                        letterSpacing: 2.0,
                        height: 1.3,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 10),
                Text(
                  'Tap the menu \u2630 \u2192 Settings to connect this app to your Rhodey backend.',
                  style: GoogleFonts.plusJakartaSans(
                    fontSize: 13,
                    color: _primaryText,
                    height: 1.4,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 32),
        ],
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
                  color: _accentGold.withValues(alpha: _pulseAnimation.value),
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
        // Editorial greeting (always at top)
        _buildEditorialGreeting(),
        const SizedBox(height: 20),

        // Conversation feed (right after greeting)
        if (_conversation.isNotEmpty) ...[
          _buildConversationFeed(),
          const SizedBox(height: 20),
        ],

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

  // ── Conversation feed ────────────────────────────────────────────────────

  Widget _buildConversationFeed() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 6),
          child: Row(
            children: [
              Text(
                'CONVERSATION',
                style: GoogleFonts.jetBrainsMono(
                  fontSize: 9,
                  fontWeight: FontWeight.w400,
                  color: _tertiaryText,
                  letterSpacing: 2.0,
                  height: 1.3,
                ),
              ),
              const Spacer(),
              GestureDetector(
                onTap: () => _confirmClearConversation(),
                child: Text(
                  'Clear',
                  style: GoogleFonts.plusJakartaSans(
                    fontSize: 10,
                    fontWeight: FontWeight.w400,
                    color: _mutedText,
                    decoration: TextDecoration.underline,
                    decorationColor: _mutedText.withValues(alpha: 0.4),
                    height: 1.3,
                  ),
                ),
              ),
            ],
          ),
        ),
        ConstrainedBox(
          constraints: BoxConstraints(
            maxHeight: MediaQuery.of(context).size.height * 0.35,
          ),
          child: ListView.builder(
            controller: _conversationScroll,
            physics: const ClampingScrollPhysics(),
            padding: const EdgeInsets.symmetric(horizontal: 16),
            itemCount: _conversation.length,
            itemBuilder: (ctx, i) {
              final entry = _conversation[i];
              final role = entry['role'] ?? '';
              final text = entry['text'] ?? '';
              final isError = entry['status'] == 'error';
              final isUser = role == 'user';

              return Padding(
                padding: const EdgeInsets.only(bottom: 4),
                child: Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: isUser
                        ? _cardBg
                        : _surface,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(
                      color: isError
                          ? _red.withValues(alpha: 0.3)
                          : isUser
                              ? _border.withValues(alpha: 0.4)
                              : _accentGold.withValues(alpha: 0.12),
                    ),
                  ),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Container(
                        width: 3,
                        height: 28,
                        margin: const EdgeInsets.only(right: 10),
                        decoration: BoxDecoration(
                          color: isUser
                              ? _mutedText
                              : isError
                                  ? _red
                                  : _accentGold,
                          borderRadius: BorderRadius.circular(2),
                        ),
                      ),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Row(
                              children: [
                                Text(
                                  isUser ? 'YOU' : 'RHODEY',
                                  style: GoogleFonts.jetBrainsMono(
                                    fontSize: 8,
                                    fontWeight: FontWeight.w400,
                                    color: isUser
                                        ? _tertiaryText
                                        : isError
                                            ? _red
                                            : _accentGold,
                                    letterSpacing: 1.5,
                                    height: 1.2,
                                  ),
                                ),
                                if (isError) ...[
                                  const SizedBox(width: 6),
                                  Icon(Icons.warning_amber_rounded,
                                      size: 10, color: _red),
                                ],
                              ],
                            ),
                            const SizedBox(height: 5),
                            Text(
                              text,
                              style: GoogleFonts.plusJakartaSans(
                                color: isError ? _red : _primaryText,
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
            },
          ),
        ),
      ],
    );
  }

  /// Add an entry to the conversation feed, capping at 50 to prevent unbounded growth.
  void _addConversationEntry(String role, String text, {bool isError = false}) {
    setState(() {
      _conversation.add({
        'role': role,
        'text': text,
        if (isError) 'status': 'error',
      });
      // Keep last 50 entries to prevent unbounded memory growth
      if (_conversation.length > 50) {
        _conversation.removeRange(0, _conversation.length - 50);
      }
    });
    _scrollToBottom();
  }

  /// Show confirmation dialog before clearing the conversation.
  Future<void> _confirmClearConversation() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: _surface,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(14),
          side: BorderSide(color: _border),
        ),
        title: Text(
          'Clear conversation?',
          style: GoogleFonts.plusJakartaSans(
            color: _primaryText,
            fontSize: 16,
            fontWeight: FontWeight.w500,
          ),
        ),
        content: Text(
          'This clears the conversation log shown here. The full history is still available in the app on next restart.',
          style: GoogleFonts.plusJakartaSans(
            color: _mutedText,
            fontSize: 13,
            height: 1.4,
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: Text(
              'Cancel',
              style: GoogleFonts.plusJakartaSans(
                color: _mutedText,
                fontSize: 13,
              ),
            ),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(
              'Clear',
              style: GoogleFonts.plusJakartaSans(
                color: _red,
                fontSize: 13,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
        ],
      ),
    );
    if (confirmed == true && mounted) {
      setState(() => _conversation.clear());
    }
  }

  // ── Editorial greeting ────────────────────────────────────────────────────

  Widget _buildEditorialGreeting() {
    final greeting = _briefing.greeting;

    // Extract the editorial greeting from the API response.
    final dotIndex = greeting.indexOf('.');
    String headline = greeting;
    String subtext = '';

    if (dotIndex > 0 && dotIndex < greeting.length - 1) {
      headline = greeting.substring(0, dotIndex + 1);
      subtext = greeting.substring(dotIndex + 1).trim();
      if (subtext.startsWith('.')) subtext = subtext.substring(1).trim();
    }

    // Compute task/event counts from briefing sections (informational only)
    int taskCount = 0;
    int eventCount = 0;
    for (final section in _briefing.sections) {
      if (section.id == 'decisions') continue;
      for (final item in section.items) {
        if (item.icon.startsWith('\ud83d\udcc5') ||
            item.icon.startsWith('\ud83d\udd34') ||
            item.icon.startsWith('\u23f0')) {
          eventCount++;
        } else {
          taskCount++;
        }
      }
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
          // Informational summary — tasks and events only, no decisions
          if (taskCount > 0 || eventCount > 0) ...[
            const SizedBox(height: 10),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              decoration: BoxDecoration(
                color: _surface,
                borderRadius: BorderRadius.circular(10),
                border: Border.all(color: _border.withValues(alpha: 0.5)),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (eventCount > 0) ...[
                    const Text('\u2600\ufe0f', style: TextStyle(fontSize: 13)),
                    const SizedBox(width: 4),
                    Text(
                      '$eventCount event${eventCount == 1 ? '' : 's'} today',
                      style: GoogleFonts.plusJakartaSans(
                        fontSize: 11,
                        color: _mutedText,
                        fontWeight: FontWeight.w300,
                      ),
                    ),
                  ],
                  if (eventCount > 0 && taskCount > 0) ...[
                    const SizedBox(width: 8),
                    Container(
                      width: 3, height: 3,
                      decoration: const BoxDecoration(
                        color: Color(0xFF57534E),
                        shape: BoxShape.circle,
                      ),
                    ),
                    const SizedBox(width: 8),
                  ],
                  if (taskCount > 0) ...[
                    const Text('\ud83d\udccb', style: TextStyle(fontSize: 13)),
                    const SizedBox(width: 4),
                    Text(
                      '$taskCount item${taskCount == 1 ? '' : 's'} to review',
                      style: GoogleFonts.plusJakartaSans(
                        fontSize: 11,
                        color: _mutedText,
                        fontWeight: FontWeight.w300,
                      ),
                    ),
                  ],
                ],
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
    // Skip the latest response card when the conversation feed shows the same
    if (_conversation.isNotEmpty) return const SizedBox.shrink();
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
            color: _accentGold.withValues(alpha: 0.15),
          ),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 3,
              height: 32,
              decoration: BoxDecoration(
                color: _accentGold,
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
                          size: 10, color: _accentGold),
                      const SizedBox(width: 4),
                      Text(
                        'RHODEY',
                        style: GoogleFonts.jetBrainsMono(
                          fontSize: 8,
                          fontWeight: FontWeight.w400,
                          color: _accentGold,
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

          ],
        ),
      ),
    );
  }



  // ── Section ───────────────────────────────────────────────────────────────

  Widget _buildSection(BriefingSection section) {
    final isDecisions = section.id == 'decisions';
    final items = section.items;

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
        // Summarized prose for non-decision sections with 3+ items
        if (!isDecisions && items.length >= 3)
          _buildSummarizedSection(items)
        else
          ...items.map((item) => _buildBriefingItem(item, isDecisions)),
      ],
    );
  }

  /// Render section items as a single prose paragraph instead of individual cards.
  Widget _buildSummarizedSection(List<BriefingItem> items) {
    // Count items by urgency
    final urgent = items.where((i) => i.isUrgent).toList();
    final regular = items.where((i) => !i.isUrgent).toList();

    // Build a concise summary
    final parts = <String>[];
    if (urgent.isNotEmpty) {
      final lines = urgent.take(2).map((i) => '${i.icon} ${i.text}').join(' • ');
      parts.add(lines);
      if (urgent.length > 2) {
        parts.add('+${urgent.length - 2} more urgent');
      }
    }
    if (regular.isNotEmpty) {
      final count = regular.length;
      if (parts.isNotEmpty) {
        parts[parts.length - 1] += ' — and $count more items';
      } else {
        parts.add('$count items to review');
      }
    }

    final summary = parts.join('\n');

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: _surface.withValues(alpha: 0.5),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: _border.withValues(alpha: 0.4)),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 3,
              height: 28,
              decoration: BoxDecoration(
                color: _champagne.withValues(alpha: 0.6),
                borderRadius: BorderRadius.circular(2),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                summary,
                style: GoogleFonts.plusJakartaSans(
                  color: _mutedText,
                  fontSize: 12,
                  fontWeight: FontWeight.w300,
                  height: 1.5,
                ),
              ),
            ),
          ],
        ),
      ),
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
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }



  // ── Traces view ───────────────────────────────────────────────────────────

  Widget _buildTracesView() {
    var traces = _briefing.traces;

    // Client-side search filter
    if (_tracesSearchQuery.isNotEmpty) {
      final q = _tracesSearchQuery.toLowerCase();
      traces = traces.where((t) =>
        t.input.toLowerCase().contains(q) ||
        t.resolution.toLowerCase().contains(q)
      ).toList();
    }

    if (_briefing.traces.isEmpty) {
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
        // Search bar
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
          child: Container(
            decoration: BoxDecoration(
              color: _cardBg,
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: _border),
            ),
            child: TextField(
              onChanged: (v) => setState(() => _tracesSearchQuery = v),
              style: GoogleFonts.plusJakartaSans(
                color: _primaryText,
                fontSize: 12,
                fontWeight: FontWeight.w300,
              ),
              decoration: InputDecoration(
                hintText: 'Search conversations...',
                hintStyle: GoogleFonts.plusJakartaSans(
                  color: _tertiaryText,
                  fontSize: 12,
                  fontWeight: FontWeight.w300,
                ),
                prefixIcon: Icon(Icons.search, color: _tertiaryText, size: 16),
                suffixIcon: _tracesSearchQuery.isNotEmpty
                    ? GestureDetector(
                        onTap: () => setState(() => _tracesSearchQuery = ''),
                        child: Icon(Icons.close, color: _tertiaryText, size: 14),
                      )
                    : null,
                border: InputBorder.none,
                contentPadding: const EdgeInsets.symmetric(vertical: 8),
                isDense: true,
              ),
            ),
          ),
        ),
        // Results label
        if (_tracesSearchQuery.isNotEmpty) ...[
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
            child: Text(
              '${traces.length} result${traces.length == 1 ? '' : 's'}',
              style: GoogleFonts.plusJakartaSans(
                fontSize: 10,
                color: _tertiaryText,
                height: 1.3,
              ),
            ),
          ),
        ] else ...[
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
        ],
        if (traces.isEmpty && _tracesSearchQuery.isNotEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 16),
            child: Text(
              'No results for "$_tracesSearchQuery"',
              style: GoogleFonts.plusJakartaSans(
                fontSize: 11,
                fontStyle: FontStyle.italic,
                color: _tertiaryText,
              ),
            ),
          )
        else
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
                color: _isProcessing
                    ? _champagne.withValues(alpha: 0.3)
                    : _accentGold.withValues(alpha: 0.3),
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
                // Accent bar
                Container(
                  width: 3,
                  height: _isProcessing ? 28 : 32,
                  decoration: BoxDecoration(
                    color: _isProcessing ? _champagne : _accentGold,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: _isProcessing
                      ? const _ProcessingWave()
                      : Text(
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

  // ── Full-screen recording overlay ──────────────────────────────────────

  /// Replaces the full screen when the mic is active.
  Widget _buildRecordingOverlay() {
    return GestureDetector(
      onTap: _onMicTap,
      child: Container(
        color: _bg,
        child: Column(
          children: [
            // Presence strip (with recording indicator)
            Container(
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
                  Container(
                    width: 8,
                    height: 8,
                    decoration: BoxDecoration(
                      color: _accentGold,
                      shape: BoxShape.circle,
                    ),
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
                  const SizedBox(width: 8),
                  Text(
                    'RECORDING',
                    style: GoogleFonts.jetBrainsMono(
                      fontSize: 9,
                      fontWeight: FontWeight.w400,
                      letterSpacing: 2.0,
                      color: _accentGold,
                      height: 1.2,
                    ),
                  ),
                ],
              ),
            ),
            // Main recording area
            Expanded(
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  // Large animated wave
                  const _RecordingWave(),
                  const SizedBox(height: 48),
                  // Live transcript
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 32),
                    child: Text(
                      _voiceText.isNotEmpty ? _voiceText : 'I\'m listening...',
                      textAlign: TextAlign.center,
                      maxLines: 4,
                      overflow: TextOverflow.ellipsis,
                      style: GoogleFonts.instrumentSerif(
                        fontSize: 26,
                        fontWeight: FontWeight.w300,
                        fontStyle: FontStyle.italic,
                        color: _voiceText.isNotEmpty
                            ? _primaryText
                            : _mutedText,
                        height: 1.4,
                      ),
                    ),
                  ),
                  const SizedBox(height: 24),
                  // Duration
                  Container(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
                    decoration: BoxDecoration(
                      color: _surface,
                      borderRadius: BorderRadius.circular(12),
                      border: Border.all(
                          color: _border.withValues(alpha: 0.4)),
                    ),
                    child: Text(
                      _formatDuration(_voiceDuration),
                      style: GoogleFonts.jetBrainsMono(
                        fontSize: 14,
                        fontWeight: FontWeight.w400,
                        color: _tertiaryText,
                        height: 1.4,
                      ),
                    ),
                  ),
                  const SizedBox(height: 80),
                  // Stop button
                  Column(
                    children: [
                      Container(
                        width: 64,
                        height: 64,
                        decoration: BoxDecoration(
                          color: _red.withValues(alpha: 0.12),
                          shape: BoxShape.circle,
                          border: Border.all(
                              color: _red.withValues(alpha: 0.4)),
                        ),
                        child: const Icon(Icons.stop_rounded,
                            color: Color(0xFFEF5350), size: 32),
                      ),
                      const SizedBox(height: 12),
                      Text(
                        'Tap anywhere to stop',
                        style: GoogleFonts.plusJakartaSans(
                          fontSize: 12,
                          color: _mutedText,
                          fontWeight: FontWeight.w400,
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ],
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

          // + Attachment (left-center)
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(8),
              onTap: _showAttachmentSheet,
              child: Container(
                padding: const EdgeInsets.all(10),
                child: Icon(Icons.add, color: _mutedText, size: 20),
              ),
            ),
          ),

          const Spacer(),

          // Primary: Tap to speak (or live recording bar when listening)
          _buildSpeakButton(),

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

  /// Speak button.
  Widget _buildSpeakButton() {
    return Material(
      color: Colors.transparent,
      borderRadius: BorderRadius.circular(20),
      child: InkWell(
        borderRadius: BorderRadius.circular(20),
        onTap: _onMicTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(20),
            border: Border.all(color: _border),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                '\uD83C\uDFA4  Speak',
                style: GoogleFonts.plusJakartaSans(
                  color: _mutedText,
                  fontSize: 13,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  String _formatDuration(int seconds) {
    final m = (seconds ~/ 60).toString().padLeft(2, '0');
    final s = (seconds % 60).toString().padLeft(2, '0');
    return '$m:$s';
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
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            // Command suggestion chips
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                child: Row(
                  children: [
                    _suggestionChip('📅  Today', () {
                      _sendMessage('/today');
                      setState(() => _isTyping = false);
                    }),
                    const SizedBox(width: 6),
                    _suggestionChip('🧠  Ask', () {
                      setState(() => _isTyping = false);
                      // Open text field with ? prefix hint
                      _textController.text = '?';
                      _typeFocus.requestFocus();
                    }),
                    const SizedBox(width: 6),
                    _suggestionChip('📝  Note', () {
                      _sendMessage('/note');
                      setState(() => _isTyping = false);
                    }),
                    const SizedBox(width: 6),
                    _suggestionChip('❓  Why', () {
                      _sendMessage('/why');
                      setState(() => _isTyping = false);
                    }),
                    const SizedBox(width: 6),
                    _suggestionChip('⚡  Quick', () {
                      _sendMessage('Quick note: ');
                      setState(() => _isTyping = false);
                    }),
                  ],
                ),
              ),
            ),
            Row(
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
          ],
        ),
      ),
    );
  }

  Widget _suggestionChip(String label, VoidCallback onTap) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(8),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: _border),
            color: _cardBg,
          ),
          child: Text(
            label,
            style: GoogleFonts.plusJakartaSans(
              color: _mutedText,
              fontSize: 12,
              fontWeight: FontWeight.w400,
            ),
          ),
        ),
      ),
    );
  }

  // ── Skeleton Loading ──────────────────────────────────────────────────────

  /// Replaces the old CircularProgressIndicator with a full-page shimmer
  /// skeleton that mirrors the real layout (greeting, chip, section cards).
  Widget _buildSkeletonLoading() {
    return _ShimmerEffect(
      child: ListView(
        padding: const EdgeInsets.only(top: 24, bottom: 16),
        physics: const NeverScrollableScrollPhysics(),
        children: [
          // Greeting skeleton
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 20),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _skeletonLine(width: 0.55, height: 32, radius: 8),
                const SizedBox(height: 10),
                _skeletonLine(width: 0.4, height: 16, radius: 6),
                const SizedBox(height: 18),
                _skeletonLine(width: 0.3, height: 28, radius: 10),
              ],
            ),
          ),
          const SizedBox(height: 40),
          // Section 1 header
          _skeletonSectionHeader(),
          // Section 1 cards (3)
          _skeletonCard(height: 56),
          _skeletonCard(height: 56),
          _skeletonCard(height: 56),
          const SizedBox(height: 16),
          // Section 2 header
          _skeletonSectionHeader(),
          // Section 2 cards (3)
          _skeletonCard(height: 44),
          _skeletonCard(height: 44),
          _skeletonCard(height: 44),
        ],
      ),
    );
  }

  Widget _skeletonSectionHeader() {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 12, 20, 8),
      child: _skeletonLine(width: 0.2, height: 12, radius: 4),
    );
  }

  Widget _skeletonCard({required double height}) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 3),
      child: _skeletonLine(width: 1.0, height: height, radius: 10),
    );
  }

  Widget _skeletonLine({
    required double width,
    required double height,
    required double radius,
  }) {
    return FractionallySizedBox(
      widthFactor: width,
      alignment: Alignment.centerLeft,
      child: Container(
        height: height,
        decoration: BoxDecoration(
          color: const Color(0xFF0E0E0D),
          borderRadius: BorderRadius.circular(radius),
        ),
      ),
    );
  }
}

// ── Supporting widgets ──────────────────────────────────────────────────────

/// Animated golden wave shown during processing (replaces "Processing...").
class _ProcessingWave extends StatefulWidget {
  const _ProcessingWave();

  @override
  State<_ProcessingWave> createState() => _ProcessingWaveState();
}

class _ProcessingWaveState extends State<_ProcessingWave>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1000),
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
          children: List.generate(5, (i) {
            final phase = (_controller.value + i * 0.2) % 1.0;
            // Wave shape: bars start short, peak at phase 0.5, then fall
            final height = 4.0 + 20.0 * (1.0 - ((phase * 2 - 1).abs()).clamp(0.0, 1.0));
            return Padding(
              padding: const EdgeInsets.symmetric(horizontal: 2.0),
              child: Container(
                width: 4,
                height: height,
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.bottomCenter,
                    end: Alignment.topCenter,
                    colors: [
                      const Color(0xFFDFCCA7).withValues(alpha: 0.3 + 0.4 * (1.0 - phase)),
                      const Color(0xFFDFCCA7).withValues(alpha: 0.7 * (1.0 - phase * 0.5)),
                    ],
                  ),
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
                  color: const Color(0xFFDFCCA7)
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

/// Large animated wave shown during full-screen recording.
class _RecordingWave extends StatefulWidget {
  const _RecordingWave();

  @override
  State<_RecordingWave> createState() => _RecordingWaveState();
}

class _RecordingWaveState extends State<_RecordingWave>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 800),
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
        return SizedBox(
          height: 160,
          child: Center(
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: List.generate(9, (i) {
                final phase = (_controller.value + i * 0.11) % 1.0;
                // Wave shape: bars pulse from 10px to 130px
                final height = 10.0 +
                    120.0 *
                        (1.0 - ((phase * 2 - 1).abs()).clamp(0.0, 1.0));
                return Container(
                  width: 5,
                  height: height,
                  margin: const EdgeInsets.symmetric(horizontal: 3),
                  decoration: BoxDecoration(
                    gradient: LinearGradient(
                      begin: Alignment.bottomCenter,
                      end: Alignment.topCenter,
                      colors: [
                        const Color(0xFFDFCCA7).withValues(alpha: 0.15),
                        const Color(0xFFDFCCA7).withValues(alpha: 0.85),
                      ],
                    ),
                    borderRadius: BorderRadius.circular(3),
                  ),
                );
              }),
            ),
          ),
        );
      },
    );
  }
}




/// Gold shimmer effect that sweeps left-to-right across skeleton placeholders.
/// Acts as a visual mask — the child's shapes are revealed with a gradient
/// that moves from dark → champagne-tinted → dark, creating a loading pulse.
class _ShimmerEffect extends StatefulWidget {
  final Widget child;
  const _ShimmerEffect({required this.child});

  @override
  State<_ShimmerEffect> createState() => _ShimmerEffectState();
}

class _ShimmerEffectState extends State<_ShimmerEffect>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1800),
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
        return ShaderMask(
          shaderCallback: (bounds) {
            final progress = _controller.value;
            return LinearGradient(
              colors: [
                const Color(0xFF0E0E0D),
                const Color(0xFFDFCCA7).withValues(alpha: 0.22),
                const Color(0xFF0E0E0D),
              ],
              stops: const [0.0, 0.5, 1.0],
              begin: Alignment(-1.0 + progress * 2.0, 0.0),
              end: Alignment(1.0 + progress * 2.0, 0.0),
            ).createShader(bounds);
          },
          blendMode: BlendMode.srcIn,
          child: child!,
        );
      },
      child: widget.child,
    );
  }
}

// ── Ambient Ember Glow ─────────────────────────────────────────────────────
/// Floating ember particles drifting slowly upward. Subtle, meditative, warm.


class _EmberData {
  final double startTime;
  final double speed;
  final double baseX;
  final double driftAmount;
  final double phase;
  final double radius;

  const _EmberData({
    required this.startTime,
    required this.speed,
    required this.baseX,
    required this.driftAmount,
    required this.phase,
    required this.radius,
  });

  factory _EmberData.random(Random rng, int index) {
    return _EmberData(
      startTime: rng.nextDouble(),
      speed: 0.04 + rng.nextDouble() * 0.08,
      baseX: 0.05 + rng.nextDouble() * 0.90,
      driftAmount: 0.01 + rng.nextDouble() * 0.03,
      phase: rng.nextDouble() * 6.28,
      radius: 1.2 + rng.nextDouble() * 1.6,
    );
  }
}

class _EmberPainter extends CustomPainter {
  final List<_EmberData> embers;
  final double time;

  _EmberPainter({required this.embers, required this.time});

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()..style = PaintingStyle.fill;

    for (final ember in embers) {
      final progress = (ember.startTime + time * ember.speed) % 1.0;
      final y = size.height * (1.0 - progress);
      final xOffset = sin(progress * pi + ember.phase) * ember.driftAmount;
      final x = size.width * (ember.baseX + xOffset);
      final opacity = sin(progress * pi) * 0.18;
      if (opacity < 0.005) continue;

      paint.color = const Color(0xFFDFCCA7).withValues(alpha: opacity);
      canvas.drawCircle(Offset(x, y), ember.radius, paint);
    }
  }

  @override
  bool shouldRepaint(_EmberPainter oldDelegate) =>
      (time - oldDelegate.time).abs() > 0.001;
}

class _AmbientEmberGlow extends StatefulWidget {
  const _AmbientEmberGlow();

  @override
  State<_AmbientEmberGlow> createState() => _AmbientEmberGlowState();
}

class _AmbientEmberGlowState extends State<_AmbientEmberGlow>
    with SingleTickerProviderStateMixin {
  late AnimationController _controller;
  late List<_EmberData> _embers;

  @override
  void initState() {
    super.initState();
    final rng = Random(42);
    _embers = List.generate(14, (i) => _EmberData.random(rng, i));
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 18),
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
        return CustomPaint(
          painter: _EmberPainter(
            embers: _embers,
            time: _controller.value,
          ),
        );
      },
    );
  }
}
