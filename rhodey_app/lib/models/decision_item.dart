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
}
