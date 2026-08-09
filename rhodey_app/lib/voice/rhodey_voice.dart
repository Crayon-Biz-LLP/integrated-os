/// Rhodey's voice — app-side lines.
///
/// One consistent person across every screen. The backend voice lives in
/// `core/prompts/voice.py` (LLM spec) and `core/lib/rhodey_voice.py` (acks);
/// this file is the app's single source for the hardcoded lines Rhodey says
/// in the UI. Keep it dry, warm, brief — no emoji-shouting, no log-line
/// phrasing, no hedging. If you add a line Rhodey says, put it here first.
library;

class RhodeyVoice {
  RhodeyVoice._();

  /// All clear — one canonical line (was three divergent phrasings across
  /// the home screen). [name] is the tenant's display name (M9.6). While the
  /// name is unknown (first paint before the resolve lands, or a tenant with
  /// no name on file) the name is omitted entirely — we NEVER flash a
  /// hardcoded 'Danny' at another user (M17).
  ///
  /// Phase 2B: [voiceStyle] is the closed-enum token ('direct' | 'calm' |
  /// 'warm') from /api/persona. It shifts only Rhodey's OWN static app line;
  /// server-composed strings (briefing, confirmations) take precedence
  /// everywhere. Unknown/empty style => the standard voice (fail-closed).
  static String allClear([String? name, String voiceStyle = '']) {
    final who = _who(name);
    final base = _allClearBase(voiceStyle);
    return who.isEmpty ? '$base.' : '$base, $who.';
  }

  /// All clear with an open prompt (empty-conversation state).
  static String allClearPrompt([String? name, String voiceStyle = '']) {
    final who = _who(name);
    final base = _allClearBase(voiceStyle);
    return who.isEmpty
        ? "$base. What's on your mind?"
        : '$base, $who. What\'s on your mind?';
  }

  /// The style-aware stem of the all-clear line. 'direct' and 'warm' are the
  /// only recognized tokens; everything else (including 'calm', which keeps
  /// today's line) falls through to the standard voice.
  static String _allClearBase(String voiceStyle) => switch (voiceStyle) {
        'direct' => "Board's clear",
        'warm' => 'All quiet on the board',
        _ => 'All clear',
      };

  /// M9.6/M17: the tenant's display name, or '' when unknown (never 'Danny').
  static String _who([String? name]) {
    final n = name?.trim() ?? '';
    return n;
  }

  // ── Failure lines — one pattern, no hedging ("will" vs "may") ──

  /// An action didn't complete — the item stays on the board.
  static String couldntComplete() =>
      'I couldn\'t complete that item — it\'s still on the board.';

  /// A deferral didn't stick — the item will come back around.
  static String couldntDefer() => 'I couldn\'t defer that item — it\'ll resurface.';

  /// A rejection didn't go through — the item will come back around.
  static String couldntReject() => 'I couldn\'t reject that item — it\'ll resurface.';

  /// Send failure snackbar.
  static String failedToSend() => 'Failed to send — check your connection.';

  /// Task-ledger intro — the board in one line.
  static String taskLedgerIntro(int active, int snoozed) {
    final base = 'Here\'s your board right now — $active active';
    if (snoozed > 0) return '$base, $snoozed snoozed.';
    return '$base.';
  }

  /// Task-ledger hint under the rows.
  static String taskLedgerHint() => 'Tap any task to make it the focus.';
}
