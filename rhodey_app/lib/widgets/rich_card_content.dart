import 'package:flutter/material.dart';
import '../theme/app_theme.dart';

// ── Card data types ───────────────────────────────────────────-

/// Types of rich cards that can be rendered inline in CONVERSATION.
enum CardType { task, taskDone, note, approval }

/// Parsed card data extracted from a Rhodey message.
class CardData {
  final CardType type;
  final String title;
  final String? subtitle;
  final String? fullText;

  const CardData({
    required this.type,
    required this.title,
    this.subtitle,
    this.fullText,
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

    // Generic ✅ prefix with approval-like content
    final genericApproval = RegExp(r'✅\s+(.+?)(?:\s+[✓✅])?$')
        .firstMatch(firstLine);
    if (genericApproval != null) {
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

  // Pattern 3: "📝 Note saved: [title]"
  if (firstLine.startsWith('📝')) {
    final noteMatch = RegExp(r'Note saved:\s*(.+)', caseSensitive: false)
        .firstMatch(firstLine);
    if (noteMatch != null) {
      return CardData(
        type: CardType.note,
        title: noteMatch.group(1)!.trim(),
        fullText: trimmed,
      );
    }
  }

  return null;
}

// ── Widgets ─────────────────────────────────────────────────────

/// Renders a rich inline card inside the conversation.
/// Falls back to showing the original text if not a recognized card.
class RichCardContent extends StatelessWidget {
  final CardData cardData;
  final VoidCallback? onMarkDone;
  final VoidCallback? onUndo;
  final VoidCallback? onTap;

  const RichCardContent({
    super.key,
    required this.cardData,
    this.onMarkDone,
    this.onUndo,
    this.onTap,
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
    return _CardContainer(
      color: AppTheme.accentBg,
      borderColor: AppTheme.accent.withValues(alpha: 0.2),
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
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: color ?? AppTheme.surface,
        borderRadius: BorderRadius.circular(10),
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
