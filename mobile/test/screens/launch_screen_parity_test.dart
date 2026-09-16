// The native launch screens copy the first Flutter frame (docs/decisions.md
// §173): the Android res and the iOS storyboard duplicate the theme's scaffold
// background and the brand mark's lift above centre, so the hand-off from the OS
// to Flutter neither moves nor recolours anything. Copies drift silently, so
// these read the native files and compare them with the real theme and the real
// SplashScreen layout. A failure names the file to update.
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/main.dart';
import 'package:feohledger_mobile/widgets/brand_mark.dart';

const _androidColors = 'android/app/src/main/res/values/colors.xml';
const _androidLaunchBackgrounds = [
  'android/app/src/main/res/drawable/launch_background.xml',
  'android/app/src/main/res/drawable-v21/launch_background.xml',
];
const _iosStoryboard = 'ios/Runner/Base.lproj/LaunchScreen.storyboard';

String _read(String path) => File(path).readAsStringSync();

RegExpMatch _match(String pattern, String path) {
  final match = RegExp(pattern).firstMatch(_read(path));
  expect(match, isNotNull, reason: '$path no longer contains $pattern');
  return match!;
}

/// The theme's scaffold background as 0xRRGGBB, dropping alpha.
int _themeBackground(WidgetTester tester) =>
    Theme.of(tester.element(find.byType(SplashScreen)))
        .scaffoldBackgroundColor
        .toARGB32() &
    0xFFFFFF;

Future<void> _pumpSplash(WidgetTester tester) => tester.pumpWidget(
      MaterialApp(theme: buildAppTheme(), home: const SplashScreen()),
    );

void main() {
  testWidgets('the Android launch window uses the theme background',
      (tester) async {
    await _pumpSplash(tester);

    final hex = _match(
      r'<color name="window_background">#([0-9A-Fa-f]{6})</color>',
      _androidColors,
    ).group(1)!;
    expect(
      int.parse(hex, radix: 16),
      _themeBackground(tester),
      reason: 'update window_background in $_androidColors',
    );
  });

  testWidgets('the iOS launch screen uses the theme background',
      (tester) async {
    await _pumpSplash(tester);

    final color = _match(
      r'<color key="backgroundColor" red="([0-9.]+)" green="([0-9.]+)" '
      r'blue="([0-9.]+)"',
      _iosStoryboard,
    );
    int channel(int group) => (double.parse(color.group(group)!) * 255).round();
    final rgb = (channel(1) << 16) | (channel(2) << 8) | channel(3);
    expect(
      rgb,
      _themeBackground(tester),
      reason: 'update the backgroundColor in $_iosStoryboard',
    );
  });

  testWidgets('both launch screens place the mark where SplashScreen does',
      (tester) async {
    await _pumpSplash(tester);

    // SplashScreen centres a column of mark, gap and spinner, so the mark sits
    // above the screen centre by half of what follows it.
    final lift = tester.getCenter(find.byType(Scaffold)).dy -
        tester.getCenter(find.byType(BrandMark)).dy;
    final markSide = tester.getSize(find.byType(BrandMark)).width;
    expect(lift, greaterThan(0));

    for (final path in _androidLaunchBackgrounds) {
      // A layer inset from the bottom by 2x moves its centred bitmap up by x.
      final bottom = _match(r'android:bottom="(\d+)dp"', path).group(1)!;
      expect(
        double.parse(bottom),
        lift * 2,
        reason: 'update android:bottom in $path',
      );
    }

    final constant = _match(
      r'firstAttribute="centerY"[^>]*constant="(-?[0-9.]+)"',
      _iosStoryboard,
    ).group(1)!;
    expect(
      double.parse(constant),
      -lift,
      reason: 'update the centerY constant in $_iosStoryboard',
    );

    final imageWidth = _match(
      r'<image name="LaunchImage" width="([0-9.]+)"',
      _iosStoryboard,
    ).group(1)!;
    expect(
      double.parse(imageWidth),
      markSide,
      reason: 'update the LaunchImage size in $_iosStoryboard',
    );
  });
}
