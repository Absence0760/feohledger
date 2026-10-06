import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;

import 'package:feohledger_mobile/config.dart';
import 'package:feohledger_mobile/services/session.dart';

class ApiException implements Exception {
  final int statusCode;
  final String message;

  /// The response's raw decoded `detail`, when a caller needs its structure —
  /// a structured refusal carries a stable `code` the client localizes from
  /// (`lib/l10n/gl_chart_refusal_messages.dart`), with [message] as the
  /// English fallback.
  final Object? detail;

  ApiException(this.statusCode, this.message, {this.detail});

  @override
  String toString() => 'ApiException($statusCode): $message';
}

/// User-facing text for anything thrown out of [ApiClient].
///
/// [ApiException.toString] prefixes the status code (`ApiException(403): ...`),
/// which belongs in a log, not in a snackbar. Use this wherever an error is
/// rendered to a person; [ApiException.message] is already the server's
/// human-readable `detail` (see [ApiClient.errorMessage]).
String describeApiError(Object error) =>
    error is ApiException ? error.message : error.toString();

class ApiClient {
  static final ApiClient _instance = ApiClient._();
  factory ApiClient() => _instance;
  ApiClient._();

  final _storage = const FlutterSecureStorage();
  http.Client _http = http.Client();

  static const _tokenKey = 'auth_token';
  static const _tenantKey = 'tenant_slug';

  /// Request timeout applied by [get], [post], [getList], [patch] and
  /// [delete] (the JSON/no-body calls — [getBytes]/[postBytes] set their own
  /// longer timeout for file transfers). Overridable only via
  /// [debugConfigure] so tests can shrink it instead of waiting out the real
  /// 10s in a hanging-request test.
  static const _defaultTimeout = Duration(seconds: 10);
  Duration _timeout = _defaultTimeout;

  String? _token;
  String? _tenantSlug;

  // Called on app start to restore session
  Future<void> init() async {
    _token = await _storage.read(key: _tokenKey);
    _tenantSlug = await _storage.read(key: _tenantKey);
    if (_tenantSlug != null) {
      AppConfig.tenantSlug = _tenantSlug;
    }
  }

  bool get hasToken => _token != null;

  /// Test seam: swap the underlying HTTP client for a fake, optionally shrink
  /// the request [timeout] (defaults back to the real 10s when omitted so
  /// tests don't leak a short timeout into each other), and reset the
  /// in-memory session so each test starts from a clean singleton. Not used
  /// by production code — guarded by [visibleForTesting].
  @visibleForTesting
  void debugConfigure({http.Client? client, Duration? timeout}) {
    if (client != null) _http = client;
    _timeout = timeout ?? _defaultTimeout;
    _token = null;
    _tenantSlug = null;
    AppConfig.tenantSlug = null;
  }

  Future<void> setToken(String token) async {
    _token = token;
    await _storage.write(key: _tokenKey, value: token);
  }

  Future<void> setTenant(String slug) async {
    _tenantSlug = slug;
    AppConfig.tenantSlug = slug;
    await _storage.write(key: _tenantKey, value: slug);
  }

  /// End the session everywhere it exists on the device: credentials, the
  /// offline SQLite cache, and the store singletons. This is the single exit
  /// path — explicit logout, a 401 on a request that CARRIED a token (expired /
  /// revoked), and a failed session restore all land here — so no forced-logout
  /// route can leave one user's financial data readable by the next one.
  ///
  /// Only the two **session** keys are deleted, never `deleteAll()`: the same
  /// [FlutterSecureStorage] also holds device preferences that outlive any
  /// account — the biometric-unlock toggle (`BiometricService`) and the display
  /// language (`LocaleStore`). Wiping those on sign-out silently turned Face ID
  /// off and reset the UI language, which is not what ending a session means.
  Future<void> clearSession() async {
    _token = null;
    _tenantSlug = null;
    AppConfig.tenantSlug = null;
    await _storage.delete(key: _tokenKey);
    await _storage.delete(key: _tenantKey);
    await SessionManager.endSession();
  }

  /// Handle a 401 uniformly across every verb.
  ///
  /// A 401 only ends the session when a credential was actually **presented**.
  /// `/auth/login`, `/auth/mfa/verify` and `/auth/mfa/challenge/email` return
  /// 401 for a wrong password / mistyped code while no token exists yet — and
  /// tearing the session down there deleted the `tenant_slug` the half-finished
  /// login still needs. The user would then retype the code, authenticate
  /// against the (control-plane-only) auth routes, land on the home screen, and
  /// have every tenant-scoped request go out with no `X-Tenant-Slug`.
  Future<void> _handleUnauthorized() async {
    if (_token != null) await clearSession();
  }

