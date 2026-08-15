/// Golden snapshot tests — the VISUAL contract for the briefing card.
///
/// The behavioral tests (briefing_card_test.dart) walk the TextSpan tree;
/// these pin the rendered PIXELS. The Aug-10 briefing bug class (server
/// switched to `**bold**` headers → body rendered twice + literal asterisks)
/// is exactly what a golden diff catches that span-walking can miss: any
/// layout/rendering change to these cards is now a reviewed golden update
/// (flutter test --update-goldens), never a silent regression.
///
/// Regenerate after an INTENTIONAL visual change:
///     cd rhodey_app && flutter test --update-goldens test/goldens/
///
/// Note: widget goldens render with the test framework's default Ahem font
/// (solid blocks) — they pin LAYOUT/STRUCTURE, not typography. That is the
/// stable, CI-safe contract; real-font rendering is an integration_test
/// concern (deferred — needs a device, see plans/75 §19 X8).
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rhodey_app/theme/app_theme.dart';
import 'package:rhodey_app/widgets/rich_card_content.dart';

/// The exact Aug-10 briefing shape that broke: `**bold**` section headers,
/// bullets — rendered in a phone-sized surface.
///
/// Emoji determinism: the cards hardcode emoji glyphs (task icon 📋, the
/// briefing time chip ☀️). The test engine resolves emoji via the host's
/// emoji font (Noto Color Emoji on Linux CI, nothing equivalent exposed on
/// macOS), which made goldens platform-dependent. The surface below forces
/// `FlutterTest` (Ahem) as the explicit font fallback, so every emoji
/// renders as a deterministic solid block on ALL platforms. Keep golden
/// fixtures ASCII-only — text set through explicit AppTheme styles (e.g. the
/// card title) cannot be pinned from outside the widget.

// ASCII-only by contract (see the emoji-determinism note): a title ☀️ would
// render through an explicit AppTheme style that the surface fallback cannot
// pin. The time chip still shows ☀️ (widget-hardcoded, inherited style).
const _briefing =
    'Morning check-in\n'
    'A quick line about today.\n'
    '\n'
    '**Work**\n'
    '- Call the client about the quote\n'
    '- Send the Q3 deck\n'
    '\n'
    '**Home**\n'
    '- Buy groceries\n'
    '\n'
    '**Done**\n'
    '- Filed the lease renewal';

Widget _surface(Widget child) {
  return MaterialApp(
    theme: AppTheme.themeData,
    home: Scaffold(
      body: DefaultTextStyle(
        // Pin every emoji glyph to the test's Ahem font (solid blocks) so the
        // golden is byte-identical on macOS and Linux CI — see the note above.
        style: const TextStyle(fontFamilyFallback: ['FlutterTest']),
        child: SizedBox(
          width: 360,
          height: 640,
          child: SingleChildScrollView(child: child),
        ),
      ),
    ),
  );
}

void main() {
  testWidgets('briefing card golden — bold-header shape (Aug-10 bug class)', (
    tester,
  ) async {
    await tester.pumpWidget(
      _surface(
        RichCardContent(
          cardData: const CardData(
            type: CardType.briefing,
            title: 'Morning check-in',
            fullText: _briefing,
          ),
        ),
      ),
    );
    // Pre-fix this golden would show the body twice + literal ** — the diff
    // against the committed snapshot is the regression alarm. Golden paths
    // are relative to the test file, so the PNG sits next to this file.
    await expectLater(
      find.byType(RichCardContent),
      matchesGoldenFile('briefing_card.png'),
    );
  });

  testWidgets('task card golden — ack verb shape', (tester) async {
    await tester.pumpWidget(
      _surface(
        RichCardContent(
          cardData: parseMessageToCardData(
            '✅ Rescheduled: Purchase the Ashraya domain → Aug 20, 2026',
          )!,
        ),
      ),
    );
    await expectLater(
      find.byType(RichCardContent),
      matchesGoldenFile('task_ack_card.png'),
    );
  });
}
