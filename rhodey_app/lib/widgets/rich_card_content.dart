import 'package:flutter/material.dart';
import '../theme/app_theme.dart';
import '../utils/markdown_spans.dart';

// ── Card data types ───────────────────────────────────────────-

/// Types of rich cards that can be rendered inline in CONVERSATION.
enum CardType { task, taskDone, note, approval, briefing, clarification, workflow }

/// Parsed card data extracted from a Rhodey message.
class CardData {
  final CardType type;
  final String title;
  final String? subtitle;
  final String? fullText;

  /// When the message was created — used by the Briefing card to decide
  /// whether it opens expanded (today) or folded (older beats).
  final DateTime? timestamp;

  const CardData({
    required this.type,
    required this.title,
    this.subtitle,
    this.fullText,
    this.timestamp,
  });
}

/// Parse a Rhodey response text and return structured card data if it
/// matches an actionable pattern. Returns null for plain text.
///
/// Detected patterns:
///   "✅ Closed: [title]"                          → taskDone (passive)
///   "📋 Task saved: [title]" or "📋 [title]"      → task (Mark Done)
///   "📝 Note saved: [title]"                      → note (passive)
///   "✅ Approved [type]: [name]"                   → approval (Undo)
CardData? parseMessageToCardData(String text) {
  final trimmed = text.trim();
  if (trimmed.isEmpty) return null;

  // Normalize: collapse multiple newlines into a single separator
  final firstLine = trimmed.split('\n').first.trim();

  // Pattern 1: starts with ✅ and contains "Closed:" or "closed:"
  if (firstLine.startsWith('✅')) {
    // Task completion
    final closedMatch = RegExp(r'Closed:\s*(.+)', caseSensitive: false)
        .firstMatch(firstLine);
    if (closedMatch != null) {
      final title = closedMatch.group(1)!.trim();
      return CardData(type: CardType.taskDone, title: title, fullText: trimmed);
    }

    // Approval confirmation: "✅ Approved [type]: [name]" or "✅ Person: [name] Approved"
    final approvedMatch = RegExp(r'Approved\s+\w+:\s*(.+)', caseSensitive: false)
        .firstMatch(firstLine);
    if (approvedMatch != null) {
      final name = approvedMatch.group(1)!.trim();
      return CardData(
        type: CardType.approval,
        title: name,
        subtitle: 'Approved',
        fullText: trimmed,
      );
    }

    // Task mutations (backend verb table — core/lib/rhodey_voice.py render_acks).
    // A reschedule / metadata update keeps the task OPEN — never a taskDone card.
    final rescheduledMatch = RegExp(r'Rescheduled:\s*(.+?)(?:\s*→\s*.+)?$',
        caseSensitive: false).firstMatch(firstLine);
    if (rescheduledMatch != null) {
      return CardData(
        type: CardType.task,
        title: rescheduledMatch.group(1)!.trim(),
        fullText: trimmed,
      );
    }

    final updatedMatch = RegExp(r'(?:Recurrence updated|Updated):\s*(.+)',
        caseSensitive: false).firstMatch(firstLine);
    if (updatedMatch != null) {
      return CardData(
        type: CardType.task,
        title: updatedMatch.group(1)!.trim(),
        fullText: trimmed,
      );
    }

    // Generic ✅ prefix — only when the line actually reads like an approval
    // (explicit "approved" word or a trailing ✓/✅ marker). Without this gate,
    // any unknown ✅ line (e.g. "✅ Rescheduled: …") rendered as an approval
    // card with a bogus approve/undo affordance.
    final genericApproval = RegExp(r'✅\s+(.+?)(?:\s+[✓✅])?$')
        .firstMatch(firstLine);
    final looksApprovalLike = RegExp(r'\bapproved\b', caseSensitive: false)
            .hasMatch(firstLine) ||
        RegExp(r'[✓✅]\s*$').hasMatch(firstLine);
    if (genericApproval != null && looksApprovalLike) {
      final title = genericApproval.group(1)!.trim();
      if (title.length > 5 && title.length < 80) {
        return CardData(type: CardType.approval, title: title, fullText: trimmed);
      }
    }
  }

  // Pattern 2: "📋 Task saved: [title]" or "📋 [title] saved"
  if (firstLine.startsWith('📋')) {
    final taskMatch = RegExp(r'Task saved:\s*(.+)', caseSensitive: false)
        .firstMatch(firstLine);
    if (taskMatch != null) {
      return CardData(type: CardType.task, title: taskMatch.group(1)!.trim(), fullText: trimmed);
    }

    // "📋 <title>" — generic task mention (likely a task nudge)
    final genericTask = firstLine.replaceAll('📋', '').trim();
    if (genericTask.isNotEmpty && genericTask.length > 5 && genericTask.length < 100) {
      // Check it's not a completion receipt
      if (!genericTask.contains('✓') && !genericTask.contains('✅')) {
        return CardData(type: CardType.task, title: genericTask, fullText: trimmed);
      }
    }
  }

  // Pattern 3: "📝 ..." — task creations ("On your list") and note saves
  if (firstLine.startsWith('📝')) {
    final onYourListMatch = RegExp(r'On your list:\s*(.+?)(?:\s*—\s*.+)?$',
        caseSensitive: false).firstMatch(firstLine);
    if (onYourListMatch != null) {
      return CardData(
        type: CardType.task,
        title: onYourListMatch.group(1)!.trim(),
        fullText: trimmed,
      );
    }

    final noteMatch = RegExp(r'(?:Note saved|Logged):\s*(.+)',
        caseSensitive: false).firstMatch(firstLine);
    if (noteMatch != null) {
      return CardData(
        type: CardType.note,
        title: noteMatch.group(1)!.trim(),
        fullText: trimmed,
      );
    }
  }

  // Pattern 4: "📅 Scheduled: <title> — <date>" (create_event)
  if (firstLine.startsWith('📅')) {
    final scheduledMatch = RegExp(r'Scheduled:\s*(.+?)(?:\s*[—–-]\s*.+)?$',
        caseSensitive: false).firstMatch(firstLine);
    if (scheduledMatch != null) {
      return CardData(
        type: CardType.task,
        title: scheduledMatch.group(1)!.trim(),
        fullText: trimmed,
      );
    }
  }

  return null;
}

