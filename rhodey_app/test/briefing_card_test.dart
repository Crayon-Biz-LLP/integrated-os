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

  testWidgets('**Done** headers split into their own section — never fused '
      'into the intro paragraph', (tester) async {
    // Real briefing (Aug-13 raw_dumps): the engine emits **Done** sections,
    // which the old vocabulary ({work,home,ideas,schedule,vault}) missed —
    // the header AND its bullet were joined into the opening paragraph as a
    // run-on ("…moving. Done - 💼 [UAT] …"), so the user saw no line break
    // or spacing where Telegram had a clean section.
    const withDone = 'Afternoon check.\n'
        'System load is heavy right now with urgent items queued up.\n'
        '\n'
        '**Done**\n'
        '- 💼 [UAT] Weekly sync 912f8d [INBOX]\n'
        '\n'
        '**Work**\n'
        '- 💼 Call Mark [INBOX]';
    await tester.pumpWidget(_host(const CardData(
      type: CardType.briefing,
      title: 'Afternoon check.',
      fullText: withDone,
    )));

    // The Done bullet lives in its own block — never fused into the intro.
    final introTexts = tester
        .widgetList<RichText>(find.byType(RichText))
        .map((rt) => rt.text.toPlainText())
        .where((t) => t.contains('System load is heavy'))
        .toList();
    expect(introTexts, hasLength(1),
        reason: 'intro must stay the pure opening narrative');
    expect(introTexts.single.contains('Weekly sync'), isFalse,
        reason: 'the Done bullet must NOT be joined into the intro paragraph');
    expect(
      find.textContaining('Weekly sync', findRichText: true),
      findsOneWidget,
      reason: 'the Done bullet must render exactly once, as its own line',
    );
    // Done and Work both render as bold headers.
    final richTexts = tester.widgetList<RichText>(find.byType(RichText));
    expect(
      richTexts.any((rt) => _hasSpan(rt.text, 'Done', bold: true)),
      isTrue,
      reason: '**Done** must paint as a bold "Done" header',
    );
  });

  testWidgets('emoji + bold headers ("🚀 **Work**") still group into sections', (
    tester,
  ) async {
    await tester.pumpWidget(_host(const CardData(
      type: CardType.briefing,
      title: 'Morning',
      fullText: 'Morning\nA quick line.\n\n'
          '🚀 **Work**\n- Call the client\n🏠 **Home**\n- Buy groceries',
    )));

    expect(find.textContaining('Call the client', findRichText: true),
        findsOneWidget);
    expect(find.textContaining('Buy groceries', findRichText: true),
        findsOneWidget);
    expect(find.textContaining('A quick line.', findRichText: true),
        findsOneWidget,
        reason: '🚀 **Work** must be recognised as a header so the intro stays the intro');
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
