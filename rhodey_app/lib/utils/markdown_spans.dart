import 'package:flutter/material.dart';

/// Parses `**bold**` markdown into styled [TextSpan]s.
///
/// The server emits lightweight markdown in briefing / reply text (e.g. the
/// pulse sanitizer converts `### Header` → `**Header**` so Telegram renders a
/// bold section header). The app must render that the same way — bold text —
/// instead of showing the raw `**Work**` asterisks.
///
/// Only `**bold**` is handled (the only marker the server produces today).
/// Unclosed or stray `*` runs are left as literal text, so user-typed content
/// is never mangled. The returned spans only override `fontWeight` — color,
/// family and size come from the ambient/TextSpan base style.
List<TextSpan> markdownBoldSpans(String text) {
  if (text.isEmpty) return const [TextSpan()];
  final spans = <TextSpan>[];
  final pattern = RegExp(r'\*\*(.+?)\*\*');
  var last = 0;
  for (final m in pattern.allMatches(text)) {
    if (m.start > last) {
      spans.add(TextSpan(text: text.substring(last, m.start)));
    }
    spans.add(
      TextSpan(
        text: m.group(1),
        style: const TextStyle(fontWeight: FontWeight.w700),
      ),
    );
    last = m.end;
  }
  if (last < text.length) {
    spans.add(TextSpan(text: text.substring(last)));
  }
  // Non-empty input always yields at least one span (a bold match or the
  // trailing plain run), so this is safe.
  return spans;
}
