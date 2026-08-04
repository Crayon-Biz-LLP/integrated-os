import 'dart:convert';
import 'package:shared_preferences/shared_preferences.dart';

/// On-device cache of the most recent conversation-history page.
///
/// WhatsApp-style delivery: the last fetched page (newest-first, exactly as
/// `/api/conversation-history` returns it) is persisted locally so cold opens
/// and notification taps render instantly — the server fetch that follows
/// reconciles and re-saves. Cache failures are never fatal: every read/write
/// fails open so the live API path stays the source of truth.
class MessageCache {
  static const _key = 'rhodey_message_cache_v1';
  static const maxEntries = 50;

  /// Returns the cached page newest-first, or an empty list when absent/corrupt.
  Future<List<Map<String, dynamic>>> load() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(_key);
      if (raw == null || raw.isEmpty) return [];
      final decoded = jsonDecode(raw);
      if (decoded is! List) return [];
      return decoded.cast<Map<String, dynamic>>().toList();
    } catch (_) {
      return [];
    }
  }

  /// Persists a fetched page (newest-first) capped to [maxEntries].
  Future<void> save(List<Map<String, dynamic>> rows) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final capped = rows.take(maxEntries).toList();
      await prefs.setString(_key, jsonEncode(capped));
    } catch (_) {
      // Never fatal — the cache is an optimization.
    }
  }

  /// Merges freshly fetched rows into the cached page, deduped by id,
  /// newest-first, capped at [maxEntries]. Used by the backup poll so a reply
  /// that lands via push+poll is persisted too — not just full history loads.
  Future<void> merge(List<Map<String, dynamic>> freshRows) async {
    try {
      final existing = await load();
      final byId = <String, Map<String, dynamic>>{};
      for (final row in existing) {
        byId[row['id'].toString()] = row;
      }
      for (final row in freshRows) {
        byId[row['id'].toString()] = row;
      }
      final merged = byId.values.toList()
        ..sort((a, b) =>
            ((b['created_at'] ?? '') as String).compareTo((a['created_at'] ?? '') as String));
      await save(merged);
    } catch (_) {
      // Never fatal.
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
