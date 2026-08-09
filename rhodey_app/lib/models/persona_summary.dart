/// Phase 2B: the per-tenant persona surface summary served by GET /api/persona
/// and embedded in /api/home-feed.
///
/// Closed-enum transport (R4): the app receives ONLY display_name +
/// voice_style + signoffs — never the raw card, curated people names, or
/// never-topics. `voice_style` is the server-derived closed enum token
/// ('direct' | 'calm' | 'warm'); anything else (or empty) means "standard
/// voice", which is fail-closed: no card => the app renders today's copy.
///
/// `signoffs` is parsed but RESERVED — no surface renders it yet; it is
/// shipped so future copy (e.g. sign-off lines) can use it without a server
/// contract change.
class PersonaSummary {
  final String displayName;
  final String voiceStyle;
  final List<String> signoffs;

  const PersonaSummary({
    required this.displayName,
    required this.voiceStyle,
    required this.signoffs,
  });

  factory PersonaSummary.fromJson(Map<String, dynamic> json) => PersonaSummary(
        displayName: (json['display_name'] as String?)?.trim() ?? '',
        voiceStyle: (json['voice_style'] as String?)?.trim() ?? '',
        signoffs:
            List<String>.from((json['signoffs'] as List<dynamic>?) ?? []),
      );

  static const empty =
      PersonaSummary(displayName: '', voiceStyle: '', signoffs: []);

  bool get isEmpty => displayName.isEmpty && voiceStyle.isEmpty;
}
