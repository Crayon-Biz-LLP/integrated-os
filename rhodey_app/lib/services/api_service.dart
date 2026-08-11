import 'dart:convert';
import 'dart:math' show Random;
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'api_config.dart';
import 'home_feed_cache.dart';
import '../models/briefing.dart';
import '../models/persona_summary.dart';

// ── Types ──────────────────────────────────────────────────────

/// Result of an API call.
class ApiResult<T> {
  final bool success;
  final T? data;
  final String? error;

  const ApiResult({required this.success, this.data, this.error});

  static ApiResult<T> ok<T>(T data) => ApiResult(success: true, data: data);
  static ApiResult<T> fail<T>(String error) =>
      ApiResult(success: false, error: error);
}

/// A pending decision item from any source channel.
class PendingDecision {
  final String id;
  final String
  source; // 'email', 'call', 'whatsapp', 'graph_node', 'graph_edge'
  final String title;
  final String? description;
  final double? confidence;
  final Map<String, dynamic> raw;

  const PendingDecision({
    required this.id,
    required this.source,
    required this.title,
    this.description,
    this.confidence,
    required this.raw,
  });
}

/// Result of the single-call home feed (/api/home-feed).
/// Bundles briefing + pending decisions + active tasks + the persona
/// surface summary so the home screen opens with ONE network round-trip.
class HomeFeed {
  final BriefingResponse briefing;
  final List<PendingDecision> decisions;
  final List<Map<String, dynamic>> tasks;
  final PersonaSummary persona;

  const HomeFeed({
    required this.briefing,
    required this.decisions,
    required this.tasks,
    this.persona = PersonaSummary.empty,
  });

  bool get isEmpty =>
      briefing.greeting.isEmpty && decisions.isEmpty && tasks.isEmpty;
}

/// Simplified calendar event.
class CalendarEventItem {
  final String id;
  final String title;
  final String timeRange;
  final bool isActive;

  const CalendarEventItem({
    required this.id,
    required this.title,
    required this.timeRange,
    this.isActive = false,
  });
}

/// Collapsed Inbox payload from GET /api/inbox — pending decisions across
/// all sources + the unverified auto-decision count in one round-trip.
class InboxBundle {
  final List<PendingDecision> decisions;
  final int autoDecisionCount;

  /// Pending email reply drafts (email_drafts status='pending').
  final List<Map<String, dynamic>> drafts;

  /// Undecided FYI items — informational, dismissed with ack.
  final List<Map<String, dynamic>> fyi;

  const InboxBundle({
    required this.decisions,
    required this.autoDecisionCount,
    this.drafts = const [],
    this.fyi = const [],
  });
}

/// Lifecycle-aware API client.
///
/// Every outbound request:
///   1. Attaches X-API-Key header
///   2. Attaches X-Idempotency-Key for mutation endpoints
///   3. Retries on 429/503 with jittered backoff (up to 2 retries)
///   4. Reports structured errors — never throws
class ApiService {
  static final ApiService _instance = ApiService._();
  factory ApiService() => _instance;
  ApiService._();

  final _config = ApiConfig();
  final _client = http.Client();
  final _homeFeedCache = HomeFeedCache();
  int _idCounter = 0;

  /// Call once at app startup to load persisted config.
  Future<void> init() => _config.load();

  // ── Low-level HTTP helpers ───────────────────────────────────

  Map<String, String> _headers({bool idempotent = false}) {
    final h = <String, String>{
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    };
    if (_config.apiKey.isNotEmpty) {
      h['X-API-Key'] = _config.apiKey;
    }
    if (idempotent) {
      h['X-Idempotency-Key'] = _idempotencyKey();
    }
    return h;
  }

  Uri _uri(String path) => Uri.parse('${_config.baseUrl}$path');

  String _idempotencyKey() {
    _idCounter++;
    final r = Random.secure();
    final bytes = List<int>.generate(12, (_) => r.nextInt(256));
    return '${DateTime.now().millisecondsSinceEpoch}-$_idCounter-${base64Url.encode(bytes)}';
  }

  /// GET with retry.
  /// Public so that screens (e.g. Inbox) can fetch arbitrary endpoints.
  /// [timeout] defaults to 10s; heavier aggregate endpoints (home-feed,
  /// briefing) pass a longer window since they bundle multiple queries.
  Future<ApiResult<dynamic>> get(
    String path, {
    Map<String, String>? query,
    Duration timeout = const Duration(seconds: 10),
  }) async {
    final uri = query != null
        ? _uri(path).replace(queryParameters: query)
        : _uri(path);
    for (var attempt = 0; attempt < 3; attempt++) {
      try {
        final resp = await _client
            .get(uri, headers: _headers())
            .timeout(timeout);
        if (resp.statusCode == 200 || resp.statusCode == 201) {
          return ApiResult.ok(jsonDecode(resp.body));
        }
        if (resp.statusCode == 429 || resp.statusCode >= 500) {
          // Retryable
          await Future.delayed(
            Duration(milliseconds: 200 * (attempt + 1) + Random().nextInt(200)),
          );
          continue;
        }
        return ApiResult.fail('${resp.statusCode}: ${resp.body}');
      } catch (e) {
        if (attempt < 2) {
          await Future.delayed(
            Duration(milliseconds: 200 * (attempt + 1) + Random().nextInt(200)),
          );
          continue;
        }
        return ApiResult.fail('$e');
      }
    }
    return ApiResult.fail('Max retries exceeded');
  }

