import 'package:flutter/material.dart';
import '../services/persona.dart';
import '../theme/app_theme.dart';

/// How Rhodey works — the full reference (M9.8).
///
/// Reached from Settings → Help. Covers the OS at the level a new tenant
/// needs: the rhythm, the board, approvals, memory + learning, privacy, and
/// best practices. A short version of this runs as an onboarding step; this
/// screen is the always-available deep version.
class HowRhodeyWorksScreen extends StatelessWidget {
  const HowRhodeyWorksScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.background,
      appBar: AppBar(
        title: const Text('How Rhodey works'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: AppTheme.textSecondary),
          onPressed: () => Navigator.pop(context),
        ),
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(20, 8, 20, 40),
        children: [
          _Section(title: 'The idea', body: _ideaBody()),
          const _Section(title: 'Briefings — your rhythm', body:
            'Rhodey checks in with a briefing at times you pick when you set '
            'up — morning, midday, evening. Your admin can adjust the times '
            'anytime. Each briefing covers what\'s '
            'on your plate, what\'s new, what\'s stale, and what needs a '
            'decision. It works in your timezone. A "Not now" on a briefing '
            'snoozes it — it is not a silent reset.'),
          _Section(title: 'Your board — the surfaces', body: _surfacesBody()),
          const _Section(title: 'Approvals — how you stay in control', body:
            'When Rhodey learns something new — a person, a relationship, a '
            'link between ideas — it proposes, and you approve or reject. '
            'That approval is the signal: it is how Rhodey decides what '
            'earns a permanent place in your knowledge. Approving is not '
            'decoration; it teaches the OS what matters to you.'),
          const _Section(title: 'Memory — how it actually remembers', body:
            'Everything you send becomes a memory. Rhodey links related '
            'memories into a knowledge graph — people, organizations, and '
            'the connections between them. Important entities earn permanent '
            'pages; patterns across your days become context Rhodey carries '
            'into future briefings. You never need to file anything — the '
            'graph does it.'),
          const _Section(title: 'Learning — the loop', body:
            'Every decision you make trains Rhodey: approve, reject, snooze, '
            'or correct. Saying "not now" defers without forgetting. The '
            'more you engage, the sharper the judgment — what shows up on '
            'your board, what gets briefed, what gets proposed to you.'),
          const _Section(title: 'Privacy & your data', body:
            'Your world is scoped to you — your memories, your graph, your '
            'tasks. Nobody else can see it. Gmail, Calendar, and Tasks are '
            'read only after you connect Google and only with the '
            'permissions you grant. Your data stays yours.'),
          const _Section(title: 'Best practices', body:
            '• Feed it daily — even one line. Memory compounds.\n'
            '• Approve and reject honestly — that is the training signal.\n'
            '• Use "not now" freely — it respects your timing without '
            'resetting.\n'
            '• Keep your people and areas current — the board stays sharper.\n'
            '• Connect Google when you can — real mail and calendar make '
            'briefings concrete.'),
          const SizedBox(height: 20),
          Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: AppTheme.surfaceAlt,
              borderRadius: BorderRadius.circular(AppTheme.cardRadius),
              border: Border.all(color: AppTheme.border),
            ),
            child: const Text(
              'Rhodey works best when you talk to it like a person — short, '
              'plain, and honest. It is built to meet you where you are.',
              style: TextStyle(
                color: AppTheme.textSecondary,
                fontSize: 12.5,
                height: 1.5,
                fontStyle: FontStyle.italic,
              ),
            ),
          ),
        ],
      ),
    );
  }

  /// M15: the intro story follows the chosen persona — Chief of Staff keeps
  /// its metaphor; every other style gets the plain "memory for life" story.
  static String _ideaBody() {
    if (PersonaStore.current.id == 'chief_staff') {
      return 'Rhodey is a chief of staff in your pocket. It knows your world — '
          'the people, work, and life areas you told it about — exercises '
          'judgment about what matters now, and learns from every decision '
          'you make. You stay in control; it does the remembering and the '
          'connecting.';
    }
    return 'Rhodey is your memory for life. Tell it things once — people, '
        'dates, decisions, ideas — and it keeps the picture straight, spots '
        'what needs you, and checks in at times you choose. You stay in '
        'control; it does the remembering and the connecting.';
  }

  /// M15: the four surfaces use the persona's names for the same doors.
  static String _surfacesBody() {
    final p = PersonaStore.current;
    return '• ${p.today} — what matters right now: the focal card, active '
        'tasks, and your briefing.\n'
        '• ${p.inbox} — things Rhodey proposes for your attention: pending '
        'people, links, and decisions waiting on you.\n'
        '• ${p.entities} — the people and organizations Rhodey knows, and '
        'what it believes about each.\n'
        '• ${p.history} — the conversation: everything you and Rhodey said, '
        'merged into one thread.';
  }
}

class _Section extends StatelessWidget {
  final String title;
  final String body;
  const _Section({required this.title, required this.body});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 22),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title,
            style: const TextStyle(
              fontFamily: 'InstrumentSerif',
              fontSize: 21,
              color: AppTheme.textPrimary,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            body,
            style: TextStyle(
              color: AppTheme.textSecondary,
              fontSize: 13,
              height: 1.55,
            ),
          ),
        ],
      ),
    );
  }
}
