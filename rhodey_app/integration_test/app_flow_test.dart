import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:rhodey_app/main.dart' as app;

/// Real-device end-to-end tests for the Rhodey app (X8).
///
/// These run on an Android emulator / device via `flutter test integration_test`
/// and exercise the REAL app binary: real platform channels, real
/// SharedPreferences / secure storage, real Firebase init — not test doubles.
///
/// A fresh install has no API key, so the app boots into the onboarding
/// journey. The tests pin the boot contract and the welcome→persona→sign-in
/// progression.
void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  /// The app's `main()` awaits real async init (secure storage, prefs,
  /// home-widget platform channel) before `runApp`. On a device that takes
  /// real time, so `pumpAndSettle` alone returns too early. Poll with real
  /// time until [finder] matches (or timeout) — the standard integration-test
  /// boot pattern.
  Future<void> pumpUntilFound(
    WidgetTester tester,
    Finder finder, {
    Duration timeout = const Duration(seconds: 30),
  }) async {
    final end = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(end)) {
      await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 250)));
      await tester.pumpAndSettle();
      if (finder.evaluate().isNotEmpty) return;
    }
    fail('Timed out waiting for $finder');
  }

  /// The post-frame update check (UpdateService) hits the production endpoint
  /// and may pop a "Later / Update now" dialog over onboarding. Dismiss it if
  /// present so it never eats a tap.
  Future<void> dismissUpdateDialogIfPresent(WidgetTester tester) async {
    if (find.text('Later').evaluate().isNotEmpty) {
      await tester.tap(find.text('Later'));
      await tester.pumpAndSettle();
    }
  }

  testWidgets('fresh install boots to onboarding welcome', (tester) async {
    app.main();
    await pumpUntilFound(tester, find.text('Rhodey'));
    await dismissUpdateDialogIfPresent(tester);

    // Gate resolved → welcome step, not a blank splash.
    expect(find.text('Rhodey'), findsOneWidget);
    expect(
      find.text('How would you like Rhodey to assist you?'),
      findsOneWidget,
    );

    // All five persona cards are offered.
    for (final name in [
      'Chief of Staff',
      'Operations Co-Pilot',
      'Daily Organizer',
      'Life & Household',
      'Keep it simple',
    ]) {
      expect(find.text(name), findsOneWidget);
    }

    // Continue is a no-op before a persona is picked — still on welcome.
    // (ensureVisible first: an off-screen tap misses silently and would make
    // this assertion vacuous.)
    await tester.ensureVisible(find.text('Continue'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();
    expect(
      find.text('How would you like Rhodey to assist you?'),
      findsOneWidget,
    );
  });

  testWidgets('persona pick enables Continue and advances to Sign in', (tester) async {
    app.main();
    await pumpUntilFound(tester, find.text('Rhodey'));
    await dismissUpdateDialogIfPresent(tester);

    // Pick the Chief of Staff persona.
    await pumpUntilFound(tester, find.text('Chief of Staff'));
    await tester.tap(find.text('Chief of Staff'));
    await tester.pumpAndSettle();

    // The Continue CTA sits at the bottom of the scrollable welcome step —
    // ensure it is actually on-screen before tapping (a tap on an off-screen
    // widget misses silently and the page never advances).
    await tester.ensureVisible(find.text('Continue'));
    await tester.pumpAndSettle();

    // Continue now advances to the Sign-in step.
    await tester.tap(find.text('Continue'));
    await pumpUntilFound(tester, find.text('Sign in'));

    expect(find.text('Continue with Google'), findsOneWidget);

    // Back returns to the welcome step with the persona still selected.
    await tester.tap(find.text('Back'));
    await tester.pumpAndSettle();
    expect(
      find.text('How would you like Rhodey to assist you?'),
      findsOneWidget,
    );
  });
}