  /// POST with idempotency key and optional retry.
  /// Public so other services (e.g., NotificationService) can call it.
  /// [maxRetries] controls how many times to retry on timeout/server errors.
  /// Set to 0 for mutation endpoints (send-message) to prevent duplicate processing.
  Future<ApiResult<dynamic>> post(
    String path, {
    Map<String, dynamic>? body,
    Duration timeout = const Duration(seconds: 10),
    int maxRetries = 3,
  }) async {
    final attempts = maxRetries + 1; // +1 for the initial attempt
    for (var attempt = 0; attempt < attempts; attempt++) {
      try {
        final resp = await _client
            .post(
              _uri(path),
              headers: _headers(idempotent: true),
              body: body != null ? jsonEncode(body) : null,
            )
            .timeout(timeout);
        if (resp.statusCode == 200 || resp.statusCode == 201) {
          return ApiResult.ok(jsonDecode(resp.body));
        }
        if (resp.statusCode == 429 || resp.statusCode >= 500) {
          await Future.delayed(
            Duration(milliseconds: 200 * (attempt + 1) + Random().nextInt(200)),
          );
          continue;
        }
        return ApiResult.fail('${resp.statusCode}: ${resp.body}');
      } catch (e) {
        if (attempt < maxRetries) {
          await Future.delayed(
            Duration(milliseconds: 200 * (attempt + 1) + Random().nextInt(200)),
          );
          continue;
        }
        return ApiResult.fail('$e');
      }
    }
    return ApiResult.fail('Max retries exceeded');
  }

  // ── Send message ─────────────────────────────────────────────

  /// Sends a message to Rhodey via /api/send-message.
  /// On success, returns the raw body (which may contain Rhodey's response
  /// and session_id). Pass [sessionId] for thread continuity.
  /// **No retries** — to prevent duplicate processing on the backend.
  Future<ApiResult<dynamic>> sendMessage(
    String text, {
    String? sessionId,
  }) async {
    final body = <String, dynamic>{'message': text};
    if (sessionId != null && sessionId.isNotEmpty) {
      body['session_id'] = sessionId;
    }
    debugPrint(
      '[API] sendMessage: "${text.length > 60 ? text.substring(0, 60) : text}"',
    );
    // Fast-ack: /api/send-message returns in ~1s on Modal (the full pipeline
    // runs on a background worker; the reply arrives via FCM push + poll).
    // 30s covers the inline fallback path (local dev / Modal spawn failure)
    // where the full LLM turn still runs synchronously before responding.
    return post(
      '/api/send-message',
      body: body,
      timeout: const Duration(seconds: 30),
      maxRetries: 0,
    );
  }

  /// Uploads a file (image, audio, document) via /api/multimodal-input.
  /// Returns the response text and updated briefing.
  Future<ApiResult<dynamic>> sendMultimodal(
    String filePath, {
    String? fieldName,
  }) async {
    debugPrint('[API] sendMultimodal: $filePath');
    try {
      final uri = _uri('/api/multimodal-input');
      final request = http.MultipartRequest('POST', uri);

      // Add API key header
      if (_config.apiKey.isNotEmpty) {
        request.headers['X-API-Key'] = _config.apiKey;
      }

      request.files.add(
        await http.MultipartFile.fromPath(fieldName ?? 'file', filePath),
      );

      final streamedResp = await request.send().timeout(
        const Duration(seconds: 60),
      );
      final resp = await http.Response.fromStream(streamedResp);

      if (resp.statusCode == 200) {
        return ApiResult.ok(jsonDecode(resp.body));
      }
      return ApiResult.fail('${resp.statusCode}: ${resp.body}');
    } catch (e) {
      return ApiResult.fail('$e');
    }
  }

  // ── Messages (history) ────────────────────────────────────────

  /// Fetches message history from /api/messages.
  /// Returns the list of raw_dump records.
  Future<ApiResult<List<Map<String, dynamic>>>> getMessages({
    int limit = 50,
    int offset = 0,
  }) async {
    final result = await get(
      '/api/messages',
      query: {'limit': limit.toString(), 'offset': offset.toString()},
    );
    if (result.success && result.data is Map) {
      final msgs = (result.data as Map)['messages'] as List? ?? [];
      return ApiResult.ok(List<Map<String, dynamic>>.from(msgs));
    }
    if (result.success) {
      return ApiResult.ok([]);
    }
    return ApiResult.fail(result.error!);
  }

  /// Fetches the real user↔Rhodey conversation from /api/conversation-history.
  /// This reads the thread-scoped `conversations` table (proper user/bot
  /// roles) instead of the raw_dumps noise stream that /api/messages returns.
  Future<ApiResult<List<Map<String, dynamic>>>> getConversationHistory({
    int limit = 100,
    int offset = 0,
  }) async {
    final result = await get(
      '/api/conversation-history',
      query: {'limit': limit.toString(), 'offset': offset.toString()},
    );
    if (result.success && result.data is Map) {
      final msgs = (result.data as Map)['messages'] as List? ?? [];
      return ApiResult.ok(List<Map<String, dynamic>>.from(msgs));
    }
    if (result.success) {
      return ApiResult.ok([]);
    }
    return ApiResult.fail(result.error!);
  }

  // ── Calendar events ──────────────────────────────────────────

  /// Fetches today's calendar events from /api/calendar-events?date=today.
  Future<ApiResult<List<CalendarEventItem>>> getCalendarEvents() async {
    final result = await get('/api/calendar-events', query: {'date': 'today'});
    if (result.success && result.data is Map) {
      final events = (result.data as Map)['events'] as List? ?? [];
      final items = events.map((e) {
        final eMap = e as Map<String, dynamic>;
        final start = eMap['start'] as Map<String, dynamic>? ?? {};
        final startDt = start['dateTime'] as String? ?? '';
        final end = eMap['end'] as Map<String, dynamic>? ?? {};
        final endDt = end['dateTime'] as String? ?? '';
        final timeRange = _formatTimeRange(startDt, endDt);
        return CalendarEventItem(
          id: eMap['id']?.toString() ?? '',
          title: eMap['summary'] as String? ?? 'Untitled',
          timeRange: timeRange,
          isActive: _isNextEvent(startDt),
        );
      }).toList();
      return ApiResult.ok(items);
    }
    return ApiResult.fail(result.error ?? 'Failed to load events');
  }