/// Intent-aware card resolution.
///
/// The backend labels every conversation row with a structured `intent`
/// (BRIEFING / CLARIFICATION / WORKFLOW_RESOLUTION / ...). That label is
/// authoritative when present — it picks the card without guessing at text.
/// Live messages (sent via the chat input) carry no intent yet, so they fall
/// back to the text-pattern parser above.
CardData? resolveCardData({
  String? intent,
  required String text,
  String? title,
  DateTime? timestamp,
}) {
  final trimmed = text.trim();
  if (trimmed.isEmpty) return null;
  final firstLine = trimmed.split('\n').first.trim();

  switch (intent) {
    case 'BRIEFING':
    case 'DAILY_BRIEF':
      // Rhodey initiated a pulse briefing → collapsible Moment card.
      return CardData(
        type: CardType.briefing,
        title: firstLine,
        fullText: trimmed,
        timestamp: timestamp,
      );
    case 'CLARIFICATION':
      // Rhodey is waiting on your input → prompt card with reply chips.
      return CardData(
        type: CardType.clarification,
        title: firstLine,
        fullText: trimmed,
        timestamp: timestamp,
      );
    case 'WORKFLOW_REPLY':
    case 'WORKFLOW_RESOLUTION':
      // A workflow decision was resolved → compact confirmation card.
      return CardData(
        type: CardType.workflow,
        title: firstLine,
        fullText: trimmed,
        timestamp: timestamp,
      );
    case 'TASK_CREATED':
    case 'TASK_RESCHEDULED':
    case 'TASK_UPDATED':
    case 'RECURRENCE_UPDATED':
    case 'EVENT_SCHEDULED':
      // Ack intents — the task is OPEN (rescheduled/updated tasks stay
      // actionable), so the card carries Mark-Done. The bare task name comes
      // structured from the backend (title) — never parsed from the voice text.
      return CardData(
        type: CardType.task,
        title: title ?? firstLine,
        fullText: trimmed,
      );
    case 'TASK_CLOSED':
    case 'RECURRENCE_CANCELLED':
      return CardData(
        type: CardType.taskDone,
        title: title ?? firstLine,
        fullText: trimmed,
      );
    case 'NOTE_LOGGED':
      return CardData(
        type: CardType.note,
        title: title ?? firstLine,
        fullText: trimmed,
      );
    default:
      // No structured intent (live messages, RESPONSE, QUERY, ...) — rely on
      // the text-pattern parser (task / taskDone / note / approval).
      return parseMessageToCardData(text);
  }
}

