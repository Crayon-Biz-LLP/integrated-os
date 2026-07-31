import 'dart:async';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:flutter_tts/flutter_tts.dart';
import 'menu_sheet.dart';
import 'inbox_screen.dart';
import 'today_screen.dart';
import '../models/message.dart';
import '../services/notification_service.dart';
import '../models/briefing.dart';
import '../services/api_service.dart';
import '../services/share_service.dart';
import '../theme/app_theme.dart';
import '../utils/route_observer.dart';
import '../services/widget_data_provider.dart';
import '../widgets/chat_bubble.dart';
import '../widgets/voice_states.dart';
import '../widgets/rich_card_content.dart';
import '../utils/home_instrumentation.dart';

// ── Types ──────────────────────────────────────────────────────

/// A focal item — the top-priority thing Rhodey surfaces.
class _FocalItem {
  final String id;
  final String title;
  final String? subtitle;
  final String? description;
  final String actionLabel;
  final String? source; // "task", "graph_node", "graph_edge", "email", etc.
  final Map<String, dynamic> metadata;

  const _FocalItem({
    required this.id,
    required this.title,
    this.subtitle,
    this.description,
    this.actionLabel = 'Action',
    this.source,
    this.metadata = const {},
  });
}

// ── AdaptiveHomeScreen ─────────────────────────────────────────

class AdaptiveHomeScreen extends StatefulWidget {
  const AdaptiveHomeScreen({super.key});

  @override
  State<AdaptiveHomeScreen> createState() => _AdaptiveHomeScreenState();
}

