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
  /// Items key — the legacy combined key, kept as-is so cached items survive
  /// the split (and so old snapshots still migrate their count, see [load]).
  static const _itemsKey = 'rhodey_inbox_cache_v1';

  /// Separate key for the auto-decision count. It lives in its own key so
  /// the parallel count fetch and items fetch can each write without a
  /// read-modify-write race clobbering the other's data.
  static const _countKey = 'rhodey_inbox_count_cache_v1';

  /// Loads the cached inbox snapshot, or an empty snapshot when absent/corrupt.
  Future<InboxCacheData> load() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(_itemsKey);
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
      var count = 0;
      if (prefs.containsKey(_countKey)) {
        count = prefs.getInt(_countKey) ?? 0;
      } else if (decoded['auto_decision_count'] is int) {
        // Migration: the count used to live inside the combined items key.
        count = decoded['auto_decision_count'] as int;
      }
      return InboxCacheData(items: items, autoDecisionCount: count);
    } catch (_) {
      return InboxCacheData.empty();
    }
  }

  /// Persists only the pending-decision items — never touches the count key.
  Future<void> saveItems(List<DecisionItem> items) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(
        _itemsKey,
        jsonEncode({'items': items.map((i) => i.toJson()).toList()}),
      );
    } catch (_) {
      // Never fatal — the cache is an optimization.
    }
  }

  /// Persists only the auto-decision count — never touches the items key, so
  /// a parallel count fetch can't clobber a freshly-written items list.
  Future<void> saveCount(int count) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setInt(_countKey, count);
    } catch (_) {
      // Never fatal — the cache is an optimization.
    }
  }

  /// Clears the cache (e.g. sign-out / debug).
  Future<void> clear() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.remove(_itemsKey);
      await prefs.remove(_countKey);
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

  const InboxCacheData.empty() : items = const [], autoDecisionCount = 0;
}
