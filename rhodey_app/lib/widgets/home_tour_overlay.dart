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
///
/// Design language: the callouts reuse the home screen's *focal card*
/// grammar — warm `surfaceAlt` fill, hairline accent border, a vertical accent
/// bar + mono label header, clean PlusJakartaSans type — so the tour reads as
/// part of the app, not a system dialog. The fill is OPAQUE on purpose: this
/// overlay renders outside the home Scaffold, and a translucent card would let
/// the home content show straight through (accentBg is a 15% wash for tinting,
/// not for readable card bodies). A small tail points at the element each
/// page explains. Motion is the app's warm fade + gentle rise.
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
  ///
  /// `tailUp` — does the element sit above the callout (tail on the top edge)?
  /// `tailAlign` — horizontal placement of the tail so it actually points at
  /// the element: -1 = left edge of the card, 0 = center, 1 = right edge.
  List<_TourStep> get _pages => [
    _TourStep(
      icon: Icons.menu,
      title: 'Menu',
      body: 'Settings, help, and more.',
      tailUp: true,
      tailAlign: -0.75,
    ),
    _TourStep(
      icon: Icons.radar,
      title: PersonaStore.current.pulse,
      body:
          'The time of day and what it\'s checking in on — morning '
          'check, wrap-up, weekend. It changes as the day does.',
      tailUp: true,
      tailAlign: 0,
    ),
    _TourStep(
      icon: Icons.checklist_rtl,
      title: 'Your open tasks',
      body:
          'The number here is your open-task count. Tap it to see the '
          'full board in chat.',
      tailUp: true,
      tailAlign: 0.75,
    ),
    _TourStep(
      icon: Icons.waving_hand_outlined,
      title: 'Your tiny brief',
      body:
          'A greeting plus one line of what Rhodey is thinking about '
          'for you today. Short on purpose — the brief, not the whole '
          'story.',
      tailUp: true,
      tailAlign: 0,
    ),
    _TourStep(
      icon: Icons.push_pin_outlined,
      title: PersonaStore.current.focal,
      body:
          'The one thing Rhodey thinks matters most right now. Tap it '
          'to act, or say "Not now" to defer it.',
      tailUp: true,
      tailAlign: 0,
    ),
    _TourStep(
      icon: Icons.chat_bubble_outline,
      title: 'Talk to Rhodey',
      body:
          'Type here — or use voice. Tasks, notes, and questions all '
          'start right here.',
      tailUp: false,
      tailAlign: 0,
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
    // The home screen renders this overlay as a SIBLING of its Scaffold (in
    // the root Stack), so the tour subtree has no Material ancestor of its
    // own. Without one, every Text here would inherit Flutter's debug
    // fallback — monospace with a double YELLOW underline (Material is what
    // supplies the theme's DefaultTextStyle). `Material` (transparency)
    // anchors normal text theming and gives InkWells a Material to splash
    // on; the explicit body style below is the nearest DefaultTextStyle, so
    // it wins regardless of what the app theme does.
    return Material(
      type: MaterialType.transparency,
      child: DefaultTextStyle(
        style: AppTheme.body,
        child: Stack(
          children: [
            // Warm charcoal scrim — tap anywhere advances (or dismisses on
            // the last page). Opaque enough that the home content behind
            // reads as clearly secondary, light enough that the header
            // elements the tails point at stay visible.
            Positioned.fill(
              child: GestureDetector(
                behavior: HitTestBehavior.opaque,
                onTap: _advance,
                child: Container(
                  color: AppTheme.background.withValues(alpha: 0.7),
                ),
              ),
            ),
            // The callout card for the current step — positioned per page so the
            // tail always points at the element it explains.
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
                      borderRadius: BorderRadius.circular(
                        AppTheme.controlRadius,
                      ),
                      onTap: _advance,
                      child: Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 30,
                          vertical: 12,
                        ),
                        child: Text(
                          isLast ? 'Got it' : 'Next',
                          style: TextStyle(
                            color: Theme.of(context).colorScheme.onPrimary,
                            fontFamily: 'PlusJakartaSans',
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
        ),
      ),
    );
  }

  /// Positions the callout near the element it describes for each page.
  Widget _positionedCallout(_TourStep step) {
    final callout = AnimatedSwitcher(
      duration: AppTheme.motionBase,
      switchInCurve: AppTheme.motionCurve,
      switchOutCurve: Curves.easeIn,
      transitionBuilder: (child, animation) {
        // Warm fade + gentle rise — the app's calm entrance motion.
        final rise = Tween(
          begin: const Offset(0, 0.05),
          end: Offset.zero,
        ).animate(animation);
        return FadeTransition(
          opacity: animation,
          child: SlideTransition(position: rise, child: child),
        );
      },
      child: KeyedSubtree(
        key: ValueKey(_page),
        child: _Callout(
          step: step,
          pageIndex: _page,
          totalPages: _pages.length,
          onTap: _advance,
        ),
      ),
    );

    switch (_page) {
      case 0: // Menu — top-left, under the header.
        return Positioned(top: 62, left: 16, right: 16, child: callout);
      case 1: // Pulse label — upper-center, under the header.
        return Positioned(top: 62, left: 60, right: 60, child: callout);
      case 2: // Task count — top-right, under the header.
        return Positioned(top: 62, right: 16, left: 60, child: callout);
      case 3: // Tiny brief — upper body.
        return Positioned(top: 138, left: 16, right: 16, child: callout);
      case 4: // Focal card — mid-lower body.
        return Positioned(bottom: 130, left: 16, right: 16, child: callout);
      default: // Input bar — just above the controls, tail points down.
        return Positioned(bottom: 116, left: 16, right: 16, child: callout);
    }
  }
}

class _TourStep {
  final IconData icon;
  final String title;
  final String body;
  final bool tailUp;
  final double tailAlign;

  const _TourStep({
    required this.icon,
    required this.title,
    required this.body,
    required this.tailUp,
    required this.tailAlign,
  });
}

class _Callout extends StatelessWidget {
  final _TourStep step;
  final int pageIndex;
  final int totalPages;
  final VoidCallback onTap;

  const _Callout({
    required this.step,
    required this.pageIndex,
    required this.totalPages,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          // Tail — points at the element this card explains.
          if (step.tailUp)
            Align(
              alignment: Alignment(step.tailAlign, 0),
              child: const _CalloutTail(up: true),
            ),
          // Card body — focal-card grammar: warm surfaceAlt fill, hairline
          // accent border, accent bar + mono label header, clean sans type.
          // The fill must stay OPAQUE so the home content behind the scrim
          // can never bleed through the card.
          Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: AppTheme.surfaceAlt,
              borderRadius: BorderRadius.circular(AppTheme.cardRadius),
              border: Border.all(
                color: AppTheme.accent.withValues(alpha: 0.3),
                width: 1,
              ),
              boxShadow: const [
                BoxShadow(
                  color: Colors.black38,
                  blurRadius: 16,
                  offset: Offset(0, 4),
                ),
              ],
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    // Vertical accent bar + mono "STEP N OF 6" label — the
                    // same ledger notation the focal card uses for FOCUS.
                    Container(
                      width: 3,
                      height: 14,
                      decoration: BoxDecoration(
                        color: AppTheme.accent,
                        borderRadius: BorderRadius.circular(1),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        'STEP ${pageIndex + 1} OF $totalPages',
                        style: AppTheme.monoLabel.copyWith(
                          color: AppTheme.accent,
                          fontSize: 9,
                          letterSpacing: 1.5,
                        ),
                      ),
                    ),
                    Icon(step.icon, color: AppTheme.accent, size: 18),
                  ],
                ),
                const SizedBox(height: 10),
                Text(
                  step.title,
                  style: AppTheme.body.copyWith(
                    fontSize: 14,
                    fontWeight: FontWeight.w600,
                    height: 1.3,
                  ),
                ),
                const SizedBox(height: 5),
                Text(
                  step.body,
                  style: AppTheme.bodySmall.copyWith(
                    color: AppTheme.textSecondary,
                    fontSize: 12,
                    height: 1.45,
                  ),
                ),
              ],
            ),
          ),
          if (!step.tailUp)
            Align(
              alignment: Alignment(step.tailAlign, 0),
              child: const _CalloutTail(up: false),
            ),
        ],
      ),
    );
  }
}

