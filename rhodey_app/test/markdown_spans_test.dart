/// Unit tests for `markdownBoldSpans` — the shared parser that turns the
/// server's `**bold**` markdown into bold spans (never literal asterisks).
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rhodey_app/utils/markdown_spans.dart';

void main() {
  test('parses a **bold** run into a bold span', () {
    final spans = markdownBoldSpans('Hello **Work** done');
    expect(spans, hasLength(3));
    expect(spans[0].text, 'Hello ');
    expect(spans[1].text, 'Work');
    expect(spans[1].style?.fontWeight, FontWeight.w700);
    expect(spans[2].text, ' done');
  });

  test('parses multiple bold runs', () {
    final spans = markdownBoldSpans('**A** and **B**');
    expect(spans.map((s) => s.text).join('|'), 'A| and |B');
    expect(
      spans.where((s) => s.style?.fontWeight == FontWeight.w700),
      hasLength(2),
    );
  });

  test('whole-string bold', () {
    final spans = markdownBoldSpans('**Work**');
    expect(spans, hasLength(1));
    expect(spans[0].text, 'Work');
    expect(spans[0].style?.fontWeight, FontWeight.w700);
  });

  test('unclosed ** stays literal — never mangles user text', () {
    final spans = markdownBoldSpans('price is **5 dollars');
    expect(spans, hasLength(1));
    expect(spans[0].text, 'price is **5 dollars');
    expect(spans[0].style?.fontWeight, isNot(FontWeight.w700));
  });

  test('empty and plain text return plain spans', () {
    expect(markdownBoldSpans(''), hasLength(1));
    expect(
      markdownBoldSpans('no markdown here').single.text,
      'no markdown here',
    );
  });
}