  /// Human-readable text for a non-2xx response.
  ///
  /// FastAPI errors arrive as `{"detail": "..."}`; pasting that raw JSON into a
  /// snackbar is how a legitimate refusal (e.g. the payment-run CFO gate) shows
  /// up as `{"detail":"This run exceeds..."}` in the UI. Falls back to a plain
  /// status line rather than echoing an un-decodable body (an HTML error page
  /// is worse than saying nothing).
  ///
  /// A structured refusal (`{code, …, message}` — the GL-chart refusal) yields
  /// its English `message` here; a screen that can state it in the reader's
  /// language reads [ApiException.detail] instead (see
  /// `lib/l10n/gl_chart_refusal_messages.dart`).
  static String errorMessage(http.Response response) {
    final body = response.body;
    if (body.isNotEmpty) {
      try {
        final decoded = jsonDecode(body);
        if (decoded is Map<String, dynamic>) {
          final detail = decoded['detail'];
          if (detail is String && detail.trim().isNotEmpty) return detail;
          if (detail is Map<String, dynamic>) {
            final message = detail['message'];
            if (message is String && message.trim().isNotEmpty) return message;
          }
          // 422 validation errors: `detail` is a list of {loc, msg, type}.
          if (detail is List) {
            final messages = detail
                .whereType<Map<String, dynamic>>()
                .map((e) => e['msg'])
                .whereType<String>()
                .toList();
            if (messages.isNotEmpty) return messages.join('; ');
          }
        }
      } catch (_) {
        // Not JSON — fall through to the status line.
      }
    }
    return 'Request failed (${response.statusCode})';
  }

  /// The response's raw `detail`, decoded — for a caller that needs its
  /// STRUCTURE rather than [errorMessage]'s sentence. `null` when the body is
  /// empty, not JSON, or carries no `detail`.
  static Object? errorDetail(http.Response response) {
    if (response.body.isEmpty) return null;
    try {
      final decoded = jsonDecode(response.body);
      return decoded is Map<String, dynamic> ? decoded['detail'] : null;
    } catch (_) {
      return null;
    }
  }

  static ApiException _failure(http.Response response) => ApiException(
        response.statusCode,
        errorMessage(response),
        detail: errorDetail(response),
      );

  /// Auth + tenant headers for JSON requests.
  Map<String, String> get _headers {
    final headers = <String, String>{
      'Content-Type': 'application/json',
    };
    if (_token != null) {
      headers['Authorization'] = 'Bearer $_token';
    }
    if (_tenantSlug != null) {
      headers['X-Tenant-Slug'] = _tenantSlug!;
    }
    return headers;
  }

  /// Auth + tenant headers without Content-Type (for multipart uploads).
  Map<String, String> get authHeaders {
    final headers = <String, String>{};
    if (_token != null) {
      headers['Authorization'] = 'Bearer $_token';
    }
    if (_tenantSlug != null) {
      headers['X-Tenant-Slug'] = _tenantSlug!;
    }
    return headers;
  }

  Uri _uri(String path, [Map<String, String>? params]) {
    return Uri.parse('${AppConfig.apiUrl}$path').replace(
      queryParameters: params,
    );
  }

  Future<Map<String, dynamic>> get(
    String path, [
    Map<String, String>? params,
  ]) async {
    final uri = _uri(path, params);
    debugPrint('[API] GET $uri');
    try {
      final response = await _http
          .get(uri, headers: _headers)
          .timeout(_timeout);
      debugPrint('[API] GET $path → ${response.statusCode}');
      // `await` inside the try, not a bare `return` of the future: returning
      // it unawaited hands the future to the caller and unwinds the try
      // first, so the catch below never saw an ApiException / decode failure
      // — i.e. the failure log was dead for exactly the responses it exists
      // for. (Dart's `unawaited_return_in_try_block` lint flags this.)
      return await _handleResponse(response);
    } catch (e) {
      debugPrint('[API] GET $path FAILED: $e');
      rethrow;
    }
  }

  Future<List<dynamic>> getList(
    String path, [
    Map<String, String>? params,
  ]) async {
    final response = await _http
        .get(_uri(path, params), headers: _headers)
        .timeout(_timeout);
    return _handleListResponse(response);
  }

  Future<Map<String, dynamic>> post(
    String path, [
    Map<String, dynamic>? body,
  ]) async {
    final uri = _uri(path);
    debugPrint('[API] POST $uri');
    try {
      final response = await _http
          .post(
            uri,
            headers: _headers,
            body: body != null ? jsonEncode(body) : null,
          )
          .timeout(_timeout);
      debugPrint('[API] POST $path → ${response.statusCode}');
      // Awaited for the same reason as [get] — see the note there.
      return await _handleResponse(response);
    } catch (e) {
      debugPrint('[API] POST $path FAILED: $e');
      rethrow;
    }
  }