  bool _isNextEvent(String startDt) {
    if (startDt.isEmpty) return false;
    try {
      // Parse in UTC, then convert to local for comparison.
      final dt = DateTime.parse(startDt).toLocal();
      final now = DateTime.now();
      return dt.isAfter(now.subtract(const Duration(hours: 1))) &&
          dt.isBefore(now.add(const Duration(hours: 2)));
    } catch (_) {
      return false;
    }
  }

  String _formatTimeRange(String startDt, String endDt) {
    try {
      final s = DateTime.parse(startDt).toLocal();
      final e = DateTime.parse(endDt).toLocal();
      final sStr =
          '${s.hour.toString().padLeft(2, '0')}:${s.minute.toString().padLeft(2, '0')}';
      final eStr =
          '${e.hour.toString().padLeft(2, '0')}:${e.minute.toString().padLeft(2, '0')}';
      // Strip date if same day
      if (s.year == e.year && s.month == e.month && s.day == e.day) {
        return '$sStr–$eStr';
      }
      return '${_fmtTime(s)}–${_fmtTime(e)}';
    } catch (_) {
      return startDt.isNotEmpty ? startDt : 'All day';
    }
  }

  String _fmtTime(DateTime dt) =>
      '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';

  // ── Tasks (Today tab) ────────────────────────────────────────

  /// Fetches active (todo) tasks from /api/tasks?status=todo.
  /// [includeSnoozed] also returns snoozed tasks (dimmed in the ledger).
  Future<ApiResult<List<Map<String, dynamic>>>> getTasks({
    String? status,
    bool includeSnoozed = false,
    int limit = 200,
  }) async {
    final query = <String, String>{
      // 200 rows by default so the active-task badge, ledger, and the
      // on-device title→id index don't undercount. Callers that only need a
      // slice of the list can pass a smaller limit.
      'limit': '$limit',
      'offset': '0',
    };
    if (status != null) {
      query['status'] = status;
    }
    if (includeSnoozed) {
      query['include_snoozed'] = 'true';
    }
    final result = await get('/api/tasks', query: query);
    if (result.success && result.data is Map) {
      final tasks = (result.data as Map)['tasks'] as List? ?? [];
      return ApiResult.ok(List<Map<String, dynamic>>.from(tasks));
    }
    return ApiResult.ok([]);
  }

  // ── Captures (Dump tab) ────────────────────────────────────────

  /// Fetches recent raw dumps from /api/captures.
  Future<ApiResult<List<Map<String, dynamic>>>> getCaptures({
    int limit = 50,
  }) async {
    final result = await get(
      '/api/captures',
      query: {'limit': limit.toString(), 'offset': '0'},
    );
    if (result.success && result.data is Map) {
      final captures = (result.data as Map)['captures'] as List? ?? [];
      return ApiResult.ok(List<Map<String, dynamic>>.from(captures));
    }
    return ApiResult.ok([]);
  }

  // ── Task status update ────────────────────────────────────────

  /// Marks a task as done/cancelled via PATCH /api/tasks/{id}/status.
  /// The backend registers this route as PATCH only (POST returns 405),
  /// so we must use PATCH — a POST here silently fails to complete the task.
  Future<ApiResult<dynamic>> updateTaskStatus(int taskId, String status) async {
    return _send(
      'PATCH',
      '/api/tasks/$taskId/status',
      body: {'status': status},
    );
  }

  // ── Entity details (People tab consolidated into Entities) ─

  /// Open tasks mentioning a person (Entities edit dialog).
  /// [personId] is the graph node UUID (migration 75 — people table removed).
  Future<ApiResult<List<Map<String, dynamic>>>> getPersonTasks(
    String personId,
    String personName,
  ) async {
    final result = await get(
      '/api/people/$personId/tasks',
      query: {'name': personName},
    );
    if (!result.success || result.data is! List) {
      return ApiResult.fail(result.error ?? 'Failed to load person tasks');
    }
    return ApiResult.ok(List<Map<String, dynamic>>.from(result.data as List));
  }

  /// All person aliases (for the Entities edit dialog's alias manager).
  Future<ApiResult<List<Map<String, dynamic>>>> getAliases() async {
    final result = await get('/api/aliases');
    if (!result.success || result.data is! Map) {
      return ApiResult.fail(result.error ?? 'Failed to load aliases');
    }
    final aliases = (result.data as Map)['aliases'];
    if (aliases is! List) return ApiResult.ok([]);
    return ApiResult.ok(List<Map<String, dynamic>>.from(aliases));
  }

  /// Create a person alias (alias -> canonical name).
  Future<ApiResult<dynamic>> createAlias(
    String alias,
    String canonicalName,
  ) async {
    return post(
      '/api/aliases',
      body: {'alias': alias, 'canonical_name': canonicalName},
    );
  }

  /// Delete a person alias from its node (migration 76: alias on node).
  Future<ApiResult<dynamic>> deleteAlias(
    String alias,
    String canonicalName,
  ) async {
    return _send(
      'DELETE',
      '/api/aliases',
      body: {'alias': alias, 'canonical_name': canonicalName},
    );
  }

  // ── Decision actions ──────────────────────────────────────────

  /// Approve a pending graph node via /api/graph-node-action.
  Future<ApiResult<dynamic>> approveGraphNode(
    int pendingId, {
    String? label,
  }) async {
    return post(
      '/api/graph-node-action',
      body: {'id': pendingId, 'action': 'approve', 'label': label},
    );
  }

  /// Approve a person node with role/relationship context.
  /// Sends [context] separately — does NOT overwrite the node label.
  Future<ApiResult<dynamic>> approveGraphNodeWithContext(
    int pendingId, {
    String? context,
  }) async {
    final body = <String, dynamic>{'id': pendingId, 'action': 'approve'};
    if (context != null && context.isNotEmpty) {
      body['context'] = context;
    }
    return post('/api/graph-node-action', body: body);
  }

