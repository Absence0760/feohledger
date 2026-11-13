import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Per-device display-language preference.
///
/// Language is a **device** choice (like the biometric-unlock toggle), NOT an
/// account-roamed setting — it is stored locally via [FlutterSecureStorage]
/// (the same device-prefs mechanism `BiometricService` uses) and never sent to
/// the backend. `null` means "follow the system locale" (`MaterialApp` resolves
/// the device locale against `supportedLocales`).
///
/// This is the mobile counterpart of the web `setLocale()` in
/// `frontend/src/lib/i18n/store.svelte.ts` (which persists to `localStorage`
/// under `feoh_locale`, also device-scoped).
class LocaleStore extends ChangeNotifier {
  static final LocaleStore instance = LocaleStore._();
  LocaleStore._();

  final _storage = const FlutterSecureStorage();
  static const _localeKey = 'display_locale';

  /// The six locales the app ships translations for. Order matches the picker
  /// and the web `SUPPORTED_LOCALES` (en, de, fr, es, pt-BR, ja).
  static const supportedLocales = <Locale>[
    Locale('en'),
    Locale('de'),
    Locale('fr'),
    Locale('es'),
    Locale('pt', 'BR'),
    Locale('ja'),
  ];

  /// Endonyms shown in the picker — each language's own name. Keyed by the
  /// canonical tag produced by [tagOf].
  static const endonyms = <String, String>{
    'en': 'English',
    'de': 'Deutsch',
    'fr': 'Français',
    'es': 'Español',
    'pt-BR': 'Português (Brasil)',
    'ja': '日本語',
  };

  Locale? _locale;
  bool _loaded = false;

  /// The selected locale, or `null` to follow the system default.
  Locale? get locale => _locale;
  bool get loaded => _loaded;

  /// A stable string tag for a locale (`en`, `pt-BR`, …) used as a map key and
  /// for persistence. Mirrors the web BCP-47 tags.
  static String tagOf(Locale locale) =>
      locale.countryCode == null || locale.countryCode!.isEmpty
          ? locale.languageCode
          : '${locale.languageCode}-${locale.countryCode}';

  static Locale? _fromTag(String? tag) {
    if (tag == null || tag.isEmpty) return null;
    for (final l in supportedLocales) {
      if (tagOf(l) == tag) return l;
    }
    return null;
  }

  /// Load the persisted choice. Call once on app start before building
  /// `MaterialApp`. Safe to call more than once.
  Future<void> init() async {
    if (_loaded) return;
    try {
      _locale = _fromTag(await _storage.read(key: _localeKey));
    } on Exception {
      // No secure-storage backend (e.g. test VM) — fall back to system locale.
      _locale = null;
    }
    _loaded = true;
    notifyListeners();
  }

  /// Set (or clear, with `null`) the device display language. Persisted
  /// locally; `MaterialApp.locale` updates reactively via the notifier.
  Future<void> setLocale(Locale? locale) async {
    _locale = locale;
    notifyListeners();
    try {
      if (locale == null) {
        await _storage.delete(key: _localeKey);
      } else {
        await _storage.write(key: _localeKey, value: tagOf(locale));
      }
    } on Exception {
      // Persistence is best-effort; the in-memory choice still applies for the
      // session even if the write fails.
    }
  }
}
