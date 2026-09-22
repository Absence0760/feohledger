// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for German (`de`).
class AppLocalizationsDe extends AppLocalizations {
  AppLocalizationsDe([String locale = 'de']) : super(locale);

  @override
  String get navDashboard => 'Übersicht';

  @override
  String get navInvoices => 'Rechnungen';

  @override
  String get navContracts => 'Verträge';

  @override
  String get navApprovals => 'Freigaben';

  @override
  String get navExceptions => 'Ausnahmen';

  @override
  String get navVendors => 'Lieferanten';

  @override
  String get navPay => 'Bezahlen';

  @override
  String get navPayments => 'Zahlungen';

  @override
  String get navSettings => 'Einstellungen';

  @override
  String get shellAppName => 'FeohLedger';

  @override
  String get commonSave => 'Speichern';

  @override
  String get commonSaving => 'Wird gespeichert …';

  @override
  String get commonCancel => 'Abbrechen';

  @override
  String get commonLoading => 'Wird geladen …';

  @override
  String get commonRetry => 'Erneut versuchen';

  @override
  String get commonAll => 'Alle';

  @override
  String get commonSearch => 'Suchen';

  @override
  String get commonClear => 'Löschen';

  @override
  String get commonApply => 'Anwenden';

  @override
  String get commonClose => 'Schließen';

  @override
  String get settingsTitle => 'Einstellungen';

  @override
  String get settingsTenant => 'Mandant';

  @override
  String get settingsTenantNotSet => 'Nicht festgelegt';

  @override
  String get settingsApiServer => 'API-Server';

  @override
  String get settingsBiometricUnlock => 'Biometrisches Entsperren';

  @override
  String get settingsBiometricHint =>
      'Fingerabdruck oder Gesicht zum Entsperren verwenden';

  @override
  String get settingsSignOut => 'Abmelden';

  @override
  String get settingsLanguage => 'Sprache';

  @override
  String get settingsLanguageHint =>
      'Wählen Sie die in der App verwendete Sprache. Ihre Auswahl wird auf diesem Gerät gespeichert.';

  @override
  String get settingsLanguageSystem => 'Systemstandard';

  @override
  String get dashboardTitle => 'Übersicht';

  @override
  String get dashboardTotalInvoices => 'Rechnungen gesamt';

  @override
  String get dashboardUpcoming => 'Anstehend';

  @override
  String get dashboardForReview => 'Zur Prüfung';

  @override
  String get dashboardApproved => 'Freigegeben';

  @override
  String get dashboardAging => 'Rechnungsalter';

  @override
  String get dashboardTopVendors => 'Top-Lieferanten';

  @override
  String get dashboardAgingCurrent => 'Aktuell';

  @override
  String get dashboardAgingDays30 => '30 Tage';

  @override
  String get dashboardAgingDays60 => '60 Tage';

  @override
  String get dashboardAgingDays90plus => 'über 90';

  @override
  String get dashboardCachedBanner =>
      'Zwischengespeicherte Daten — Server nicht erreichbar';

  @override
  String dashboardErrorPrefix(String error) {
    return 'Fehler: $error';
  }

