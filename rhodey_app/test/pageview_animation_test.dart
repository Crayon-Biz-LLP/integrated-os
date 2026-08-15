import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('animateToPage works with NeverScrollableScrollPhysics', (tester) async {
    final controller = PageController();
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: PageView(
            controller: controller,
            physics: const NeverScrollableScrollPhysics(),
            children: [
              for (var i = 0; i < 3; i++)
                Center(child: Text('Page $i', key: ValueKey('page_$i'))),
            ],
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('Page 0'), findsOneWidget);

    controller.animateToPage(1,
        duration: const Duration(milliseconds: 350), curve: Curves.easeOutCubic);
    await tester.pumpAndSettle();

    debugPrint('after animateToPage(1): page=${controller.page} '
        'Page1=${find.text('Page 1').evaluate().length}');
    expect(find.text('Page 1'), findsOneWidget);
  });

  testWidgets('animateToPage works with default physics', (tester) async {
    final controller = PageController();
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: PageView(
            controller: controller,
            children: [
              for (var i = 0; i < 3; i++)
                Center(child: Text('Page $i', key: ValueKey('page_$i'))),
            ],
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    controller.animateToPage(1,
        duration: const Duration(milliseconds: 350), curve: Curves.easeOutCubic);
    await tester.pumpAndSettle();

    debugPrint('after animateToPage(1) default: page=${controller.page} '
        'Page1=${find.text('Page 1').evaluate().length}');
    expect(find.text('Page 1'), findsOneWidget);
  });
}
