import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Persisted API configuration.
///
/// The user sets the base URL and API key once in Settings.
/// The API key is a credential — stored in the OS keychain/Keystore via
/// flutter_secure_storage (audit 2.4), never plaintext SharedPreferences.
/// Base URL and display name are non-secret and stay in SharedPreferences.
class ApiConfig {
  static final ApiConfig _instance = ApiConfig._();
  factory ApiConfig() => _instance;
  ApiConfig._();

  static const String _keyBaseUrl = 'api_base_url';
  static const String _keyApiKey = 'api_api_key';
  static const String _keyUserName = 'api_user_name';

  static const _secure = FlutterSecureStorage();

  /// Production backend URL (Modal).
  static const String defaultBaseUrl = 'https://danielyashwant--rhodey-os-web-endpoint.modal.run';

  String _baseUrl = defaultBaseUrl;
  String _apiKey = '';
  String _userName = '';

  String get baseUrl => _baseUrl;
  String get apiKey => _apiKey;

  /// The tenant's display name (M9.6) — resolved from the API on first
  /// use, then persisted here. Empty until resolved.
  String get userName => _userName;

  bool get isConfigured => _apiKey.isNotEmpty;

  /// Load persisted config from disk.
  Future<void> load() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      _baseUrl = prefs.getString(_keyBaseUrl) ?? defaultBaseUrl;
      // Legacy migration: a previously-plaintext key is moved into secure
      // storage on first load (audit 2.4), then the plaintext copy is removed.
      _apiKey = await _secure.read(key: _keyApiKey) ?? '';
      final legacy = prefs.getString(_keyApiKey);
      if (_apiKey.isEmpty && legacy != null && legacy.isNotEmpty) {
        _apiKey = legacy;
        await _secure.write(key: _keyApiKey, value: legacy);
        await prefs.remove(_keyApiKey);
      } else if (legacy != null && legacy.isNotEmpty && legacy != _apiKey) {
        await prefs.remove(_keyApiKey);
      }
      _userName = prefs.getString(_keyUserName) ?? '';
      debugPrint('[ApiConfig] loaded: baseUrl=$_baseUrl configured=$isConfigured');
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

  /// Persist a new API key (stored in secure storage, not plaintext prefs).
  Future<void> setApiKey(String key) async {
    _apiKey = key.trim();
    await _secure.write(key: _keyApiKey, value: _apiKey);
  }

  /// Persist the tenant's display name (M9.6) — survives restarts.
  Future<void> setUserName(String name) async {
    _userName = name.trim();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_keyUserName, _userName);
  }

  /// Clear all API settings.
  Future<void> clear() async {
    _baseUrl = defaultBaseUrl;
    _apiKey = '';
    _userName = '';
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_keyBaseUrl);
    await prefs.remove(_keyUserName);
    await _secure.delete(key: _keyApiKey);
  }
}
