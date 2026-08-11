/// Regression tests for the briefing Moment card (`_BriefingInlineCard`).
///
/// The Aug-10 pulse change made the server emit `**bold**` section headers
/// (e.g. "**Work**") instead of emoji-prefixed ones. The old section parser
/// only recognised "🚀 Work" style headers, so a briefing with bold headers
/// rendered the whole body TWICE (once as the white collapsed intro, once as
/// the dull full-text fallback) and showed `**Work**` literally. These tests
/// pin: bold renders as bold (never literal asterisks) and the briefing body
/// renders exactly once in every shape.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rhodey_app/theme/app_theme.dart';
import 'package:rhodey_app/widgets/rich_card_content.dart';

/// Depth-first walk of a TextSpan tree looking for a span whose text is
/// exactly [text] (e.g. a bold run that the parser split out).
bool _hasSpan(InlineSpan? span, String text, {required bool bold}) {
  if (span is TextSpan) {
    if (span.text == text) {
      final w = span.style?.fontWeight;
      if (bold && w == FontWeight.w700) return true;
      if (!bold && w != FontWeight.w700) return true;
    }
    for (final child in span.children ?? const <InlineSpan>[]) {
      if (_hasSpan(child, text, bold: bold)) return true;
    }
  }
  return false;
}

/// Like [_hasSpan] but matches spans whose text merely CONTAINS [text]
/// (bullets keep their "- " prefix).
bool _hasSpanContaining(InlineSpan? span, String text, {required bool bold}) {
  if (span is TextSpan && (span.text ?? '').contains(text)) {
    final w = span.style?.fontWeight;
    if (bold && w == FontWeight.w700) return true;
    if (!bold && w != FontWeight.w700) return true;
  }
  for (final child
      in (span is TextSpan ? span.children : null) ?? const <InlineSpan>[]) {
    if (_hasSpanContaining(child, text, bold: bold)) return true;
  }
  return false;
}

Widget _host(CardData card) {
  return MaterialApp(
    theme: AppTheme.themeData,
    home: Scaffold(
      body: SingleChildScrollView(child: RichCardContent(cardData: card)),
    ),
  );
}

void main() {
  // Today's briefing (no timestamp) — opens expanded so the body renders.
  const briefing =
      '☀️ Morning check-in\n'
      'A quick line about today.\n'
      '\n'
      '**Work**\n'
      '- Call the client about the quote\n'
      '**Home**\n'
      '- Buy groceries';

  testWidgets('bold headers render as bold — no literal ** anywhere', (
    tester,
  ) async {
    await tester.pumpWidget(
      _host(
        const CardData(
          type: CardType.briefing,
          title: '☀️ Morning check-in',
          fullText: briefing,
        ),
      ),
    );

    expect(
      find.textContaining('**', findRichText: true),
      findsNothing,
      reason: 'the server bold marker must never render literally',
    );

    final richTexts = tester.widgetList<RichText>(find.byType(RichText));
    expect(
      richTexts.any((rt) => _hasSpan(rt.text, 'Work', bold: true)),
      isTrue,
      reason: '**Work** must paint as a bold "Work" span',
    );
    // The bullet keeps its "- " prefix — check by substring.
    expect(
      richTexts.any(
        (rt) => _hasSpanContaining(
          rt.text,
          'Call the client about the quote',
          bold: false,
        ),
      ),
      isTrue,
    );
  });

  testWidgets('briefing body renders exactly ONCE for bold-header briefings', (
    tester,
  ) async {
    await tester.pumpWidget(
      _host(
        const CardData(
          type: CardType.briefing,
          title: '☀️ Morning check-in',
          fullText: briefing,
        ),
      ),
    );

    // Pre-fix: the whole body was rendered twice (intro + fullText fallback).
    expect(
      find.textContaining(
        'Call the client about the quote',
        findRichText: true,
      ),
      findsOneWidget,
    );
    expect(
      find.textContaining('A quick line about today.', findRichText: true),
      findsOneWidget,
    );
    expect(
      find.textContaining('Buy groceries', findRichText: true),
      findsOneWidget,
    );
  });

  testWidgets('legacy emoji-prefixed headers still group into sections',
      (tester) async {
    // Old server format (existing history briefings) — the parser change
    // must not regress it: headers still split sections, body renders once.
    await tester.pumpWidget(_host(const CardData(
      type: CardType.briefing,
      title: '☀️ Morning check-in',
      fullText: '☀️ Morning check-in\nA quick line.\n'
          '🚀 Work\n- Call the client\n🏠 Home\n- Buy groceries',
    )));

    expect(find.textContaining('Call the client', findRichText: true),
        findsOneWidget);
    expect(find.textContaining('Buy groceries', findRichText: true),
        findsOneWidget);
    expect(find.textContaining('A quick line.', findRichText: true),
        findsOneWidget,
        reason: 'emoji headers must be recognised so intro stays the intro');
  });

  testWidgets('plain-text briefing (no section headers) renders once too', (
    tester,
  ) async {
    await tester.pumpWidget(
      _host(
        const CardData(
          type: CardType.briefing,
          title: 'Quiet day',
          fullText: 'Quiet day\nNothing actionable — just a status line.',
        ),
      ),
    );

    // NOTE: 'Quiet day' legitimately appears twice (card title = first line).
    expect(
      find.textContaining('Nothing actionable', findRichText: true),
      findsOneWidget,
      reason: 'no-headers briefings must not duplicate via intro+fullText',
    );
  });
}
