import 'dart:async';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/config.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/widgets/invoice_file_viewer.dart';

/// Wraps the viewer in a MaterialApp carrying the localization delegates so
/// `AppLocalizations.of(context)` resolves (defaults to English).
Widget _localized(String fileUrl) => MaterialApp(
      localizationsDelegates: appLocalizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: InvoiceFileViewer(fileUrl: fileUrl),
    );

void main() {
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
    AppConfig.apiBaseUrl = AppConfig.defaultApiUrl;
  });

  group('isPdf', () {
    test('detects a .pdf URL (case-insensitive)', () {
      expect(InvoiceFileViewer.isPdf('/api/invoices/file/k/scan.pdf'), isTrue);
      expect(InvoiceFileViewer.isPdf('/api/invoices/file/k/SCAN.PDF'), isTrue);
    });

    test('treats image extensions as not-PDF', () {
      expect(InvoiceFileViewer.isPdf('/api/invoices/file/k/photo.png'), isFalse);
      expect(InvoiceFileViewer.isPdf('/api/invoices/file/k/photo.jpg'), isFalse);
      expect(InvoiceFileViewer.isPdf('/api/invoices/file/k/scan.tiff'), isFalse);
    });

    test('ignores a query string when sniffing the extension', () {
      expect(
        InvoiceFileViewer.isPdf('/api/invoices/file/k/scan.pdf?sig=abc'),
        isTrue,
      );
    });
  });

  group('absoluteUrl', () {
    test('prefixes the configured API host', () {
      AppConfig.apiBaseUrl = 'https://api.example.com';
      expect(
        InvoiceFileViewer.absoluteUrl('/api/invoices/file/k/scan.pdf'),
        'https://api.example.com/api/invoices/file/k/scan.pdf',
      );
    });
  });

  testWidgets('renders the PDF app-bar title and a spinner while the bytes '
      'fetch is in flight', (tester) async {
    // Hold the byte fetch open so the loading state is observable; the title
    // disambiguates the file kind for screen readers even before the page
    // renders. (The native PDF engine has no test-host impl, so we never
    // complete the decode.)
    final gate = Completer<http.Response>();
    ApiClient().debugConfigure(
      client: MockClient((req) => gate.future),
    );

    await tester.pumpWidget(
      _localized('/api/invoices/file/k/scan.pdf'),
    );
    await tester.pump();

    expect(find.widgetWithText(AppBar, 'Invoice PDF'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsOneWidget);

    // Release the fetch with a 400 so the widget reaches a terminal (error)
    // state and no async work outlives the test.
    gate.complete(http.Response('boom', 400));
    await tester.pump();
    await tester.pump();
  });

  testWidgets('shows an error + Retry when the PDF fetch fails', (tester) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async => http.Response('boom', 500)),
    );

    await tester.pumpWidget(
      _localized('/api/invoices/file/k/scan.pdf'),
    );
    // Pump bounded frames until the async fetch resolves into the error state.
    for (var i = 0; i < 20 && find.text('Unable to load PDF').evaluate().isEmpty;
        i++) {
      await tester.pump(const Duration(milliseconds: 20));
    }

    expect(find.text('Unable to load PDF'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
  });

  testWidgets('uses the image title for a non-PDF file', (tester) async {
    await tester.pumpWidget(
      _localized('/api/invoices/file/k/photo.png'),
    );
    await tester.pump();

    expect(find.widgetWithText(AppBar, 'Invoice Image'), findsOneWidget);
    // Images stream via Image.network — no pre-fetch spinner blocks the body.
    expect(find.byType(InteractiveViewer), findsOneWidget);
  });
}
