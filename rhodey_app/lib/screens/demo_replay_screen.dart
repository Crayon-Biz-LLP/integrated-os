import 'package:flutter/material.dart';
import '../services/api_service.dart';
import '../services/persona.dart';
import '../theme/app_theme.dart';

/// Replay of the onboarding "Try it now" demo (M10) — reachable from
/// Settings > Help. Lets users who have been using the app re-experience
/// what Rhodey does: tap-to-try cards run scripted messages through the REAL
/// pipeline via /api/demo/message, so a demo task lands on the board, a demo
/// note links to a person, and a demo query reads THEIR graph — stamped
/// demo-owned and cleanable (Settings > Demo > "Clear onboarding demo
/// items").
///  /// Mirrors the onboarding flow's demo step (onboarding_flow.dart — the
  /// "Try it now" step): same scripts, same tap-to-try cards, same mini
  /// thread. KEEP THESE TWO IN SYNC when the demo scripts/cards change. The
  /// "first person" is resolved from the tenant's LIVE graph (their nodes
  /// exist post-onboarding) instead of the onboarding form, with the same
  /// person-neutral fallback — never a phantom name.
class DemoReplayScreen extends StatefulWidget {
  const DemoReplayScreen({super.key});

  @override
  State<DemoReplayScreen> createState() => _DemoReplayScreenState();
}

class _DemoReplayScreenState extends State<DemoReplayScreen> {
  final _api = ApiService();

  /// The person used by the demo script — the first PERSON node in the
  /// tenant's live graph (they added people during onboarding, so the node
  /// exists), else null (person-neutral scripts — no phantom names).
  String? _person;

  // Demo chat state — the tap-to-try actions and the mini thread they build
  // (role: user|rhodey, text, tag).
  final List<Map<String, String>> _demoChat = [];
  bool _demoSending = false;
  final Set<String> _demoDone = {};
  final _demoScroll = ScrollController();

  @override
  void initState() {
    super.initState();
    _resolveDemoPerson();
  }

  @override
  void dispose() {
    _demoScroll.dispose();
    super.dispose();
  }

  /// Resolves the first live person node. Best-effort: any failure leaves
  /// [_person] null and the demo falls back to person-neutral scripts.
  Future<void> _resolveDemoPerson() async {
    try {
      final r = await _api.getLiveGraphNodes(limit: 200);
      if (!mounted || !r.success) return;
      final rows = r.data ?? const [];
      for (final n in rows) {
        if ((n['type'] as String? ?? '') == 'person') {
          final name = (n['label'] as String? ?? '').trim();
          if (name.isNotEmpty) {
            setState(() => _person = name);
            return;
          }
        }
      }
    } catch (_) {
      // Person-neutral fallback.
    }
  }

  /// Runs ONE demo message through the REAL pipeline (inline), appending
  /// the user message + Rhodey's reply (+ intent tag) to the mini thread.
  /// Returns true when the demo message was accepted by the server.
  Future<bool> _runDemoMessage(String text, {String? tag}) async {
    if (_demoSending) return false;
    setState(() {
      _demoSending = true;
      _demoChat.add({'role': 'user', 'text': text});
    });
    final r = await _api.sendDemoMessage(text);
    if (!mounted) return false;
    final ok = r.success && r.data is Map;
    final reply = ok
        ? ((r.data as Map)['response'] as String?)?.trim() ?? 'Got it.'
        : 'That didn\'t go through — ${r.error}.';
    setState(() {
      _demoSending = false;
      _demoChat.add({'role': 'rhodey', 'text': reply, 'tag': ok ? (tag ?? '') : ''});
    });
    _demoScrollToBottom();
    return ok;
  }

  /// Runs one scripted demo card — marked done ONLY when it actually went
  /// through, so a failed tap can be retried.
  Future<void> _tryDemoAction(String key, String text, String tag) async {
    final ok = await _runDemoMessage(text, tag: tag);
    if (ok && mounted) setState(() => _demoDone.add(key));
    if (ok && mounted) _demoScrollToBottom();
  }

