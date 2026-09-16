import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/widgets/brand_mark.dart';

Widget _host(Widget child) => Directionality(
      textDirection: TextDirection.ltr,
      child: Center(child: child),
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('renders the bundled mark at the requested size', (tester) async {
    await tester.pumpWidget(_host(const BrandMark(size: 48)));

    final image = tester.widget<Image>(find.byType(Image));
    expect((image.image as AssetImage).assetName, BrandMark.assetPath);
    expect(tester.getSize(find.byType(Image)), const Size(48, 48));
  });

  testWidgets('is hidden from assistive technology', (tester) async {
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(_host(const BrandMark()));

    // Decorative wherever it is used: on login the app name sits right below
    // it, and announcing the image too would say the product name twice.
    final exclude = tester.widget<ExcludeSemantics>(
      find.descendant(
        of: find.byType(BrandMark),
        matching: find.byType(ExcludeSemantics),
      ),
    );
    expect(exclude.excluding, isTrue);
    handle.dispose();
  });

  // A pubspec that forgets the asset directory still compiles; the image just
  // fails to load at runtime. So load it the way the app does.
  test('the asset is declared and ships its 2x and 3x variants', () async {
    final bytes = await rootBundle.load(BrandMark.assetPath);
    expect(bytes.lengthInBytes, greaterThan(0));

    final manifest = await AssetManifest.loadFromAssetBundle(rootBundle);
    final ratios = manifest
        .getAssetVariants(BrandMark.assetPath)!
        .map((variant) => variant.targetDevicePixelRatio)
        .toSet();
    expect(ratios, containsAll(<double>[2.0, 3.0]));
  });
}
