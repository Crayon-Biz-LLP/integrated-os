import 'dart:convert';
import 'dart:math' show Random;
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'api_config.dart';
import '../models/briefing.dart';

// ── Types ──────────────────────────────────────────────────────

/// Result of an API call.
class ApiResult<T> {
  final bool success;
  final T? data;
  final String? error;

  const ApiResult({required this.success, this.data, this.error});

  static ApiResult<T> ok<T>(T data) => ApiResult(success: true, data: data);
  static ApiResult<T> fail<T>(String error) => ApiResult(success: false, error: error);
}

/// A pending decision item from any source channel.
class PendingDecision {
  final String id;
  final String source; // 'email', 'call', 'whatsapp', 'graph_node', 'graph_edge'
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
  Future<ApiResult<dynamic>> _get(String path,
      {Map<String, String>? query}) async {
    final uri = query != null
        ? _uri(path).replace(queryParameters: query)
        : _uri(path);
    for (var attempt = 0; attempt < 3; attempt++) {
      try {
        final resp = await _client
            .get(uri, headers: _headers())
            .timeout(const Duration(seconds: 15));
        if (resp.statusCode == 200 || resp.statusCode == 201) {
          return ApiResult.ok(jsonDecode(resp.body));
        }
        if (resp.statusCode == 429 || resp.statusCode >= 500) {
          // Retryable
          await Future.delayed(Duration(
              milliseconds: 200 * (attempt + 1) + Random().nextInt(200)));
          continue;
        }
        return ApiResult.fail('${resp.statusCode}: ${resp.body}');
      } catch (e) {
        if (attempt < 2) {
          await Future.delayed(Duration(
              milliseconds: 200 * (attempt + 1) + Random().nextInt(200)));
          continue;
        }
        return ApiResult.fail('$e');
      }
    }
    return ApiResult.fail('Max retries exceeded');
  }

