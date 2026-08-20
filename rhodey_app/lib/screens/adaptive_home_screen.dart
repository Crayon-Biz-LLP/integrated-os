import 'dart:async';
import 'package:flutter/material.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:flutter_tts/flutter_tts.dart';
import 'package:image_picker/image_picker.dart';
import 'package:file_picker/file_picker.dart';
import 'menu_sheet.dart';
import 'inbox_screen.dart';
import 'today_screen.dart';
import '../models/message.dart';
import '../services/notification_service.dart';
import '../models/briefing.dart';
import '../services/api_service.dart';
import '../services/message_cache.dart';
import '../services/share_service.dart';
import '../theme/app_theme.dart';
import '../voice/rhodey_voice.dart';
import '../utils/route_observer.dart';
import '../services/widget_data_provider.dart';
import '../widgets/chat_bubble.dart';
import '../widgets/merge_search_sheet.dart';
import '../widgets/voice_states.dart';
import '../widgets/rich_card_content.dart';
import '../widgets/suggestion_card.dart';
import '../widgets/home_tour_overlay.dart';
import 'package:shared_preferences/shared_preferences.dart';
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

/// One dynamic action button on the focal card.
class _FocalAction {
  final String label;
  final Color color;
  final VoidCallback onTap;

  const _FocalAction({
    required this.label,
    required this.color,
    required this.onTap,
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

  /// Phase 2B: the tenant's persona voice_style token ('direct' | 'calm' |
  /// 'warm' | '' for standard). Drives Rhodey's own static app lines; the
  /// server-composed strings (briefing, focal confirmations) already carry
  /// the persona. '' => standard voice (fail-closed).
  String _voiceStyle = '';
  bool _briefingLoading = true;

  /// M9.6: the tenant's display name — resolved from the API on load and
  /// used in greetings instead of the hardcoded 'Danny'.
  String _userName = '';

  // ── Focal zone (Phase 2 v2: LLM-chosen top item + three-button model) ──
  List<_FocalItem> _focalItems = [];

  /// Session-scoped memory of items the user deferred or resolved this
  /// session ("type:id"). The stored briefing's top_focal_item is static —
  /// it outlives the board — so without this a "Not now" item would be
  /// re-promoted on the next home load (the "came back" bug).
  final Set<String> _deferredFocalKeys = <String>{};
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

  /// Memoized card resolution keyed by message id — resolveCardData (regex
  /// parsing) runs at most once per message instead of on every rebuild.
  final _cardDataCache = <String, CardData?>{};
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

  /// Id of the in-flight fast-ack bubble ("Got it. Processing..."). The
  /// backup poll removes/replaces it when Rhodey's real reply lands.
  String? _pendingAckId;

  /// Expiry watchdog for the fast-ack bubble: if Rhodey's reply doesn't land
  /// within 3 minutes (worker failure / LLM outage), soften the ack instead
  /// of leaving a dead "Processing..." bubble forever.
  Timer? _ackExpiryTimer;

  /// True while the /api/send-message request is still awaiting a response.
  /// The timeout watchdog extends while this is true (the inline fallback
  /// runs the full LLM turn and can take 30-60s).
  bool _sendInFlight = false;

  /// Guards against duplicate history loads.
  bool _historyLoaded = false;

  /// M14: one-time first-run home tour — teaches what the header icons and
  /// the input bar are (the task-count icon especially was unclear).
  bool _showHomeTour = false;

  /// Guards against double-tap on the task icon while the ledger is posting.
  bool _taskTapping = false;

  // ── Two-state layout (idle ⇄ conversation) ──
  bool _conversationOpen = false;

  /// Active (todo) task count — shown on the task icon badge.
  int _activeTaskCount = 0;

  /// Title→id index of the last-fetched todo tasks — powers card "Mark done"
  /// with a synchronous local lookup (no per-tap network round-trip) and
  /// fail-closed matching (never completes the wrong task).
  final Map<String, int> _taskIdByTitle = {};

  /// True once a backfill fetch has been attempted — the index is normally
  /// populated by _applyFocalData, so the fetch only fires when that was empty.
  bool _taskIndexBackfilled = false;

  /// Debounce timer for push notifications — coalesces rapid pushes.
  Timer? _pushDebounce;

  /// Coalesces home refreshes triggered back-to-back (a route pop and a
  /// lifecycle resume can both fire within the same second) into one call.
  Timer? _homeRefreshDebounce;

  // ── WhatsApp-style instant delivery ──
  /// Number of live home-feed reconciles currently in flight (after the cached
  /// instant render). A counter — not a bare bool — so overlapping refreshes
  /// (initState + didPopNext + lifecycle resume) never clear the "syncing"
  /// chip while another fetch is still running. The chip is visible while
  /// > 0, making the background refresh visible, not silent.
  int _syncingCount = 0;
  bool get _isSyncing => _syncingCount > 0;

  /// On-device cache of the last conversation page — cold opens and
  /// notification taps render from this instantly, then reconcile with the
  /// server (write-behind on every successful fetch).
  final _messageCache = MessageCache();

  /// Content rendered instantly from a tapped push (provisional bubble). The
  /// real server row replaces it when history lands — tracked to dedupe.
  String? _pendingPushContent;
  String? _pendingPushMessageId;

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
  /// decision digest and focal board reflect decisions made there — debounced
  /// so a simultaneous lifecycle resume doesn't trigger a duplicate refresh.
  /// Also picks up a "replay the home tour" request set from Settings (the
  /// tour is anchored to this screen's layout, so it can only run here).
  @override
  void didPopNext() {
    _scheduleHomeRefresh();
    _maybeShowHomeTour();
  }

  /// App returned to foreground. FCM `onMessage` only fires in the foreground,
  /// so a pulse briefing that ran while the app was backgrounded would leave
  /// home stale (yesterday's voice line, old focal board) until pull-to-refresh.
  /// Refresh the pulse intelligence + focal board on every genuine resume.
  /// If a reply is still pending (fast-ack sent, worker still running), also
  /// poll conversation history immediately — the 60s backup poll alone would
  /// otherwise delay an already-arrived reply by up to a minute after resume.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && _hasStarted) {
      // Only refresh home when Home is actually the visible route. A pushed
      // screen (Inbox/Today/Entities) refreshes itself on resume and fires
      // didPopNext on the way back — refreshing home underneath a pushed
      // screen would be a wasted /api/home-feed round-trip (and with the
      // briefing rebuild that used to mean a 10-16s server hog per resume).
      final route = ModalRoute.of(context);
      if (route == null || route.isCurrent) {
        _scheduleHomeRefresh();
      }
      if (_awaitingResponse) {
        _pollForUpdates();
      }
    }
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _hasStarted = true;
    });
    _loadHome();
    _initSpeech();
    _tts.setLanguage("en-US");
    _tts.setSpeechRate(0.5);
    _maybeShowHomeTour();

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
    if (NotificationService.onNotificationOpened ==
        _handlePushNotificationTap) {
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
    _ackExpiryTimer?.cancel();
    _pushDebounce?.cancel();
    _homeRefreshDebounce?.cancel();
    _textController.dispose();
    _scrollController.dispose();
    _instrumentation.log();
    super.dispose();
  }

  // ── Pulse intelligence ───────────────────────────────────────────

  Future<void> _loadBriefing() async {
    try {
      final briefing = await _api.getBriefing();
      if (mounted) _applyBriefing(briefing);
    } catch (_) {
      if (mounted) {
        setState(() => _briefingLoading = false);
      }
    }
  }

  /// Applies a fetched briefing to state — shared by the legacy _loadBriefing
  /// and the single-call _loadHome (P2).
  ///
  /// [liveItemIds] — ids currently on the live, snooze-filtered board
  /// (pending decisions + active tasks). When provided, the LLM's
  /// top_focal_item is only promoted while it still maps to a live item: an
  /// item the user deferred, or that got resolved since the pulse wrote the
  /// briefing, must not resurface (the "Not now came back" bug).
  void _applyBriefing(
    BriefingResponse briefing, {
    Set<String>? liveItemIds,
    bool promoteFocal = true,
  }) {
    if (!mounted) return;
    setState(() {
      _briefing = briefing;
      _briefingLoading = false;

      // If briefing has an LLM-chosen top_focal_item, promote it as #1 — but
      // only while it's still genuinely actionable. Cached renders (cold open)
      // skip promotion entirely: the cache can't prove an item is still live,
      // and the live fetch that follows promotes the real one.
      final focal = promoteFocal ? briefing.topFocalItem : null;
      if (focal != null && focal.isNotEmpty && focal['title'] != null) {
        // Normalize case: the LLM type drives both the deferred-key memory and
        // the live-board cross-check — a "Task" vs "task" mismatch would let a
        // deferred item resurface. The raw value is kept for display/source.
        final itemTypeRaw = focal['type'] as String? ?? 'task';
        final itemType = itemTypeRaw.toLowerCase();
        final naturalId = focal['item_id']?.toString() ?? '';
        final deferredKey = naturalId.isEmpty ? '' : '$itemType:$naturalId';
        final wasDeferredThisSession =
            deferredKey.isNotEmpty && _deferredFocalKeys.contains(deferredKey);
        final stillLive =
            liveItemIds == null ||
            _focalItemStillLive(itemType, naturalId, liveItemIds);
        if (!wasDeferredThisSession && stillLive) {
          final existingIdx = _focalItems.indexWhere(
            (f) => f.id == 'focal_${focal['item_id']}',
          );
          final llmLabel = focal['action_label'] as String?;
          final newItem = _FocalItem(
            id: 'focal_${focal['item_id'] ?? focal['title']}',
            title: focal['title'] as String? ?? '',
            description: focal['reason'] as String?,
            actionLabel: _actionLabelForType(itemType, llmLabel),
            source: itemTypeRaw,
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
          if (naturalId.isNotEmpty) {
            _focalItems.removeWhere(
              (f) => f.id == naturalId && f.id != newItem.id,
            );
          }
        }
      }
    });
    WidgetDataProvider().updatePulseWidget(briefing);
  }

  /// True when the briefing's focal item still exists on the live board.
  /// The live lists are snooze-filtered server-side, so an id that is absent
  /// was deferred, resolved, or auto-approved since the pulse ran — promoting
  /// it again is exactly the resurrection bug. Only task / pending-decision
  /// types have a live list to verify against; anything else (calendar, notes,
  /// vault) is the LLM's judgment call and is kept.
  bool _focalItemStillLive(
    String type,
    String naturalId,
    Set<String> liveItemIds,
  ) {
    if (naturalId.isEmpty) return true;
    final mapsToBoard =
        type == 'task' ||
        type == 'graph_node' ||
        type == 'graph_edge' ||
        type == 'merge';
    if (!mapsToBoard) return true;
    // Trade-off: a home-feed sub-query that fails returns an empty list (its
    // fetcher swallows errors), so a genuinely-live item could be skipped for
    // one load. That self-heals on the next refresh and is rarer than the
    // ghost-resurrection this prevents — the session deferred-key set already
    // covers the same-session case; this cross-check is the restart safety net.
    return liveItemIds.contains(naturalId);
  }

  // ── Focal items ─────────────────────────────────────────────────

  Future<void> _loadFocalItems() async {
    try {
    final decResult = await _api.getPendingDecisions();
    // Fetch open (todo) AND committed (in_progress) tasks: committed tasks
    // stay visible in the ledger/count but never become focal cards below.
    final taskResult = await _api.getTasks(status: 'todo,in_progress');
      if (!mounted) return;
      final decisions = (decResult.success && decResult.data != null)
          ? decResult.data!
          : <PendingDecision>[];
      final tasks = (taskResult.success && taskResult.data != null)
          ? taskResult.data!
          : <Map<String, dynamic>>[];
      _applyFocalData(decisions, tasks);
    } catch (_) {
      // Network or parse error — show empty state
    } finally {
      if (mounted) {
        setState(() => _focalLoading = false);
      }
    }
  }

  /// Applies fetched decisions + tasks to the focal board — shared by the
  /// legacy _loadFocalItems and the single-call _loadHome (P2).
  void _applyFocalData(
    List<PendingDecision> decisions,
    List<Map<String, dynamic>> tasks,
  ) {
    if (!mounted) return;
    final items = <_FocalItem>[];

    // Pending decisions are deliberately NOT added to the focal board — they
    // live in Quick Confirmation (the single decision surface). Auto-dumping
    // them here caused the same item to appear in two places with two
    // different action vocabularies (View/Not now vs Approve/Reject).
    // Decisions still feed the digest count below, and the briefing may
    // promote ONE as the focal card when it's genuinely the top priority.
    _pendingDecisionCount = decisions.length;

    // Only non-terminal tasks count toward the active ledger and the
    // title→id index — a stale on-device cache can briefly carry
    // done/cancelled rows, and a done title in the index could make a
    // conversation "Mark done" resolve to an already-completed task.
    final activeTasks = tasks.where((t) {
      final st = (t['status'] as String? ?? 'todo').toLowerCase();
      return st != 'done' && st != 'cancelled';
    }).toList();

    // Due/overdue tasks fill remaining slots
    _activeTaskCount = activeTasks.length;

    // Refresh the title→id index with the freshest task list — card "Mark
    // done" resolves ids locally from this (no per-tap network fetch).
    _taskIdByTitle.clear();
    for (final t in activeTasks) {
      final id = t['id'] as int?;
      final title = (t['title'] as String? ?? '').toLowerCase().trim();
      if (id != null && title.isNotEmpty) {
        // putIfAbsent: first match wins for duplicate titles (same semantics
        // as the old first-match loop).
        _taskIdByTitle.putIfAbsent(title, () => id);
      }
    }

    for (final t in activeTasks) {
      // Defensive: never surface a terminal-status task on the board. The
      // server filters to 'todo', but the on-device cache can briefly hold
      // pre-completion rows — a task completed elsewhere (Today screen,
      // conversation card, Telegram) must not repaint while the live fetch
      // reconciles.
      final st = (t['status'] as String? ?? 'todo').toLowerCase();
      if (st == 'done' || st == 'cancelled') continue;
      // Committed ("I'll do it") tasks never get pushed back onto the focal
      // board — the user already took them on; they stay on the board.
      if (st == 'in_progress') continue;
      final deadline = t['deadline'] as String?;
      if (deadline == null || !_isUrgent(deadline)) continue;
      items.add(
        _FocalItem(
          id: t['id'].toString(),
          title: t['title'] as String? ?? 'Untitled',
          subtitle: _formatTaskContext(t),
          source: 'task',
          actionLabel: 'Done',
          metadata: {'task_id': t['id']},
        ),
      );
    }

    // Live id sets for the preserved-item cross-check. A briefing-chosen or
    // manually-promoted item whose underlying task/decision was resolved
    // since it was surfaced (completed via another surface, a timed-out
    // completion that still landed server-side, Telegram, etc.) must not be
    // resurrected — that ghost is exactly this bug (task done in the DB but
    // still showing on the focal card). Reuses the promotion guard's
    // still-live semantics via _focalItemStillLive.
    final liveIds = <String>{
      for (final t in tasks) t['id'].toString(),
      for (final d in decisions) d.id,
    };

    setState(() {
      // Preserve items that were deliberately surfaced: the LLM-chosen
      // briefing item AND any task the user manually promoted to focus —
      // but only while the underlying item is still genuinely actionable.
      // Everything else rebuilds from live pending/task data.
      final preserved = <_FocalItem>[];
      for (final f in _focalItems) {
        if (f.metadata['from_briefing'] != true &&
            f.metadata['promoted'] != true) {
          continue;
        }
        // Resolve the item's server-side identity for the cross-check.
        final naturalId =
            (f.metadata['focal_item_id'] ??
                    f.metadata['task_id'] ??
                    f.metadata['pending_id'])
                ?.toString() ??
            '';
        final type = (f.metadata['focal_type'] as String? ?? f.source ?? 'task')
            .toLowerCase();
        final stillLive = _focalItemStillLive(type, naturalId, liveIds);
        // Drop only when we have real knowledge: an empty live set means the
        // sub-fetch failed (fetchers swallow errors → []). A dropped briefing
        // item self-heals via re-promotion, but a manually promoted item
        // would be permanently lost — keep it when we can't prove it's gone.
        if (liveIds.isNotEmpty && !stillLive) continue;
        preserved.add(f);
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

  /// ONE-CALL home load (P2): briefing + focal board + counts from
  /// /api/home-feed — collapses the app's 6 startup round-trips into 1.
  /// Falls back to the two legacy calls when the endpoint is unavailable
  /// (older backend) or returns an empty payload.
  Future<void> _loadHome() async {
    // M9.6: resolve the tenant's display name in parallel with the feed — a
    // local prefs read after the first fetch, one /api/onboarding/status
    // round-trip before that.
    final nameFuture = _api.resolveUserName();
    try {
      // WhatsApp-style instant render: show the cached home feed (voice line,
      // focal board, briefing) BEFORE any network round-trip, so cold opens and
      // notification taps never stare at a spinner while the server responds.
      // The live fetch below reconciles and re-saves the cache.
      final cached = await _api.getCachedHomeFeed();
      if (cached != null && mounted) {
        // Cached render: paint the voice line + board instantly, but never
        // promote the cached briefing's top_focal_item. The cache is stale by
        // definition, so its own decision/task lists can't validate whether an
        // item is still live (e.g. snoozed since the cache was written) — the
        // live fetch a moment later promotes the correct focal item.
        _applyHomeFeed(cached, promoteFocal: false);
      }
      // Background reconcile: keep the sync chip visible while the live
      // fetch refreshes what the cache just painted. Counter-based so an
      // overlapping refresh never clears the chip for the other.
      if (mounted) setState(() => _syncingCount++);
      try {
        final feed = await _api.getHomeFeed();
        if (!mounted) return;
        if (feed.isEmpty) {
          // Older backend without /api/home-feed — use the legacy calls.
          await Future.wait([_loadBriefing(), _loadFocalItems()]);
          return;
        }
        _applyHomeFeed(feed);
        // Phase 2B: if the feed carried no persona block (older backend or a
        // cache written before the persona key shipped), fall back to the
        // dedicated endpoint so the voice token still lands.
        if (feed.persona.isEmpty) {
          unawaited(_resolvePersonaFallback());
        }
      } finally {
        if (mounted) {
          setState(() => _syncingCount = (_syncingCount - 1).clamp(0, 1 << 30));
        }
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          _briefingLoading = false;
          _focalLoading = false;
          _syncingCount = 0;
        });
      }
    }
    // M9.6: apply the resolved display name (usually instant — local read;
    // never throws — the resolver swallows errors and returns ''). The
    // equality guard skips a pointless rebuild when the name is unchanged
    // across home refreshes.
    final who = await nameFuture;
    if (mounted && who.isNotEmpty && who != _userName) {
      setState(() => _userName = who);
    }
  }

  /// Applies a home feed to state — shared by the cached instant render and
  /// the live fetch so both paths agree on the focal board + briefing.
  void _applyHomeFeed(HomeFeed feed, {bool promoteFocal = true}) {
    // Phase 2B: adopt the persona voice token from the same round-trip.
    _voiceStyle = feed.persona.voiceStyle;
    // Live, snooze-filtered board ids. A stored briefing's top_focal_item
    // can outlive its item (deferred/resolved since the pulse ran) — the
    // home-feed lists are snooze-filtered server-side, so an id absent here
    // is gone from the board and must not be re-promoted.
    final liveItemIds = <String>{
      for (final d in feed.decisions) d.id,
      for (final t in feed.tasks) t['id'].toString(),
    };
    _applyBriefing(
      feed.briefing,
      liveItemIds: liveItemIds,
      promoteFocal: promoteFocal,
    );
    _applyFocalData(feed.decisions, feed.tasks);
  }

  /// Phase 2B fallback: when the home-feed payload carries no persona block,
  /// resolve the surface summary from GET /api/persona so Rhodey's voice
  /// token still lands. Fail-closed: any failure keeps the standard voice.
  Future<void> _resolvePersonaFallback() async {
    try {
      final p = await _api.getPersona();
      if (!mounted || p.isEmpty || p.voiceStyle == _voiceStyle) return;
      setState(() => _voiceStyle = p.voiceStyle);
    } catch (_) {
      // Keep the standard voice — never throw from a home load.
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
      final isEcho =
          title.isNotEmpty &&
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

  /// The focal item's type discriminator ("task", "graph_node", ...) from
  /// either the briefing's focal_type or the legacy source field.
  String _focalType(_FocalItem item) =>
      (item.metadata['focal_type'] as String?) ?? item.source ?? '';

  /// True only for graph_node (person/org/concept) items — the merge picker
  /// applies to pending nodes only, never edges or merge proposals.
  bool _isFocalPerson(_FocalItem item) => _focalType(item) == 'graph_node';

  /// The dynamic action set for a focal card — chosen by item type so the
  /// options always match the Inbox's vocabulary for that item:
  ///   task        → I'll do it · Not now · Not right (home trio)
  ///   graph_node  → Approve · Not now · Reject (+ merge link below the card)
  ///   graph_edge  → Approve · Edit · Reject
  ///   merge       → Accept · Not now · Reject
  ///   channel suggestion (email/whatsapp/teams/call) → Approve · Not right
  ///     (2 buttons — messages have no snooze columns, and "Not right" keeps
  ///     the correction signal while the item stays in the Inbox queue)
  List<_FocalAction> _focalActionsFor(_FocalItem item) {
    final type = _focalType(item);
    switch (type) {
      case 'graph_edge':
        return [
          _FocalAction(
            label: 'Approve',
            color: AppTheme.green,
            onTap: () => _completeFocalItem(item),
          ),
          _FocalAction(
            label: 'Edit',
            color: AppTheme.accent,
            onTap: () => _editFocalEdge(item),
          ),
          _FocalAction(
            label: 'Reject',
            color: AppTheme.red,
            onTap: () => _rejectFocalItem(item),
          ),
        ];
      case 'merge':
        return [
          _FocalAction(
            label: 'Accept',
            color: AppTheme.green,
            onTap: () => _completeFocalItem(item),
          ),
          _FocalAction(
            label: 'Snooze',
            color: AppTheme.amber,
            onTap: () => _snoozeFocalItem(item),
          ),
          _FocalAction(
            label: 'Reject',
            color: AppTheme.red,
            onTap: () => _rejectFocalItem(item),
          ),
        ];
      case 'graph_node':
        return [
          _FocalAction(
            label: 'Approve',
            color: AppTheme.green,
            onTap: () => _completeFocalItem(item),
          ),
          _FocalAction(
            label: 'Snooze',
            color: AppTheme.amber,
            onTap: () => _snoozeFocalItem(item),
          ),
          _FocalAction(
            label: 'Reject',
            color: AppTheme.red,
            onTap: () => _rejectFocalItem(item),
          ),
        ];
      case 'email':
      case 'whatsapp':
      case 'teams':
      case 'call':
        return [
          _FocalAction(
            label: 'Approve',
            color: AppTheme.green,
            onTap: () => _completeFocalItem(item),
          ),
          _FocalAction(
            label: 'Not right',
            color: AppTheme.red,
            onTap: () => _correctFocalItem(item),
          ),
        ];
      case 'task':
      default:
        return [
          _FocalAction(
            label: item.actionLabel,
            color: AppTheme.green,
            onTap: () => _completeFocalItem(item),
          ),
          _FocalAction(
            label: 'Snooze',
            color: AppTheme.amber,
            onTap: () => _snoozeFocalItem(item),
          ),
          _FocalAction(
            label: 'Not right',
            color: AppTheme.red,
            onTap: () => _correctFocalItem(item),
          ),
        ];
    }
  }

  /// Render the dynamic action buttons as an evenly-spaced row (2 or 3
  /// buttons depending on the item type).
  List<Widget> _buildFocalActions(_FocalItem item) {
    final actions = _focalActionsFor(item);
    return [
      for (var i = 0; i < actions.length; i++) ...[
        if (i > 0) const SizedBox(width: 6),
        Expanded(
          child: _MiniButton(
            label: actions[i].label,
            color: actions[i].color,
            onTap: actions[i].onTap,
          ),
        ),
      ],
    ];
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
      // Pending channel suggestions (email/whatsapp/teams/call) promoted by
      // the briefing — same verb as the Inbox so the action always matches.
      case 'email':
      case 'whatsapp':
      case 'teams':
      case 'call':
        return 'Approve';
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
    // "I'll do it" is a commitment, never a completion: the task flips to
    // in_progress (stays on the board) instead of being closed. The user may
    // not have finished it yet — 'done' only happens when they really do.
    final focalType =
        item.metadata['focal_type'] as String? ?? item.source ?? 'task';
    final isTaskCommit = focalType == 'task';
    if (fromBriefing && item.metadata['focal_item_id'] != null) {
      // LLM-chosen item from briefing — use unified endpoint
      result = await _api.focalAction(
        action: isTaskCommit ? 'commit' : 'done',
        itemType: focalType,
        itemId: item.metadata['focal_item_id'].toString(),
        title: item.title,
      );
    } else {
      // Legacy fallback: direct API calls
      if (item.source == 'task') {
        final taskId = item.metadata['task_id'];
        if (taskId != null) {
          result =
              await _api.updateTaskStatus(taskId as int, 'in_progress');
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
            case 'teams':
              result = await _api.approveTeams(pendingId);
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
        final serverMsg = (data is Map && data['message'] is String)
            ? data['message'] as String
            : null;
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              result == null
                  ? RhodeyVoice.couldntComplete()
                  : (serverMsg ??
                        result.error ??
                        RhodeyVoice.couldntComplete()),
              style: const TextStyle(fontSize: 12),
            ),
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
      _addRhodeyResponse(RhodeyVoice.allClear(_userName, _voiceStyle));
    }

    // N4: local count decrement instead of a 5-call refetch — the action
    // already succeeded; the digest ticks down here and the next natural
    // refresh (pull / resume / Inbox return) reconciles with the server.
    // A task commit is NOT a count decrement — the task stays active
    // (in_progress) and still counts toward the ledger; only done/cancelled
    // removals tick the count.
    if (!isTaskCommit) {
      _decrementCountsFor(item);
    }

    // Honest commit feedback: the card vanishes but the task didn't close —
    // say so, so "I'll do it" never reads like a completion.
    if (isTaskCommit && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            "Marked as in progress — it stays on your board until you finish it.",
            style: TextStyle(fontSize: 12),
          ),
          backgroundColor: AppTheme.green,
          duration: Duration(seconds: 2),
        ),
      );
    }
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
  ///
  /// Snooze escalation (M12): 1st "Not now" → 1 day, 2nd → 3 days, 3rd →
  /// 7 days behind a warning + feedback gate, 4th+ → 7 days (cap, quiet).
  /// A dry-run call reveals the ladder position without persisting; the
  /// warning fires exactly once (3rd tap) and the typed feedback is stored
  /// as a learning signal server-side.
  Future<void> _snoozeFocalItem(_FocalItem item) async {
    _instrumentation.itemsDismissed++;

    final args = _focalActionArgs(item);
    if (args == null) {
      // No server-side id to defer — don't pretend the card is dismissed.
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              RhodeyVoice.couldntDefer(),
              style: TextStyle(fontSize: 12),
            ),
            backgroundColor: AppTheme.amber,
            duration: const Duration(seconds: 2),
          ),
        );
      }
      return;
    }

    // ── Escalation gate (M12): ask before the 3rd snooze ──
    // Dry-run tells us the ladder position without persisting anything. If
    // the check fails (network, pre-db/92) we degrade to a direct snooze —
    // the gate is a courtesy, never a blocker.
    String feedback = '';
    try {
      final check = await _api.focalAction(
        action: 'snooze',
        itemType: args.$1,
        itemId: args.$2,
        title: item.title,
        dryRun: true,
      );
      final checkOk = _focalActionOk(check);
      final checkData = (checkOk && check.data is Map) ? check.data as Map : null;
      if (checkData != null && checkData['warn'] == true) {
        final count = (checkData['count'] as num?)?.toInt() ?? 3;
        final typed = await _showSnoozeWarningDialog(
          item,
          count: count,
          voiceStyle: _voiceStyle,
        );
        if (typed == null || !mounted) return; // cancelled — card stays
        feedback = typed;
      }
    } catch (_) {
      // Degrade gracefully — proceed with a direct snooze.
    }

    final result = await _api.focalAction(
      action: 'snooze',
      itemType: args.$1,
      itemId: args.$2,
      title: item.title,
      feedback: feedback,
    );
    final persisted = _focalActionOk(result);

    if (!persisted) {
      // Deferral wasn't persisted server-side — don't pretend it was.
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: const Text(
              "Couldn't defer that item — it will resurface. Please try again.",
              style: TextStyle(fontSize: 12),
            ),
            backgroundColor: AppTheme.red,
            duration: const Duration(seconds: 2),
          ),
        );
      }
      return;
    }

    // Session-scoped memory: this item was deferred — a stale stored briefing
    // must not re-promote it on the next home load.
    _deferredFocalKeys.add('${args.$1.toLowerCase()}:${args.$2}');
    setState(() {
      _focalItems.removeWhere((c) => c.id == item.id);
    });
    if (_focalItems.isEmpty && mounted) {
      _addRhodeyResponse(
        "Noted. I'll keep it off your board for a while — it'll resurface if it's still relevant.",
      );
    }
    // N4: local count decrement instead of a 5-call refetch.
    _decrementCountsFor(item);
  }

  /// Third-tap "Not now" gate (M12): warning + feedback box. Returns the
  /// typed feedback, or null when the user cancels (card stays on board).
  ///
  /// Phase 2B: the system gate (title/body) stays neutral — only the
  /// feedback hint line shifts with the persona voice_style token.
  Future<String?> _showSnoozeWarningDialog(
    _FocalItem item, {
    required int count,
    String voiceStyle = '',
  }) async {
    final controller = TextEditingController();
    final shouldSnooze = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
        title: const Text('You\u2019ve snoozed this before'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'This is the ${_ordinal(count)} time this item has been '
                'snoozed. It will stay off your board for 7 days.',
                style: const TextStyle(fontSize: 14),
              ),
              const SizedBox(height: 8),
              Text(
                switch (voiceStyle) {
                  'direct' =>
                    'Tell Rhodey why — one line is enough. It learns from this.',
                  'warm' => 'Tell Rhodey what changed — every answer helps it '
                      'understand you better.',
                  _ => 'Tell Rhodey why you\u2019re deferring it \u2014 every '
                      'answer makes it smarter.',
                },
                style: const TextStyle(fontSize: 12, color: Colors.grey),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: controller,
                maxLines: 3,
                maxLength: 500,
                decoration: InputDecoration(
                  hintText: 'e.g. Not urgent right now, follow up next month...',
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                  contentPadding: const EdgeInsets.all(12),
                ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Keep it'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Snooze 7 days'),
          ),
        ],
      ),
    );
    final feedback = (shouldSnooze == true && mounted) ? controller.text.trim() : null;
    controller.dispose();
    return feedback;
  }

  /// "3rd", "4th", ... — used by the snooze warning dialog.
  String _ordinal(int n) {
    final mod100 = n % 100;
    if (mod100 >= 11 && mod100 <= 13) return '${n}th';
    switch (n % 10) {
      case 1:
        return '${n}st';
      case 2:
        return '${n}nd';
      case 3:
        return '${n}rd';
      default:
        return '${n}th';
    }
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

  /// N4: decrement the digest / active-task count locally after a focal
  /// action succeeds. The card is already removed from the board — a full
  /// _loadFocalItems refetch (5 network calls) is redundant for one action.
  void _decrementCountsFor(_FocalItem item) {
    if (!mounted) return;
    setState(() {
      if (item.source == 'task') {
        if (_activeTaskCount > 0) _activeTaskCount--;
      } else if (_pendingDecisionCount > 0) {
        _pendingDecisionCount--;
      }
    });
  }

  /// Open the merge picker for a person (graph_node) focal card — mirrors the
  /// Inbox's "Merge into existing" action. The pending id comes from either
  /// the legacy pending_id metadata or the briefing's focal_item_id.
  Future<void> _openFocalMerge(_FocalItem item) async {
    final m = item.metadata;
    int? pendingId = m['pending_id'] as int?;
    if (pendingId == null) {
      final focalId = m['focal_item_id'];
      if (focalId != null) pendingId = int.tryParse(focalId.toString());
    }
    if (pendingId == null) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text(
              "I couldn't start a merge for that item.",
              style: TextStyle(fontSize: 12),
            ),
            backgroundColor: AppTheme.amber,
            duration: Duration(seconds: 2),
          ),
        );
      }
      return;
    }

    // Focal cards don't carry the node type discriminator — search all live
    // nodes so any org/person/concept can be chosen as the target.
    final target = await MergeSearchSheet.show(context, nodeType: null);
    if (target == null || !mounted) return;
    final targetId = target['id'] as String?;
    if (targetId == null) return;
    final targetLabel = target['label'] as String? ?? 'existing node';

    final result = await _api.mergeGraphNode(
      pendingId.toString(),
      targetId,
      scope: 'pending',
    );
    if (!mounted) return;
    if (!_focalActionOk(result)) {
      // Business failures come back as HTTP 200 + {"success": false,
      // "message": ...} — result.error is '' then, so read the server's
      // message from the body (same pattern as _completeFocalItem).
      final data = result.data;
      final serverMsg = (data is Map && data['message'] is String)
          ? data['message'] as String
          : null;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            serverMsg ?? result.error ?? "Couldn't merge that item.",
            style: const TextStyle(fontSize: 12),
          ),
          backgroundColor: AppTheme.red,
          duration: const Duration(seconds: 2),
        ),
      );
      return;
    }

    // Merged — remove from the board (next home load reconciles), remember
    // the id so a stale stored briefing can't re-promote it, and tick the
    // digest down locally like the other focal actions.
    _deferredFocalKeys.add('graph_node:$pendingId');
    setState(() {
      _focalItems.removeWhere((c) => c.id == item.id);
    });
    if (_focalItems.isEmpty && mounted) {
      _addRhodeyResponse("Merged — that's tidied up in your knowledge graph.");
    }
    _decrementCountsFor(item);
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            '🔀 Merged into "$targetLabel"',
            style: const TextStyle(fontSize: 12),
          ),
          backgroundColor: AppTheme.green,
          duration: const Duration(seconds: 2),
        ),
      );
    }
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
            content: Text(
              "I couldn't reject that item — it may resurface.",
              style: TextStyle(fontSize: 12),
            ),
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
          content: Text(
            result.error ?? RhodeyVoice.couldntReject(),
            style: const TextStyle(fontSize: 12),
          ),
          backgroundColor: AppTheme.red,
          duration: const Duration(seconds: 2),
        ),
      );
      return;
    }
    if (!mounted) return;

    // Session-scoped memory: rejected items must not be re-promoted from a
    // stale stored briefing either.
    _deferredFocalKeys.add('${args.$1.toLowerCase()}:${args.$2}');
    setState(() {
      _focalItems.removeWhere((c) => c.id == item.id);
    });
    if (_focalItems.isEmpty && mounted) {
      _addRhodeyResponse(
        "Rejected — I'll leave that one off the board and adjust my judgment.",
      );
    }
    // N4: local count decrement instead of a 5-call refetch.
    _decrementCountsFor(item);
  }

  /// "Edit" on graph-edge cards — same bottom sheet as the Inbox: change the
  /// relationship and approve in one step (approve with the new relation).
  Future<void> _editFocalEdge(_FocalItem item) async {
    final parts = item.title.split(' → ');
    final currentRel = parts.length >= 2 ? parts[1] : '';
    final textController = TextEditingController(text: currentRel);

    final newRel = await showModalBottomSheet<String>(
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

    if (newRel == null || newRel.isEmpty) return;
    final rawId = item.metadata['focal_item_id'] ?? item.metadata['pending_id'];
    final pendingId = int.tryParse(rawId.toString());
    if (pendingId == null) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text(
              "I couldn't edit that edge — it may have changed.",
              style: TextStyle(fontSize: 12),
            ),
            backgroundColor: AppTheme.amber,
            duration: Duration(seconds: 2),
          ),
        );
      }
      return;
    }
    final result = await _api.approveGraphEdgeWithRelation(pendingId, newRel);
    if (_focalActionOk(result)) {
      setState(() {
        _focalItems.removeWhere((c) => c.id == item.id);
      });
      _decrementCountsFor(item);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(
              'Edge updated: $newRel',
              style: const TextStyle(fontSize: 12),
            ),
            backgroundColor: AppTheme.green,
            duration: const Duration(seconds: 2),
          ),
        );
      }
    } else if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            "I couldn't update that edge — it may have changed.",
            style: TextStyle(fontSize: 12),
          ),
          backgroundColor: AppTheme.amber,
          duration: Duration(seconds: 2),
        ),
      );
    }
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
            content: Text(
              RhodeyVoice.couldntDefer(),
              style: TextStyle(fontSize: 12),
            ),
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
            content: const Text(
              "I noted that, but couldn't defer the item — it may resurface.",
              style: TextStyle(fontSize: 12),
            ),
            backgroundColor: AppTheme.amber,
            duration: const Duration(seconds: 2),
          ),
        );
      }
      return;
    }

    // Session-scoped memory: corrected items must not be re-promoted from a
    // stale stored briefing either.
    _deferredFocalKeys.add('${args.$1.toLowerCase()}:${args.$2}');
    setState(() {
      _focalItems.removeWhere((c) => c.id == item.id);
    });
    if (_focalItems.isEmpty && mounted) {
      _addRhodeyResponse(
        "Got it — I've noted that wasn't the right priority. I'll adjust.",
      );
    }
    // N4: local count decrement instead of a 5-call refetch.
    _decrementCountsFor(item);
  }

  // ── Conversation ─────────────────────────────────────────────

  Future<void> _loadHistory({bool force = false}) async {
    if (_historyLoaded && !force) return;
    // First-ever load (pill tap / task-icon backfill) should land on the
    // newest message — subsequent force-refreshes keep the user's position.
    final isInitialBackfill = !_historyLoaded;
    _historyLoaded = true;

    // WhatsApp-style instant render: show the cached conversation page BEFORE
    // any network round-trip, so cold opens / notification taps never wait on
    // the server. The fetch below reconciles (appends anything newer).
    if (isInitialBackfill) {
      final cached = await _messageCache.load();
      if (cached.isNotEmpty && mounted) {
        _mergeHistoryRows(cached, idPrefix: 'c');
        _scrollToBottom();
      }
    }

    // Unified with the History screen: both read /api/conversation-history
    // (the merged user↔Rhodey thread) instead of the raw_dumps noise stream,
    // so home backfill and History tell the same story.
    final result = await _api.getConversationHistory(limit: 30);
    if (!mounted) return;

    if (result.success && result.data!.isNotEmpty) {
      _mergeHistoryRows(result.data!, idPrefix: 'h');
      // Write-behind: keep the on-device cache fresh for the next open.
      await _messageCache.save(result.data!);
      if (isInitialBackfill) _scrollToBottom();
    }
  }

  /// Merges API-shaped history rows (newest-first) into the chat in
  /// chronological order, deduping by server id. If a real Rhodey row matches
  /// the pending push-preview bubble, the preview is replaced (no duplicate).
  /// Returns true when at least one row was added.
  bool _mergeHistoryRows(
    List<Map<String, dynamic>> rows, {
    required String idPrefix,
    void Function()? onRhodey,
  }) {
    var addedAny = false;
    for (final m in rows.reversed) {
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

      if (role == MessageRole.rhodey) {
        onRhodey?.call();
        // The real server row landed — drop the instant push-preview if it
        // corresponds to this message (exact match, or one is a truncation).
        if (_pendingPushContent != null && _pushContentMatches(content)) {
          _removePendingPushPreview();
        }
      }

      final createdAt = m['created_at'] as String? ?? '';
      // Server timestamps are UTC (timestamptz) — convert to device-local so
      // the bubble clock and Today/date labels match the phone's clock.
      final ts = createdAt.isNotEmpty
          ? ((DateTime.tryParse(createdAt) ?? DateTime.now()).toLocal())
          : DateTime.now();
      MessageType msgType = MessageType.text;
      Map<String, dynamic>? suggestionBreakdown;
      
      final rawMsgType = m['message_type'] as String? ?? 'text';
      if (rawMsgType == 'suggestion') {
        msgType = MessageType.suggestion;
        final meta = m['metadata'];
        if (meta is Map<String, dynamic> && meta['suggestion_breakdown'] != null) {
          suggestionBreakdown = Map<String, dynamic>.from(meta['suggestion_breakdown']);
        }
      }

      _messages.add(
        ChatMessage(
          id: '$idPrefix${_msgCounter++}',
          role: role,
          type: msgType,
          text: content,
          timestamp: ts,
          intent: m['intent'] as String?,
          ackTitle: m['title'] as String?,
          sendStatus: role == MessageRole.user ? SendStatus.sent : null,
          suggestionBreakdown: suggestionBreakdown,
        ),
      );
      addedAny = true;
    }
    if (addedAny) {
      // Sort-safe merge: timestamps are the single source of truth for thread
      // order (ledger echoes, push previews, and history all coexist).
      _messages.sort((a, b) => a.timestamp.compareTo(b.timestamp));
      if (mounted) setState(() {});
    }
    return addedAny;
  }

  /// True when server [content] corresponds to the pending push-preview:
  /// exact match, or the server text is the full version of a truncated
  /// preview (and vice-versa). Short strings require an exact match to avoid
  /// collapsing unrelated one-liners.
  bool _pushContentMatches(String content) {
    final preview = _pendingPushContent;
    if (preview == null) return false;
    final a = content.trim();
    final b = preview.trim();
    if (a == b) return true;
    final short = a.length < b.length ? a : b;
    if (short.length < 40) return false;
    return a.startsWith(b) || b.startsWith(a);
  }

  /// Renders a Rhodey message instantly from the push payload (no network
  /// wait) — the provisional bubble replaced by the real row on history load.
  void _addPushPreviewBubble(String content) {
    if (!mounted) return;
    _removePendingPushPreview(); // never stack two previews
    final id = 'pv${++_msgCounter}';
    _pendingPushMessageId = id;
    _pendingPushContent = content;
    setState(() {
      _messages.add(
        ChatMessage(
          id: id,
          role: MessageRole.rhodey,
          text: content,
          timestamp: DateTime.now(),
        ),
      );
    });
    _scrollToBottom();
  }

  void _removePendingPushPreview() {
    final id = _pendingPushMessageId;
    if (id == null) return;
    _pendingPushMessageId = null;
    _pendingPushContent = null;
    setState(() {
      _messages.removeWhere((m) => m.id == id);
    });
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
      if (!_awaitingResponse) {
        _stopBackupPoll();
        return;
      }
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
    // API returns messages newest-first — merge chronological + dedupe via the
    // shared row merger (also reconciles the push-preview bubble if present).
    var replyArrived = false;
    final addedAny = _mergeHistoryRows(
      result.data!,
      idPrefix: 'p',
      onRhodey: () {
        replyArrived = true;
        // Replace the fast-ack "Processing..." bubble with the real reply.
        _removePendingAck();
      },
    );
    if (addedAny) {
      _scrollToBottom();
      // Keep the on-device cache warm: a reply that lands via push+poll must
      // be persisted too, or the next cold open renders a stale page (the
      // server fetch would fix it, but this avoids the gap entirely).
      await _messageCache.merge(result.data!);
    }
    // P3 fast-ack: once Rhodey's real reply lands in history, the awaited
    // response is complete — stop the backup poll.
    if (replyArrived && _awaitingResponse) {
      _stopBackupPoll();
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
    // Double-send guard: while a send is in flight, drop re-entry — a second
    // tap on the send button / keyboard submit must not POST the same message
    // twice (each POST spawns a Modal worker, so a double-send doubles task
    // creation + LLM spend server-side). The watchdog clears this on timeout.
    // Feedback instead of silence: a quick-reply chip or voice phrase tapped
    // during a slow inline send gets a hint, not a silent vanish.
    if (_sendInFlight) {
      ScaffoldMessenger.of(context)
        ..hideCurrentSnackBar()
        ..showSnackBar(
          const SnackBar(
            content: Text('Still processing — one message at a time.'),
            duration: Duration(seconds: 2),
          ),
        );
      return;
    }
    // Sending a message enters the conversation state (welcome hides).
    if (!_conversationOpen) setState(() => _conversationOpen = true);
    final id = 'u${++_msgCounter}';

    // Start as 'sent' immediately (optimistic) — avoids a pending spinner
    // flicker while the API call is in flight.
    final msg = ChatMessage(
      id: id,
      role: MessageRole.user,
      text: text.trim(),
      timestamp: DateTime.now(),
      sendStatus: SendStatus.sent,
    );

    setState(() {
      _messages.add(msg);
    });
    _textController.clear();
    _scrollToBottom();
    _startBackupPoll();

    // Timeout watchdog: the fast-ack API returns in ~1s; the inline fallback
    // (local dev / Modal spawn failure) runs the full LLM turn and can take
    // 30-60s. The watchdog extends its window while the request is still in
    // flight and only hard-fails a genuinely hung request (~70s total). In
    // fast-ack mode a soft "still working" note replaces the ack bubble and
    // the backup poll stays alive so the reply still lands via FCM push.
    _sendInFlight = true;
    _sendTimeout?.cancel();
    // A new send invalidates any prior fast-ack expiry watchdog — it must not
    // fire mid-send and kill the new message's poll.
    _ackExpiryTimer?.cancel();
    var watchTicks = 0;
    void watchdogTick() {
      if (!mounted) return;
      if (_sendInFlight && watchTicks < 2) {
        watchTicks++;
        _sendTimeout = Timer(const Duration(seconds: 25), watchdogTick);
        return;
      }
      _sendInFlight = false;
      if (_pendingAckId != null) {
        // Fast-ack path: the backend accepted the message; the reply is just
        // slow (Modal worker still running). Soften the ack and keep the
        // backup poll alive so the reply still lands when it arrives.
        _updateMessage(
          _pendingAckId!,
          text: "Still working on it — I'll ping you when it's ready.",
        );
        return;
      }
      _updateMessage(id, sendStatus: SendStatus.failed);
      _stopBackupPoll();
    }

    _sendTimeout = Timer(const Duration(seconds: 20), watchdogTick);

    _api.sendMessage(text.trim()).then((result) {
      _sendTimeout?.cancel();
      _sendInFlight = false;
      if (!mounted) return;
      if (result.success) {
        _updateMessage(id, sendStatus: SendStatus.resolved);
        final data = result.data is Map ? result.data as Map : null;
        final isFastAck = data?['fast_ack'] == true;
        final responseText =
            data?['response'] as String? ?? 'Got it. Processing...';
        if (isFastAck) {
          // P3 fast-ack: the backend returned instantly; the real reply is on
          // its way via FCM push + backup poll. Show the ack now (poll keeps
          // running — it stops itself when the reply lands in history).
          _addRhodeyAck(responseText);
          // Ack-expiry watchdog: if Rhodey's reply never lands (worker crash,
          // LLM outage), soften the ack and stop polling after 3 minutes
          // instead of leaving a dead "Processing..." bubble forever.
          _ackExpiryTimer?.cancel();
          _ackExpiryTimer = Timer(const Duration(seconds: 180), () {
            if (!mounted) return;
            _stopBackupPoll();
            if (_pendingAckId != null) {
              _updateMessage(
                _pendingAckId!,
                text: "Still working on it — I'll ping you when it's ready.",
              );
            }
          });
        } else {
          // Inline fallback (non-Modal): the reply is already in the response.
          _addRhodeyResponse(responseText);
        }
      } else {
        final errMsg = result.error ?? '';
        final isTimeout =
            errMsg.contains('TimeoutException') ||
            errMsg.toLowerCase().contains('timed out');
        if (isTimeout) {
          // A timeout is NOT proof the message failed — the backend may still
          // be processing (slow inline fallback / worker cold start) and the
          // real reply arrives via FCM push + backup poll. Never show the
          // destructive "Failed to send, Retry" here — a retry could create a
          // duplicate task. Keep the poll alive; the ack-expiry watchdog
          // softens the placeholder if nothing lands.
          _addRhodeyAck("Still working on it — I'll ping you when it's ready.");
          _ackExpiryTimer?.cancel();
          _ackExpiryTimer = Timer(const Duration(seconds: 180), () {
            if (!mounted) return;
            _stopBackupPoll();
            if (_pendingAckId != null) {
              _updateMessage(
                _pendingAckId!,
                text: "Still working on it — I'll ping you when it's ready.",
              );
            }
          });
          return;
        }
        _updateMessage(id, sendStatus: SendStatus.failed);
        // Stop the poll only if no prior fast-ack is still awaiting its reply;
        // otherwise keep that ack's poll + expiry backstop alive so it resolves
        // normally (a failed send must not orphan an earlier "Processing..."
        // bubble or kill its reply polling).
        if (_pendingAckId == null) {
          _stopBackupPoll();
          _ackExpiryTimer?.cancel();
        }
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Text(
                RhodeyVoice.failedToSend(),
                style: const TextStyle(fontSize: 12),
              ),
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
      // Only a text change can alter the card — status-only updates (ack
      // bubbles) keep the memoized card.
      if (text != null && text != _messages[idx].text) {
        _cardDataCache.remove(id);
      }
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
      _messages.add(
        ChatMessage(
          id: id,
          role: MessageRole.rhodey,
          text: text,
          timestamp: DateTime.now(),
        ),
      );
    });
    _scrollToBottom();
    _tts.speak(text);
  }

  /// P3 fast-ack acknowledgment: adds Rhodey's bubble WITHOUT stopping the
  /// backup poll and WITHOUT speaking — the real reply is on its way and the
  /// poll stops itself the moment it lands in conversation history.
  void _addRhodeyAck(String text) {
    _removePendingAck(); // never stack two "Processing..." bubbles
    final id = 'r${++_msgCounter}';
    _pendingAckId = id;
    setState(() {
      _messages.add(
        ChatMessage(
          id: id,
          role: MessageRole.rhodey,
          text: text,
          timestamp: DateTime.now(),
        ),
      );
    });
    _scrollToBottom();
  }

  /// Removes the pending fast-ack bubble once Rhodey's real reply lands in
  /// history (via the backup poll), so a turn never shows both the
  /// "Processing..." ack and the real reply.
  void _removePendingAck() {
    final ackId = _pendingAckId;
    if (ackId == null) return;
    _pendingAckId = null;
    _ackExpiryTimer?.cancel();
    setState(() {
      _messages.removeWhere((m) => m.id == ackId);
    });
  }

  // ── Task ledger (task icon → conversation) ────────────────

  /// M14: show the one-time home tour on first real entry (post-onboarding).
  /// Gate: only when the user is configured and has never seen it. Best-effort
  /// — any prefs failure just skips the tour.
  ///
  /// Replay (Settings > Help > "Show me around"): the Settings screen sets
  /// `home_tour_replay` before popping back here; didPopNext calls this and
  /// the flag overrides the seen-gate exactly once (consumed on read). The
  /// tour must run on this screen — its callouts anchor to the home layout.
  Future<void> _maybeShowHomeTour() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final replay = prefs.getBool('home_tour_replay') ?? false;
      if (replay) {
        await prefs.setBool('home_tour_replay', false); // consume
      }
      if (!replay && (prefs.getBool('home_tour_seen') ?? false)) return;
      if (!mounted) return;
      setState(() => _showHomeTour = true);
    } catch (_) {
      // Non-fatal — never block the home screen on the tour.
    }
  }

  Future<void> _dismissHomeTour() async {
    setState(() => _showHomeTour = false);
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool('home_tour_seen', true);
    } catch (_) {
      // Non-fatal.
    }
  }

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
    // Ledger shows open + committed tasks (committed = "I'll do it"), so a
    // committed task stays visible until it's truly done.
    final result = await _api.getTasks(status: 'todo,in_progress', includeSnoozed: true);
    if (!mounted) return;
    if (!result.success || result.data == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            "Couldn't load your board right now — check your connection.",
            style: TextStyle(fontSize: 12),
          ),
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
      _messages.add(
        ChatMessage(
          id: id,
          role: MessageRole.rhodey,
          type: MessageType.taskList,
          text: RhodeyVoice.taskLedgerIntro(active.length, snoozed.length),
          taskList: tasks,
          timestamp: DateTime.now(),
        ),
      );
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
      final result = await _api.vaultAction(
        action: 'pull_forward',
        taskId: taskId,
      );
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
          backgroundColor: wasSnoozed && !pulledForward
              ? AppTheme.amber
              : AppTheme.accent,
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
      final isCurrentFocus =
          !dim &&
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
                : (dim
                      ? AppTheme.surfaceAlt.withValues(alpha: 0.5)
                      : AppTheme.surfaceAlt),
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
                        color: dim
                            ? AppTheme.textTertiary
                            : AppTheme.textPrimary,
                      ),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    if (deadline != null && deadline.isNotEmpty)
                      Text(
                        dim
                            ? 'Snoozed · ${_formatDeadline(deadline)}'
                            : _formatDeadline(deadline),
                        style: AppTheme.monoCaption.copyWith(
                          color: dim
                              ? AppTheme.textTertiary
                              : AppTheme.textSecondary,
                        ),
                      ),
                  ],
                ),
              ),
              if (isCurrentFocus)
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 6,
                    vertical: 2,
                  ),
                  decoration: BoxDecoration(
                    color: AppTheme.accent.withValues(alpha: 0.12),
                    borderRadius: BorderRadius.circular(4),
                  ),
                  child: Text(
                    'in focus',
                    style: AppTheme.caption.copyWith(
                      color: AppTheme.accent,
                      fontSize: 8,
                    ),
                  ),
                )
              else if (dim)
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 6,
                    vertical: 2,
                  ),
                  decoration: BoxDecoration(
                    color: AppTheme.textTertiary.withValues(alpha: 0.1),
                    borderRadius: BorderRadius.circular(4),
                  ),
                  child: Text(
                    'snoozed',
                    style: AppTheme.caption.copyWith(
                      color: AppTheme.textTertiary,
                      fontSize: 8,
                    ),
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
            padding: const EdgeInsets.only(bottom: 4),
            child: Text(
              'Rhodey',
              style: AppTheme.caption.copyWith(
                color: AppTheme.accent,
                letterSpacing: 0.3,
              ),
            ),
          ),
          Padding(
            // Intro text sits on the same 32px baseline as bubble/card text
            // (16 gutter + 16 indent) — the ledger reads like a real message.
            padding: const EdgeInsets.only(left: 16, bottom: 8),
            child: Text(
              msg.text,
              style: AppTheme.body.copyWith(fontSize: 12.5, height: 1.35),
            ),
          ),
          if (active.isNotEmpty) ...active.map((t) => row(t)),
          if (snoozed.isNotEmpty) ...[
            const SizedBox(height: 4),
            ...snoozed.map((t) => row(t, dim: true)),
          ],
          const SizedBox(height: 4),
          Padding(
            padding: const EdgeInsets.only(left: 4),
            child: Text(
              RhodeyVoice.taskLedgerHint(),
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

  /// Home pull-to-refresh: reload the pulse intelligence and the focal board
  /// in ONE /api/home-feed call (P2) instead of six round-trips.
  Future<void> _refreshHome() async {
    await _loadHome();
  }

  /// Coalesces resume/pop-triggered refreshes into a single /api/home-feed
  /// call — a route pop and a foreground transition can fire within the same
  /// second, and each refresh is a full network round-trip. A 400ms window
  /// is imperceptible; the last scheduled refresh always wins.
  void _scheduleHomeRefresh() {
    _homeRefreshDebounce?.cancel();
    _homeRefreshDebounce = Timer(const Duration(milliseconds: 400), () {
      if (!mounted) return;
      _loadHome();
    });
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

  /// Capitalizes the first letter of a string — guards against backend
  /// voice lines that arrive lowercase (e.g. "good morning, danny.").
  String _capitalizeFirst(String s) {
    if (s.isEmpty) return s;
    return s[0].toUpperCase() + s.substring(1);
  }

  /// The idle greeting — Rhodey's voice line, time-prefixed, 1-2 lines.
  /// This is the app-intelligence summary: a greeting with judgment, never
  /// a status dump.
  Widget _buildWelcomeBanner() {
    final voice = _briefing.voiceLine?.trim() ?? '';
    final hour = DateTime.now().hour;
    final greeting = hour < 12
        ? 'Good Morning'
        : (hour < 17 ? 'Good Afternoon' : 'Good Evening');
    // M9.6: the tenant's own name once resolved. While unknown (first paint
    // before the resolve lands) omit it entirely — never flash 'Danny' at a
    // tenant who isn't Danny. 'Danny' remains only as the voice-line
    // last-resort fallback (RhodeyVoice._who), which fires post-resolution.
    final who = _userName.trim();
    final line = _capitalizeFirst(
      voice.isNotEmpty
          ? voice
          : (who.isNotEmpty
                ? "What's on your mind, $who?"
                : "What's on your mind?"),
    );

    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 18, 20, 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            who.isNotEmpty ? '$greeting, $who.' : '$greeting.',
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
                ? _FadeRise(
                    key: const ValueKey('all-clear-compact'),
                    child: _buildAllClearCompact(),
                  )
                : _FadeRise(
                    key: ValueKey('focal-${_focalItems.first.id}'),
                    child: _buildFocalCard(_focalItems.first, compact: true),
                  ),
          ),
          // Collapse back to idle
          Material(
            color: Colors.transparent,
            child: InkWell(
              borderRadius: BorderRadius.circular(10),
              onTap: () => setState(() => _conversationOpen = false),
              child: const Padding(
                padding: EdgeInsets.all(8),
                child: Icon(
                  Icons.keyboard_arrow_down,
                  color: AppTheme.textTertiary,
                  size: 22,
                ),
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
          decoration: const BoxDecoration(
            color: AppTheme.green,
            shape: BoxShape.circle,
          ),
        ),
        const SizedBox(width: 8),
        Text(
          RhodeyVoice.allClear(_userName, _voiceStyle),
          style: const TextStyle(
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
          setState(() {
            _voiceState = VoiceState.idle;
            _voiceError = null;
          });
          return;
        }
        setState(() {
          _voiceState = VoiceState.error;
          _voiceError = error.errorMsg;
        });
      },
      onStatus: (status) {
        debugPrint('[Voice] Status: $status');
      },
    );
    if (!_speechAvailable && mounted) {
      setState(() {
        _voiceState = VoiceState.error;
        _voiceError = 'Speech recognition not available';
      });
    }
  }

  void _toggleVoice() {
    if (_voiceState == VoiceState.idle) {
      _startListening();
    } else if (_voiceState == VoiceState.listening) {
      _stopListening();
    } else {
      setState(() {
        _voiceState = VoiceState.idle;
        _transcribedText = null;
        _voiceError = null;
      });
    }
  }

  Future<void> _startListening() async {
    _voiceError = null;
    if (!_speechAvailable) {
      _speechAvailable = await _speech.initialize();
      if (!_speechAvailable) {
        if (mounted) {
          setState(() {
            _voiceState = VoiceState.error;
            _voiceError = 'Speech recognition not available';
          });
        }
        return;
      }
    }
    setState(() => _voiceState = VoiceState.listening);
    await _speech.listen(
      onResult: (result) {
        if (!mounted) return;
        final text = result.recognizedWords;
        if (text.isNotEmpty) {
          _transcribedText = text;
        }
        if (result.finalResult) {
          final words = result.recognizedWords;
          if (words.isNotEmpty) {
            _sendMessage(words);
          }
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
    setState(() {
      _voiceState = VoiceState.idle;
      _transcribedText = null;
    });
  }

  void _retryVoice() {
    setState(() {
      _voiceState = VoiceState.idle;
      _transcribedText = null;
    });
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

  void _openInbox([Map<String, dynamic>? pushData]) {
    _instrumentation.inboxBadgeTaps++;
    // The Inbox is always pushed as a route above home, so RouteAware's
    // didPopNext() fires on return and refreshes the live pending count +
    // focal board — no explicit refresh needed here (avoids double fetch).
    // When opened from a decision/delegation push, pass the payload so the
    // pushed summary renders instantly (WhatsApp-style) while the list loads.
    Navigator.push(
      context,
      MaterialPageRoute(builder: (_) => InboxScreen(pushData: pushData)),
    );
  }

  void _handlePushNotificationTap(Map<String, dynamic> data) {
    final type = data['type'];
    debugPrint('[PushNav] Notification type=$type data=$data');
    switch (type) {
      case 'decision':
      case 'delegation':
        _openInbox(data);
        break;
      case 'nudge':
        // Pass the nudge payload so the meeting content renders instantly on
        // the Today screen while its calendar/tasks fetch in the background.
        Navigator.push(
          context,
          MaterialPageRoute(builder: (_) => TodayScreen(pushData: data)),
        );
        break;
      case 'briefing':
        // Mirror Telegram: tapping the pulse notification opens the
        // conversation, where the full briefing lives as a BRIEFING card.
        // WhatsApp-style: the push now carries the text — render it instantly
        // so content is on screen the moment the app opens, then the history
        // load replaces the preview with the real row (no spinner wait).
        if (!_conversationOpen) setState(() => _conversationOpen = true);
        final pushContent = (data['content'] as String?)?.trim() ?? '';
        if (pushContent.isNotEmpty) {
          _addPushPreviewBubble(pushContent);
        }
        // Force the history load and always land on the newest message — a
        // second tap in the same session must still show the fresh briefing
        // even if the user had scrolled up.
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
      case 'morning':
        return 'MORNING';
      case 'afternoon':
        return 'AFTERNOON';
      case 'closing_loop':
        return 'CLOSING';
      case 'weekend':
        return 'WEEKEND';
      case 'pre_monday':
        return 'PRE-MONDAY';
      case 'intel':
        return 'INTEL';
      default:
        return '';
    }
  }

  Color _pulseColor() {
    final mode = _briefing.pulseMode ?? '';
    switch (mode.toLowerCase()) {
      case 'morning':
        return AppTheme.accent;
      case 'afternoon':
        return AppTheme.amber;
      case 'closing_loop':
        return AppTheme.red;
      case 'intel':
        return AppTheme.textTertiary;
      default:
        return AppTheme.accent;
    }
  }

  // ═══════════════════════════════════════════════════════════
  // BUILD
  // ═══════════════════════════════════════════════════════════

  @override
  Widget build(BuildContext context) {
    return Stack(
      children: [
        Scaffold(
          backgroundColor: AppTheme.background,
          body: SafeArea(
            child: Column(
              children: [
                // Repaint boundaries isolate the header (syncing chip animation)
                // and the content area (chat scroll + focal entrance motion) so a
                // repaint in one never repaints the other.
                RepaintBoundary(child: _buildAmbientHeader()),
                Expanded(
                  child: RepaintBoundary(
                    child: AnimatedSwitcher(
                      duration: const Duration(milliseconds: 350),
                      switchInCurve: Curves.easeOut,
                      switchOutCurve: Curves.easeIn,
                      child: _conversationOpen
                          ? _buildConversationState()
                          : _buildIdleState(),
                    ),
                  ),
                ),
                if (_voiceState == VoiceState.listening ||
                    _voiceState == VoiceState.error)
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
        ),
        // M14: one-time first-run tour — paged arrow callouts for the header,
        // the tiny brief, the focal card, and the input bar.
        if (_showHomeTour)
          HomeTourOverlay(
            onDismiss: _dismissHomeTour,
          ),
      ],
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
                child: const Icon(
                  Icons.menu,
                  color: AppTheme.textSecondary,
                  size: 22,
                ),
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
          // Expanded (tight fit) so the label fills all free space and the
          // task pill stays pinned to the right edge no matter how long the
          // pulse label is — a loose Flexible left dead space after the text
          // and the pill floated mid-row as the label grew.
          Expanded(
            child: Text(
              'Rhodey${pulseLabel.isNotEmpty ? ' · $pulseLabel' : ''}',
              style: AppTheme.body.copyWith(
                fontWeight: FontWeight.w600,
                fontSize: 13,
                letterSpacing: 0.3,
              ),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),

          // Subtle background-sync chip — visible only while the live
          // reconcile runs after the cached instant render.
          if (_isSyncing) ...[
            const SizedBox(width: 8),
            SizedBox(
              width: 10,
              height: 10,
              child: CircularProgressIndicator(
                strokeWidth: 1.4,
                color: AppTheme.textTertiary,
              ),
            ),
            const SizedBox(width: 4),
            Text(
              'syncing',
              style: AppTheme.monoCaption.copyWith(
                color: AppTheme.textTertiary,
                fontSize: 9,
              ),
            ),
          ],

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
                    const Icon(
                      Icons.checklist_rtl,
                      size: 12,
                      color: AppTheme.textSecondary,
                    ),
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
        child: const Center(
          child: SizedBox(
            width: 14,
            height: 14,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              color: AppTheme.textTertiary,
            ),
          ),
        ),
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
              child: _FadeRise(
                key: ValueKey('focal-${_focalItems.first.id}'),
                child: _buildFocalCard(_focalItems.first),
              ),
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
          if (_briefing.transparencyReport != null &&
              _briefing.transparencyReport!.isNotEmpty)
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
          color: isCompleting
              ? AppTheme.green.withValues(alpha: 0.08)
              : AppTheme.accentBg,
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
            if (!compact &&
                focalReason != null &&
                focalReason.isNotEmpty &&
                !isCompleting)
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
            if (!compact &&
                item.subtitle != null &&
                item.subtitle!.isNotEmpty &&
                focalReason == null)
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
            if (_briefing.nextEvent != null &&
                _briefing.nextEvent!.isNotEmpty &&
                !isFromBriefing)
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

            // Dynamic per-type actions — the button SET mirrors the Inbox's
            // actions for that item's type (one vocabulary per surface):
            //   task    → I'll do it · Not now · Not right
            //   node    → Approve · Not now · Reject
            //   edge    → Approve · Edit · Reject
            //   merge   → Accept · Not now · Reject
            //   channel → Approve · Not right (2 buttons)
            Row(
              children: _buildFocalActions(item),
            ),

            // Merge into existing — person (graph_node) cards only. A quiet
            // secondary action below the buttons, mirroring the Inbox's merge.
            if (!compact && _isFocalPerson(item) && !isCompleting)
              Padding(
                padding: const EdgeInsets.only(top: 10),
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: GestureDetector(
                    onTap: () => _openFocalMerge(item),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        const Icon(
                          Icons.call_merge,
                          size: 13,
                          color: AppTheme.accent,
                        ),
                        const SizedBox(width: 4),
                        Text(
                          'Merge into existing',
                          style: AppTheme.caption.copyWith(
                            color: AppTheme.accent,
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
          const Icon(
            Icons.check_circle_outline,
            size: 14,
            color: AppTheme.green,
          ),
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
        borderRadius: BorderRadius.circular(AppTheme.cardRadius),
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
                  _openFullReport(report);
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

  /// Opens the full weekly learning report in a themed, scrollable sheet —
  /// the affordance opens the document instead of silently dropping it into
  /// a hidden conversation (and reading it aloud).
  void _openFullReport(String report) {
    final lines = report.split('\n').where((l) => l.trim().isNotEmpty).toList();
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: Colors.transparent,
      isScrollControlled: true,
      builder: (sheetContext) => Container(
        constraints: BoxConstraints(
          maxHeight: MediaQuery.of(sheetContext).size.height * 0.75,
        ),
        margin: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: AppTheme.surface,
          borderRadius: BorderRadius.circular(20),
          border: Border.all(color: AppTheme.border),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.max,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 14, 8, 8),
              child: Row(
                children: [
                  const Text('🧠', style: TextStyle(fontSize: 16)),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'What I Learned This Week',
                      style: AppTheme.label.copyWith(
                        fontSize: 12,
                        letterSpacing: 0.5,
                        color: AppTheme.textPrimary,
                      ),
                    ),
                  ),
                  IconButton(
                    icon: const Icon(
                      Icons.close,
                      size: 18,
                      color: AppTheme.textTertiary,
                    ),
                    onPressed: () => Navigator.pop(sheetContext),
                  ),
                ],
              ),
            ),
            const Divider(color: AppTheme.border, height: 1),
            Flexible(
              child: SingleChildScrollView(
                padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    for (final line in lines)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 6),
                        child: Text(
                          line,
                          style: AppTheme.body.copyWith(
                            color: AppTheme.textPrimary,
                            fontSize: 13,
                            height: 1.45,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ),
          ],
        ),
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
            decoration: const BoxDecoration(
              color: AppTheme.green,
              shape: BoxShape.circle,
            ),
          ),
          const SizedBox(width: 8),
          Flexible(
            child: Text(
              RhodeyVoice.allClearPrompt(_userName, _voiceStyle),
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

    final now = DateTime.now();
    return RefreshIndicator(
      onRefresh: () => _loadHistory(force: true),
      color: AppTheme.accent,
      // reverse:true pins offset 0 = the BOTTOM = the newest message. The task
      // ledger (posted at the end) is therefore visible BY CONSTRUCTION when
      // the conversation opens — no scroll race with the history backfill.
      child: ListView.builder(
        reverse: true,
        controller: _scrollController,
        padding: const EdgeInsets.only(top: 4, bottom: 8),
        // Lazy list: only visible rows are built (previously every message was
        // materialized + regex-parsed on every rebuild). Each message occupies
        // two slots — [content, gap] — followed by the optional "Show earlier
        // activity" footer and a top spacer (later slots render at the very
        // top because the list is reversed).
        itemCount: _messages.length * 2 + (_historyLoaded ? 0 : 1) + 1,
        itemBuilder: (context, index) {
          // Slot layout (index 0 = bottom = newest message):
          //   even indices → message content · odd indices → 10px gap
          //   then "Show earlier activity" (if not loaded), then a spacer.
          final msgSlots = _messages.length * 2;
          if (index < msgSlots) {
            if (index.isOdd) return const SizedBox(height: 10);
            final msgIndex = index ~/ 2;
            final msg = _messages[_messages.length - 1 - msgIndex];

            // The newest live message fades/rises in on arrival (older
            // history renders statically — calm on open).
            final isNewestLive =
                msgIndex == 0 &&
                msg.timestamp.isAfter(now.subtract(const Duration(minutes: 5)));

            // Task ledger (from the task icon) — rendered as an actionable list
            Widget? content;
            var holdsState = false;
            if (msg.type == MessageType.taskList && msg.taskList != null) {
              content = _buildTaskListMessage(msg);
              holdsState = true;
            } else if ((msg.type == MessageType.documentReview && msg.documentBreakdown != null) ||
                       (msg.type == MessageType.suggestion && msg.suggestionBreakdown != null)) {
              // Document review / Message suggestion card — structured breakdown with checkboxes
              content = _buildSuggestionCard(msg);
              holdsState = true;
            } else if (msg.role == MessageRole.rhodey && !msg.isRhodeyTyping) {
              // Memoized: resolveCardData runs once per message, not per rebuild.
              final cardData = _cachedCardData(msg);
              if (cardData != null) {
                content = _buildRichCard(msg, cardData);
                holdsState = true; // briefing expand/collapse lives in the card
              }
            }
            if (content == null) {
              final isGroupStart =
                  msgIndex == 0 ||
                  _messages[_messages.length - msgIndex].role != msg.role;
              content = ChatBubble(
                message: msg,
                isGroupStart: isGroupStart,
                onRetry: msg.isFailed ? () => _retryMessage(msg.id) : null,
                onTap: !msg.isUser && msg.text.isNotEmpty
                    ? () => _speakMessage(msg.text)
                    : null,
                onQuickReply: (reply) => _sendMessage(reply),
              );
            }
            // Keyed shell: EVERY message gets a stable key so per-message
            // state survives list churn. Without it, insertions recycle card
            // states by position — the newest briefing's collapse would
            // silently reset on the next poll, making its arrow feel dead.
            // Stateful rows (cards/ledgers) stay mounted on scroll-off; plain
            // bubbles dispose freely. The newest live message fades/rises in
            // on arrival (older history renders statically — calm on open).
            return _FadeRise(
              key: ValueKey('msg-${msg.id}'),
              animate: isNewestLive,
              keepAlive: holdsState,
              child: content,
            );
          }

          // 'Show earlier activity' renders at the very TOP (reverse:true
          // flips the axis). Hidden once loaded.
          if (index == msgSlots && !_historyLoaded) {
            return GestureDetector(
              onTap: () => _loadHistory(),
              child: Container(
                margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                padding: const EdgeInsets.symmetric(
                  horizontal: 14,
                  vertical: 8,
                ),
                decoration: BoxDecoration(
                  color: AppTheme.surfaceAlt,
                  borderRadius: BorderRadius.circular(20),
                  border: Border.all(color: AppTheme.border, width: 1),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(
                      Icons.history,
                      size: 14,
                      color: AppTheme.textTertiary,
                    ),
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
            );
          }
          return const SizedBox(height: 16);
        },
      ),
    );
  }

  Widget _buildEmptyConversation() {
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 0, end: 1),
      duration: AppTheme.motionBase,
      curve: AppTheme.motionCurve,
      builder: (context, t, child) => Opacity(opacity: t, child: child),
      child: ListView(
        controller: _scrollController,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          const SizedBox(height: 16),
          const Center(
            child: Icon(
              Icons.chat_bubble_outline,
              size: 28,
              color: AppTheme.textMuted,
            ),
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
      ),
    );
  }

  /// Resolves a task id from a card title WITHOUT a per-tap network call.
  ///
  /// The title→id index is populated by _applyFocalData from the tasks
  /// already fetched for the focal board; a single lazy fetch backfills it if
  /// that list was empty (focal load failed). Matching is fail-closed: an
  /// exact normalized match wins; a containment match is only accepted when
  /// exactly one task qualifies — never an arbitrary pick, so a "Fix login
  /// bug" card can't complete "Fix login bug on Android". Null → the caller
  /// shows the existing "not found" snackbar.
  Future<int?> _taskIdForTitle(String title) async {
    if (_taskIdByTitle.isEmpty && !_taskIndexBackfilled) {
      // Include committed (in_progress) tasks so "Mark done" on a card
      // still resolves a task the user has already taken on.
      final result = await _api.getTasks(status: 'todo,in_progress');
      if (result.success && result.data != null) {
        // Only mark backfilled on success — a transient failure retries on
        // the next tap instead of silently disabling lookup for the session.
        _taskIndexBackfilled = true;
        for (final t in result.data!) {
          final id = t['id'] as int?;
          final taskTitle = (t['title'] as String? ?? '').toLowerCase().trim();
          if (id != null && taskTitle.isNotEmpty) {
            // putIfAbsent: first match wins for duplicate titles (same
            // semantics as the old first-match loop).
            _taskIdByTitle.putIfAbsent(taskTitle, () => id);
          }
        }
      }
    }
    final lower = title.toLowerCase().trim();
    if (lower.isEmpty) return null;
    final exact = _taskIdByTitle[lower];
    if (exact != null) return exact;
    final candidates = <int>{};
    for (final e in _taskIdByTitle.entries) {
      if (e.key.contains(lower) || lower.contains(e.key)) {
        candidates.add(e.value);
      }
    }
    return candidates.length == 1 ? candidates.first : null;
  }

  /// resolveCardData, memoized by message id — the regex parser runs at most
  /// once per message instead of on every rebuild.
  CardData? _cachedCardData(ChatMessage msg) {
    if (!_cardDataCache.containsKey(msg.id)) {
      _cardDataCache[msg.id] = resolveCardData(
        intent: msg.intent,
        text: msg.text,
        title: msg.ackTitle,
        timestamp: msg.timestamp,
      );
    }
    return _cardDataCache[msg.id];
  }

  Widget _buildRichCard(ChatMessage msg, CardData cardData) {
    VoidCallback? onMarkDone;
    if (cardData.type == CardType.task) {
      onMarkDone = () async {
        _instrumentation.homeActionsCompleted++;
        final taskId = await _taskIdForTitle(cardData.title);
        if (taskId != null && mounted) {
          await _api.updateTaskStatus(taskId, 'done');
        } else if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text(
                'Task already completed or not found.',
                style: TextStyle(fontSize: 12),
              ),
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
            child: Text(
              'Rhodey',
              style: AppTheme.caption.copyWith(
                color: AppTheme.accent,
                letterSpacing: 0.3,
              ),
            ),
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

  Widget _buildSuggestionCard(ChatMessage msg) {
    final breakdown = msg.suggestionBreakdown ?? msg.documentBreakdown!;
    final isDoc = msg.type == MessageType.documentReview;
    final sourceId = breakdown['document_id'] ?? breakdown['message_id'] ?? msg.id;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.only(left: 16, bottom: 4),
            child: Text(
              'Rhodey',
              style: AppTheme.caption.copyWith(
                color: AppTheme.accent,
                letterSpacing: 0.3,
              ),
            ),
          ),
          SuggestionCard(
            breakdown: breakdown,
            sourceType: isDoc ? 'document' : 'message',
            sourceId: sourceId,
            filename: breakdown['filename'],
            onConfirm: (selectedTasks, selectedEntities) async {
              if (sourceId == null) return;
              final result = await _api.confirmSuggestions(
                isDoc ? 'document' : 'message',
                sourceId,
                selectedTasks,
                selectedEntities,
              );
              // Optimistic removal to avoid stuck card
              setState(() {
                _messages.removeWhere((m) => m.id == msg.id);
              });
              if (result.success && mounted) {
                final count = result.data['count'] ?? 0;
                final confirmMsg = ChatMessage(
                  id: 'doc-confirm-${DateTime.now().millisecondsSinceEpoch}',
                  role: MessageRole.rhodey,
                  text: '✅ Created $count ${count == 1 ? 'item' : 'items'}.',
                  timestamp: DateTime.now(),
                );
                setState(() {
                  _messages.add(confirmMsg);
                });
                _scrollToBottom();
              }
            },
            onSkip: () {
              // Just remove the card
              setState(() {
                _messages.removeWhere((m) => m.id == msg.id);
              });
            },
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

  // ── Attachment picker ──────────────────────────────────────────────

  void _showAttachmentSheet() {
    showModalBottomSheet(
      context: context,
      backgroundColor: AppTheme.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (ctx) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 36, height: 4,
                decoration: BoxDecoration(
                  color: AppTheme.border,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              const SizedBox(height: 16),
              Text(
                'Add attachment',
                style: AppTheme.bodySmall.copyWith(
                  fontWeight: FontWeight.w500,
                ),
              ),
              const SizedBox(height: 16),
              ListTile(
                leading: Icon(Icons.photo_outlined, color: AppTheme.textSecondary, size: 22),
                title: Text('Gallery', style: AppTheme.body.copyWith(fontSize: 14)),
                onTap: () {
                  Navigator.pop(ctx);
                  _pickAndUploadDocument(source: 'gallery');
                },
              ),
              ListTile(
                leading: Icon(Icons.description_outlined, color: AppTheme.textSecondary, size: 22),
                title: Text('Document (PDF, DOCX, TXT)', style: AppTheme.body.copyWith(fontSize: 14)),
                onTap: () {
                  Navigator.pop(ctx);
                  _pickAndUploadDocument(source: 'document');
                },
              ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _pickAndUploadDocument({required String source}) async {
    String? filePath;
    String? fileName;

    try {
      if (source == 'gallery') {
        final picker = ImagePicker();
        final file = await picker.pickImage(source: ImageSource.gallery);
        if (file == null) return; // User cancelled
        filePath = file.path;
        fileName = file.name;
      } else {
        final result = await FilePicker.platform.pickFiles(
          type: FileType.custom,
          allowedExtensions: ['pdf', 'docx', 'txt', 'jpg', 'jpeg', 'png'],
        );
        if (result == null || result.files.single.path == null) return; // User cancelled
        filePath = result.files.single.path!;
        fileName = result.files.single.name;
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text('Failed to pick file: $e'),
            backgroundColor: AppTheme.red,
            duration: const Duration(seconds: 2),
          ),
        );
      }
      return;
    }

    if (!mounted) return;
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppTheme.surface,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
        title: Text('Upload $fileName?', style: const TextStyle(fontSize: 16, color: AppTheme.textPrimary)),
        content: const Text(
          'This will be sent to Rhodey for analysis.',
          style: TextStyle(fontSize: 13, color: AppTheme.textSecondary),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Upload', style: TextStyle(color: AppTheme.accent)),
          ),
        ],
      ),
    );
    if (confirm != true) return;

    // Show user message
    final msgId = 'upload-${DateTime.now().millisecondsSinceEpoch}';
    final userMsg = ChatMessage(
      id: msgId,
      role: MessageRole.user,
      text: '📎 $fileName',
      timestamp: DateTime.now(),
      sendStatus: SendStatus.sending,
    );
    setState(() {
      _messages.add(userMsg);
    });
    _scrollToBottom();

    // Show typing indicator
    final typingId = 'typing-${DateTime.now().millisecondsSinceEpoch}';
    final typingMsg = ChatMessage(
      id: typingId,
      role: MessageRole.rhodey,
      text: '...',
      timestamp: DateTime.now(),
    );
    setState(() {
      _messages.add(typingMsg);
    });
    _scrollToBottom();

    // Upload to API
    final result = await _api.sendMultimodal(filePath, fieldName: 'file');
    if (!mounted) return;

    // Remove typing indicator
    setState(() {
      _messages.removeWhere((m) => m.id == typingId);
    });

    // Mark user message as sent
    final userMsgIdx = _messages.indexWhere((m) => m.id == msgId);
    if (userMsgIdx != -1) {
      setState(() {
        _messages[userMsgIdx] = ChatMessage(
          id: msgId,
          role: MessageRole.user,
          text: '📎 $fileName',
          timestamp: DateTime.now(),
          sendStatus: SendStatus.sent,
        );
      });
    }

    if (result.success && result.data is Map) {
      final data = result.data as Map<String, dynamic>;
      final breakdown = data['document_breakdown'];

      if (breakdown != null) {
        // Document Intelligence: show breakdown card
        final reviewMsg = ChatMessage(
          id: 'doc-review-${DateTime.now().millisecondsSinceEpoch}',
          role: MessageRole.rhodey,
          text: '📄 ${breakdown['summary'] ?? 'Document processed'}',
          timestamp: DateTime.now(),
          type: MessageType.documentReview,
          documentBreakdown: Map<String, dynamic>.from(breakdown),
        );
        setState(() {
          _messages.add(reviewMsg);
        });
        _scrollToBottom();
      } else {
        // Classic flow: show response text
        final responseText = data['response'] as String? ?? 'Got it.';
        final rhodeyMsg = ChatMessage(
          id: 'doc-response-${DateTime.now().millisecondsSinceEpoch}',
          role: MessageRole.rhodey,
          text: responseText,
          timestamp: DateTime.now(),
        );
        setState(() {
          _messages.add(rhodeyMsg);
        });
        _scrollToBottom();
      }
    } else {
      // Error
      final errorMsg = ChatMessage(
        id: 'doc-error-${DateTime.now().millisecondsSinceEpoch}',
        role: MessageRole.rhodey,
        text: '⚠️ ${result.error ?? 'Upload failed. Try again.'}',
        timestamp: DateTime.now(),
      );
      setState(() {
        _messages.add(errorMsg);
      });
      _scrollToBottom();
    }
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
              // Attachment button
              Material(
                color: Colors.transparent,
                child: InkWell(
                  borderRadius: BorderRadius.circular(20),
                  onTap: _showAttachmentSheet,
                  child: Container(
                    padding: const EdgeInsets.all(8),
                    child: Icon(
                      Icons.attach_file,
                      color: AppTheme.textTertiary,
                      size: 20,
                    ),
                  ),
                ),
              ),

              const SizedBox(width: 4),

              // Voice button
              Material(
                color: Colors.transparent,
                child: InkWell(
                  borderRadius: BorderRadius.circular(20),
                  onTap: _toggleVoice,
                  child: Container(
                    padding: const EdgeInsets.all(8),
                    child: Icon(
                      _voiceState == VoiceState.listening
                          ? Icons.mic
                          : Icons.mic_none,
                      color: _voiceState == VoiceState.listening
                          ? AppTheme.accent
                          : AppTheme.textTertiary,
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
                    child: const Icon(
                      Icons.arrow_upward,
                      color: AppTheme.accent,
                      size: 20,
                    ),
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
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
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

// ── Warm entrance ──────────────────────────────────────────────
//
// The motion language for anything Rhodey "hands" you: a soft fade + gentle
// 10px rise on easeOutCubic, never a bounce. Key it (per message, per focal
// item) so a swap replays it while ordinary rebuilds do not.

class _FadeRise extends StatefulWidget {
  final Widget child;

  /// When false (older history), the child renders statically — the tween
  /// starts at its end value so nothing animates. The stable key is what
  /// matters: it keeps per-message state (briefing expand/collapse) alive
  /// across list churn.
  final bool animate;

  /// Whether the row must stay mounted when scrolled off a lazy list. Only
  /// rows holding state (rich cards, task ledgers) need this — plain bubbles
  /// are stateless and can be disposed freely. No-op outside a sliver (e.g.
  /// the focal board's Column).
  final bool keepAlive;

  const _FadeRise({
    super.key,
    required this.child,
    this.animate = true,
    this.keepAlive = true,
  });

  @override
  State<_FadeRise> createState() => _FadeRiseState();
}

class _FadeRiseState extends State<_FadeRise>
    with AutomaticKeepAliveClientMixin {
  @override
  bool get wantKeepAlive => widget.keepAlive;

  @override
  Widget build(BuildContext context) {
    super.build(context);
    final child = widget.child;
    if (!widget.animate) return child;
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 0, end: 1),
      duration: AppTheme.motionBase,
      curve: AppTheme.motionCurve,
      builder: (context, t, child) => Opacity(
        opacity: t,
        child: Transform.translate(
          offset: Offset(0, 10 * (1 - t)),
          child: child,
        ),
      ),
      child: child,
    );
  }
}
