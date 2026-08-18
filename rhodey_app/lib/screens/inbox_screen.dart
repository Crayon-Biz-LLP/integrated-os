import 'package:flutter/material.dart';
import '../models/decision_item.dart';
import '../services/api_service.dart';
import '../services/inbox_cache.dart';
import '../services/notification_service.dart';
import '../services/persona.dart';
import '../theme/app_theme.dart';
import '../utils/route_observer.dart';
import '../widgets/decision_card.dart';
import '../widgets/merge_search_sheet.dart';
import '../widgets/push_banner.dart';

/// Everything the Quick Confirmation queue can be filtered by — the decision
/// types plus the sections (drafts / FYI) that live alongside the cards.
/// Every chip is always shown (with a live count) so the filter vocabulary
/// never depends on what happens to be pending right now.
enum InboxFilter {
  all,
  drafts,
  fyi,
  edges,
  nodes,
  emails,
  chats,
  calls,
  teams,
  merges,
}

/// Plural noun for a filter chip.
String _chipLabel(InboxFilter f) {
  switch (f) {
    case InboxFilter.edges:
      return 'Edges';
    case InboxFilter.nodes:
      return 'Nodes';
    case InboxFilter.emails:
      return 'Emails';
    case InboxFilter.chats:
      return 'Chats';
    case InboxFilter.calls:
      return 'Calls';
    case InboxFilter.teams:
      return 'Teams';
    case InboxFilter.merges:
      return 'Merges';
    case InboxFilter.drafts:
      return 'Drafts';
    case InboxFilter.fyi:
      return 'FYI';
    case InboxFilter.all:
      return '';
  }
}

/// Singular label for the filtered-empty state.
String _filterLabel(InboxFilter f) {
  switch (f) {
    case InboxFilter.drafts:
      return 'drafts';
    case InboxFilter.fyi:
      return 'FYI items';
    case InboxFilter.edges:
      return 'edges';
    case InboxFilter.nodes:
      return 'nodes';
    case InboxFilter.emails:
      return 'emails';
    case InboxFilter.chats:
      return 'chats';
    case InboxFilter.calls:
      return 'calls';
    case InboxFilter.teams:
      return 'teams items';
    case InboxFilter.merges:
      return 'merge proposals';
    case InboxFilter.all:
      return 'pending items';
  }
}

class InboxScreen extends StatefulWidget {
  /// Push payload when this screen was opened from a notification tap. The
  /// carried `content` renders instantly (WhatsApp-style) while the real
  /// pending list loads in the background.
  final Map<String, dynamic>? pushData;

  const InboxScreen({super.key, this.pushData});

  @override
  State<InboxScreen> createState() => _InboxScreenState();
}

