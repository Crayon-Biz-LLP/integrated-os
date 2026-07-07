enum DecisionPriority { high, standard, low }

enum DecisionType {
  clarification,
  person,
  edge,
  email,
  whatsapp,
  call,
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
    }
  }
}
