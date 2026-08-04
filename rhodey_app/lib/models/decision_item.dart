enum DecisionPriority { high, standard, low }

enum DecisionType {
  clarification,
  person,
  edge,
  email,
  whatsapp,
  call,
  merge,
}

class DecisionItem {
  final String id;
  final DecisionType type;
  final DecisionPriority priority;
  final String title;
  final String? description;
  final double? confidence;
  final String? contextLabel;
  final DateTime createdAt;
  final Map<String, dynamic> metadata;

  /// The raw node_type from the API (e.g. "person", "organization", "concept") —
  /// used to determine whether to show the context sheet for person approvals.
  final String? nodeType;

  const DecisionItem({
    required this.id,
    required this.type,
    required this.priority,
    required this.title,
    this.description,
    this.confidence,
    this.contextLabel,
    required this.createdAt,
    this.metadata = const {},
    this.nodeType,
  });

  String get urgencyIcon {
    switch (priority) {
      case DecisionPriority.high:
        return '🔴';
      case DecisionPriority.standard:
        return '🟡';
      case DecisionPriority.low:
        return '🟢';
    }
  }

  String get typeIcon {
    switch (type) {
      case DecisionType.clarification:
        return '❓';
      case DecisionType.person:
        return '👤';
      case DecisionType.edge:
        return '🔗';
      case DecisionType.email:
        return '📧';
      case DecisionType.whatsapp:
        return '💬';
      case DecisionType.call:
        return '📞';
      case DecisionType.merge:
        return '🔀';
    }
  }

  /// Whether this item is a person node that needs context input on approve.
  bool get needsPersonContext => type == DecisionType.person && nodeType == 'person';

  /// Whether this item is a merge proposal.
  bool get isMergeProposal => type == DecisionType.merge;

  /// Serializes this item for the on-device inbox cache.
  Map<String, dynamic> toJson() => {
        'id': id,
        'type': type.name,
        'priority': priority.name,
        'title': title,
        'description': description,
        'confidence': confidence,
        'contextLabel': contextLabel,
        'createdAt': createdAt.toIso8601String(),
        'metadata': metadata,
        'nodeType': nodeType,
      };

  /// Rebuilds an item from a cached [json] map. Falls back to safe defaults so
  /// a schema drift in the cache can never crash the inbox render.
  factory DecisionItem.fromJson(Map<String, dynamic> json) {
    DecisionType type = DecisionType.clarification;
    DecisionPriority priority = DecisionPriority.standard;
    try {
      type = DecisionType.values.byName(json['type'] as String? ?? '');
    } catch (_) {}
    try {
      priority = DecisionPriority.values.byName(json['priority'] as String? ?? '');
    } catch (_) {}
    return DecisionItem(
      id: json['id'] as String? ?? '',
      type: type,
      priority: priority,
      title: json['title'] as String? ?? '',
      description: json['description'] as String?,
      confidence: (json['confidence'] as num?)?.toDouble(),
      contextLabel: json['contextLabel'] as String?,
      createdAt: DateTime.tryParse(json['createdAt'] as String? ?? '') ??
          DateTime.now(),
      metadata: (json['metadata'] as Map?)?.cast<String, dynamic>() ?? const {},
      nodeType: json['nodeType'] as String?,
    );
  }
}
