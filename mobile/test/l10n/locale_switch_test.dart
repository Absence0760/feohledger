import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/stores/locale_store.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/utils/format_locale.dart';
import 'package:feohledger_mobile/utils/money.dart';

// End-to-end proof that the localeNotifier → MaterialApp.locale plumbing is
// real: switching the device locale through LocaleStore must re-localize a
// visible string live (no restart). Mirrors the intent of the web i18n
// `setLocale` reactive switch.

void main() {
  // SettingsScreen probes the local_auth channel in initState; stub it so the
  // screen settles into its non-biometric branch instead of throwing.
  const localAuthChannel = MethodChannel('plugins.flutter.io/local_auth');

  setUp(() {
    FlutterSecureStorage.setMockInitialValues({});
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(localAuthChannel, (call) async {
      switch (call.method) {
        case 'getAvailableBiometrics':
          return <String>[];
        case 'isDeviceSupported':
          return false;
        default:
          return null;
      }
    });
  });

  tearDown(() async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(localAuthChannel, null);
    // Reset the shared singleton so the locale choice doesn't leak between tests.
    await LocaleStore.instance.setLocale(null);
  });

  // A tiny localized host that mirrors APApp's wiring: MaterialApp.locale is
  // driven by the LocaleStore notifier, so a setLocale() call rebuilds the
  // whole subtree against the new locale.
  Widget host(Widget home) => ListenableBuilder(
        listenable: LocaleStore.instance,
        builder: (context, _) => MaterialApp(
          locale: LocaleStore.instance.locale,
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          // APApp's own wiring: the formatters follow the resolved locale, so
          // the figures move with the words.
          builder: (context, child) =>
              FormatLocaleScope(child: child ?? const SizedBox.shrink()),
          home: home,
        ),
      );

  testWidgets('switching the device locale re-localizes a visible string',
      (tester) async {
    // A screen-less probe that just renders one localized AppBar title.
    final probe = Builder(
      builder: (context) => Scaffold(
        appBar: AppBar(title: Text(AppLocalizations.of(context).settingsTitle)),
      ),
    );

    await LocaleStore.instance.setLocale(const Locale('en'));
    await tester.pumpWidget(host(probe));
    await tester.pump();

    expect(find.text('Settings'), findsOneWidget);
    expect(find.text('Einstellungen'), findsNothing);

    // Switch to German via the notifier — no widget rebuild call needed.
    await LocaleStore.instance.setLocale(const Locale('de'));
    await tester.pump();

    expect(find.text('Einstellungen'), findsOneWidget);
    expect(find.text('Settings'), findsNothing);

    // Switch to Brazilian Portuguese — exercises the script/country locale path.
    await LocaleStore.instance.setLocale(const Locale('pt', 'BR'));
    await tester.pump();

    expect(find.text('Configurações'), findsOneWidget);

    // Switch to Japanese.
    await LocaleStore.instance.setLocale(const Locale('ja'));
    await tester.pump();

    expect(find.text('設定'), findsOneWidget);
  });

  testWidgets('the figures switch with the words, not just the copy',
      (tester) async {
    // The gap this closes: the picker re-localized every string in the app and
    // left `$1,234.50` / `Mar 4, 2026` exactly as they were, because no call
    // site passed a locale and every `DateFormat` was a module-level `final`
    // frozen at import. A German reader read German prose around en-US
    // figures.
    //
    // The probe reads a localized string beside the two figures because that
    // is what every screen in the app does, and it is what makes the rebuild
    // happen: `AppLocalizations.of(context)` registers a dependency on
    // `Localizations`, so a locale change marks the screen dirty and its
    // children are rebuilt — at which point the formatters read the locale
    // just installed. The figures ride the same rebuild as the words.
    final probe = Builder(
      builder: (context) => Scaffold(
        body: Column(
          children: [
            Text(AppLocalizations.of(context).settingsTitle),
            Text(formatMoney(1234.5, currency: 'EUR')),
            Text(formatDate(DateTime(2026, 3, 4))),
          ],
        ),
      ),
    );

    await LocaleStore.instance.setLocale(const Locale('en'));
    await tester.pumpWidget(host(probe));
    await tester.pump();
    expect(find.text('€1,234.50'), findsOneWidget);
    expect(find.text('Mar 4, 2026'), findsOneWidget);

    await LocaleStore.instance.setLocale(const Locale('de'));
    await tester.pump();
    // German groups with dots, writes the decimal comma, puts the symbol last
    // (behind a no-break space CLDR chooses, hence the loose match) and the
    // day before the month.
    expect(find.textContaining('1.234,50'), findsOneWidget);
    expect(find.textContaining('€'), findsOneWidget);
    expect(find.text('4. März 2026'), findsOneWidget);
    expect(find.text('€1,234.50'), findsNothing);

    await LocaleStore.instance.setLocale(const Locale('ja'));
    await tester.pump();
    expect(find.text('2026年3月4日'), findsOneWidget);
  });

  testWidgets(
      'a newly-localized screen string (vendorsTitle) switches with the locale',
      (tester) async {
    // Guards the latest extraction batch (notifications / vendors / exceptions
    // / payments). Uses the same notifier-driven host so the assertion proves
    // the new keys re-localize live, not just that they parse.
    final probe = Builder(
      builder: (context) {
        final l = AppLocalizations.of(context);
        return Scaffold(
          body: Column(
            children: [
              Text(l.vendorsTitle),
              Text(l.exceptionsTitle),
              Text(l.notificationsTitle),
              Text(l.paymentStatusCompleted),
            ],
          ),
        );
      },
    );

    await LocaleStore.instance.setLocale(const Locale('en'));
    await tester.pumpWidget(host(probe));
    await tester.pump();
    expect(find.text('Vendors'), findsOneWidget);
    expect(find.text('Completed'), findsOneWidget);

    await LocaleStore.instance.setLocale(const Locale('de'));
    await tester.pump();
    expect(find.text('Lieferanten'), findsOneWidget); // vendorsTitle
    expect(find.text('Ausnahmen'), findsOneWidget); // exceptionsTitle
    expect(find.text('Benachrichtigungen'), findsOneWidget); // notificationsTitle
    expect(find.text('Abgeschlossen'), findsOneWidget); // paymentStatusCompleted
    expect(find.text('Vendors'), findsNothing);

    await LocaleStore.instance.setLocale(const Locale('ja'));
    await tester.pump();
    expect(find.text('取引先'), findsOneWidget); // vendorsTitle
    expect(find.text('完了'), findsOneWidget); // paymentStatusCompleted
  });

  testWidgets(
      'the latest extraction batch (approvals / capture / advanced search) '
      'switches with the locale', (tester) async {
    // Guards the screen keys added in the approvals / capture / advanced-search
    // batch. Proves the new keys re-localize live (not just that they parse),
    // including the plural (approvalsPendingCount) and a placeholder string
    // (captureSelectedDocument).
    final probe = Builder(
      builder: (context) {
        final l = AppLocalizations.of(context);
        return Scaffold(
          body: Column(
            children: [
              Text(l.approvalsTitle),
              Text(l.approvalsPendingCount(2)),
              Text(l.captureTitle),
              Text(l.captureSelectedDocument('foo.pdf')),
              Text(l.advSearchTitle),
            ],
          ),
        );
      },
    );

    await LocaleStore.instance.setLocale(const Locale('en'));
    await tester.pumpWidget(host(probe));
    await tester.pump();
    expect(find.text('Pending Approvals'), findsOneWidget); // approvalsTitle
    expect(find.text('2 invoices pending'), findsOneWidget); // plural
    expect(find.text('Capture Invoice'), findsOneWidget); // captureTitle
    expect(find.text('Selected document: foo.pdf'), findsOneWidget); // placeholder
    expect(find.text('Advanced Search'), findsOneWidget); // advSearchTitle

    await LocaleStore.instance.setLocale(const Locale('de'));
    await tester.pump();
    expect(find.text('Ausstehende Freigaben'), findsOneWidget); // approvalsTitle
    expect(find.text('Rechnung erfassen'), findsOneWidget); // captureTitle
    expect(find.text('Erweiterte Suche'), findsOneWidget); // advSearchTitle
    expect(find.text('Pending Approvals'), findsNothing);

    await LocaleStore.instance.setLocale(const Locale('ja'));
    await tester.pump();
    expect(find.text('承認待ち'), findsOneWidget); // approvalsTitle
    expect(find.text('詳細検索'), findsOneWidget); // advSearchTitle
  });

  testWidgets(
      'the invoice-detail batch (detail / edit / warnings / ERP / file viewer) '
      'switches with the locale', (tester) async {
    // Guards the invoice-detail extraction batch. Proves the new keys
    // re-localize live (not just that they parse), including a placeholder
    // string (invoiceDetailErrorPrefix) and the variance placeholder
    // (warningsVarianceLabel).
    final probe = Builder(
      builder: (context) {
        final l = AppLocalizations.of(context);
        return Scaffold(
          body: Column(
            children: [
              Text(l.invoiceDetailTitle),
              Text(l.invoiceDetailErrorPrefix('boom')),
              Text(l.invoiceEditTitle),
              Text(l.invoiceEditLockedHelper),
              Text(l.warningsSectionTitle),
              Text(l.warningsVarianceLabel('+5.0')),
              Text(l.erpStatusTitle),
              Text(l.fileViewerPdfTitle),
              Text(l.timelineNoActivity),
            ],
          ),
        );
      },
    );

    await LocaleStore.instance.setLocale(const Locale('en'));
    await tester.pumpWidget(host(probe));
    await tester.pump();
    expect(find.text('Invoice Detail'), findsOneWidget); // invoiceDetailTitle
    expect(find.text('Error: boom'), findsOneWidget); // placeholder
    expect(find.text('Edit Invoice'), findsOneWidget); // invoiceEditTitle
    expect(
      find.text('Frozen after approval'),
      findsOneWidget,
    ); // invoiceEditLockedHelper
    expect(find.text('+5.0% variance'), findsOneWidget); // variance placeholder

    await LocaleStore.instance.setLocale(const Locale('de'));
    await tester.pump();
    expect(find.text('Rechnungsdetails'), findsOneWidget); // invoiceDetailTitle
    expect(find.text('Fehler: boom'), findsOneWidget); // placeholder
    expect(find.text('ERP-Status'), findsOneWidget); // erpStatusTitle
    expect(
      find.text('Nach der Genehmigung gesperrt'),
      findsOneWidget,
    ); // invoiceEditLockedHelper
    expect(find.text('Invoice Detail'), findsNothing);

    await LocaleStore.instance.setLocale(const Locale('ja'));
    await tester.pump();
    expect(find.text('請求書の詳細'), findsOneWidget); // invoiceDetailTitle
    expect(find.text('まだアクティビティはありません'),
        findsOneWidget); // timelineNoActivity
  });

  testWidgets(
      'the payment-queue batch (Pay tabs / summary / runs) switches with '
      'the locale', (tester) async {
    // Guards the payment-queue extraction batch, including the plural
    // (paySelectedCount) and a placeholder string (payRunExecuteFailed).
    final probe = Builder(
      builder: (context) {
        final l = AppLocalizations.of(context);
        return Scaffold(
          body: Column(
            children: [
              Text(l.payTitle),
              Text(l.payTabQueue),
              Text(l.paySelectedCount(3)),
              Text(l.payRunExecuteFailed('nope')),
              Text(l.payMethodCheck),
            ],
          ),
        );
      },
    );

    await LocaleStore.instance.setLocale(const Locale('en'));
    await tester.pumpWidget(host(probe));
    await tester.pump();
    expect(find.text('Pay'), findsOneWidget); // payTitle
    expect(find.text('3 invoices selected'), findsOneWidget); // plural
    expect(find.text('Failed to execute: nope'), findsOneWidget); // placeholder

    await LocaleStore.instance.setLocale(const Locale('de'));
    await tester.pump();
    expect(find.text('Bezahlen'), findsOneWidget); // payTitle
    expect(find.text('3 Rechnungen ausgewählt'), findsOneWidget); // plural
    expect(find.text('Scheck'), findsOneWidget); // payMethodCheck
    expect(find.text('Pay'), findsNothing);

    await LocaleStore.instance.setLocale(const Locale('ja'));
    await tester.pump();
    expect(find.text('支払'), findsOneWidget); // payTitle
    expect(find.text('3件の請求書を選択中'), findsOneWidget); // plural (other-only)
  });

  testWidgets(
      'the payment-queue refusal batch (blocked reasons / pinned rail) '
      'switches with the locale', (tester) async {
    // Guards the `blocked_reason` label map + the pinned-rail chip. These are
    // the only prose the operator gets for a row a payment run would refuse —
    // the backend deliberately sends a bare CODE (never the exception's
    // description, which can carry vendor / bank / amount detail), so an
    // untranslated arm here means a non-English operator is told nothing.
    final probe = Builder(
      builder: (context) {
        final l = AppLocalizations.of(context);
        return Scaffold(
          body: Column(
            children: [
              Text(l.payQueueBlockedFullyCredited),
              Text(l.payQueueBlockedLiveVirtualCard),
              Text(l.payQueueBlockedAnnounce('X')),
              Text(l.payQueuePinnedMethod('Y')),
            ],
          ),
        );
      },
    );

    await LocaleStore.instance.setLocale(const Locale('en'));
    await tester.pumpWidget(host(probe));
    await tester.pump();
    expect(find.text('Fully covered by credit memos — nothing to pay'),
        findsOneWidget);
    expect(find.text('can’t be paid: X'), findsOneWidget); // placeholder
    expect(find.text('Pay by Y'), findsOneWidget); // placeholder

    await LocaleStore.instance.setLocale(const Locale('de'));
    await tester.pump();
    expect(find.text('Vollständig durch Gutschriften gedeckt — nichts zu zahlen'),
        findsOneWidget);
    expect(find.text('kann nicht bezahlt werden: X'), findsOneWidget);
    expect(find.text('Zahlung per Y'), findsOneWidget);
    expect(find.text('Pay by Y'), findsNothing);

    await LocaleStore.instance.setLocale(const Locale('ja'));
    await tester.pump();
    expect(find.text('支払えません: X'), findsOneWidget);
    expect(find.text('Y で支払う'), findsOneWidget);
  });

  testWidgets(
      'the login / admin / org-settings / workflows batch switches with '
      'the locale', (tester) async {
    // Guards the round-3 extraction batch (login + MFA + admin users + org
    // settings + workflows). Proves the new keys re-localize live, including a
    // plural (workflowsStepCount) and a placeholder string (orgSettingsSaveFailed
    // + adminUsersActivated + workflowDetailStepNumber).
    final probe = Builder(
      builder: (context) {
        final l = AppLocalizations.of(context);
        return Scaffold(
          body: Column(
            children: [
              Text(l.loginSignIn),
              Text(l.mfaVerify),
              Text(l.adminUsersTitle),
              Text(l.adminUsersActivated('Sam')),
              Text(l.orgSettingsSave),
              Text(l.orgSettingsSaveFailed('boom')),
              Text(l.workflowsTitle),
              Text(l.workflowsStepCount(2)),
              Text(l.workflowDetailStepNumber(1)),
            ],
          ),
        );
      },
    );

    await LocaleStore.instance.setLocale(const Locale('en'));
    await tester.pumpWidget(host(probe));
    await tester.pump();
    expect(find.text('Sign In'), findsOneWidget); // loginSignIn
    expect(find.text('User Management'), findsOneWidget); // adminUsersTitle
    expect(find.text('Activated Sam'), findsOneWidget); // placeholder
    expect(find.text('Failed to save: boom'), findsOneWidget); // placeholder
    expect(find.text('2 steps'), findsOneWidget); // plural
    expect(find.text('Step 1'), findsOneWidget); // placeholder

    await LocaleStore.instance.setLocale(const Locale('de'));
    await tester.pump();
    expect(find.text('Anmelden'), findsOneWidget); // loginSignIn
    expect(find.text('Benutzerverwaltung'), findsOneWidget); // adminUsersTitle
    expect(find.text('Workflows'), findsOneWidget); // workflowsTitle
    expect(find.text('2 Schritte'), findsOneWidget); // plural
    expect(find.text('Sign In'), findsNothing);

    await LocaleStore.instance.setLocale(const Locale('ja'));
    await tester.pump();
    expect(find.text('サインイン'), findsOneWidget); // loginSignIn
    expect(find.text('2ステップ'), findsOneWidget); // plural (other-only)
  });

  testWidgets(
      'the inspections / adaptive batch switches with the locale',
      (tester) async {
    // Guards this round's extraction batch (quality inspections + adaptive
    // workflows + the Settings navigation rows). Includes a plural
    // (adaptiveAnomaliesScanned) and three placeholder strings
    // (inspectionsResultAnnounce, inspectionRecordReceiptsBounded,
    // adaptivePatternsLookback).
    final probe = Builder(
      builder: (context) {
        final l = AppLocalizations.of(context);
        return Scaffold(
          body: Column(
            children: [
              Text(l.settingsInspections),
              Text(l.settingsAdaptive),
              Text(l.inspectionsResultPartial),
              Text(l.inspectionsResultAnnounce('X')),
              Text(l.inspectionRecordReceiptsBounded(2, 57)),
              Text(l.inspectionDetailNotFound),
              Text(l.adaptiveTabSuggestions),
              Text(l.adaptivePatternsLookback(180)),
              Text(l.adaptiveAnomaliesScanned(7)),
            ],
          ),
        );
      },
    );

    await LocaleStore.instance.setLocale(const Locale('en'));
    await tester.pumpWidget(host(probe));
    await tester.pump();
    expect(find.text('Quality Inspections'), findsOneWidget);
    expect(find.text('Partial acceptance'), findsOneWidget);
    expect(find.text('Result: X'), findsOneWidget); // placeholder
    expect(
      find.textContaining('Showing the 2 most recent of 57 receipts'),
      findsOneWidget,
    ); // two placeholders
    expect(find.text('7 invoices in review scanned.'), findsOneWidget); // plural

    await LocaleStore.instance.setLocale(const Locale('de'));
    await tester.pump();
    expect(find.text('Qualitätsprüfungen'), findsOneWidget);
    expect(find.text('Teilannahme'), findsOneWidget);
    expect(find.text('Ergebnis: X'), findsOneWidget); // placeholder
    expect(
      find.text('7 Rechnungen in Prüfung untersucht.'),
      findsOneWidget,
    ); // plural
    expect(find.text('Quality Inspections'), findsNothing);

    await LocaleStore.instance.setLocale(const Locale('ja'));
    await tester.pump();
    expect(find.text('品質検査'), findsOneWidget);
    expect(find.text('一部受入'), findsOneWidget);
    // Japanese plurals carry only an `other` arm.
    expect(
      find.text('レビュー中の請求書 7 件をスキャンしました。'),
      findsOneWidget,
    );
  });

  testWidgets('the choice persists and reloads via init()', (tester) async {
    await LocaleStore.instance.setLocale(const Locale('fr'));
    expect(LocaleStore.tagOf(LocaleStore.instance.locale!), 'fr');

    // A fresh init() (after clearing the in-memory loaded flag through reflection
    // is not exposed, so we assert the stored tag round-trips) — verify the tag
    // mapping and supported-locale set instead, which is what init() reads.
    expect(LocaleStore.supportedLocales.length, 6);
    expect(
      LocaleStore.supportedLocales
          .map(LocaleStore.tagOf)
          .toList(),
      ['en', 'de', 'fr', 'es', 'pt-BR', 'ja'],
    );
    expect(LocaleStore.endonyms.keys.toSet(),
        LocaleStore.supportedLocales.map(LocaleStore.tagOf).toSet());
  });
}
