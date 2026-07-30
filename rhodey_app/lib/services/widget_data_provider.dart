import 'dart:convert';
import 'package:home_widget/home_widget.dart';
import '../models/briefing.dart';

class WidgetDataProvider {
  static final WidgetDataProvider _instance = WidgetDataProvider._();
  factory WidgetDataProvider() => _instance;
  WidgetDataProvider._();

  Future<void> updatePulseWidget(BriefingResponse briefing) async {
    final headline = (briefing.voiceLine != null && briefing.voiceLine!.isNotEmpty)
        ? briefing.voiceLine!
        : ((briefing.contextBar != null && briefing.contextBar!.isNotEmpty)
            ? briefing.contextBar!
            : briefing.greeting);

    List<Map<String, String>> insightsList = [];
    
    switch (briefing.homeMode) {
      case 'sprint':
        if (briefing.nextEvent != null) {
          insightsList.add({"text": "🎯 Sprinting: ${briefing.nextEvent}", "link": "rhodey://today"});
        }
        if (briefing.vaultedUrgentCount > 0) {
          insightsList.add({"text": "🔴 ${briefing.vaultedUrgentCount} urgent", "link": "rhodey://surface"});
        }
        break;
      case 'decide':
        if (briefing.pendingCount > 0) {
          insightsList.add({"text": "⚖️ ${briefing.pendingCount} pending decisions", "link": "rhodey://inbox"});
        }
        break;
      default:
        for (final insight in briefing.insights) {
          insightsList.add({"text": insight, "link": "rhodey://surface"});
        }
    }

    final insightsJson = jsonEncode(insightsList);

    await HomeWidget.saveWidgetData<String>('headline', headline);
    await HomeWidget.saveWidgetData<String>('insights_json', insightsJson);

    await HomeWidget.updateWidget(
      name: 'PulseStripWidgetProvider',
      qualifiedAndroidName: 'com.crayon.rhodey_app.PulseStripWidgetProvider',
    );
  }
}