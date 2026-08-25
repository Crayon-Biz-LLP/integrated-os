import 'package:flutter/material.dart';
import '../theme/app_theme.dart';
import 'merge_search_sheet.dart';

class SuggestionCard extends StatefulWidget {
  final Map<String, dynamic> breakdown;
  final String sourceType;
  final dynamic sourceId;
  final String? filename;
  final Function(List<Map<String, dynamic>> selectedTasks, List<Map<String, dynamic>> selectedEntities) onConfirm;
  final VoidCallback? onSkip;

  const SuggestionCard({
    super.key,
    required this.breakdown,
    required this.sourceType,
    required this.sourceId,
    this.filename,
    required this.onConfirm,
    this.onSkip,
  });

  @override
  State<SuggestionCard> createState() => _SuggestionCardState();
}

class _SuggestionItem {
  final String category; // 'task' or 'entity'
  final String type;
  String title; // label for entity
  String? owner;
  String? deadline;
  String? date;
  String? description;
  bool selected;
  bool edited;
  
  double? confidence;
  List<Map<String, dynamic>>? existingMatches;
  Map<String, dynamic>? mergeWith;
  Map<String, dynamic>? rawAction;

  _SuggestionItem({
    required this.category,
    required this.type,
    required this.title,
    this.owner,
    this.deadline,
    this.date,
    this.description,
    this.selected = true,
    this.edited = false,
    this.confidence,
    this.existingMatches,
    this.mergeWith,
    this.rawAction,
  }) {
    if (existingMatches != null && existingMatches!.isNotEmpty && mergeWith == null) {
      mergeWith = existingMatches![0];
    }
  }

  Map<String, dynamic> toMap() {
    if (category == 'task') {
      return {
        'type': type,
        'title': title,
        'owner': owner,
        'deadline': deadline,
        'date': date,
        'description': description,
        'edited': edited,
        'raw_action': rawAction,
      };
    } else {
      return {
        'type': type,
        'label': title,
        'edited': edited,
        'merge_with': mergeWith,
      };
    }
  }
}

class _SuggestionCardState extends State<SuggestionCard> {
  late List<_SuggestionItem> _items;
  bool _confirmed = false;

  @override
  void initState() {
    super.initState();
    _items = _parseItems();
  }

  List<_SuggestionItem> _parseItems() {
    final List<_SuggestionItem> items = [];
    final actions = widget.breakdown['suggested_actions'] as List? ?? [];
    for (var action in actions) {
      String op = action['operation'] ?? '';
      String t = action['human_label'] ?? action['title'] ?? '';
      if (t.isEmpty && action['params'] is Map) {
        t = action['params']['title'] ?? action['params']['content'] ?? '';
      }
      String mappedType = 'task';
      if (op == 'create_event' || op == 'delete_event') {
        mappedType = 'event';
      } else if (op == 'create_note') {
        mappedType = 'note';
      } else if (action['type'] != null) {
        mappedType = action['type'];
      }

      items.add(_SuggestionItem(
        category: 'task',
        type: mappedType,
        title: t,
        owner: action['owner'],
        deadline: action['deadline'] ?? (action['params'] is Map ? action['params']['deadline'] : null),
        date: action['date'] ?? (action['params'] is Map ? action['params']['time'] : null),
        description: action['description'] ?? (action['params'] is Map ? action['params']['notes'] : null),
        rawAction: action,
      ));
    }
    
    final entities = widget.breakdown['suggested_entities'] as List? ?? [];
    for (var entity in entities) {
      items.add(_SuggestionItem(
        category: 'entity',
        type: entity['type'] ?? 'concept',
        title: entity['label'] ?? '',
        confidence: (entity['confidence'] as num?)?.toDouble(),
        existingMatches: (entity['existing_matches'] as List?)?.cast<Map<String, dynamic>>(),
      ));
    }
    return items;
  }

  void _toggleItem(int index) {
    setState(() {
      _items[index].selected = !_items[index].selected;
    });
  }

  Future<void> _openMergeSheet(int index) async {
    final item = _items[index];
    final selected = await MergeSearchSheet.show(context, nodeType: item.type);
    if (selected != null) {
      setState(() {
        item.mergeWith = selected;
      });
    }
  }

