import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/services/camera_capture.dart';

void main() {
  // FilePicker.pickFile touches a platform channel; the binding must exist
  // before the swallowed MissingPluginException can be observed.
  TestWidgetsFlutterBinding.ensureInitialized();

  group('documentExtensions', () {
    test('covers the backend-accepted document types (PDF/PNG/JPG/TIFF)', () {
      // Mirrors the server ALLOWED_CONTENT_TYPES so the picker can't surface a
      // file the upload would then 422 on.
      expect(
        CameraCapture.documentExtensions,
        containsAll(<String>['pdf', 'png', 'jpg', 'jpeg', 'tiff', 'tif']),
      );
    });

    test('does not offer unsupported types', () {
      expect(CameraCapture.documentExtensions, isNot(contains('docx')));
      expect(CameraCapture.documentExtensions, isNot(contains('xlsx')));
      expect(CameraCapture.documentExtensions, isNot(contains('csv')));
    });
  });

  test('pickDocument returns null (no picker platform impl in tests) without '
      'throwing', () async {
    // FilePicker.pickFile has no test-host implementation; the service
    // swallows the MissingPluginException and returns null rather than throwing.
    expect(await CameraCapture.pickDocument(), isNull);
  });
}
