/// The locale the `intl` formatters format against — the mobile mirror of the
/// web `frontend/src/lib/i18n/formatLocale.ts`.
///
/// **Words and figures must move together.** `LocaleStore` re-localizes the
/// whole tree when the picker changes, but every `NumberFormat` and
/// `DateFormat` in the app was constructed with no locale at all, so a German
/// reader got German copy with `1,234.50` and `Mar 4, 2026`. The ISO currency
/// code drives the *symbol* and the minor-unit count; the locale drives
/// grouping, separators, symbol placement and the order of a date's parts —
/// `€1,234.50` to an `en` reader is `1.234,50 €` to a `de` one, and `Mar 4,
/// 2026` is `04.03.2026`.
///
/// One module-level value rather than a parameter threaded through every call
/// site, for the same reason the web keeps one: a money cell deep in a list
/// tile has no business knowing about the picker, and a formatter that has to
/// be *given* the locale is a formatter that will eventually be given nothing.
/// [activeFormatLocale] is what `utils/money.dart` and `utils/dates.dart`
/// default to.
///
/// It is kept in sync by [FormatLocaleScope], which reads the locale
/// `MaterialApp` actually resolved — so "follow the system locale" (a `null`
/// `LocaleStore.locale`) and a negotiated fallback are both handled by the one
/// authority that already decides them, rather than re-derived here.
library;

import 'package:flutter/widgets.dart';
import 'package:intl/intl.dart';

/// The active locale tag (`de`, `pt_BR`, …), or `null` to let `intl` use its
/// own default.
///
/// `null` is the pre-picker behaviour — nothing regresses until a locale is
/// actually resolved.
String? _activeFormatLocale;

/// The locale the formatters use. `null` means "whatever `intl` defaults to".
String? get activeFormatLocale => _activeFormatLocale;

/// Point the formatters at [locale] (an `intl` tag such as `de` or `pt_BR`).
///
/// Deliberately does NOT assign [Intl.defaultLocale]. That would make a
/// locale-less `DateFormat` anywhere in the process format for the reader,
/// which sounds like belt and braces and is really a trap: `intl` loads date
/// symbols per locale, so a default with none turns every such formatter into
/// a throw, including the fallback path in [dateFormatLocale]. The app's two
/// formatting modules pass the locale explicitly instead, and
/// `test/utils/dates_test.dart` fails if a third one appears.
///
/// Returns true when the value changed, so a caller can skip a no-op rebuild.
bool setActiveFormatLocale(String? locale) {
  final trimmed = (locale ?? '').trim();
  final next = trimmed.isEmpty ? null : Intl.canonicalizedLocale(trimmed);
  if (next == _activeFormatLocale) return false;
  _activeFormatLocale = next;
  return true;
}

/// The locale to hand a `DateFormat`, or `null` to let `intl` use its own
/// default when it holds no date symbols for the active one.
///
/// Date symbols are loaded per locale (`flutter_localizations` initializes the
/// ones it resolves, so every locale this app can actually be in has them);
/// a `DateFormat` constructed against a locale with none throws. This is a
/// capability check, not a fallback for a bad value — the alternative to
/// formatting in `intl`'s built-in default is not formatting at all. Number
/// symbols are compiled in for every locale, so `NumberFormat` needs no
/// equivalent.
String? dateFormatLocale([String? override]) {
  final locale = override ?? _activeFormatLocale;
  if (locale == null) return null;
  try {
    return DateFormat.localeExists(locale) ? locale : null;
  } on Exception {
    // `LocaleDataException`, which `package:intl` does not export, so it
    // cannot be named here. It means no date symbols have been loaded AT ALL
    // — a plain Dart test, or a formatter reached before the localizations
    // delegate ran. That is the same answer as "none for this locale", and
    // the check throwing is not a reason to fail a render.
    return null;
  }
}

/// Keeps [activeFormatLocale] equal to the locale `MaterialApp` resolved.
///
/// Mounted through `MaterialApp.builder`, which builds *below* the
/// `Localizations` widget — so this reads the negotiated locale (the picker's
/// choice, or the device's own resolved against `supportedLocales`) rather
/// than re-implementing that negotiation.
///
/// **What re-formats a figure already on screen is the rebuild, not this
/// widget.** A locale change marks every element that depends on
/// `Localizations` dirty — which is every screen, because each reads
/// `AppLocalizations.of(context)` — and their children are rebuilt with it, by
/// which point the value here is the new one. Words and figures therefore ride
/// the same rebuild; `test/l10n/locale_switch_test.dart` pins that.
///
/// It lives beside the value it maintains rather than in `widgets/`: it renders
/// nothing and exists only to close this seam, the same way the web keeps its
/// setter next to `getActiveFormatLocale`.
class FormatLocaleScope extends StatelessWidget {
  final Widget child;

  const FormatLocaleScope({super.key, required this.child});

  @override
  Widget build(BuildContext context) {
    // Set during build, read by descendants formatting in the same frame.
    setActiveFormatLocale(Localizations.localeOf(context).toString());
    return child;
  }
}
