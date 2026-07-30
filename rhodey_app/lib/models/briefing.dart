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

  /// Pulse intelligence: true if pulse flagged this as overdue/stale
  final bool isStale;

  const BriefingItem({
    required this.icon,
    required this.text,
    required this.status,
    this.decisionId,
    this.decisionType,
    this.isStale = false,
  });

  factory BriefingItem.fromJson(Map<String, dynamic> json) {
    return BriefingItem(
      icon: json['icon'] as String? ?? '📝',
      text: json['text'] as String? ?? '',
      status: json['status'] as String? ?? 'note',
      decisionId: json['decision_id'] as String?,
      decisionType: json['decision_type'] as String?,
      isStale: json['is_stale'] as bool? ?? false,
    );
  }

  bool get isUrgent => status == 'urgent';
  bool get isPending => status == 'pending';
  bool get isDecision => decisionId != null && decisionType != null;

  Map<String, dynamic> toJson() => {
        'icon': icon,
        'text': text,
        'status': status,
        'decision_id': decisionId,
        'decision_type': decisionType,
        'is_stale': isStale,
      };
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

  Map<String, dynamic> toJson() => {
        'id': id,
        'title': title,
        'items': items.map((e) => e.toJson()).toList(),
      };
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

  Map<String, dynamic> toJson() => {
        'time': time,
        'input': input,
        'resolution': resolution,
      };
}

/// A catch-up delta item — what changed since the user was last active.
class DeltaItem {
  final String icon;   // "🆕", "✅", "🔗", "👤"
  final String text;
  final String time;   // "2m ago", "1h ago"

  const DeltaItem({
    required this.icon,
    required this.text,
    required this.time,
  });

  factory DeltaItem.fromJson(Map<String, dynamic> json) {
    return DeltaItem(
      icon: json['icon'] as String? ?? '📝',
      text: json['text'] as String? ?? '',
      time: json['time'] as String? ?? '',
    );
  }

  Map<String, dynamic> toJson() => {
        'icon': icon,
        'text': text,
        'time': time,
      };
}

/// A wrap item — completed today or rolling to tomorrow.
class WrapItem {
  final String icon;   // "✅", "📋"
  final String text;   // Task title
  final String detail; // Detail line: time ago, priority, etc.

  const WrapItem({
    required this.icon,
    required this.text,
    required this.detail,
  });

  factory WrapItem.fromJson(Map<String, dynamic> json) {
    return WrapItem(
      icon: json['icon'] as String? ?? '📝',
      text: json['text'] as String? ?? '',
      detail: json['detail'] as String? ?? '',
    );
  }

  Map<String, dynamic> toJson() => {
        'icon': icon,
        'text': text,
        'detail': detail,
      };
}

class BriefingResponse {
  final String greeting;
  final String? nextEvent;
  final List<BriefingSection> sections;
  final int pendingCount;
  final List<TraceItem> traces;
  final String? latestResponse; // Most recent bot response text

  // Pulse intelligence fields
  final String? contextBar;      // "Closing the loop — clear banking before sign-off"
  final String? voiceLine;       // Rhodey's voice line (1-2 sentences)
  final String? pulseMode;       // "morning", "afternoon", "weekend", etc.
  final List<String> insights;   // ["🔴 2 stale tasks", "📦 3 vaulted"]
  final int vaultedCount;        // Tasks hidden by pulse vault

  // Home screen mode (drives Flutter layout)
  final String homeMode;         // "proceed" | "decide" | "sprint" | "catch_up" | "wrap"
  final int vaultedUrgentCount;  // Vaulted urgent tasks (shown as 🔴 count)
  final int vaultedHighCount;    // Vaulted high-priority tasks (shown as 🟡 count)
  // Catch-up delta items
  final List<DeltaItem> deltaItems; // What changed since last session
  // Wrap mode: completed today + rolling tasks
  final List<WrapItem> wrapDoneToday;
  final List<WrapItem> wrapRolling;

  const BriefingResponse({
    required this.greeting,
    this.nextEvent,
    required this.sections,
    this.pendingCount = 0,
    this.traces = const [],
    this.latestResponse,
    this.contextBar,
    this.voiceLine,
    this.pulseMode,
    this.insights = const [],
    this.vaultedCount = 0,
    this.homeMode = 'proceed',
    this.vaultedUrgentCount = 0,
    this.vaultedHighCount = 0,
    this.deltaItems = const [],
    this.wrapDoneToday = const [],
    this.wrapRolling = const [],
  });

  factory BriefingResponse.fromJson(Map<String, dynamic> json) {
    final rawSections = json['sections'] as List<dynamic>? ?? [];
    final rawTraces = json['traces'] as List<dynamic>? ?? [];
    final rawInsights = json['insights'] as List<dynamic>? ?? [];
    final rawDelta = json['delta_items'] as List<dynamic>? ?? [];
    final rawDoneToday = json['wrap_done_today'] as List<dynamic>? ?? [];
    final rawRolling = json['wrap_rolling'] as List<dynamic>? ?? [];
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
      latestResponse: json['latest_response'] as String?,
      contextBar: json['context_bar'] as String?,
      voiceLine: json['voice_line'] as String?,
      pulseMode: json['pulse_mode'] as String?,
      insights: rawInsights.whereType<String>().toList(),
      vaultedCount: json['vaulted_count'] as int? ?? 0,
      homeMode: json['home_mode'] as String? ?? 'proceed',
      vaultedUrgentCount: json['vaulted_urgent_count'] as int? ?? 0,
      vaultedHighCount: json['vaulted_high_count'] as int? ?? 0,
      deltaItems: rawDelta
          .map((e) => DeltaItem.fromJson(e as Map<String, dynamic>))
          .toList(),
      wrapDoneToday: rawDoneToday
          .map((e) => WrapItem.fromJson(e as Map<String, dynamic>))
          .toList(),
      wrapRolling: rawRolling
          .map((e) => WrapItem.fromJson(e as Map<String, dynamic>))
          .toList(),
    );
  }

  /// Empty briefing (e.g. on error or initial load)
  static BriefingResponse empty() => const BriefingResponse(
        greeting: 'Hey.',
        sections: [],
      );

  Map<String, dynamic> toJson() => {
        'greeting': greeting,
        'next_event': nextEvent,
        'sections': sections.map((e) => e.toJson()).toList(),
        'pending_count': pendingCount,
        'traces': traces.map((e) => e.toJson()).toList(),
        'latest_response': latestResponse,
        'context_bar': contextBar,
        'voice_line': voiceLine,
        'pulse_mode': pulseMode,
        'insights': insights,
        'vaulted_count': vaultedCount,
        'home_mode': homeMode,
        'vaulted_urgent_count': vaultedUrgentCount,
        'vaulted_high_count': vaultedHighCount,
        'delta_items': deltaItems.map((e) => e.toJson()).toList(),
        'wrap_done_today': wrapDoneToday.map((e) => e.toJson()).toList(),
        'wrap_rolling': wrapRolling.map((e) => e.toJson()).toList(),
      };
}
