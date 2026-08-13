import 'package:flutter_test/flutter_test.dart';

import 'package:rhodey_app/widgets/rich_card_content.dart';

/// Pins the app's ack parser to the backend verb table
/// (core/lib/rhodey_voice.py `render_acks`). The emoji + verb vocabulary is
/// the contract between Rhodey's acknowledgements and how the app renders
/// them — change the table and this test together.
void main() {
  group('task mutations (stay OPEN — never taskDone)', () {
    test('reschedule renders a task card, date stripped from title', () {
      final card = parseMessageToCardData('✅ Rescheduled: Purchase the Ashraya domain → Aug 20, 2026');
      expect(card, isNotNull);
      expect(card!.type, CardType.task);
      expect(card.title, 'Purchase the Ashraya domain');
    });

    test('updated renders a task card', () {
      final card = parseMessageToCardData('✅ Updated: Renew the lease');
      expect(card, isNotNull);
      expect(card!.type, CardType.task);
      expect(card.title, 'Renew the lease');
    });

    test('recurrence updated renders a task card', () {
      final card = parseMessageToCardData('✅ Recurrence updated: Weekly sync');
      expect(card, isNotNull);
      expect(card!.type, CardType.task);
      expect(card.title, 'Weekly sync');
    });
  });

  group('creations', () {
    test('task creation "On your list" renders a task card', () {
      final card = parseMessageToCardData('📝 On your list: File the Q3 report — Aug 20, 2026');
      expect(card, isNotNull);
      expect(card!.type, CardType.task);
      expect(card.title, 'File the Q3 report');
    });

    test('event creation "Scheduled" renders a task card', () {
      final card = parseMessageToCardData('📅 Scheduled: Team standup — Aug 20, 2026');
      expect(card, isNotNull);
      expect(card!.type, CardType.task);
      expect(card.title, 'Team standup');
    });

    test('note "Logged" renders a note card', () {
      final card = parseMessageToCardData('📝 Logged: Meeting notes from today');
      expect(card, isNotNull);
      expect(card!.type, CardType.note);
      expect(card.title, 'Meeting notes from today');
    });

    test('legacy "Note saved" still renders a note card', () {
      final card = parseMessageToCardData('📝 Note saved: A quick thought');
      expect(card, isNotNull);
      expect(card!.type, CardType.note);
    });
  });

  group('closures + approvals (existing contract preserved)', () {
    test('closed renders a taskDone card', () {
      final card = parseMessageToCardData('✅ Closed: Reply to the client');
      expect(card, isNotNull);
      expect(card!.type, CardType.taskDone);
      expect(card.title, 'Reply to the client');
    });

    test('approval confirmation still renders an approval card', () {
      final card = parseMessageToCardData('✅ Approved email: Finance update');
      expect(card, isNotNull);
      expect(card!.type, CardType.approval);
    });

    test('person approved style still renders an approval card', () {
      final card = parseMessageToCardData('✅ Priya Sharma Approved');
      expect(card, isNotNull);
      expect(card!.type, CardType.approval);
    });
  });

  group('tightened generic fallback', () {
    test('unknown ✅ line without approval language renders no card', () {
      // Regression: genericApproval used to turn ANY unknown ✅ line into an
      // approval card — e.g. "✅ Rescheduled: …" showed a bogus approve affordance.
      expect(parseMessageToCardData('✅ Something happened'), isNull);
    });

    test('trailing ✓ marker still counts as approval-like', () {
      final card = parseMessageToCardData('✅ The merge went through ✓');
      expect(card, isNotNull);
      expect(card!.type, CardType.approval);
    });
  });

  group('structured ack intents (card from metadata, text unparsed)', () {
    test('TASK_CREATED renders a task card with the bare title', () {
      final card = resolveCardData(
        intent: 'TASK_CREATED',
        text: 'Got it — Purchase the Ashraya domain is on your list for Aug 20, 2026.',
        title: 'Purchase the Ashraya domain',
      );
      expect(card, isNotNull);
      expect(card!.type, CardType.task);
      expect(card.title, 'Purchase the Ashraya domain');
    });

    test('TASK_RESCHEDULED renders an open task card, never taskDone', () {
      final card = resolveCardData(
        intent: 'TASK_RESCHEDULED',
        text: 'Moved Purchase the Ashraya domain to Aug 20, 2026.',
        title: 'Purchase the Ashraya domain',
      );
      expect(card!.type, CardType.task);
    });

    test('TASK_CLOSED renders a taskDone card', () {
      final card = resolveCardData(
        intent: 'TASK_CLOSED',
        text: "Done — Reply to the client is off your plate.",
        title: 'Reply to the client',
      );
      expect(card!.type, CardType.taskDone);
    });

    test('NOTE_LOGGED renders a note card', () {
      final card = resolveCardData(
        intent: 'NOTE_LOGGED',
        text: 'Meeting notes from today — logged.',
        title: 'Meeting notes from today',
      );
      expect(card!.type, CardType.note);
    });

    test('EVENT_SCHEDULED renders a task card', () {
      final card = resolveCardData(
        intent: 'EVENT_SCHEDULED',
        text: 'Added Team standup to your calendar for Aug 20, 2026.',
        title: 'Team standup',
      );
      expect(card!.type, CardType.task);
      expect(card.title, 'Team standup');
    });

    test('ack intent with no title falls back to the first line', () {
      final card = resolveCardData(
        intent: 'TASK_UPDATED',
        text: "Updated Renew the lease's priority.",
      );
      expect(card!.type, CardType.task);
      expect(card.title, "Updated Renew the lease's priority.");
    });

    test('voice text without intent is plain (no emoji to parse)', () {
      // New acks carry no emoji prefixes — without structured intent the
      // parser intentionally returns null (plain bubble), which is correct:
      // the card comes from metadata, never from the voice prose.
      expect(parseMessageToCardData('Got it — X is on your list.'), isNull);
      expect(parseMessageToCardData('Moved X to Aug 20, 2026.'), isNull);
    });
  });
}