// ── Widgets ─────────────────────────────────────────────────────

/// Renders a rich inline card inside the conversation.
/// Falls back to showing the original text if not a recognized card.
class RichCardContent extends StatelessWidget {
  final CardData cardData;
  final VoidCallback? onMarkDone;
  final VoidCallback? onUndo;
  final VoidCallback? onTap;

  /// Quick-reply chips to render inside a Clarification prompt card.
  final List<String>? quickReplies;
  final ValueChanged<String>? onQuickReply;

  const RichCardContent({
    super.key,
    required this.cardData,
    this.onMarkDone,
    this.onUndo,
    this.onTap,
    this.quickReplies,
    this.onQuickReply,
  });

  @override
  Widget build(BuildContext context) {
    Widget card;
    switch (cardData.type) {
      case CardType.task:
        card = _TaskInlineCard(
          title: cardData.title,
          subtitle: cardData.subtitle,
          onMarkDone: onMarkDone,
        );
        break;
      case CardType.taskDone:
        card = _TaskDoneInlineCard(title: cardData.title);
        break;
      case CardType.note:
        card = _NoteInlineCard(title: cardData.title);
        break;
      case CardType.approval:
        card = _ApprovalInlineCard(
          title: cardData.title,
          subtitle: cardData.subtitle,
          onUndo: onUndo,
        );
        break;
      case CardType.briefing:
        card = _BriefingInlineCard(
          title: cardData.title,
          fullText: cardData.fullText ?? cardData.title,
          timestamp: cardData.timestamp,
        );
        break;
      case CardType.clarification:
        card = _ClarificationInlineCard(
          title: cardData.title,
          fullText: cardData.fullText ?? cardData.title,
          quickReplies: quickReplies,
          onQuickReply: onQuickReply,
        );
        break;
      case CardType.workflow:
        card = _WorkflowInlineCard(
          title: cardData.title,
          fullText: cardData.fullText ?? cardData.title,
        );
        break;
    }

    // Wrap in GestureDetector for TTS on tap if callback provided
    if (onTap != null) {
      card = GestureDetector(
        onTap: onTap,
        child: card,
      );
    }

    return card;
  }
}

/// A compact card for a saved task with a Mark Done button.
class _TaskInlineCard extends StatelessWidget {
  final String title;
  final String? subtitle;
  final VoidCallback? onMarkDone;

  const _TaskInlineCard({
    required this.title,
    this.subtitle,
    this.onMarkDone,
  });

  @override
  Widget build(BuildContext context) {
    return _CardContainer(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 22,
                height: 22,
                decoration: BoxDecoration(
                  color: AppTheme.amberBg,
                  borderRadius: BorderRadius.circular(5),
                ),
                child: const Center(child: Text('📋', style: TextStyle(fontSize: 12))),
              ),
              const SizedBox(width: 8),
              Text('TASK', style: AppTheme.statusDot.copyWith(
                color: AppTheme.amber, fontSize: 9, letterSpacing: 1.2,
              )),
            ],
          ),
          const SizedBox(height: 6),
          Text(title, style: AppTheme.body.copyWith(
            fontSize: 13, fontWeight: FontWeight.w600,
          ), maxLines: 2, overflow: TextOverflow.ellipsis),
          if (subtitle != null) ...[
            const SizedBox(height: 2),
            Text(subtitle!, style: AppTheme.caption.copyWith(
              color: AppTheme.amber, fontSize: 10,
            )),
          ],
          const SizedBox(height: 8),
          Row(
            children: [
              _SmallActionButton(
                label: '✓ Mark done',
                color: AppTheme.green,
                onTap: onMarkDone,
              ),
              const SizedBox(width: 6),
              Text('Tap to complete', style: AppTheme.caption.copyWith(
                color: AppTheme.textMuted, fontSize: 9,
              )),
            ],
          ),
        ],
      ),
    );
  }
}

