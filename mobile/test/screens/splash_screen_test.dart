import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/main.dart';
import 'package:feohledger_mobile/widgets/brand_mark.dart';

void main() {
  // The startup placeholder is what every cold start shows first, and it used to
  // draw Material's stock receipt_long icon in blue.
  testWidgets('the startup placeholder shows the brand mark, not a stock icon',
      (tester) async {
    await tester.pumpWidget(const MaterialApp(home: SplashScreen()));

    expect(find.byType(BrandMark), findsOneWidget);
    expect(find.byType(Icon), findsNothing);
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
  });
}
