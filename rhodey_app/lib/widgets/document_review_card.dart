import 'package:flutter/material.dart';
import '../theme/app_theme.dart';

/// A card displayed in the conversation feed when a document is uploaded
/// and parsed into structured items. The user can select/deselect items
/// and confirm to batch-create tasks/events/notes.
class DocumentReviewCard extends StatefulWidget {
  final Map<String, dynamic> breakdown;
  final int? documentId;
  final String? filename;
  final Function(List<Map<String, dynamic>> selectedItems) onConfirm;
  final VoidCallback? onSkip;

  const DocumentReviewCard({
    super.key,
    required this.breakdown,
    this.documentId,
    this.filename,
    required this.onConfirm,
    this.onSkip,
  });

  @override
  State<DocumentReviewCard> createState() => _DocumentReviewCardState();
}

class _DocumentReviewCardState extends State<DocumentReviewCard> {
  late List<_DocumentItem> _items;
  bool _confirmed = false;

  @override
  void initState() {
    super.initState();
    _items = _parseItems();
  }

  List<_DocumentItem> _parseItems() {
    final actions = widget.breakdown['suggested_actions'] as List? ?? [];
    return actions.map((action) {
      return _DocumentItem(
        type: action['type'] ?? 'task',
        title: action['title'] ?? '',
        owner: action['owner'],
        deadline: action['deadline'],
        date: action['date'],
        orgHint: action['org_hint'],
        description: action['description'],
        selected: true, // All selected by default
      );
    }).toList();
  }

  void _toggleItem(int index) {
    setState(() {
      _items[index].selected = !_items[index].selected;
    });
  }

  void _confirm() {
    final selected = _items
        .where((item) => item.selected)
        .map((item) => item.toMap())
        .toList();
    if (selected.isEmpty) {
      widget.onSkip?.call();
      return;
    }
    setState(() => _confirmed = true);
    widget.onConfirm(selected);
  }

  IconData _typeIcon(String type) {
    switch (type) {
      case 'task':
        return Icons.check_circle_outline;
      case 'event':
        return Icons.event_outlined;
      case 'note':
        return Icons.note_outlined;
      default:
        return Icons.circle_outlined;
    }
  }

  Color _typeColor(String type) {
    switch (type) {
      case 'task':
        return AppTheme.accent;
      case 'event':
        return AppTheme.green;
      case 'note':
        return AppTheme.amber;
      default:
        return AppTheme.textTertiary;
    }
  }