/// Confirmation card for a completed task (passive display).
class _TaskDoneInlineCard extends StatelessWidget {
  final String title;

  const _TaskDoneInlineCard({required this.title});

  @override
  Widget build(BuildContext context) {
    return _CardContainer(
      color: AppTheme.greenBg,
      borderColor: AppTheme.green.withValues(alpha: 0.2),
      child: Row(
        children: [
          Container(
            width: 22, height: 22,
            decoration: BoxDecoration(
              color: AppTheme.green.withValues(alpha: 0.15),
              borderRadius: BorderRadius.circular(5),
            ),
            child: const Center(
              child: Icon(Icons.check_circle, size: 14, color: AppTheme.green),
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(title, style: AppTheme.body.copyWith(
              fontSize: 12, color: AppTheme.green,
            ), maxLines: 1, overflow: TextOverflow.ellipsis),
          ),
          Text('Done', style: AppTheme.caption.copyWith(
            color: AppTheme.green, fontSize: 10, fontWeight: FontWeight.w600,
          )),
        ],
      ),
    );
  }
}

/// Confirmation card for a saved note (passive display).
class _NoteInlineCard extends StatelessWidget {
  final String title;

  const _NoteInlineCard({required this.title});

  @override
  Widget build(BuildContext context) {
    return _CardContainer(
      child: Row(
        children: [
          const Text('📝', style: TextStyle(fontSize: 14)),
          const SizedBox(width: 8),
          Expanded(
            child: Text(title, style: AppTheme.body.copyWith(
              fontSize: 12,
            ), maxLines: 1, overflow: TextOverflow.ellipsis),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
            decoration: BoxDecoration(
              color: AppTheme.accentBg,
              borderRadius: BorderRadius.circular(4),
            ),
            child: Text('Note saved', style: AppTheme.caption.copyWith(
              color: AppTheme.accent, fontSize: 9,
            )),
          ),
        ],
      ),
    );
  }
}

/// Confirmation card for a decision approval with Undo button.
class _ApprovalInlineCard extends StatelessWidget {
  final String title;
  final String? subtitle;
  final VoidCallback? onUndo;

  const _ApprovalInlineCard({
    required this.title,
    this.subtitle,
    this.onUndo,
  });

  @override
  Widget build(BuildContext context) {
    // GREEN: a decision was made — acknowledgement, not a question.
    return _CardContainer(
      color: AppTheme.greenBg,
      borderColor: AppTheme.green.withValues(alpha: 0.25),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Text('✅', style: TextStyle(fontSize: 12)),
              const SizedBox(width: 6),
              Text('APPROVED', style: AppTheme.statusDot.copyWith(
                color: AppTheme.green, fontSize: 9, letterSpacing: 1.2,
              )),
              const Spacer(),
              if (onUndo != null)
                _SmallActionButton(
                  label: '↩ Undo',
                  color: AppTheme.accent,
                  onTap: onUndo,
                ),
            ],
          ),
          const SizedBox(height: 4),
          Text(title, style: AppTheme.body.copyWith(
            fontSize: 13, fontWeight: FontWeight.w600,
          )),
          if (subtitle != null)
            Text(subtitle!, style: AppTheme.caption.copyWith(
              color: AppTheme.textTertiary, fontSize: 10,
            )),
        ],
      ),
    );
  }
}

// ── Shared helpers ─────────────────────────────────────────────-

/// The day's pulse briefing as a collapsible "moment" card.
///
/// Rhodey initiates these (Morning / Afternoon / Closing / Intel), so they
/// render as day-beats with a time chip and structured sections — NOT as
/// ordinary chat bubbles. Tap the header to fold/unfold.
class _BriefingInlineCard extends StatefulWidget {
  final String title;
  final String fullText;
  final DateTime? timestamp;

  const _BriefingInlineCard({
    required this.title,
    required this.fullText,
    this.timestamp,
  });

  @override
  State<_BriefingInlineCard> createState() => _BriefingInlineCardState();
}

class _BriefingInlineCardState extends State<_BriefingInlineCard> {
  late bool _expanded;

