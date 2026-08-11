/// Regression tests for the "Show me around" home tour overlay (M14).
///
/// Two user-visible bugs prompted these:
///  1. Every Text in the tour rendered with Flutter's DEBUG FALLBACK text
///     style (monospace + double YELLOW underline). Cause: the home screen
///     renders the overlay as a SIBLING of its Scaffold (root Stack), so the
///     tour subtree had no Material / DefaultTextStyle ancestor — Text then
///     falls back to `DefaultTextStyle.fallback`, whose decoration survives
///     the style merge even though each Text sets its own color/font.
///  2. The callout card was filled with `AppTheme.accentBg` — a 15%-opacity
///     champagne wash — so the home screen text behind the scrim bled
///     straight through the card. The fill must be opaque.
library;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rhodey_app/theme/app_theme.dart';
import 'package:rhodey_app/widgets/home_tour_overlay.dart';

void main() {
  /// Mirrors the REAL hosting: the home screen's build returns a root Stack
  /// with the Scaffold and the tour overlay as SIBLINGS (the overlay is NOT
  /// inside the Scaffold, so it has no Material ancestor of its own).
  Widget host() {
    return MaterialApp(
      theme: AppTheme.themeData,
      home: Stack(
        children: [
          const Scaffold(
            backgroundColor: AppTheme.background,
            body: SafeArea(
              child: Text('Rhodey · greeting and one-line thought'),
            ),
          ),
          HomeTourOverlay(onDismiss: () {}),
        ],
      ),
    );
  }

  /// The RESOLVED text style Flutter actually paints for a tour label —
  /// i.e. after merging the explicit style with the ambient DefaultTextStyle.
  TextStyle? paintedStyle(WidgetTester tester, String label) {
    final render = tester.renderObject<RenderParagraph>(find.text(label));
    return render.text.style;
  }

  testWidgets(
    'tour text never renders with the fallback double-yellow underline',
    (tester) async {
      await tester.pumpWidget(host());

      // The mono "STEP 1 OF 6" header carries an explicit color/font but NO
      // decoration — under the old code the ambient DefaultTextStyle.fallback
      // merged in `decoration: underline (double, yellow)`. Now it must be null.
      expect(
        paintedStyle(tester, 'STEP 1 OF 6')?.decoration,
        isNull,
        reason:
            'fallback DefaultTextStyle would paint a double yellow underline',
      );
      expect(paintedStyle(tester, 'Menu')?.decoration, isNull);
      expect(
        paintedStyle(tester, 'Settings, help, and more.')?.decoration,
        isNull,
      );

      // Advance a few pages — every label stays decoration-free.
      await tester.tap(find.text('Next'));
      await tester.pumpAndSettle();
      expect(paintedStyle(tester, 'STEP 2 OF 6')?.decoration, isNull);
      await tester.tap(find.text('Next'));
      await tester.pumpAndSettle();
      expect(paintedStyle(tester, 'STEP 3 OF 6')?.decoration, isNull);
    },
  );

  testWidgets(
    'callout card has an OPAQUE fill — background cannot bleed through',
    (tester) async {
      await tester.pumpWidget(host());

      // The card body is the nearest Container ancestor of the step label.
      final card = tester.widget<Container>(
        find
            .ancestor(
              of: find.text('STEP 1 OF 6'),
              matching: find.byType(Container),
            )
            .first,
      );
      final deco = card.decoration! as BoxDecoration;
      expect(deco.color, AppTheme.surfaceAlt);
      expect(
        deco.color!.a,
        1.0,
        reason:
            'a translucent fill (accentBg is 15% champagne) lets the home '
            'screen text show straight through the tour card',
      );
    },
  );
}
