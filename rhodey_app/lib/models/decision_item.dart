enum DecisionPriority { high, standard, low }

enum DecisionType {
  clarification,
  person,
  edge,
  email,
  whatsapp,
  call,
  teams,
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

  /// Ingest chat key for channel items (metadata.chat_id — the room name or
  /// phone). Lets the reply flow send back to the right chat via Beeper.
  final String? chatId;

  /// True when this channel item arrived via the Beeper bridge (native Matrix
  /// event id present) vs the legacy MacroDroid path. Renders as BEEPER.
  final bool viaBeeper;

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
    this.chatId,
    this.viaBeeper = false,
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
        return personIcon;
      case DecisionType.edge:
        return '🔗';
      case DecisionType.email:
        return '📧';
      case DecisionType.whatsapp:
        return '💬';
      case DecisionType.call:
        return '📞';
      case DecisionType.teams:
        return '🟣';
      case DecisionType.merge:
        return '🔀';
    }
  }

  /// Badge for a person-type decision, derived from the RAW node type
  /// (person / organization / concept / project / ...). The Inbox used to
  /// label every new node "NEW PERSON" regardless of what it actually was.
  String get personBadge {
    switch (nodeType?.toLowerCase()) {
      case 'organization':
        return 'NEW ORG';
      case 'concept':
        return 'NEW CONCEPT';
      case 'project':
        return 'NEW PROJECT';
      case 'event':
        return 'NEW EVENT';
      case 'place':
        return 'NEW PLACE';
      case 'animal':
        return 'NEW ANIMAL';
      case 'emotional_state':
        return 'NEW STATE';
      case 'practice':
        return 'NEW PRACTICE';
      case 'person':
      default:
        return 'NEW PERSON';
    }
  }

  /// Icon matching [personBadge] — so an org approval shows 🏢, not 👤.
  String get personIcon {
    switch (nodeType?.toLowerCase()) {
      case 'organization':
        return '🏢';
      case 'concept':
        return '💡';
      case 'project':
        return '📦';
      case 'event':
        return '📅';
      case 'place':
        return '📍';
      case 'animal':
        return '🐾';
      case 'emotional_state':
      case 'practice':
        return '🧠';
      case 'person':
      default:
        return '👤';
    }
  }

  /// Whether this item is a person node that needs context input on approve.
  bool get needsPersonContext => type == DecisionType.person && nodeType == 'person';

  /// Whether this item is a merge proposal.
  bool get isMergeProposal => type == DecisionType.merge;

  /// Whether this card can reply directly into the chat (WhatsApp/Beeper
  /// channel items carry a chat key; the send path needs it).
  ///
  /// Beeper-sourced rows always qualify (their chat key is a room name or
  /// phone the bridge has seen). Legacy MacroDroid-era keys may not resolve
  /// to a Matrix room yet — the send then fails with a clear no_room message.
  bool get canReply => type == DecisionType.whatsapp && chatId != null && chatId!.isNotEmpty;

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
        'chatId': chatId,
        'viaBeeper': viaBeeper,
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
      chatId: json['chatId'] as String?,
      viaBeeper: json['viaBeeper'] as bool? ?? false,
    );
  }
}