  /// Reject a pending graph node.
  Future<ApiResult<dynamic>> rejectGraphNode(int pendingId) async {
    return post(
      '/api/graph-node-action',
      body: {'id': pendingId, 'action': 'reject'},
    );
  }

  /// Approve/reject a pending graph edge via /api/graph-edge-action.
  Future<ApiResult<dynamic>> approveGraphEdge(int pendingId) async {
    return post(
      '/api/graph-edge-action',
      body: {'id': pendingId, 'action': 'approve'},
    );
  }

  /// Approve an edge with a corrected relationship label.
  Future<ApiResult<dynamic>> approveGraphEdgeWithRelation(
    int pendingId,
    String newRelationship,
  ) async {
    return post(
      '/api/graph-edge-action',
      body: {'id': pendingId, 'action': 'approve', 'new_rel': newRelationship},
    );
  }

  /// Accept or reject a merge proposal via /api/graph-merge-action.
  Future<ApiResult<dynamic>> acceptMerge(int pendingId) async {
    return post(
      '/api/graph-merge-action',
      body: {'id': pendingId, 'action': 'accept'},
    );
  }

  Future<ApiResult<dynamic>> rejectMerge(int pendingId) async {
    return post(
      '/api/graph-merge-action',
      body: {'id': pendingId, 'action': 'reject'},
    );
  }

  Future<ApiResult<dynamic>> rejectGraphEdge(int pendingId) async {
    return post(
      '/api/graph-edge-action',
      body: {'id': pendingId, 'action': 'reject'},
    );
  }

  /// Approve/reject email pending item via /api/email-action.
  Future<ApiResult<dynamic>> approveEmail(int pendingId) async {
    return post(
      '/api/email-action',
      body: {'id': pendingId, 'action': 'approve'},
    );
  }

  Future<ApiResult<dynamic>> rejectEmail(int pendingId) async {
    return post(
      '/api/email-action',
      body: {'id': pendingId, 'action': 'reject'},
    );
  }

  /// Approve/reject WhatsApp pending item via /api/whatsapp-action.
  Future<ApiResult<dynamic>> approveWhatsApp(int pendingId) async {
    return post(
      '/api/whatsapp-action',
      body: {'id': pendingId, 'action': 'approve'},
    );
  }

  Future<ApiResult<dynamic>> rejectWhatsApp(int pendingId) async {
    return post(
      '/api/whatsapp-action',
      body: {'id': pendingId, 'action': 'reject'},
    );
  }

  /// Approve/reject call pending item via /api/call-action.
  Future<ApiResult<dynamic>> approveCall(int pendingId) async {
    return post(
      '/api/call-action',
      body: {'id': pendingId, 'action': 'approve'},
    );
  }

  Future<ApiResult<dynamic>> rejectCall(int pendingId) async {
    return post(
      '/api/call-action',
      body: {'id': pendingId, 'action': 'reject'},
    );
  }

  /// Approve/reject Teams pending item via /api/teams-action.
  Future<ApiResult<dynamic>> approveTeams(int pendingId) async {
    return post(
      '/api/teams-action',
      body: {'id': pendingId, 'action': 'approve'},
    );
  }

  Future<ApiResult<dynamic>> rejectTeams(int pendingId) async {
    return post(
      '/api/teams-action',
      body: {'id': pendingId, 'action': 'reject'},
    );
  }

  /// Send or drop a pending email reply draft.
  Future<ApiResult<dynamic>> draftAction(int draftId, {required String action}) async {
    return post(
      '/api/draft-action',
      body: {'draft_id': draftId, 'action': action},
    );
  }

  /// Acknowledge (dismiss) an FYI item.
  Future<ApiResult<dynamic>> acknowledgeFyi(int itemId) async {
    return post(
      '/api/fyi-action',
      body: {'id': itemId},
    );
  }

  /// Submit a clarification answer via /api/clarification.
  Future<ApiResult<dynamic>> submitClarification(
    String shortcode,
    String answer,
  ) async {
    return post(
      '/api/clarification',
      body: {'shortcode': shortcode, 'answer': answer},
    );
  }

  // ── Fetch pending items (composite) ───────────────────────────

  /// Fetches pending graph nodes from /api/pending-graph-nodes.
  Future<ApiResult<List<PendingDecision>>> fetchPendingGraphNodes() async {
    final result = await get('/api/pending-graph-nodes');
    if (!result.success || result.data is! Map) {
      return ApiResult.ok([]);
    }
    return ApiResult.ok(
      _parseGraphNodes((result.data as Map)['data'] as List? ?? []),
    );
  }

  /// Fetches pending graph edges from /api/pending-graph-edges.
  Future<ApiResult<List<PendingDecision>>> fetchPendingGraphEdges() async {
    final result = await get('/api/pending-graph-edges');
    if (!result.success || result.data is! Map) {
      return ApiResult.ok([]);
    }
    return ApiResult.ok(
      _parseGraphEdges((result.data as Map)['data'] as List? ?? []),
    );
  }

  /// Fetches pending merge proposals from /api/pending-merges.
  Future<ApiResult<List<PendingDecision>>> fetchPendingMerges() async {
    final result = await get('/api/pending-merges');
    if (!result.success || result.data is! Map) {
      return ApiResult.ok([]);
    }
    return ApiResult.ok(
      _parseMerges((result.data as Map)['data'] as List? ?? []),
    );
  }