  @override
  void initState() {
    super.initState();
    // Today's briefing arrives expanded; older beats fold so History scrolls
    // fast instead of surfacing a wall of old briefings.
    final raw = widget.timestamp;
    final today = DateTime.now();
    final ts = raw?.toLocal();
    _expanded = ts == null ||
        (ts.year == today.year && ts.month == today.month && ts.day == today.day);
  }

  String _timeChip() {
    final t = widget.title.toLowerCase();
    if (t.contains('morning')) return '☀️';
    if (t.contains('afternoon')) return '🌤️';
    // Renamed headlines (voice pass): "Friday wrap-up." / "Wrap-up." and
    // "Night wind-down." — keep the chips matching the new names.
    if (t.contains('wrap') || t.contains('closing') || t.contains('sign off')) return '🌙';
    if (t.contains('night') || t.contains('wind') || t.contains('intel')) return '🧠';
    if (t.contains('weekend') || t.contains('chores')) return '🌿';
    if (t.contains('pre-monday') || t.contains('loading')) return '📈';
    return '📋';
  }

  String _dayLabel() {
    final raw = widget.timestamp;
    if (raw == null) return '';
    // Server timestamps are UTC (timestamptz) — always render device-local so
    // the card's clock matches the phone's.
    final ts = raw.toLocal();
    final today = DateTime.now();
    if (ts.year == today.year && ts.month == today.month && ts.day == today.day) {
      final hh = ts.hour.toString().padLeft(2, '0');
      final mm = ts.minute.toString().padLeft(2, '0');
      return 'Today · $hh:$mm';
    }
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return '${months[ts.month - 1]} ${ts.day}';
  }

  /// Section labels the Pulse briefing engine actually emits. Mirror of the
  /// server's authoritative vocabulary — `core/pulse/briefing.py`
  /// `_BARE_SECTION_NAMES` plus the M9.3 board skeleton
  /// (`core/services/briefing_sections.py`): Schedule · Done · Work · Home ·
  /// Church · Ideas · Stale Loops. Restricting the vocabulary (plus the
  /// bold-wrapped fallback below) prevents a short bullet like "🔴 Call Bob"
  /// from being misclassified as a section header.
  static const _sectionLabels = {
    'home', 'work', 'church', 'ideas', 'schedule', 'done',
    'stale', 'stale loops', 'backlog', 'weekend recon', 'urgent', 'important', 'vault',
  };

  /// True when the whole trimmed line is `**bold**`-wrapped. The pulse prompt
  /// mandates `**bold**` for section breaks and nothing else, so a fully bold
  /// line is a section header even when the label isn't in [_sectionLabels]
  /// (e.g. a creative "**Priority Watch**") — splitting beats fusing it into
  /// the intro paragraph as a run-on.
  static final RegExp _wholeLineBold = RegExp(r'^\*\*.+\*\*$');

  /// Parse the briefing body into (header, bulletLines) section groups.
  ///
  /// A section header is one of:
  ///  - a line that is entirely `**bold**`-wrapped (e.g. "**Work**"),
  ///  - a known [_sectionLabels] label, optionally `**`-wrapped and/or emoji
  ///    prefixed ("🚀 Work", "**Done**", "⛪ Church", bare "Home").
  /// Anything else under a header is a bullet. The raw line is kept so the
  /// renderer paints bold headers bold — markers are stripped only for
  /// matching.
  List<_BriefingSection> _sections() {
    final sections = <_BriefingSection>[];
    _BriefingSection? current;
    for (final line in widget.fullText.split('\n')) {
      final t = line.trim();
      if (t.isEmpty) continue;
      // Strip all `**` markers for matching only — handles "**Work**",
      // "🚀 **Work**" and "🏠 **Home**" alike. Bullets keep their "- "
      // prefix so they never match the emoji-header pattern below.
      final candidate = t.replaceAll('**', '').trim();
      final headerMatch =
          RegExp(r'^([^\x00-\x7F]{1,4})\s+([A-Za-z][A-Za-z ]{0,20})$')
              .firstMatch(candidate);
      final label = headerMatch?.group(2) ?? candidate;
      if (_wholeLineBold.hasMatch(t) ||
          _sectionLabels.contains(label.trim().toLowerCase())) {
        current = _BriefingSection(t, []);
        sections.add(current);
        continue;
      }
      if (current != null) {
        current.items.add(t);
      }
    }
    return sections;
  }

