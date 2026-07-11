import 'dart:async';
import 'package:flutter/material.dart';

// ─────────────────────────────────────────────────────────────────────────────
//  Rhodey Surface — Interactive Prototype
//  ─────────────────────────────────────────────────────────────────────────────
//  Layout:
//    Fixed presence strip (top) → ● Rhodey
//    Scrollable surface (middle) → items with 3 visual weights
//    Fixed bottom dock (bottom) → [≡]  [🎤 Tap to speak]  [⌨︎]
//
//  3 simulated flows (triggered by tapping the corresponding dock element):
//    1. Mic → capture a note by voice
//    2. Structured approval request arrives (auto-triggered after 5s)
//    3. Reopen after hours (greeting updates, history preserved)
// ─────────────────────────────────────────────────────────────────────────────

// ── Item types that can appear on the surface ────────────────────────────────

enum SurfaceItemType {
  greeting,           // Rhodey's headline greeting
  userCapture,        // Your message — muted, icon-led
  rhodeyResponse,     // Rhodey's reply — primary weight, no icon
  structuredDecision, // Decision card — thin card with accent + chips
  chronology,         // Time marker — faint, centered
  historyHint,        // "scroll up for older" — disappears after first scroll
  starterChips,       // Blank-state suggestion chips
}

// ── Main prototype widget ────────────────────────────────────────────────────

class SurfacePrototype extends StatefulWidget {
  const SurfacePrototype({super.key});

  @override
  State<SurfacePrototype> createState() => _SurfacePrototypeState();
}

