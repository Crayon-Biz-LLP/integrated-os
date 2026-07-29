import 'package:flutter/material.dart';
import '../models/decision_item.dart';
import '../services/api_service.dart';
import '../theme/app_theme.dart';
import '../widgets/decision_card.dart';

class InboxScreen extends StatefulWidget {
  const InboxScreen({super.key});

  @override
  State<InboxScreen> createState() => _InboxScreenState();
}

class _InboxScreenState extends State<InboxScreen> {
  final _api = ApiService();
  List<DecisionItem> _items = [];
  bool _loading = true;
  String _error = '';

  /// Count of unverified auto-decisions — fetched on load
  int _autoDecisionCount = 0;

  @override
  void initState() {
    super.initState();
    _loadDecisions();
    _loadAutoDecisionCount();
  }

  Future<void> _loadDecisions() async {
    setState(() => _loading = true);
    final result = await _api.getPendingDecisions();
    if (!mounted) return;

    final items = <DecisionItem>[];

    if (result.success) {
      for (final pd in result.data!) {
        double confidence;
        try {
          confidence = (pd.raw['confidence'] as num?)?.toDouble() ?? 0.0;
        } catch (_) {
          confidence = 0.0;
        }

        // Detect merge proposals and person type from raw data
        final status = pd.raw['status'] as String? ?? '';
        final nodeType = pd.raw['type'] as String? ?? pd.raw['node_type'] as String?;
        final isMerge = status == 'merge_proposed';
        final source = pd.source;

        items.add(DecisionItem(
          id: 'api_${source}_${pd.id}',
          type: isMerge
              ? DecisionType.merge
              : _sourceToType(source),
          priority: isMerge ? DecisionPriority.high : DecisionPriority.standard,
          title: pd.title,
          description: isMerge
              ? 'Merge proposed — accept to combine duplicate nodes'
              : pd.description,
          confidence: confidence > 0 ? confidence : null,
          createdAt: DateTime.now().subtract(const Duration(hours: 2)),
          nodeType: nodeType,
          metadata: {'api_id': pd.id, 'source': source},
        ));
      }
    } else {
      _error = result.error ?? '';
    }

    if (mounted) {
      setState(() {
        _items = items;
        _loading = false;
      });
    }
  }

  Future<void> _loadAutoDecisionCount() async {
    final result = await _api.get('/api/auto-decisions',
        query: {'status': 'unverified', 'limit': '1'});
    if (!mounted) return;
    if (result.success && result.data is Map) {
      final count = (result.data as Map)['count'] as int? ?? 0;
      setState(() => _autoDecisionCount = count);
    }
  }

  DecisionType _sourceToType(String source) {
    switch (source) {
      case 'email':
        return DecisionType.email;
      case 'whatsapp':
        return DecisionType.whatsapp;
      case 'call':
        return DecisionType.call;
      case 'graph_node':
        return DecisionType.person;
      case 'graph_edge':
        return DecisionType.edge;
      default:
        return DecisionType.clarification;
    }
  }

  // ── Person Context Bottom Sheet (S1#1) ───────────────────────