  /// Scrolls the demo thread to the newest message so the reply to the card
  /// you just tapped is always visible — no hunting for the chat at the bottom.
  void _demoScrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_demoScroll.hasClients) return;
      _demoScroll.animateTo(
        _demoScroll.position.maxScrollExtent,
        duration: const Duration(milliseconds: 320),
        curve: Curves.easeOutCubic,
      );
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.background,
      appBar: AppBar(
        title: const Text('Try it now'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: AppTheme.textSecondary),
          onPressed: () => Navigator.pop(context),
        ),
      ),
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // Intro line — same framing as the onboarding demo step.
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
            child: Text(
              'Tap a card — Rhodey does it for real. Demo items stay marked '
              'and cleanable.',
              style: AppTheme.subGreetingStyle.copyWith(fontSize: 12.5),
            ),
          ),
          Expanded(
            child: ListView(
              controller: _demoScroll,
              padding: const EdgeInsets.symmetric(horizontal: 16),
              children: [
                ..._demoActionCards(),
                const SizedBox(height: 18),
                if (_demoChat.isNotEmpty) ...[
                  Text(
                    'Your mini thread',
                    style: TextStyle(
                      color: AppTheme.textTertiary.withValues(alpha: 0.8),
                      fontSize: 11,
                      letterSpacing: 0.6,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 10),
                  ..._demoChat.map((m) => _demoBubble(m)),
                  if (_demoSending)
                    ...[const SizedBox(height: 8), _demoBubble({'role': 'rhodey', 'text': '…', 'tag': ''})],
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }

  List<Widget> _demoActionCards() {
    // Person-specific scripts when the tenant has a person node (exists
    // post-onboarding); person-neutral scripts otherwise.
    final person = _person;
    final taskText = person != null
        ? 'Remind me to call $person about the hiring plan tomorrow morning'
        : 'Remind me to send the hiring plan draft tomorrow morning';
    final doneText = person != null
        ? 'The $person call is done'
        : 'The hiring plan draft is done';
    final noteText = person != null
        ? '$person said the offer letter is out'
        : 'The offer letter is out for the new hire';
    final taskKey = 'task';
    final doneKey = 'done';
    final noteKey = 'note';
    final queryKey = 'query';
    // The "up next" card — the first card that is not done AND can be tapped
    // right now. Highlighting it removes the "what do I tap next?" guesswork.
    String? nextKey;
    for (final k in [taskKey, doneKey, noteKey, queryKey]) {
      if (_demoDone.contains(k)) continue;
      if (k == doneKey && !_demoDone.contains(taskKey)) continue;
      nextKey = k;
      break;
    }
    return [
      _demoCard(
        icon: '📋',
        label: 'Add a task',
        message: taskText,
        expect: '→ a task lands on your ${PersonaStore.current.today} board',
        done: _demoDone.contains(taskKey),
        isNext: nextKey == taskKey,
        onTap: () => _tryDemoAction(taskKey, taskText, '📋 Task created'),
      ),
      const SizedBox(height: 10),
      _demoCard(
        icon: '✅',
        label: 'Close it',
        message: doneText,
        expect: '→ that task closes — the board updates',
        done: _demoDone.contains(doneKey),
        enabled: _demoDone.contains(taskKey),
        isNext: nextKey == doneKey,
        onTap: () => _tryDemoAction(doneKey, doneText, '✅ Closed'),
      ),
      const SizedBox(height: 10),
      _demoCard(
        icon: '📝',
        label: 'Drop a note',
        message: noteText,
        expect: person != null ? '→ a note, linked to $person' : '→ a note, filed for later',
        done: _demoDone.contains(noteKey),
        isNext: nextKey == noteKey,
        onTap: () => _tryDemoAction(noteKey, noteText, '📝 Noted'),
      ),
      const SizedBox(height: 10),
      _demoCard(
        icon: '❓',
        label: 'Ask it something',
        message: 'Who is in my world?',
        expect: '→ answered from YOUR world',
        done: _demoDone.contains(queryKey),
        isNext: nextKey == queryKey,
        onTap: () => _tryDemoAction(queryKey, 'Who is in my world?', '❓ Answered'),
      ),
    ];
  }

  Widget _demoCard({
    required String icon,
    required String label,
    required String message,
    required String expect,
    required bool done,
    bool enabled = true,
    bool isNext = false,
    required VoidCallback onTap,
  }) {
    final canRun = enabled && !done && !_demoSending;
    return Container(
      decoration: BoxDecoration(
        color: done ? AppTheme.surfaceAlt.withValues(alpha: 0.5) : AppTheme.surface,
        borderRadius: BorderRadius.circular(AppTheme.cardRadius),
        border: Border.all(
          color: isNext && !done
              ? AppTheme.accent
              : done
                  ? AppTheme.border
                  : enabled
                      ? AppTheme.accent.withValues(alpha: 0.25)
                      : AppTheme.border.withValues(alpha: 0.4),
          width: isNext && !done ? 1.5 : 1,
        ),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(AppTheme.cardRadius),
        onTap: canRun ? onTap : null,
        child: Padding(
          padding: const EdgeInsets.all(13),
          child: Row(
            children: [
              Text(icon, style: const TextStyle(fontSize: 20)),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Text(
                          label,
                          style: const TextStyle(
                            color: AppTheme.textPrimary,
                            fontSize: 13.5,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        const SizedBox(width: 8),
                        if (done)
                          const Text('✓ tried',
                              style: TextStyle(
                                color: AppTheme.green,
                                fontSize: 10.5,
                              ))
                        else if (isNext && !_demoSending)
                          Text('↑ up next',
                              style: TextStyle(
                                color: AppTheme.accent,
                                fontSize: 10.5,
                                fontWeight: FontWeight.w600,
                              ))
                        else if (!enabled)
                          Text('do #1 first',
                              style: TextStyle(
                                color: AppTheme.textTertiary.withValues(alpha: 0.7),
                                fontSize: 10.5,
                              )),
                      ],
                    ),
                    const SizedBox(height: 4),
                    Text(
                      message,
                      style: TextStyle(
                        color: done
                            ? AppTheme.textTertiary
                            : AppTheme.textSecondary,
                        fontSize: 12,
                        fontStyle: FontStyle.italic,
                      ),
                    ),
                    const SizedBox(height: 3),
                    Text(
                      expect,
                      style: TextStyle(
                        color: done
                            ? AppTheme.textTertiary.withValues(alpha: 0.6)
                            : AppTheme.textTertiary,
                        fontSize: 11,
                      ),
                    ),
                  ],
                ),
              ),
              if (!done)
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
                  decoration: BoxDecoration(
                    color: canRun ? AppTheme.accent : AppTheme.textMuted,
                    borderRadius: BorderRadius.circular(AppTheme.controlRadius),
                  ),
                  child: Text(
                    canRun ? 'Try it' : '…',
                    style: const TextStyle(
                      color: Color(0xFF161510),
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _demoBubble(Map<String, String> m) {
    final isUser = m['role'] == 'user';
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.only(bottom: 8),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
        constraints: const BoxConstraints(maxWidth: 300),
        decoration: BoxDecoration(
          color: isUser ? AppTheme.userBubble : AppTheme.botBubble,
          borderRadius: BorderRadius.circular(14),
          border: Border.all(
            color: isUser ? AppTheme.border : AppTheme.borderLight,
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              m['text'] ?? '',
              style: const TextStyle(color: AppTheme.textPrimary, fontSize: 12.5, height: 1.4),
            ),
            if (!isUser && (m['tag'] ?? '').isNotEmpty) ...[
              const SizedBox(height: 5),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(
                  color: AppTheme.accentBg,
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Text(
                  m['tag']!,
                  style: const TextStyle(color: AppTheme.champagne, fontSize: 10),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
