import 'dart:convert';
import 'package:shared_preferences/shared_preferences.dart';
import '../models/decision_item.dart';

/// On-device cache of the inbox's pending-decision list + unverified
/// auto-decision count.
///
/// WhatsApp-style delivery: the last fetched list is persisted locally so a
/// cold open (vault tap / notification tap) renders instantly — the live
/// fetch that follows reconciles and re-saves. Writes are also done on item
/// actions (approve/reject) so an actioned item never resurrects on the next
/// open. Cache failures are never fatal: every read/write fails open so the
/// live API path stays the source of truth.
class InboxCache {
  static const _key = 'rhodey_inbox_cache_v1';

  /// Loads the cached inbox snapshot, or an empty snapshot when absent/corrupt.
  Future<InboxCacheData> load() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(_key);
      if (raw == null || raw.isEmpty) return InboxCacheData.empty();
      final decoded = jsonDecode(raw);
      if (decoded is! Map) return InboxCacheData.empty();
      final itemsRaw = decoded['items'];
      final items = itemsRaw is List
          ? itemsRaw
              .whereType<Map>()
              .map((m) => DecisionItem.fromJson(m.cast<String, dynamic>()))
              .toList()
          : <DecisionItem>[];
      final count = decoded['auto_decision_count'] is int
          ? decoded['auto_decision_count'] as int
          : 0;
      return InboxCacheData(items: items, autoDecisionCount: count);
    } catch (_) {
      return InboxCacheData.empty();
    }
  }

  /// Persists the inbox snapshot (items + unverified auto-decision count).
  Future<void> save(InboxCacheData data) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_key, jsonEncode({
        'items': data.items.map((i) => i.toJson()).toList(),
        'auto_decision_count': data.autoDecisionCount,
      }));
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

/// A point-in-time inbox snapshot: the pending list + auto-decision count.
class InboxCacheData {
  final List<DecisionItem> items;
  final int autoDecisionCount;

  const InboxCacheData({required this.items, required this.autoDecisionCount});

  const InboxCacheData.empty()
      : items = const [],
        autoDecisionCount = 0;
}
