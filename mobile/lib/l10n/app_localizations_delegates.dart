import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';

/// The localization delegates every `MaterialApp` in the app (and every test
/// host) passes as `localizationsDelegates`.
///
/// Use this, never the generated `AppLocalizations.localizationsDelegates`.
/// Flutter 3.50's gen-l10n still fills that list from
/// `package:flutter_localizations` (flutter/flutter#196475), whose delegates
/// load the *legacy* `package:flutter/material.dart` `MaterialLocalizations`
/// type. The widgets here come from `package:material_ui`, which looks up its
/// own, distinct `MaterialLocalizations`. Under `en` that is masked by the
/// built-in English default; under `de` or `ja` there is none at all, so the
/// back button, tooltips, date pickers and text-field menus fail their lookup
/// (a debug assertion, a null-check crash in release).
///
/// `GlobalMaterialLocalizations.delegates` (from `material_ui`) carries the
/// Material, Cupertino and Widgets delegates together, as the migration guide
/// prescribes. Pinned by `test/l10n/material_delegates_test.dart`.
const List<LocalizationsDelegate<dynamic>> appLocalizationsDelegates =
    <LocalizationsDelegate<dynamic>>[
      AppLocalizations.delegate,
      ...GlobalMaterialLocalizations.delegates,
    ];
