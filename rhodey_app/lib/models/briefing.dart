// Models for the structured briefing response from GET /api/briefing.
//
// Sections: briefing (tasks + calendar), decisions (pending items), recent (outcomes),
// traces (paired input→outcome history for Traces view).
// Decisions section is omitted on the API when empty.

class BriefingItem {
  final String icon;   // "⏰", "✅", "🔗", "📝", "⚠️", etc.
  final String text;
  final String status; // "urgent", "active", "pending", "done", "note"

  /// Decision action fields — null for non-decision items.
  final String? decisionId;
  final String? decisionType;
  // "graph_node", "graph_edge", "email", "whatsapp", "call", "merge", "channel"

  const BriefingItem({
    required this.icon,
    required this.text,
    required this.status,
    this.decisionId,
    this.decisionType,
  });

  factory BriefingItem.fromJson(Map<String, dynamic> json) {
    return BriefingItem(
      icon: json['icon'] as String? ?? '📝',
      text: json['text'] as String? ?? '',
      status: json['status'] as String? ?? 'note',
      decisionId: json['decision_id'] as String?,
      decisionType: json['decision_type'] as String?,
    );
  }

  bool get isUrgent => status == 'urgent';
  bool get isPending => status == 'pending';
  bool get isDecision => decisionId != null && decisionType != null;
}

class BriefingSection {
  final String id;    // "briefing", "decisions", "recent"
  final String title; // "Your morning", "Decisions", "Recent"
  final List<BriefingItem> items;

  const BriefingSection({
    required this.id,
    required this.title,
    required this.items,
  });

  factory BriefingSection.fromJson(Map<String, dynamic> json) {
    final rawItems = json['items'] as List<dynamic>? ?? [];
    return BriefingSection(
      id: json['id'] as String? ?? '',
      title: json['title'] as String? ?? '',
      items: rawItems
          .map((e) => BriefingItem.fromJson(e as Map<String, dynamic>))
          .toList(),
    );
  }
}

/// A paired input→outcome trace for the Traces view.
class TraceItem {
  /// Human-readable time: "2m ago", "1h ago"
  final String time;

  /// What the user said/asked (brief)
  final String input;

  /// What happened / outcome
  final String resolution;

  const TraceItem({
    required this.time,
    required this.input,
    required this.resolution,
  });

  factory TraceItem.fromJson(Map<String, dynamic> json) {
    return TraceItem(
      time: json['time'] as String? ?? '',
      input: json['input'] as String? ?? '',
      resolution: json['resolution'] as String? ?? '',
    );
  }
}

class BriefingResponse {
  final String greeting;
  final String? nextEvent;
  final List<BriefingSection> sections;
  final int pendingCount;
  final List<TraceItem> traces;

  const BriefingResponse({
    required this.greeting,
    this.nextEvent,
    required this.sections,
    this.pendingCount = 0,
    this.traces = const [],
  });

  factory BriefingResponse.fromJson(Map<String, dynamic> json) {
    final rawSections = json['sections'] as List<dynamic>? ?? [];
    final rawTraces = json['traces'] as List<dynamic>? ?? [];
    return BriefingResponse(
      greeting: json['greeting'] as String? ?? 'Hey.',
      nextEvent: json['next_event'] as String?,
      sections: rawSections
          .map((e) => BriefingSection.fromJson(e as Map<String, dynamic>))
          .toList(),
      pendingCount: json['pending_count'] as int? ?? 0,
      traces: rawTraces
          .map((e) => TraceItem.fromJson(e as Map<String, dynamic>))
          .toList(),
    );
  }

  /// Empty briefing (e.g. on error or initial load)
  static BriefingResponse empty() => const BriefingResponse(
        greeting: 'Hey.',
        sections: [],
      );
}
