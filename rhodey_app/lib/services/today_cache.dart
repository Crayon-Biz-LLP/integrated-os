import 'dart:convert';
import 'package:shared_preferences/shared_preferences.dart';
import '../services/api_service.dart';

/// On-device cache of the Today screen's last-known snapshot (events, tasks,
/// captures) — the same WhatsApp-style instant-render pattern as home-feed /
/// inbox / messages: navigation paints the cached snapshot instantly, and the
/// live fetch that follows reconciles and re-saves. Cache failures are never
/// fatal — every read/write fails open so the live API stays the source of
/// truth.
class TodayCache {
  static const _key = 'rhodey_today_cache_v1';

  /// Returns the cached Today snapshot, or null when absent/corrupt.
  Future<TodayCacheData?> load() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(_key);
      if (raw == null || raw.isEmpty) return null;
      final decoded = jsonDecode(raw);
      if (decoded is! Map) return null;
      final events = ((decoded['events'] as List?) ?? []).whereType<Map>().map((
        e,
      ) {
        final m = e.cast<String, dynamic>();
        return CalendarEventItem(
          id: m['id'] as String? ?? '',
          title: m['title'] as String? ?? '',
          timeRange: m['timeRange'] as String? ?? '',
          isActive: m['isActive'] == true,
        );
      }).toList();
      final tasks = ((decoded['tasks'] as List?) ?? [])
          .whereType<Map>()
          .map((m) => m.cast<String, dynamic>())
          .toList();
      final captures = ((decoded['captures'] as List?) ?? [])
          .whereType<Map>()
          .map((m) => m.cast<String, dynamic>())
          .toList();
      return TodayCacheData(events: events, tasks: tasks, captures: captures);
    } catch (_) {
      return null;
    }
  }

  /// Persists the last-known Today snapshot for the next instant open.
  Future<void> save({
    required List<CalendarEventItem> events,
    required List<Map<String, dynamic>> tasks,
    required List<Map<String, dynamic>> captures,
  }) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(
        _key,
        jsonEncode({
          'events': events
              .map(
                (e) => {
                  'id': e.id,
                  'title': e.title,
                  'timeRange': e.timeRange,
                  'isActive': e.isActive,
                },
              )
              .toList(),
          'tasks': tasks,
          'captures': captures,
        }),
      );
    } catch (_) {
      // Never fatal — the cache is an optimization.
    }
  }

  /// Clears the cache (e.g. sign-out / debug).
  Future<void> clear() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.remove(_key);
    } catch (_) {
      // Never fatal.
    }
  }
}

/// A point-in-time Today snapshot.
class TodayCacheData {
  final List<CalendarEventItem> events;
  final List<Map<String, dynamic>> tasks;
  final List<Map<String, dynamic>> captures;

  const TodayCacheData({
    required this.events,
    required this.tasks,
    required this.captures,
  });

  bool get isEmpty => events.isEmpty && tasks.isEmpty && captures.isEmpty;
}