  /// Fetches ALL pending decisions from all sources (4 concurrent calls).
  Future<ApiResult<List<PendingDecision>>> getPendingDecisions() async {
    final nodeFut = fetchPendingGraphNodes();
    final edgeFut = fetchPendingGraphEdges();
    final mergeFut = fetchPendingMerges();
    final msgFut = getMessages(limit: 50);

    final nodeResult = await nodeFut;
    final edgeResult = await edgeFut;
    final mergeResult = await mergeFut;
    final msgResult = await msgFut;

    final decisions = <PendingDecision>[];
    if (nodeResult.success) decisions.addAll(nodeResult.data!);
    if (mergeResult.success) decisions.addAll(mergeResult.data!);
    if (edgeResult.success) decisions.addAll(edgeResult.data!);
    if (msgResult.success) decisions.addAll(_parseMessages(msgResult.data!));

    _sortDecisions(decisions);
    return ApiResult.ok(decisions);
  }

  /// One-call Inbox payload via /api/inbox — collapses the four pending-source
  /// fetches + the auto-decision count into a single round-trip (the same
  /// consolidation /api/home-feed did for the home screen). Returns failure
  /// when the endpoint is unavailable so callers fall back to the legacy
  /// per-endpoint path.
  Future<ApiResult<InboxBundle>> getInboxBundle() async {
    final result = await get('/api/inbox');
    if (!result.success || result.data is! Map) {
      return ApiResult.fail(result.error ?? 'Inbox bundle unavailable');
    }
    final data = result.data as Map;
    final decisions = <PendingDecision>[
      ..._parseGraphNodes(data['pending_nodes'] as List? ?? []),
      ..._parseMerges(data['pending_merges'] as List? ?? []),
      ..._parseGraphEdges(data['pending_edges'] as List? ?? []),
      ..._parseMessages(
        List<Map<String, dynamic>>.from(
          data['pending_messages'] as List? ?? [],
        ),
      ),
      // Actionable channel items (email/whatsapp/call/teams) — the real
      // Quick-Confirmation feed. Served alongside raw_dumps rows so the
      // Inbox shows everything awaiting a decision.
      ..._parseMessages(
        (data['pending_channel_messages'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            [],
      ),
    ];
    _sortDecisions(decisions);
    return ApiResult.ok(
      InboxBundle(
        decisions: decisions,
        autoDecisionCount: (data['auto_decision_count'] as num?)?.toInt() ?? 0,
        drafts: (data['pending_drafts'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            const [],
        fyi: (data['pending_fyi'] as List?)?.cast<Map<String, dynamic>>() ??
            const [],
      ),
    );
  }

  /// Sorts pending decisions: merges first, then graph nodes, edges, channels.
  void _sortDecisions(List<PendingDecision> decisions) {
    decisions.sort((a, b) {
      const order = [
        'merge',
        'graph_node',
        'graph_edge',
        'email',
        'call',
        'whatsapp',
      ];
      final ai = order.indexOf(a.source);
      final bi = order.indexOf(b.source);
      return ai.compareTo(bi);
    });
  }

  List<PendingDecision> _parseGraphNodes(List<dynamic> items) {
    return items.map((n) {
      final node = n as Map<String, dynamic>;
      final label = node['label'] as String? ?? 'Untitled';
      final nodeType = node['type'] as String? ?? 'person';
      final status = node['status'] as String? ?? 'pending';
      final conf = (node['confidence'] as num?)?.toDouble();
      final ctx = node['eval_context'] as Map<String, dynamic>?;

      String title = label;
      String? description;
      // Awaiting-details / awaiting-clarification nodes are surfaced so the
      // user can approve (accept with context) or reject — the Inbox shows
      // them with the CLARIFICATION badge.
      String source = 'graph_node';
      final awaiting = status == 'awaiting_details' ||
          status == 'awaiting_clarification';
      if (awaiting) {
        source = 'clarification';
        title = label;
        description = status == 'awaiting_clarification'
            ? 'Awaiting your input — approve to accept, or reject'
            : 'Needs context — approve to accept, or reject';
      } else if (nodeType == 'concept' && ctx != null) {
        final linked = ctx['linked_entity'] as String?;
        if (linked != null) {
          title = '$label (→ $linked)';
          description = 'Concept linked to $linked';
        }
      } else {
        description = 'New $nodeType node';
      }
      if (status == 'merge_proposed') {
        title = 'Merge: $label';
        description = 'Merge proposed — review and accept or reject';
      }

      return PendingDecision(
        id: node['id'].toString(),
        source: source,
        title: title,
        description: description,
        confidence: conf,
        raw: node,
      );
    }).toList();
  }

  List<PendingDecision> _parseGraphEdges(List<dynamic> items) {
    return items.map((e) {
      final edge = e as Map<String, dynamic>;
      final src = edge['source_label'] as String? ?? '?';
      final tgt = edge['target_label'] as String? ?? '?';
      final rel = edge['relationship'] as String? ?? 'relates_to';
      final conf = (edge['confidence'] as num?)?.toDouble();
      final ctx = edge['context'] as String?;

      return PendingDecision(
        id: edge['id'].toString(),
        source: 'graph_edge',
        title: '$src → $rel → $tgt',
        description: ctx ?? 'Pending edge',
        confidence: conf,
        raw: edge,
      );
    }).toList();
  }

  List<PendingDecision> _parseMerges(List<dynamic> items) {
    return items.map((m) {
      final mp = m as Map<String, dynamic>;
      final sourceLabel = mp['source_label'] as String? ?? '?';
      final targetLabel = mp['target_label'] as String? ?? '?';

      return PendingDecision(
        id: mp['id'].toString(),
        source: 'merge',
        title: '$sourceLabel → $targetLabel',
        description:
            mp['rationale'] as String? ??
            'Merge proposed — accept to combine, reject to keep separate',
        raw: {...mp, 'status': 'merge_proposed'},
      );
    }).toList();
  }

  List<PendingDecision> _parseMessages(List<Map<String, dynamic>> msgs) {
    final decisions = <PendingDecision>[];
    for (final m in msgs) {
      final source = m['source'] as String? ?? '';
      final status = m['status'] as String? ?? '';
      final messageType = m['message_type'] as String? ?? '';

      if (status != 'pending') continue;

      String decisionSource;
      if (source == 'email' || messageType == 'email_action') {
        decisionSource = 'email';
      } else if (source == 'whatsapp') {
        decisionSource = 'whatsapp';
      } else if (source == 'call') {
        decisionSource = 'call';
      } else if (source == 'teams') {
        decisionSource = 'teams';
      } else {
        continue;
      }

      decisions.add(
        PendingDecision(
          id: m['id'].toString(),
          source: decisionSource,
          title: m['content'] as String? ?? 'Untitled',
          description: 'via $decisionSource',
          raw: m,
        ),
      );
    }
    return decisions;
  }

  // ── Briefing ──────────────────────────────────────────────

  /// Fetches the structured briefing from /api/briefing.
  Future<BriefingResponse> getBriefing() async {
    final result = await get(
      '/api/briefing',
      timeout: const Duration(seconds: 15),
    );
    if (result.success && result.data is Map) {
      return BriefingResponse.fromJson(result.data as Map<String, dynamic>);
    }
    return BriefingResponse.empty();
  }

  /// ONE-CALL home load (P2): briefing + pending decisions + active tasks
  /// from /api/home-feed. Collapses the app's 6 startup round-trips into 1.
  Future<HomeFeed> getHomeFeed() async {
    final result = await get(
      '/api/home-feed',
      timeout: const Duration(seconds: 15),
    );
    if (!result.success || result.data is! Map) {
      // Fallback: home-feed unavailable (older backend) — the caller's
      // individual fetchers remain the source of truth.
      return HomeFeed(
        briefing: BriefingResponse.empty(),
        decisions: const [],
        tasks: const [],
      );
    }
    final data = result.data as Map<String, dynamic>;
    // Write-behind: keep the on-device cache warm for instant cold opens.
    // Only persist a payload that actually carries content — a transient
    // success-but-empty response must not wipe a good cache (which would
    // make the next cold open render nothing at all).
    final feed = _parseHomeFeed(data);
    if (!feed.isEmpty) {
      await _homeFeedCache.save(data);
    }
    return feed;
  }

  /// Renders the last cached home feed instantly (no network wait), or null
  /// when no cache exists yet. Cold opens / notification taps call this first,
  /// then [getHomeFeed] reconciles with live data.
  Future<HomeFeed?> getCachedHomeFeed() async {
    final cached = await _homeFeedCache.load();
    if (cached == null || cached.isEmpty) return null;
    final feed = _parseHomeFeed(cached);
    return feed.isEmpty ? null : feed;
  }

  /// Parses a raw /api/home-feed payload into a [HomeFeed]. Shared by the live
  /// fetch and the local cache so both paths agree on shape.
  HomeFeed _parseHomeFeed(Map<String, dynamic> data) {
    final briefing = BriefingResponse.fromJson(
      (data['briefing'] as Map<String, dynamic>?) ?? {},
    );
    final decisions = <PendingDecision>[
      ..._parseGraphNodes((data['pending_nodes'] as List?) ?? []),
      ..._parseMerges((data['pending_merges'] as List?) ?? []),
      ..._parseGraphEdges((data['pending_edges'] as List?) ?? []),
      ..._parseMessages(
        (data['pending_messages'] as List?)?.cast<Map<String, dynamic>>() ?? [],
      ),
      ..._parseMessages(
        (data['pending_channel_messages'] as List?)
                ?.cast<Map<String, dynamic>>() ??
            [],
      ),
    ];
    _sortDecisions(decisions);
    final tasks = (data['tasks'] as List?)?.cast<Map<String, dynamic>>() ?? [];
    final persona = data['persona'] is Map<String, dynamic>
        ? PersonaSummary.fromJson(data['persona'] as Map<String, dynamic>)
        : PersonaSummary.empty;
    return HomeFeed(
      briefing: briefing,
      decisions: decisions,
      tasks: tasks,
      persona: persona,
    );
  }

  // ── Persona surface summary (Phase 2B) ─────────────────────────

  /// Fetches the per-tenant persona surface summary from /api/persona.
  /// Fail-closed: null payload / network error => [PersonaSummary.empty],
  /// so the app renders today's standard voice.
  Future<PersonaSummary> getPersona() async {
    final result = await get('/api/persona', timeout: const Duration(seconds: 10));
    if (result.success && result.data is Map) {
      return PersonaSummary.fromJson(result.data as Map<String, dynamic>);
    }
    return PersonaSummary.empty;
  }

  // ── Home mode switch ────────────────────────────────────────

  /// Sends a mode switch correction signal to /api/home-mode-switch.
  /// This trains Rhodey by recording the user's mode preference.
  Future<ApiResult<dynamic>> switchHomeMode(
    String previousMode,
    String newMode,
  ) async {
    return post(
      '/api/home-mode-switch',
      body: {'previous_mode': previousMode, 'new_mode': newMode},
      maxRetries: 1,
    );
  }

  // ── Focal item action (Phase 2 v2: done/snooze/correct) ──

  /// Send a focal item action to /api/focal-action.
  /// [action] is "done", "snooze", or "correct".
  /// [dryRun] (snooze only) asks the server where the item sits on the
  /// snooze escalation ladder (1d → 3d → 7d-warn → 7d cap) WITHOUT
  /// persisting anything — the app uses it to decide whether to show the
  /// 3rd-tap warning + feedback gate.
  /// [feedback] (snooze only) is the reason typed at the 3rd-tap gate; it
  /// is stored server-side as a learning signal.
  Future<ApiResult<dynamic>> focalAction({
    required String action,
    required String itemType,
    required String itemId,
    String title = '',
    String reason = '',
    bool dryRun = false,
    String feedback = '',
  }) async {
    return post(
      '/api/focal-action',
      body: {
        'action': action,
        'item_type': itemType,
        'item_id': itemId,
        'title': title,
        'reason': reason,
        if (dryRun) 'dry_run': true,
        if (feedback.isNotEmpty) 'feedback': feedback,
      },
      maxRetries: 1,
    );
  }

  /// Pull a vaulted task forward via /api/vault-action so it re-enters
  /// the briefing horizon. [action] is "pull_forward".
  Future<ApiResult<dynamic>> vaultAction({
    required String action,
    required int taskId,
  }) async {
    return post(
      '/api/vault-action',
      body: {'action': action, 'task_id': taskId},
      maxRetries: 1,
    );
  }

  // ── Live Entities (Decisions tab mirror) ─────────────────────

  /// Fetches live graph nodes (people, orgs, concepts) from /api/graph-nodes/live.
  /// Fetches live graph nodes (people, orgs, concepts) from
  /// /api/graph-nodes/live — paginated + searchable so the Entities screen
  /// never downloads the whole graph (was a 5000-row dump with metadata).
  Future<ApiResult<List<Map<String, dynamic>>>> getLiveGraphNodes({
    int limit = 200,
    int offset = 0,
    String? q,
  }) async {
    final query = <String, String>{'limit': '$limit', 'offset': '$offset'};
    if (q != null && q.trim().isNotEmpty) query['q'] = q.trim();
    final result = await get('/api/graph-nodes/live', query: query);
    if (result.success && result.data is Map) {
      final nodes = (result.data as Map)['data'] as List? ?? [];
      return ApiResult.ok(List<Map<String, dynamic>>.from(nodes));
    }
    if (result.success) {
      return ApiResult.ok([]);
    }
    return ApiResult.fail(result.error ?? 'Failed to load live entities');
  }

  /// Searches live graph nodes by label for merge targeting.
  Future<ApiResult<List<Map<String, dynamic>>>> searchGraphNodes(
    String query, {
    String? type,
    String scope = 'live',
  }) async {
    final q = <String, String>{'q': query, 'scope': scope};
    if (type != null) q['type'] = type;
    final result = await get('/api/graph-nodes/search', query: q);
    if (result.success && result.data is List) {
      return ApiResult.ok(List<Map<String, dynamic>>.from(result.data as List));
    }
    if (result.success) {
      return ApiResult.ok([]);
    }
    return ApiResult.ok([]);
  }

  /// Merges one live node into another via /api/graph-node-merge.
  /// One-shot operation: no retry — a merge already applied server-side must
  /// not re-fire (and _send carries no idempotency key).
  Future<ApiResult<dynamic>> mergeGraphNode(
    String sourceId,
    String targetId, {
    String scope = 'live',
  }) async {
    return _send(
      'POST',
      '/api/graph-node-merge',
      body: {'id': sourceId, 'target_id': targetId, 'scope': scope},
      retries: 0,
    );
  }

  /// Renames a live node via PUT /api/graph-node/{id}.
  Future<ApiResult<dynamic>> renameGraphNode(
    String id,
    String newLabel, {
    String scope = 'live',
  }) async {
    return _send(
      'PUT',
      '/api/graph-node/$id',
      body: {'label': newLabel, 'scope': scope},
    );
  }

  /// Changes a live node's type via PATCH /api/graph-node/{id}/type.
  Future<ApiResult<dynamic>> changeGraphNodeType(
    String id,
    String newType, {
    String scope = 'live',
  }) async {
    return _send(
      'PATCH',
      '/api/graph-node/$id/type',
      body: {'type': newType, 'scope': scope},
    );
  }

  /// Deletes a live node via DELETE /api/graph-node/{id}?scope=live.
  Future<ApiResult<dynamic>> deleteGraphNode(
    String id, {
    String scope = 'live',
  }) async {
    return _send('DELETE', '/api/graph-node/$id', query: {'scope': scope});
  }

  /// Updates enrichment on a live node via PATCH /api/graph-node/{id}/enrichment.
  ///
  /// Consolidation (migrations 74-76): role, strategic_weight, is_active,
  /// org_type, description, organization_name all live in metadata.enrichment.
  Future<ApiResult<dynamic>> updateGraphNodeEnrichment(
    String id,
    Map<String, dynamic> enrichment,
  ) async {
    return _send('PATCH', '/api/graph-node/$id/enrichment', body: enrichment);
  }

  /// Low-level PUT/PATCH/DELETE with the same auth + JSON conventions.
  /// Low-level PUT/PATCH/DELETE with the same auth + JSON conventions.
  ///
  /// [retries] retries ONLY on 429/5xx responses — never on exceptions or
  /// timeouts, where the write may already have been applied (unlike post(),
  /// _send sends no idempotency key). A 429/5xx means the request almost
  /// certainly wasn't applied, so a retry is safe for idempotent writes
  /// (status updates, renames, deletes). Pass `retries: 0` for one-shot
  /// operations that must not re-fire (e.g. a merge).
  Future<ApiResult<dynamic>> _send(
    String method,
    String path, {
    Map<String, dynamic>? body,
    Map<String, String>? query,
    int retries = 1,
  }) async {
    final uri = query != null
        ? _uri(path).replace(queryParameters: query)
        : _uri(path);
    for (var attempt = 0; attempt <= retries; attempt++) {
      try {
        late http.Response resp;
        if (method == 'PUT') {
          resp = await _client
              .put(
                uri,
                headers: _headers(),
                body: body != null ? jsonEncode(body) : null,
              )
              .timeout(const Duration(seconds: 15));
        } else if (method == 'PATCH') {
          resp = await _client
              .patch(
                uri,
                headers: _headers(),
                body: body != null ? jsonEncode(body) : null,
              )
              .timeout(const Duration(seconds: 15));
        } else if (method == 'DELETE') {
          resp = await _client
              .delete(uri, headers: _headers())
              .timeout(const Duration(seconds: 15));
        } else {
          return ApiResult.fail('Unsupported method $method');
        }
        if (resp.statusCode == 200 || resp.statusCode == 201) {
          final decoded = resp.body.isNotEmpty
              ? jsonDecode(resp.body)
              : {'success': true};
          return ApiResult.ok(decoded);
        }
        if (attempt < retries &&
            (resp.statusCode == 429 || resp.statusCode >= 500)) {
          // Server explicitly said retryable — jittered backoff, single pass.
          await Future.delayed(
            Duration(milliseconds: 250 * (attempt + 1) + Random().nextInt(150)),
          );
          continue;
        }
        return ApiResult.fail('${resp.statusCode}: ${resp.body}');
      } catch (e) {
        return ApiResult.fail('$e');
      }
    }
    return ApiResult.fail('Max retries exceeded');
  }

  // ── Onboarding journey (M8) ─────────────────────────────────

  /// Fetches the user's onboarding state: new | in_progress | seeded.
  Future<ApiResult<dynamic>> getOnboardingStatus() =>
      get('/api/onboarding/status', timeout: const Duration(seconds: 12));

  /// M9.8: the briefing-schedule presets (times + default) served by the
  /// server — the single source of truth for the onboarding picker, so the
  /// displayed slots can never drift from the heartbeat gate. Static
  /// config, no auth.
  Future<ApiResult<dynamic>> getBriefingPresets() =>
      get('/api/onboarding/presets', timeout: const Duration(seconds: 12));

  /// M9.6: the tenant's display name for the UI (greetings, "All clear",
  /// placeholders). Stored locally after first resolution — the backend's
  /// /api/onboarding/status returns users.name for ANY journey state, so
  /// seeded tenants (tenant #1) and freshly-onboarded ones both resolve
  /// correctly. Returns '' only when the backend can't tell us; callers
  /// fall back to the legacy name.
  ///
  /// NOTE: once stored, the name is treated as authoritative (no per-load
  /// refresh) — an admin-side rename propagates only after the app clears
  /// or reinstalls. Deliberate: this is a display name, not live state.
  Future<String> resolveUserName() async {
    final stored = _config.userName;
    if (stored.isNotEmpty) return stored;
    try {
      final r = await getOnboardingStatus();
      if (r.success && r.data is Map) {
        final name = ((r.data as Map)['name'] as String?)?.trim() ?? '';
        if (name.isNotEmpty) {
          await _config.setUserName(name);
          return name;
        }
      }
    } catch (_) {
      // Network/parse failure — return '' and retry on the next load.
    }
    return '';
  }

  /// Submits the completed journey and returns the first welcome briefing.
  /// No retries — the seed is idempotent but must not double-fire.
  Future<ApiResult<dynamic>> completeOnboarding(
    Map<String, dynamic> payload,
  ) =>
      post(
        '/api/onboarding/complete',
        body: payload,
        timeout: const Duration(seconds: 60),
        maxRetries: 0,
      );

  /// Onboarding demo: runs a scripted message through the REAL pipeline
  /// inline (reply returns synchronously — no background worker) and stamps
  /// the artifacts it creates as demo-owned ([onboarding-demo] / metadata
  /// demo) so they're cleanable. No retries — a retry would re-run the
  /// action and double-create the demo item.
  Future<ApiResult<dynamic>> sendDemoMessage(
    String text, {
    String? sessionId,
  }) async {
    final body = <String, dynamic>{'message': text};
    if (sessionId != null && sessionId.isNotEmpty) {
      body['session_id'] = sessionId;
    }
    return post(
      '/api/demo/message',
      body: body,
      timeout: const Duration(seconds: 90),
      maxRetries: 0,
    );
  }

  /// Deletes the tenant's demo-tagged artifacts (raw_dumps rows, open demo
  /// tasks, demo memories). Idempotent — safe to call more than once.
  Future<ApiResult<dynamic>> cleanupDemo() =>
      post('/api/demo/cleanup', body: const {}, timeout: const Duration(seconds: 30));

  /// Starts in-app Google OAuth — returns {url, state}.
  Future<ApiResult<dynamic>> startGoogleOAuth() =>
      get('/api/oauth/start', timeout: const Duration(seconds: 12));

  /// Exchanges the OAuth code (captured from the rhodey:// callback) for
  /// tokens stored per-user on the backend.
  Future<ApiResult<dynamic>> exchangeGoogleOAuth(String code, String state) =>
      post(
        '/api/oauth/exchange',
        body: {'code': code, 'state': state},
        timeout: const Duration(seconds: 25),
        maxRetries: 0,
      );

  // ── M11 sign-in (Google + email/OTP) ────────────────────────────

  /// Requests a 6-digit sign-in code for an email (keyless, rate-limited).
  Future<ApiResult<dynamic>> sendOtp(String email) =>
      post(
        '/api/auth/otp/send',
        body: {'email': email},
        timeout: const Duration(seconds: 12),
        maxRetries: 0,
      );

  /// Validates the code and returns the issued API key (stored by caller).
  Future<ApiResult<dynamic>> verifyOtp(String email, String code) =>
      post(
        '/api/auth/otp/verify',
        body: {'email': email, 'code': code},
        timeout: const Duration(seconds: 12),
        maxRetries: 0,
      );

  /// Starts identity-only Google sign-in: returns {url, state} (keyless).
  Future<ApiResult<dynamic>> googleSignInStart() =>
      get('/api/auth/google/start', timeout: const Duration(seconds: 12));

  /// Exchanges the identity code for the issued API key (keyless).
  Future<ApiResult<dynamic>> googleSignInExchange(String code, String state) =>
      post(
        '/api/auth/google/exchange',
        body: {'code': code, 'state': state},
        timeout: const Duration(seconds: 25),
        maxRetries: 0,
      );

  // ── Config access ────────────────────────────────────────────

  ApiConfig get config => _config;
}