  Future<Map<String, dynamic>> patch(
    String path, [
    Map<String, dynamic>? body,
  ]) async {
    final response = await _http
        .patch(
          _uri(path),
          headers: _headers,
          body: body != null ? jsonEncode(body) : null,
        )
        .timeout(_timeout);
    return _handleResponse(response);
  }

  /// Fetch a file's raw bytes (auth + tenant headers attached) via the
  /// swappable HTTP client. [path] is API-relative (e.g.
  /// `/api/invoices/file/{key}`). Used by the invoice file viewer to load a PDF
  /// the native engine can't fetch with custom headers. A 401 clears the
  /// session; any other non-2xx throws an [ApiException].
  Future<Uint8List> getBytes(String path) async {
    final uri = Uri.parse('${AppConfig.apiBaseUrl}$path');
    final response = await _http
        .get(uri, headers: authHeaders)
        .timeout(const Duration(seconds: 30));
    if (response.statusCode == 401) {
      await _handleUnauthorized();
      throw ApiException(401, 'Unauthorized');
    }
    if (response.statusCode >= 400) {
      throw ApiException(response.statusCode, 'Failed to load file');
    }
    return response.bodyBytes;
  }

  /// POST [body] (JSON) and return the raw response bytes (auth + tenant
  /// headers attached) via the swappable HTTP client. [path] is API-relative
  /// (e.g. `/invoices/bulk/export`). Used by the bulk-export action, whose
  /// endpoint streams a CSV/XML file rather than JSON. A 401 clears the
  /// session; any other non-2xx throws an [ApiException]. Returns the body
  /// bytes plus the server-suggested filename parsed from `Content-Disposition`
  /// (null when absent).
  Future<({Uint8List bytes, String? filename})> postBytes(
    String path, [
    Map<String, dynamic>? body,
  ]) async {
    final uri = _uri(path);
    debugPrint('[API] POST(bytes) $uri');
    final response = await _http
        .post(
          uri,
          headers: _headers,
          body: body != null ? jsonEncode(body) : null,
        )
        .timeout(const Duration(seconds: 30));
    debugPrint('[API] POST(bytes) $path → ${response.statusCode}');
    if (response.statusCode == 401) {
      await _handleUnauthorized();
      throw ApiException(401, 'Unauthorized');
    }
    if (response.statusCode >= 400) {
      throw _failure(response);
    }
    return (
      bytes: response.bodyBytes,
      filename: _filenameFromDisposition(response.headers['content-disposition']),
    );
  }

  /// Pull a `filename="..."` token out of a `Content-Disposition` header.
  /// Returns null when the header is absent or has no filename.
  static String? _filenameFromDisposition(String? disposition) {
    if (disposition == null) return null;
    final match = RegExp(r'filename\*?=(?:UTF-8'
            "''"
            r')?"?([^";]+)"?')
        .firstMatch(disposition);
    return match?.group(1)?.trim();
  }

  Future<void> delete(String path) async {
    final response = await _http
        .delete(_uri(path), headers: _headers)
        .timeout(_timeout);
    // Same forced-logout treatment as every other verb — clearSession()'s
    // "a 401 on any request lands here" is only true if this path honours it.
    if (response.statusCode == 401) {
      await _handleUnauthorized();
      throw ApiException(401, 'Unauthorized');
    }
    if (response.statusCode >= 400) {
      throw _failure(response);
    }
  }

  /// Forced logout on a 401 is **awaited before the throw** so the local
  /// teardown (cache + stores) has finished by the time a caller's `catch`
  /// runs — otherwise an offline-fallback `catch` could still read the cache
  /// of the session being torn down.
  Future<Map<String, dynamic>> _handleResponse(http.Response response) async {
    if (response.statusCode == 401) {
      await _handleUnauthorized();
      throw ApiException(401, 'Unauthorized');
    }
    if (response.statusCode >= 400) {
      throw _failure(response);
    }
    if (response.body.isEmpty) return {};
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  Future<List<dynamic>> _handleListResponse(http.Response response) async {
    if (response.statusCode == 401) {
      await _handleUnauthorized();
      throw ApiException(401, 'Unauthorized');
    }
    if (response.statusCode >= 400) {
      throw _failure(response);
    }
    if (response.body.isEmpty) return [];
    final decoded = jsonDecode(response.body);
    if (decoded is List) return decoded;
    // Backend wraps lists in { "items": [...] } or { "invoices": [...] }
    if (decoded is Map<String, dynamic>) {
      for (final value in decoded.values) {
        if (value is List) return value;
      }
    }
    return [];
  }
}