class _InboxScreenState extends State<InboxScreen>
    with RouteAware, WidgetsBindingObserver {
  final _api = ApiService();
  final _inboxCache = InboxCache();
  List<DecisionItem> _items = [];

  /// Pending email reply drafts (from the inbox bundle) — rendered in the
  /// "Email Drafts" section above the decision cards.
  List<Map<String, dynamic>> _drafts = [];

  /// Undecided FYI items (from the inbox bundle) — "For your info" section.
  List<Map<String, dynamic>> _fyi = [];
  bool _loading = true;
  String _error = '';

  /// True once the cached inbox has been painted on the initial open. Guards
  /// the cache-first render so it only runs on the very first load — silent
  /// refreshes, pull-to-refresh, and post-action reloads skip it.
  bool _cachePainted = false;

  /// Count of unverified auto-decisions — fetched on load
  int _autoDecisionCount = 0;

  /// Active filter for the whole Quick Confirmation queue — every section the
  /// inbox can show, not just decision types. Drafts and FYI filter their own
  /// lists; the type filters zoom the decision cards (all = everything).
  InboxFilter _filter = InboxFilter.all;

  /// True while batch-selection mode is active (select → approve/reject).
  bool _selectionMode = false;

  /// True while a background batch is in flight. Guards against starting a
  /// second batch and swaps the action bar to a non-blocking chip until the
  /// server accepts the job.
  bool _batchInFlight = false;

  /// The [NotificationService.onPushReceived] handler set before this screen
  /// mounted, plus the wrapper this screen installs. Saved/restored so the
  /// shell's own handler keeps working while the Inbox is on screen.
  void Function(Map<String, dynamic> data)? _prevOnPushReceived;
  void Function(Map<String, dynamic> data)? _onPushReceivedWrapper;

  /// Selected decision ids (DecisionItem.id — the stable card id). Only
  /// selectable types (everything except clarifications) can be selected.
  final Set<String> _selectedIds = {};

  /// Selected email-draft ids (draft['id']) when the Drafts filter is active.
  final Set<int> _selectedDraftIds = {};

  /// Selected FYI ids (item['id']) when the FYI filter is active.
  final Set<int> _selectedFyiIds = {};

  /// Notification content carried in the push — rendered instantly on tap so
  /// the Inbox never shows a bare spinner before the real list arrives.
  String _pushContent = '';

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _pushContent = (widget.pushData?['content'] as String?)?.trim() ?? '';
    // Chain on top of any existing handler (e.g. the home shell's): while the
    // Inbox is mounted, a background-batch completion push triggers a silent
    // reload so the queue reconciles the moment Rhodey finishes the job.
    _prevOnPushReceived = NotificationService.onPushReceived;
    _onPushReceivedWrapper = (data) {
      _prevOnPushReceived?.call(data);
      final type = data['type'];
      if ((type == 'batch_done' || type == 'batch_failed') && mounted) {
        _refreshSilently();
      }
    };
    NotificationService.onPushReceived = _onPushReceivedWrapper;
    _loadDecisions();
    _loadAutoDecisionCount();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final route = ModalRoute.of(context);
    if (route != null) routeObserver.subscribe(this, route);
  }

  @override
  void dispose() {
    routeObserver.unsubscribe(this);
    WidgetsBinding.instance.removeObserver(this);
    // Restore the previous handler so the shell keeps receiving pushes after
    // the Inbox unmounts.
    if (NotificationService.onPushReceived == _onPushReceivedWrapper) {
      NotificationService.onPushReceived = _prevOnPushReceived;
    }
    super.dispose();
  }

  /// Refresh when a route pushed on top of the Inbox pops (e.g. an item was
  /// actioned from the home focal card while the Inbox sat in the stack).
  @override
  void didPopNext() {
    _refreshSilently();
  }

  /// Refresh when the app returns to foreground — items may have been
  /// actioned from the home focal card, another device, or Telegram.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      _refreshSilently();
    }
  }

  void _refreshSilently() {
    _loadDecisions(silent: true);
    _loadAutoDecisionCount();
  }

  Future<void> _loadDecisions({bool silent = false}) async {
    // WhatsApp-style instant render: on the initial open, paint the cached
    // inbox from disk BEFORE any network round-trip, so the Inbox never shows
    // a bare spinner. The live fetch below reconciles and re-saves.
    if (!silent && !_cachePainted) {
      final cached = await _inboxCache.load();
      if (mounted && cached.items.isNotEmpty) {
        setState(() {
          _items = cached.items;
          _autoDecisionCount = cached.autoDecisionCount;
          _loading = false;
        });
      } else if (mounted) {
        setState(() => _loading = true);
      }
    } else if (!silent && mounted) {
      setState(() => _loading = true);
    }
    _cachePainted = true;

    // One round-trip via the collapsed /api/inbox endpoint when available;
    // falls back to the legacy 4-call path for older backends.
    final bundle = await _api.getInboxBundle();
    if (!mounted) return;

    final result = bundle.success
        ? ApiResult.ok(bundle.data!.decisions)
        : await _api.getPendingDecisions();
    if (bundle.success) {
      setState(() {
        _autoDecisionCount = bundle.data!.autoDecisionCount;
        _drafts = bundle.data!.drafts;
        _fyi = bundle.data!.fyi;
      });
    }
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
        final nodeType =
            pd.raw['type'] as String? ?? pd.raw['node_type'] as String?;
        final isMerge = status == 'merge_proposed';
        final source = pd.source;
        final type = isMerge ? DecisionType.merge : _sourceToType(source);

        // Channel items carry their ingest chat key in metadata.chat_id (the
        // room name or phone) — the reply flow sends back to that chat. A
        // native Matrix event id marks the item as Beeper-sourced.
        final rawMeta = pd.raw['metadata'] is Map
            ? (pd.raw['metadata'] as Map).cast<String, dynamic>()
            : <String, dynamic>{};
        final chatId = rawMeta['chat_id'] as String?;
        final messageId = pd.raw['message_id'] as String?;
        final viaBeeper = messageId != null && messageId.isNotEmpty;

        items.add(
          DecisionItem(
            id: 'api_${source}_${pd.id}',
            type: type,
            priority: _derivePriority(type, confidence, isMerge),
            title: pd.title,
            description: isMerge
                ? 'Merge proposed — accept to combine duplicate nodes'
                : pd.description,
            confidence: confidence > 0 ? confidence : null,
            createdAt: DateTime.now().subtract(const Duration(hours: 2)),
            nodeType: nodeType,
            chatId: chatId,
            viaBeeper: viaBeeper,
            metadata: {'api_id': pd.id, 'source': source},
          ),
        );
      }
    } else {
      _error = result.error ?? '';
    }

    if (mounted) {
      setState(() {
        // Keep the last-known-good list on ANY failed fetch (cached or live)
        // so a flaky connection at cold open can never blank the instantly-
        // rendered inbox; the full spinner only shows on first load.
        if (!result.success && _items.isNotEmpty) return;
        _items = items;
        _loading =
            false;        // A filter pointing at a category with no remaining items would
        // strand the screen on an empty view (and its chip has now
        // disappeared) — fall back to All.
        if (_filter != InboxFilter.all &&
            !_categoryHasItems(_filter)) {
          _filter = InboxFilter.all;
        }
        // Prune selections that no longer exist after a silent reload (items
        // actioned elsewhere) — the "N selected" count must stay honest.
        _selectedIds.retainWhere((id) => _items.any((d) => d.id == id));
        _selectedDraftIds.retainWhere(
          (id) => _drafts.any((d) => d['id'] == id),
        );
        _selectedFyiIds.retainWhere(
          (id) => _fyi.any((f) => f['id'] == id),
        );
      });
    }

    // Write-behind: keep the on-device cache fresh for the next open. Only
    // persist a successful (possibly empty) list so a stale cache can never
    // be wiped by a transient failure. Items live in their own key — the
    // parallel count fetch can't clobber them.
    if (result.success) {
      await _inboxCache.saveItems(items);
    }
  }

  Future<void> _loadAutoDecisionCount() async {
    final result = await _api.get(
      '/api/auto-decisions',
      query: {'status': 'unverified', 'limit': '1'},
    );
    if (!mounted) return;
    if (result.success && result.data is Map) {
      final count = (result.data as Map)['count'] as int? ?? 0;
      setState(() => _autoDecisionCount = count);
      // Persist so the auto-decision banner also renders instantly on open.
      // The count has its own cache key — no read-modify-write, so this
      // parallel fetch can never clobber the items cache.
      await _inboxCache.saveCount(count);
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
      case 'teams':
        return DecisionType.teams;
      case 'graph_node':
        return DecisionType.person;
      case 'graph_edge':
        return DecisionType.edge;
      default:
        return DecisionType.clarification;
    }
  }

  /// Derive the priority bucket from what the feed actually tells us — the
  /// old code hardcoded every non-merge item to standard, so the High
  /// Priority / Low Effort sections were always empty.
  ///
  /// Rules (deliberately simple, judgment-based):
  ///   - merges stay high (they decide data correctness)
  ///   - low-confidence graph items (< 0.7) need human judgment → high
  ///   - high-confidence graph items (>= 0.9) are near-certain → low effort
  ///   - everything else (channels, clarifications, mid-confidence graph) → standard
  DecisionPriority _derivePriority(
    DecisionType type,
    double confidence,
    bool isMerge,
  ) {
    if (isMerge) return DecisionPriority.high;
    // Clarifications are "awaiting your input" — blocking, like a merge.
    if (type == DecisionType.clarification) return DecisionPriority.high;
    final isGraph = type == DecisionType.edge || type == DecisionType.person;
    if (isGraph) {
      if (confidence > 0 && confidence < 0.7) return DecisionPriority.high;
      if (confidence >= 0.9) return DecisionPriority.low;
    }
    return DecisionPriority.standard;
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
                width: 36,
                height: 4,
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
                  color: AppTheme.textTertiary,
                  fontSize: 12,
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: textController,
                autofocus: true,
                textInputAction: TextInputAction.done,
                onSubmitted: (v) => Navigator.pop(ctx, v.trim()),
                decoration: InputDecoration(
                  hintText: 'e.g. Friend from church, VP at their company',
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(10),
                    borderSide: BorderSide(color: AppTheme.border),
                  ),
                  contentPadding: const EdgeInsets.symmetric(
                    horizontal: 14,
                    vertical: 12,
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
                              color: AppTheme.textSecondary,
                              fontSize: 13,
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
      await _doApprovePerson(
        item,
        context: contextResult.isEmpty ? null : contextResult,
      );
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

  // ── Org relationship prompt ───────────────────────────────────

  /// Shows a relationship prompt after an org is approved.
  /// Lets user link the new org to an existing org (Vendor/Client/Partner).
  void _showOrgRelationshipPrompt(Map<String, dynamic> followUp) {
    final newOrg = followUp['new_org'] as Map<String, dynamic>?;
    final knownOrgs = (followUp['known_orgs'] as List?)?.cast<Map<String, dynamic>>() ?? [];
    final message = followUp['message'] as String? ?? 'How does this org relate to your orgs?';
    final options = (followUp['options'] as List?)?.cast<String>() ?? ['Vendor', 'Client', 'Partner', 'Standalone'];

    if (newOrg == null || knownOrgs.isEmpty || !mounted) return;

    showModalBottomSheet(
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
                width: 36,
                height: 4,
                decoration: BoxDecoration(
                  color: AppTheme.textTertiary.withValues(alpha: 0.3),
                  borderRadius: BorderRadius.circular(2),
                ),
                margin: const EdgeInsets.only(bottom: 16),
              ),
              Text(
                message,
                style: AppTheme.title.copyWith(fontSize: 15),
              ),
              const SizedBox(height: 16),
              // Relationship type buttons
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: options.map((opt) {
                  return Material(
                    color: AppTheme.accent.withValues(alpha: 0.1),
                    borderRadius: BorderRadius.circular(10),
                    child: InkWell(
                      borderRadius: BorderRadius.circular(10),
                      onTap: () {
                        Navigator.pop(ctx);
                        if (opt.toLowerCase() == 'standalone') return;
                        _showOrgPicker(newOrg, opt, knownOrgs);
                      },
                      child: Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                        child: Text(
                          opt,
                          style: TextStyle(
                            color: AppTheme.accent,
                            fontSize: 14,
                            fontWeight: FontWeight.w500,
                          ),
                        ),
                      ),
                    ),
                  );
                }).toList(),
              ),
              const SizedBox(height: 12),
              // Skip button
              SizedBox(
                width: double.infinity,
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
                        'Skip for now',
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: AppTheme.textSecondary,
                          fontSize: 13,
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  /// Shows org picker after relationship type is selected.
  void _showOrgPicker(Map<String, dynamic> newOrg, String relationship, List<Map<String, dynamic>> knownOrgs) {
    showModalBottomSheet(
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
                width: 36,
                height: 4,
                decoration: BoxDecoration(
                  color: AppTheme.textTertiary.withValues(alpha: 0.3),
                  borderRadius: BorderRadius.circular(2),
                ),
                margin: const EdgeInsets.only(bottom: 16),
              ),
              Text(
                '${newOrg['label']} — $relationship to which org?',
                style: AppTheme.title.copyWith(fontSize: 15),
              ),
              const SizedBox(height: 16),
              // Org list
              ...knownOrgs.map((org) {
                return ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text(
                    org['label'] ?? 'Unknown',
                    style: AppTheme.body,
                  ),
                  trailing: const Icon(Icons.chevron_right, color: AppTheme.textTertiary),
                  onTap: () {
                    Navigator.pop(ctx);
                    _showNoteInput(newOrg, org, relationship);
                  },
                );
              }),
            ],
          ),
        ),
      ),
    );
  }

  /// Shows optional note input after org is selected.
  void _showNoteInput(Map<String, dynamic> newOrg, Map<String, dynamic> targetOrg, String relationship) {
    final textController = TextEditingController();
    showModalBottomSheet<String>(
      context: context,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      isScrollControlled: true,
      builder: (ctx) => Padding(
        padding: EdgeInsets.fromLTRB(
          20, 16, 20, MediaQuery.of(ctx).viewInsets.bottom + 24,
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
              '${newOrg['label']} → $relationship → ${targetOrg['label']}',
              style: AppTheme.title.copyWith(fontSize: 15),
            ),
            const SizedBox(height: 4),
            Text(
              'Add a note? (optional)',
              style: AppTheme.bodySmall.copyWith(
                color: AppTheme.textTertiary,
                fontSize: 12,
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: textController,
              autofocus: true,
              textInputAction: TextInputAction.done,
              onSubmitted: (v) => Navigator.pop(ctx, v.trim()),
              decoration: InputDecoration(
                hintText: 'e.g. Hotel bookings for church events',
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide: BorderSide(color: AppTheme.border),
                ),
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 14,
                  vertical: 12,
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
                            color: AppTheme.textSecondary,
                            fontSize: 13,
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
                        Navigator.pop(ctx, text.isNotEmpty ? text : '');
                      },
                      child: Container(
                        padding: const EdgeInsets.symmetric(vertical: 12),
                        child: const Text(
                          'Save',
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
    ).then((note) {
      if (note != null) {
        _createOrgRelationship(newOrg, targetOrg, relationship, note: note.isEmpty ? null : note);
      }
    });
  }

  /// Creates the org→org relationship via API.
  Future<void> _createOrgRelationship(
    Map<String, dynamic> newOrg,
    Map<String, dynamic> targetOrg,
    String relationship, {
    String? note,
  }) async {
    if (!mounted) return;

    final sourceId = newOrg['id'] as String?;
    final targetId = targetOrg['id'] as String?;
    if (sourceId == null || targetId == null) return;

    final relMap = {
      'Vendor': 'VENDOR_TO',
      'Client': 'CLIENT_OF',
      'Partner': 'PARTNER',
    };
    final apiRel = relMap[relationship] ?? relationship.toUpperCase();

    final result = await _api.createOrgRelationship(
      sourceOrgId: sourceId,
      targetOrgId: targetId,
      relationship: apiRel,
      note: note,
    );

    if (mounted) {
      if (result.success) {
        _showSnack('✅ ${newOrg['label']} linked to ${targetOrg['label']} ($relationship)', isError: false);
      } else {
        _showSnack(result.error ?? 'Failed to create relationship');
      }
    }
  }

  // ── Merge into existing (mirrors the web dashboard) ───────────

  /// Opens a search sheet to pick an existing live node to merge [item] into.
  Future<void> _showMergeSheet(DecisionItem item) async {
    final target = await MergeSearchSheet.show(
      context,
      nodeType: item.nodeType,
    );
    if (target == null || !mounted) return;
    await _handleMerge(item, target);
  }

  /// Executes the merge against /api/graph-node-merge, then removes the item
  /// from the list (with cache write-through) on success.
  Future<void> _handleMerge(
    DecisionItem item,
    Map<String, dynamic> target,
  ) async {
    final apiId = item.metadata['api_id'] as String?;
    if (apiId == null) return;
    final id = int.tryParse(apiId);
    if (id == null) return;
    final targetId = target['id'] as String?;
    if (targetId == null) return;
    final targetLabel = target['label'] as String? ?? 'existing node';

    // Pending → live merge: scope='pending' so the backend rewires the
    // pending node's edges into the target and marks the source merged.
    final result = await _api.mergeGraphNode(
      id.toString(),
      targetId,
      scope: 'pending',
    );
    if (!mounted) return;
    if (result.success) {
      _removeItem(item);
      _showSnack('🔀 Merged into "$targetLabel"', isError: false);
    } else {
      _showSnack(result.error ?? 'Failed to merge');
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
          case 'teams':
            result = await _api.approveTeams(id);
            break;
          case 'graph_node':
          case 'clarification':
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
          _offerUndo('Approved', result);
          // Check for org relationship follow-up
          final followUp = result.data?['follow_up'];
          if (followUp != null && followUp['type'] == 'org_relationship') {
            _showOrgRelationshipPrompt(followUp);
          }
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
        case 'teams':
          result = await _api.rejectTeams(id);
          break;
        case 'graph_node':
        case 'clarification':
          result = await _api.rejectGraphNode(id);
          break;
        case 'graph_edge':
          result = await _api.rejectGraphEdge(id);
          break;
        default:
          return; // unknown source, item already removed
      }
    }

    if (!mounted) return;
    if (result.success) {
      // The item was already removed optimistically; offer the undo net so
      // an accidental reject is a one-tap recovery, not a permanent loss.
      _offerUndo('Rejected', result);
    } else {
      _showSnack(result.error ?? '⚠️ Reject failed on server');
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
                width: 36,
                height: 4,
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
                  color: AppTheme.textTertiary,
                  fontSize: 12,
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
                    horizontal: 14,
                    vertical: 12,
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
                              color: AppTheme.textSecondary,
                              fontSize: 13,
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
          final result = await _api.approveGraphEdgeWithRelation(
            id,
            newRelation,
          );
          if (result.success) {
            _removeItem(item);
          } else {
            _showSnack(result.error ?? 'Failed to approve edge');
          }
        }
      }
    }
  }

  // ── Beeper Reply (Phase C — reply straight into the chat) ──────

  Future<void> _showBeeperReplySheet(DecisionItem item) async {
    final chatKey = item.chatId;
    if (chatKey == null || chatKey.isEmpty) return;

    final textController = TextEditingController();
    final reply = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (ctx) => Padding(
        padding: EdgeInsets.only(
          left: 20,
          right: 20,
          top: 16,
          bottom: MediaQuery.of(ctx).viewInsets.bottom + 24,
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
            Row(
              children: [
                const Text('💬', style: TextStyle(fontSize: 14)),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    'Reply to $chatKey',
                    style: AppTheme.title.copyWith(fontSize: 15),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              'Sends via Beeper to the original chat',
              style: AppTheme.bodySmall.copyWith(
                color: AppTheme.textTertiary,
                fontSize: 12,
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: textController,
              autofocus: true,
              maxLines: 4,
              minLines: 2,
              textInputAction: TextInputAction.newline,
              decoration: InputDecoration(
                hintText: 'Type your reply…',
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide: BorderSide(color: AppTheme.border),
                ),
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 14,
                  vertical: 12,
                ),
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
                          'Cancel',
                          textAlign: TextAlign.center,
                          style: TextStyle(
                            color: AppTheme.textSecondary,
                            fontSize: 13,
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
                        Navigator.pop(ctx, text);
                      },
                      child: Container(
                        padding: const EdgeInsets.symmetric(vertical: 12),
                        child: const Text(
                          'Send via Beeper',
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
    );

    if (reply != null && reply.isNotEmpty && mounted) {
      await _sendBeeperReply(item, reply);
    }
  }

  Future<void> _sendBeeperReply(DecisionItem item, String message) async {
    final chatKey = item.chatId;
    if (chatKey == null || chatKey.isEmpty) return;

    final result = await _api.sendBeeperMessage(chatKey, message);
    if (!mounted) return;
    if (result.success) {
      // The send is recorded as outgoing → auto-resolve marks this chat's
      // pending items responded, so the card (and its siblings in the chat)
      // are handled — reload the feed to reflect the resolved state.
      _showSnack('💬 Reply sent via Beeper', isError: false);
      _removeItem(item);
      _loadDecisions(silent: true);
    } else {
      final err = result.error ?? 'Failed to send reply';
      // no_room is the honest failure for legacy chat keys the bridge hasn't
      // seen yet — say so instead of a generic error.
      if (err.contains('no Matrix room')) {
        _showSnack(
          '⚠️ This chat isn\'t synced by the bridge yet — approve or reject instead',
        );
      } else {
        _showSnack(err);
      }
    }
  }

  // ── Auto-Decision Confirm/Undo (S1#3) ────────────────────────

  // ── Email Drafts ──────────────────────────────────────────

  Future<void> _sendDraft(Map<String, dynamic> draft) async {
    final draftId = draft['id'] as int?;
    if (draftId == null) return;
    final result = await _api.draftAction(draftId, action: 'send');
    if (!mounted) return;
    if (result.success) {
      setState(() => _drafts.removeWhere((d) => d['id'] == draftId));
      _showSnack('📤 Draft sent', isError: false);
    } else {
      _showSnack(result.error ?? 'Failed to send draft');
    }
  }

  Future<void> _dropDraft(Map<String, dynamic> draft) async {
    final draftId = draft['id'] as int?;
    if (draftId == null) return;
    final result = await _api.draftAction(draftId, action: 'drop');
    if (!mounted) return;
    if (result.success) {
      setState(() => _drafts.removeWhere((d) => d['id'] == draftId));
      _showSnack('Draft dropped', isError: false);
    } else {
      _showSnack(result.error ?? 'Failed to drop draft');
    }
  }

  /// Edit a draft body before sending (mirrors the web dashboard).
  Future<void> _editDraft(Map<String, dynamic> draft) async {
    final draftId = draft['id'] as int?;
    if (draftId == null) return;
    final textController = TextEditingController(
      text: draft['draft_body'] as String? ?? '',
    );

    final newBody = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (ctx) => Padding(
        padding: EdgeInsets.only(
          left: 20,
          right: 20,
          top: 16,
          bottom: MediaQuery.of(ctx).viewInsets.bottom + 24,
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
            Text('Edit draft', style: AppTheme.title.copyWith(fontSize: 15)),
            const SizedBox(height: 4),
            Text(
              draft['subject'] as String? ?? 'Email draft',
              style: AppTheme.bodySmall.copyWith(
                color: AppTheme.textTertiary,
                fontSize: 12,
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: textController,
              autofocus: true,
              maxLines: 8,
              minLines: 4,
              textInputAction: TextInputAction.newline,
              decoration: InputDecoration(
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(10),
                  borderSide: BorderSide(color: AppTheme.border),
                ),
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 14,
                  vertical: 12,
                ),
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
                            color: AppTheme.textSecondary,
                            fontSize: 13,
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
                          'Save',
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
    );

    if (newBody != null && newBody.isNotEmpty && mounted) {
      final result = await _api.editDraft(draftId, newBody);
      if (!mounted) return;
      if (result.success) {
        setState(() {
          final i = _drafts.indexWhere((d) => d['id'] == draftId);
          if (i >= 0) _drafts[i] = {..._drafts[i], 'draft_body': newBody};
        });
        _showSnack('✅ Draft updated', isError: false);
      } else {
        _showSnack(result.error ?? 'Failed to update draft');
      }
    }
  }

  // ── FYI ───────────────────────────────────────────────────

  Future<void> _acknowledgeFyi(Map<String, dynamic> item) async {
    final itemId = item['id'] as int?;
    if (itemId == null) return;
    final result = await _api.acknowledgeFyi(itemId);
    if (!mounted) return;
    if (result.success) {
      setState(() => _fyi.removeWhere((f) => f['id'] == itemId));
    } else {
      _showSnack(result.error ?? 'Failed to acknowledge');
    }
  }

  Future<void> _confirmAllAutoDecisions() async {
    final result = await _api.post('/api/auto-decisions/confirm');
    if (!mounted) return;
    if (result.success) {
      setState(() => _autoDecisionCount = 0);
      // Write-through so the banner doesn't reappear from cache on next open.
      _inboxCache.saveItems(List.of(_items));
      _inboxCache.saveCount(0);
      _showSnack('✅ Auto-decisions verified', isError: false);
    } else {
      _showSnack(result.error ?? 'Failed to confirm');
    }
  }

  Future<void> _undoAutoChannelDecisions() async {
    final result = await _api.post(
      '/api/auto-decisions/undo',
      body: {'target': 'channels'},
    );
    if (!mounted) return;
    if (result.success) {
      _loadDecisions();
      _showSnack('↩️ Undone channel auto-decisions', isError: false);
    } else {
      _showSnack(result.error ?? 'Failed to undo');
    }
  }

  // ── Batch Selection (filter + select → approve/reject) ───────

  void _toggleSelectionMode() {
    setState(() {
      _selectionMode = !_selectionMode;
      _selectedIds.clear();
      _selectedDraftIds.clear();
      _selectedFyiIds.clear();
    });
  }

  /// How many items are selected under the ACTIVE filter — drafts count draft
  /// ids, FYI counts fyi ids, everything else counts decision-card ids. Keeps
  /// the selection-bar count honest per section.
  int _selectedCount() {
    if (_filter == InboxFilter.drafts) return _selectedDraftIds.length;
    if (_filter == InboxFilter.fyi) return _selectedFyiIds.length;
    return _selectedIds.length;
  }

  void _toggleSelectDraft(int id) {
    setState(() {
      if (!_selectedDraftIds.add(id)) _selectedDraftIds.remove(id);
    });
  }

  void _toggleSelectFyi(int id) {
    setState(() {
      if (!_selectedFyiIds.add(id)) _selectedFyiIds.remove(id);
    });
  }

  /// An item can be batch-selected only when it has a resolvable server id
  /// (api_id) — a selection that can't be acted on would silently vanish from
  /// the batch and make the "N selected" count lie.
  bool _isSelectable(DecisionItem d) {
    if (d.type == DecisionType.clarification) return false;
    return int.tryParse(d.metadata['api_id'] as String? ?? '') != null;
  }

  /// Whether the active filter's category currently has any items — the same
  /// gate the chip row uses, so a filter never outlives its own chip.
  bool _categoryHasItems(InboxFilter f) {
    switch (f) {
      case InboxFilter.drafts:
        return _drafts.isNotEmpty;
      case InboxFilter.fyi:
        return _fyi.isNotEmpty;
      case InboxFilter.all:
        return true;
      default:
        final t = _filterType(f);
        return t != null && _items.any((d) => d.type == t);
    }
  }

  /// The decision type a filter chip zooms to, or null for the sections
  /// (drafts / fyi / all) that don't map to a card type.
  DecisionType? _filterType(InboxFilter f) {
    switch (f) {
      case InboxFilter.edges:
        return DecisionType.edge;
      case InboxFilter.nodes:
        return DecisionType.person;
      case InboxFilter.emails:
        return DecisionType.email;
      case InboxFilter.chats:
        return DecisionType.whatsapp;
      case InboxFilter.calls:
        return DecisionType.call;
      case InboxFilter.teams:
        return DecisionType.teams;
      case InboxFilter.merges:
        return DecisionType.merge;
      case InboxFilter.all:
      case InboxFilter.drafts:
      case InboxFilter.fyi:
        return null;
    }
  }

  void _toggleSelect(String id) {
    for (final d in _items) {
      if (d.id == id) {
        if (!_isSelectable(d)) return;
        setState(() {
          if (!_selectedIds.add(id)) _selectedIds.remove(id);
        });
        return;
      }
    }
  }

  void _selectAllVisible() {
    setState(() {
      _selectedIds.clear();
      _selectedDraftIds.clear();
      _selectedFyiIds.clear();
      if (_filter == InboxFilter.drafts) {
        for (final d in _drafts) {
          final id = d['id'] as int?;
          if (id != null) _selectedDraftIds.add(id);
        }
        return;
      }
      if (_filter == InboxFilter.fyi) {
        for (final f in _fyi) {
          final id = f['id'] as int?;
          if (id != null) _selectedFyiIds.add(id);
        }
        return;
      }
      // A type filter only selects cards of that type; All selects every
      // selectable card.
      for (final d in _items) {
        if (!_isSelectable(d)) continue;
        if (_filter != InboxFilter.all && d.type != _filterType(_filter)) {
          continue;
        }
        _selectedIds.add(d.id);
      }
    });
  }

  /// Group the selected items by type and route each group through its batch
  /// endpoint (the same per-item handlers the Inbox approve/reject use, so
  /// every decision still writes its learning signals). Merges have no batch
  /// route — they go through the per-item accept/reject calls.
  Future<void> _batchSelected(String action) async {
    if (_selectedIds.isEmpty) return;

    final selected = _items.where((d) => _selectedIds.contains(d.id)).toList();
    final graphCount = selected
        .where(
          (d) => d.type == DecisionType.person || d.type == DecisionType.edge,
        )
        .length;
    final verb = action == 'approve' ? 'Approve' : 'Reject';
    final message = action == 'reject' && graphCount > 0
        ? 'Rejecting ${_selectedIds.length} item(s) — $graphCount graph '
              'item(s). Graph rejects are permanent and can\'t be undone. '
              'Continue?'
        : '$verb ${_selectedIds.length} selected item(s)?';
    final confirm = await _showBatchConfirm(message);
    if (!confirm || !mounted) return;

    // Route by type: graph + channel batches run in the BACKGROUND — the tap
    // returns in milliseconds, the items leave the queue optimistically, and
    // Rhodey pushes the final counts when done. Merges have no batch route,
    // so they stay per-item and awaited.
    final batchTypes = {
      DecisionType.edge,
      DecisionType.person,
      DecisionType.email,
      DecisionType.whatsapp,
      DecisionType.call,
      DecisionType.teams,
    };
    final batchItems =
        selected.where((d) => batchTypes.contains(d.type)).toList();
    final mergeItems =
        selected.where((d) => d.type == DecisionType.merge).toList();

    if (batchItems.isNotEmpty) {
      final byType = <DecisionType, List<int>>{};
      for (final d in batchItems) {
        final apiId = int.tryParse(d.metadata['api_id'] as String? ?? '');
        if (apiId != null) byType.putIfAbsent(d.type, () => []).add(apiId);
      }

      setState(() => _batchInFlight = true);

      // Optimistic: drop the in-flight items now so the list stays usable.
      // The completion push reconciles; anything that fails reloads back.
      final inFlightApiIds = byType.values.expand((v) => v).toSet();
      setState(() {
        _items.removeWhere((d) {
          final apiId = int.tryParse(d.metadata['api_id'] as String? ?? '');
          return apiId != null && inFlightApiIds.contains(apiId);
        });
      });

      var outstanding = byType.length;
      for (final entry in byType.entries) {
        final ids = entry.value;
        final Future<ApiResult<dynamic>> req;
        switch (entry.key) {
          case DecisionType.edge:
            req = _api.post(
              '/api/graph-edge-action/batch',
              body: {'ids': ids, 'action': action, 'background': true},
              timeout: const Duration(seconds: 30),
              maxRetries: 0,
            );
            break;
          case DecisionType.person:
            req = _api.post(
              '/api/graph-node-action/batch',
              body: {'ids': ids, 'action': action, 'background': true},
              timeout: const Duration(seconds: 30),
              maxRetries: 0,
            );
            break;
          default:
            req = _api.batchChannelAction(
              entry.key.name,
              ids,
              action: action,
              background: true,
            );
            break;
        }
        // Fire and forget: the accepted response lands in milliseconds; the
        // real status arrives as the completion push.
        req.then((r) {
          outstanding -= 1;
          if (!mounted) return;
          if (!r.success) {
            _showSnack(r.error ?? 'Failed to start $verb batch',
                isError: true);
            _refreshSilently();
          }
          if (outstanding <= 0) setState(() => _batchInFlight = false);
        }).catchError((_) {
          if (mounted) setState(() => _batchInFlight = false);
        });
      }
    }

    // Merges: per-item, awaited, removed on success (no batch route exists).
    var mergeProcessed = 0;
    var mergeFailed = 0;
    for (final d in mergeItems) {
      final apiId = int.tryParse(d.metadata['api_id'] as String? ?? '');
      if (apiId == null) continue;
      final r = action == 'approve'
          ? await _api.acceptMerge(apiId)
          : await _api.rejectMerge(apiId);
      if (!mounted) return;
      if (r.success) {
        mergeProcessed += 1;
        _removeItem(d);
      } else {
        mergeFailed += 1;
      }
    }

    if (!mounted) return;
    _selectionMode = false;
    _selectedIds.clear();
    if (mergeItems.isNotEmpty) {
      _showSnack(
        mergeFailed == 0
            ? '✅ $verb $mergeProcessed merge(s)'
            : '⚠️ $verb $mergeProcessed, $mergeFailed failed',
        isError: mergeFailed > 0,
      );
    }
    // No reload here — the background job hasn't committed yet, so a fetch
    // would resurrect the just-removed items while the server is still
    // processing. The completion push (batch_done/batch_failed) reconciles.
  }

  /// Batch send or drop the selected email drafts (Drafts filter).
  Future<void> _batchDrafts(String action) async {
    if (_selectedDraftIds.isEmpty) return;
    final verb = action == 'send' ? 'Send' : 'Drop';
    final confirm = await _showBatchConfirm(
      '$verb ${_selectedDraftIds.length} selected draft(s)?',
    );
    if (!confirm || !mounted) return;

    var processed = 0;
    var failed = 0;
    
    setState(() => _loading = true);
    
    for (final id in _selectedDraftIds.toList()) {
      final result = await _api.draftAction(id, action: action);
      if (result.success) {
        processed += 1;
      } else {
        failed += 1;
      }
    }
    if (!mounted) return;
    _selectionMode = false;
    _selectedDraftIds.clear();
    _loadDecisions();
    _showSnack(
      failed == 0
          ? '✅ $verb $processed draft(s)'
          : '⚠️ $verb $processed, $failed failed',
      isError: failed > 0,
    );
  }

  /// Batch acknowledge (Got it) the selected FYI items (FYI filter).
  Future<void> _batchFyi() async {
    if (_selectedFyiIds.isEmpty) return;
    if (_batchInFlight) {
      _showSnack('A batch is already processing…');
      return;
    }
    final confirm = await _showBatchConfirm(
      'Got it — acknowledge ${_selectedFyiIds.length} selected FYI item(s)?',
    );
    if (!confirm || !mounted) return;

    setState(() => _batchInFlight = true);

    // Optimistic: drop the selected FYI items now. The completion push
    // reconciles; anything that fails reloads back.
    final selected = _selectedFyiIds.toList();
    setState(() {
      _fyi.removeWhere((f) => selected.contains(f['id']));
    });

    _api.batchFyiAction(selected, background: true).then((r) {
      if (!mounted) return;
      if (!r.success) {
        _showSnack(r.error ?? 'Failed to start Got it batch', isError: true);
        _refreshSilently();
      }
      setState(() => _batchInFlight = false);
    }).catchError((_) {
      if (mounted) setState(() => _batchInFlight = false);
    });

    _selectionMode = false;
    _selectedFyiIds.clear();
    // No reload here — the job hasn't committed yet, so a fetch would bring
    // the just-removed items back while the server is still processing. The
    // completion push (batch_done/batch_failed) reconciles.
  }

  /// Horizontal chip row that filters the whole queue. A chip appears only
  /// when its category actually has items in the queue (drafts / fyi / any
  /// decision type) — an empty chip is noise. All is always shown as the
  /// default, and the row stays clean because zero-count categories are
  /// simply absent.
  Widget _buildFilterChips() {
    final chips = <(InboxFilter, int)>[
      if (_drafts.isNotEmpty)
        (InboxFilter.drafts, _drafts.length),
      if (_fyi.isNotEmpty) (InboxFilter.fyi, _fyi.length),
      if (_items.any((d) => d.type == DecisionType.edge))
        (InboxFilter.edges,
            _items.where((d) => d.type == DecisionType.edge).length),
      if (_items.any((d) => d.type == DecisionType.person))
        (InboxFilter.nodes,
            _items.where((d) => d.type == DecisionType.person).length),
      if (_items.any((d) => d.type == DecisionType.email))
        (InboxFilter.emails,
            _items.where((d) => d.type == DecisionType.email).length),
      if (_items.any((d) => d.type == DecisionType.whatsapp))
        (InboxFilter.chats,
            _items.where((d) => d.type == DecisionType.whatsapp).length),
      if (_items.any((d) => d.type == DecisionType.call))
        (InboxFilter.calls,
            _items.where((d) => d.type == DecisionType.call).length),
      if (_items.any((d) => d.type == DecisionType.teams))
        (InboxFilter.teams,
            _items.where((d) => d.type == DecisionType.teams).length),
      if (_items.any((d) => d.type == DecisionType.merge))
        (InboxFilter.merges,
            _items.where((d) => d.type == DecisionType.merge).length),
    ];
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: Row(
        children: [
          _ChipButton(
            label: 'All',
            color: _filter == InboxFilter.all
                ? AppTheme.accent
                : AppTheme.textTertiary,
            // Clearing selection on filter change keeps the batch honest:
            // selections never silently include items hidden by the filter.
            onTap: () => setState(() {
              _filter = InboxFilter.all;
              _selectedIds.clear();
            }),
          ),
          for (final (f, count) in chips) ...[
            const SizedBox(width: 6),
            _ChipButton(
              label: '${_chipLabel(f)} $count',
              color: _filter == f
                  ? AppTheme.accent
                  : AppTheme.textTertiary,
              onTap: () => setState(() {
                _filter = f;
                _selectedIds.clear();
              }),
            ),
          ],
        ],
      ),
    );
  }

  /// Bottom action bar while selection mode is active. The actions adapt to
  /// the active filter: Drafts get Send/Drop, FYI gets Got it, decision
  /// filters get Approve/Reject.
  Widget _buildSelectionBar() {
    final n = _selectedCount();
    // Drafts / FYI have no approve/reject — their batch verbs differ.
    final isDrafts = _filter == InboxFilter.drafts;
    final isFyi = _filter == InboxFilter.fyi;
    return SafeArea(
      child: Container(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 10),
        decoration: BoxDecoration(
          color: AppTheme.surface,
          border: const Border(top: BorderSide(color: AppTheme.border)),
        ),
        // Horizontally scrollable so the bar never overflows on narrow
        // phones (count label + Select all + actions).
        child: SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: [
              Text(
                n == 0 ? 'Select items' : '$n selected',
                style: AppTheme.caption.copyWith(
                  color: n == 0 ? AppTheme.textTertiary : AppTheme.accent,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(width: 12),
              _ChipButton(
                label: 'Select all',
                onTap: _selectAllVisible,
                color: AppTheme.textTertiary,
              ),
              const SizedBox(width: 6),
              if (isDrafts) ...[
                _ChipButton(
                  label: '📤 Send',
                  onTap: n == 0 ? null : () => _batchDrafts('send'),
                  color: AppTheme.green,
                ),
                const SizedBox(width: 6),
                _ChipButton(
                  label: '🗑 Drop',
                  onTap: n == 0 ? null : () => _batchDrafts('drop'),
                  color: AppTheme.red,
                ),
              ] else if (isFyi) ...[
                if (_batchInFlight)
                  _ChipButton(
                    label: 'Processing…',
                    onTap: null,
                    color: AppTheme.accent,
                  )
                else
                  _ChipButton(
                    label: '✅ Got it',
                    onTap: n == 0 ? null : _batchFyi,
                    color: AppTheme.green,
                  ),
              ] else if (_batchInFlight) ...[
                _ChipButton(
                  label: 'Processing…',
                  onTap: null,
                  color: AppTheme.accent,
                ),
              ] else ...[
                _ChipButton(
                  label: '✅ Approve',
                  onTap: n == 0 ? null : () => _batchSelected('approve'),
                  color: AppTheme.green,
                ),
                const SizedBox(width: 6),
                _ChipButton(
                  label: '❌ Reject',
                  onTap: n == 0 ? null : () => _batchSelected('reject'),
                  color: AppTheme.red,
                ),
              ],
            ],
          ),
        ),
      ),
    );
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
            child: Text('Proceed', style: TextStyle(color: AppTheme.accent)),
          ),
        ],
      ),
    );
    return result ?? false;
  }

  // ── Helpers ──────────────────────────────────────────────────

  void _removeItem(DecisionItem item) {
    setState(() => _items.removeWhere((i) => i.id == item.id));
    // Write-through: an actioned item must not resurrect on the next cold
    // open, so keep the on-device cache in sync with the UI immediately.
    _inboxCache.saveItems(List.of(_items));
    _inboxCache.saveCount(_autoDecisionCount);
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

  /// Offer the undo net after a manual approve/reject. The decision_id comes
  /// back on the channel action responses; graph/merge paths that don't
  /// return one simply get no undo (their flows have their own confirmation).
  void _offerUndo(String verb, ApiResult<dynamic> result) {
    if (!mounted) return;
    final data = result.data is Map ? result.data as Map : const {};
    final decisionId = data['decision_id'];
    if (decisionId is! int) return;
    _showUndoSnack('$verb — undo if this was a mistake', decisionId);
  }

  void _showUndoSnack(String message, int decisionId) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(message, style: const TextStyle(fontSize: 12)),
        duration: const Duration(seconds: 10),
        action: SnackBarAction(
          label: 'Undo',
          onPressed: () => _undoDecision(decisionId),
        ),
      ),
    );
  }

  Future<void> _undoDecision(int decisionId) async {
    final r = await _api.undoDecision(decisionId);
    if (!mounted) return;
    if (r.success) {
      _showSnack('↩️ Undone — item is back in your queue', isError: false);
      _refreshSilently();
    } else {
      _showSnack(r.error ?? 'Undo failed', isError: true);
    }
  }

  // ── Build ──

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      // WhatsApp-style instant content: if this screen was opened from a push
      // tap, render the pushed summary NOW above the spinner instead of a bare
      // loader — the real list replaces it when the fetch lands.
      if (_pushContent.isNotEmpty) {
        return Scaffold(
          appBar: AppBar(title: Text(PersonaStore.current.inbox)),
          body: ListView(
            padding: const EdgeInsets.fromLTRB(0, 8, 0, 40),
            children: [
              PushBanner(
                title: PersonaStore.current.inbox,
                content: _pushContent,
              ),
              const SizedBox(height: 48),
              const Center(child: CircularProgressIndicator()),
            ],
          ),
        );
      }
      return Scaffold(
        appBar: AppBar(title: Text(PersonaStore.current.inbox)),
        body: const Center(child: CircularProgressIndicator()),
      );
    }

    // Apply the active filter first — every section below derives from the
    // filtered pool so "All" shows everything and a single-type filter zooms
    // the decision cards to that bucket. Drafts / FYI filters show no cards;
    // those sections filter their own lists below.
    final filterType = _filterType(_filter);
    final visible = filterType == null
        ? (_filter == InboxFilter.all ? _items : <DecisionItem>[])
        : _items.where((d) => d.type == filterType).toList();
    final merges = visible.where((d) => d.isMergeProposal).toList();
    final nonMerges = visible.where((d) => !d.isMergeProposal).toList();
    final highPriority = nonMerges
        .where((d) => d.priority == DecisionPriority.high)
        .toList();
    final standardPriority = nonMerges
        .where((d) => d.priority == DecisionPriority.standard)
        .toList();
    final lowPriority = nonMerges
        .where((d) => d.priority == DecisionPriority.low)
        .toList();

    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            Text(PersonaStore.current.inbox),
            const Spacer(),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
              decoration: BoxDecoration(
                color: AppTheme.accentBg,
                borderRadius: BorderRadius.circular(AppTheme.controlRadius),
              ),
              child: Text(
                _filter == InboxFilter.all
                    ? '${_items.length} pending'
                    : _filter == InboxFilter.drafts
                        ? '${_drafts.length} drafts'
                        : _filter == InboxFilter.fyi
                            ? '${_fyi.length} FYI'
                            : '${visible.length} of ${_items.length}',
                style: AppTheme.monoLabel.copyWith(color: AppTheme.accent),
              ),
            ),
          ],
        ),
        actions: [
          // Batch-selection toggle — shown whenever the ACTIVE section has
          // something to select: decision cards, drafts (Drafts filter), or
          // FYI items (FYI filter). Hidden when the filter is empty.
          if (_filter == InboxFilter.drafts
              ? _drafts.isNotEmpty
              : _filter == InboxFilter.fyi
                  ? _fyi.isNotEmpty
                  : _items.any(_isSelectable))
            TextButton(
              onPressed: _toggleSelectionMode,
              child: Text(
                _selectionMode ? 'Done' : 'Select',
                style: TextStyle(
                  color: _selectionMode
                      ? AppTheme.accent
                      : AppTheme.textTertiary,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          const SizedBox(width: 4),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _loadDecisions,
        child: ListView(
          // Extra bottom padding in selection mode so the last card clears
          // the selection action bar.
          padding: EdgeInsets.fromLTRB(0, 8, 0, _selectionMode ? 140 : 80),
          children: [
            // Instant content from the notification that opened this screen
            if (_pushContent.isNotEmpty)
              PushBanner(
                title: 'From your notification',
                content: _pushContent,
              ),

            // Error banner
            if (_error.isNotEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 8,
                ),
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

            // ── Type filter chips ──
            _buildFilterChips(),

            // ── Email Drafts (generated reply drafts awaiting approval) ──
            // Shown on All, or when the Drafts filter is active.
            if (_drafts.isNotEmpty &&
                (_filter == InboxFilter.all ||
                    _filter == InboxFilter.drafts)) ...[
              _SectionLabel('Email Drafts'),
              ..._drafts.map(
                (d) {
                  final draftId = d['id'] as int?;
                  return _DraftCard(
                    draft: d,
                    selectionMode: _selectionMode &&
                        _filter == InboxFilter.drafts,
                    selected: _selectedDraftIds.contains(draftId),
                    onToggleSelect: draftId == null
                        ? null
                        : () => _toggleSelectDraft(draftId),
                    onSend: () => _sendDraft(d),
                    onDrop: () => _dropDraft(d),
                    onEdit: () => _editDraft(d),
                  );
                },
              ),
              const SizedBox(height: 16),
            ],

            // ── FYI (informational, no decision needed) ──
            // Shown on All, or when the FYI filter is active.
            if (_fyi.isNotEmpty &&
                (_filter == InboxFilter.all || _filter == InboxFilter.fyi)) ...[
              _SectionLabel('For your info'),
              ..._fyi.map(
                (f) {
                  final fyiId = f['id'] as int?;
                  return _FyiCard(
                    item: f,
                    selectionMode: _selectionMode &&
                        _filter == InboxFilter.fyi,
                    selected: _selectedFyiIds.contains(fyiId),
                    onToggleSelect: fyiId == null
                        ? null
                        : () => _toggleSelectFyi(fyiId),
                    onAck: () => _acknowledgeFyi(f),
                  );
                },
              ),
              const SizedBox(height: 16),
            ],

            // ── Merge Proposals ──
            if (merges.isNotEmpty) ...[
              _SectionLabel('Merge Proposals'),
              ...merges.map(
                (d) => DecisionCard(
                  item: d,
                  onApprove: () => _handleApprove(d),
                  onReject: () => _handleReject(d),
                  selectionMode: _selectionMode && _isSelectable(d),
                  selected: _selectedIds.contains(d.id),
                  onToggleSelect: () => _toggleSelect(d.id),
                ),
              ),
              if (nonMerges.isNotEmpty) const SizedBox(height: 16),
            ],

            // ── Standard items ──
            if (highPriority.isNotEmpty) _SectionLabel('High Priority'),
            ...highPriority.map(
              (d) => DecisionCard(
                item: d,
                onApprove: () => _handleApprove(d),
                onReject: () => _handleReject(d),
                onEdit: d.type == DecisionType.edge
                    ? () => _showEdgeEditSheet(d)
                    : null,
                onMerge: d.type == DecisionType.person
                    ? () => _showMergeSheet(d)
                    : null,
                onReply: d.canReply ? () => _showBeeperReplySheet(d) : null,
                selectionMode: _selectionMode && _isSelectable(d),
                selected: _selectedIds.contains(d.id),
                onToggleSelect: () => _toggleSelect(d.id),
              ),
            ),
            if (highPriority.isNotEmpty && standardPriority.isNotEmpty)
              const SizedBox(height: 16),

            if (standardPriority.isNotEmpty) _SectionLabel('Standard'),
            ...standardPriority.map(
              (d) => DecisionCard(
                item: d,
                onApprove: () => _handleApprove(d),
                onReject: () => _handleReject(d),
                onEdit: d.type == DecisionType.edge
                    ? () => _showEdgeEditSheet(d)
                    : null,
                onMerge: d.type == DecisionType.person
                    ? () => _showMergeSheet(d)
                    : null,
                onReply: d.canReply ? () => _showBeeperReplySheet(d) : null,
                selectionMode: _selectionMode && _isSelectable(d),
                selected: _selectedIds.contains(d.id),
                onToggleSelect: () => _toggleSelect(d.id),
              ),
            ),
            if (standardPriority.isNotEmpty && lowPriority.isNotEmpty)
              const SizedBox(height: 16),

            if (lowPriority.isNotEmpty) _SectionLabel('Low Effort'),
            ...lowPriority.map(
              (d) => DecisionCard(
                item: d,
                onApprove: () => _handleApprove(d),
                onReject: () => _handleReject(d),
                onEdit: d.type == DecisionType.edge
                    ? () => _showEdgeEditSheet(d)
                    : null,
                onMerge: d.type == DecisionType.person
                    ? () => _showMergeSheet(d)
                    : null,
                onReply: d.canReply ? () => _showBeeperReplySheet(d) : null,
                selectionMode: _selectionMode && _isSelectable(d),
                selected: _selectedIds.contains(d.id),
                onToggleSelect: () => _toggleSelect(d.id),
              ),
            ),

            if (_items.isEmpty &&
                _drafts.isEmpty &&
                _fyi.isEmpty &&
                _filter == InboxFilter.all) ...[
              const SizedBox(height: 40),
              const Center(
                child: Text(
                  'No pending decisions',
                  style: TextStyle(color: AppTheme.textTertiary, fontSize: 13),
                ),
              ),
            ] else if (visible.isEmpty &&
                (_filter != InboxFilter.drafts || _drafts.isEmpty) &&
                (_filter != InboxFilter.fyi || _fyi.isEmpty) &&
                !(_filter == InboxFilter.all &&
                    (_drafts.isNotEmpty || _fyi.isNotEmpty))) ...[
              const SizedBox(height: 40),
              Center(
                child: Text(
                  'No ${_filterLabel(_filter)} in the queue',
                  style: const TextStyle(
                    color: AppTheme.textTertiary,
                    fontSize: 13,
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
      // Batch-selection action bar — swaps in while selection mode is on.
      bottomNavigationBar: _selectionMode ? _buildSelectionBar() : null,
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
        borderRadius: BorderRadius.circular(AppTheme.cardRadius),
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
                style: AppTheme.monoCaption.copyWith(
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

// ── Chip Button Widget ──────────────────────────────────────────

class _ChipButton extends StatelessWidget {
  final String label;

  /// Null disables the chip (dimmed, no tap) — used by the selection bar's
  /// Approve/Reject buttons when nothing is selected.
  final VoidCallback? onTap;
  final Color color;

  const _ChipButton({
    required this.label,
    required this.onTap,
    required this.color,
  });

  @override
  Widget build(BuildContext context) {
    final dimmed = onTap == null;
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
              color: dimmed ? color.withValues(alpha: 0.35) : color,
              fontSize: 11,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ),
    );
  }
}

// ── Action Button (local copy — decision_card's is file-private) ──

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
            color: color.withValues(alpha: 0.1),
            borderRadius: BorderRadius.circular(8),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 14, color: color),
              const SizedBox(width: 4),
              Text(
                label,
                style: TextStyle(
                  color: color,
                  fontSize: 11,
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

// ── Selection Checkbox ───────────────────────────────────────────

class _SelectionCheck extends StatelessWidget {
  final bool selected;
  const _SelectionCheck({required this.selected});

  @override
  Widget build(BuildContext context) {
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
          color: AppTheme.textTertiary,
          fontSize: 11,
          letterSpacing: 0.8,
        ),
      ),
    );
  }
}

// ── Email Draft Card (generated reply draft awaiting approval) ────

class _DraftCard extends StatelessWidget {
  final Map<String, dynamic> draft;
  final VoidCallback onSend;
  final VoidCallback onDrop;
  final VoidCallback? onEdit;

  /// Batch-selection mode (Drafts filter): checkbox + tap-to-toggle, and the
  /// action buttons give way to a selection hint.
  final bool selectionMode;
  final bool selected;
  final VoidCallback? onToggleSelect;

  const _DraftCard({
    required this.draft,
    required this.onSend,
    required this.onDrop,
    this.onEdit,
    this.selectionMode = false,
    this.selected = false,
    this.onToggleSelect,
  });

  @override
  Widget build(BuildContext context) {
    final sender = draft['sender_name'] as String?;
    final subject = draft['subject'] as String? ?? 'Email draft';
    final body = (draft['draft_body'] as String? ?? '').trim();
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
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 12, 14, 6),
              child: Row(
                children: [
                  if (selectionMode) ...[
                    _SelectionCheck(selected: selected),
                    const SizedBox(width: 8),
                  ],
                  const Text('📧', style: TextStyle(fontSize: 14)),
                  const SizedBox(width: 6),
                  Text('DRAFT REPLY', style: AppTheme.monoLabel),
                  const Spacer(),
                  if (sender != null)
                    Flexible(
                      child: Text(
                        sender,
                        style: AppTheme.caption.copyWith(
                          color: AppTheme.textTertiary,
                        ),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                ],
              ),
            ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14),
            child: Text(
              subject,
              style: AppTheme.title.copyWith(fontSize: 14),
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
            ),
          ),
          if (body.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 4, 14, 0),
              child: Text(
                body,
                style: AppTheme.bodySmall.copyWith(fontSize: 12),
                maxLines: 4,
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
                    label: 'Send',
                    icon: Icons.send,
                    color: AppTheme.green,
                    onTap: onSend,
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
                    label: 'Drop',
                    icon: Icons.delete_outline,
                    color: AppTheme.textTertiary,
                    onTap: onDrop,
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

// ── FYI Card (informational — acknowledge to dismiss) ─────────────

class _FyiCard extends StatelessWidget {
  final Map<String, dynamic> item;
  final VoidCallback onAck;

  /// Batch-selection mode (FYI filter): checkbox + tap-to-toggle, and the
  /// Got-it button gives way to a selection hint.
  final bool selectionMode;
  final bool selected;
  final VoidCallback? onToggleSelect;

  const _FyiCard({
    required this.item,
    required this.onAck,
    this.selectionMode = false,
    this.selected = false,
    this.onToggleSelect,
  });

  @override
  Widget build(BuildContext context) {
    final channel = item['channel'] as String? ?? 'email';
    final title = item['title'] as String? ?? 'Untitled';
    final summary = (item['summary'] as String? ?? '').trim();
    final sender = item['sender_name'] as String?;
    final channelIcon = switch (channel) {
      'whatsapp' => '💬',
      'call' => '📞',
      'teams' => '🟣',
      _ => '📧',
    };
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
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 12, 14, 6),
              child: Row(
                children: [
                  if (selectionMode) ...[
                    _SelectionCheck(selected: selected),
                    const SizedBox(width: 8),
                  ],
                  Text(channelIcon, style: const TextStyle(fontSize: 14)),
                  const SizedBox(width: 6),
                  Text(
                    'FYI · ${channel.toUpperCase()}',
                    style: AppTheme.monoLabel,
                  ),
                  const Spacer(),
                  if (sender != null)
                    Flexible(
                      child: Text(
                        sender,
                        style: AppTheme.caption.copyWith(
                          color: AppTheme.textTertiary,
                        ),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 14),
              child: Text(
                title,
                style: AppTheme.title.copyWith(fontSize: 14),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              ),
            ),
            if (summary.isNotEmpty)
              Padding(
                padding: const EdgeInsets.fromLTRB(14, 4, 14, 0),
                child: Text(
                  summary,
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
                padding: const EdgeInsets.fromLTRB(8, 6, 8, 8),
                child: Align(
                  alignment: Alignment.centerRight,
                  child: _ActionButton(
                    label: 'Got it',
                    icon: Icons.done,
                    color: AppTheme.accent,
                    onTap: onAck,
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
