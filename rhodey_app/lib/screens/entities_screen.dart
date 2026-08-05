import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';

/// Live knowledge-graph entities (people, orgs, concepts) — mirrors the web
/// UI's Decisions → Entities tab so node monitoring/merging works on-device.
///
/// Actions are destructive-by-intent, so every one is guarded by a confirm
/// dialog. All calls go through ApiService to the backend routes
/// (/api/graph-nodes/live with limit/offset/q pagination + search,
/// /api/graph-nodes/search, /api/graph-node-merge,
/// /api/graph-node/{id} PUT/PATCH/DELETE).
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

  /// Server-side pagination + search: the backend filters by label and pages
  /// the result, so the phone never downloads the whole graph (was 5000 rows
  /// with metadata on every open).
  static const _pageSize = 200;
  int _offset = 0;
  bool _hasMore = false;
  Timer? _searchDebounce;

  /// Monotonic load sequence — a slow response from an older search/pagination
  /// must never overwrite a newer one (the debounce shrinks but can't close
  /// that race).
  int _loadSeq = 0;

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
    _search.addListener(_onSearchChanged);
  }

  @override
  void dispose() {
    _search.removeListener(_onSearchChanged);
    _searchDebounce?.cancel();
    _search.dispose();
    super.dispose();
  }

  Future<void> _load({bool more = false}) async {
    final seq = ++_loadSeq;
    if (more) {
      setState(() => _busy = true);
    } else {
      _offset = 0;
      _all = [];
      setState(() {
        _loading = true;
        _error = '';
      });
    }
    final q = _search.text.trim();
    final result = await _api.getLiveGraphNodes(
      limit: _pageSize,
      offset: _offset,
      q: q.isEmpty ? null : q,
    );
    // A newer load superseded this one — drop the stale response.
    if (!mounted || seq != _loadSeq) return;
    setState(() {
      _loading = false;
      _busy = false;
      if (result.success) {
        final rows = result.data ?? [];
        _all = more ? [..._all, ...rows] : rows;
        _offset = _all.length;
        _hasMore = rows.length == _pageSize;
        _applyFilter();
      } else {
        if (_all.isEmpty) _error = result.error ?? 'Failed to load entities';
      }
    });
  }

  /// Server-side search, debounced — the backend filters + pages, so typing
  /// never re-downloads the whole graph.
  void _onSearchChanged() {
    _searchDebounce?.cancel();
    _searchDebounce = Timer(const Duration(milliseconds: 300), () {
      _load();
    });
  }

  Future<void> _loadMore() async {
    if (_busy || !_hasMore) return;
    await _load(more: true);
  }

  void _applyFilter() {
    final q = _search.text.trim().toLowerCase();
    setState(() {
      _filtered =
          _all.where((n) {
            final label = (n['label'] as String? ?? '').toLowerCase();
            final type = n['type'] as String? ?? '';
            final matchesType = _filterType == 'all' || type == _filterType;
            final matchesQuery = q.isEmpty || label.contains(q);
            return matchesType && matchesQuery;
          }).toList()..sort((a, b) {
            final ta = (a['type'] as String? ?? '');
            final tb = (b['type'] as String? ?? '');
            if (ta != tb) return ta.compareTo(tb);
            return (a['label'] as String? ?? '').compareTo(
              b['label'] as String? ?? '',
            );
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
                prefixIcon: Icon(
                  Icons.search,
                  color: AppTheme.textTertiary,
                  size: 20,
                ),
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
                      color: selected
                          ? AppTheme.champagne
                          : AppTheme.textSecondary,
                    ),
                  ),
                );
              }).toList(),
            ),
          ),
          // List
          Expanded(
            child: _loading
                ? const Center(
                    child: CircularProgressIndicator(color: AppTheme.champagne),
                  )
                : _error.isNotEmpty
                ? _buildError()
                : _filtered.isEmpty
                ? _buildEmpty()
                : RefreshIndicator(
                    onRefresh: _refresh,
                    color: AppTheme.champagne,
                    child: ListView.builder(
                      padding: const EdgeInsets.fromLTRB(16, 8, 16, 80),
                      itemCount: _filtered.length + (_hasMore ? 1 : 0),
                      itemBuilder: (_, i) {
                        if (i >= _filtered.length) {
                          return _buildLoadMoreTile();
                        }
                        return _buildNodeTile(_filtered[i]);
                      },
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
            Text(
              _error,
              style: AppTheme.hintStyle,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 16),
            OutlinedButton(onPressed: _load, child: const Text('Retry')),
          ],
        ),
      ),
    );
  }

  Widget _buildEmpty() {
    return Center(child: Text('No entities found.', style: AppTheme.hintStyle));
  }

  Widget _buildLoadMoreTile() {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 12),
      child: Center(
        child: _busy
            ? const SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(
                  strokeWidth: 2,
                  color: AppTheme.champagne,
                ),
              )
            : OutlinedButton(
                onPressed: _loadMore,
                child: const Text('Show more'),
              ),
      ),
    );
  }

  Widget _buildNodeTile(Map<String, dynamic> node) {
    final label = node['label'] as String? ?? 'Untitled';
    final type = node['type'] as String? ?? 'unknown';
    final createdAt = node['created_at'] as String? ?? '';
    final enrichment = _enrichmentOf(node);

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

    final enrichSummary = _enrichmentSummary(type, enrichment);

    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      color: AppTheme.surface,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppTheme.cardRadius),
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
                    Text(
                      label,
                      style: AppTheme.bodyStyle,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    const SizedBox(height: 2),
                    if (enrichSummary.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 2),
                        child: Text(
                          enrichSummary,
                          style: AppTheme.caption.copyWith(
                            color:
                                (enrichment?['strategic_weight'] as num?)
                                        ?.toInt() ==
                                    null
                                ? AppTheme.textSecondary
                                : ((enrichment?['strategic_weight'] as num?)
                                              ?.toInt() ??
                                          0) >=
                                      8
                                ? AppTheme.champagne
                                : AppTheme.textSecondary,
                          ),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                    Text(
                      createdAt.isNotEmpty
                          ? '$type · ${_shortDate(createdAt)}'
                          : type,
                      style: AppTheme.caption.copyWith(
                        color: AppTheme.textSecondary,
                      ),
                    ),
                  ],
                ),
              ),
              const Icon(
                Icons.more_horiz,
                color: AppTheme.textTertiary,
                size: 18,
              ),
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
              icon: Icons.tune,
              label: 'Edit details (role, weight, active…)',
              color: AppTheme.champagne,
              onTap: () async {
                Navigator.pop(ctx);
                await _enrichFlow(node);
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

  Future<Map<String, dynamic>?> _pickTargetNode(
    String excludeLabel,
    String? nodeType,
  ) async {
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
                      .where(
                        (n) => (n['label'] as String? ?? '') != excludeLabel,
                      )
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
                      child: Text(
                        'Type at least 2 characters to search.',
                        style: AppTheme.hintStyle,
                      ),
                    )
                  : ListView.builder(
                      shrinkWrap: true,
                      itemCount: results.length,
                      itemBuilder: (_, i) {
                        final n = results[i];
                        return ListTile(
                          dense: true,
                          title: Text(
                            n['label'] as String? ?? '',
                            style: AppTheme.body,
                          ),
                          subtitle: Text(
                            n['type'] as String? ?? '',
                            style: AppTheme.caption,
                          ),
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
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, controller.text.trim()),
            child: const Text('Save'),
          ),
        ],
      ),
    );
    if (newLabel == null ||
        newLabel.isEmpty ||
        newLabel == oldLabel ||
        !mounted) {
      return;
    }

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
                    t == current
                        ? Icons.radio_button_checked
                        : Icons.radio_button_off,
                    color: t == current
                        ? AppTheme.champagne
                        : AppTheme.textTertiary,
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

  /// Inline enrichment editing — role, weight, active, org fields, aliases
  /// and active tasks. Enrichment lives on graph_nodes.metadata.enrichment
  /// (migrations 74-76); aliases on metadata.aliases (migration 76). The
  /// People tab was consolidated here, so person nodes get alias + task
  /// management in the same dialog.
  Future<void> _enrichFlow(Map<String, dynamic> node) async {
    final id = node['id'].toString();
    final label = node['label'] as String? ?? 'Untitled';
    final isPerson = (node['type'] as String? ?? '') == 'person';
    final e = _enrichmentOf(node);

    final role = TextEditingController(text: (e?['role'] as String?) ?? '');
    final orgName = TextEditingController(
      text: (e?['organization_name'] as String?) ?? '',
    );
    final orgType = TextEditingController(
      text: (e?['org_type'] as String?) ?? '',
    );
    final weight = TextEditingController(
      text: (e?['strategic_weight'] as num?)?.toInt().toString() ?? '',
    );
    final description = TextEditingController(
      text: (e?['description'] as String?) ?? '',
    );
    final aliasController = TextEditingController();
    var isActive = (e?['is_active'] as bool?) ?? true;

    // Aliases + tasks (person nodes only) load in the background while the
    // dialog opens, so the dialog never feels frozen on slow networks.
    List<Map<String, dynamic>> aliases = [];
    var aliasesLoading = isPerson;
    List<Map<String, dynamic>> tasks = [];
    var tasksLoading = isPerson;

    if (!mounted) return;
    final saved = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setSheet) {
          // Kick off the background alias/task load once the dialog is up.
          if (isPerson && aliasesLoading) {
            Future(() async {
              final aliasRes = await _api.getAliases();
              final nameLc = label.toLowerCase();
              final loaded =
                  List<Map<String, dynamic>>.from(
                        aliasRes.success ? (aliasRes.data ?? []) : [],
                      )
                      .where(
                        (a) =>
                            (a['canonical_name'] as String? ?? '')
                                .toLowerCase() ==
                            nameLc,
                      )
                      .toList()
                      .cast<Map<String, dynamic>>();
              final tasksRes = await _api.getPersonTasks(id, label);
              final loadedTasks = tasksRes.success
                  ? List<Map<String, dynamic>>.from(tasksRes.data ?? [])
                  : <Map<String, dynamic>>[];
              if (!mounted) return;
              setSheet(() {
                aliases = loaded;
                tasks = loadedTasks;
                aliasesLoading = false;
                tasksLoading = false;
              });
            });
          }
          return AlertDialog(
            backgroundColor: AppTheme.surface,
            title: Text('Edit details — $label', style: AppTheme.title),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _enrichField('Role', 'e.g. Wife, Auditor, Vendor…', role),
                  _enrichField('Organization', 'e.g. CrayonBiz LLP', orgName),
                  _enrichField(
                    'Org type',
                    'e.g. company, nonprofit, family…',
                    orgType,
                  ),
                  _enrichField(
                    'Strategic weight (1–10)',
                    '5',
                    weight,
                    keyboardType: TextInputType.number,
                  ),
                  const SizedBox(height: 10),
                  Text('Description', style: AppTheme.caption),
                  const SizedBox(height: 4),
                  TextField(
                    controller: description,
                    style: AppTheme.body,
                    maxLines: 2,
                    decoration: const InputDecoration(
                      hintText: 'One line about this entity…',
                      isDense: true,
                    ),
                  ),
                  const SizedBox(height: 12),
                  Row(
                    children: [
                      const Icon(Icons.circle, size: 10, color: AppTheme.green),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          'Active — inactive entities are deprioritized.',
                          style: AppTheme.bodyMuted,
                        ),
                      ),
                      Switch(
                        value: isActive,
                        activeTrackColor: AppTheme.green,
                        onChanged: (v) => setSheet(() => isActive = v),
                      ),
                    ],
                  ),
                  if (isPerson) ...[
                    const SizedBox(height: 14),
                    Text(
                      'ALIASES (${aliasesLoading ? '' : aliases.length})',
                      style: AppTheme.label.copyWith(
                        color: AppTheme.textTertiary,
                        fontSize: 10,
                        letterSpacing: 1.2,
                      ),
                    ),
                    const SizedBox(height: 6),
                    if (aliasesLoading)
                      const Padding(
                        padding: EdgeInsets.symmetric(vertical: 6),
                        child: Center(
                          child: SizedBox(
                            width: 16,
                            height: 16,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          ),
                        ),
                      )
                    else if (aliases.isEmpty)
                      Text(
                        'No aliases — add nicknames or alternate names',
                        style: AppTheme.hintStyle.copyWith(fontSize: 11),
                      )
                    else
                      Wrap(
                        spacing: 6,
                        runSpacing: 6,
                        children: aliases.map((a) {
                          final aliasText = a['alias'] as String? ?? '';
                          return Chip(
                            label: Text(aliasText),
                            labelStyle: AppTheme.caption.copyWith(
                              color: AppTheme.champagne,
                            ),
                            backgroundColor: AppTheme.champagneMuted,
                            side: BorderSide(
                              color: AppTheme.champagne.withValues(alpha: 0.25),
                            ),
                            deleteIcon: const Icon(
                              Icons.close,
                              size: 14,
                              color: AppTheme.textTertiary,
                            ),
                            onDeleted: () async {
                              final canonical =
                                  a['canonical_name'] as String? ?? label;
                              final res = await _api.deleteAlias(
                                aliasText,
                                canonical,
                              );
                              if (!mounted) return;
                              if (res.success) {
                                setSheet(() {
                                  aliases.removeWhere(
                                    (x) =>
                                        (x['alias'] as String? ?? '')
                                            .toLowerCase() ==
                                        aliasText.toLowerCase(),
                                  );
                                });
                              } else {
                                _toast(
                                  res.error ?? 'Failed to delete alias',
                                  error: true,
                                );
                              }
                            },
                          );
                        }).toList(),
                      ),
                    const SizedBox(height: 6),
                    Row(
                      children: [
                        Expanded(
                          child: TextField(
                            controller: aliasController,
                            style: AppTheme.body.copyWith(fontSize: 13),
                            decoration: const InputDecoration(
                              hintText: 'e.g. Nickname…',
                              isDense: true,
                            ),
                          ),
                        ),
                        const SizedBox(width: 8),
                        TextButton(
                          onPressed: () async {
                            final alias = aliasController.text.trim();
                            if (alias.isEmpty) return;
                            final res = await _api.createAlias(alias, label);
                            if (!mounted) return;
                            final created = res.data;
                            final ok =
                                res.success &&
                                (created is! Map ||
                                    created['success'] != false);
                            if (ok &&
                                created is Map &&
                                created['alias'] is Map) {
                              setSheet(() {
                                aliases.add(
                                  (created['alias'] as Map)
                                      .cast<String, dynamic>(),
                                );
                                aliasController.clear();
                              });
                            } else {
                              _toast(
                                (created is Map && created['message'] is String)
                                    ? created['message'] as String
                                    : 'Failed to add alias',
                                error: true,
                              );
                            }
                          },
                          child: const Text(
                            'Add',
                            style: TextStyle(color: AppTheme.champagne),
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 14),
                    Text(
                      'ACTIVE TASKS (${tasksLoading ? '' : tasks.length})',
                      style: AppTheme.label.copyWith(
                        color: AppTheme.textTertiary,
                        fontSize: 10,
                        letterSpacing: 1.2,
                      ),
                    ),
                    const SizedBox(height: 6),
                    if (tasksLoading)
                      const Padding(
                        padding: EdgeInsets.symmetric(vertical: 6),
                        child: Center(
                          child: SizedBox(
                            width: 16,
                            height: 16,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          ),
                        ),
                      )
                    else if (tasks.isEmpty)
                      Text(
                        'No active tasks mention this person',
                        style: AppTheme.hintStyle.copyWith(fontSize: 11),
                      )
                    else
                      ...tasks
                          .take(8)
                          .map(
                            (t) => Padding(
                              padding: const EdgeInsets.only(bottom: 6),
                              child: Row(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Container(
                                    padding: const EdgeInsets.symmetric(
                                      horizontal: 6,
                                      vertical: 1,
                                    ),
                                    decoration: BoxDecoration(
                                      color: AppTheme.surfaceAlt,
                                      borderRadius: BorderRadius.circular(6),
                                    ),
                                    child: Text(
                                      t['priority'] as String? ?? '',
                                      style: AppTheme.caption.copyWith(
                                        color: AppTheme.textSecondary,
                                      ),
                                    ),
                                  ),
                                  const SizedBox(width: 8),
                                  Expanded(
                                    child: Text(
                                      t['title'] as String? ?? '',
                                      style: AppTheme.bodySmall.copyWith(
                                        fontSize: 12,
                                      ),
                                      maxLines: 2,
                                      overflow: TextOverflow.ellipsis,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          ),
                  ],
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(ctx, false),
                child: const Text('Cancel'),
              ),
              TextButton(
                onPressed: () => Navigator.pop(ctx, true),
                child: const Text(
                  'Save',
                  style: TextStyle(color: AppTheme.champagne),
                ),
              ),
            ],
          );
        },
      ),
    );

    if (saved != true || !mounted) return;

    final weightVal = int.tryParse(weight.text.trim());
    final updates = <String, dynamic>{
      'role': role.text.trim().isEmpty ? null : role.text.trim(),
      'organization_name': orgName.text.trim().isEmpty
          ? null
          : orgName.text.trim(),
      'org_type': orgType.text.trim().isEmpty ? null : orgType.text.trim(),
      'strategic_weight': weightVal,
      'description': description.text.trim().isEmpty
          ? null
          : description.text.trim(),
      'is_active': isActive,
    };
    if (weightVal != null && (weightVal < 1 || weightVal > 10)) {
      _toast('Strategic weight must be between 1 and 10', error: true);
      return;
    }

    setState(() => _busy = true);
    final result = await _api.updateGraphNodeEnrichment(id, updates);
    if (!mounted) return;
    setState(() => _busy = false);

    if (result.success) {
      _toast('Details updated');
      await _load();
    } else {
      _toast(result.error ?? 'Update failed', error: true);
    }
  }

  Widget _enrichField(
    String label,
    String hint,
    TextEditingController controller, {
    TextInputType? keyboardType,
  }) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: AppTheme.caption),
          const SizedBox(height: 4),
          TextField(
            controller: controller,
            style: AppTheme.body,
            keyboardType: keyboardType,
            decoration: InputDecoration(hintText: hint, isDense: true),
          ),
        ],
      ),
    );
  }

  Map<String, dynamic>? _enrichmentOf(Map<String, dynamic> node) {
    final meta = node['metadata'];
    if (meta is Map) {
      final e = meta['enrichment'];
      if (e is Map) return Map<String, dynamic>.from(e);
    }
    return null;
  }

  String _enrichmentSummary(String type, Map<String, dynamic>? e) {
    if (e == null) return '';
    final bits = <String>[];
    if (type == 'person') {
      final role = e['role'] as String?;
      final w = (e['strategic_weight'] as num?)?.toInt();
      if (role != null && role.isNotEmpty) bits.add(role);
      if (w != null) bits.add('weight $w/10');
    } else if (type == 'organization') {
      final t = e['org_type'] as String?;
      final d = e['description'] as String?;
      if (t != null && t.isNotEmpty) bits.add(t);
      if (d != null && d.isNotEmpty) bits.add(d);
    }
    if (bits.isEmpty) return '';
    return bits.take(3).join(' · ');
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
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(
              confirmLabel,
              style: TextStyle(
                color: destructive ? AppTheme.red : AppTheme.champagne,
              ),
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
