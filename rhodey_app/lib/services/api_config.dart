import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Persisted API configuration.
///
/// The user sets the base URL and API key once in Settings.
/// Both survive app restarts via SharedPreferences.
class ApiConfig {
  static final ApiConfig _instance = ApiConfig._();
  factory ApiConfig() => _instance;
  ApiConfig._();

  static const String _keyBaseUrl = 'api_base_url';
  static const String _keyApiKey = 'api_api_key';

  /// Production backend URL.
  static const String defaultBaseUrl = 'https://integrated-os.vercel.app';

  String _baseUrl = defaultBaseUrl;
  String _apiKey = '';

  String get baseUrl => _baseUrl;
  String get apiKey => _apiKey;
  bool get isConfigured => _apiKey.isNotEmpty;

  /// Load persisted config from disk.
  Future<void> load() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      _baseUrl = prefs.getString(_keyBaseUrl) ?? defaultBaseUrl;
      _apiKey = prefs.getString(_keyApiKey) ?? '';
      debugPrint('[ApiConfig] loaded: baseUrl=$_baseUrl configured=${isConfigured}');
    } catch (e) {
      debugPrint('[ApiConfig] load error: $e (using defaults)');
    }
  }

  /// Persist a new base URL.
  Future<void> setBaseUrl(String url) async {
    _baseUrl = url.endsWith('/') ? url.substring(0, url.length - 1) : url;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_keyBaseUrl, _baseUrl);
  }

  /// Persist a new API key.
  Future<void> setApiKey(String key) async {
    _apiKey = key.trim();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_keyApiKey, _apiKey);
  }

  /// Clear all API settings.
  Future<void> clear() async {
    _baseUrl = defaultBaseUrl;
    _apiKey = '';
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_keyBaseUrl);
    await prefs.remove(_keyApiKey);
  }
}
