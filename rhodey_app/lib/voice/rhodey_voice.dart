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
  static String allClear([String? name]) {
    final who = _who(name);
    return who.isEmpty ? 'All clear.' : 'All clear, $who.';
  }

  /// All clear with an open prompt (empty-conversation state).
  static String allClearPrompt([String? name]) {
    final who = _who(name);
    return who.isEmpty
        ? "All clear. What's on your mind?"
        : 'All clear, $who. What\'s on your mind?';
  }

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