  @override
  Widget build(BuildContext context) {
    final summary = widget.breakdown['summary'] as String? ?? '';
    final docType = widget.breakdown['document_type'] as String? ?? 'document';
    final keyFacts = widget.breakdown['key_facts'] as Map<String, dynamic>? ?? {};
    final selectedCount = _items.where((i) => i.selected).length;

    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
      decoration: BoxDecoration(
        color: AppTheme.surface,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppTheme.border),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Header
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
            decoration: BoxDecoration(
              color: AppTheme.surfaceAlt,
              borderRadius: const BorderRadius.vertical(top: Radius.circular(12)),
              border: Border(
                bottom: BorderSide(color: AppTheme.border),
              ),
            ),
            child: Row(
              children: [
                const Icon(Icons.description_outlined, size: 18, color: AppTheme.accent),
                const SizedBox(width: 8),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        widget.filename ?? 'Document',
                        style: AppTheme.bodySmall.copyWith(
                          fontWeight: FontWeight.w600,
                          fontSize: 13,
                        ),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                      Text(
                        docType.replaceAll('_', ' ').toUpperCase(),
                        style: AppTheme.caption.copyWith(
                          color: AppTheme.textTertiary,
                          fontSize: 10,
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),

          // Summary
          if (summary.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 10, 14, 4),
              child: Text(
                summary,
                style: AppTheme.bodySmall.copyWith(
                  color: AppTheme.textSecondary,
                  fontSize: 12,
                  height: 1.4,
                ),
              ),
            ),

          // Key Facts (if any)
          if (keyFacts.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 4, 14, 8),
              child: Wrap(
                spacing: 6,
                runSpacing: 4,
                children: keyFacts.entries.take(5).map((e) {
                  return Container(
                    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                    decoration: BoxDecoration(
                      color: AppTheme.surfaceAlt,
                      borderRadius: BorderRadius.circular(6),
                    ),
                    child: Text(
                      '${e.key}: ${e.value}',
                      style: AppTheme.caption.copyWith(
                        fontSize: 10,
                        color: AppTheme.textTertiary,
                      ),
                    ),
                  );
                }).toList(),
              ),
            ),

          // Divider
          const Divider(height: 1, thickness: 1, color: AppTheme.border),

          // Items list
          if (!_confirmed) ...[
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 8, 14, 4),
              child: Text(
                'Suggested Actions ($selectedCount/${_items.length})',
                style: AppTheme.caption.copyWith(
                  fontWeight: FontWeight.w600,
                  fontSize: 11,
                  color: AppTheme.textSecondary,
                ),
              ),
            ),
            ...List.generate(_items.length, (index) {
              final item = _items[index];
              return InkWell(
                onTap: () => _toggleItem(index),
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
                  child: Row(
                    children: [
                      Icon(
                        item.selected
                            ? Icons.check_circle
                            : Icons.circle_outlined,
                        size: 18,
                        color: item.selected
                            ? _typeColor(item.type)
                            : AppTheme.textTertiary,
                      ),
                      const SizedBox(width: 10),
                      Icon(
                        _typeIcon(item.type),
                        size: 14,
                        color: _typeColor(item.type),
                      ),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              item.title,
                              style: AppTheme.bodySmall.copyWith(
                                fontSize: 13,
                                decoration: item.selected
                                    ? null
                                    : TextDecoration.lineThrough,
                                color: item.selected
                                    ? AppTheme.textPrimary
                                    : AppTheme.textTertiary,
                              ),
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                            ),
                            if (item.owner != null || item.deadline != null)
                              Text(
                                [
                                  if (item.owner != null) '👤 ${item.owner}',
                                  if (item.deadline != null) '📅 ${item.deadline}',
                                  if (item.date != null) '📅 ${item.date}',
                                ].join(' · '),
                                style: AppTheme.caption.copyWith(
                                  fontSize: 10,
                                  color: AppTheme.textTertiary,
                                ),
                              ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              );
            }),

            // Confirm button
            Padding(
              padding: const EdgeInsets.all(12),
              child: Row(
                children: [
                  if (widget.onSkip != null)
                    Expanded(
                      child: TextButton(
                        onPressed: widget.onSkip,
                        child: Text(
                          'Skip',
                          style: AppTheme.bodySmall.copyWith(
                            color: AppTheme.textTertiary,
                          ),
                        ),
                      ),
                    ),
                  if (widget.onSkip != null) const SizedBox(width: 8),
                  Expanded(
                    child: ElevatedButton(
                      onPressed: selectedCount > 0 ? _confirm : null,
                      style: ElevatedButton.styleFrom(
                        backgroundColor: AppTheme.accent,
                        foregroundColor: Colors.white,
                        padding: const EdgeInsets.symmetric(vertical: 10),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(8),
                        ),
                      ),
                      child: Text(
                        'Create $selectedCount ${selectedCount == 1 ? 'Item' : 'Items'}',
                        style: AppTheme.bodySmall.copyWith(
                          color: Colors.white,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],

          // Confirmed state
          if (_confirmed)
            Padding(
              padding: const EdgeInsets.all(14),
              child: Row(
                children: [
                  const Icon(Icons.check_circle, size: 18, color: AppTheme.green),
                  const SizedBox(width: 8),
                  Text(
                    'Creating ${_items.where((i) => i.selected).length} items...',
                    style: AppTheme.bodySmall.copyWith(
                      color: AppTheme.green,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

class _DocumentItem {
  final String type;
  final String title;
  final String? owner;
  final String? deadline;
  final String? date;
  final String? orgHint;
  final String? description;
  bool selected;

  _DocumentItem({
    required this.type,
    required this.title,
    this.owner,
    this.deadline,
    this.date,
    this.orgHint,
    this.description,
    this.selected = true,
  });

  Map<String, dynamic> toMap() {
    return {
      'type': type,
      'title': title,
      'owner': owner,
      'deadline': deadline,
      'date': date,
      'org_hint': orgHint,
      'description': description,
      'edited': false, // TODO: track if user edited
    };
  }
}
