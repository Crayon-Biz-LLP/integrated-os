/// Phase 2B Step 4 tests: the closed-enum voice transport (R4) and Rhodey's
/// app-side voice variants.
///
/// Fail-closed (R5): unknown/empty voice_style => the standard voice, byte-
/// identical to pre-2B copy. Only 'direct' and 'warm' shift Rhodey's own
/// static lines — server-composed strings take precedence everywhere else.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:rhodey_app/models/persona_summary.dart';
import 'package:rhodey_app/voice/rhodey_voice.dart';

void main() {
  group('PersonaSummary.fromJson', () {
    test('parses the closed-enum shape', () {
      final s = PersonaSummary.fromJson({
        'display_name': 'Danny',
        'voice_style': 'direct',
        'signoffs': ['Rest well.', 'Onward.'],
      });
      expect(s.displayName, 'Danny');
      expect(s.voiceStyle, 'direct');
      expect(s.signoffs, ['Rest well.', 'Onward.']);
      expect(s.isEmpty, isFalse);
    });

    test('null payload is empty (fail-closed)', () {
      final s = PersonaSummary.fromJson(const {});
      expect(s.isEmpty, isTrue);
      expect(s.voiceStyle, '');
    });

    test('card-only keys are ignored — nothing else leaks', () {
      final s = PersonaSummary.fromJson({
        'display_name': 'Danny',
        'voice_style': 'warm',
        // These MUST NOT be present in the payload — if they ever are, the
        // model ignores them (and the server test forbids them).
        'never': ['debt'],
        'people': ['Sunjula Daniel'],
        'who': 'founder',
      });
      expect(s.isEmpty, isFalse);
    });

    test('unknown style token survives as-is (app falls back to standard)', () {
      final s = PersonaSummary.fromJson({
        'display_name': 'X',
        'voice_style': 'loud',
      });
      expect(s.voiceStyle, 'loud');
      // RhodeyVoice must treat it as standard.
      expect(RhodeyVoice.allClear('X', s.voiceStyle), 'All clear, X.');
    });
  });

  group('RhodeyVoice voice-style variants', () {
    test('standard voice is byte-identical pre-2B (no style)', () {
      expect(RhodeyVoice.allClear('Danny'), 'All clear, Danny.');
      expect(RhodeyVoice.allClear('Danny', ''), 'All clear, Danny.');
      expect(RhodeyVoice.allClearPrompt('Danny'),
          "All clear, Danny. What's on your mind?");
    });

    test('direct variant', () {
      expect(RhodeyVoice.allClear('Danny', 'direct'),
          "Board's clear, Danny.");
      expect(RhodeyVoice.allClearPrompt('Danny', 'direct'),
          "Board's clear, Danny. What's on your mind?");
    });

    test('warm variant', () {
      expect(RhodeyVoice.allClear('Danny', 'warm'),
          'All quiet on the board, Danny.');
    });

    test('calm keeps the standard line', () {
      expect(RhodeyVoice.allClear('Danny', 'calm'), 'All clear, Danny.');
    });

    test('unknown style falls back to standard (fail-closed)', () {
      expect(RhodeyVoice.allClear('Danny', 'loud'), 'All clear, Danny.');
    });

    test('no name omits the name in every variant (M17)', () {
      expect(RhodeyVoice.allClear(null, 'direct'), "Board's clear.");
      expect(RhodeyVoice.allClear(null, 'warm'), 'All quiet on the board.');
      expect(RhodeyVoice.allClearPrompt(null, 'warm'),
          "All quiet on the board. What's on your mind?");
    });
  });
}
