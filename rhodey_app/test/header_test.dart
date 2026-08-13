/// Regression test: the header task pill must stay pinned to the right edge
/// no matter how long the pulse label is ("Rhodey" → "Rhodey · PRE-MONDAY").
///
/// The old layout used `Flexible` (loose fit) + `Spacer`: the loose text left
/// its unused allocation as dead space, so the pill floated mid-row and moved
/// as the label grew. With `Expanded` (tight fit) the text fills all free
/// space and the pill is always flush right.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rhodey_app/theme/app_theme.dart';

Widget headerRow(String pulseLabel, {bool syncing = false}) {
  return Container(
    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
    decoration: const BoxDecoration(
      border: Border(bottom: BorderSide(color: AppTheme.border, width: 1)),
    ),
    child: Row(
      children: [
        const Material(
          child: Padding(
            padding: EdgeInsets.all(8),
            child: Icon(Icons.menu, size: 22),
          ),
        ),
        const SizedBox(width: 8),
        Container(
          width: 8,
          height: 8,
          decoration: const BoxDecoration(shape: BoxShape.circle),
        ),
        const SizedBox(width: 6),
        Expanded(
          child: Text(
            'Rhodey${pulseLabel.isNotEmpty ? ' · $pulseLabel' : ''}',
            style: AppTheme.body.copyWith(fontSize: 13, letterSpacing: 0.3),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
          ),
        ),
        if (syncing) ...[
          const SizedBox(width: 8),
          const SizedBox(width: 10, height: 10),
          const SizedBox(width: 4),
          const Text('syncing'),
        ],
        Material(
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
            decoration: BoxDecoration(
              color: AppTheme.surfaceAlt,
              borderRadius: BorderRadius.circular(6),
              border: Border.all(color: AppTheme.border, width: 1),
            ),
            child: const Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.checklist_rtl, size: 12),
                SizedBox(width: 3),
                Text('5'),
              ],
            ),
          ),
        ),
      ],
    ),
  );
}

void main() {
  testWidgets('task pill is pinned right for every pulse label',
      (tester) async {
    const labels = ['', 'MORNING', 'AFTERNOON', 'PRE-MONDAY', 'INTEL'];
    final rightEdges = <double>[];
    for (final label in labels) {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: SafeArea(child: headerRow(label))),
        ),
      );
      rightEdges.add(tester.getRect(find.text('5')).right);
    }
    final spread = rightEdges.reduce((a, b) => a > b ? a : b) -
        rightEdges.reduce((a, b) => a < b ? a : b);
    expect(spread, 0, reason: 'pill right edge moved: $rightEdges');
  });

  testWidgets('task pill stays pinned when the sync chip appears',
      (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SafeArea(child: headerRow('MORNING', syncing: true)),
        ),
      ),
    );
    expect(tester.takeException(), isNull);
    final rect = tester.getRect(find.text('5'));
    // Pill must sit at the far right, not mid-row.
    expect(rect.right, greaterThan(700));
  });
}