  /// Intro paragraph — everything between the title line and the first
  /// section header (e.g. "The signal's quiet on the update front...").
  ///
  /// Bullet-prefixed and fully bold-wrapped lines are section artifacts, not
  /// narrative — if one shows up before the first recognised header (an
  /// unrecognised section), stop collecting so the intro never fuses headers
  /// and bullets into a run-on paragraph.
  String _intro() {
    final lines = widget.fullText.split('\n')
        .map((l) => l.trim())
        .where((l) => l.isNotEmpty)
        .toList();
    if (lines.isEmpty) return '';
    final sections = _sections();
    final firstHeader = sections.isEmpty ? null : sections.first.header;
    final buf = <String>[];
    for (final line in lines) {
      if (line == firstHeader) break;
      if (line.startsWith('-') ||
          line.startsWith('•') ||
          _wholeLineBold.hasMatch(line)) {
        break;
      }
      buf.add(line);
    }
    // Skip the title line itself. Keep the opening's own line breaks (one
    // per line) instead of fusing a multi-line opening into a run-on
    // paragraph — Telegram shows the opening paragraphs on separate lines.
    return buf.skip(1).join('\n');
  }

  @override
  Widget build(BuildContext context) {
    final sections = _sections();
    final intro = _intro();
    // GOLD: Rhodey initiates these (Morning / Afternoon / Closing / Intel) —
    // the "day beats" stand apart from transactional confirmations.
    return _CardContainer(
      color: AppTheme.champagneMuted,
      borderColor: AppTheme.champagne.withValues(alpha: 0.35),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          InkWell(
            onTap: () => setState(() => _expanded = !_expanded),
            borderRadius: BorderRadius.circular(8),
            child: Row(
              children: [
                Container(
                  width: 24,
                  height: 24,
                  decoration: BoxDecoration(
                    color: AppTheme.champagne.withValues(alpha: 0.15),
                    borderRadius: BorderRadius.circular(6),
                  ),
                  child: Center(child: Text(_timeChip(), style: const TextStyle(fontSize: 12))),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text.rich(
                        TextSpan(
                          style: AppTheme.body.copyWith(
                            fontSize: 13,
                            fontWeight: FontWeight.w600,
                          ),
                          children: markdownBoldSpans(widget.title),
                        ),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                      if (_dayLabel().isNotEmpty)
                        Text(_dayLabel(), style: AppTheme.monoCaption.copyWith(
                          color: AppTheme.textSecondary,
                        )),
                    ],
                  ),
                ),
                Icon(
                  _expanded ? Icons.expand_less : Icons.expand_more,
                  size: 18,
                  color: AppTheme.textTertiary,
                ),
              ],
            ),
          ),
          if (_expanded) ...[
            const SizedBox(height: 6),
            // Sections found → intro + structured sections, each rendered
            // once. NO sections (plain-text briefing, or headers the parser
            // can't recognise) → render the full text ONCE. Never both: the
            // intro collects the whole body when there are no headers, so
            // showing it next to the fullText would duplicate the briefing
            // (the Aug-10 double-render regression). All body text goes
            // through the bold parser so `**Work**` paints as bold Work,
            // never literal asterisks.
            if (sections.isNotEmpty) ...[
              if (intro.isNotEmpty)
                Padding(
                  // Air between the opening and the first section — closer to
                  // Telegram's blank-line rhythm than the old 4px squeeze.
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Text.rich(
                    TextSpan(
                      style: AppTheme.body.copyWith(
                        color: AppTheme.textPrimary,
                      ),
                      children: markdownBoldSpans(intro),
                    ),
                  ),
                ),
              for (final section in sections) ...[
                Text.rich(
                  TextSpan(
                    style: AppTheme.body.copyWith(
                      fontWeight: FontWeight.w700,
                      color: AppTheme.champagne,
                    ),
                    children: markdownBoldSpans(section.header),
                  ),
                ),
                const SizedBox(height: 4),
                for (final item in section.items)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 4),
                    // No maxLines cap: bullets render in full like Telegram —
                    // a long task title is never cut off mid-thought.
                    child: Text.rich(
                      TextSpan(
                        style: AppTheme.body.copyWith(
                          color: AppTheme.textPrimary,
                        ),
                        children: markdownBoldSpans(item),
                      ),
                    ),
                  ),
                // Section-to-section gap — reads as the blank line Telegram
                // puts between sections.
                const SizedBox(height: 10),
              ],
            ] else
              Text.rich(
                TextSpan(
                  style: AppTheme.body.copyWith(
                    color: AppTheme.textPrimary,
                  ),
                  children: markdownBoldSpans(widget.fullText),
                ),
              ),
          ],
        ],
      ),
    );
  }
}

