import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';

/// Live knowledge-graph entities (people, orgs, concepts) — mirrors the web
/// UI's Decisions → Entities tab so node monitoring/merging works on-device.
///
/// Actions are destructive-by-intent, so every one is guarded by a confirm
/// dialog. All calls go through ApiService to the existing backend routes
/// (/api/graph-nodes/live, /api/graph-nodes/search, /api/graph-node-merge,
/// /api/graph-node/{id} PUT/PATCH/DELETE) — no backend changes needed.
class EntitiesScreen extends StatefulWidget {
  const EntitiesScreen({super.key});

  @override
  State<EntitiesScreen> createState() => _EntitiesScreenState();
}

class _EntitiesScreenState extends State<EntitiesScreen> {
  final _api = ApiService();
  final _search = TextEditingController();

  List<Map<String, dynamic>> _all = [];
  List<Map<String, dynamic>> _filtered = [];
  bool _loading = true;
  String _error = '';
  String _filterType = 'all';
  bool _busy = false;

  static const _types = [
    'all',
    'person',
    'organization',
    'concept',
    'place',
    'event',
    'animal',
    'emotional_state',
  ];

  @override
  void initState() {
    super.initState();
    _load();
    _search.addListener(_applyFilter);
  }

  @override
  void dispose() {
    _search.removeListener(_applyFilter);
    _search.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = '';
    });
    final result = await _api.getLiveGraphNodes();
    if (!mounted) return;
    setState(() {
      _loading = false;
      if (result.success) {
        _all = result.data ?? [];
        _applyFilter();
      } else {
        _error = result.error ?? 'Failed to load entities';
      }
    });
  }

  void _applyFilter() {
    final q = _search.text.trim().toLowerCase();
    setState(() {
      _filtered = _all.where((n) {
        final label = (n['label'] as String? ?? '').toLowerCase();
        final type = n['type'] as String? ?? '';
        final matchesType = _filterType == 'all' || type == _filterType;
        final matchesQuery = q.isEmpty || label.contains(q);
        return matchesType && matchesQuery;
      }).toList()
        ..sort((a, b) {
          final ta = (a['type'] as String? ?? '');
          final tb = (b['type'] as String? ?? '');
          if (ta != tb) return ta.compareTo(tb);
          return (a['label'] as String? ?? '').compareTo(b['label'] as String? ?? '');
        });
    });
  }

  Future<void> _refresh() async {
    await _load();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.background,
      appBar: AppBar(
        title: const Text('Entities'),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 16),
            child: Center(
              child: Text(
                '${_filtered.length} live',
                style: AppTheme.statusDot.copyWith(color: AppTheme.green),
              ),
            ),
          ),
        ],
      ),
      body: Column(
        children: [
          // Mutation progress (merge/rename/type/delete in flight)
          if (_busy)
            const LinearProgressIndicator(
              minHeight: 2,
              color: AppTheme.champagne,
              backgroundColor: AppTheme.border,
            ),
          // Search
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
            child: TextField(
              controller: _search,
              style: AppTheme.body,
              decoration: const InputDecoration(
                hintText: 'Search people, orgs, concepts…',
                prefixIcon: Icon(Icons.search, color: AppTheme.textTertiary, size: 20),
                isDense: true,
              ),
            ),
          ),
          // Type filter chips
          SizedBox(
            height: 40,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
              children: _types.map((t) {
                final selected = _filterType == t;
                return Padding(
                  padding: const EdgeInsets.only(right: 6),
                  child: ChoiceChip(
                    label: Text(t, style: AppTheme.chipStyle),
                    selected: selected,
                    onSelected: (_) {
                      setState(() => _filterType = t);
                      _applyFilter();
                    },
                    selectedColor: AppTheme.champagneMuted,
                    backgroundColor: AppTheme.surfaceAlt,
                    side: BorderSide(
                      color: selected ? AppTheme.champagne : AppTheme.border,
                    ),
                    showCheckmark: false,
                    labelStyle: TextStyle(
                      color: selected ? AppTheme.champagne : AppTheme.textSecondary,
                    ),
                  ),
                );
              }).toList(),
            ),
          ),
          // List
          Expanded(
            child: _loading
                ? const Center(child: CircularProgressIndicator(color: AppTheme.champagne))
                : _error.isNotEmpty
                    ? _buildError()
                    : _filtered.isEmpty
                        ? _buildEmpty()
                        : RefreshIndicator(
                            onRefresh: _refresh,
                            color: AppTheme.champagne,
                            child: ListView.builder(
                              padding: const EdgeInsets.fromLTRB(16, 8, 16, 80),
                              itemCount: _filtered.length,
                              itemBuilder: (_, i) => _buildNodeTile(_filtered[i]),
                            ),
                          ),
          ),
        ],
      ),
    );
  }

  Widget _buildError() {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.cloud_off, color: AppTheme.textMuted, size: 32),
            const SizedBox(height: 12),
            Text(_error, style: AppTheme.hintStyle, textAlign: TextAlign.center),
            const SizedBox(height: 16),
            OutlinedButton(onPressed: _load, child: const Text('Retry')),
          ],
        ),
      ),
    );
  }

  Widget _buildEmpty() {
    return Center(
      child: Text(
        'No entities found.',
        style: AppTheme.hintStyle,
      ),
    );
  }

  Widget _buildNodeTile(Map<String, dynamic> node) {
    final label = node['label'] as String? ?? 'Untitled';
    final type = node['type'] as String? ?? 'unknown';
    final createdAt = node['created_at'] as String? ?? '';

    final typeColor = switch (type) {
      'person' => AppTheme.green,
      'organization' => AppTheme.blue,
      'concept' => AppTheme.champagne,
      _ => AppTheme.textTertiary,
    };
    final typeIcon = switch (type) {
      'person' => Icons.person_outline,
      'organization' => Icons.business_outlined,
      'concept' => Icons.lightbulb_outline,
      'place' => Icons.place_outlined,
      'event' => Icons.event_outlined,
      _ => Icons.circle_outlined,
    };

    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      color: AppTheme.surface,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(10),
        side: const BorderSide(color: AppTheme.border),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: () => _showNodeActions(node),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: Row(
            children: [
              Container(
                width: 34,
                height: 34,
                decoration: BoxDecoration(
                  color: AppTheme.surfaceAlt,
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: AppTheme.border),
                ),
                child: Icon(typeIcon, color: typeColor, size: 18),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(label, style: AppTheme.bodyStyle, maxLines: 1, overflow: TextOverflow.ellipsis),
                    const SizedBox(height: 2),
                    Text(
                      createdAt.isNotEmpty ? '$type · ${_shortDate(createdAt)}' : type,
                      style: AppTheme.caption.copyWith(color: AppTheme.textSecondary),
                    ),
                  ],
                ),
              ),
              const Icon(Icons.more_horiz, color: AppTheme.textTertiary, size: 18),
            ],
          ),
        ),
      ),
    );
  }

  String _shortDate(String iso) {
    try {
      final dt = DateTime.parse(iso).toLocal();
      final now = DateTime.now();
      final sameYear = dt.year == now.year;
      final mm = dt.month.toString().padLeft(2, '0');
      final dd = dt.day.toString().padLeft(2, '0');
      return sameYear ? '$mm/$dd' : '$mm/$dd/${dt.year}';
    } catch (_) {
      return '';
    }
  }

  // ── Actions ──────────────────────────────────────────────────

  Future<void> _showNodeActions(Map<String, dynamic> node) async {
    if (_busy) return; // block re-entry while a mutation is in flight
    final label = node['label'] as String? ?? 'Untitled';

    await showModalBottomSheet(
      context: context,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const SizedBox(height: 12),
            Text(label, style: AppTheme.title, textAlign: TextAlign.center),
            const SizedBox(height: 4),
            Text('Node actions', style: AppTheme.caption),
            const SizedBox(height: 12),
            _ActionTile(
              icon: Icons.call_merge,
              label: 'Merge into existing…',
              color: AppTheme.champagne,
              onTap: () async {
                Navigator.pop(ctx);
                await _mergeFlow(node);
              },
            ),
            _ActionTile(
              icon: Icons.edit_outlined,
              label: 'Rename',
              onTap: () async {
                Navigator.pop(ctx);
                await _renameFlow(node);
              },
            ),
            _ActionTile(
              icon: Icons.swap_horiz,
              label: 'Change type',
              onTap: () async {
                Navigator.pop(ctx);
                await _typeFlow(node);
              },
            ),
            _ActionTile(
              icon: Icons.delete_outline,
              label: 'Delete',
              color: AppTheme.red,
              onTap: () async {
                Navigator.pop(ctx);
                await _deleteFlow(node);
              },
            ),
            const SizedBox(height: 12),
          ],
        ),
      ),
    );
  }

  /// Merge: pick a target via live search, then confirm.
  Future<void> _mergeFlow(Map<String, dynamic> node) async {
    final id = node['id'].toString();
    final label = node['label'] as String? ?? 'Untitled';

    final target = await _pickTargetNode(label, node['type'] as String?);
    if (target == null || !mounted) return;

    final ok = await _confirm(
      title: 'Merge "$label"?',
      message:
          '"$label" will be merged into "${target['label']}". Its edges and records move to the target, then it is archived. This cannot be undone.',
      confirmLabel: 'Merge',
      destructive: true,
    );
    if (ok != true || !mounted) return;

    setState(() => _busy = true);
    final result = await _api.mergeGraphNode(id, target['id'].toString());
    if (!mounted) return;
    setState(() => _busy = false);

    if (result.success) {
      _toast('Merged into ${target['label']}');
      await _load();
    } else {
      _toast(result.error ?? 'Merge failed', error: true);
    }
  }

  Future<Map<String, dynamic>?> _pickTargetNode(String excludeLabel, String? nodeType) async {
    final controller = TextEditingController();
    List<Map<String, dynamic>> results = [];
    Map<String, dynamic>? selected;

    await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      isScrollControlled: true,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (ctx) => Padding(
        padding: EdgeInsets.only(
          left: 16,
          right: 16,
          top: 16,
          bottom: MediaQuery.of(ctx).viewInsets.bottom + 16,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text('Merge into which node?', style: AppTheme.title),
            const SizedBox(height: 12),
            TextField(
              controller: controller,
              autofocus: true,
              style: AppTheme.body,
              decoration: const InputDecoration(
                hintText: 'Type to search live nodes…',
                isDense: true,
              ),
              onChanged: (q) async {
                if (q.trim().length < 2) {
                  setState(() {
                    results = [];
                  });
                  return;
                }
                final r = await _api.searchGraphNodes(q.trim(), type: nodeType);
                // Sheet may have been dismissed while the search was in flight.
                if (!mounted) return;
                if (!r.success) return;
                setState(() {
                  results = (r.data ?? [])
                      .where((n) => (n['label'] as String? ?? '') != excludeLabel)
                      .take(8)
                      .toList();
                });
              },
            ),
            const SizedBox(height: 8),
            Flexible(
              child: results.isEmpty
                  ? Padding(
                      padding: const EdgeInsets.all(16),
                      child: Text('Type at least 2 characters to search.',
                          style: AppTheme.hintStyle),
                    )
                  : ListView.builder(
                      shrinkWrap: true,
                      itemCount: results.length,
                      itemBuilder: (_, i) {
                        final n = results[i];
                        return ListTile(
                          dense: true,
                          title: Text(n['label'] as String? ?? '', style: AppTheme.body),
                          subtitle: Text(n['type'] as String? ?? '', style: AppTheme.caption),
                          onTap: () {
                            selected = n;
                            Navigator.pop(ctx, n);
                          },
                        );
                      },
                    ),
            ),
          ],
        ),
      ),
    );
    controller.dispose();
    return selected;
  }

  Future<void> _renameFlow(Map<String, dynamic> node) async {
    final id = node['id'].toString();
    final oldLabel = node['label'] as String? ?? '';
    final controller = TextEditingController(text: oldLabel);

    final newLabel = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppTheme.surface,
        title: const Text('Rename node'),
        content: TextField(
          controller: controller,
          autofocus: true,
          style: AppTheme.body,
          decoration: const InputDecoration(hintText: 'New label'),
          onSubmitted: (v) => Navigator.pop(ctx, v.trim()),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),
          TextButton(
            onPressed: () => Navigator.pop(ctx, controller.text.trim()),
            child: const Text('Save'),
          ),
        ],
      ),
    );
    if (newLabel == null || newLabel.isEmpty || newLabel == oldLabel || !mounted) return;

    setState(() => _busy = true);
    final result = await _api.renameGraphNode(id, newLabel);
    if (!mounted) return;
    setState(() => _busy = false);

    if (result.success) {
      _toast('Renamed to $newLabel');
      await _load();
    } else {
      _toast(result.error ?? 'Rename failed', error: true);
    }
  }

  Future<void> _typeFlow(Map<String, dynamic> node) async {
    final id = node['id'].toString();
    final label = node['label'] as String? ?? '';
    final current = node['type'] as String? ?? '';

    final newType = await showDialog<String>(
      context: context,
      builder: (ctx) => SimpleDialog(
        backgroundColor: AppTheme.surface,
        title: Text('Change type — $label', style: AppTheme.title),
        children: [
          for (final t in _types.where((t) => t != 'all'))
            SimpleDialogOption(
              onPressed: () => Navigator.pop(ctx, t),
              child: Row(
                children: [
                  Icon(
                    t == current ? Icons.radio_button_checked : Icons.radio_button_off,
                    color: t == current ? AppTheme.champagne : AppTheme.textTertiary,
                    size: 18,
                  ),
                  const SizedBox(width: 10),
                  Text(t, style: AppTheme.body),
                ],
              ),
            ),
        ],
      ),
    );
    if (newType == null || newType == current || !mounted) return;

    setState(() => _busy = true);
    final result = await _api.changeGraphNodeType(id, newType);
    if (!mounted) return;
    setState(() => _busy = false);

    if (result.success) {
      _toast('Type → $newType');
      await _load();
    } else {
      _toast(result.error ?? 'Type change failed', error: true);
    }
  }

  Future<void> _deleteFlow(Map<String, dynamic> node) async {
    final id = node['id'].toString();
    final label = node['label'] as String? ?? 'Untitled';

    final ok = await _confirm(
      title: 'Delete "$label"?',
      message:
          'This deletes the live node and its edges. The linked person/org record is deactivated. This cannot be undone.',
      confirmLabel: 'Delete',
      destructive: true,
    );
    if (ok != true || !mounted) return;

    setState(() => _busy = true);
    final result = await _api.deleteGraphNode(id);
    if (!mounted) return;
    setState(() => _busy = false);

    if (result.success) {
      _toast('Deleted $label');
      await _load();
    } else {
      _toast(result.error ?? 'Delete failed', error: true);
    }
  }

  Future<bool?> _confirm({
    required String title,
    required String message,
    required String confirmLabel,
    bool destructive = false,
  }) {
    return showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppTheme.surface,
        title: Text(title, style: AppTheme.title),
        content: Text(message, style: AppTheme.bodyMuted),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(
              confirmLabel,
              style: TextStyle(color: destructive ? AppTheme.red : AppTheme.champagne),
            ),
          ),
        ],
      ),
    );
  }

  void _toast(String message, {bool error = false}) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(message),
        backgroundColor: error ? AppTheme.redBg : AppTheme.surfaceAlt,
        behavior: SnackBarBehavior.floating,
      ),
    );
    HapticFeedback.selectionClick();
  }
}

class _ActionTile extends StatelessWidget {
  final IconData icon;
  final String label;
  final Color? color;
  final VoidCallback onTap;

  const _ActionTile({
    required this.icon,
    required this.label,
    this.color,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final c = color ?? AppTheme.textPrimary;
    return ListTile(
      leading: Icon(icon, color: c, size: 20),
      title: Text(label, style: AppTheme.body.copyWith(color: c)),
      onTap: onTap,
    );
  }
}
