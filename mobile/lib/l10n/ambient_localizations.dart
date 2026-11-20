/// The reader's [AppLocalizations] for code that has no `BuildContext` — the
/// ChangeNotifier stores, which turn a failure or a server result into the
/// sentence a screen then shows.
///
/// It resolves the same language `MaterialApp` does: the device choice in
/// [LocaleStore] when there is one, else the first system locale this build
/// ships, else English. A string built here is fixed in the language that was
/// current when it was built; a store's next request rebuilds it, so a
/// language switch reaches it on the next refresh.
///
/// Screens keep using `AppLocalizations.of(context)`; this exists only for the
/// stores (see `describeApiError` in `lib/api/api_client.dart`).
library;

import 'dart:ui' show Locale, PlatformDispatcher;

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/stores/locale_store.dart';

AppLocalizations ambientLocalizations() {
  final chosen = LocaleStore.instance.locale;
  if (chosen != null && AppLocalizations.delegate.isSupported(chosen)) {
    return lookupAppLocalizations(chosen);
  }
  for (final system in PlatformDispatcher.instance.locales) {
    if (AppLocalizations.delegate.isSupported(system)) {
      return lookupAppLocalizations(system);
    }
  }
  return lookupAppLocalizations(const Locale('en'));
}
