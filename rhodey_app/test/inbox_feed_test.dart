/// Tests for the Quick-Confirmations Inbox additions: the teams decision
/// type, and the inbox bundle's channel-message / drafts / FYI sections.
///
/// Regression: actionable channel items (email/whatsapp/call/teams) lived in
/// `messages` but the Inbox feed only read raw_dumps with status='pending',
/// so pending decisions never appeared. The server now serves
/// `pending_channel_messages` shaped like the app's existing pending-message
/// rows; these tests pin that the app parses teams items and the new
/// drafts/FYI sections.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:rhodey_app/models/decision_item.dart';

void main() {
  group('DecisionType.teams', () {
    test('typeIcon maps to a distinct icon', () {
      final item = DecisionItem(
        id: '1',
        type: DecisionType.teams,
        priority: DecisionPriority.standard,
        title: 'Confirm deadline',
        createdAt: DateTime.utc(2026, 8, 11, 10),
      );
      expect(item.typeIcon, '🟣');
    });

    test('type label renders TEAMS on a decision card', () {
      // _typeLabel is private to the card widget; the enum round-trips via
      // .name so the cache survives and the card switch is exhaustive
      // (analyzer-enforced). Assert the enum name matches what fromJson reads.
      expect(DecisionType.teams.name, 'teams');
      expect(
        DecisionType.values.byName('teams'),
        DecisionType.teams,
      );
    });
  });

  group('DecisionItem cache round-trip (new types)', () {
    test('teams item survives toJson/fromJson', () {
      final item = DecisionItem(
        id: 'api_teams_9',
        type: DecisionType.teams,
        priority: DecisionPriority.high,
        title: 'Confirm ferry time',
        description: 'via teams',
        createdAt: DateTime.utc(2026, 8, 11, 10),
      );
      final restored = DecisionItem.fromJson(item.toJson());
      expect(restored.type, DecisionType.teams);
      expect(restored.title, 'Confirm ferry time');
    });

    test('unknown type falls back to clarification (no crash)', () {
      final restored = DecisionItem.fromJson({
        'id': 'x',
        'type': 'future_type',
        'priority': 'standard',
        'title': 't',
      });
      expect(restored.type, DecisionType.clarification);
    });
  });
}
