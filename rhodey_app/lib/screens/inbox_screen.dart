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

  @override
  void initState() {
    super.initState();
    _loadDecisions();
  }

  Future<void> _loadDecisions() async {
    setState(() => _loading = true);
    final result = await _api.getPendingDecisions();
    if (!mounted) return;

    // Build DecisionItems from API results + fallback mock data
    final items = <DecisionItem>[];

    if (result.success) {
      for (final pd in result.data!) {
        double confidence;
        try {
          confidence = (pd.raw['confidence'] as num?)?.toDouble() ?? 0.0;
        } catch (_) {
          confidence = 0.0;
        }
        items.add(DecisionItem(
          id: 'api_${pd.source}_${pd.id}',
          type: _sourceToType(pd.source),
          priority: DecisionPriority.standard,
          title: pd.title,
          description: pd.description,
          confidence: confidence > 0 ? confidence : null,
          createdAt: DateTime.now().subtract(const Duration(hours: 2)),
          metadata: {'api_id': pd.id, 'source': pd.source},
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

  /// Handle approve action — calls the appropriate API endpoint.
  Future<void> _handleApprove(DecisionItem item) async {
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

  Future<void> _handleReject(DecisionItem item) async {
    final meta = item.metadata;
    final apiId = meta['api_id'] as String?;
    final source = meta['source'] as String?;

    if (apiId != null && source != null) {
      final id = int.tryParse(apiId);
      if (id != null) {
        ApiResult result;
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
            result = ApiResult.fail('Unknown source: $source');
        }
        if (result.success) {
          _removeItem(item);
        } else if (mounted) {
          _showSnack(result.error ?? 'Failed to reject');
        }
        return;
      }
    }

    _removeItem(item);
  }

  void _removeItem(DecisionItem item) {
    setState(() => _items.removeWhere((i) => i.id == item.id));
  }

  void _showSnack(String msg) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(msg, style: const TextStyle(fontSize: 12)),
        backgroundColor: AppTheme.red,
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

    final highPriority = _items.where((d) => d.priority == DecisionPriority.high).toList();
    final standardPriority = _items.where((d) => d.priority == DecisionPriority.standard).toList();
    final lowPriority = _items.where((d) => d.priority == DecisionPriority.low).toList();

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
            if (_error.isNotEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                child: Text(
                  '⚠️ $_error',
                  style: const TextStyle(color: AppTheme.red, fontSize: 11),
                ),
              ),
            if (highPriority.isNotEmpty)
              _SectionLabel('High Priority'),
            ...highPriority.map((d) => DecisionCard(
              item: d,
              onApprove: () => _handleApprove(d),
              onReject: () => _handleReject(d),
              onEdit: d.type == DecisionType.person ? () {} : null,
            )),
            if (highPriority.isNotEmpty && standardPriority.isNotEmpty)
              const SizedBox(height: 16),

            if (standardPriority.isNotEmpty)
              _SectionLabel('Standard'),
            ...standardPriority.map((d) => DecisionCard(
              item: d,
              onApprove: () => _handleApprove(d),
              onReject: () => _handleReject(d),
              onEdit: d.type == DecisionType.edge ? () {} : null,
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
          ],
        ),
      ),
    );
  }
}

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
