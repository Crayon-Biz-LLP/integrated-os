import 'package:flutter/material.dart';
import '../models/decision_item.dart';
import '../theme/app_theme.dart';

class DecisionCard extends StatelessWidget {
  final DecisionItem item;
  final VoidCallback? onApprove;
  final VoidCallback? onReject;
  final VoidCallback? onEdit;
  final VoidCallback? onMerge;

  /// Opens the compose sheet to reply directly into the chat (Beeper send).
  final VoidCallback? onReply;

  /// When true the card renders in batch-selection mode: a checkbox appears,
  /// tapping the card toggles selection, and the action buttons are replaced
  /// by a selection hint. Used by the Inbox batch approve/reject flow.
  final bool selectionMode;

  /// Whether this card is currently selected (only meaningful in selection
  /// mode).
  final bool selected;

  /// Called when the card is tapped in selection mode.
  final VoidCallback? onToggleSelect;

  const DecisionCard({
    super.key,
    required this.item,
    this.onApprove,
    this.onReject,
    this.onEdit,
    this.onMerge,
    this.onReply,
    this.selectionMode = false,
    this.selected = false,
    this.onToggleSelect,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: selectionMode ? onToggleSelect : null,
      behavior: HitTestBehavior.opaque,
      child: Container(
        margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
        decoration: BoxDecoration(
          color: selected ? AppTheme.greenBg : AppTheme.surface,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
            color: selected ? AppTheme.green : AppTheme.border,
            width: selected ? 1.5 : 1,
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Header
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 12, 14, 8),
              child: Row(
                children: [
                  if (selectionMode) ...[
                    _buildSelectionCheck(),
                    const SizedBox(width: 8),
                  ],
                  Text(item.typeIcon, style: const TextStyle(fontSize: 14)),
                  const SizedBox(width: 6),
                  Text(_typeLabel, style: AppTheme.monoLabel),
                  const Spacer(),
                  if (item.confidence != null)
                    Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 6,
                        vertical: 2,
                      ),
                      decoration: BoxDecoration(
                        color: AppTheme.accentBg,
                        borderRadius: BorderRadius.circular(4),
                      ),
                      child: Text(
                        '${(item.confidence! * 100).toInt()}%',
                        style: AppTheme.monoCaption.copyWith(
                          color: AppTheme.accent,
                        ),
                      ),
                    ),
                  const SizedBox(width: 6),
                  Text(item.urgencyIcon, style: const TextStyle(fontSize: 10)),
                ],
              ),
            ),

            // Title
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 14),
              child: Text(
                item.title,
                style: AppTheme.title.copyWith(fontSize: 14),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              ),
            ),

            // Description
            if (item.description != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(14, 4, 14, 0),
                child: Text(
                  item.description!,
                  style: AppTheme.bodySmall.copyWith(fontSize: 12),
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                ),
              ),

            // Actions — replaced by a selection hint while in selection mode
            if (selectionMode)
              Padding(
                padding: const EdgeInsets.fromLTRB(14, 4, 14, 12),
                child: Text(
                  selected ? 'Selected — tap to deselect' : 'Tap to select',
                  style: AppTheme.caption.copyWith(
                    color: selected ? AppTheme.green : AppTheme.textTertiary,
                    fontSize: 11,
                  ),
                ),
              ),
            if (!selectionMode)
              Padding(
                padding: const EdgeInsets.fromLTRB(8, 8, 8, 8),
                child: Row(
                  children: [
                    _ActionButton(
                      label: 'Approve',
                      icon: Icons.check_circle_outline,
                      color: AppTheme.green,
                      onTap: onApprove,
                    ),
                    if (onEdit != null) ...[
                      const SizedBox(width: 6),
                      _ActionButton(
                        label: 'Edit',
                        icon: Icons.edit_outlined,
                        color: AppTheme.accent,
                        onTap: onEdit,
                      ),
                    ],
                    if (onReply != null) ...[
                      const SizedBox(width: 6),
                      _ActionButton(
                        label: 'Reply',
                        icon: Icons.reply,
                        color: AppTheme.accent,
                        onTap: onReply,
                      ),
                    ],
                    if (onMerge != null) ...[
                      const SizedBox(width: 6),
                      _ActionButton(
                        label: 'Merge',
                        icon: Icons.call_merge,
                        // Accent (distinct from Approve's green); never collides
                        // with Edit since Edit only renders on edge cards.
                        color: AppTheme.accent,
                        onTap: onMerge,
                      ),
                    ],
                    const Spacer(),
                    _ActionButton(
                      label: 'Reject',
                      icon: Icons.close,
                      color: AppTheme.textTertiary,
                      onTap: onReject,
                    ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }

  /// Small square checkbox shown at the start of the header in selection mode.
  Widget _buildSelectionCheck() {
    return Container(
      width: 20,
      height: 20,
      decoration: BoxDecoration(
        color: selected ? AppTheme.green : Colors.transparent,
        borderRadius: BorderRadius.circular(6),
        border: Border.all(
          color: selected ? AppTheme.green : AppTheme.textTertiary,
          width: 1.5,
        ),
      ),
      child: selected
          ? const Icon(Icons.check, size: 14, color: Colors.white)
          : null,
    );
  }

  String get _typeLabel {
    switch (item.type) {
      case DecisionType.clarification:
        return 'CLARIFICATION';
      case DecisionType.person:
        return item.personBadge;
      case DecisionType.edge:
        return 'NEW EDGE';
      case DecisionType.email:
        return 'EMAIL';
      case DecisionType.whatsapp:
        return item.viaBeeper ? 'BEEPER' : 'WHATSAPP';
      case DecisionType.call:
        return 'CALL';
      case DecisionType.teams:
        return 'TEAMS';
      case DecisionType.merge:
        return 'MERGE PROPOSAL';
    }
  }
}

class _ActionButton extends StatelessWidget {
  final String label;
  final IconData icon;
  final Color color;
  final VoidCallback? onTap;

  const _ActionButton({
    required this.label,
    required this.icon,
    required this.color,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(8),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: color.withValues(alpha: 0.3), width: 1),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 14, color: color),
              const SizedBox(width: 4),
              Text(
                label,
                style: AppTheme.caption.copyWith(
                  color: color,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