class _SurfacePrototypeState extends State<SurfacePrototype>
    with TickerProviderStateMixin {
  // ── Surface data ──
  final List<_RenderableItem> _items = [];
  final _scrollController = ScrollController();
  int _itemIdSeq = 0;

  // ── State ──
  bool _hasScrolledOnce = false;
  bool _showHistoryHint = true;
  bool _isListening = false;
  bool _isTyping = false;
  bool _decisionCardExpanded = true;
  String? _decisionChipSelected;

  // ── Animations ──
  late AnimationController _pulseController;
  late Animation<double> _pulseAnimation;

  // ── Flow timers ──
  Timer? _decisionTimer;
  Timer? _voiceDelayTimer;

  // ── Text editing ──
  final _textController = TextEditingController();
  final _typeFocus = FocusNode();

  // ── Visual constants ──
  static const Color _surfaceBg = Color(0xFF0E0E10);
  static const Color _primaryText = Color(0xFFF2F2F2);
  static const Color _mutedText = Color(0xFF6B6B70);
  static const Color _accentGold = Color(0xFFDFCCA7);
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

    // Populate initial surface
    _buildInitialSurface();

    // Auto-trigger structured decision after 5 seconds (Flow 2)
    _decisionTimer = Timer(const Duration(seconds: 5), () {
      if (mounted) _injectDecisionItem();
    });

    // History hint is handled by NotificationListener<ScrollNotification>
    // in _buildSurfaceList — no separate listener needed.
  }

  @override
  void dispose() {
    _pulseController.dispose();
    _decisionTimer?.cancel();
    _voiceDelayTimer?.cancel();
    _scrollController.dispose();
    _textController.dispose();
    _typeFocus.dispose();
    super.dispose();
  }

  // ── Surface building ──────────────────────────────────────────────────────

  void _buildInitialSurface() {
    // Simulate "reopen after hours" — build realistic history
    final isEvening = DateTime.now().hour >= 17;

    _items.addAll([
      // Older items (scroll up to see)
      _renderable(SurfaceItemType.chronology, '─ 2:15 PM ──'.trimLeft(),
          timestamp: '2:15 PM'),
      _renderable(SurfaceItemType.userCapture,
          'Remind me to call Sunju about school',
          icon: '📝'),
      _renderable(SurfaceItemType.rhodeyResponse,
          '✅ Task created: Call Sunju re school.\nDue Monday.'),
      _renderable(SurfaceItemType.chronology, '─ 2:08 PM ──'.trimLeft(),
          timestamp: '2:08 PM'),
      _renderable(SurfaceItemType.userCapture,
          "What's new with Qhord this week?",
          icon: '🗣️'),
      _renderable(SurfaceItemType.rhodeyResponse,
          'Qhord GA is on track for next week. '
              'The pricing page needs your review — '
              'pushed a draft to your inbox.'),
      _renderable(SurfaceItemType.chronology, '─ 11:30 AM ──'.trimLeft(),
          timestamp: '11:30 AM'),
      _renderable(SurfaceItemType.structuredDecision,
          'Found "Anjali" in the Equisoft transcript.',
          subtitle: 'Should I add them as a contact?',
          chips: const ['Add them', 'Edit details', 'Skip'],
          isUrgent: false),
    ]);

    // Current greeting (bottom of surface, newest)
    _items.add(
      _renderable(
        SurfaceItemType.greeting,
        isEvening
            ? 'Good evening, Danny. '
                'Qhord sync came and went. '
                'Two decisions from this afternoon still open.'
            : 'Good morning, Danny. '
                '30 minutes until the Qhord sync. '
                'Anything to flag before then?',
      ),
    );

    _itemIdSeq = _items.length;

    // Scroll to bottom on next frame
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _scrollToBottom();
    });
  }

  _RenderableItem _renderable(SurfaceItemType type, String text,
      {String? icon,
      String? subtitle,
      String? timestamp,
      List<String>? chips,
      bool isUrgent = false}) {
    return _RenderableItem(
      id: _itemIdSeq++,
      type: type,
      text: text,
      icon: icon,
      subtitle: subtitle,
      timestamp: timestamp,
      chips: chips,
      isUrgent: isUrgent,
    );
  }

  // ── Flow 1: Capture a note by voice ───────────────────────────────────────

  void _onMicTap() {
    if (_isListening) {
      _stopListening();
      return;
    }

    setState(() => _isListening = true);

    // Simulate voice capture
    _voiceDelayTimer = Timer(const Duration(seconds: 2), () {
      if (!mounted) return;

      // Add the captured note
      _addItem(SurfaceItemType.userCapture,
          'Remind me to pick up milk on the way home',
          icon: '📝');

      setState(() => _isListening = false);

      // Rhodey responds after a beat
      Future.delayed(const Duration(milliseconds: 800), () {
        if (!mounted) return;
        _addItem(SurfaceItemType.rhodeyResponse,
            '✅ Task created: Pick up milk.\nDue today — flagging as errand.');
      });
    });
  }

  void _stopListening() {
    _voiceDelayTimer?.cancel();
    setState(() => _isListening = false);
  }

  // ── Flow 2: Structured decision arrives (auto or manual) ──────────────────

  void _injectDecisionItem() {
    _addItem(
      SurfaceItemType.structuredDecision,
      'New person from Solvstratum meeting: "Regi."',
      subtitle: 'Mentioned in context of the Qhord pricing review. Add them?',
      chips: const ['Approve', 'Edit name', 'Dismiss'],
      isUrgent: true,
    );
  }

  void _onDecisionChipTap(String chip, _RenderableItem item) {
    setState(() {
      _decisionChipSelected = chip;
      _decisionCardExpanded = false;
    });

    // After collapse animation, show confirmation
    Future.delayed(const Duration(milliseconds: 350), () {
      if (!mounted) return;
      if (chip == 'Dismiss') {
        // Remove the item entirely
        setState(() => _items.remove(item));
      } else {
        // Update the item to show as resolved
        setState(() {
          item.type = SurfaceItemType.rhodeyResponse;
          item.text = chip == 'Approve'
              ? '✅ Regi added as contact (Solvstratum).'
              : '✏️ Regi saved with edited name.';
          item.chips = null;
          item.isUrgent = false;
        });
      }
      _decisionChipSelected = null;
      _decisionCardExpanded = true;
    });
  }

  // ── Flow 3: Reopen after hours (handled by _buildInitialSurface) ───────────
  // The greeting updates based on time of day, and items from earlier
  // are visible when scrolling up.

  // ── Shared helpers ────────────────────────────────────────────────────────

  void _addItem(SurfaceItemType type, String text,
      {String? icon,
      String? subtitle,
      List<String>? chips,
      bool isUrgent = false}) {
    // Add a chronology marker if enough time has passed
    // (simulated: every 3rd item gets a marker)
    final hasRecentMarker = _items.any((item) =>
        item.type == SurfaceItemType.chronology &&
        _items.indexOf(item) > _items.length - 4);
    if (!hasRecentMarker && _items.isNotEmpty) {
      final now = DateTime.now();
      final timeStr =
          '${now.hour.toString().padLeft(2, '0')}:${now.minute.toString().padLeft(2, '0')}';
      _items.add(_renderable(
        SurfaceItemType.chronology,
        '── $timeStr ──',
        timestamp: timeStr,
      ));
    }

    _items.add(_renderable(type, text,
        icon: icon,
        subtitle: subtitle,
        chips: chips,
        isUrgent: isUrgent));

    setState(() {});

    // Scroll to bottom after a short delay to let the animation start
    Future.delayed(const Duration(milliseconds: 50), _scrollToBottom);
  }

  void _scrollToBottom() {
    if (_scrollController.hasClients) {
      _scrollController.animateTo(
        _scrollController.position.maxScrollExtent,
        duration: const Duration(milliseconds: 300),
        curve: Curves.easeOut,
      );
    }
  }

  // ── Menu ──────────────────────────────────────────────────────────────────

  void _openMenu() {
    showModalBottomSheet(
      context: context,
      backgroundColor: _dockBg,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (_) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              _menuItem(Icons.inbox_outlined, 'Inbox', 'Pending decisions'),
              _menuItem(Icons.history_outlined, 'History', 'Full message archive'),
              _menuItem(Icons.settings_outlined, 'Settings', 'API key, base URL'),
            ],
          ),
        ),
      ),
    );
  }

  Widget _menuItem(IconData icon, String title, String subtitle) {
    return ListTile(
      leading: Icon(icon, color: _mutedText, size: 20),
      title: Text(title,
          style: const TextStyle(
              color: _primaryText, fontSize: 14, fontWeight: FontWeight.w500)),
      subtitle: Text(subtitle,
          style: const TextStyle(color: _mutedText, fontSize: 11)),
      onTap: () {
        Navigator.pop(context);
      },
    );
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    // Keyboard handling
    if (_isTyping) {
      // Show the surface with keyboard exposed
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
              final isActive = _isListening || _decisionChipSelected != null;
              return Container(
                width: 8,
                height: 8,
                decoration: BoxDecoration(
                  color: isActive
                      ? _accentGold
                      : _accentGold.withValues(alpha: _pulseAnimation.value),
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
            _ListeningIndicator(),
          ],
        ],
      ),
    );
  }

  // ── Surface list ──────────────────────────────────────────────────────────

  Widget _buildSurfaceList() {
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
        // Greeting
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
        // Starter chips
        _starterChip('📝  "Remind me to call Sunju"', () {
          _addItem(SurfaceItemType.userCapture,
              'Remind me to call Sunju about school',
              icon: '📝');
          Future.delayed(const Duration(milliseconds: 800), () {
            _addItem(SurfaceItemType.rhodeyResponse,
                '✅ Task created: Call Sunju\nre school. Due Monday.');
          });
        }),
        const SizedBox(height: 8),
        _starterChip('🗣️  "What\'s new today?"', () {
          _addItem(SurfaceItemType.userCapture, "What's new today?",
              icon: '🗣️');
          Future.delayed(const Duration(milliseconds: 800), () {
            _addItem(SurfaceItemType.rhodeyResponse,
                'Nothing urgent. Next thing is the\n'
                    'Qhord sync in a few hours. Equisoft\n'
                    'pricing is due Friday.');
          });
        }),
        const SizedBox(height: 8),
        _starterChip('📝  "Note down an idea"', () {
          _addItem(SurfaceItemType.userCapture,
              'Note down: explore AI-powered\nmeeting summaries for Qhord',
              icon: '📝');
          Future.delayed(const Duration(milliseconds: 800), () {
            _addItem(SurfaceItemType.rhodeyResponse,
                '📝 Noted: explore AI meeting\nsummaries for Qhord.');
          });
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

  /// Build an item with a one-shot slide + fade entry animation.
  /// Uses TweenAnimationBuilder which only fires when the widget is first created.
  Widget _buildItem(_RenderableItem item) {
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

  Widget _buildItemContent(_RenderableItem item) {
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

  Widget _buildGreeting(_RenderableItem item) {
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

  Widget _buildUserCapture(_RenderableItem item) {
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

  Widget _buildRhodeyResponse(_RenderableItem item) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 6, 20, 6),
      child: Text(
        item.text,
        style: const TextStyle(
          color: _primaryText,
          fontSize: 14,
          height: 1.5,
          fontWeight: FontWeight.w400,
        ),
      ),
    );
  }

  // ── Decision card ─────────────────────────────────────────────────────────

  Widget _buildDecisionCard(_RenderableItem item) {
    final accentColor = item.isUrgent ? _accentAmber : _accentBlue;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: AnimatedSize(
        duration: const Duration(milliseconds: 300),
        curve: Curves.easeOut,
        alignment: Alignment.topCenter,
        child: AnimatedOpacity(
          duration: const Duration(milliseconds: 200),
          opacity: _decisionCardExpanded ? 1.0 : 0.0,
          child: _decisionCardExpanded
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
                              accent: chip == 'Approve' || chip == 'Add them'
                                  ? _accentGold
                                  : chip == 'Dismiss' || chip == 'Skip'
                                      ? _mutedText
                                      : _accentBlue,
                              onTap: () => _onDecisionChipTap(chip, item),
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

  Widget _buildChronology(_RenderableItem item) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Center(
        child: Text(
          '── ${item.timestamp ?? ''} ──',
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
            color: _isListening ? _accentGold.withValues(alpha: 0.15) : Colors.transparent,
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
                        ? _accentGold.withValues(alpha: 0.5)
                        : _cardBorder,
                  ),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      _isListening ? '🎤 Listening...' : '🎤  Tap to speak',
                      style: TextStyle(
                        color: _isListening ? _accentGold : _mutedText,
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
                child: Icon(Icons.keyboard_outlined, color: _mutedText, size: 20),
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
                    _addItem(SurfaceItemType.userCapture, value.trim(),
                        icon: '🗣️');
                    _textController.clear();
                    setState(() => _isTyping = false);
                    Future.delayed(const Duration(milliseconds: 800), () {
                      _addItem(SurfaceItemType.rhodeyResponse,
                          'Got it. I\'ll keep an eye on that.');
                    });
                  },
                  decoration: const InputDecoration(
                    hintText: 'Type a message...',
                    border: InputBorder.none,
                    contentPadding:
                        EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                    isDense: true,
                  ),
                  style: const TextStyle(color: _primaryText, fontSize: 14),
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
                  _addItem(SurfaceItemType.userCapture, value, icon: '🗣️');
                  _textController.clear();
                  setState(() => _isTyping = false);
                  Future.delayed(const Duration(milliseconds: 800), () {
                    _addItem(SurfaceItemType.rhodeyResponse,
                        'Got it. I\'ll keep an eye on that.');
                  });
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
            // Close keyboard
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

// ── Internal renderable item model (with animation metadata) ─────────────────

class _RenderableItem {
  int id;
  SurfaceItemType type;
  String text;
  String? icon;
  String? subtitle;
  String? timestamp;
  List<String>? chips;
  bool isUrgent;

  _RenderableItem({
    required this.id,
    required this.type,
    required this.text,
    this.icon,
    this.subtitle,
    this.timestamp,
    this.chips,
    this.isUrgent = false,
  });
}