/// A small triangular tail that visually connects the callout card to the
/// element it explains. Matches the card's fill and hairline border.
class _CalloutTail extends StatelessWidget {
  final bool up;

  const _CalloutTail({required this.up});

  @override
  Widget build(BuildContext context) {
    return CustomPaint(
      size: const Size(14, 8),
      painter: _TailPainter(up: up),
    );
  }
}

class _TailPainter extends CustomPainter {
  final bool up;

  _TailPainter({required this.up});

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width;
    final h = size.height;
    final fill = Paint()
      ..color = AppTheme.surfaceAlt
      ..style = PaintingStyle.fill;
    final stroke = Paint()
      ..color = AppTheme.accent.withValues(alpha: 0.3)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1;

    final path = Path();
    if (up) {
      // Triangle pointing up (base at bottom, apex at top center).
      path.moveTo(0, h);
      path.lineTo(w / 2, 0);
      path.lineTo(w, h);
    } else {
      // Triangle pointing down (base at top, apex at bottom center).
      path.moveTo(0, 0);
      path.lineTo(w / 2, h);
      path.lineTo(w, 0);
    }
    path.close();
    canvas.drawPath(path, fill);
    canvas.drawPath(path, stroke);
  }

  @override
  bool shouldRepaint(_TailPainter oldDelegate) => oldDelegate.up != up;
}
