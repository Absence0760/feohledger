import 'dart:io';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';

// The app's widgets come from `package:material_ui`, whose
// `MaterialLocalizations` is a different type from the legacy
// `package:flutter/material.dart` one. Flutter 3.50's gen-l10n still builds
// `AppLocalizations.localizationsDelegates` from `flutter_localizations`,
// i.e. from the legacy type (flutter/flutter#196475). A MaterialApp handed
// that list in a non-English locale localizes our own strings but has no
// material_ui MaterialLocalizations at all, so every widget that needs one
// (back button, tooltips, pickers, text-field menus) fails its lookup: a
// debug assertion, a null-check crash in release. These tests pin the
// shared `appLocalizationsDelegates` list, and keep the generated one out of
// the code base until gen-l10n catches up.

/// The `material_ui` MaterialLocalizations a `de` MaterialApp built with
/// [delegates] provides, or null when none of them serves that type. (Handed
/// the generated list instead, WidgetsApp reports "A MaterialLocalizations
/// delegate that supports the de locale was not found", observed on 3.50.0.)
Future<MaterialLocalizations?> _materialStringsUnder(
  WidgetTester tester,
  List<LocalizationsDelegate<dynamic>> delegates,
) async {
  MaterialLocalizations? strings;
  await tester.pumpWidget(
    MaterialApp(
      locale: const Locale('de'),
      localizationsDelegates: delegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: Builder(
        builder: (context) {
          strings = Localizations.of<MaterialLocalizations>(
            context,
            MaterialLocalizations,
          );
          return const SizedBox.shrink();
        },
      ),
    ),
  );
  await tester.pumpAndSettle();
  return strings;
}

void main() {
  testWidgets('appLocalizationsDelegates localizes material_ui widgets', (
    tester,
  ) async {
    final strings = await _materialStringsUnder(
      tester,
      appLocalizationsDelegates,
    );
    expect(strings?.cancelButtonLabel, 'Abbrechen');
  });

  test('nothing hand-written uses AppLocalizations.localizationsDelegates', () {
    const banned = 'AppLocalizations.localizationsDelegates';
    const self = 'test/l10n/material_delegates_test.dart';
    final offenders = <String>[];
    for (final root in ['lib', 'test']) {
      for (final entity in Directory(root).listSync(recursive: true)) {
        if (entity is! File || !entity.path.endsWith('.dart')) continue;
        final path = entity.path.replaceAll(r'\', '/');
        if (path.startsWith('lib/l10n/gen/') || path == self) continue;
        // Doc comments may name it to explain why it is avoided.
        final code = entity
            .readAsLinesSync()
            .where((line) => !line.trimLeft().startsWith('//'))
            .join('\n');
        if (code.contains(banned)) offenders.add(path);
      }
    }
    expect(
      offenders,
      isEmpty,
      reason: 'use appLocalizationsDelegates (lib/l10n/app_localizations_delegates.dart)',
    );
  });

  test('no Dart file imports the deprecated in-framework Material library', () {
    // Belt and braces for `flutter analyze`'s deprecated_member_use, which a
    // local `// ignore` could silence one file at a time.
    final legacy = RegExp(
      r'''^import\s+['"]package:flutter/(material|cupertino)\.dart['"]''',
      multiLine: true,
    );
    final offenders = <String>[
      for (final root in ['lib', 'test'])
        for (final entity in Directory(root).listSync(recursive: true))
          if (entity is File &&
              entity.path.endsWith('.dart') &&
              legacy.hasMatch(entity.readAsStringSync()))
            entity.path,
    ];
    expect(offenders, isEmpty);
  });
}