  Future<void> _showPersonContextSheet(DecisionItem item) async {
    final textController = TextEditingController();
    final contextResult = await showModalBottomSheet<String>(
      context: context,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (ctx) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Container(
                width: 36, height: 4,
                decoration: BoxDecoration(
                  color: AppTheme.textTertiary.withValues(alpha: 0.3),
                  borderRadius: BorderRadius.circular(2),
                ),
                margin: const EdgeInsets.only(bottom: 16),
              ),
              Text(
                'Any context for "${item.title}"?',
                style: AppTheme.title.copyWith(fontSize: 15),
              ),
              const SizedBox(height: 4),
              Text(
                'Role, relationship, or organization',
                style: AppTheme.bodySmall.copyWith(
                  color: AppTheme.textTertiary, fontSize: 12,
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: textController,
                autofocus: true,
                textInputAction: TextInputAction.done,
                onSubmitted: (v) => Navigator.pop(ctx, v.trim()),
                decoration: InputDecoration(
                  hintText: 'e.g. Friend from church, VP at Qhord',
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
              const SizedBox(height: 16),
              Row(
                children: [
                  Expanded(
                    child: Material(
                      color: Colors.transparent,
                      child: InkWell(
                        borderRadius: BorderRadius.circular(10),
                        onTap: () => Navigator.pop(ctx, ''),
                        child: Container(
                          padding: const EdgeInsets.symmetric(vertical: 12),
                          decoration: BoxDecoration(
                            borderRadius: BorderRadius.circular(10),
                            border: Border.all(color: AppTheme.border),
                          ),
                          child: const Text(
                            'Skip',
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              color: AppTheme.textSecondary, fontSize: 13,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    flex: 2,
                    child: Material(
                      color: AppTheme.accent,
                      borderRadius: BorderRadius.circular(10),
                      child: InkWell(
                        borderRadius: BorderRadius.circular(10),
                        onTap: () {
                          final text = textController.text.trim();
                          Navigator.pop(ctx, text.isNotEmpty ? text : null);
                        },
                        child: Container(
                          padding: const EdgeInsets.symmetric(vertical: 12),
                          child: const Text(
                            'Save Context',
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              color: Colors.white,
                              fontSize: 13,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );

    if (contextResult != null && mounted) {
      // contextResult is '' for Skip, or the entered text
      await _doApprovePerson(item, context: contextResult.isEmpty ? null : contextResult);
    }
  }

  Future<void> _doApprovePerson(DecisionItem item, {String? context}) async {
    final apiId = item.metadata['api_id'] as String?;
    if (apiId == null) return;
    final id = int.tryParse(apiId);
    if (id == null) return;

    // Use approveGraphNodeWithContext — sends 'context' param, NOT 'label'.
    // The backend process_graph_pending_decision accepts a context parameter
    // for person nodes (role/org), separate from the node label.
    final result = await _api.approveGraphNodeWithContext(id, context: context);
    if (!mounted) return;
    if (result.success) {
      _removeItem(item);
    } else {
      _showSnack(result.error ?? 'Failed to approve');
    }
  }

  // ── Inline Decision Handlers ──────────────────────────────────

  Future<void> _handleApprove(DecisionItem item) async {
    // Merge proposal: use graph-merge-action endpoint
    if (item.isMergeProposal) {
      final apiId = item.metadata['api_id'] as String?;
      if (apiId != null) {
        final id = int.tryParse(apiId);
        if (id != null) {
          final result = await _api.acceptMerge(id);
          if (result.success) {
            _removeItem(item);
          } else {
            _showSnack(result.error ?? 'Failed to accept merge');
          }
        }
      }
      return;
    }

    // Person context: show bottom sheet first
    if (item.needsPersonContext) {
      await _showPersonContextSheet(item);
      return;
    }

    final meta = item.metadata;
    final apiId = meta['api_id'] as String?;
    final source = meta['source'] as String?;

    if (apiId != null && source != null) {
      final id = int.tryParse(apiId);
      if (id != null) {
        ApiResult result;
        switch (source) {
          case 'email':
            result = await _api.approveEmail(id);
            break;
          case 'whatsapp':
            result = await _api.approveWhatsApp(id);
            break;
          case 'call':
            result = await _api.approveCall(id);
            break;
          case 'graph_node':
            result = await _api.approveGraphNode(id);
            break;
          case 'graph_edge':
            result = await _api.approveGraphEdge(id);
            break;
          default:
            result = ApiResult.fail('Unknown source: $source');
        }
        if (result.success) {
          _removeItem(item);
        } else if (mounted) {
          _showSnack(result.error ?? 'Failed to approve');
        }
        return;
      }
    }

    _removeItem(item);
  }

  /// Optimistic dismiss — remove item from UI immediately,
  /// then fire the reject API in the background.
  void _handleReject(DecisionItem item) {
    // Remove from UI instantly
    _removeItem(item);

    // Fire the reject API in the background (fire-and-forget)
    _fireRejectApi(item);
  }

  Future<void> _fireRejectApi(DecisionItem item) async {
    final meta = item.metadata;
    final apiId = meta['api_id'] as String?;
    final source = meta['source'] as String?;

    if (apiId == null || source == null) return;
    final id = int.tryParse(apiId);
    if (id == null) return;

    ApiResult result;
    if (item.isMergeProposal) {
      result = await _api.rejectMerge(id);
    } else {
      switch (source) {
        case 'email':
          result = await _api.rejectEmail(id);
          break;
        case 'whatsapp':
          result = await _api.rejectWhatsApp(id);
          break;
        case 'call':
          result = await _api.rejectCall(id);
          break;
        case 'graph_node':
          result = await _api.rejectGraphNode(id);
          break;
        case 'graph_edge':
          result = await _api.rejectGraphEdge(id);
          break;
        default:
          return; // unknown source, item already removed
      }
    }

    if (!result.success && mounted) {
      _showSnack(result.error ?? '⚠️ Dismiss failed on server');
    }
  }

  // ── Edge Editing (S2#6) ──────────────────────────────────────

  Future<void> _showEdgeEditSheet(DecisionItem item) async {
    final textController = TextEditingController();
    // Extract current relationship from title like "Alice → KNOWS → Bob"
    final parts = item.title.split(' → ');
    final currentRel = parts.length >= 2 ? parts[1] : '';
    textController.text = currentRel;

    final newRelation = await showModalBottomSheet<String>(
      context: context,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (ctx) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Container(
                width: 36, height: 4,
                decoration: BoxDecoration(
                  color: AppTheme.textTertiary.withValues(alpha: 0.3),
                  borderRadius: BorderRadius.circular(2),
                ),
                margin: const EdgeInsets.only(bottom: 16),
              ),
              Text(
                'Edit relationship',
                style: AppTheme.title.copyWith(fontSize: 15),
              ),
              const SizedBox(height: 4),
              Text(
                item.title,
                style: AppTheme.bodySmall.copyWith(
                  color: AppTheme.textTertiary, fontSize: 12,
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: textController,
                autofocus: true,
                textInputAction: TextInputAction.done,
                onSubmitted: (v) => Navigator.pop(ctx, v.trim()),
                decoration: InputDecoration(
                  hintText: 'KNOWS, WORKS_WITH, MEMBER_OF...',
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
              const SizedBox(height: 16),
              Row(
                children: [
                  Expanded(
                    child: Material(
                      color: Colors.transparent,
                      child: InkWell(
                        borderRadius: BorderRadius.circular(10),
                        onTap: () => Navigator.pop(ctx),
                        child: Container(
                          padding: const EdgeInsets.symmetric(vertical: 12),
                          decoration: BoxDecoration(
                            borderRadius: BorderRadius.circular(10),
                            border: Border.all(color: AppTheme.border),
                          ),
                          child: const Text(
                            'Cancel',
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              color: AppTheme.textSecondary, fontSize: 13,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    flex: 2,
                    child: Material(
                      color: AppTheme.accent,
                      borderRadius: BorderRadius.circular(10),
                      child: InkWell(
                        borderRadius: BorderRadius.circular(10),
                        onTap: () {
                          final text = textController.text.trim();
                          if (text.isNotEmpty) Navigator.pop(ctx, text);
                        },
                        child: Container(
                          padding: const EdgeInsets.symmetric(vertical: 12),
                          child: const Text(
                            'Save & Approve',
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              color: Colors.white,
                              fontSize: 13,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );

    if (newRelation != null && mounted) {
      // Approve edge with corrected relationship
      final apiId = item.metadata['api_id'] as String?;
      if (apiId != null) {
        final id = int.tryParse(apiId);
        if (id != null) {
          final result = await _api.approveGraphEdgeWithRelation(id, newRelation);
          if (result.success) {
            _removeItem(item);
          } else {
            _showSnack(result.error ?? 'Failed to approve edge');
          }
        }
      }
    }
  }

  // ── Auto-Decision Confirm/Undo (S1#3) ────────────────────────

  Future<void> _confirmAllAutoDecisions() async {
    final result = await _api.post('/api/auto-decisions/confirm');
    if (!mounted) return;
    if (result.success) {
      setState(() => _autoDecisionCount = 0);
      _showSnack('✅ Auto-decisions verified', isError: false);
    } else {
      _showSnack(result.error ?? 'Failed to confirm');
    }
  }

  Future<void> _undoAutoChannelDecisions() async {
    final result = await _api.post('/api/auto-decisions/undo',
        body: {'target': 'channels'});
    if (!mounted) return;
    if (result.success) {
      _loadDecisions();
      _showSnack('↩️ Undone channel auto-decisions', isError: false);
    } else {
      _showSnack(result.error ?? 'Failed to undo');
    }
  }

  // ── Batch Operations (S2#7) ──────────────────────────────────

  Future<void> _batchApprove(String itemType) async {
    final confirm = await _showBatchConfirm('Approve all $itemType?');
    if (!confirm || !mounted) return;

    ApiResult result;
    switch (itemType) {
      case 'edges':
        result = await _api.post('/api/graph-edge-action/batch',
            body: {'action': 'approve'});
        break;
      case 'nodes':
        result = await _api.post('/api/graph-node-action/batch',
            body: {'action': 'approve'});
        break;
      case 'emails':
        result = await _api.post('/api/email-action/batch',
            body: {'action': 'approve'});
        break;
      default:
        return;
    }

    if (!mounted) return;
    if (result.success) {
      _loadDecisions();
      _showSnack('✅ Approved all $itemType', isError: false);
    } else {
      _showSnack(result.error ?? 'Batch approve failed');
    }
  }

  Future<bool> _showBatchConfirm(String message) async {
    final result = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppTheme.surface,
        content: Text(message, style: AppTheme.body),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: Text('Proceed',
                style: TextStyle(color: AppTheme.accent)),
          ),
        ],
      ),
    );
    return result ?? false;
  }

  // ── Helpers ──────────────────────────────────────────────────

  void _removeItem(DecisionItem item) {
    setState(() => _items.removeWhere((i) => i.id == item.id));
  }

  void _showSnack(String msg, {bool isError = true}) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(msg, style: const TextStyle(fontSize: 12)),
        backgroundColor: isError ? AppTheme.red : AppTheme.green,
        duration: const Duration(seconds: 2),
      ),
    );
  }

  // ── Build ──

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return Scaffold(
        appBar: AppBar(title: const Text('Inbox')),
        body: const Center(child: CircularProgressIndicator()),
      );
    }

    final merges = _items.where((d) => d.isMergeProposal).toList();
    final nonMerges = _items.where((d) => !d.isMergeProposal).toList();
    final highPriority = nonMerges.where((d) => d.priority == DecisionPriority.high).toList();
    final standardPriority = nonMerges.where((d) => d.priority == DecisionPriority.standard).toList();
    final lowPriority = nonMerges.where((d) => d.priority == DecisionPriority.low).toList();

    // Count types for batch operations
    final edgeCount = nonMerges.where((d) => d.type == DecisionType.edge).length;
    final nodeCount = nonMerges.where((d) => d.type == DecisionType.person).length;

    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            const Text('Inbox'),
            const Spacer(),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
              decoration: BoxDecoration(
                color: AppTheme.accentBg,
                borderRadius: BorderRadius.circular(6),
              ),
              child: Text(
                '${_items.length} pending',
                style: AppTheme.statusDot.copyWith(color: AppTheme.accent),
              ),
            ),
          ],
        ),
      ),
      body: RefreshIndicator(
        onRefresh: _loadDecisions,
        child: ListView(
          padding: const EdgeInsets.fromLTRB(0, 8, 0, 80),
          children: [
            // Error banner
            if (_error.isNotEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                child: Text(
                  '⚠️ $_error',
                  style: const TextStyle(color: AppTheme.red, fontSize: 11),
                ),
              ),

            // ── Auto-Decision Confirm Banner (S1#3) ──
            if (_autoDecisionCount > 0)
              _AutoDecisionBanner(
                count: _autoDecisionCount,
                onConfirm: _confirmAllAutoDecisions,
                onUndoChannels: _undoAutoChannelDecisions,
              ),

            // ── Batch Operations (S2#7) ──
            if (edgeCount >= 2 || nodeCount >= 2)
              _BatchOperationsBar(
                edgeCount: edgeCount,
                nodeCount: nodeCount,
                onApproveEdges: () => _batchApprove('edges'),
                onApproveNodes: () => _batchApprove('nodes'),
              ),

            // ── Merge Proposals ──
            if (merges.isNotEmpty) ...[
              _SectionLabel('Merge Proposals'),
              ...merges.map((d) => DecisionCard(
                item: d,
                onApprove: () => _handleApprove(d),
                onReject: () => _handleReject(d),
              )),
              if (nonMerges.isNotEmpty) const SizedBox(height: 16),
            ],

            // ── Standard items ──
            if (highPriority.isNotEmpty)
              _SectionLabel('High Priority'),
            ...highPriority.map((d) => DecisionCard(
              item: d,
              onApprove: () => _handleApprove(d),
              onReject: () => _handleReject(d),
              onEdit: d.type == DecisionType.edge
                  ? () => _showEdgeEditSheet(d)
                  : null,
            )),
            if (highPriority.isNotEmpty && standardPriority.isNotEmpty)
              const SizedBox(height: 16),

            if (standardPriority.isNotEmpty)
              _SectionLabel('Standard'),
            ...standardPriority.map((d) => DecisionCard(
              item: d,
              onApprove: () => _handleApprove(d),
              onReject: () => _handleReject(d),
              onEdit: d.type == DecisionType.edge
                  ? () => _showEdgeEditSheet(d)
                  : null,
            )),
            if (standardPriority.isNotEmpty && lowPriority.isNotEmpty)
              const SizedBox(height: 16),

            if (lowPriority.isNotEmpty)
              _SectionLabel('Low Effort'),
            ...lowPriority.map((d) => DecisionCard(
              item: d,
              onApprove: () => _handleApprove(d),
              onReject: () => _handleReject(d),
            )),

            if (_items.isEmpty) ...[
              const SizedBox(height: 40),
              const Center(
                child: Text(
                  'No pending decisions',
                  style: TextStyle(color: AppTheme.textTertiary, fontSize: 13),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

// ── Auto-Decision Banner (S1#3) ─────────────────────────────────

class _AutoDecisionBanner extends StatelessWidget {
  final int count;
  final VoidCallback onConfirm;
  final VoidCallback onUndoChannels;

  const _AutoDecisionBanner({
    required this.count,
    required this.onConfirm,
    required this.onUndoChannels,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: AppTheme.accentBg,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: AppTheme.accent.withValues(alpha: 0.3)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.auto_awesome, size: 14, color: AppTheme.accent),
              const SizedBox(width: 6),
              Text(
                '$count auto-decisions pending review',
                style: const TextStyle(
                  color: AppTheme.accent,
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _ChipButton(
                label: '✅ Confirm All',
                onTap: onConfirm,
                color: AppTheme.green,
              ),
              _ChipButton(
                label: '↩️ Undo Channels',
                onTap: onUndoChannels,
                color: AppTheme.amber,
              ),
            ],
          ),
        ],
      ),
    );
  }
}

// ── Batch Operations Bar (S2#7) ─────────────────────────────────

class _BatchOperationsBar extends StatelessWidget {
  final int edgeCount;
  final int nodeCount;
  final VoidCallback onApproveEdges;
  final VoidCallback onApproveNodes;

  const _BatchOperationsBar({
    required this.edgeCount,
    required this.nodeCount,
    required this.onApproveEdges,
    required this.onApproveNodes,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'BATCH ACTIONS',
            style: TextStyle(
              color: AppTheme.textTertiary.withValues(alpha: 0.6),
              fontSize: 10,
              letterSpacing: 0.8,
            ),
          ),
          const SizedBox(height: 6),
          Wrap(
            spacing: 6, runSpacing: 6,
            children: [
              if (edgeCount >= 2)
                _ChipButton(
                  label: '✅ Approve $edgeCount edges',
                  onTap: onApproveEdges,
                  color: AppTheme.accent,
                ),
              if (nodeCount >= 2)
                _ChipButton(
                  label: '✅ Approve $nodeCount nodes',
                  onTap: onApproveNodes,
                  color: AppTheme.accent,
                ),
            ],
          ),
        ],
      ),
    );
  }
}

// ── Chip Button Widget ──────────────────────────────────────────

class _ChipButton extends StatelessWidget {
  final String label;
  final VoidCallback onTap;
  final Color color;

  const _ChipButton({
    required this.label,
    required this.onTap,
    required this.color,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      color: color.withValues(alpha: 0.1),
      borderRadius: BorderRadius.circular(8),
      child: InkWell(
        borderRadius: BorderRadius.circular(8),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          child: Text(
            label,
            style: TextStyle(
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

// ── Section Label ───────────────────────────────────────────────

class _SectionLabel extends StatelessWidget {
  final String label;
  const _SectionLabel(this.label);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 6),
      child: Text(
        label.toUpperCase(),
        style: AppTheme.label.copyWith(
          color: AppTheme.textTertiary, fontSize: 11, letterSpacing: 0.8,
        ),
      ),
    );
  }
}
