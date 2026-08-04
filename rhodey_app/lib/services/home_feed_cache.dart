import 'dart:convert';
import 'package:shared_preferences/shared_preferences.dart';

/// On-device cache of the last home-feed response (the raw /api/home-feed map).
///
/// WhatsApp-style delivery: cold opens render the cached voice line, focal
/// board, and briefing instantly, and the network fetch that follows
/// reconciles and re-saves. Cache failures are never fatal — every read/write
/// fails open so the live API stays the source of truth.
class HomeFeedCache {
  static const _key = 'rhodey_home_feed_cache_v1';

  /// Returns the cached home-feed payload (raw map), or null when
  /// absent/corrupt.
  Future<Map<String, dynamic>?> load() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(_key);
      if (raw == null || raw.isEmpty) return null;
      final decoded = jsonDecode(raw);
      if (decoded is! Map) return null;
      return decoded.cast<String, dynamic>();
    } catch (_) {
      return null;
    }
  }

  /// Persists a home-feed payload (raw map).
  Future<void> save(Map<String, dynamic> payload) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_key, jsonEncode(payload));
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