class _AdaptiveHomeScreenState extends State<AdaptiveHomeScreen>
    with RouteAware, WidgetsBindingObserver {
  final _api = ApiService();
  final _instrumentation = HomeInstrumentation();

  /// Set true after the first post-frame callback — prevents the lifecycle's
  /// initial `resumed` event (fires at app launch) from duplicating the
  /// initState load. Only genuine background→foreground transitions refresh.
  bool _hasStarted = false;

  // ── Pulse intelligence ──
  BriefingResponse _briefing = BriefingResponse.empty();
  bool _briefingLoading = true;

  // ── Focal zone (Phase 2 v2: LLM-chosen top item + three-button model) ──
  List<_FocalItem> _focalItems = [];
  bool _focalLoading = true;
  /// ID of the focal item currently animating to "done" state
  String? _completingCardId;

  /// Live count of pending Inbox decisions — sourced from getPendingDecisions()
  /// (the same data the Inbox screen reads), refreshed whenever focal items
  /// reload. The decision digest uses this instead of the stale briefing
  /// snapshot so the count tracks actions in real time (fixes the count
  /// staying non-zero after the Inbox is emptied).
  int _pendingDecisionCount = 0;

  // ── CONVERSATION ──
  final _messages = <ChatMessage>[];
  final _scrollController = ScrollController();
  final _textController = TextEditingController();
  int _msgCounter = 0;
  Timer? _pollTimer;
  final _seenMessageIds = <String>{};

  // Push-driven delivery: replaces aggressive 5s polling.
  static const _backupPollInterval = Duration(seconds: 60);
  bool _awaitingResponse = false;

  /// Timeout guard for slow send-message calls.
  Timer? _sendTimeout;
  /// Guards against duplicate history loads.
  bool _historyLoaded = false;
  /// Guards against double-tap on the task icon while the ledger is posting.
  bool _taskTapping = false;

  // ── Two-state layout (idle ⇄ conversation) ──
  bool _conversationOpen = false;
  /// Active (todo) task count — shown on the task icon badge.
  int _activeTaskCount = 0;
  /// Debounce timer for push notifications — coalesces rapid pushes.
  Timer? _pushDebounce;

  // ── Voice ──
  VoiceState _voiceState = VoiceState.idle;
  String? _transcribedText;
  String? _voiceError;
  final stt.SpeechToText _speech = stt.SpeechToText();
  bool _speechAvailable = false;
  final FlutterTts _tts = FlutterTts();

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // Subscribe so didPopNext fires when any pushed screen (Inbox via menu /
    // home widget, Today, etc.) pops and we regain focus — the live pending
    // count must reflect decisions made there, from ANY push path.
    final route = ModalRoute.of(context);
    if (route != null) routeObserver.subscribe(this, route);
  }

  /// Regained focus after a pushed screen popped. Refresh live data so the
  /// decision digest and focal board reflect decisions made there.
  @override
  void didPopNext() {
    _loadFocalItems();
  }

  /// App returned to foreground. FCM `onMessage` only fires in the foreground,
  /// so a pulse briefing that ran while the app was backgrounded would leave
  /// home stale (yesterday's voice line, old focal board) until pull-to-refresh.
  /// Refresh the pulse intelligence + focal board on every genuine resume.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && _hasStarted) {
      _refreshHome();
    }
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _hasStarted = true;
    });
    _loadBriefing();
    _loadFocalItems();
    _initSpeech();
    _tts.setLanguage("en-US");
    _tts.setSpeechRate(0.5);

    NotificationService.onNotificationOpened = _handlePushNotificationTap;
    NotificationService.onPushReceived = _handlePushReceived;
    ShareService.onShared = _handleSharedText;

    final pendingData = NotificationService.pendingOpenData;
    if (pendingData != null) {
      NotificationService.pendingOpenData = null;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _handlePushNotificationTap(pendingData);
      });
    }
    // A link shared into Rhodey at cold start (before this screen mounted).
    final pendingShare = ShareService.pendingSharedText;
    if (pendingShare != null) {
      ShareService.pendingSharedText = null;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _handleSharedText(pendingShare);
      });
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    routeObserver.unsubscribe(this);
    if (NotificationService.onNotificationOpened == _handlePushNotificationTap) {
      NotificationService.onNotificationOpened = null;
    }
    if (NotificationService.onPushReceived == _handlePushReceived) {
      NotificationService.onPushReceived = null;
    }
    if (ShareService.onShared == _handleSharedText) {
      ShareService.onShared = null;
    }
    _pollTimer?.cancel();
    _sendTimeout?.cancel();
    _pushDebounce?.cancel();
    _textController.dispose();
    _scrollController.dispose();
    _instrumentation.log();
    super.dispose();
  }

  // ── Pulse intelligence ───────────────────────────────────────────

  Future<void> _loadBriefing() async {
    try {
      final briefing = await _api.getBriefing();
      if (mounted) {
        setState(() {
          _briefing = briefing;
          _briefingLoading = false;

          // If briefing has an LLM-chosen top_focal_item, promote it as #1
          final focal = briefing.topFocalItem;
          if (focal != null && focal.isNotEmpty && focal['title'] != null) {
            final existingIdx = _focalItems.indexWhere(
              (f) => f.id == 'focal_${focal['item_id']}',
            );
            final itemType = focal['type'] as String? ?? 'task';
            final llmLabel = focal['action_label'] as String?;
            final newItem = _FocalItem(
              id: 'focal_${focal['item_id'] ?? focal['title']}',
              title: focal['title'] as String? ?? '',
              description: focal['reason'] as String?,
              actionLabel: _actionLabelForType(itemType, llmLabel),
              source: itemType,
              metadata: {
                'focal_item_id': focal['item_id'],
                'focal_type': focal['type'],
                'reason': focal['reason'],
                'urgency': focal['urgency'],
                'from_briefing': true,
              },
            );
            if (existingIdx >= 0) {
              _focalItems[existingIdx] = newItem;
            } else {
              _focalItems.insert(0, newItem);
            }
            // Remove any duplicate from _loadFocalItems that has the same natural ID
            final naturalId = focal['item_id']?.toString() ?? '';
            if (naturalId.isNotEmpty) {
              _focalItems.removeWhere((f) =>
                f.id == naturalId && f.id != newItem.id
              );
            }
          }
        });
        WidgetDataProvider().updatePulseWidget(briefing);
      }
    } catch (_) {
      if (mounted) {
        setState(() => _briefingLoading = false);
      }
    }
  }

  // ── Focal items ─────────────────────────────────────────────────

  Future<void> _loadFocalItems() async {
    try {
      final decResult = await _api.getPendingDecisions();
      final taskResult = await _api.getTasks(status: 'todo');
      if (!mounted) return;

      final items = <_FocalItem>[];

      // Decisions first (graph nodes, edges, channel items)
      if (decResult.success && decResult.data != null) {
        // Live pending count for the decision digest — same source the Inbox
        // screen reads, so the digest drops as items are actioned.
        _pendingDecisionCount = decResult.data!.length;
        for (final pd in decResult.data!) {
          final source = pd.source;
          items.add(_FocalItem(
            id: pd.id,
            title: pd.title,
            description: pd.description,
            actionLabel: _sourceActionLabel(source),
            source: source,
            metadata: {
              'source': source,
              'api_id': pd.id,
              'pending_id': int.tryParse(pd.id),
            },
          ));
        }
      }

      // Due/overdue tasks fill remaining slots
      if (taskResult.success && taskResult.data != null) {
        _activeTaskCount = taskResult.data!.length;
        for (final t in taskResult.data!) {
          final deadline = t['deadline'] as String?;
          if (deadline == null || !_isUrgent(deadline)) continue;
          items.add(_FocalItem(
            id: t['id'].toString(),
            title: t['title'] as String? ?? 'Untitled',
            subtitle: _formatTaskContext(t),
            source: 'task',
            actionLabel: 'Done',
            metadata: {'task_id': t['id']},
          ));
        }
      }

      if (mounted) {
        setState(() {
          // Preserve items that were deliberately surfaced: the LLM-chosen
          // briefing item AND any task the user manually promoted to focus.
          // Everything else rebuilds from live pending/task data.
          final preserved = <_FocalItem>[];
          for (final f in _focalItems) {
            if (f.metadata['from_briefing'] == true ||
                f.metadata['promoted'] == true) {
              preserved.add(f);
            }
          }
          _focalItems = items;
          for (final p in preserved.reversed) {
            _focalItems.removeWhere((f) => f.id == p.id);
            _focalItems.insert(0, p);
          }
          _focalLoading = false;
          _instrumentation.nowCardsShown = _focalItems.length;
        });
      }
    } catch (_) {
      // Network or parse error — show empty state
    } finally {
      if (mounted) {
        setState(() => _focalLoading = false);
      }
    }
  }

  bool _isUrgent(String deadline) {
    try {
      final dt = DateTime.parse(deadline).toLocal();
      return dt.isBefore(DateTime.now().add(const Duration(hours: 24)));
    } catch (_) {
      return false;
    }
  }

  String _formatDeadline(String dt) {
    try {
      final parsed = DateTime.parse(dt).toLocal();
      final diff = parsed.difference(DateTime.now());
      if (diff.inDays < 0) return '${diff.inDays.abs()}d overdue';
      if (diff.inDays == 0) return 'Today';
      return '${diff.inDays}d left';
    } catch (_) {
      return '';
    }
  }

  /// The chief-of-staff context line for a promoted task: when it was added,
  /// which org it belongs to, its deadline status, and a snippet of the
  /// original message note — so the focal card reads like a person handing
  /// over a file, not a bare title.
  String _formatTaskContext(Map<String, dynamic> task) {
    final parts = <String>[];
    final created = task['created_at'] as String?;
    if (created != null && created.isNotEmpty) {
      parts.add('Added ${_formatCreated(created)}');
    }
    final org = task['organization_name'] as String?;
    if (org != null && org.trim().isNotEmpty) parts.add(org.trim());
    final deadline = task['deadline'] as String?;
    if (deadline != null && deadline.isNotEmpty) {
      final dl = _formatDeadline(deadline);
      if (dl.isNotEmpty) parts.add(dl);
    }
    final notes = task['notes'] as String?;
    if (notes != null && notes.trim().isNotEmpty) {
      // De-dup: the note is the original message and the title is usually
      // extracted FROM it — only suppress near-identical echoes (e.g. title
      // 'Fill DBS forms' + note 'Fill DBS forms'), never notes that carry
      // real extra context ('Fill DBS forms, then call Timmy...').
      final title = (task['title'] as String? ?? '').trim().toLowerCase();
      final noteClean = notes.trim().toLowerCase();
      final isEcho = title.isNotEmpty &&
          (noteClean == title ||
              (noteClean.contains(title) &&
                  (noteClean.length - title.length) < 10));
      if (!isEcho) {
        final snippet = _shortenNotes(notes);
        if (snippet.isNotEmpty) parts.add(snippet);
      }
    }
    return parts.join(' · ');
  }

  /// Clip a task note to a single ~80-char line for the focal card.
  String _shortenNotes(String notes) {
    final clean = notes.replaceAll(RegExp(r'\s+'), ' ').trim();
    if (clean.length <= 80) return clean;
    return '${clean.substring(0, 77)}…';
  }

  String _formatCreated(String created) {
    try {
      final parsed = DateTime.parse(created).toLocal();
      final diff = DateTime.now().difference(parsed);
      if (diff.inDays < 1) return 'today';
      if (diff.inDays == 1) return 'yesterday';
      if (diff.inDays < 7) return '${diff.inDays}d ago';
      return '${diff.inDays ~/ 7}w ago';
    } catch (_) {
      return '';
    }
  }

  String _sourceActionLabel(String source) {
    switch (source) {
      case 'graph_node': return 'Approve';
      case 'merge': return 'Approve';
      case 'graph_edge': return 'Review';
      case 'email': return 'Create';
      case 'whatsapp': return 'Create';
      case 'call': return 'Create';
      default: return 'View';
    }
  }

  /// True for graph items (node/edge/merge) where the third button should be
  /// a permanent "Reject" — mirroring the Inbox. Tasks keep "Not right".
  bool _isGraphType(_FocalItem item) {
    final t = (item.metadata['focal_type'] as String?) ?? item.source ?? '';
    return t == 'graph_node' || t == 'graph_edge' || t == 'merge';
  }

  /// Derive the first-button label from the item's type field.
  /// This is the authoritative source — overrides the LLM's `action_label`
  /// to ensure the button text always makes sense for the item type.
  String _actionLabelForType(String type, [String? llmFallback]) {
    switch (type) {
      case 'task':
        return llmFallback ?? "I'll do it";
      case 'graph_node':
        return 'Approve';
      case 'merge':
        return 'Approve';
      case 'graph_edge':
        return 'Review';
      case 'email':
        return 'Create';
      case 'whatsapp':
        return 'Create';
      case 'call':
        return 'Create';
      default:
        return llmFallback ?? "I'll do it";
    }
  }

  // ── Focal card action + promotion ─────────────────────────

  /// Complete the current focal card via /api/focal-action, then promote next.
  Future<void> _completeFocalItem(_FocalItem item) async {
    // 1. Start done animation (green flash, 300ms)
    setState(() => _completingCardId = item.id);
    await Future.delayed(const Duration(milliseconds: 300));
    if (!mounted) return;

    // 2. Execute via /api/focal-action (Phase 2 v2 unified endpoint)
    ApiResult<dynamic>? result;
    final fromBriefing = item.metadata['from_briefing'] == true;
    if (fromBriefing && item.metadata['focal_item_id'] != null) {
      // LLM-chosen item from briefing — use unified endpoint
      result = await _api.focalAction(
        action: 'done',
        itemType: item.metadata['focal_type'] as String? ?? item.source ?? 'task',
        itemId: item.metadata['focal_item_id'].toString(),
        title: item.title,
      );
    } else {
      // Legacy fallback: direct API calls
      if (item.source == 'task') {
        final taskId = item.metadata['task_id'];
        if (taskId != null) {
          result = await _api.updateTaskStatus(taskId as int, 'done');
        }
      } else {
        final source = item.metadata['source'] as String?;
        final pendingId = item.metadata['pending_id'] as int?;
        if (source != null && pendingId != null) {
          switch (source) {
            case 'graph_node':
              result = await _api.approveGraphNode(pendingId);
              break;
            case 'merge':
              result = await _api.acceptMerge(pendingId);
              break;
            case 'graph_edge':
              result = await _api.approveGraphEdge(pendingId);
              break;
            case 'email':
              result = await _api.approveEmail(pendingId);
              break;
            case 'whatsapp':
              result = await _api.approveWhatsApp(pendingId);
              break;
            case 'call':
              result = await _api.approveCall(pendingId);
              break;
          }
        }
      }
    }

    // 2b. Honest completion — the backend must confirm before the card is
    // removed. A failed 'done' (HTTP 200 + {"success": false}), or an item
    // with no server-side id to act on (null result), keeps the card on the
    // board and tells Danny — never a silent false confirmation.
    if (result == null || !_focalActionOk(result)) {
      if (mounted) {
        setState(() => _completingCardId = null);
        final data = result?.data;
        final serverMsg =
            (data is Map && data['message'] is String) ? data['message'] as String : null;
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
                result == null
                    ? "I couldn't complete that item — it may need a refresh."
                    : (serverMsg ?? result.error ?? "Couldn't complete that — it's still pending."),
                style: const TextStyle(fontSize: 12)),
            // Amber for the unactionable case, red for a real failed action.
            backgroundColor: result == null ? AppTheme.amber : AppTheme.red,
            duration: const Duration(seconds: 2),
          ),
        );
      }
      return;
    }
    _instrumentation.homeActionsCompleted++;
    if (!mounted) return;

    // 3. Remove completed item — the next item promotes immediately
    setState(() {
      _focalItems.removeWhere((c) => c.id == item.id);
      _completingCardId = null;
    });

    // 4. If no items remain, show "all clear" from Rhodey
    if (_focalItems.isEmpty && mounted) {
      _addRhodeyResponse(
        "All clear for now, Danny. You have some deep work time before your next sync — I'll keep tracking the board.",
      );
    }

    // 5. Background refresh for fresh counts (deferred 2s to avoid flicker)
    Future.delayed(const Duration(seconds: 2), () {
      if (mounted) _loadFocalItems();
    });
  }

  /// Resolve the (itemType, itemId) pair for ANY focal item so the unified
  /// /api/focal-action endpoint can persist the decision server-side.
  /// Works for briefing items, legacy pending-node/edge/merge items, and tasks.
  (String, String)? _focalActionArgs(_FocalItem item) {
    final m = item.metadata;
    final focalType = m['focal_type'] as String?;
    final focalId = m['focal_item_id'];
    if (focalType != null && focalId != null) {
      return (focalType, focalId.toString());
    }
    final pendingId = m['pending_id'];
    if (pendingId != null) {
      return (item.source ?? 'graph_node', pendingId.toString());
    }
    final taskId = m['task_id'];
    if (taskId != null) {
      return ('task', taskId.toString());
    }
    return null;
  }

  /// Skip/dismiss the current focal item — persists a real deferral on the
  /// server (snoozed_until) for ALL item types, so "Not now" never silently
  /// resurrects on the next load. No learning signal (defer ≠ reject).
  Future<void> _snoozeFocalItem(_FocalItem item) async {
    _instrumentation.itemsDismissed++;

    final args = _focalActionArgs(item);
    if (args == null) {
      // No server-side id to defer — don't pretend the card is dismissed.
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: const Text("I couldn't defer that item — it will resurface.",
                style: TextStyle(fontSize: 12)),
            backgroundColor: AppTheme.amber,
            duration: const Duration(seconds: 2),
          ),
        );
      }
      return;
    }

    final result = await _api.focalAction(
      action: 'snooze',
      itemType: args.$1,
      itemId: args.$2,
      title: item.title,
    );
    final persisted = _focalActionOk(result);

    if (!persisted) {
      // Deferral wasn't persisted server-side — don't pretend it was.
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: const Text("Couldn't defer that item — it will resurface. Please try again.",
                style: TextStyle(fontSize: 12)),
            backgroundColor: AppTheme.red,
            duration: const Duration(seconds: 2),
          ),
        );
      }
      return;
    }

    setState(() {
      _focalItems.removeWhere((c) => c.id == item.id);
    });
    if (_focalItems.isEmpty && mounted) {
      _addRhodeyResponse(
        "Noted. I'll keep it off your board for a while — it'll resurface if it's still relevant.",
      );
    }
    // Refresh the live pending count so the digest drops with the deferral.
    Future.delayed(const Duration(seconds: 2), () {
      if (mounted) _loadFocalItems();
    });
  }

  /// True when the /api/focal-action response actually reports success.
  /// The backend returns HTTP 200 with {"success": false} on business
  /// failures (deferral failed, merge proposal not found), and ApiService.post
  /// treats any 200 as ApiResult.success — so the body flag is the only
  /// trustworthy signal. Without this, a failed reject/snooze/correct would
  /// silently remove the card.
  bool _focalActionOk(ApiResult<dynamic> result) {
    if (!result.success) return false;
    final data = result.data;
    if (data is Map) return data['success'] != false;
    return true;
  }

  /// "Reject" — permanent rejection for graph nodes/edges/merges, mirroring
  /// the Inbox's Reject. Routes through the unified /api/focal-action
  /// (reject) so the learning loop records the decision server-side.
  /// Tasks have no reject semantics — they keep "Not right".
  Future<void> _rejectFocalItem(_FocalItem item) async {
    final args = _focalActionArgs(item);
    if (args == null) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text("I couldn't reject that item — it may resurface.",
                style: TextStyle(fontSize: 12)),
            backgroundColor: AppTheme.red,
            duration: Duration(seconds: 2),
          ),
        );
      }
      return;
    }

    final result = await _api.focalAction(
      action: 'reject',
      itemType: args.$1,
      itemId: args.$2,
      title: item.title,
    );
    if (!_focalActionOk(result) && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(result.error ?? "Couldn't reject that item — it may resurface.",
              style: const TextStyle(fontSize: 12)),
          backgroundColor: AppTheme.red,
          duration: const Duration(seconds: 2),
        ),
      );
      return;
    }
    if (!mounted) return;

    setState(() {
      _focalItems.removeWhere((c) => c.id == item.id);
    });
    if (_focalItems.isEmpty && mounted) {
      _addRhodeyResponse(
        "Rejected — I'll leave that one off the board and adjust my judgment.",
      );
    }
    // Refresh the live pending count so the digest drops with the rejection.
    Future.delayed(const Duration(seconds: 2), () {
      if (mounted) _loadFocalItems();
    });
  }

  /// "Not right" — persists the same deferral AND sends a correction signal
  /// to the learning loop, for ALL focal item types.
  Future<void> _correctFocalItem(_FocalItem item) async {
    final args = _focalActionArgs(item);
    if (args == null) {
      // No server-side id to defer — don't pretend the card is dismissed.
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: const Text("I couldn't defer that item — it may resurface.",
                style: TextStyle(fontSize: 12)),
            backgroundColor: AppTheme.amber,
            duration: const Duration(seconds: 2),
          ),
        );
      }
      return;
    }

    final result = await _api.focalAction(
      action: 'correct',
      itemType: args.$1,
      itemId: args.$2,
      title: item.title,
      reason: item.metadata['reason'] as String? ?? '',
    );
    final persisted = _focalActionOk(result);

    if (!persisted) {
      // Correction may still have been recorded, but the deferral failed —
      // don't pretend the card is gone if it will resurface.
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: const Text("I noted that, but couldn't defer the item — it may resurface.",
                style: TextStyle(fontSize: 12)),
            backgroundColor: AppTheme.amber,
            duration: const Duration(seconds: 2),
          ),
        );
      }
      return;
    }

    setState(() {
      _focalItems.removeWhere((c) => c.id == item.id);
    });
    if (_focalItems.isEmpty && mounted) {
      _addRhodeyResponse(
        "Got it — I've noted that wasn't the right priority. I'll adjust.",
      );
    }
    // Refresh the live pending count so the digest drops with the correction.
    Future.delayed(const Duration(seconds: 2), () {
      if (mounted) _loadFocalItems();
    });
  }



  // ── Conversation ─────────────────────────────────────────────

  Future<void> _loadHistory({bool force = false}) async {
    if (_historyLoaded && !force) return;
    // First-ever load (pill tap / task-icon backfill) should land on the
    // newest message — subsequent force-refreshes keep the user's position.
    final isInitialBackfill = !_historyLoaded;
    _historyLoaded = true;
    // Unified with the History screen: both read /api/conversation-history
    // (the merged user↔Rhodey thread) instead of the raw_dumps noise stream,
    // so home backfill and History tell the same story.
    final result = await _api.getConversationHistory(limit: 30);
    if (!mounted) return;

    if (result.success && result.data!.isNotEmpty) {
      // API returns messages newest-first — reverse to oldest-first
      // so they display in correct chat order (oldest at top).
      for (final m in result.data!.reversed) {
        final id = m['id'].toString();
        if (_seenMessageIds.contains(id)) continue;
        _seenMessageIds.add(id);
        final content = m['content'] as String? ?? '';
        if (content.isEmpty) continue;
        final roleField = m['role'] as String? ?? 'bot';
        final role = roleField == 'user' ? MessageRole.user : MessageRole.rhodey;

        // Mirror the poll's guard: skip server-side copies of a message the
        // user just sent optimistically (its local echo is already on screen).
        if (role == MessageRole.user && _hasLocalEcho(content)) continue;

        final createdAt = m['created_at'] as String? ?? '';
        final ts = createdAt.isNotEmpty
            ? (DateTime.tryParse(createdAt) ?? DateTime.now())
            : DateTime.now();
        _messages.add(ChatMessage(
          id: 'h${_msgCounter++}',
          role: role,
          text: content,
          timestamp: ts,
          intent: m['intent'] as String?,
          sendStatus: role == MessageRole.user ? SendStatus.sent : null,
        ));
      }
      // Sort-safe merge: history may arrive around a ledger echo already on
      // screen — timestamps are the single source of truth for thread order.
      _messages.sort((a, b) => a.timestamp.compareTo(b.timestamp));
      if (mounted) setState(() {});
      // reverse:true pins the viewport to the bottom by construction, so the
      // backfill (older rows above) can't push the ledger out of view. The
      // extra nudge below is harmless belt-and-suspenders for the first load.
      if (isInitialBackfill) _scrollToBottom();
    }
  }

  void _handlePushReceived(Map<String, dynamic> data) {
    debugPrint('[PushDelivery] Push received: type=${data['type']}');
    // Debounce: coalesce rapid pushes (e.g. from vault tap + poll collision)
    // into a single refresh. The last push in a 2-second window wins.
    _pushDebounce?.cancel();
    _pushDebounce = Timer(const Duration(seconds: 2), () {
      if (!mounted) return;
      final type = data['type'];
      if (type == 'briefing' || type == 'briefing_refresh') {
        _loadBriefing();
      }
      _pollForUpdates();
    });
  }

  void _startBackupPoll() {
    _awaitingResponse = true;
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(_backupPollInterval, (_) {
      if (!_awaitingResponse) { _stopBackupPoll(); return; }
      _pollForUpdates();
    });
  }

  void _stopBackupPoll() {
    _awaitingResponse = false;
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  Future<void> _pollForUpdates() async {
    final result = await _api.getConversationHistory(limit: 5);
    if (!mounted || !result.success) return;
    // API returns messages newest-first — reverse to oldest-first
    // so new messages append at the end (bottom of chat).
    for (final m in result.data!.reversed) {
      final id = m['id'].toString();
      if (_seenMessageIds.contains(id)) continue;
      _seenMessageIds.add(id);
      final content = m['content'] as String? ?? '';
      if (content.isEmpty) continue;

      final roleField = m['role'] as String? ?? 'bot';
      final role = roleField == 'user' ? MessageRole.user : MessageRole.rhodey;

      // Guard: skip server-side copies of a message the user just sent
      // optimistically (its local echo is already on screen).
      if (role == MessageRole.user && _hasLocalEcho(content)) continue;

      final createdAt = m['created_at'] as String? ?? '';
      final ts = createdAt.isNotEmpty
          ? (DateTime.tryParse(createdAt) ?? DateTime.now())
          : DateTime.now();
      final msg = ChatMessage(
        id: 'p${_msgCounter++}',
        role: role,
        text: content,
        timestamp: ts,
        intent: m['intent'] as String?,
        sendStatus: role == MessageRole.user ? SendStatus.sent : null,
      );
      setState(() { _messages.add(msg); });
      _scrollToBottom();
    }
  }

  /// True if a locally-added user message with the same text exists within
  /// the last 90 seconds. Prevents the backup poll from duplicating the
  /// optimistic echo that _sendMessage already placed on screen.
  bool _hasLocalEcho(String content) {
    final now = DateTime.now();
    final normalized = content.trim();
    for (final m in _messages) {
      if (m.role == MessageRole.user &&
          m.text.trim() == normalized &&
          now.difference(m.timestamp).inSeconds < 90) {
        return true;
      }
    }
    return false;
  }

  void _sendMessage(String text) {
    if (text.trim().isEmpty) return;
    // Sending a message enters the conversation state (welcome hides).
    if (!_conversationOpen) setState(() => _conversationOpen = true);
    final id = 'u${++_msgCounter}';

    // Start as 'sent' immediately (optimistic) — avoids a pending spinner
    // flicker while the API call is in flight.
    final msg = ChatMessage(
      id: id, role: MessageRole.user, text: text.trim(),
      timestamp: DateTime.now(), sendStatus: SendStatus.sent,
    );

    setState(() { _messages.add(msg); });
    _textController.clear();
    _scrollToBottom();
    _startBackupPoll();

    // Timeout guard: if the API takes > 45s, flag as failed
    _sendTimeout?.cancel();
    _sendTimeout = Timer(const Duration(seconds: 45), () {
      if (!mounted) return;
      _updateMessage(id, sendStatus: SendStatus.failed);
      _stopBackupPoll();
    });

    _api.sendMessage(text.trim()).then((result) {
      _sendTimeout?.cancel();
      if (!mounted) return;
      if (result.success) {
        _updateMessage(id, sendStatus: SendStatus.resolved);
        String responseText;
        if (result.data is Map) {
          responseText = (result.data as Map)['response'] as String? ?? 'Got it. Processing...';
        } else {
          responseText = 'Got it. Processing...';
        }
        Future.delayed(const Duration(milliseconds: 400), () {
          if (!mounted) return;
          _addRhodeyResponse(responseText);
        });
      } else {
        _updateMessage(id, sendStatus: SendStatus.failed);
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: const Text('Failed to send message. Check your connection.',
                  style: TextStyle(fontSize: 12)),
              backgroundColor: AppTheme.red,
              duration: const Duration(seconds: 3),
            ),
          );
        }
      }
    });
  }

  void _updateMessage(String id, {String? text, SendStatus? sendStatus}) {
    setState(() {
      final idx = _messages.indexWhere((m) => m.id == id);
      if (idx == -1) return;
      _messages[idx] = ChatMessage(
        id: _messages[idx].id,
        role: _messages[idx].role,
        type: _messages[idx].type,
        text: text ?? _messages[idx].text,
        timestamp: _messages[idx].timestamp,
        quickReplies: _messages[idx].quickReplies,
        taskList: _messages[idx].taskList,
        intent: _messages[idx].intent,
        sendStatus: sendStatus ?? _messages[idx].sendStatus,
      );
    });
  }

  void _addRhodeyResponse(String text) {
    _stopBackupPoll();
    final id = 'r${++_msgCounter}';
    setState(() {
      _messages.add(ChatMessage(
        id: id, role: MessageRole.rhodey, text: text, timestamp: DateTime.now(),
      ));
    });
    _scrollToBottom();
    _tts.speak(text);
  }

  // ── Task ledger (task icon → conversation) ────────────────

  /// Task icon tap: enter the conversation state and post Rhodey's ledger —
  /// the active task list as a chat message. Tapping a row promotes it.
  Future<void> _handleTaskIconTap() async {
    if (_taskTapping) return;
    _taskTapping = true;

    // Enter conversation state (welcome hides; card pins to top).
    if (!_conversationOpen) setState(() => _conversationOpen = true);

    // Post the ledger FIRST for instant feedback, then bring the full thread
    // in behind it — the sort-safe merge keeps the ledger at the bottom.
    await _postTaskLedger();
    _loadHistory();

    Future.delayed(const Duration(milliseconds: 600), () {
      if (mounted) _taskTapping = false;
    });
  }

  /// Post the task ledger as a Rhodey chat message (type == taskList).
  /// Re-tapping the task icon refreshes the ledger in place — it never stacks
  /// duplicate ledger messages.
  Future<void> _postTaskLedger() async {
    final result = await _api.getTasks(includeSnoozed: true);
    if (!mounted) return;
    if (!result.success || result.data == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text("Couldn't load your board right now — check your connection.",
              style: TextStyle(fontSize: 12)),
          backgroundColor: AppTheme.red,
          duration: Duration(seconds: 2),
        ),
      );
      return;
    }

    final tasks = result.data!;
    final active = tasks.where((t) => !_isSnoozedTask(t)).toList();
    final snoozed = tasks.where((t) => _isSnoozedTask(t)).toList();

    final id = 'tl${++_msgCounter}';
    setState(() {
      // Keep the header badge live right after a ledger tap — we already
      // have fresh data here, no need to wait for the next refresh.
      _activeTaskCount = active.length;
      _messages.removeWhere((m) => m.type == MessageType.taskList);
      _messages.add(ChatMessage(
        id: id,
        role: MessageRole.rhodey,
        type: MessageType.taskList,
        text: 'Here\'s your board right now — ${active.length} active'
            '${snoozed.isNotEmpty ? ', ${snoozed.length} snoozed' : ''}. Tap any task to put it in focus.',
        taskList: tasks,
        timestamp: DateTime.now(),
      ));
    });
    _scrollToBottom();
  }

  /// Tapping a task row in the ledger promotes it to the focus card.
  /// If the task was snoozed, promoting also pulls it forward (clears the
  /// deferral) — the ledger acts, it doesn't just display. The "pulled
  /// forward" confirmation is only shown when the server confirms it, so we
  /// never assert a state that didn't happen.
  Future<void> _promoteToFocus(Map<String, dynamic> task) async {
    final taskId = task['id'];
    if (taskId == null) return;
    final wasSnoozed = _isSnoozedTask(task);
    final item = _FocalItem(
      id: taskId.toString(),
      title: task['title'] as String? ?? 'Untitled',
      subtitle: _formatTaskContext(task),
      source: 'task',
      actionLabel: "I'll do it",
      metadata: {'task_id': taskId, 'promoted': true},
    );
    // Pulling a snoozed task into focus clears its deferral server-side.
    var pulledForward = false;
    if (wasSnoozed && taskId is int) {
      final result = await _api.vaultAction(action: 'pull_forward', taskId: taskId);
      pulledForward = result.success;
    }
    if (!mounted) return;
    setState(() {
      _focalItems.removeWhere((f) => f.id == item.id);
      _focalItems.insert(0, item);
    });
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            wasSnoozed && pulledForward
                ? '"${item.title}" pulled forward — in focus.'
                : (wasSnoozed
                    ? '"${item.title}" is in focus — I couldn\'t clear its deferral.'
                    : '"${item.title}" is now the focus.'),
            style: const TextStyle(fontSize: 12),
          ),
          backgroundColor: wasSnoozed && !pulledForward ? AppTheme.amber : AppTheme.accent,
          duration: const Duration(seconds: 2),
        ),
      );
    }
  }

  /// A task is snoozed only while its deferral is still in the future — an
  /// expired deferral is back in the active queue (matches the backend's
  /// snoozed_until.lt.now semantics).
  bool _isSnoozedTask(Map<String, dynamic> t) {
    final until = t['snoozed_until'];
    if (until == null || until.toString().isEmpty) return false;
    final dt = DateTime.tryParse(until.toString());
    return dt != null && dt.isAfter(DateTime.now());
  }

  /// Render a taskList message (Rhodey's ledger) in the conversation.
  Widget _buildTaskListMessage(ChatMessage msg) {
    final tasks = msg.taskList ?? [];
    if (tasks.isEmpty) return const SizedBox.shrink();

    final active = tasks.where((t) => !_isSnoozedTask(t)).toList();
    final snoozed = tasks.where((t) => _isSnoozedTask(t)).toList();

    int prioRank(Map<String, dynamic> t) {
      switch (t['priority']) {
        case 'urgent':
          return 0;
        case 'high':
          return 1;
        default:
          return 2;
      }
    }
    // Urgent first, then high, then the rest — matches the web UI's feel.
    // Snoozed items also sort by priority for a uniform ledger.
    active.sort((a, b) => prioRank(a).compareTo(prioRank(b)));
    snoozed.sort((a, b) => prioRank(a).compareTo(prioRank(b)));

    Widget row(Map<String, dynamic> t, {bool dim = false}) {
      final title = t['title'] as String? ?? 'Untitled';
      final deadline = t['deadline'] as String?;
      final priority = t['priority'] as String? ?? 'medium';

      Color pColor;
      switch (priority) {
        case 'urgent':
          pColor = AppTheme.red;
          break;
        case 'high':
          pColor = AppTheme.amber;
          break;
        default:
          pColor = AppTheme.textTertiary;
      }

      // Mark the currently-focused task so the ledger visibly responds to a tap.
      final isCurrentFocus = !dim &&
          _focalItems.isNotEmpty &&
          _focalItems.first.id == t['id'].toString();

      return GestureDetector(
        onTap: () => _promoteToFocus(t),
        child: Container(
          margin: const EdgeInsets.only(bottom: 6),
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
          decoration: BoxDecoration(
            color: isCurrentFocus
                ? AppTheme.accent.withValues(alpha: 0.06)
                : (dim ? AppTheme.surfaceAlt.withValues(alpha: 0.5) : AppTheme.surfaceAlt),
            borderRadius: BorderRadius.circular(8),
            border: Border.all(
              color: isCurrentFocus
                  ? AppTheme.accent.withValues(alpha: 0.35)
                  : AppTheme.border,
              width: 1,
            ),
          ),
          child: Row(
            children: [
              Container(
                width: 3,
                height: 26,
                decoration: BoxDecoration(
                  color: dim ? AppTheme.textTertiary : pColor,
                  borderRadius: BorderRadius.circular(1),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: AppTheme.body.copyWith(
                        fontSize: 12.5,
                        fontWeight: FontWeight.w500,
                        color: dim ? AppTheme.textTertiary : AppTheme.textPrimary,
                      ),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    if (deadline != null && deadline.isNotEmpty)
                      Text(
                        dim ? 'Snoozed · ${_formatDeadline(deadline)}' : _formatDeadline(deadline),
                        style: AppTheme.caption.copyWith(
                          color: dim ? AppTheme.textTertiary : AppTheme.textSecondary,
                          fontSize: 9,
                        ),
                      ),
                  ],
                ),
              ),
              if (isCurrentFocus)
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                  decoration: BoxDecoration(
                    color: AppTheme.accent.withValues(alpha: 0.12),
                    borderRadius: BorderRadius.circular(4),
                  ),
                  child: Text(
                    'in focus',
                    style: AppTheme.caption.copyWith(color: AppTheme.accent, fontSize: 8),
                  ),
                )
              else if (dim)
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                  decoration: BoxDecoration(
                    color: AppTheme.textTertiary.withValues(alpha: 0.1),
                    borderRadius: BorderRadius.circular(4),
                  ),
                  child: Text(
                    'snoozed',
                    style: AppTheme.caption.copyWith(color: AppTheme.textTertiary, fontSize: 8),
                  ),
                ),
            ],
          ),
        ),
      );
    }

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 2),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.only(left: 16, bottom: 4),
            child: Text(
              'Rhodey',
              style: AppTheme.caption.copyWith(color: AppTheme.accent, letterSpacing: 0.3),
            ),
          ),
          Padding(
            padding: const EdgeInsets.only(left: 16, bottom: 8),
            child: Text(
              msg.text,
              style: AppTheme.body.copyWith(fontSize: 12.5, height: 1.35),
            ),
          ),
          if (active.isNotEmpty) ...active.map((t) => row(t)),
          if (snoozed.isNotEmpty) ...[const SizedBox(height: 4), ...snoozed.map((t) => row(t, dim: true))],
          const SizedBox(height: 4),
          Padding(
            padding: const EdgeInsets.only(left: 4),
            child: Text(
              'Tap a task to make it the focus.',
              style: AppTheme.caption.copyWith(
                color: AppTheme.textTertiary,
                fontSize: 9,
                fontStyle: FontStyle.italic,
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ── Two-state layout: IDLE ⇄ CONVERSATION ─────────────────

  /// IDLE: Rhodey's welcome + deliberately clean middle + the single focal
  /// card pinned just above the input bar. Zero decision fatigue.
  /// Pull-to-refresh reloads the pulse intelligence + focal board.
  Widget _buildIdleState() {
    return RefreshIndicator(
      onRefresh: _refreshHome,
      color: AppTheme.accent,
      // AlwaysScrollableScrollPhysics keeps the pull gesture alive even when
      // the content is shorter than the viewport (the idle state's norm).
      child: LayoutBuilder(
        builder: (context, constraints) => SingleChildScrollView(
          physics: const AlwaysScrollableScrollPhysics(),
          child: ConstrainedBox(
            constraints: BoxConstraints(minHeight: constraints.maxHeight),
            child: Column(
              key: const ValueKey('idle'),
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                _buildWelcomeBanner(),
                const Spacer(),
                _buildFocalArea(),
              ],
            ),
          ),
        ),
      ),
    );
  }

  /// Home pull-to-refresh: reload the pulse intelligence and the focal board.
  Future<void> _refreshHome() async {
    await Future.wait([_loadBriefing(), _loadFocalItems()]);
  }

  /// CONVERSATION: compact focus bar pinned at top + the chat stream.
  /// The welcome hides; the card becomes pinned context while you talk.
  Widget _buildConversationState() {
    return Column(
      key: const ValueKey('conversation'),
      children: [
        _buildCompactFocalBar(),
        Expanded(child: _buildConversationArea()),
      ],
    );
  }

  /// The idle greeting — Rhodey's voice line, time-prefixed, 1-2 lines.
  /// This is the app-intelligence summary: a greeting with judgment, never
  /// a status dump.
  Widget _buildWelcomeBanner() {
    final voice = _briefing.voiceLine?.trim() ?? '';
    final hour = DateTime.now().hour;
    final greeting = hour < 12
        ? 'Good morning'
        : (hour < 17 ? 'Good afternoon' : 'Good evening');
    final line = voice.isNotEmpty
        ? voice
        : "What's on your mind, Danny?";

    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 18, 20, 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '$greeting, Danny.',
            style: AppTheme.caption.copyWith(
              color: AppTheme.accent,
              fontSize: 11,
              letterSpacing: 0.6,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            line,
            style: const TextStyle(
              fontFamily: 'InstrumentSerif',
              fontSize: 19,
              fontWeight: FontWeight.w300,
              color: AppTheme.textPrimary,
              height: 1.3,
            ),
          ),
        ],
      ),
    );
  }

  /// CONVERSATION header: the current focus, compact, with a collapse
  /// chevron to return to the calm idle state.
  Widget _buildCompactFocalBar() {
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: AppTheme.border, width: 1)),
      ),
      child: Row(
        children: [
          Expanded(
            child: _focalItems.isEmpty
                ? _buildAllClearCompact()
                : _buildFocalCard(_focalItems.first, compact: true),
          ),
          // Collapse back to idle
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(10),
              onTap: () => setState(() => _conversationOpen = false),
              child: const Padding(
                padding: EdgeInsets.all(8),
                child: Icon(Icons.keyboard_arrow_down, color: AppTheme.textTertiary, size: 22),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildAllClearCompact() {
    return Row(
      children: [
        Container(
          width: 6,
          height: 6,
          decoration: const BoxDecoration(color: AppTheme.green, shape: BoxShape.circle),
        ),
        const SizedBox(width: 8),
        const Text(
          'All clear, Danny.',
          style: TextStyle(
            fontSize: 12,
            color: AppTheme.textTertiary,
            fontWeight: FontWeight.w300,
          ),
        ),
      ],
    );
  }


  // ── Voice ────────────────────────────────────────────────────

  Future<void> _initSpeech() async {
    _speechAvailable = await _speech.initialize(
      onError: (error) {
        if (!mounted) return;
        debugPrint('[Voice] Error: ${error.errorMsg}');
        if (error.errorMsg == 'error_speech_timeout') {
          setState(() { _voiceState = VoiceState.idle; _voiceError = null; });
          return;
        }
        setState(() { _voiceState = VoiceState.error; _voiceError = error.errorMsg; });
      },
      onStatus: (status) { debugPrint('[Voice] Status: $status'); },
    );
    if (!_speechAvailable && mounted) {
      setState(() { _voiceState = VoiceState.error; _voiceError = 'Speech recognition not available'; });
    }
  }

  void _toggleVoice() {
    if (_voiceState == VoiceState.idle) { _startListening(); }
    else if (_voiceState == VoiceState.listening) { _stopListening(); }
    else { setState(() { _voiceState = VoiceState.idle; _transcribedText = null; _voiceError = null; }); }
  }

  Future<void> _startListening() async {
    _voiceError = null;
    if (!_speechAvailable) {
      _speechAvailable = await _speech.initialize();
      if (!_speechAvailable) {
        if (mounted) { setState(() { _voiceState = VoiceState.error; _voiceError = 'Speech recognition not available'; }); }
        return;
      }
    }
    setState(() => _voiceState = VoiceState.listening);
    await _speech.listen(
      onResult: (result) {
        if (!mounted) return;
        final text = result.recognizedWords;
        if (text.isNotEmpty) { _transcribedText = text; }
        if (result.finalResult) {
          final words = result.recognizedWords;
          if (words.isNotEmpty) { _sendMessage(words); }
          setState(() => _voiceState = VoiceState.idle);
          _transcribedText = null;
        }
      },
      listenOptions: stt.SpeechListenOptions(
        listenFor: const Duration(seconds: 30),
        pauseFor: const Duration(seconds: 5),
        partialResults: true,
        cancelOnError: false,
      ),
    );
  }

  Future<void> _stopListening() async {
    await _speech.stop();
    if (!mounted) return;
    if (_transcribedText != null && _transcribedText!.isNotEmpty) {
      _sendMessage(_transcribedText!);
      setState(() => _voiceState = VoiceState.idle);
      _transcribedText = null;
      return;
    }
    setState(() { _voiceState = VoiceState.idle; _transcribedText = null; });
  }

  void _retryVoice() {
    setState(() { _voiceState = VoiceState.idle; _transcribedText = null; });
    Future.delayed(const Duration(milliseconds: 300), () {
      if (!mounted) return;
      _startListening();
    });
  }

  // ── Navigation ───────────────────────────────────────────────

  void _showMenu() {
    _instrumentation.menuOpens++;
    showMenuSheet(context);
  }

  void _openInbox() {
    _instrumentation.inboxBadgeTaps++;
    // The Inbox is always pushed as a route above home, so RouteAware's
    // didPopNext() fires on return and refreshes the live pending count +
    // focal board — no explicit refresh needed here (avoids double fetch).
    Navigator.push(context, MaterialPageRoute(builder: (_) => const InboxScreen()));
  }

  void _handlePushNotificationTap(Map<String, dynamic> data) {
    final type = data['type'];
    debugPrint('[PushNav] Notification type=$type data=$data');
    switch (type) {
      case 'decision':
      case 'delegation':
        _openInbox();
        break;
      case 'nudge':
        Navigator.push(context, MaterialPageRoute(builder: (_) => const TodayScreen()));
        break;
      case 'briefing':
        // Mirror Telegram: tapping the pulse notification opens the
        // conversation, where the full briefing lives as a BRIEFING card.
        // Force the history load and always land on the newest message — a
        // second tap in the same session must still show the fresh briefing
        // even if the user had scrolled up.
        if (!_conversationOpen) setState(() => _conversationOpen = true);
        _loadHistory(force: true).then((_) {
          if (mounted) _scrollToBottom();
        });
        break;
      default:
        break;
    }
  }

  /// A URL/text shared into Rhodey from another app (browser → Share sheet).
  /// Mirrors the Telegram save-link flow: forward it to Rhodey as a message —
  /// the backend's url_filter auto-saves bare URLs into `resources`. Sending
  /// enters the conversation state so the shared item + Rhodey's ack are
  /// visible in the thread.
  void _handleSharedText(String text) {
    if (text.trim().isEmpty) return;
    _sendMessage(text.trim());
  }

  // ── Scroll & TTS ─────────────────────────────────────────────

  void _scrollToBottom() {
    // The conversation area fades in through a 350ms AnimatedSwitcher — a
    // single delayed animateTo can fire before the list has clients (or a
    // settled extent), silently landing above the newest message. Retry until
    // the list attaches, then glide to the true bottom.
    _retryScrollToBottom(0);
  }

  void _retryScrollToBottom(int attempt) {
    if (!mounted) return;
    if (!_scrollController.hasClients) {
      if (attempt >= 10) return; // give up after ~1.2s
      Future.delayed(const Duration(milliseconds: 120), () {
        if (mounted) _retryScrollToBottom(attempt + 1);
      });
      return;
    }
    // reverse:true → offset 0 IS the bottom (the newest message). Measure
    // after the frame so the just-added message's layout is included.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_scrollController.hasClients) return;
      _scrollController.animateTo(
        0,
        duration: const Duration(milliseconds: 250),
        curve: Curves.easeOut,
      );
    });
  }

  void _speakMessage(String text) {
    if (text.trim().isEmpty) return;
    _tts.stop();
    _tts.speak(text.trim());
  }

  // ── Pulse mode helpers ──────────────────────────────────────

  String _pulseLabel() {
    final mode = _briefing.pulseMode ?? '';
    switch (mode.toLowerCase()) {
      case 'morning': return 'MORNING';
      case 'afternoon': return 'AFTERNOON';
      case 'closing_loop': return 'CLOSING';
      case 'weekend': return 'WEEKEND';
      case 'pre_monday': return 'PRE-MONDAY';
      case 'intel': return 'INTEL';
      default: return '';
    }
  }

  Color _pulseColor() {
    final mode = _briefing.pulseMode ?? '';
    switch (mode.toLowerCase()) {
      case 'morning': return AppTheme.accent;
      case 'afternoon': return AppTheme.amber;
      case 'closing_loop': return AppTheme.red;
      case 'intel': return AppTheme.textTertiary;
      default: return AppTheme.accent;
    }
  }

  // ═══════════════════════════════════════════════════════════
  // BUILD
  // ═══════════════════════════════════════════════════════════

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppTheme.background,
      body: SafeArea(
        child: Column(
          children: [
            _buildAmbientHeader(),
            Expanded(
              child: AnimatedSwitcher(
                duration: const Duration(milliseconds: 350),
                switchInCurve: Curves.easeOut,
                switchOutCurve: Curves.easeIn,
                child: _conversationOpen
                    ? _buildConversationState()
                    : _buildIdleState(),
              ),
            ),
            if (_voiceState == VoiceState.listening || _voiceState == VoiceState.error)
              VoiceStateMachine(
                state: _voiceState,
                transcribedText: _transcribedText,
                errorMessage: _voiceError,
                onCancel: _toggleVoice,
                onRetry: _retryVoice,
              ),
            _buildInputBar(),
          ],
        ),
      ),
    );
  }

  // ── Ambient Header ─────────────────────────────────────────

  Widget _buildAmbientHeader() {
    final pulseLabel = _pulseLabel();
    final pulseColor = _pulseColor();

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: AppTheme.border, width: 1)),
      ),
      child: Row(
        children: [
          // Menu
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(10),
              onTap: _showMenu,
              child: Container(
                padding: const EdgeInsets.all(8),
                child: const Icon(Icons.menu, color: AppTheme.textSecondary, size: 22),
              ),
            ),
          ),

          const SizedBox(width: 8),

          // Rhodey dot + pulse mode
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(
              color: pulseColor,
              shape: BoxShape.circle,
            ),
          ),
          const SizedBox(width: 6),
          Text(
            'Rhodey${pulseLabel.isNotEmpty ? ' · $pulseLabel' : ''}',
            style: AppTheme.body.copyWith(
              fontWeight: FontWeight.w600,
              fontSize: 13,
              letterSpacing: 0.3,
            ),
          ),

          const Spacer(),

          // Task icon — active task count. Tap opens the task ledger in chat.
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(10),
              onTap: _handleTaskIconTap,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                decoration: BoxDecoration(
                  color: AppTheme.surfaceAlt,
                  borderRadius: BorderRadius.circular(6),
                  border: Border.all(color: AppTheme.border, width: 1),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(Icons.checklist_rtl, size: 12, color: AppTheme.textSecondary),
                    const SizedBox(width: 3),
                    Text(
                      '$_activeTaskCount',
                      style: AppTheme.caption.copyWith(
                        color: AppTheme.textTertiary,
                        fontSize: 11,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
              ),
            ),
            ),
        ],
      ),
    );
  }

  // ── Focal Area (Chief's Stage) ─────────────────────────────

  Widget _buildFocalArea() {
    if (_briefingLoading || _focalLoading) {
      return Container(
        padding: const EdgeInsets.all(16),
        child: const Center(child: SizedBox(
          width: 14, height: 14,
          child: CircularProgressIndicator(strokeWidth: 2, color: AppTheme.textTertiary),
        )),
      );
    }

    return Container(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          // Focal card (Top-1 item)
          if (_focalItems.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: _buildFocalCard(_focalItems.first),
            ),

          // Decision digest
          if (!_focalLoading && _pendingDecisionCount > 0)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: _buildDecisionDigest(),
            ),

          // All-clear state
          if (_focalItems.isEmpty && !_focalLoading)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: _buildAllClear(),
            ),

          // Sunday weekly transparency report
          if (_briefing.transparencyReport != null && _briefing.transparencyReport!.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: _buildTransparencyReport(),
            ),
        ],
      ),
    );
  }

  // ── Focal Card (Phase 2 v2: three-button model with LLM reason) ──

  Widget _buildFocalCard(_FocalItem item, {bool compact = false}) {
    final isCompleting = _completingCardId == item.id;
    final isFromBriefing = item.metadata['from_briefing'] == true;
    final focalReason = item.metadata['reason'] as String?;
    final urgency = item.metadata['urgency'] as String?;

    // Urgency color
    Color urgencyColor;
    switch (urgency) {
      case 'critical':
        urgencyColor = AppTheme.red;
        break;
      case 'important':
        urgencyColor = AppTheme.amber;
        break;
      default:
        urgencyColor = AppTheme.accent;
    }

    return AnimatedOpacity(
      opacity: isCompleting ? 0.0 : 1.0,
      duration: const Duration(milliseconds: 350),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 350),
        width: double.infinity,
        padding: EdgeInsets.all(compact ? 10 : 14),
        decoration: BoxDecoration(
          color: isCompleting ? AppTheme.green.withValues(alpha: 0.08) : AppTheme.accentBg,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
            color: isCompleting
                ? AppTheme.green.withValues(alpha: 0.3)
                : urgencyColor.withValues(alpha: 0.18),
            width: 1,
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(
              children: [
                Container(
                  width: 3,
                  height: 14,
                  decoration: BoxDecoration(
                    color: isCompleting ? AppTheme.green : urgencyColor,
                    borderRadius: BorderRadius.circular(1),
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  isCompleting ? 'DONE' : 'FOCUS',
                  style: AppTheme.label.copyWith(
                    color: isCompleting ? AppTheme.green : urgencyColor,
                    fontSize: 9,
                    letterSpacing: 1.5,
                  ),
                ),
                if (isFromBriefing && !isCompleting)
                  Padding(
                    padding: const EdgeInsets.only(left: 6),
                    child: Text(
                      'RHODEY',
                      style: AppTheme.caption.copyWith(
                        color: AppTheme.textTertiary,
                        fontSize: 7,
                        letterSpacing: 1.0,
                      ),
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 10),

            // Title
            Text(
              item.title,
              style: AppTheme.body.copyWith(
                fontSize: compact ? 13 : 14,
                fontWeight: FontWeight.w600,
                height: 1.3,
              ),
            ),

            // LLM reason — the "why" behind this choice (hidden when compact)
            if (!compact && focalReason != null && focalReason.isNotEmpty && !isCompleting)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  focalReason,
                  style: AppTheme.bodySmall.copyWith(
                    color: AppTheme.textSecondary,
                    fontSize: 11,
                    height: 1.4,
                    fontStyle: FontStyle.italic,
                  ),
                ),
              ),

            // Chief-of-staff context line (added · org · deadline). Hidden
            // when compact (the narrow header shows just title + buttons) and
            // when the LLM reason is shown instead.
            if (!compact && item.subtitle != null && item.subtitle!.isNotEmpty && focalReason == null)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  item.subtitle!,
                  style: AppTheme.caption.copyWith(
                    color: AppTheme.textTertiary,
                    fontSize: 10,
                  ),
                ),
              ),

            // Next event (contextual)
            if (_briefing.nextEvent != null && _briefing.nextEvent!.isNotEmpty && !isFromBriefing)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  '📅 ${_briefing.nextEvent}',
                  style: AppTheme.caption.copyWith(
                    color: AppTheme.textTertiary,
                    fontSize: 10,
                  ),
                ),
              ),

            SizedBox(height: compact ? 8 : 12),

            // Three-button model
            Row(
              children: [
                // "I'll do it" — confirm and complete
                Expanded(
                  child: _MiniButton(
                    label: item.actionLabel,
                    color: AppTheme.green,
                    onTap: () => _completeFocalItem(item),
                  ),
                ),
                const SizedBox(width: 6),
                // "Not now" — snooze (no learning signal)
                Expanded(
                  child: _MiniButton(
                    label: 'Not now',
                    color: AppTheme.amber,
                    onTap: () => _snoozeFocalItem(item),
                  ),
                ),
                // Third button — type-aware:
                //  graph nodes/edges/merges → "Reject" (permanent, mirrors Inbox)
                //  tasks → "Not right" (defer + correction signal)
                const SizedBox(width: 6),
                Expanded(
                  child: _MiniButton(
                    label: _isGraphType(item) ? 'Reject' : 'Not right',
                    color: AppTheme.red,
                    onTap: () => _isGraphType(item)
                        ? _rejectFocalItem(item)
                        : _correctFocalItem(item),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  // ── Decision Digest ─────────────────────────────────────────

  Widget _buildDecisionDigest() {
    final count = _pendingDecisionCount;

    return Container(
      padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 12),
      decoration: BoxDecoration(
        color: AppTheme.green.withValues(alpha: 0.06),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(
          color: AppTheme.green.withValues(alpha: 0.15),
          width: 1,
        ),
      ),
      child: Row(
        children: [
          const Icon(Icons.check_circle_outline, size: 14, color: AppTheme.green),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              '$count item${count != 1 ? 's' : ''} in Inbox awaiting your decision.',
              style: AppTheme.bodySmall.copyWith(
                fontSize: 11,
                color: AppTheme.textSecondary,
                height: 1.3,
              ),
            ),
          ),
          const SizedBox(width: 8),
          GestureDetector(
            onTap: _openInbox,
            child: Text(
              'View',
              style: AppTheme.caption.copyWith(
                color: AppTheme.accent,
                fontSize: 11,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ── Transparency Report ──────────────────────────────────

  Widget _buildTransparencyReport() {
    final report = _briefing.transparencyReport ?? '';
    if (report.isEmpty) return const SizedBox.shrink();

    // Simplify for app display: extract the first few lines
    final lines = report.split('\n').where((l) => l.trim().isNotEmpty).toList();
    final displayLines = lines.length > 8 ? lines.take(8).toList() : lines;

    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: AppTheme.surfaceAlt,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: AppTheme.border, width: 1),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              const Text('🧠', style: TextStyle(fontSize: 14)),
              const SizedBox(width: 6),
              Text(
                "What I Learned This Week",
                style: AppTheme.label.copyWith(
                  fontSize: 11,
                  letterSpacing: 0.5,
                  color: AppTheme.textPrimary,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          for (final line in displayLines) ...[
            Padding(
              padding: const EdgeInsets.only(bottom: 3),
              child: Text(
                line,
                style: AppTheme.caption.copyWith(
                  fontSize: 10,
                  color: AppTheme.textSecondary,
                  height: 1.4,
                ),
              ),
            ),
          ],
          if (lines.length > 8)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: GestureDetector(
                onTap: () {
                  _addRhodeyResponse(report);
                },
                child: Text(
                  '+ See full report',
                  style: AppTheme.caption.copyWith(
                    color: AppTheme.accent,
                    fontSize: 10,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }

  // ── All Clear ──────────────────────────────────────────────

  Widget _buildAllClear() {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(
        children: [
          Container(
            width: 6,
            height: 6,
            decoration: const BoxDecoration(color: AppTheme.green, shape: BoxShape.circle),
          ),
          const SizedBox(width: 8),
          Flexible(
            child: Text(
              'All clear, Danny. What\'s on your mind?',
              style: const TextStyle(
                fontSize: 12,
                color: AppTheme.textTertiary,
                fontWeight: FontWeight.w300,
              ),
            ),
          ),
        ],
      ),
    );
  }

  // ── Conversation area ────────────────────────────────────────

  Widget _buildConversationArea() {
    if (_messages.isEmpty && _focalItems.isEmpty) {
      return _buildEmptyConversation();
    }

    return RefreshIndicator(
      onRefresh: () => _loadHistory(force: true),
      color: AppTheme.accent,
      // reverse:true pins offset 0 = the BOTTOM = the newest message. The task
      // ledger (posted at the end) is therefore visible BY CONSTRUCTION when
      // the conversation opens — no scroll race with the history backfill.
      child: ListView(
        reverse: true,
        controller: _scrollController,
        padding: const EdgeInsets.only(top: 4, bottom: 8),
        children: () {
          // _messages is stored oldest-first; with reverse:true the FIRST child
          // renders at the bottom, so iterate newest-first for a real chat.
          final reversed = _messages.reversed.toList();
          final entries = <Widget>[];

          for (var i = 0; i < reversed.length; i++) {
            final msg = reversed[i];

            // Task ledger (from the task icon) — rendered as an actionable list
            if (msg.type == MessageType.taskList && msg.taskList != null) {
              entries.add(_buildTaskListMessage(msg));
              continue;
            }

            if (msg.role == MessageRole.rhodey && !msg.isRhodeyTyping) {
              final cardData = resolveCardData(
                intent: msg.intent,
                text: msg.text,
                timestamp: msg.timestamp,
              );
              if (cardData != null) {
                entries.add(_buildRichCard(msg, cardData));
                continue;
              }
            }

            final isGroupStart = i == 0 || reversed[i - 1].role != msg.role;
            entries.add(ChatBubble(
              message: msg,
              isGroupStart: isGroupStart,
              onRetry: msg.isFailed ? () => _retryMessage(msg.id) : null,
              onTap: !msg.isUser && msg.text.isNotEmpty ? () => _speakMessage(msg.text) : null,
              onQuickReply: (reply) => _sendMessage(reply),
            ));
          }

          // 'Show earlier activity' is the LAST child → renders at the very
          // TOP (reverse:true flips the axis). Hidden once loaded.
          if (!_historyLoaded) {
            entries.add(GestureDetector(
              onTap: () => _loadHistory(),
              child: Container(
                margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
                decoration: BoxDecoration(
                  color: AppTheme.surfaceAlt,
                  borderRadius: BorderRadius.circular(20),
                  border: Border.all(color: AppTheme.border, width: 1),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(Icons.history, size: 14, color: AppTheme.textTertiary),
                    const SizedBox(width: 6),
                    Text(
                      'Show earlier activity',
                      style: AppTheme.caption.copyWith(
                        color: AppTheme.textTertiary,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              ),
            ));
          }

          entries.add(const SizedBox(height: 16));
          return entries;
        }(),
      ),
    );
  }

  Widget _buildEmptyConversation() {
    return ListView(
      controller: _scrollController,
      padding: const EdgeInsets.symmetric(vertical: 8),
      children: [
        const SizedBox(height: 16),
        const Center(
          child: Icon(Icons.chat_bubble_outline, size: 28, color: AppTheme.textMuted),
        ),
        const SizedBox(height: 12),
        const Center(
          child: Text(
            'Speak or type to get started',
            style: TextStyle(
              fontSize: 14,
              color: AppTheme.textTertiary,
              fontWeight: FontWeight.w300,
            ),
            textAlign: TextAlign.center,
          ),
        ),
      ],
    );
  }

  /// Find a task ID from a card title by fetching open tasks.
  Future<int?> _findTaskIdForCard(String title) async {
    final result = await _api.getTasks(status: 'todo');
    if (!result.success || result.data == null) return null;
    final lower = title.toLowerCase().trim();
    for (final t in result.data!) {
      final taskTitle = (t['title'] as String? ?? '').toLowerCase().trim();
      if (taskTitle == lower || taskTitle.contains(lower) || lower.contains(taskTitle)) {
        return t['id'] as int?;
      }
    }
    return null;
  }

  Widget _buildRichCard(ChatMessage msg, CardData cardData) {
    VoidCallback? onMarkDone;
    if (cardData.type == CardType.task) {
      onMarkDone = () async {
        _instrumentation.homeActionsCompleted++;
        final taskId = await _findTaskIdForCard(cardData.title);
        if (taskId != null && mounted) {
          await _api.updateTaskStatus(taskId, 'done');
        } else if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text('Task already completed or not found.', style: TextStyle(fontSize: 12)),
              backgroundColor: AppTheme.amber,
              duration: Duration(seconds: 2),
            ),
          );
        }
      };
    }

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.only(left: 16, bottom: 4),
            child: Text('Rhodey', style: AppTheme.caption.copyWith(
              color: AppTheme.accent, letterSpacing: 0.3,
            )),
          ),
          RichCardContent(
            cardData: cardData,
            onMarkDone: onMarkDone,
            onTap: () => _speakMessage(msg.text),
            quickReplies: msg.quickReplies,
            onQuickReply: msg.quickReplies?.isNotEmpty == true
                ? (reply) => _sendMessage(reply)
                : null,
          ),
        ],
      ),
    );
  }

  void _retryMessage(String id) {
    final idx = _messages.indexWhere((m) => m.id == id);
    if (idx == -1) return;
    final text = _messages[idx].text;
    setState(() {
      _messages.removeAt(idx);
    });
    _sendMessage(text);
  }

  // ── Input bar ──────────────────────────────────────────────

  Widget _buildInputBar() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: const BoxDecoration(
        border: Border(top: BorderSide(color: AppTheme.border, width: 1)),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
          // Voice button
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(20),
              onTap: _toggleVoice,
              child: Container(
                padding: const EdgeInsets.all(8),
                child: Icon(
                  _voiceState == VoiceState.listening ? Icons.mic : Icons.mic_none,
                  color: _voiceState == VoiceState.listening ? AppTheme.accent : AppTheme.textTertiary,
                  size: 20,
                ),
              ),
            ),
          ),

          const SizedBox(width: 4),

          // Text field
          Expanded(
            child: TextField(
              controller: _textController,
              style: AppTheme.body.copyWith(fontSize: 14),
              decoration: InputDecoration(
                hintText: 'Ask Rhodey...',
                hintStyle: AppTheme.caption.copyWith(
                  color: AppTheme.textTertiary,
                  fontSize: 14,
                ),
                border: InputBorder.none,
                contentPadding: const EdgeInsets.symmetric(vertical: 8),
                isDense: true,
              ),
              textInputAction: TextInputAction.send,
              onSubmitted: (text) {
                if (text.trim().isNotEmpty) {
                  _sendMessage(text.trim());
                }
              },
            ),
          ),

          // Send button
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(20),
              onTap: () {
                final text = _textController.text;
                if (text.trim().isNotEmpty) {
                  _sendMessage(text.trim());
                }
              },
              child: Container(
                padding: const EdgeInsets.all(8),
                child: const Icon(Icons.arrow_upward, color: AppTheme.accent, size: 20),
              ),
            ),
          ),
        ],
          ),
        ],
      ),
    );
  }
}

// ── Mini Button Widget ─────────────────────────────────────────

class _MiniButton extends StatelessWidget {
  final String label;
  final Color color;
  final VoidCallback onTap;

  const _MiniButton({
    required this.label,
    required this.color,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 8),
        decoration: BoxDecoration(
          color: color.withValues(alpha: 0.1),
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: color.withValues(alpha: 0.25), width: 1),
        ),
        child: Center(
          child: Text(
            label,
            style: AppTheme.caption.copyWith(
              color: color,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ),
    );
  }
}
