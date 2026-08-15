import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rhodey_app/screens/onboarding/onboarding_flow.dart';

void main() {
  testWidgets('persona pick + Continue advances to Sign in step', (tester) async {
    await tester.pumpWidget(
      MaterialApp(home: OnboardingFlow(onDone: () {})),
    );
    await tester.pumpAndSettle();

    expect(find.text('Rhodey'), findsOneWidget);
    expect(find.text('Chief of Staff'), findsOneWidget);

    // Pick a persona.
    await tester.tap(find.text('Chief of Staff'));
    await tester.pumpAndSettle();

    // Continue is below the fold — scroll it into view.
    await tester.ensureVisible(find.text('Continue'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();

    expect(find.text('Sign in'), findsOneWidget);
    expect(find.text('Continue with Google'), findsOneWidget);

    // Back returns to welcome.
    await tester.tap(find.text('Back'));
    await tester.pumpAndSettle();
    expect(find.text('How would you like Rhodey to assist you?'), findsOneWidget);
  });

  testWidgets('PageView element survives page transitions (regression)', (tester) async {
    await tester.pumpWidget(
      MaterialApp(home: OnboardingFlow(onDone: () {})),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.text('Chief of Staff'));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('Continue'));
    await tester.pumpAndSettle();

    final before = tester.element(find.byType(PageView));
    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();

    final after = tester.element(find.byType(PageView));
    expect(identical(before, after), isTrue,
        reason: 'PageView must not be recreated when the progress bar / bottom '
            'bar appear — recreation cancels animateToPage and snaps back to '
            'page 0, making the Sign-in step unreachable');
    expect(find.text('Sign in'), findsOneWidget);
  });
}
