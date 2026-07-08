import 'package:flutter/foundation.dart';

/// Local instrumentation counters for the adaptive home screen.
///
/// Logged to debug console on relevant events. All counters reset on
/// app cold start. Future: POST to /api/telemetry/home-metrics.
class HomeInstrumentation {
  int nowCardsShown = 0;
  int inboxBadgeTaps = 0;
  int menuOpens = 0;
  int homeActionsCompleted = 0;
  int dedupSuppressions = 0;
  int itemsDismissed = 0;

  void log() {
    debugPrint('[HomeInstrumentation] NOW=$nowCardsShown '
        'badgeTaps=$inboxBadgeTaps '
        'menuOpens=$menuOpens '
        'actions=$homeActionsCompleted '
        'dedup=$dedupSuppressions '
        'dismissed=$itemsDismissed');
  }

  void reset() {
    nowCardsShown = 0;
    inboxBadgeTaps = 0;
    menuOpens = 0;
    homeActionsCompleted = 0;
    dedupSuppressions = 0;
    itemsDismissed = 0;
  }
}