  @override
  String dashboardInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen',
      one: '$count Rechnung',
    );
    return '$_temp0';
  }

  @override
  String get partialConversionCurrencyFallback => 'die Berichtswährung';

  @override
  String dashboardKpiUnconverted(String label, int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen',
      one: '$count Rechnung',
    );
    return '$label: teilweise – $_temp0 ohne Wechselkurs in $currency, zum Nennwert erfasst; in diesem Umfang mischt die Summe Währungen.';
  }

  @override
  String dashboardAgingUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen',
      one: '$count Rechnung',
    );
    return 'Teilweise: $_temp0 ohne Wechselkurs in $currency, zum Nennwert erfasst – in diesem Umfang mischen die Bänder unten Währungen.';
  }

  @override
  String dashboardTopVendorsUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen',
      one: '$count Rechnung',
    );
    return 'Teilweise: $_temp0 ohne Wechselkurs in $currency, zum Nennwert erfasst – diese Lieferanten sind daher nicht alle in derselben Währung gereiht. Erfassen Sie den fehlenden Kurs, bevor Sie sich auf die Reihenfolge stützen.';
  }

  @override
  String dashboardVendorFaceValueCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count zum Nennwert',
      one: '$count zum Nennwert',
    );
    return '$_temp0';
  }

  @override
  String get invoicesTitle => 'Rechnungen';

  @override
  String get invoicesSearchHint => 'Rechnungen suchen …';

  @override
  String get invoicesSearchAria => 'Rechnungen suchen';

  @override
  String get invoicesAdvancedSearch => 'Erweiterte Suche';

  @override
  String get invoicesAdvancedSearchActive => 'Erweiterte Suche, Filter aktiv';

  @override
  String get invoicesCaptureInvoice => 'Rechnung erfassen';

  @override
  String get invoicesCaptureInvoiceLabel => 'Rechnung erfassen';

  @override
  String get invoicesEmpty => 'Keine Rechnungen gefunden';

  @override
  String get invoicesFilterAll => 'Alle';

  @override
  String get invoicesFilterNew => 'Neu';

  @override
  String get invoicesFilterPending => 'Ausstehend';

  @override
  String get invoicesFilterReview => 'Prüfung';

  @override
  String get invoicesFilterApproved => 'Freigegeben';

  @override
  String get invoicesFilterRejected => 'Abgelehnt';

  @override
  String get invoicesFilterPaid => 'Bezahlt';

  @override
  String get invoicesColInvoiceNumber => 'Rechnungs-Nr.';

  @override
  String get invoicesColVendor => 'Lieferant';

  @override
  String get invoicesColAmount => 'Betrag';

  @override
  String get invoicesColDueDate => 'Fälligkeitsdatum';

  @override
  String get invoicesColStatus => 'Status';

  @override
  String get notificationsTitle => 'Benachrichtigungen';

  @override
  String get notificationsMarkAllRead => 'Alle als gelesen markieren';

  @override
  String get notificationsMarkAllReadLabel =>
      'Alle Benachrichtigungen als gelesen markieren';

  @override
  String get notificationsFilterUnread => 'Ungelesen';

  @override
  String get notificationsAllMarkedRead =>
      'Alle Benachrichtigungen als gelesen markiert';

  @override
  String get notificationsCouldNotMarkAll =>
      'Konnte nicht alle als gelesen markieren';

  @override
  String get notificationsEmptyUnread => 'Keine ungelesenen Benachrichtigungen';

  @override
  String get notificationsEmpty => 'Keine Benachrichtigungen';

  @override
  String get notificationsCaughtUp => 'Sie sind auf dem neuesten Stand';

  @override
  String get notificationsNothingYet => 'Hier ist noch nichts';

  @override
  String get notificationsLoadError =>
      'Benachrichtigungen konnten nicht geladen werden';

  @override
  String get vendorsTitle => 'Lieferanten';

  @override
  String get vendorsSyncErp => 'Aus ERP synchronisieren';

  @override
  String get vendorsSyncErpLabel => 'Lieferanten aus ERP synchronisieren';

  @override
  String get vendorsSearchHint => 'Lieferanten suchen …';

  @override
  String get vendorsFilterUnverified => 'Ungeprüft';

  @override
  String get vendorsFilterActive => 'Aktiv';

  @override
  String get vendorsFilterInactive => 'Inaktiv';

  @override
  String get vendorsFilterRejected => 'Abgelehnt';

  @override
  String get vendorsEmpty => 'Keine Lieferanten gefunden';

  @override
  String get vendorsLoadError => 'Lieferanten konnten nicht geladen werden';

  @override
  String get vendorActionVerify => 'Bestätigen';

  @override
  String get vendorActionReject => 'Ablehnen';

  @override
  String get vendorUnverifiedLabel => 'Ungeprüfter Lieferant';

  @override
  String get vendorVerifyHint => 'Für Zahlung freigeben';

  @override
  String get vendorRejectHint => 'Als ungültig / Dublette markieren';

  @override
  String get vendorVerified => 'Lieferant bestätigt';

  @override
  String get vendorRejected => 'Lieferant abgelehnt';

  @override
  String get vendorActionFailed => 'Aktion fehlgeschlagen';

  @override
  String vendorSyncFailed(String error) {
    return 'ERP-Synchronisierung fehlgeschlagen: $error';
  }

  @override
  String get exceptionsTitle => 'Ausnahmen';

  @override
  String get exceptionsFilterOpen => 'Offen';

  @override
  String get exceptionsFilterEscalated => 'Eskaliert';

  @override
  String get exceptionsFilterResolved => 'Gelöst';

  @override
  String get exceptionsFilterDismissed => 'Verworfen';

  @override
  String get exceptionsEmpty => 'Keine Ausnahmen';

  @override
  String get exceptionsQueueClear => 'Die Ausnahmewarteschlange ist leer';

  @override
  String get exceptionActionResolve => 'Lösen';

  @override
  String get exceptionActionEscalate => 'Eskalieren';

  @override
  String get exceptionActionDismiss => 'Verwerfen';

  @override
  String get exceptionResolved => 'Ausnahme gelöst';

  @override
  String get exceptionEscalated => 'Ausnahme eskaliert';

  @override
  String get exceptionDismissed => 'Ausnahme verworfen';

  @override
  String get exceptionActionFailed => 'Aktion fehlgeschlagen';

  @override
  String get paymentsTitle => 'Zahlungen';

  @override
  String get paymentsEmpty => 'Keine Zahlungen';

  @override
  String paymentsErrorPrefix(String error) {
    return 'Fehler: $error';
  }

  @override
  String get paymentStatusPending => 'Ausstehend';

  @override
  String get paymentStatusProcessing => 'In Bearbeitung';

  @override
  String get paymentStatusCompleted => 'Abgeschlossen';

  @override
  String get paymentStatusFailed => 'Fehlgeschlagen';

  @override
  String get paymentStatusCancelled => 'Storniert';

  @override
  String get approvalsTitle => 'Ausstehende Freigaben';

  @override
  String get approvalsAllCaughtUp => 'Alles erledigt!';

  @override
  String get approvalsNoneWaiting => 'Keine Rechnungen warten auf Freigabe';

  @override
  String get approvalsLoadError =>
      'Ausstehende Freigaben konnten nicht geladen werden';

  @override
  String approvalsPendingCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen ausstehend',
      one: '$count Rechnung ausstehend',
    );
    return '$_temp0';
  }

  @override
  String get approvalActionApprove => 'Freigeben';

  @override
  String get approvalActionReject => 'Ablehnen';

  @override
  String get approvalApproved => 'Rechnung freigegeben';

  @override
  String get captureTitle => 'Rechnung erfassen';

  @override
  String get captureChange => 'Ändern';

  @override
  String get captureUpload => 'Hochladen';

  @override
  String get captureUploading => 'Wird hochgeladen …';

  @override
  String get captureEmptyPrompt =>
      'Foto aufnehmen, aus der Galerie wählen oder eine Datei auswählen';

  @override
  String get captureCamera => 'Kamera';

  @override
  String get captureGallery => 'Galerie';

  @override
  String get captureChooseFile => 'Datei auswählen';

  @override
  String get captureSupportedFormats => 'Unterstützt PDF, PNG, JPG und TIFF';

  @override
  String get captureUploadSuccess => 'Rechnung erfolgreich hochgeladen';

  @override
  String captureUploadFailedStatus(int status, String message) {
    return 'Hochladen fehlgeschlagen ($status): $message';
  }

  @override
  String captureUploadFailed(String error) {
    return 'Hochladen fehlgeschlagen: $error';
  }

  @override
  String captureSelectedDocument(String name) {
    return 'Ausgewähltes Dokument: $name';
  }

  @override
  String get capturePdfReady => 'PDF-Dokument bereit zum Hochladen';

  @override
  String get advSearchTitle => 'Erweiterte Suche';

  @override
  String get advSearchClose => 'Erweiterte Suche schließen';

  @override
  String get advSearchVendor => 'Lieferant';

  @override
  String get advSearchPoNumber => 'Bestellnummer';

  @override
  String get advSearchMinAmount => 'Mindestbetrag';

  @override
  String get advSearchMaxAmount => 'Höchstbetrag';

  @override
  String get advSearchDueFrom => 'Fällig ab';

  @override
  String get advSearchDueTo => 'Fällig bis';

  @override
  String get advSearchAny => 'Beliebig';

  @override
  String get advSearchInvalidAmount => 'Gültigen Betrag eingeben (z. B. 1000)';

  @override
  String get advSearchMinMaxError =>
      'Minimum darf das Maximum nicht überschreiten';

  @override
  String advSearchClearField(String label) {
    return '$label löschen';
  }

  @override
  String advSearchDateFieldHint(String label, String value) {
    return '$label, aktuell $value. Zum Ändern doppeltippen.';
  }

  @override
  String get invoiceDetailTitle => 'Rechnungsdetails';

  @override
  String get invoiceDetailEdit => 'Bearbeiten';

  @override
  String get invoiceDetailEditLabel => 'Rechnung bearbeiten';

  @override
  String get invoiceDetailRetry => 'Erneut versuchen';

  @override
  String invoiceDetailErrorPrefix(String error) {
    return 'Fehler: $error';
  }

  @override
  String get invoiceDetailNoChanges => 'Keine Änderungen zu speichern';

  @override
  String get invoiceDetailUpdated => 'Rechnung aktualisiert';

  @override
  String get invoiceDetailUpdateFailed =>
      'Änderungen konnten nicht gespeichert werden — bitte erneut versuchen';

  @override
  String get invoiceDetailApproved => 'Rechnung freigegeben';

  @override
  String get invoiceDetailApproveFailed =>
      'Rechnung konnte nicht freigegeben werden — bitte erneut versuchen';

  @override
  String get invoiceDetailRejected => 'Rechnung abgelehnt';

  @override
  String get invoiceDetailRejectFailed =>
      'Rechnung konnte nicht abgelehnt werden — bitte erneut versuchen';

  @override
  String get invoiceDetailRejectTitle => 'Rechnung ablehnen';

  @override
  String get invoiceDetailRejectReason => 'Grund';

  @override
  String get invoiceDetailReject => 'Ablehnen';

  @override
  String get invoiceDetailApprove => 'Freigeben';

  @override
  String get invoiceDetailUnknownVendor => 'Unbekannter Lieferant';

  @override
  String get invoiceDetailFieldInvoiceNumber => 'Rechnungs-Nr.';

  @override
  String get invoiceDetailFieldPoNumber => 'Bestellnummer';

  @override
  String get invoiceDetailFieldCurrency => 'Währung';

  @override
  String get invoiceDetailFieldInvoiceDate => 'Rechnungsdatum';

  @override
  String get invoiceDetailFieldDueDate => 'Fälligkeitsdatum';

  @override
  String get invoiceDetailFieldDescription => 'Beschreibung';

  @override
  String get invoiceDetailFieldGlAccount => 'Sachkonto';

  @override
  String get invoiceDetailFieldCreated => 'Erstellt';

  @override
  String get invoiceDetailActivity => 'Aktivität';

  @override
  String get invoiceDetailActivityError =>
      'Aktivität konnte nicht geladen werden';

  @override
  String get invoiceDetailFilePdfLabel =>
      'Rechnungs-PDF. Zum Anzeigen im Vollbild doppeltippen.';

  @override
  String get invoiceDetailFileLabel =>
      'Rechnungsdatei. Zum Anzeigen im Vollbild doppeltippen.';

  @override
  String get invoiceDetailTapToViewPdf => 'Zum Anzeigen des PDFs tippen';

  @override
  String get invoiceDetailTapToViewFile => 'Zum Anzeigen der Datei tippen';

  @override
  String get invoiceEditTitle => 'Rechnung bearbeiten';

  @override
  String get invoiceEditClose => 'Bearbeitungsformular schließen';

  @override
  String get invoiceEditVendor => 'Lieferant';

  @override
  String get invoiceEditInvoiceNumber => 'Rechnungs-Nr.';

  @override
  String get invoiceEditAmount => 'Betrag';

  @override
  String get invoiceEditPoNumber => 'Bestellnummer';

  @override
  String get invoiceEditGlAccount => 'Sachkonto';

  @override
  String get invoiceEditDescription => 'Beschreibung';

  @override
  String get invoiceEditDueDate => 'Fälligkeitsdatum';

  @override
  String get invoiceEditNotSet => 'Nicht festgelegt';

  @override
  String get invoiceEditInvalidAmount =>
      'Gültigen Betrag eingeben (z. B. 1234,56)';

  @override
  String get invoiceEditClearDueDate => 'Fälligkeitsdatum löschen';

  @override
  String get invoiceEditLockedNotice =>
      'Genehmigt — Zahlungsempfänger und Betrag sind gesperrt. Zum Ändern die Rechnung ablehnen, korrigieren und erneut genehmigen.';

  @override
  String get invoiceEditLockedHelper => 'Nach der Genehmigung gesperrt';

  @override
  String invoiceEditDueDateHint(String value) {
    return 'Fälligkeitsdatum, aktuell $value. Zum Ändern doppeltippen.';
  }

  @override
  String get warningsSectionTitle => 'Warnungen & Betrugshinweise';

  @override
  String get warningsPoMatchTitle => 'Bestellabgleich';

  @override
  String get warningsSeverityError => 'Fehler';

  @override
  String get warningsSeverityWarning => 'Warnung';

  @override
  String get warningsSeverityInfo => 'Info';

  @override
  String get warningsPoLabel => 'Bestellung';

  @override
  String warningsMatchLabel(String type) {
    return '$type-Abgleich';
  }

  @override
  String warningsVarianceLabel(String value) {
    return '$value % Abweichung';
  }

  @override
  String get erpStatusTitle => 'ERP-Status';

  @override
  String get erpStatusReference => 'ERP-Referenz';

  @override
  String get erpStatusDocumentId => 'Dokument-ID';

  @override
  String get erpStatusError => 'Fehler';

  @override
  String get erpStatusLastUpdate => 'Letzte Aktualisierung';

  @override
  String get erpStatusStatus => 'Status';

  @override
  String get fileViewerPdfTitle => 'Rechnungs-PDF';

  @override
  String get fileViewerImageTitle => 'Rechnungsbild';

  @override
  String get fileViewerPdfError => 'PDF konnte nicht geladen werden';

  @override
  String get fileViewerImageError => 'Bild konnte nicht geladen werden';

  @override
  String get fileViewerRetry => 'Erneut versuchen';

  @override
  String get timelineNoActivity => 'Noch keine Aktivität';

  @override
  String get payTitle => 'Bezahlen';

  @override
  String get payTabQueue => 'Warteschlange';

  @override
  String get payTabRuns => 'Läufe';

  @override
  String get paySummaryTotalPaid => 'Gesamt bezahlt';

  @override
  String get paySummaryPending => 'Ausstehend';

  @override
  String get paySummaryInQueue => 'In Warteschlange';

  @override
  String get paySummaryCardRebates => 'Karten-Rückvergütungen';

  @override
  String paySummaryPaymentsSubtitle(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Zahlungen',
      one: '$count Zahlung',
    );
    return '$_temp0';
  }

  @override
  String get payQueueEmpty => 'Keine Rechnungen zur Zahlung';

  @override
  String get payQueueError =>
      'Zahlungswarteschlange konnte nicht geladen werden';

  @override
  String get payQueueRetry => 'Erneut versuchen';

  @override
  String payQueueDue(String date) {
    return 'Fällig $date';
  }

  @override
  String get payQueueNoDueDate => 'Kein Fälligkeitsdatum';

  @override
  String payQueueDiscount(String amount) {
    return 'Rabatt $amount';
  }

  @override
  String get payQueueOverdue => 'überfällig';

  @override
  String get payQueueSelected => 'ausgewählt';

  @override
  String get payQueueBlockedDuplicate => 'Mögliches Duplikat — ungeklärt';

  @override
  String get payQueueBlockedFraudFlag => 'Betrugsverdacht — ungeklärt';

  @override
  String get payQueueBlockedLineTotalMismatch =>
      'Positionssummen stimmen nicht überein — ungeklärt';

  @override
  String get payQueueBlockedPaymentReconciliation =>
      'Frühere Zahlung nicht abgestimmt — möglicherweise noch unterwegs';

  @override
  String get payQueueBlockedFullyCredited =>
      'Vollständig durch Gutschriften gedeckt — nichts zu zahlen';

  @override
  String get payQueueBlockedLiveVirtualCard =>
      'Eine aktive virtuelle Karte beansprucht diese Rechnung — per Karte bezahlen';

  @override
  String get payQueueBlockedGeneric =>
      'Ungeklärte Ausnahme blockiert die Zahlung';

  @override
  String payQueueBlockedAnnounce(String reason) {
    return 'kann nicht bezahlt werden: $reason';
  }

  @override
  String payQueuePinnedMethod(String method) {
    return 'Zahlung per $method';
  }

  @override
  String payMethodLabel(String invoiceNumber) {
    return 'Zahlungsmethode für $invoiceNumber';
  }

  @override
  String get payMethodAch => 'ACH';

  @override
  String get payMethodWire => 'Überweisung';

  @override
  String get payMethodCheck => 'Scheck';

  @override
  String get payMethodVirtualCard => 'Virtuelle Karte';

  @override
  String paySelectedCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen ausgewählt',
      one: '$count Rechnung ausgewählt',
    );
    return '$_temp0';
  }

  @override
  String get payClear => 'Löschen';

  @override
  String get payCreateRun => 'Lauf erstellen';

  @override
  String payCreateRunFailed(String error) {
    return 'Lauf konnte nicht erstellt werden: $error';
  }

  @override
  String get payRunsEmpty => 'Keine Zahlungsläufe';

  @override
  String payRunSubtitle(int count, String date) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Zahlungen',
      one: '$count Zahlung',
    );
    return '$_temp0 • $date';
  }

  @override
  String get payRunCfoRequiredSuffix => ' • CFO-Freigabe erforderlich';

  @override
  String payRunAnnounce(String amount, String status, String subtitle) {
    return 'Lauf $amount, $status, $subtitle';
  }

  @override
  String get payRunActions => 'Lauf-Aktionen';

  @override
  String get payRunActionExecute => 'Ausführen';

  @override
  String get payRunActionCancel => 'Stornieren';

  @override
  String get payRunActionApprove => 'Als CFO freigeben';

  @override
  String get payRunApproveTitle => 'Zahlungslauf freigeben?';

  @override
  String payRunApproveBody(String date, int count, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Zahlungen',
      one: '$count Zahlung',
    );
    return 'Freigabe des am $date erstellten Laufs — $_temp0 über insgesamt $amount. Dies genehmigt die Ausführung; es wird kein Geld bewegt.';
  }

  @override
  String get payRunApproveConfirm => 'Freigeben';

  @override
  String payRunApproveFailed(String error) {
    return 'Freigabe fehlgeschlagen: $error';
  }

  @override
  String get payRunCfoBlocked =>
      'Dieser Lauf benötigt eine CFO-Freigabe, bevor er ausgeführt werden kann.';

  @override
  String get payRunExecuteTitle => 'Zahlungslauf ausführen?';

  @override
  String payRunExecuteBody(String amount) {
    return 'Dies sendet $amount über den konfigurierten Zahlungsdienstleister.';
  }

  @override
  String payRunExecuteFailed(String error) {
    return 'Ausführen fehlgeschlagen: $error';
  }

  @override
  String payRunCancelFailed(String error) {
    return 'Stornieren fehlgeschlagen: $error';
  }

  @override
  String get payRunStatusDraft => 'Entwurf';

  @override
  String get payRunStatusCompleted => 'Abgeschlossen';

  @override
  String get payRunStatusSubmitted => 'Eingereicht';

  @override
  String get payRunStatusPartial => 'Teilweise';

  @override
  String get payRunStatusFailed => 'Fehlgeschlagen';

  @override
  String get payRunStatusCancelled => 'Storniert';

  @override
  String get payConfirmCancel => 'Abbrechen';

  @override
  String get payConfirmExecute => 'Ausführen';

  @override
  String get loginAppName => 'FeohLedger';

  @override
  String get loginTagline => 'Kreditorenbuchhaltung, ganz einfach';

  @override
  String get loginTenant => 'Mandant';

  @override
  String get loginEmail => 'E-Mail';

  @override
  String get loginPassword => 'Passwort';

  @override
  String get loginShowPassword => 'Passwort anzeigen';

  @override
  String get loginHidePassword => 'Passwort verbergen';

  @override
  String get loginRequired => 'Erforderlich';

  @override
  String get loginSignIn => 'Anmelden';

  @override
  String get mfaTitle => 'Zwei-Faktor-Authentifizierung';

  @override
  String get mfaHeading => 'Bestätigen Sie Ihre Identität';

  @override
  String get mfaPromptEmail =>
      'Geben Sie den 6-stelligen Code ein, den wir Ihnen per E-Mail gesendet haben.';

  @override
  String get mfaPromptTotp =>
      'Geben Sie den 6-stelligen Code aus Ihrer Authenticator-App ein.';

  @override
  String get mfaEnforcedNotice =>
      'Ihre Organisation erfordert eine Zwei-Faktor-Authentifizierung. Bestätigen Sie jetzt mit einem E-Mail-Code und richten Sie anschließend in der Web-App eine Authenticator-App ein.';

  @override
  String get mfaCode => 'Code';

  @override
  String get mfaCodeRequired => 'Erforderlich';

  @override
  String get mfaCodeTooShort => 'Mindestens 6 Ziffern eingeben';

  @override
  String get mfaVerify => 'Bestätigen';

  @override
  String get mfaSending => 'Wird gesendet…';

  @override
  String get mfaResendEmailCode => 'E-Mail-Code erneut senden';

  @override
  String get mfaSendEmailCode => 'E-Mail-Code senden';

  @override
  String get mfaUseEmailInstead => 'Stattdessen einen E-Mail-Code verwenden';

  @override
  String get mfaUseAuthenticatorInstead =>
      'Stattdessen die Authenticator-App verwenden';

  @override
  String get mfaEmailedAnnounce =>
      'Ein Anmeldecode wurde Ihnen per E-Mail gesendet.';

  @override
  String get adminUsersTitle => 'Benutzerverwaltung';

  @override
  String get adminUsersSearchHint => 'Nach Name oder E-Mail suchen';

  @override
  String get adminUsersEmpty => 'Keine Benutzer gefunden';

  @override
  String get adminUsersLoadError => 'Benutzer konnten nicht geladen werden';

  @override
  String get adminUsersEditRoles => 'Rollen bearbeiten';

  @override
  String get adminUsersNoRoles => 'Keine Rollen';

  @override
  String get adminUsersDeactivate => 'Benutzer deaktivieren';

  @override
  String get adminUsersActivate => 'Benutzer aktivieren';

  @override
  String get adminUsersCannotDeactivateSelf =>
      'Sie können Ihr eigenes Konto nicht deaktivieren';

  @override
  String get adminUsersDeactivateHint =>
      'Meldet sie ab und blockiert die Anmeldung';

  @override
  String get adminUsersActivateHint => 'Stellt den Anmeldezugang wieder her';

  @override
  String get adminUsersRoleActive => 'aktiv';

  @override
  String get adminUsersRoleInactive => 'inaktiv';

  @override
  String get adminUsersInactiveBadge => 'Inaktiv';

  @override
  String adminUsersRolesUpdated(String name) {
    return 'Rollen für $name aktualisiert';
  }

  @override
  String adminUsersRolesUpdateFailed(String error) {
    return 'Rollen konnten nicht aktualisiert werden: $error';
  }

  @override
  String adminUsersActivated(String name) {
    return '$name aktiviert';
  }

  @override
  String adminUsersDeactivated(String name) {
    return '$name deaktiviert';
  }

  @override
  String adminUsersUpdateFailed(String name, String error) {
    return '$name konnte nicht aktualisiert werden: $error';
  }

  @override
  String get adminUsersCreateUser => 'Benutzer erstellen';

  @override
  String get adminUsersCreateTitle => 'Neuer Benutzer';

  @override
  String get adminUsersFieldFullName => 'Vollständiger Name';

  @override
  String get adminUsersFieldEmail => 'E-Mail';

  @override
  String get adminUsersFieldRoles => 'Rollen';

  @override
  String get adminUsersValidationNameRequired =>
      'Vollständiger Name ist erforderlich';

  @override
  String get adminUsersValidationEmailRequired => 'E-Mail ist erforderlich';

  @override
  String get adminUsersValidationEmailInvalid =>
      'Geben Sie eine gültige E-Mail-Adresse ein';

  @override
  String get adminUsersCreateSubmit => 'Erstellen';

  @override
  String get adminUsersCreating => 'Wird erstellt…';

  @override
  String adminUsersCreated(String name) {
    return '$name erstellt';
  }

  @override
  String adminUsersCreateFailed(String error) {
    return 'Benutzer konnte nicht erstellt werden: $error';
  }

  @override
  String get adminUsersTempPasswordTitle => 'Benutzer erstellt';

  @override
  String adminUsersTempPasswordBody(String name) {
    return 'Teilen Sie dieses Einmalpasswort mit $name. Bei der ersten Anmeldung wird es geändert. Es wird nicht erneut angezeigt.';
  }

  @override
  String get adminUsersDelete => 'Benutzer löschen';

  @override
  String get adminUsersDeleteHint => 'Entfernt dieses Konto dauerhaft';

  @override
  String get adminUsersCannotDeleteSelf =>
      'Sie können Ihr eigenes Konto nicht löschen';

  @override
  String adminUsersDeleteConfirmTitle(String name) {
    return '$name löschen?';
  }

  @override
  String adminUsersDeleteConfirmBody(String name, String email) {
    return 'Dadurch wird $name ($email) dauerhaft entfernt. Dies kann nicht rückgängig gemacht werden.';
  }

  @override
  String adminUsersDeleted(String name) {
    return '$name gelöscht';
  }

  @override
  String adminUsersDeleteFailed(String name, String error) {
    return '$name konnte nicht gelöscht werden: $error';
  }

  @override
  String get orgSettingsTitle => 'Organisationseinstellungen';

  @override
  String get orgSettingsNoSettings => 'Keine Einstellungen';

  @override
  String get orgSettingsLoadError =>
      'Einstellungen konnten nicht geladen werden';

  @override
  String get orgSettingsSectionCompany => 'Unternehmen';

  @override
  String get orgSettingsSectionInvoiceDefaults => 'Rechnungsstandards';

  @override
  String get orgSettingsName => 'Name der Organisation';

  @override
  String get orgSettingsAddress => 'Adresse';

  @override
  String get orgSettingsPhone => 'Telefon';

  @override
  String get orgSettingsWebsite => 'Website';

  @override
  String get orgSettingsTaxId => 'Steuernummer';

  @override
  String get orgSettingsCurrency => 'Standardwährung';

  @override
  String get orgSettingsPaymentTerms => 'Zahlungsbedingungen';

  @override
  String get orgSettingsNumberPrefix => 'Rechnungsnummer-Präfix';

  @override
  String get orgSettingsGlAccount => 'Standard-Sachkonto';

  @override
  String get orgSettingsCostCenter => 'Standard-Kostenstelle';

  @override
  String get orgSettingsSave => 'Änderungen speichern';

  @override
  String get orgSettingsSaving => 'Wird gespeichert…';

  @override
  String orgSettingsFieldRequired(String label) {
    return '$label ist erforderlich';
  }

  @override
  String get orgSettingsSaved => 'Organisationseinstellungen gespeichert';

  @override
  String orgSettingsSaveFailed(String error) {
    return 'Speichern fehlgeschlagen: $error';
  }

  @override
  String get workflowsTitle => 'Workflows';

  @override
  String get workflowsEmpty => 'Keine Workflows gefunden';

  @override
  String get workflowsLoadError => 'Workflows konnten nicht geladen werden';

  @override
  String get workflowsStatusActive => 'Aktiv';

  @override
  String get workflowsStatusInactive => 'Inaktiv';

  @override
  String get workflowsDefault => 'Standard';

  @override
  String workflowsStepCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Schritte',
      one: '$count Schritt',
    );
    return '$_temp0';
  }

  @override
  String get workflowDetailFallbackTitle => 'Workflow';

  @override
  String get workflowDetailLoadError => 'Workflow konnte nicht geladen werden';

  @override
  String get workflowDetailNoSteps => 'Dieser Workflow hat keine Schritte.';

  @override
  String get workflowDetailDefaultWorkflow => 'Standard-Workflow';

  @override
  String workflowDetailStepNumber(int number) {
    return 'Schritt $number';
  }

  @override
  String get workflowDetailStepEnabled => 'Aktiviert';

  @override
  String get workflowDetailStepDisabled => 'Deaktiviert';

  @override
  String workflowDetailApproverCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Genehmiger',
      one: '$count Genehmiger',
    );
    return '$_temp0';
  }

  @override
  String workflowDetailDelaySummary(String hours) {
    return 'Verzögerung $hours Std.';
  }

  @override
  String workflowDetailConditionSummary(String field) {
    return 'Bei $field';
  }

  @override
  String get cashFlowTitle => 'Cashflow-Prognose';

  @override
  String cashFlowErrorPrefix(String error) {
    return 'Fehler: $error';
  }

  @override
  String cashFlowHorizonDays(int days) {
    return '$days Tage';
  }

  @override
  String get cashFlowLowBalanceAlert => 'Warnung: niedriger Kontostand';

  @override
  String cashFlowBreachSingle(
    String threshold,
    String period,
    String shortfall,
  ) {
    return 'Voraussichtlich unter dem Mindestsaldo von $threshold in $period (Unterdeckung $shortfall).';
  }

  @override
  String cashFlowBreachMultiple(int count, String period, String shortfall) {
    return '$count Zeiträume fallen voraussichtlich unter den Mindestsaldo. Schlimmster: $period, Unterdeckung $shortfall.';
  }

  @override
  String get cashFlowMinimum => 'Mindest';

  @override
  String get cashFlowOpeningBalance => 'Anfangssaldo';

  @override
  String get cashFlowProjectedEnd => 'Prognostiziertes Ende';

  @override
  String cashFlowProjectedEndSubtitle(int days) {
    return 'in $days Tagen';
  }

  @override
  String get cashFlowCommittedOut => 'Fest zugesagt';

  @override
  String get cashFlowCommittedSubtitle => 'feste Zusagen';

  @override
  String get cashFlowPendingOut => 'Ausstehend';

  @override
  String get cashFlowPendingSubtitle => 'laufende Pipeline';

  @override
  String get cashFlowOpeningSourceProvider => 'von Bank synchronisiert';

  @override
  String get cashFlowOpeningSourceSettings => 'gespeicherter Saldo';

  @override
  String get cashFlowOpeningSourceQuery => 'manuell';

  @override
  String get cashFlowOpeningSourceUnset => 'Saldo festlegen';

  @override
  String get cashFlowProjectedOutflows => 'Prognostizierte Abflüsse';

  @override
  String get cashFlowNoOutflows =>
      'Keine prognostizierten Abflüsse in diesem Zeitraum.';

  @override
  String cashFlowInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen',
      one: '$count Rechnung',
    );
    return '$_temp0';
  }

  @override
  String cashFlowCommittedAmount(String amount) {
    return 'zugesagt $amount';
  }

  @override
  String cashFlowPendingAmount(String amount) {
    return 'ausstehend $amount';
  }

  @override
  String get cashFlowPosition => 'Liquiditätsstatus';

  @override
  String get cashFlowNoPosition =>
      'Keine Liquiditätsprognose für diesen Zeitraum.';

  @override
  String cashFlowOutAmount(String amount) {
    return 'Abfluss $amount';
  }

  @override
  String cashFlowForecastRowLabel(
    String period,
    String scheduled,
    String committed,
    String pending,
    int count,
  ) {
    return '$period: geplant $scheduled, zugesagt $committed, ausstehend $pending, $count Rechnungen';
  }

  @override
  String cashFlowPositionRowLabel(
    String period,
    String opening,
    String outflow,
    String closing,
  ) {
    return '$period: Anfang $opening, Abfluss $outflow, Ende $closing';
  }

  @override
  String get cashFlowBelowThresholdSuffix => ', unter dem Schwellenwert';

  @override
  String cashFlowLowBalanceAlertLabel(String message) {
    return 'Warnung: niedriger Kontostand. $message';
  }

  @override
  String cashFlowForecastUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen',
      one: '$count Rechnung',
    );
    return 'Teilweise: $_temp0 ohne Wechselkurs in $currency, zum Nennwert erfasst – in diesem Umfang mischen diese Abflüsse und die fest zugesagten und ausstehenden Summen oben Währungen.';
  }

  @override
  String cashFlowPositionUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen',
      one: '$count Rechnung',
    );
    return 'Nicht umgerechnet: $_temp0 ohne Wechselkurs in $currency, zum Nennwert gezählt — der laufende Saldo unten und das prognostizierte Ende oben vermischen dadurch Währungen. Erfassen Sie den fehlenden Kurs, bevor Sie auf Basis dieser Zahlen handeln.';
  }

  @override
  String get contractsTitle => 'Verträge';

  @override
  String get contractsSearchHint => 'Verträge suchen …';

  @override
  String get contractsEmpty => 'Keine Verträge gefunden';

  @override
  String get contractsFilterDraft => 'Entwurf';

  @override
  String get contractsFilterActive => 'Aktiv';

  @override
  String get contractsFilterExpired => 'Abgelaufen';

  @override
  String get contractsFilterTerminated => 'Gekündigt';

  @override
  String get contractsFilterCancelled => 'Storniert';

  @override
  String get contractDetailTitle => 'Vertragsdetails';

  @override
  String contractDetailErrorPrefix(String error) {
    return 'Fehler: $error';
  }

  @override
  String get contractDetailUntitled => 'Unbenannter Vertrag';

  @override
  String get contractDetailFieldContractNumber => 'Vertragsnr.';

  @override
  String get contractDetailFieldVendor => 'Lieferant';

  @override
  String get contractDetailFieldType => 'Typ';

  @override
  String get contractDetailFieldCurrency => 'Währung';

  @override
  String get contractDetailFieldSpendLimit => 'Ausgabenlimit';

  @override
  String get contractDetailNotToExceed => ' (nicht zu überschreiten)';

  @override
  String get contractDetailFieldStartDate => 'Startdatum';

  @override
  String get contractDetailFieldEndDate => 'Enddatum';

  @override
  String get contractDetailFieldSigned => 'Unterzeichnet';

  @override
  String get contractDetailFieldAutoRenew => 'Automatische Verlängerung';

  @override
  String get contractDetailYes => 'Ja';

  @override
  String get contractDetailNo => 'Nein';

  @override
  String get contractDetailFieldRenewalTerm => 'Verlängerungslaufzeit';

  @override
  String contractDetailRenewalTermMonths(int months) {
    return '$months Monate';
  }

  @override
  String get contractDetailFieldRenewalNotice => 'Kündigungsfrist';

  @override
  String contractDetailRenewalNoticeDays(int days) {
    return '$days Tage';
  }

  @override
  String get contractDetailFieldPaymentTerms => 'Zahlungsbedingungen';

  @override
  String get contractDetailFieldDescription => 'Beschreibung';

  @override
  String get contractDetailFieldCreated => 'Erstellt';

  @override
  String get contractDetailSectionSpend => 'Ausgaben';

  @override
  String get contractDetailSectionLineItems => 'Positionen';

  @override
  String get contractDetailSpendInvoiced => 'In Rechnung gestellt';

  @override
  String contractDetailSpendInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen',
      one: '$count Rechnung',
    );
    return '$_temp0';
  }

  @override
  String get contractDetailSpendOverLimit => 'Über Limit';

  @override
  String get contractDetailSpendRemaining => 'Verbleibend';

  @override
  String contractDetailSpendOfLimit(String limit) {
    return 'von $limit';
  }

  @override
  String get contractDetailSpendNoLimit => 'kein Limit festgelegt';

  @override
  String get contractDetailLineItemFallback => 'Position';

  @override
  String contractDetailLineQty(String quantity) {
    return 'Menge $quantity';
  }

  @override
  String contractDetailLineUnitPrice(String price) {
    return '@ $price';
  }

  @override
  String contractDetailLineGl(String account) {
    return 'Sachkonto $account';
  }

  @override
  String get contractActivate => 'Aktivieren';

  @override
  String get contractActivated => 'Vertrag aktiviert';

  @override
  String get contractActivateFailed =>
      'Vertrag konnte nicht aktiviert werden – bitte erneut versuchen';

  @override
  String get contractTerminate => 'Kündigen';

  @override
  String get contractTerminateTitle => 'Vertrag kündigen';

  @override
  String get contractTerminateBody =>
      'Damit wird der Vertrag vorzeitig beendet. Dies kann nicht rückgängig gemacht werden. Fortfahren?';

  @override
  String get contractTerminated => 'Vertrag gekündigt';

  @override
  String get contractTerminateFailed =>
      'Vertrag konnte nicht gekündigt werden – bitte erneut versuchen';

  @override
  String get exceptionDetailTitle => 'Ausnahme';

  @override
  String get exceptionDetailNotFound => 'Ausnahme nicht gefunden';

  @override
  String get exceptionDetailOverdue => 'Überfällig';

  @override
  String get exceptionDetailSectionDescription => 'Beschreibung';

  @override
  String get exceptionDetailSectionInvoice => 'Rechnung';

  @override
  String get exceptionDetailNoLinkedInvoice => 'Keine verknüpfte Rechnung';

  @override
  String get exceptionDetailFieldNumber => 'Nummer';

  @override
  String get exceptionDetailFieldVendor => 'Lieferant';

  @override
  String get exceptionDetailFieldAmount => 'Betrag';

  @override
  String get exceptionDetailFieldSeverity => 'Schweregrad';

  @override
  String get exceptionDetailSectionSla => 'SLA';

  @override
  String get exceptionDetailFieldCreated => 'Erstellt';

  @override
  String get exceptionDetailFieldDue => 'Fällig';

  @override
  String get exceptionDetailNoSla => 'Kein SLA festgelegt';

  @override
  String get exceptionDetailFieldStatus => 'Status';

  @override
  String get exceptionDetailOnTrack => 'Im Plan';

  @override
  String get exceptionDetailResolvedIn => 'Gelöst in';

  @override
  String exceptionDetailResolvedInHours(String hours) {
    return '$hours Std.';
  }

  @override
  String get exceptionDetailSectionAssignee => 'Zuständig';

  @override
  String get exceptionDetailUnassigned => 'Nicht zugewiesen';

  @override
  String get exceptionDetailAssign => 'Zuweisen';

  @override
  String get exceptionDetailReassign => 'Neu zuweisen';

  @override
  String get exceptionDetailSectionResolution => 'Lösung';

  @override
  String get exceptionDetailResolutionNote => 'Notiz';

  @override
  String get exceptionDetailResolutionBy => 'Von';

  @override
  String get exceptionDetailResolutionAt => 'Am';

  @override
  String get exceptionDetailActionResolved => 'Ausnahme gelöst';

  @override
  String get exceptionDetailActionEscalated => 'Ausnahme eskaliert';

  @override
  String get exceptionDetailActionDismissed => 'Ausnahme verworfen';

  @override
  String get exceptionDetailActionResolveFailed =>
      'Ausnahme konnte nicht gelöst werden';

  @override
  String get exceptionDetailActionEscalateFailed =>
      'Ausnahme konnte nicht eskaliert werden';

  @override
  String get exceptionDetailActionDismissFailed =>
      'Ausnahme konnte nicht verworfen werden';

  @override
  String get exceptionDetailAssignTo => 'Zuweisen an';

  @override
  String get exceptionDetailUnassign => 'Zuweisung aufheben';

  @override
  String exceptionDetailLoadUsersFailed(String error) {
    return 'Benutzer konnten nicht geladen werden: $error';
  }

  @override
  String get exceptionDetailAssigneeUpdateFailed =>
      'Zuständigkeit konnte nicht aktualisiert werden';

  @override
  String get exceptionDetailUnassigned2 => 'Zuweisung der Ausnahme aufgehoben';

  @override
  String exceptionDetailAssignedTo(String name) {
    return 'Zugewiesen an $name';
  }

  @override
  String get settingsProcurement => 'Beschaffung';

  @override
  String get settingsInspections => 'Qualitätsprüfungen';

  @override
  String get settingsInspectionsHint =>
      'Prüfungen für den 4-Wege-Abgleich erfassen und ansehen';

  @override
  String get settingsAdministration => 'Verwaltung';

  @override
  String get settingsAdminUsers => 'Benutzerverwaltung';

  @override
  String get settingsAdminUsersHint =>
      'Rollen, Benutzer aktivieren / deaktivieren';

  @override
  String get settingsAdminOrg => 'Organisationseinstellungen';

  @override
  String get settingsAdminOrgHint => 'Firmenprofil, Rechnungsvorgaben';

  @override
  String get settingsAdminWorkflows => 'Workflows';

  @override
  String get settingsAdminWorkflowsHint =>
      'Workflow-Definitionen und Schritte ansehen';

  @override
  String get settingsAdaptive => 'Adaptive Workflows';

  @override
  String get settingsAdaptiveHint =>
      'Freigabemuster, Auffälligkeiten, Vorschläge';

  @override
  String get inspectionsTitle => 'Qualitätsprüfungen';

  @override
  String get inspectionsRecord => 'Prüfung erfassen';

  @override
  String get inspectionsEmpty => 'Keine Qualitätsprüfungen erfasst.';

  @override
  String get inspectionsEmptyFiltered => 'Keine Prüfungen mit diesem Ergebnis.';

  @override
  String get inspectionsLoadError => 'Prüfungen konnten nicht geladen werden';

  @override
  String get inspectionsResultPass => 'Bestanden';

  @override
  String get inspectionsResultFail => 'Nicht bestanden';

  @override
  String get inspectionsResultPartial => 'Teilannahme';

  @override
  String get inspectionsResultUnknown => 'Unbekanntes Ergebnis';

  @override
  String inspectionsResultAnnounce(String result) {
    return 'Ergebnis: $result';
  }

  @override
  String get inspectionsNotLinked => 'Nicht verknüpft';

  @override
  String get inspectionsNotLinkedHint =>
      'Diese Prüfung ist weder einem Wareneingang noch einer Bestellung zugeordnet, der Bestellabgleich liest sie daher nie.';

  @override
  String get inspectionsReceiptUnnamed => 'Wareneingang ohne Nummer';

  @override
  String get inspectionsHintPass =>
      'Ware angenommen — der Abgleich bleibt unverändert.';

  @override
  String get inspectionsHintFail =>
      'Ware abgelehnt — die Rechnung fällt auf „Abweichung“ und eine Qualitätssperre blockiert die Zahlung.';

  @override
  String get inspectionsHintPartial =>
      'Ware teilweise angenommen — der Abgleich fällt auf „teilweise“ und die angenommene Menge wird markiert.';

  @override
  String get inspectionsHintUnknown =>
      'Dieses Ergebnis liegt außerhalb von Bestanden / Nicht bestanden / Teilannahme; seine Auswirkung auf den Abgleich lässt sich nicht angeben.';

  @override
  String inspectionsRecorded(String number) {
    return 'Prüfung $number erfasst';
  }

  @override
  String inspectionsRecordFailed(String error) {
    return 'Prüfung konnte nicht erfasst werden: $error';
  }

  @override
  String inspectionsReceiptsLoadFailed(String error) {
    return 'Wareneingänge konnten nicht geladen werden: $error';
  }

  @override
  String get inspectionRecordTitle => 'Qualitätsprüfung erfassen';

  @override
  String get inspectionRecordClose => 'Prüfformular schließen';

  @override
  String get inspectionRecordReceipt => 'Wareneingang';

  @override
  String get inspectionRecordReceiptHint =>
      'Der Bestellabgleich liest eine Prüfung nur über ihren Wareneingang, daher muss eine Prüfung die Lieferung benennen, die sie abdeckt.';

  @override
  String inspectionRecordReceiptsBounded(int shown, int total) {
    return 'Es werden die $shown neuesten von $total Wareneingängen angezeigt. Erfassen Sie die Prüfung in der Web-App, wenn die gesuchte Lieferung nicht aufgeführt ist.';
  }

  @override
  String get inspectionRecordNoReceipts =>
      'Noch keine Wareneingänge — eine Prüfung bezieht sich auf eine Lieferung, es gibt also nichts zu erfassen.';

  @override
  String get inspectionRecordNumber => 'Prüfnr.';

  @override
  String get inspectionRecordNumberRequired => 'Prüfnummer eingeben';

  @override
  String get inspectionRecordResult => 'Ergebnis';

  @override
  String get inspectionRecordAcceptedQuantity => 'Angenommene Menge';

  @override
  String get inspectionRecordRejectedQuantity => 'Abgelehnte Menge';

  @override
  String get inspectionRecordAcceptedRequired =>
      'Bei einer Teilannahme erforderlich';

  @override
  String get inspectionRecordInvalidQuantity =>
      'Maximal 8 Ziffern und 4 Dezimalstellen';

  @override
  String get inspectionRecordInspectedDate => 'Prüfdatum';

  @override
  String get inspectionRecordDateNotSet => 'Nicht festgelegt';

  @override
  String get inspectionRecordClearDate => 'Prüfdatum löschen';

  @override
  String get inspectionRecordInspector => 'Prüfer';

  @override
  String get inspectionRecordNotes => 'Abweichungsnotizen';

  @override
  String get inspectionRecordNotesHint =>
      'Werden bei einer nicht bestandenen Prüfung wörtlich in den Abgleichshinweis der Rechnung übernommen und von der Person gelesen, die die Qualitätssperre bearbeitet.';

  @override
  String get inspectionRecordSubmit => 'Prüfung erfassen';

  @override
  String get inspectionDetailTitle => 'Prüfung';

  @override
  String get inspectionDetailNotFound => 'Prüfung nicht gefunden';

  @override
  String inspectionDetailErrorPrefix(String error) {
    return 'Prüfung konnte nicht geladen werden: $error';
  }

  @override
  String get inspectionDetailFieldReceipt => 'Wareneingang';

  @override
  String get inspectionDetailFieldInspectedDate => 'Geprüft';

  @override
  String get inspectionDetailFieldInspector => 'Prüfer';

  @override
  String get inspectionDetailFieldAccepted => 'Angenommene Menge';

  @override
  String get inspectionDetailFieldRejected => 'Abgelehnte Menge';

  @override
  String get inspectionDetailFieldStatus => 'Status';

  @override
  String get inspectionDetailFieldCreated => 'Erstellt';

  @override
  String get inspectionDetailSectionNotes => 'Abweichungsnotizen';

  @override
  String get adaptiveTitle => 'Adaptive Workflows';

  @override
  String get adaptiveTabSuggestions => 'Vorschläge';

  @override
  String get adaptiveTabPatterns => 'Freigabemuster';

  @override
  String get adaptiveTabAnomalies => 'Auffälligkeiten';

  @override
  String get adaptiveAdvisoryNote =>
      'Alles hier ist eine Empfehlung. Auf diesem Bildschirm wurde kein Workflow geändert — eine Empfehlung wird erst wirksam, wenn jemand mit der passenden Rolle sie in der Web-App anwendet, über denselben protokollierten Weg wie eine manuelle Änderung.';

  @override
  String get adaptiveSuggestionsEmpty =>
      'Keine Vorschläge — noch zu wenig konsistente Freigabehistorie.';

  @override
  String get adaptiveSuggestionsError =>
      'Die Vorschläge konnten nicht geladen werden.';

  @override
  String get adaptiveSuggestionsShowOpen => 'Offen';

  @override
  String get adaptiveSuggestionsShowAll => 'Alle';

  @override
  String adaptiveSuggestionsConfidence(String pct) {
    return 'Konfidenz $pct %';
  }

  @override
  String get adaptiveSuggestionsDismiss => 'Verwerfen';

  @override
  String get adaptiveSuggestionsDismissTitle => 'Diesen Vorschlag verwerfen?';

  @override
  String get adaptiveSuggestionsDismissBody =>
      'Er bleibt auch nach einer Neuberechnung verworfen, und es ändert sich ohnehin kein Workflow.';

  @override
  String get adaptiveSuggestionsDismissed => 'Vorschlag verworfen.';

  @override
  String adaptiveSuggestionsDismissFailed(String error) {
    return 'Der Vorschlag konnte nicht verworfen werden: $error';
  }

  @override
  String get adaptiveSuggestionStatusOpen => 'Offen';

  @override
  String get adaptiveSuggestionStatusDismissed => 'Verworfen';

  @override
  String get adaptiveSuggestionStatusApplied => 'Angewendet';

  @override
  String get adaptiveSuggestionStatusStale => 'Veraltet';

  @override
  String get adaptiveSuggestionStatusUnknown => 'Unbekannter Status';

  @override
  String adaptiveSuggestionStatusAnnounce(String status) {
    return 'Status: $status';
  }

  @override
  String get adaptivePatternsError =>
      'Die Freigabemuster konnten nicht geladen werden.';

  @override
  String get adaptivePatternsEmptyApprovers =>
      'In diesem Zeitraum noch keine Freigabeentscheidungen.';

  @override
  String get adaptivePatternsEmptyVendors =>
      'In diesem Zeitraum noch keine Lieferantenhistorie.';

  @override
  String get adaptivePatternsSectionApprovers => 'Nach Freigebendem';

  @override
  String get adaptivePatternsSectionVendors => 'Nach Lieferant';

  @override
  String adaptivePatternsLookback(int days) {
    return 'Deterministische Statistiken über die Freigabehistorie dieses Mandanten der letzten $days Tage — kein Modell, bei jedem Aufruf neu berechnet.';
  }

  @override
  String get adaptivePatternsCurrencyNote =>
      'Beträge sind in der Berichtswährung Ihrer Organisation angegeben.';

  @override
  String get adaptivePatternsUnknownApprover => 'Unbekannte freigebende Person';

  @override
  String adaptivePatternsApproverSummary(int approved, int rejected) {
    return '$approved freigegeben · $rejected abgelehnt';
  }

  @override
  String adaptivePatternsApproverTiming(String rate, String days) {
    return '$rate % Freigabequote · Median $days Tage';
  }

  @override
  String adaptivePatternsVendorConsistency(String pct) {
    return '$pct % ohne Korrektur freigegeben';
  }

  @override
  String adaptivePatternsVendorMoney(String median, String avg) {
    return 'Median $median · Durchschnitt $avg';
  }

  @override
  String adaptivePatternsUnconverted(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other:
          '$count Genehmigungen konnten nicht in der Berichtswährung ausgedrückt werden und sind von den Beträgen oben ausgeschlossen — die Stichprobe zählt sie weiterhin',
      one:
          '$count Genehmigung konnte nicht in der Berichtswährung ausgedrückt werden und ist von den Beträgen oben ausgeschlossen — die Stichprobe zählt sie weiterhin',
    );
    return '$_temp0';
  }

  @override
  String get adaptiveAnomaliesIntro =>
      'Rechnungen in Prüfung, die außerhalb des etablierten Musters ihres Lieferanten liegen — beim Betrag, bei der freigebenden Person oder bei der Wartezeit. Nur lesend: eine Markierung hier löst nichts aus und blockiert nichts.';

  @override
  String get adaptiveAnomaliesEmpty =>
      'Nichts in Prüfung liegt außerhalb des üblichen Musters seines Lieferanten.';

  @override
  String get adaptiveAnomaliesError =>
      'Der Auffälligkeitsscan konnte nicht geladen werden.';

  @override
  String adaptiveAnomaliesScanned(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen in Prüfung untersucht.',
      one: '$count Rechnung in Prüfung untersucht.',
    );
    return '$_temp0';
  }

  @override
  String adaptiveAnomaliesAmount(String amount, String currency) {
    return '$amount $currency';
  }

  @override
  String get adaptiveAnomaliesInsufficient =>
      'Für diesen Lieferanten liegt noch zu wenig Historie vor.';

  @override
  String get invoiceWarningMissingVendorName => 'Lieferantenname fehlt';

  @override
  String get invoiceWarningMissingInvoiceNumber => 'Rechnungsnummer fehlt';

  @override
  String get invoiceWarningMissingAmount => 'Betrag fehlt oder ist null';

  @override
  String get invoiceWarningDuplicateInvoiceNumber =>
      'Doppelte Rechnungsnummer für diesen Lieferanten';

  @override
  String invoiceWarningDuplicateSimilar(
    String similarity,
    String invoiceNumber,
    String vendorName,
    int crossEntityCount,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      crossEntityCount,
      locale: localeName,
      other:
          ' (zusätzlich $crossEntityCount nahezu identische Rechnungen in einer anderen Einheit)',
      one:
          ' (zusätzlich $crossEntityCount nahezu identische Rechnung in einer anderen Einheit)',
      zero: '',
    );
    return 'Möglicher Doppeleintrag: $similarity Übereinstimmung mit $invoiceNumber von $vendorName$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarUnnamedVendor(
    String similarity,
    String invoiceNumber,
    int crossEntityCount,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      crossEntityCount,
      locale: localeName,
      other:
          ' (zusätzlich $crossEntityCount nahezu identische Rechnungen in einer anderen Einheit)',
      one:
          ' (zusätzlich $crossEntityCount nahezu identische Rechnung in einer anderen Einheit)',
      zero: '',
    );
    return 'Möglicher Doppeleintrag: $similarity Übereinstimmung mit $invoiceNumber$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarUnnumbered(
    String similarity,
    String vendorName,
    int crossEntityCount,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      crossEntityCount,
      locale: localeName,
      other:
          ' (zusätzlich $crossEntityCount nahezu identische Rechnungen in einer anderen Einheit)',
      one:
          ' (zusätzlich $crossEntityCount nahezu identische Rechnung in einer anderen Einheit)',
      zero: '',
    );
    return 'Möglicher Doppeleintrag: $similarity Übereinstimmung mit einer anderen Rechnung von $vendorName$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarUnnumberedUnnamedVendor(
    String similarity,
    int crossEntityCount,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      crossEntityCount,
      locale: localeName,
      other:
          ' (zusätzlich $crossEntityCount nahezu identische Rechnungen in einer anderen Einheit)',
      one:
          ' (zusätzlich $crossEntityCount nahezu identische Rechnung in einer anderen Einheit)',
      zero: '',
    );
    return 'Möglicher Doppeleintrag: $similarity Übereinstimmung mit einer anderen Rechnung$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarCrossEntity(String similarity) {
    return 'Möglicher Doppeleintrag: $similarity Übereinstimmung mit einer nahezu identischen Rechnung in einer anderen Einheit';
  }

  @override
  String invoiceWarningRoundAmount(String amount) {
    return 'Runder Betrag: $amount';
  }

  @override
  String get invoiceWarningFutureInvoiceDate =>
      'Das Rechnungsdatum liegt in der Zukunft';

  @override
  String invoiceWarningRushPayment(int days) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days Tagen',
      one: '$days Tag',
    );
    return 'Eilzahlung: fällig innerhalb von $_temp0 nach dem Rechnungsdatum';
  }

  @override
  String get invoiceWarningPastDue => 'Die Rechnung ist überfällig';

  @override
  String get invoiceWarningUnverifiedVendor =>
      'Der Lieferant ist nicht verifiziert';

  @override
  String invoiceWarningPersonalEmailDomain(String domain) {
    return 'Die E-Mail-Adresse des Lieferanten nutzt eine private Domain: $domain';
  }

  @override
  String invoiceWarningNewVendorLargeAmount(int days, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days Tagen',
      one: '$days Tag',
    );
    return 'Neuer Lieferant (vor $_temp0 angelegt) stellt eine hohe Rechnung über $amount';
  }

  @override
  String get invoiceWarningRemitToChanged =>
      'Die Zahlungsadresse hat sich seit der letzten genehmigten Rechnung dieses Lieferanten geändert';

  @override
  String invoiceWarningAmountAboveVendorMean(
    String amount,
    String sigma,
    String mean,
  ) {
    return 'Der Betrag $amount liegt $sigmaσ über dem historischen Mittelwert dieses Lieferanten ($mean)';
  }

  @override
  String invoiceWarningLlmAnomaly(String reason) {
    return 'Von der KI gemeldete Anomalie: $reason';
  }

  @override
  String invoiceWarningLineTotalMismatch(
    String lineItemsTotal,
    String headerAmount,
  ) {
    return 'Die Positionssumme beträgt $lineItemsTotal, der Rechnungsbetrag jedoch $headerAmount';
  }

  @override
  String invoiceWarningPriceVarianceOver(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Einzelpreis $deltaPct über der Referenz dieses Lieferanten für $item ($unitPrice statt $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPriceVarianceUnder(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Einzelpreis $deltaPct unter der Referenz dieses Lieferanten für $item ($unitPrice statt $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPoNotFound(String poNumber) {
    return 'Bestellung $poNumber nicht gefunden';
  }

  @override
  String invoiceWarningPoAmountVariance(
    String variancePct,
    String poNumber,
    String invoiceAmount,
    String poTotal,
  ) {
    return 'Betragsabweichung $variancePct gegenüber Bestellung $poNumber (Rechnung $invoiceAmount, Bestellung $poTotal)';
  }

  @override
  String invoiceWarningPoPartialReceipt(String matchType, String poNumber) {
    return 'Teilweiser 3-Wege-Abgleich — $matchType-Abgleich gegen Bestellung $poNumber, es wurde jedoch nur ein Teil der bestellten Menge geliefert';
  }

  @override
  String invoiceWarningPoOverReceipt(
    String receivedQuantity,
    String orderedQuantity,
    String excessQuantity,
    String poNumber,
  ) {
    return 'Überlieferung: $receivedQuantity geliefert gegenüber $orderedQuantity bestellt (+$excessQuantity) bei Bestellung $poNumber';
  }

  @override
  String invoiceWarningPoOverReceiptUnquantified(String poNumber) {
    return 'Es wurden mehr Waren geliefert als bei Bestellung $poNumber bestellt';
  }

  @override
  String invoiceWarningQualityInspectionFailed(String poNumber) {
    return 'Qualitätsprüfung für Bestellung $poNumber nicht bestanden';
  }

  @override
  String invoiceWarningQualityInspectionFailedNotes(
    String poNumber,
    String notes,
  ) {
    return 'Qualitätsprüfung für Bestellung $poNumber nicht bestanden: $notes';
  }

  @override
  String invoiceWarningQualityInspectionMissing(String poNumber) {
    return 'Qualitätsprüfung für Bestellung $poNumber erforderlich, aber nicht vorhanden';
  }

  @override
  String invoiceWarningQualityPartialAcceptance(
    String acceptedQuantity,
    String poNumber,
  ) {
    return 'Teilannahme: $acceptedQuantity der bestellten Menge bei Bestellung $poNumber angenommen';
  }

  @override
  String invoiceWarningQualityPartialAcceptanceUnquantified(String poNumber) {
    return 'Teilweise Qualitätsannahme bei der Prüfung für Bestellung $poNumber';
  }

  @override
  String invoiceWarningRecurringVarianceOver(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'Der Betrag $amount liegt $deltaPct über dem erwarteten Betrag $expectedAmount der Serienvorlage „$templateName“';
  }

  @override
  String invoiceWarningRecurringVarianceUnder(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'Der Betrag $amount liegt $deltaPct unter dem erwarteten Betrag $expectedAmount der Serienvorlage „$templateName“';
  }

  @override
  String invoiceWarningContractExpired(
    String invoiceDate,
    String contractNumber,
    String endDate,
  ) {
    return 'Die Rechnung vom $invoiceDate liegt nach dem Ablauf des Vertrags $contractNumber ($endDate)';
  }

  @override
  String invoiceWarningContractNotStarted(
    String invoiceDate,
    String contractNumber,
    String startDate,
  ) {
    return 'Die Rechnung vom $invoiceDate liegt vor dem Beginn des Vertrags $contractNumber ($startDate)';
  }

  @override
  String invoiceWarningContractTerminated(String contractNumber) {
    return 'Ausgabe für den beendeten Vertrag $contractNumber erfasst';
  }

  @override
  String invoiceWarningContractCancelled(String contractNumber) {
    return 'Ausgabe für den stornierten Vertrag $contractNumber erfasst';
  }

  @override
  String invoiceWarningContractVendorMismatch(String contractNumber) {
    return 'Der Lieferant der Rechnung stimmt nicht mit dem Lieferanten des Vertrags $contractNumber überein';
  }

  @override
  String invoiceWarningContractSpendLimitExceeded(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'Die kumulierte Ausgabe $cumulativeSpend übersteigt das Limit $spendLimit des Vertrags $contractNumber';
  }

  @override
  String invoiceWarningContractSpendLimitExceededNotToExceed(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'Die kumulierte Ausgabe $cumulativeSpend übersteigt das Limit $spendLimit des Vertrags $contractNumber (nicht überschreitbar)';
  }

  @override
  String invoiceWarningContractGlNotAllowed(
    String glAccount,
    String contractNumber,
  ) {
    return 'Das Sachkonto $glAccount liegt außerhalb der im Vertrag $contractNumber zugelassenen Konten';
  }

  @override
  String invoiceWarningSelfCorrectionTotalReconciliation(
    String subtotal,
    String tax,
    String shipping,
    String discount,
    String expected,
    String amount,
  ) {
    return 'Die Beträge stimmen nicht: Zwischensumme ($subtotal) + Steuer ($tax) + Versand ($shipping) − Rabatt ($discount) = $expected, die Gesamtsumme lautet jedoch $amount.';
  }

  @override
  String invoiceWarningSelfCorrectionDateOrdering(
    String dueDate,
    String invoiceDate,
  ) {
    return 'Das Fälligkeitsdatum ($dueDate) liegt vor dem Rechnungsdatum ($invoiceDate).';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemsSum(
    String lineItemsTotal,
    String amount,
  ) {
    return 'Die Positionssumme ($lineItemsTotal) stimmt nicht mit dem Rechnungsbetrag ($amount) überein.';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemMath(
    String lineNumber,
    String quantity,
    String unitPrice,
    String expected,
    String total,
  ) {
    return 'Position $lineNumber: $quantity × $unitPrice = $expected, die Summe lautet jedoch $total.';
  }

  @override
  String invoiceWarningGlCodesNotInChart(String codes) {
    return 'Von der KI vorgeschlagene Sachkonten fehlen im aktiven Kontenplan: $codes';
  }

  @override
  String invoiceWarningGlCodeStalePrior(String code) {
    return 'Das zwischengespeicherte Sachkonto „$code“ des Lieferanten ist nicht mehr im aktiven Kontenplan.';
  }
}