  /// POST with idempotency key and retry.
  /// Public so other services (e.g., NotificationService) can call it.
  Future<ApiResult<dynamic>> post(String path,
      {Map<String, dynamic>? body, Duration timeout = const Duration(seconds: 15)}) async {
    for (var attempt = 0; attempt < 3; attempt++) {
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
          await Future.delayed(Duration(
              milliseconds: 200 * (attempt + 1) + Random().nextInt(200)));
          continue;
        }
        return ApiResult.fail('${resp.statusCode}: ${resp.body}');
      } catch (e) {
        if (attempt < 2) {
          await Future.delayed(Duration(
              milliseconds: 200 * (attempt + 1) + Random().nextInt(200)));
          continue;
        }
        return ApiResult.fail('$e');
      }
    }
    return ApiResult.fail('Max retries exceeded');
  }

  // ── Send message ─────────────────────────────────────────────

  /// Sends a message to Rhodey via /api/send-message.
  /// On success, returns the raw body (which may contain Rhodey's response and session_id).
  /// Pass [sessionId] for thread continuity across messages.
  /// Uses a longer timeout (30s) because process_webhook on Vercel includes LLM calls.
  Future<ApiResult<dynamic>> sendMessage(String text, {String? sessionId}) async {
    final body = <String, dynamic>{'message': text};
    if (sessionId != null && sessionId.isNotEmpty) {
      body['session_id'] = sessionId;
    }
    debugPrint('[API] sendMessage: "${text.length > 60 ? text.substring(0, 60) : text}"');
    return post('/api/send-message', body: body,
        timeout: const Duration(seconds: 30));
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
        await http.MultipartFile.fromPath(
          fieldName ?? 'file',
          filePath,
        ),
      );
      
      final streamedResp = await request.send().timeout(const Duration(seconds: 60));
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
    final result = await _get('/api/messages',
        query: {
          'limit': limit.toString(),
          'offset': offset.toString(),
        });
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
    final result =
        await _get('/api/calendar-events', query: {'date': 'today'});
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
      final sStr = '${s.hour.toString().padLeft(2, '0')}:${s.minute.toString().padLeft(2, '0')}';
      final eStr = '${e.hour.toString().padLeft(2, '0')}:${e.minute.toString().padLeft(2, '0')}';
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
  Future<ApiResult<List<Map<String, dynamic>>>> getTasks({String? status}) async {
    final query = <String, String>{
      'limit': '30',
      'offset': '0',
    };
    if (status != null) {
      query['status'] = status;
    }
    final result = await _get('/api/tasks', query: query);
    if (result.success && result.data is Map) {
      final tasks = (result.data as Map)['tasks'] as List? ?? [];
      return ApiResult.ok(List<Map<String, dynamic>>.from(tasks));
    }
    return ApiResult.ok([]);
  }

  // ── Captures (Dump tab) ────────────────────────────────────────

  /// Fetches recent raw dumps from /api/captures.
  Future<ApiResult<List<Map<String, dynamic>>>> getCaptures({int limit = 50}) async {
    final result = await _get('/api/captures', query: {
      'limit': limit.toString(),
      'offset': '0',
    });
    if (result.success && result.data is Map) {
      final captures = (result.data as Map)['captures'] as List? ?? [];
      return ApiResult.ok(List<Map<String, dynamic>>.from(captures));
    }
    return ApiResult.ok([]);
  }

  // ── Task status update ────────────────────────────────────────

  /// Marks a task as done/cancelled via /api/tasks/{id}/status.
  Future<ApiResult<dynamic>> updateTaskStatus(
      int taskId, String status) async {
    return post('/api/tasks/$taskId/status', body: {'status': status});
  }

  // ── Decision actions ──────────────────────────────────────────

  /// Approve a pending graph node via /api/graph-node-action.
  Future<ApiResult<dynamic>> approveGraphNode(int pendingId,
      {String? label}) async {
    return post('/api/graph-node-action', body: {
      'id': pendingId,
      'action': 'approve',
      'label': label,
    });
  }

  /// Reject a pending graph node.
  Future<ApiResult<dynamic>> rejectGraphNode(int pendingId) async {
    return post('/api/graph-node-action', body: {
      'id': pendingId,
      'action': 'reject',
    });
  }

  /// Approve/reject a pending graph edge via /api/graph-edge-action.
  Future<ApiResult<dynamic>> approveGraphEdge(int pendingId) async {
    return post('/api/graph-edge-action', body: {
      'id': pendingId,
      'action': 'approve',
    });
  }

  Future<ApiResult<dynamic>> rejectGraphEdge(int pendingId) async {
    return post('/api/graph-edge-action', body: {
      'id': pendingId,
      'action': 'reject',
    });
  }

  /// Approve/reject email pending item via /api/email-action.
  Future<ApiResult<dynamic>> approveEmail(int pendingId) async {
    return post('/api/email-action', body: {
      'id': pendingId,
      'action': 'approve',
    });
  }

  Future<ApiResult<dynamic>> rejectEmail(int pendingId) async {
    return post('/api/email-action', body: {
      'id': pendingId,
      'action': 'reject',
    });
  }

  /// Approve/reject WhatsApp pending item via /api/whatsapp-action.
  Future<ApiResult<dynamic>> approveWhatsApp(int pendingId) async {
    return post('/api/whatsapp-action', body: {
      'id': pendingId,
      'action': 'approve',
    });
  }

  Future<ApiResult<dynamic>> rejectWhatsApp(int pendingId) async {
    return post('/api/whatsapp-action', body: {
      'id': pendingId,
      'action': 'reject',
    });
  }

  /// Approve/reject call pending item via /api/call-action.
  Future<ApiResult<dynamic>> approveCall(int pendingId) async {
    return post('/api/call-action', body: {
      'id': pendingId,
      'action': 'approve',
    });
  }

  Future<ApiResult<dynamic>> rejectCall(int pendingId) async {
    return post('/api/call-action', body: {
      'id': pendingId,
      'action': 'reject',
    });
  }

  /// Submit a clarification answer via /api/clarification.
  Future<ApiResult<dynamic>> submitClarification(
      String shortcode, String answer) async {
    return post('/api/clarification', body: {
      'shortcode': shortcode,
      'answer': answer,
    });
  }

  // ── Fetch pending items (composite) ───────────────────────────

  /// Fetches pending graph nodes from /api/pending-graph-nodes.
  Future<ApiResult<List<PendingDecision>>> fetchPendingGraphNodes() async {
    final result = await _get('/api/pending-graph-nodes');
    if (!result.success || result.data is! Map) {
      return ApiResult.ok([]);
    }
    final items = (result.data as Map)['data'] as List? ?? [];
    final decisions = items.map((n) {
      final node = n as Map<String, dynamic>;
      final label = node['label'] as String? ?? 'Untitled';
      final nodeType = node['type'] as String? ?? 'person';
      final status = node['status'] as String? ?? 'pending';
      final conf = (node['confidence'] as num?)?.toDouble();
      final ctx = node['eval_context'] as Map<String, dynamic>?;

      String title = label;
      String? description;
      if (nodeType == 'concept' && ctx != null) {
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
        source: 'graph_node',
        title: title,
        description: description,
        confidence: conf,
        raw: node,
      );
    }).toList();
    return ApiResult.ok(decisions);
  }

  /// Fetches pending graph edges from /api/pending-graph-edges.
  Future<ApiResult<List<PendingDecision>>> fetchPendingGraphEdges() async {
    final result = await _get('/api/pending-graph-edges');
    if (!result.success || result.data is! Map) {
      return ApiResult.ok([]);
    }
    final items = (result.data as Map)['data'] as List? ?? [];
    final decisions = items.map((e) {
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
    return ApiResult.ok(decisions);
  }

  /// Fetches ALL pending decisions from all sources.
  Future<ApiResult<List<PendingDecision>>> getPendingDecisions() async {
    // Fire all three requests concurrently, then await.
    final nodeFut = fetchPendingGraphNodes();
    final edgeFut = fetchPendingGraphEdges();
    final msgFut = getMessages(limit: 50);

    final nodeResult = await nodeFut;
    final edgeResult = await edgeFut;
    final msgResult = await msgFut;

    final decisions = <PendingDecision>[];

    if (nodeResult.success) {
      decisions.addAll(nodeResult.data!);
    }
    if (edgeResult.success) {
      decisions.addAll(edgeResult.data!);
    }
    if (msgResult.success) {
      for (final m in msgResult.data!) {
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
        } else {
          continue;
        }

        decisions.add(PendingDecision(
          id: m['id'].toString(),
          source: decisionSource,
          title: m['content'] as String? ?? 'Untitled',
          description: 'via $decisionSource',
          raw: m,
        ));
      }
    }

    // Sort: graph nodes first, then edges, then channel items
    decisions.sort((a, b) {
      const order = ['graph_node', 'graph_edge', 'email', 'call', 'whatsapp'];
      final ai = order.indexOf(a.source);
      final bi = order.indexOf(b.source);
      return ai.compareTo(bi);
    });

    return ApiResult.ok(decisions);
  }  // ── Briefing ──────────────────────────────────────────────

  /// Fetches the structured briefing from /api/briefing.
  Future<BriefingResponse> getBriefing() async {
    final result = await _get('/api/briefing');
    if (result.success && result.data is Map) {
      return BriefingResponse.fromJson(result.data as Map<String, dynamic>);
    }
    return BriefingResponse.empty();
  }

  // ── Config access ────────────────────────────────────────────

  ApiConfig get config => _config;
}