  void _editItem(int index) {
    final item = _items[index];
    
    // Very simple inline edit dialog
    final titleCtrl = TextEditingController(text: item.title);
    
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppTheme.surface,
        title: Text('Edit ${item.category == 'task' ? 'Task' : 'Entity'}', style: AppTheme.bodySmall),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: titleCtrl,
              decoration: const InputDecoration(labelText: 'Title / Name'),
              style: AppTheme.bodySmall,
            ),
          ],
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          ElevatedButton(
            onPressed: () {
              setState(() {
                item.title = titleCtrl.text;

                item.edited = true;
              });
              Navigator.pop(ctx);
            },
            child: const Text('Save'),
          ),
        ],
      ),
    );
  }

  void _confirm() {
    final selectedTasks = _items.where((i) => i.selected && i.category == 'task').map((i) => i.toMap()).toList();
    final selectedEntities = _items.where((i) => i.selected && i.category == 'entity').map((i) => i.toMap()).toList();
    
    if (selectedTasks.isEmpty && selectedEntities.isEmpty) {
      widget.onSkip?.call();
      return;
    }
    
    setState(() => _confirmed = true);
    widget.onConfirm(selectedTasks, selectedEntities);
  }

  String _entityLabel(_SuggestionItem item) {
    final matches = item.existingMatches;
    if (matches != null && matches.isNotEmpty) {
      final scope = matches.first['scope'];
      if (scope == 'live') return 'Existing: ${matches.first['label']} (${item.type})';
      if (scope == 'pending') return 'Pending: ${matches.first['label']} (${item.type})';
    }
    if (item.mergeWith != null) return 'Merge: ${item.mergeWith!['label']} (${item.type})';
    return 'New ${item.type}';
  }

  Color _entityColor(_SuggestionItem item) {
    final matches = item.existingMatches;
    if (matches != null && matches.isNotEmpty) {
      final scope = matches.first['scope'];
      if (scope == 'live') return AppTheme.green;
      if (scope == 'pending') return AppTheme.amber;
    }
    if (item.mergeWith != null) return AppTheme.green;
    return AppTheme.blue;
  }

  IconData _typeIcon(String category, String type) {
    if (category == 'entity') {
      if (type == 'person') return Icons.person_outline;
      if (type == 'organization') return Icons.business_outlined;
      return Icons.tag;
    }
    switch (type) {
      case 'task': return Icons.check_circle_outline;
      case 'event': return Icons.event_outlined;
      case 'note': return Icons.note_outlined;
      default: return Icons.circle_outlined;
    }
  }

  Color _typeColor(String category, String type) {
    if (category == 'entity') return AppTheme.blue;
    switch (type) {
      case 'task': return AppTheme.accent;
      case 'event': return AppTheme.green;
      case 'note': return AppTheme.amber;
      default: return AppTheme.textTertiary;
    }
  }

  @override
  Widget build(BuildContext context) {
    final summary = widget.breakdown['summary'] as String? ?? '';
    final docType = widget.breakdown['document_type'] as String? ?? widget.sourceType;
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
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
            decoration: BoxDecoration(
              color: AppTheme.surfaceAlt,
              borderRadius: const BorderRadius.vertical(top: Radius.circular(12)),
              border: Border(bottom: BorderSide(color: AppTheme.border)),
            ),
            child: Row(
              children: [
                Icon(widget.sourceType == 'document' ? Icons.description_outlined : Icons.chat_bubble_outline, 
                     size: 18, color: AppTheme.accent),
                const SizedBox(width: 8),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        widget.filename ?? (widget.sourceType == 'document' ? 'Document' : 'Message Analysis'),
                        style: AppTheme.bodySmall.copyWith(fontWeight: FontWeight.w600, fontSize: 13),
                        maxLines: 1, overflow: TextOverflow.ellipsis,
                      ),
                      Text(
                        docType.replaceAll('_', ' ').toUpperCase(),
                        style: AppTheme.caption.copyWith(color: AppTheme.textTertiary, fontSize: 10),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          if (summary.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 10, 14, 4),
              child: Text(
                summary,
                style: AppTheme.bodySmall.copyWith(color: AppTheme.textSecondary, fontSize: 12, height: 1.4),
              ),
            ),
          const Divider(height: 1, thickness: 1, color: AppTheme.border),
          if (!_confirmed) ...[
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 8, 14, 4),
              child: Text(
                'Suggested Actions & Entities ($selectedCount/${_items.length})',
                style: AppTheme.caption.copyWith(fontWeight: FontWeight.w600, fontSize: 11, color: AppTheme.textSecondary),
              ),
            ),
            ...List.generate(_items.length, (index) {
              final item = _items[index];
              return InkWell(
                onTap: () => _toggleItem(index),
                onLongPress: () => _editItem(index),
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
                  child: Row(
                    children: [
                      Icon(
                        item.selected ? Icons.check_circle : Icons.circle_outlined,
                        size: 18,
                        color: item.selected ? _typeColor(item.category, item.type) : AppTheme.textTertiary,
                      ),
                      const SizedBox(width: 10),
                      Icon(_typeIcon(item.category, item.type), size: 14, color: _typeColor(item.category, item.type)),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              item.title,
                              style: AppTheme.bodySmall.copyWith(
                                fontSize: 13,
                                decoration: item.selected ? null : TextDecoration.lineThrough,
                                color: item.selected ? AppTheme.textPrimary : AppTheme.textTertiary,
                              ),
                              maxLines: 2, overflow: TextOverflow.ellipsis,
                            ),
                            if (item.category == 'task' && item.owner != null)
                              Text(
                                '👤 ${item.owner}',
                                style: AppTheme.caption.copyWith(fontSize: 10, color: AppTheme.textTertiary),
                              ),
                            if (item.category == 'entity')
                              Row(
                                children: [
                                  Expanded(
                                    child: Text(
                                      _entityLabel(item),
                                      style: AppTheme.caption.copyWith(fontSize: 10, color: _entityColor(item)),
                                      overflow: TextOverflow.ellipsis,
                                    ),
                                  ),
                                  if (item.confidence != null)
                                    Row(
                                      mainAxisSize: MainAxisSize.min,
                                      children: [
                                        Container(
                                          width: 6,
                                          height: 6,
                                          margin: const EdgeInsets.only(right: 4),
                                          decoration: BoxDecoration(
                                            shape: BoxShape.circle,
                                            color: item.confidence! > 0.8 ? AppTheme.green : (item.confidence! > 0.5 ? AppTheme.amber : AppTheme.red),
                                          ),
                                        ),
                                        Text('${(item.confidence! * 100).toInt()}%', style: AppTheme.caption.copyWith(fontSize: 10)),
                                      ],
                                    ),
                                ],
                              ),
                          ],
                        ),
                      ),
                      if (item.category == 'entity')
                        IconButton(
                          icon: const Icon(Icons.call_merge, size: 16),
                          color: item.mergeWith != null ? AppTheme.accent : AppTheme.textTertiary,
                          onPressed: () => _openMergeSheet(index),
                          padding: EdgeInsets.zero,
                          constraints: const BoxConstraints(),
                        ),
                      IconButton(
                        icon: const Icon(Icons.edit_outlined, size: 16),
                        color: AppTheme.textTertiary,
                        onPressed: () => _editItem(index),
                        padding: EdgeInsets.zero,
                        constraints: const BoxConstraints(),
                      ),
                    ],
                  ),
                ),
              );
            }),
            Padding(
              padding: const EdgeInsets.all(12),
              child: Row(
                children: [
                  if (widget.onSkip != null)
                    Expanded(
                      child: TextButton(
                        onPressed: widget.onSkip,
                        child: Text('Skip', style: AppTheme.bodySmall.copyWith(color: AppTheme.textTertiary)),
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
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                      ),
                      child: Text(
                        'Create $selectedCount ${selectedCount == 1 ? 'Item' : 'Items'}',
                        style: AppTheme.bodySmall.copyWith(color: Colors.white, fontWeight: FontWeight.w600),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
          if (_confirmed)
            Padding(
              padding: const EdgeInsets.all(14),
              child: Row(
                children: [
                  const Icon(Icons.check_circle, size: 18, color: AppTheme.green),
                  const SizedBox(width: 8),
                  Text(
                    'Creating ${_items.where((i) => i.selected).length} items...',
                    style: AppTheme.bodySmall.copyWith(color: AppTheme.green, fontWeight: FontWeight.w500),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}
