import 'dart:async';

import 'package:flutter/material.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';

/// Bottom sheet that searches existing live graph nodes (type-ahead) so the
/// user can pick a merge target — mirrors the web dashboard's "Merge into
/// existing node" dropdown. Pops with the selected `{id, label, type}` map.
///
/// Shared by the Inbox (person cards) and the home focal card so both
/// surfaces expose the same merge picker.
class MergeSearchSheet extends StatefulWidget {
  /// Optional node type filter (e.g. 'person', 'organization') so results are
  /// scoped to nodes of the same kind as the source.
  final String? nodeType;

  const MergeSearchSheet({super.key, this.nodeType});

  /// Opens the sheet and resolves with the selected `{id, label, type}` map,
  /// or null when dismissed. Shared by the Inbox and the home focal card so
  /// both surfaces present the identical merge picker.
  static Future<Map<String, dynamic>?> show(
    BuildContext context, {
    String? nodeType,
  }) {
    return showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      backgroundColor: AppTheme.surface,
      isScrollControlled: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (ctx) => MergeSearchSheet(nodeType: nodeType),
    );
  }

  @override
  State<MergeSearchSheet> createState() => _MergeSearchSheetState();
}

class _MergeSearchSheetState extends State<MergeSearchSheet> {
  final _api = ApiService();
  final _controller = TextEditingController();
  Timer? _debounce;
  List<Map<String, dynamic>> _results = [];
  bool _searching = false;
  bool _hasSearched = false;

  /// Monotonic sequence guard so a slow earlier response can never overwrite
  /// the results of a newer query (classic stale-response race in type-ahead).
  int _searchSeq = 0;

  @override
  void dispose() {
    _debounce?.cancel();
    _controller.dispose();
    super.dispose();
  }

  void _onQueryChanged(String value) {
    _debounce?.cancel();
    final q = value.trim();
    if (q.length < 2) {
      setState(() {
        _results = [];
        _searching = false;
        _hasSearched = false;
      });
      return;
    }
    setState(() => _searching = true);
    final seq = ++_searchSeq;
    _debounce = Timer(const Duration(milliseconds: 300), () async {
      final result =
          await _api.searchGraphNodes(q, type: widget.nodeType);
      if (!mounted || seq != _searchSeq) return;
      setState(() {
        _results = result.success ? result.data! : [];
        _searching = false;
        _hasSearched = true;
      });
    });
  }

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: EdgeInsets.only(
          left: 20,
          right: 20,
          top: 12,
          bottom: MediaQuery.of(context).viewInsets.bottom + 16,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 36,
              height: 4,
              decoration: BoxDecoration(
                color: AppTheme.textTertiary.withValues(alpha: 0.3),
                borderRadius: BorderRadius.circular(2),
              ),
              margin: const EdgeInsets.only(bottom: 16),
            ),
            Text(
              'Merge into existing node',
              style: AppTheme.title.copyWith(fontSize: 15),
            ),
            const SizedBox(height: 4),
            Text(
              'Search your knowledge graph, then tap a node to merge into it.',
              style: AppTheme.bodySmall.copyWith(
                color: AppTheme.textTertiary, fontSize: 12,
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _controller,
              autofocus: true,
              onChanged: _onQueryChanged,
              decoration: InputDecoration(
                hintText: 'Type to search…',
                prefixIcon: const Icon(Icons.search, size: 18),
                suffixIcon: _searching
                    ? const Padding(
                        padding: EdgeInsets.all(10),
                        child: SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        ),
                      )
                    : null,
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide: BorderSide(color: AppTheme.border),
                ),
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 14, vertical: 12,
                ),
                isDense: true,
              ),
              style: AppTheme.body,
            ),
            const SizedBox(height: 12),
            if (_hasSearched && !_searching && _results.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 24),
                child: Center(
                  child: Text(
                    'No matching nodes found',
                    style: AppTheme.bodySmall.copyWith(
                      color: AppTheme.textTertiary, fontSize: 12,
                    ),
                  ),
                ),
              )
            else
              Flexible(
                child: ListView(
                  shrinkWrap: true,
                  children: [
                    for (final r in _results)
                      Material(
                        color: Colors.transparent,
                        child: InkWell(
                          borderRadius: BorderRadius.circular(10),
                          onTap: () => Navigator.pop(context, r),
                          child: Padding(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 8, vertical: 10,
                            ),
                            child: Row(
                              children: [
                                const Icon(Icons.account_tree_outlined,
                                    size: 16, color: AppTheme.textTertiary),
                                const SizedBox(width: 10),
                                Expanded(
                                  child: Text(
                                    r['label'] as String? ?? '',
                                    style: AppTheme.body.copyWith(fontSize: 14),
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                  ),
                                ),
                                const SizedBox(width: 8),
                                Text(
                                  r['type'] as String? ?? '',
                                  style: AppTheme.monoCaption
                                      .copyWith(color: AppTheme.textTertiary),
                                ),
                              ],
                            ),
                          ),
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
}
