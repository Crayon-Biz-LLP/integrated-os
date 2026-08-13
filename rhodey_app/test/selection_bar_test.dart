/// Regression test for the Inbox batch-selection bottom bar.
///
/// The bar is wrapped in a horizontal SingleChildScrollView so it never
/// overflows on narrow phones. A `Spacer` (flex child) inside an unbounded
/// Row throws "RenderFlex children have non-zero flex but incoming width
/// constraints are unbounded" — which broke the whole bar the moment
/// selection mode turned on, making Select / Select all unusable. The bar
/// must use a fixed-width gap instead.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rhodey_app/theme/app_theme.dart';

void main() {
  testWidgets('selection bar renders without a RenderFlex exception',
      (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SafeArea(
            child: Container(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 10),
              decoration: BoxDecoration(
                color: AppTheme.surface,
                border: const Border(top: BorderSide(color: AppTheme.border)),
              ),
              child: SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                child: Row(
                  children: [
                    const Text('Select items', style: AppTheme.caption),
                    // Fixed gap — NOT Spacer (flex in unbounded width throws).
                    const SizedBox(width: 12),
                    Material(
                      color: AppTheme.textTertiary.withValues(alpha: 0.1),
                      borderRadius: BorderRadius.circular(8),
                      child: const Padding(
                        padding: EdgeInsets.symmetric(
                          horizontal: 12,
                          vertical: 6,
                        ),
                        child: Text('Select all'),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
    expect(tester.takeException(), isNull);
  });
}
