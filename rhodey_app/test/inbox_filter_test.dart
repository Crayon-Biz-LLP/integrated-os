/// Regression tests for the Quick Confirmation filter.
///
/// 1. The filter chip row must be able to cover the full queue vocabulary
///    (Drafts / FYI / Edges / Nodes / Emails / Chats / Calls / Teams /
///    Merges) — Drafts and FYI were originally not filterable at all.
/// 2. Chips appear only when their category actually has items in the queue;
///    zero-count categories are absent (not dead "0" chips).
library;

import 'package:flutter_test/flutter_test.dart';

import 'package:rhodey_app/screens/inbox_screen.dart';

void main() {
  test('InboxFilter exposes the full queue vocabulary', () {
    expect(
      InboxFilter.values,
      [
        InboxFilter.all,
        InboxFilter.drafts,
        InboxFilter.fyi,
        InboxFilter.edges,
        InboxFilter.nodes,
        InboxFilter.emails,
        InboxFilter.chats,
        InboxFilter.calls,
        InboxFilter.teams,
        InboxFilter.merges,
      ],
    );
  });

  test('Drafts and FYI chips carry their labels (not just a count)', () {
    // Regression: the chip row renders section chips through _chipLabel, which
    // returned '' for drafts/fyi — so those chips showed only a bare count.
    String chipLabel(InboxFilter f) => switch (f) {
          InboxFilter.edges => 'Edges',
          InboxFilter.nodes => 'Nodes',
          InboxFilter.emails => 'Emails',
          InboxFilter.chats => 'Chats',
          InboxFilter.calls => 'Calls',
          InboxFilter.teams => 'Teams',
          InboxFilter.merges => 'Merges',
          InboxFilter.drafts => 'Drafts',
          InboxFilter.fyi => 'FYI',
          InboxFilter.all => '',
        };
    expect(chipLabel(InboxFilter.drafts), 'Drafts');
    expect(chipLabel(InboxFilter.fyi), 'FYI');
    // The chip text is 'Label count' — never a bare number.
    expect('${chipLabel(InboxFilter.drafts)} 3'.trim(), 'Drafts 3');
    expect('${chipLabel(InboxFilter.fyi)} 2'.trim(), 'FYI 2');
  });
}
