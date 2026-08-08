import 'package:flutter/material.dart';
import '../services/persona.dart';
import '../theme/app_theme.dart';

/// First-run home tour (M14): a paged walkthrough that answers "what is each
/// part of the home screen?" with arrow-annotated callout cards. One element
/// per page (dots + Next), so even six callouts stay readable on a phone:
///   1. Menu            (top-left)
///   2. Pulse label     ("Rhodey · time-of-day" — header center)
///   3. Task count      (top-right)
///   4. The tiny brief  (greeting + one-line Rhodey thought)
///   5. Focal card      (what matters most, bottom-center)
///   6. Chat input bar  (bottom)
/// Tapping the scrim or Next advances; the last page is "Got it". Dismissing
/// marks the tour as seen (handled by the caller via onDismiss).
class HomeTourOverlay extends StatefulWidget {
  final VoidCallback onDismiss;

  const HomeTourOverlay({super.key, required this.onDismiss});

  @override
  State<HomeTourOverlay> createState() => _HomeTourOverlayState();
}

class _HomeTourOverlayState extends State<HomeTourOverlay> {
  int _page = 0;

  /// M15: persona-aware titles for the pulse + focal pages (e.g. "Executive
  /// check-in" for Chief of Staff, "Don't forget" for Life & Household);
  /// the four shared pages keep today's copy.
  List<_TourStep> get _pages => [
        _TourStep(
          icon: Icons.menu,
          title: 'Menu',
          body: 'Settings, help, and more.',
          arrow: Icons.arrow_downward,
        ),
        _TourStep(
          icon: Icons.radar,
          title: PersonaStore.current.pulse,
          body: 'The time of day and what it\'s checking in on — morning '
              'check, wrap-up, weekend. It changes as the day does.',
          arrow: Icons.arrow_downward,
        ),
        _TourStep(
          icon: Icons.checklist_rtl,
          title: 'Your open tasks',
          body: 'The number here is your open-task count. Tap it to see the '
              'full board in chat.',
          arrow: Icons.arrow_downward,
        ),
        _TourStep(
          icon: Icons.waving_hand_outlined,
          title: 'Your tiny brief',
          body: 'A greeting plus one line of what Rhodey is thinking about '
              'for you today. Short on purpose — the brief, not the whole '
              'story.',
          arrow: Icons.arrow_downward,
        ),
        _TourStep(
          icon: Icons.push_pin_outlined,
          title: PersonaStore.current.focal,
          body: 'The one thing Rhodey thinks matters most right now. Tap it '
              'to act, or say "Not now" to defer it.',
          arrow: Icons.arrow_downward,
        ),
        _TourStep(
          icon: Icons.chat_bubble_outline,
          title: 'Talk to Rhodey',
          body: 'Type here — or use voice. Tasks, notes, and questions all '
              'start right here.',
          arrow: Icons.arrow_upward,
        ),
      ];

  void _advance() {
    if (_page >= _pages.length - 1) {
      widget.onDismiss();
      return;
    }
    setState(() => _page++);
  }

  @override
  Widget build(BuildContext context) {
    final step = _pages[_page];
    final isLast = _page == _pages.length - 1;
    return Stack(
      children: [
        // Scrim — tap anywhere advances (or dismisses on the last page).
        Positioned.fill(
          child: GestureDetector(
            behavior: HitTestBehavior.opaque,
            onTap: _advance,
            child: Container(
              color: Colors.black.withValues(alpha: 0.62),
            ),
          ),
        ),
        // The callout card for the current step — positioned per page so the
        // arrow always points at the element it explains.
        _positionedCallout(step),
        // Bottom controls: page dots + Next / Got it.
        Positioned(
          bottom: 20,
          left: 0,
          right: 0,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: List.generate(_pages.length, (i) {
                  final active = i == _page;
                  return AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    margin: const EdgeInsets.symmetric(horizontal: 3),
                    width: active ? 16 : 6,
                    height: 6,
                    decoration: BoxDecoration(
                      color: active
                          ? AppTheme.accent
                          : AppTheme.textTertiary.withValues(alpha: 0.5),
                      borderRadius: BorderRadius.circular(3),
                    ),
                  );
                }),
              ),
              const SizedBox(height: 14),
              Material(
                color: AppTheme.accent,
                borderRadius: BorderRadius.circular(AppTheme.controlRadius),
                child: InkWell(
                  borderRadius: BorderRadius.circular(AppTheme.controlRadius),
                  onTap: _advance,
                  child: Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 30,
                      vertical: 12,
                    ),
                    child: Text(
                      isLast ? 'Got it' : 'Next',
                      style: const TextStyle(
                        color: Color(0xFF161510),
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }

  /// Positions the callout near the element it describes for each page.
  Widget _positionedCallout(_TourStep step) {
    switch (_page) {
      case 0: // Menu — top-left, under the header.
        return Positioned(
          top: 56,
          left: 16,
          right: 16,
          child: _Callout(step: step, onTap: _advance),
        );
      case 1: // Pulse label — upper-center, under the header.
        return Positioned(
          top: 56,
          left: 60,
          right: 60,
          child: _Callout(step: step, onTap: _advance),
        );
      case 2: // Task count — top-right, under the header.
        return Positioned(
          top: 56,
          right: 16,
          left: 60,
          child: _Callout(step: step, onTap: _advance),
        );
      case 3: // Tiny brief — upper body.
        return Positioned(
          top: 130,
          left: 16,
          right: 16,
          child: _Callout(step: step, onTap: _advance),
        );
      case 4: // Focal card — mid-lower body.
        return Positioned(
          bottom: 120,
          left: 16,
          right: 16,
          child: _Callout(step: step, onTap: _advance),
        );
      default: // Input bar — just above the controls, arrow up.
        return Positioned(
          bottom: 110,
          left: 16,
          right: 16,
          child: _Callout(step: step, onTap: _advance),
        );
    }
  }
}

class _TourStep {
  final IconData icon;
  final String title;
  final String body;
  final IconData arrow;

  const _TourStep({
    required this.icon,
    required this.title,
    required this.body,
    required this.arrow,
  });
}

class _Callout extends StatelessWidget {
  final _TourStep step;
  final VoidCallback onTap;

  const _Callout({required this.step, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        constraints: const BoxConstraints(maxWidth: 340),
        padding: const EdgeInsets.all(13),
        decoration: BoxDecoration(
          color: AppTheme.surfaceAlt,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: AppTheme.accent, width: 1.2),
          boxShadow: [
            BoxShadow(
              color: Colors.black.withValues(alpha: 0.35),
              blurRadius: 14,
              offset: const Offset(0, 4),
            ),
          ],
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(step.icon, color: AppTheme.accent, size: 18),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    step.title,
                    style: const TextStyle(
                      color: AppTheme.textPrimary,
                      fontSize: 13.5,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                Icon(step.arrow, color: AppTheme.textTertiary, size: 16),
              ],
            ),
            const SizedBox(height: 5),
            Text(
              step.body,
              style: TextStyle(
                color: AppTheme.textSecondary,
                fontSize: 12,
                height: 1.45,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
