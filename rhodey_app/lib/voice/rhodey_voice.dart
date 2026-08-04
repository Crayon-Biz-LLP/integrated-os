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
  /// the home screen).
  static String allClear() => 'All clear, Danny.';

  /// All clear with an open prompt (empty-conversation state).
  static String allClearPrompt() => 'All clear, Danny. What\'s on your mind?';

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
