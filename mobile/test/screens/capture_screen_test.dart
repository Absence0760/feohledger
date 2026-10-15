import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/capture_screen.dart';

/// Wraps the screen in a MaterialApp carrying the localization delegates so the
/// localized `AppLocalizations.of(context)` resolves (defaults to English).
Widget _localized() => MaterialApp(
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: const CaptureScreen(),
    );

void main() {
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  testWidgets('renders the app bar title', (tester) async {
    await tester.pumpWidget(_localized());

    expect(find.widgetWithText(AppBar, 'Capture Invoice'), findsOneWidget);
  });

  testWidgets('shows the empty-state prompt and placeholder icon when no '
      'image is selected', (tester) async {
    await tester.pumpWidget(_localized());

    expect(
      find.text('Take a photo, choose from gallery, or pick a file'),
      findsOneWidget,
    );
    // The large camera placeholder icon (size 80) in the empty state.
    expect(find.byIcon(Icons.camera_alt), findsWidgets);
  });

  testWidgets('offers Camera, Gallery and Choose file buttons in the empty '
      'state', (tester) async {
    await tester.pumpWidget(_localized());

    expect(find.widgetWithText(FilledButton, 'Camera'), findsOneWidget);
    expect(find.widgetWithText(OutlinedButton, 'Gallery'), findsOneWidget);
    expect(find.widgetWithText(OutlinedButton, 'Choose file'), findsOneWidget);
  });

  testWidgets('advertises the supported document types in the empty state',
      (tester) async {
    await tester.pumpWidget(_localized());

    expect(find.text('Supports PDF, PNG, JPG and TIFF'), findsOneWidget);
    expect(
      find.text('Take a photo, choose from gallery, or pick a file'),
      findsOneWidget,
    );
  });

  testWidgets('tapping Choose file does not throw or change the static layout '
      '(picker is a no-op platform channel in tests)', (tester) async {
    await tester.pumpWidget(_localized());

    // FilePicker.pickFile has no test platform implementation; the service
    // swallows the MissingPluginException and returns null, so the empty state
    // stays put and no exception escapes.
    await tester.tap(find.widgetWithText(OutlinedButton, 'Choose file'));
    await tester.pump();

    expect(
      find.text('Take a photo, choose from gallery, or pick a file'),
      findsOneWidget,
    );
    expect(find.text('Upload'), findsNothing);
  });

  testWidgets('does not render the selected-image actions (Retake/Upload) '
      'before an image is picked', (tester) async {
    await tester.pumpWidget(_localized());

    // Retake/Upload + the InteractiveViewer image only appear once a file is
    // selected via the (platform-channel) picker, which is unreachable here.
    expect(find.text('Change'), findsNothing);
    expect(find.text('Upload'), findsNothing);
    expect(find.byType(InteractiveViewer), findsNothing);
    expect(find.byType(Image), findsNothing);
  });

  testWidgets('does not show an error message on first render', (tester) async {
    await tester.pumpWidget(_localized());

    // _error is null initially, so no red error text is painted.
    final errorText = tester
        .widgetList<Text>(find.byType(Text))
        .where((t) => t.style?.color == Colors.red);
    expect(errorText, isEmpty);
  });

  testWidgets('tapping Camera does not throw or change the static layout '
      '(picker is a no-op platform channel in tests)', (tester) async {
    await tester.pumpWidget(_localized());

    // CameraCapture.pickImage swallows platform-channel failures and returns
    // null, so the empty state stays put and no exception escapes.
    await tester.tap(find.widgetWithText(FilledButton, 'Camera'));
    await tester.pump();

    expect(
      find.text('Take a photo, choose from gallery, or pick a file'),
      findsOneWidget,
    );
    expect(find.text('Change'), findsNothing);
  });

  testWidgets('tapping Gallery does not throw or change the static layout',
      (tester) async {
    await tester.pumpWidget(_localized());

    await tester.tap(find.widgetWithText(OutlinedButton, 'Gallery'));
    await tester.pump();

    expect(
      find.text('Take a photo, choose from gallery, or pick a file'),
      findsOneWidget,
    );
    expect(find.text('Upload'), findsNothing);
  });
}