class _BriefingSection {
  final String header;
  final List<String> items;
  _BriefingSection(this.header, this.items);
}

/// A clarification ask — Rhodey is waiting on YOUR input. Renders as a
/// "waiting on you" prompt card with quick-reply chips when available.
class _ClarificationInlineCard extends StatelessWidget {
  final String title;
  final String? fullText;
  final List<String>? quickReplies;
  final ValueChanged<String>? onQuickReply;

  const _ClarificationInlineCard({
    required this.title,
    this.fullText,
    this.quickReplies,
    this.onQuickReply,
  });

  @override
  Widget build(BuildContext context) {
    final chips = quickReplies;
    return _CardContainer(
      color: AppTheme.amberBg,
      borderColor: AppTheme.amber.withValues(alpha: 0.35),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.help_outline, size: 14, color: AppTheme.amber),
              const SizedBox(width: 6),
              Text('WAITING ON YOU', style: AppTheme.statusDot.copyWith(
                color: AppTheme.amber, fontSize: 9, letterSpacing: 1.2,
              )),
            ],
          ),
          const SizedBox(height: 6),
          Text.rich(
            TextSpan(
              style: AppTheme.body.copyWith(fontSize: 12),
              children: markdownBoldSpans(fullText ?? title),
            ),
          ),
          if (chips != null && chips.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: chips.map((c) {
                return _SmallActionButton(
                  label: c,
                  color: AppTheme.amber,
                  onTap: onQuickReply == null ? null : () => onQuickReply!(c),
                );
              }).toList(),
            ),
          ],
        ],
      ),
    );
  }
}

/// A resolved workflow decision — compact confirmation card.
class _WorkflowInlineCard extends StatelessWidget {
  final String title;
  final String? fullText;

  const _WorkflowInlineCard({required this.title, this.fullText});

  @override
  Widget build(BuildContext context) {
    // GREEN: resolved confirmation.
    return _CardContainer(
      color: AppTheme.greenBg,
      borderColor: AppTheme.green.withValues(alpha: 0.25),
      child: Row(
        children: [
          const Icon(Icons.compare_arrows, size: 14, color: AppTheme.green),
          const SizedBox(width: 8),
          Expanded(
            child: Text(title, style: AppTheme.body.copyWith(
              fontSize: 12,
            ), maxLines: 2, overflow: TextOverflow.ellipsis),
          ),
          Text('RESOLVED', style: AppTheme.statusDot.copyWith(
            color: AppTheme.green, fontSize: 9, letterSpacing: 1.2,
          )),
        ],
      ),
    );
  }
}

class _CardContainer extends StatelessWidget {
  final Widget child;
  final Color? color;
  final Color? borderColor;

  const _CardContainer({
    required this.child,
    this.color,
    this.borderColor,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      // Horizontal padding matches the chat bubble's (16) so card text sits
      // on the exact same baseline as bubble text — one 32px text line runs
      // across bubbles, cards, and bullets in the conversation.
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
      decoration: BoxDecoration(
        color: color ?? AppTheme.surface,
        borderRadius: BorderRadius.circular(AppTheme.cardRadius),
        border: Border.all(
          color: borderColor ?? AppTheme.border,
          width: 1,
        ),
      ),
      child: child,
    );
  }
}

class _SmallActionButton extends StatelessWidget {
  final String label;
  final Color color;
  final VoidCallback? onTap;

  const _SmallActionButton({
    required this.label,
    required this.color,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(6),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(6),
            border: Border.all(color: color.withValues(alpha: 0.4), width: 1),
          ),
          child: Text(
            label,
            style: AppTheme.caption.copyWith(
              color: color,
              fontSize: 11,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ),
    );
  }
}
