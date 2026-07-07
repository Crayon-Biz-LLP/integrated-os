import 'package:flutter/material.dart';
import '../models/decision_item.dart';
import '../theme/app_theme.dart';

class DecisionCard extends StatelessWidget {
  final DecisionItem item;
  final VoidCallback? onApprove;
  final VoidCallback? onReject;
  final VoidCallback? onEdit;

  const DecisionCard({
    super.key,
    required this.item,
    this.onApprove,
    this.onReject,
    this.onEdit,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      decoration: BoxDecoration(
        color: AppTheme.surface,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppTheme.border, width: 1),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Header
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 12, 14, 8),
            child: Row(
              children: [
                Text(
                  item.typeIcon,
                  style: const TextStyle(fontSize: 14),
                ),
                const SizedBox(width: 6),
                Text(
                  _typeLabel,
                  style: AppTheme.caption.copyWith(
                    color: AppTheme.textTertiary,
                    letterSpacing: 0.5,
                  ),
                ),
                const Spacer(),
                if (item.confidence != null)
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                    decoration: BoxDecoration(
                      color: AppTheme.accentBg,
                      borderRadius: BorderRadius.circular(4),
                    ),
                    child: Text(
                      '${(item.confidence! * 100).toInt()}%',
                      style: AppTheme.caption.copyWith(
                        color: AppTheme.accent,
                        fontSize: 10,
                      ),
                    ),
                  ),
                const SizedBox(width: 6),
                Text(
                  item.urgencyIcon,
                  style: const TextStyle(fontSize: 10),
                ),
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

          // Actions
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
                const Spacer(),
                _ActionButton(
                  label: 'Dismiss',
                  icon: Icons.close,
                  color: AppTheme.textTertiary,
                  onTap: onReject,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  String get _typeLabel {
    switch (item.type) {
      case DecisionType.clarification:
        return 'CLARIFICATION';
      case DecisionType.person:
        return 'NEW PERSON';
      case DecisionType.edge:
        return 'NEW EDGE';
      case DecisionType.email:
        return 'EMAIL';
      case DecisionType.whatsapp:
        return 'WHATSAPP';
      case DecisionType.call:
        return 'CALL';
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
            border: Border.all(
              color: color.withValues(alpha: 0.3),
              width: 1,
            ),
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
