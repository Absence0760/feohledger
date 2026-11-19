// ignore_for_file: deprecated_member_use

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
  String get dashboardOverdue => 'Überfällig';

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
  String get dashboardLoadError =>
      'Das Dashboard konnte nicht geladen werden. Prüfen Sie Ihre Verbindung und tippen Sie auf „Erneut versuchen“.';

  @override
  String get dashboardRefreshFailed =>
      'Aktualisierung fehlgeschlagen – dies sind die zuletzt geladenen Zahlen. Zum erneuten Versuch nach unten ziehen.';

  @override
  String get dashboardNeedsAttention => 'Ihre Aufmerksamkeit erforderlich';

  @override
  String get dashboardOverview => 'Übersicht';

  @override
  String get dashboardEmpty =>
      'Noch keine Rechnungen. Erfasste und hochgeladene Rechnungen erscheinen hier.';

  @override
  String get dashboardTopVendorsEmpty => 'Noch keine Lieferantenausgaben.';

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
  String vendorCardsNotClosed(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other:
          '$count virtuelle Karten konnten beim Kartenanbieter nicht storniert werden und sind weiterhin aktiv. Erneut versuchen in der Web-App.',
      one:
          '$count virtuelle Karte konnte beim Kartenanbieter nicht storniert werden und ist weiterhin aktiv. Erneut versuchen in der Web-App.',
    );
    return '$_temp0';
  }

  @override
  String vendorCardsRequireVoid(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other:
          '$count virtuelle Karten decken geplante Zahlungen und sind weiterhin aktiv – stornieren Sie diese Zahlungen, um die Karten zu sperren.',
      one:
          '$count virtuelle Karte deckt eine geplante Zahlung und ist weiterhin aktiv – stornieren Sie diese Zahlung, um die Karte zu sperren.',
    );
    return '$_temp0';
  }

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
  String invoiceDetailUpdateFailed(String error) {
    return 'Änderungen konnten nicht gespeichert werden: $error';
  }

  @override
  String glChartRefusalForeign(int count, String codes) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: 'Die Sachkonten',
      one: 'Das Sachkonto',
    );
    String _temp1 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: 'gehören zum',
      one: 'gehört zum',
    );
    return '$_temp0 $codes $_temp1 Kontenplan einer anderen Einheit, nicht zu dem dieser Rechnung.';
  }

  @override
  String glChartRefusalRetired(int count, String codes) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: 'Die Sachkonten',
      one: 'Das Sachkonto',
    );
    String _temp1 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: 'sind im',
      one: 'ist im',
    );
    return '$_temp0 $codes $_temp1 Kontenplan dieser Rechnung stillgelegt.';
  }

  @override
  String glChartRefusalUnknown(int count, String codes) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: 'Die Sachkonten',
      one: 'Das Sachkonto',
    );
    String _temp1 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: 'sind nicht',
      one: 'ist nicht',
    );
    return '$_temp0 $codes $_temp1 im Kontenplan dieser Rechnung enthalten.';
  }

  @override
  String get glChartRefusalPick =>
      'Wählen Sie ein Konto aus dem eigenen Kontenplan der Rechnung – den gemeinsamen Konten plus denen ihrer Einheit.';

  @override
  String get glChartRefusalPickActive =>
      'Wählen Sie ein aktives Konto aus dem eigenen Kontenplan der Rechnung – den gemeinsamen Konten plus denen ihrer Einheit.';

  @override
  String glChartRefusalOnLines(String reasons) {
    return 'Positionen: $reasons';
  }

  @override
  String get codedRefusalApprovalSegregation =>
      'Funktionstrennung: Wer an der Erstellung dieser Rechnung beteiligt war, darf sie nicht auch freigeben.';

  @override
  String get codedRefusalApprovalSegregationReceiver =>
      'Funktionstrennung: Wer einen Wareneingang zu dieser Rechnung erfasst hat, darf sie nicht auch freigeben.';

  @override
  String get codedRefusalApprovalSegregationInspector =>
      'Funktionstrennung: Wer eine Qualitätsprüfung der Ware zu dieser Rechnung erfasst hat, darf sie nicht auch freigeben.';

  @override
  String get codedRefusalInvoiceStaleApproval =>
      'Diese Rechnung wurde geändert, nachdem Sie sie geladen haben. Laden Sie sie neu und prüfen Sie die aktuelle Fassung, bevor Sie sie genehmigen.';

  @override
  String get codedRefusalApprovalLevelReuse =>
      'Sie haben bereits eine frühere Stufe dieser Freigabekette freigegeben; eine andere Person muss freigeben.';

  @override
  String get codedRefusalApprovalNotNamedApprover =>
      'Sie sind für diesen Schritt nicht als Freigebende(r) berechtigt.';

  @override
  String codedRefusalApprovalMaxExceeded(String amount, String limit) {
    return 'Der Rechnungsbetrag $amount übersteigt den zulässigen Höchstbetrag $limit.';
  }

  @override
  String get codedRefusalApprovalMaxMisconfigured =>
      'Der Freigabeschritt dieses Workflows hat einen unbrauchbaren maximalen Rechnungsbetrag. Die Freigabe ist gesperrt, bis ein Admin die Workflow-Definition korrigiert.';

  @override
  String codedRefusalApprovalCfoRequired(String amount, String limit) {
    return 'Der Rechnungsbetrag $amount übersteigt $limit. CFO-Freigabe erforderlich.';
  }

  @override
  String codedRefusalApprovalCfoRequiredUnknownLimit(String amount) {
    return 'Der Rechnungsbetrag $amount übersteigt das konfigurierte Limit. CFO-Freigabe erforderlich.';
  }

  @override
  String codedRefusalGateStructuring(
    String recent,
    int days,
    String aggregate,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: 'letzte $days Tage',
      one: 'letzter $days Tag',
    );
    return 'Diese Rechnung allein liegt unter dem Schwellenwert, zusammen mit $recent aus anderen aktuellen Rechnungen dieses Lieferanten ($_temp0) ergibt sie jedoch $aggregate.';
  }

  @override
  String codedRefusalGateInexpressible(String currency) {
    return 'Diese Rechnung konnte nicht in $currency ausgedrückt werden, der Währung des Limits, und kann daher nicht dagegen geprüft werden.';
  }

  @override
  String codedRefusalGateMeasured(String measured, String currency) {
    return 'Gemessen als $measured – das Limit ist in $currency festgelegt.';
  }

  @override
  String get codedRefusalExceptionSegregationRaiser =>
      'Funktionstrennung: Wer diese Ausnahme durch die eigene Aktion ausgelöst hat, darf sie nicht auch auflösen. Eskalieren Sie sie oder bitten Sie eine andere Person um die Entscheidung.';

  @override
  String get codedRefusalExceptionSegregationImplicated =>
      'Funktionstrennung: Wer an der Erstellung dieser Rechnung beteiligt war, darf keine Ausnahme auflösen, die ihre Zahlung blockiert. Eskalieren Sie sie oder bitten Sie eine andere Person um die Entscheidung.';

  @override
  String get codedRefusalExceptionSegregationApprover =>
      'Funktionstrennung: Wer diese Rechnung genehmigt hat, darf ihre Wareneingangs- oder Prüfsperre nicht auch auflösen. Eskalieren Sie sie oder bitten Sie eine andere Person um die Entscheidung.';

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
  String get invoiceDetailRecall => 'Genehmigung zurücknehmen';

  @override
  String get invoiceDetailRecallTitle => 'Diese Genehmigung zurücknehmen?';

  @override
  String get invoiceDetailRecallBody =>
      'Die Rechnung geht zurück in die Prüfung, damit ihre Beträge korrigiert werden können, und braucht danach eine neue Genehmigung über die gesamte Genehmigungskette. Die Funktionstrennung zählt Sie dann, wie den Einreicher, zu den an dieser Rechnung Beteiligten. Die ursprüngliche Genehmigung bleibt im Prüfprotokoll erhalten.';

  @override
  String get invoiceDetailRecallReason => 'Warum nehmen Sie sie zurück?';

  @override
  String get invoiceDetailRecalled =>
      'Genehmigung zurückgenommen. Die Rechnung ist wieder in der Prüfung.';

  @override
  String get invoiceDetailRecallFailed =>
      'Die Genehmigung konnte nicht zurückgenommen werden – bitte erneut versuchen';

  @override
  String get codedRefusalInvoiceNotRecallable =>
      'Nur bei einer genehmigten Rechnung oder einer, deren ERP-Übertragung nach der Genehmigung fehlgeschlagen ist, kann die Genehmigung zurückgenommen werden.';

  @override
  String get codedRefusalInvoiceRecallInErp =>
      'Im ERP liegt möglicherweise bereits eine Rechnung zu diesem Beleg vor, und sie kann dort bezahlt werden. Korrigieren oder stornieren Sie sie im ERP; die Genehmigung kann in FeohLedger nicht zurückgenommen werden.';

  @override
  String get codedRefusalPaymentBlocksRecall =>
      'FeohLedger bezahlt diese Rechnung bereits: Es gibt eine aktive Zahlung. Brechen Sie den Zahlungslauf ab oder stornieren Sie die Zahlung, bevor Sie die Genehmigung zurücknehmen.';

  @override
  String get codedRefusalCardBlocksRecall =>
      'Für die Zahlung dieser Rechnung wurde eine virtuelle Karte ausgestellt. Stornieren Sie die Karte, bevor Sie die Genehmigung zurücknehmen.';

  @override
  String invoicesBulkRejectTitle(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen',
      one: '$count Rechnung',
    );
    return '$_temp0 ablehnen';
  }

  @override
  String get commonDelete => 'Löschen';

  @override
  String bulkSelectedCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count ausgewählt',
      one: '$count ausgewählt',
    );
    return '$_temp0';
  }

  @override
  String get bulkCancelSelection => 'Auswahl abbrechen';

  @override
  String get bulkSelectAll => 'Alle auswählen';

  @override
  String get bulkActionExport => 'Exportieren';

  @override
  String get bulkActionStatus => 'Status';

  @override
  String bulkResultWithSkipped(String result, int skipped) {
    return '$result ($skipped übersprungen)';
  }

  @override
  String get bulkUnknownError => 'unbekannter Fehler';

  @override
  String get invoicesSelectMultiple => 'Mehrere auswählen';

  @override
  String get invoicesBulkDeleteTitle => 'Rechnungen löschen?';

  @override
  String invoicesBulkDeleteBody(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count ausgewählte Rechnungen endgültig löschen?',
      one: '$count ausgewählte Rechnung endgültig löschen?',
    );
    return '$_temp0 Bereits bezahlte oder an das ERP gesendete Rechnungen werden übersprungen.';
  }

  @override
  String invoicesBulkDeleted(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen gelöscht',
      one: '$count Rechnung gelöscht',
    );
    return '$_temp0';
  }

  @override
  String invoicesBulkDeleteFailed(String error) {
    return 'Massenlöschung fehlgeschlagen: $error';
  }

  @override
  String get invoicesBulkStatusSheetTitle => 'Status ändern in…';

  @override
  String invoicesBulkMoved(int count, String status) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen auf „$status“ gesetzt',
      one: '$count Rechnung auf „$status“ gesetzt',
    );
    return '$_temp0';
  }

  @override
  String invoicesBulkStatusFailed(String error) {
    return 'Statusänderung fehlgeschlagen: $error';
  }

  @override
  String get invoicesBulkExportSheetTitle => 'Exportieren als…';

  @override
  String get invoicesExportFormatCsv => 'CSV';

  @override
  String get invoicesExportFormatXml => 'XML';

  @override
  String invoicesBulkExportFailed(String error) {
    return 'Export fehlgeschlagen: $error';
  }

  @override
  String invoicesBulkShareFailed(String error) {
    return 'Teilen-Dialog konnte nicht geöffnet werden: $error';
  }

  @override
  String invoicesBulkExported(int count, String format) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen als $format exportiert',
      one: '$count Rechnung als $format exportiert',
    );
    return '$_temp0';
  }

  @override
  String get exceptionsSelectMultiple => 'Ausnahmen auswählen';

  @override
  String exceptionsBulkResolved(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Ausnahmen gelöst',
      one: '$count Ausnahme gelöst',
    );
    return '$_temp0';
  }

  @override
  String exceptionsBulkDismissed(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Ausnahmen verworfen',
      one: '$count Ausnahme verworfen',
    );
    return '$_temp0';
  }

  @override
  String get invoiceStatusNew => 'Neu';

  @override
  String get invoiceStatusPending => 'Ausstehend';

  @override
  String get invoiceStatusReadyForReview => 'Prüfbereit';

  @override
  String get invoiceStatusApproved => 'Freigegeben';

  @override
  String get invoiceStatusRejected => 'Abgelehnt';

  @override
  String get invoiceStatusSendingToErp => 'Wird an ERP gesendet';

  @override
  String get invoiceStatusSentToErp => 'An ERP gesendet';

  @override
  String get invoiceStatusPostedInErp => 'In ERP gebucht';

  @override
  String get invoiceStatusPaymentScheduled => 'Zahlung geplant';

  @override
  String get invoiceStatusPaid => 'Bezahlt';

  @override
  String get invoiceStatusDone => 'Abgeschlossen';

  @override
  String get invoiceStatusFailed => 'Fehlgeschlagen';

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
      'Genehmigt — Zahlungsempfänger und Betrag sind gesperrt.';

  @override
  String get invoiceEditLockedNoticeRecall =>
      'Genehmigt — Zahlungsempfänger und Betrag sind gesperrt. Zum Ändern die Genehmigung zurücknehmen, die Rechnung korrigieren und erneut genehmigen lassen.';

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
  String erpStatusActionBy(String action, String actor) {
    return '$action von $actor';
  }

  @override
  String get erpStatusStatus => 'Status';

  @override
  String get erpStatusOpensInErp => 'öffnet die Rechnung im ERP';

  @override
  String get externalLinkOpenFailed => 'Der Link konnte nicht geöffnet werden';

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
  String get payQueueBlockedQualityHold =>
      'Qualitätssperre — Ware abgelehnt oder Prüfung ausstehend';

  @override
  String get payQueueBlockedPoMismatch =>
      'Weicht von der Bestellung ab — ungeklärt';

  @override
  String get payQueueBlockedVendorNotActive =>
      'Lieferant nicht verifiziert oder nicht aktiv — zuerst den Lieferanten verifizieren';

  @override
  String get payQueueBlockedCurrencyUnknown =>
      'Für diese Rechnung ist keine Währung erfasst — zuerst die Währung festlegen';

  @override
  String get payQueueBlockedAppliedCreditMismatch =>
      'Eine verrechnete Gutschrift passt nicht mehr zu Lieferant oder Währung dieser Rechnung — Rechnung zuerst korrigieren';

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
  String adminUsersInvited(String name, String email) {
    return '$name eingeladen. Wir haben $email einen Link zum Festlegen des Passworts gesendet.';
  }

  @override
  String adminUsersCreateFailed(String error) {
    return 'Benutzer konnte nicht erstellt werden: $error';
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
  String cashFlowOverdueAmount(String amount) {
    return 'überfällig $amount';
  }

  @override
  String cashFlowOverdueSuffix(String amount) {
    return ', davon $amount bereits überfällig';
  }

  @override
  String cashFlowForecastOverdue(int count, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Rechnungen',
      one: '$count Rechnung',
    );
    return '$amount aus $_temp0 ist bereits überfällig. Jetzt geschuldet, daher in der ersten Periode berücksichtigt.';
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
  String get contractCancel => 'Vertrag stornieren';

  @override
  String get contractCancelTitle => 'Vertrag stornieren';

  @override
  String get contractCancelBody =>
      'Damit wird der Vertrag zurückgezogen – es können keine weiteren Rechnungen mehr zugeordnet werden. Dies kann nicht rückgängig gemacht werden. Fortfahren?';

  @override
  String get contractCancelled => 'Vertrag storniert';

  @override
  String get contractCancelFailed =>
      'Vertrag konnte nicht storniert werden – bitte erneut versuchen';

  @override
  String get exceptionDetailTitle => 'Ausnahme';

  @override
  String get exceptionDetailNotFound => 'Ausnahme nicht gefunden';

  @override
  String get exceptionDetailOverdue => 'Überfällig';

  @override
  String get exceptionDetailSectionDescription => 'Beschreibung';

  @override
  String exceptionDetailPoRematch(String po, String erp, String date) {
    return 'Bestellung $po wurde am $date in $erp geändert; eine erneute Prüfung stimmt jetzt überein. Prüfen Sie sie, um die Sperre aufzuheben.';
  }

  @override
  String exceptionDetailPoRematchNoDate(String po, String erp) {
    return 'Bestellung $po wurde in $erp geändert; eine erneute Prüfung stimmt jetzt überein. Prüfen Sie sie, um die Sperre aufzuheben.';
  }

  @override
  String exceptionDetailPoRematchNoPo(String erp) {
    return 'Die Bestellung wurde in $erp geändert; eine erneute Prüfung stimmt jetzt überein. Prüfen Sie sie, um die Sperre aufzuheben.';
  }

  @override
  String erpRefusalVendorNotLinked(String provider) {
    return 'Dieser Lieferant ist noch nicht in $provider. Synchronisieren Sie die Lieferanten aus $provider oder legen Sie den Lieferanten dort an und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalAccountNotLinked(String provider, String accounts) {
    return 'Nicht mit einem Konto in $provider verknüpft: $accounts. Synchronisieren Sie den Kontenplan aus $provider oder kontieren Sie die Rechnung auf ein verknüpftes Konto um und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalAccountNotLinkedUncoded(String provider) {
    return 'Eine Position hat kein Sachkonto, und die Rechnung hat keines, auf das zurückgegriffen werden kann. Kontieren Sie sie auf ein mit $provider verknüpftes Konto und versuchen Sie es dann erneut.';
  }

  @override
  String get erpRefusalNotConnected =>
      'Das ERP ist nicht verbunden oder die Verbindung ist abgelaufen. Ein Administrator kann es unter Organisation neu verbinden; versuchen Sie es dann erneut.';

  @override
  String get erpRefusalInvoiceDueBeforeDate =>
      'Diese Rechnung ist vor ihrem Rechnungsdatum fällig, was das ERP ablehnt. Korrigieren Sie die Daten und versuchen Sie es erneut.';

  @override
  String erpRefusalAmountMismatch(String provider) {
    return 'Die Positionsbeträge ergeben nicht den genehmigten Betrag, daher wurde die Rechnung nicht an $provider gesendet. Korrigieren Sie die Positionen oder die Steuer und versuchen Sie es dann erneut.';
  }

  @override
  String get erpRefusalLineAmountMissing =>
      'Eine Position hat keinen Betrag – weder eine Summe noch Menge und Einzelpreis, aus denen er sich berechnen ließe. Ergänzen Sie ihn und versuchen Sie es dann erneut.';

  @override
  String erpRefusalPostedTotalMismatch(String provider) {
    return '$provider hat einen anderen Gesamtbetrag als den genehmigten berechnet (oft wegen eines Standardsteuercodes), daher wurde die Rechnung nicht übernommen. Prüfen Sie die Steuereinstellungen des Lieferanten und der Konten in $provider und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalPostedTotalUnconfirmed(String provider) {
    return 'Die Rechnung wurde in $provider angelegt, aber $provider hat ihren Gesamtbetrag nicht gemeldet, daher kann sie nicht bestätigt werden. Prüfen Sie die Rechnung in $provider, bevor Sie es erneut versuchen.';
  }

  @override
  String erpRefusalPreviousBillRemoved(String provider) {
    return 'Jede für diese Rechnung gesendete Buchung wurde in $provider gelöscht oder storniert, daher wird sie nicht automatisch erneut gesendet. Prüfen Sie $provider und fragen Sie dann einen Administrator, wie es weitergeht.';
  }

  @override
  String erpRefusalTaxNotItemised(String provider) {
    return '$provider benötigt die Steuer je Position, diese Rechnung weist sie aber nur insgesamt aus. Erfassen Sie die Steuer je Position und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalTaxRateUnresolved(String provider) {
    return '$provider hat keinen Steuersatz, der zur Steuer dieser Rechnung passt. Legen Sie den Satz in $provider an oder korrigieren Sie die Steuer und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalMissingDates(String provider) {
    return '$provider benötigt ein Rechnungsdatum und ein Fälligkeitsdatum. Ergänzen Sie beide und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalBillRefused(String provider) {
    return '$provider hat diese Rechnung abgelehnt und nicht angelegt. Gleichen Sie Lieferant, Konten, Daten und Beträge der Rechnung mit $provider ab, korrigieren Sie sie, und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalDuplicateNumber(String provider) {
    return '$provider enthält bereits eine Rechnung mit dieser Rechnungsnummer. Prüfen Sie $provider auf ein Duplikat, bevor Sie es erneut versuchen.';
  }

  @override
  String erpRefusalDocNumberTooLong(String provider) {
    return 'Die Rechnungsnummer ist länger, als $provider zulässt. Kürzen Sie sie und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalCurrencyUnknown(String provider) {
    return '$provider meldet keine Hauswährung, daher kann die Währung der Rechnung nicht geprüft werden. Legen Sie die Hauswährung in $provider fest und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalCurrencyNotEnabled(String provider) {
    return 'Diese Rechnung ist nicht in der Hauswährung von $provider, und Fremdwährungen sind dort deaktiviert. Aktivieren Sie Fremdwährungen in $provider oder korrigieren Sie die Währung und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalIdempotencyLookupIncomplete(String provider) {
    return '$provider konnte nicht bestätigen, ob diese Rechnung bereits gesendet wurde, daher wurde sie nicht erneut gesendet. Versuchen Sie es in einigen Minuten erneut.';
  }

  @override
  String erpRefusalPurchasesNotPermitted(String provider) {
    return '$provider hat das Anlegen dieser Rechnung nicht erlaubt. Mit dem Tarif Accounting Start können keine Eingangsrechnungen angelegt werden, oder dem verbundenen Sage-Benutzer fehlt der Zugriff auf Einkäufe. Wechseln Sie den Tarif oder geben Sie diesem Benutzer in $provider Zugriff auf Einkäufe, und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalExistingInvoiceNotOpen(String provider) {
    return '$provider enthält diese Rechnung bereits, und sie ist kein Entwurf mehr, daher kann sie von hier aus nicht geändert werden. Prüfen Sie sie in $provider.';
  }

  @override
  String erpRefusalLineAmountPrecision(String provider) {
    return 'Ein Positionsbetrag hat mehr Nachkommastellen, als diese Währung in $provider zulässt. Runden Sie ihn und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalPayToVendorDiffers(String provider) {
    return 'In $provider ist für diese Rechnung ein anderer Zahlungsempfänger als der genehmigte Lieferant eingetragen (Zahlung an Kreditor), daher wurde sie nicht als diese Rechnung gebucht. Prüfen Sie den Zahlungsempfänger auf der Rechnung und auf der Kreditorenkarte in $provider und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalBillNotPosted(String provider) {
    return '$provider enthält diese Rechnung, zeigt sie aber noch nicht als gebucht an, meist weil sie dort auf Genehmigung wartet. Genehmigen Sie sie in $provider und versuchen Sie es dann erneut. Sie wird nicht doppelt angelegt.';
  }

  @override
  String erpRefusalBillRejected(String provider) {
    return '$provider enthält diese Rechnung, sie wurde dort aber abgelehnt oder storniert und wird daher nie gebucht. Löschen Sie sie in $provider oder erfassen Sie sie neu, bevor Sie es erneut versuchen, und schließen Sie diese Rechnung nicht ab: Niemand würde sie bezahlen.';
  }

  @override
  String erpRefusalCurrencyNotSetUp(String provider) {
    return '$provider hat keine aktive Währung mit dem Währungscode dieser Rechnung. Legen Sie die Währung in $provider an oder aktivieren Sie sie wieder (für eine Fremdwährung müssen mehrere Währungen aktiviert sein), oder korrigieren Sie die Währung der Rechnung, und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalCurrencyNotSupported(String provider) {
    return '$provider nimmt Rechnungen nur in der eigenen Währung des Unternehmens an. Korrigieren Sie die Währung oder erfassen Sie die Rechnung direkt in $provider.';
  }

  @override
  String erpRefusalForeignCurrencySupplier(String provider) {
    return 'Dieser Lieferant ist in $provider für eine Fremdwährung eingerichtet, auf die von hier aus nicht gebucht werden kann. Erfassen Sie die Rechnung direkt in $provider.';
  }

  @override
  String erpRefusalCorrelationTotalMismatch(String provider) {
    return '$provider enthält bereits eine Rechnung aus einem früheren Versuch mit einem anderen Gesamtbetrag. Prüfen Sie diese Rechnung in $provider, bevor Sie es erneut versuchen.';
  }

  @override
  String erpRefusalTaxTypeNotResolved(String provider) {
    return 'Der Steuertyp eines Kontos in $provider ist kein fester Satz, daher kann die Steuer nicht herausgerechnet werden. Weisen Sie dem Konto in $provider einen Steuertyp zu und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalTaxMismatch(String provider) {
    return 'Die Steuer, die $provider buchen würde, weicht von der Steuer auf der Rechnung ab. Prüfen Sie die Steuertypen der Konten in $provider oder korrigieren Sie die Steuer und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalTaxNotStated(String provider) {
    return 'Die Rechnung weist keine Steuer aus, ihre Konten sind in $provider aber steuerpflichtig. Erfassen Sie die Steuer der Rechnung und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalSubscriptionKeyMissing(String provider) {
    return 'Der Abonnementschlüssel für $provider fehlt. Ein Administrator kann ihn unter Organisation hinzufügen; versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalApAccountNotConfigured(String provider) {
    return 'Für $provider ist kein Kreditorenkonto festgelegt. Ein Administrator kann es unter Organisation festlegen; versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalCurrencyNotConfigured(String provider) {
    return 'Für $provider ist keine Buchungswährung festgelegt. Ein Administrator kann sie unter Organisation festlegen; versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalCurrencyMismatch(String provider) {
    return 'Die Währung der Rechnung ist nicht die Buchungswährung von $provider. Korrigieren Sie die Währung und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalInvoiceDateMissing(String provider) {
    return '$provider benötigt ein Rechnungsdatum. Ergänzen Sie es und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalDueDateMissing(String provider) {
    return '$provider benötigt ein Fälligkeitsdatum. Ergänzen Sie es und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalAmountNotPositive(String provider) {
    return '$provider nimmt nur Rechnungen über einen Betrag größer als null an. Korrigieren Sie den Betrag oder erfassen Sie stattdessen eine Gutschrift.';
  }

  @override
  String erpRefusalJobUnconfirmed(String provider) {
    return '$provider hat die Rechnung angenommen, aber noch nicht bestätigt, dass sie angelegt wurde. Versuchen Sie es erneut, um nachzusehen – sie wird nicht doppelt gesendet.';
  }

  @override
  String erpRefusalJobFailed(String provider) {
    return '$provider hat die Rechnung angenommen, aber der Hintergrundauftrag ist fehlgeschlagen und hat nichts angelegt. Prüfen Sie Lieferant, Konten, Transaktionscodes und Buchungszeitraum in $provider, und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalCallBudgetTenant(String provider) {
    return 'Dieser Arbeitsbereich hat seinen Anteil an den heutigen $provider-API-Aufrufen verbraucht, daher hat FeohLedger keinen Aufruf gesendet. Das Kontingent wird um Mitternacht UTC zurückgesetzt – versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalCallBudgetQuota(String provider) {
    return '$provider hat die API-Aufrufe für das von FeohLedger genutzte Abonnement pausiert (das Tageskontingent ist verbraucht), daher hat FeohLedger keinen Aufruf gesendet. Es wird um Mitternacht UTC zurückgesetzt – versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalCallBudgetRateLimited(String provider) {
    return '$provider hat FeohLedger gebeten, langsamer zu werden, daher wurde kein Aufruf gesendet. Versuchen Sie es in einer Minute erneut.';
  }

  @override
  String erpRefusalBillLeftUnposted(String provider) {
    return '$provider führt diese Rechnung als Entwurf oder zur Genehmigung, nicht gebucht, daher steht dort noch nichts in der Kreditorenbuchhaltung. Buchen oder genehmigen Sie sie in $provider, und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalMergeSyncPending(String provider) {
    return '$provider hat die Rechnungen seit einem früheren Sendeversuch mit unbekanntem Ausgang noch nicht synchronisiert, daher könnte die Rechnung dort bereits vorhanden sein. Es wurde nichts gesendet. Versuchen Sie es nach der nächsten Rechnungssynchronisierung in $provider erneut: Ist die Rechnung dann vorhanden, wird sie verknüpft statt ein zweites Mal angelegt.';
  }

  @override
  String get erpRefusalCredentialsUnavailable =>
      'Die ERP-Zugangsdaten konnten gerade nicht geöffnet werden. Versuchen Sie es in einigen Minuten erneut; wenn es weiterhin fehlschlägt, wenden Sie sich an einen Administrator.';

  @override
  String erpRefusalPeriodClosed(String provider) {
    return '$provider hat diese Rechnung abgelehnt, weil ihr Datum in einem abgeschlossenen Zeitraum liegt (die Bücher sind geschlossen). Ändern Sie das Rechnungsdatum oder öffnen Sie den Zeitraum in $provider wieder, und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalVendorInactive(String provider) {
    return '$provider hat diese Rechnung abgelehnt, weil der Lieferant dort inaktiv oder gelöscht ist. Aktivieren Sie den Lieferanten in $provider, und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalAccountInactive(String provider, String accounts) {
    return '$provider hat diese Rechnung abgelehnt, weil $accounts dort inaktiv oder gelöscht ist. Aktivieren Sie das Konto in $provider oder kontieren Sie die Rechnung auf ein aktives Konto um, und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalReferenceInvalid(String provider, String code) {
    return '$provider hat diese Rechnung abgelehnt, weil sie auf einen Lieferanten oder ein Konto verweist, das dort nicht akzeptiert wird (Intuit-Fehler $code). Prüfen Sie, ob der Lieferant und jedes Konto der Rechnung in $provider existieren und aktiv sind, synchronisieren Sie sie erneut, und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalCompanySubscriptionInactive(String provider) {
    return '$provider hat diese Rechnung abgelehnt, weil das Abonnement des Unternehmens abgelaufen, gekündigt oder von einem Zahlungsproblem betroffen ist. Klären Sie das in $provider, und versuchen Sie es dann erneut.';
  }

  @override
  String erpRefusalIntuitFault(String provider, String code) {
    return '$provider hat diese Rechnung abgelehnt (Intuit-Fehler $code). Gleichen Sie die Angaben der Rechnung mit $provider ab und korrigieren Sie sie, und versuchen Sie es dann erneut. Wenn es weiterhin fehlschlägt, nennen Sie einem Administrator den Fehlercode.';
  }

  @override
  String erpRefusalPaymentAccountNotMapped(String provider) {
    return '$provider wurde über diese Zahlung nicht informiert: Es ist kein Konto festgelegt, von dem sie verbucht wird. Wählen Sie das Konto für Rechnungszahlungen unter Organisation → ERP-Integration und wiederholen Sie dann den Zahlungsabgleich.';
  }

  @override
  String erpRefusalBillNotInErp(String provider) {
    return 'Die Rechnung, für die diese Zahlung bestimmt war, ist in $provider nicht mehr vorhanden (sie wurde dort gelöscht), daher wurde die Zahlung dort nicht verbucht. Prüfen Sie die Rechnung in $provider.';
  }

  @override
  String erpRefusalPaymentExceedsBalance(String provider) {
    return '$provider zeigt für diese Rechnung einen geringeren offenen Betrag als diese Zahlung; eine Verbuchung würde die Rechnung überzahlen. Prüfen Sie, was in $provider bereits bezahlt wurde.';
  }

  @override
  String erpRefusalBillPaymentVoided(String provider) {
    return 'Diese Zahlung wurde in $provider verbucht und dort anschließend storniert, daher wird sie nicht erneut verbucht. Entscheiden Sie in $provider, ob die Rechnung bezahlt ist.';
  }

  @override
  String erpRefusalPaymentsScopeMissing(String provider) {
    return '$provider wurde verbunden, bevor FeohLedger die Berechtigung zum Verbuchen von Zahlungen angefordert hat, daher wird diese Zahlung dort nicht verbucht. Verbinden Sie $provider in den Organisationseinstellungen neu, um sie zu erteilen, und verbuchen Sie die Zahlung dann in $provider oder versuchen Sie es erneut.';
  }

  @override
  String erpRefusalExceptionBlocksExport(String type) {
    return 'Diese Rechnung hat eine offene Ausnahme ($type). Lösen oder verwerfen Sie sie unter Ausnahmen und senden Sie die Rechnung dann an das ERP.';
  }

  @override
  String erpRefusalExceptionBlocksExportPossiblyPosted(String type) {
    return 'Diese Rechnung hat eine offene Ausnahme ($type), und ein früherer Versuch hat die Rechnung möglicherweise bereits im ERP angelegt. Prüfen Sie das ERP, bevor jemand sie bezahlt, und lösen oder verwerfen Sie die Ausnahme dann unter Ausnahmen.';
  }

  @override
  String get erpRefusalPaymentBlocksExport =>
      'FeohLedger bezahlt diese Rechnung bereits: Es gibt eine aktive Zahlung dafür. Brechen Sie ihren Zahlungslauf ab oder stornieren Sie die Zahlung, bevor Sie die Rechnung an das ERP senden.';

  @override
  String get erpRefusalPaymentBlocksExportPossiblyPosted =>
      'FeohLedger bezahlt diese Rechnung bereits, und ein früherer Versuch hat die Rechnung möglicherweise bereits im ERP angelegt. Prüfen Sie das ERP und stellen Sie sicher, dass niemand die Rechnung dort zusätzlich bezahlt.';

  @override
  String get erpRefusalVendorBlocked =>
      'Der Lieferant dieser Rechnung ist für Zahlungen gesperrt. Heben Sie die Sperre auf, bevor Sie die Rechnung an das ERP senden.';

  @override
  String get erpRefusalVendorSanctionsMatch =>
      'Der Lieferant dieser Rechnung wurde auf einer Sanktionsliste gefunden. Prüfen Sie das Screening des Lieferanten, bevor Sie die Rechnung an das ERP senden.';

  @override
  String get erpRefusalVendorSanctionsReview =>
      'Der Lieferant dieser Rechnung wartet auf die Prüfung seines Sanktions-Screenings. Prüfen Sie den Lieferanten nach Abschluss der Prüfung erneut und senden Sie die Rechnung dann an das ERP.';

  @override
  String get erpRefusalVendorRejected =>
      'Der Lieferant dieser Rechnung wurde abgelehnt. Ordnen Sie die Rechnung dem richtigen Lieferanten zu, bevor Sie sie an das ERP senden.';

  @override
  String get erpRefusalVendorStatusUnknown =>
      'Der Lieferant dieser Rechnung hat einen Status, mit dem sie nicht an das ERP gesendet werden kann. Setzen Sie den Lieferanten auf aktiv, bevor Sie die Rechnung an das ERP senden.';

  @override
  String get erpRefusalVendorHoldPossiblyPosted =>
      'Ein früherer Versuch hat die Rechnung möglicherweise bereits im ERP angelegt; prüfen Sie dort, bevor jemand sie bezahlt.';

  @override
  String get erpRefusalCurrencyBlocksExport =>
      'Diese Rechnung hat keine gültige Währung. Legen Sie ihre Währung fest, bevor Sie sie an das ERP senden.';

  @override
  String get erpRefusalRetriesExhausted =>
      'Für diese Übertragung waren keine Versuche mehr übrig, daher wurde diesmal kein Versuch unternommen. Versuchen Sie es erneut, um sie mit neuen Versuchen zu senden.';

  @override
  String get erpRefusalPushInterrupted =>
      'Diese Übertragung wurde unterbrochen und nie abgeschlossen. Sie ist zu alt (oder älter als die aktuelle ERP-Verbindung), um sie ungeprüft erneut zu senden. Stellen Sie sicher, dass die Rechnung nicht schon im ERP ist, und versuchen Sie es dann erneut.';

  @override
  String get erpRefusalCardBlocksExport =>
      'Für die Zahlung dieser Rechnung wurde eine virtuelle Karte ausgestellt. Stornieren Sie die Karte, bevor Sie die Rechnung an das ERP senden.';

  @override
  String get erpRefusalCardBlocksExportPossiblyPosted =>
      'Für die Zahlung dieser Rechnung wurde eine virtuelle Karte ausgestellt, und ein früherer Versuch hat die Rechnung möglicherweise bereits im ERP angelegt. Prüfen Sie das ERP und stellen Sie sicher, dass niemand die Rechnung dort zusätzlich bezahlt.';

  @override
  String get erpRefusalTypeDuplicate => 'Doppelte Rechnung';

  @override
  String get erpRefusalTypeFraudFlag => 'Betrugshinweis';

  @override
  String exceptionDetailErpPaymentTitle(String erp) {
    return 'Von $erp gemeldete Zahlung';
  }

  @override
  String exceptionDetailErpPaymentAmount(String erp) {
    return 'Laut $erp gezahlter Betrag';
  }

  @override
  String get exceptionDetailErpPaymentPaidOn => 'Bezahlt am';

  @override
  String get exceptionDetailErpPaymentReference => 'Referenz';

  @override
  String erpRefusalUnknown(String erp) {
    return 'Dies wurde in $erp aus einem Grund abgelehnt, den diese App-Version nicht beschreiben kann. Aktualisieren Sie die App oder prüfen Sie den Vorgang in $erp.';
  }

  @override
  String get exceptionDetailErpPaymentNotStated => 'Nicht angegeben';

  @override
  String get exceptionDetailErpPaymentRecordOnWeb =>
      'Erfassen Sie diese Zahlung unter „Ausnahmen“ in der Web-App: Damit wird die Ausnahme gelöst und die Rechnung als bezahlt markiert.';

  @override
  String get exceptionDetailYourErp => 'Ihrem ERP';

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
  String invoiceWarningAiAllowanceReached(int included) {
    String _temp0 = intl.Intl.pluralLogic(
      included,
      locale: localeName,
      other: '$included KI-gelesenen Rechnungen',
      one: '$included KI-gelesenen Rechnung',
    );
    return 'KI-Erfassung pausiert: Das Monatskontingent von $_temp0 ist aufgebraucht. Erfassen Sie die Angaben manuell oder wechseln Sie unter Einstellungen → Abonnement in einen höheren Tarif.';
  }

  @override
  String get invoiceWarningAiSpendCapReached =>
      'KI-Erfassung pausiert: Das KI-Ausgabenlimit für diesen Monat ist erreicht. Erfassen Sie die Angaben manuell oder erhöhen Sie das Limit unter Einstellungen → Abonnement.';

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
  String invoiceWarningPoBilledBeyondTotal(
    String billedAmount,
    String poNumber,
    String poTotal,
  ) {
    return 'Andere Rechnungen stellen bereits $billedAmount gegen Bestellung $poNumber mit einer Gesamtsumme von $poTotal in Rechnung — diese Rechnung überschreitet diese Summe';
  }

  @override
  String invoiceWarningPoBilledBeyondReceipt(
    String billedAmount,
    String poNumber,
    String receivedValue,
  ) {
    return 'Andere Rechnungen stellen bereits $billedAmount gegen Bestellung $poNumber in Rechnung, von der Waren im Wert von $receivedValue eingegangen sind — diese Rechnung überschreitet den Wareneingang';
  }

  @override
  String invoiceWarningPoAmountVariancePoCurrencyUnknown(
    String variancePct,
    String poNumber,
    String invoiceAmount,
    String poTotal,
  ) {
    return 'Betragsabweichung $variancePct gegenüber Bestellung $poNumber, für die keine Währung erfasst ist (Rechnung $invoiceAmount, Bestellung $poTotal)';
  }

  @override
  String invoiceWarningPoCurrencyMismatch(
    String invoiceCurrency,
    String poNumber,
    String poCurrency,
  ) {
    return 'Die Rechnung lautet auf $invoiceCurrency, Bestellung $poNumber jedoch auf $poCurrency — die Beträge wurden nicht verglichen';
  }

  @override
  String invoiceWarningPoCurrencyMismatchInvoiceCurrencyUnknown(
    String poNumber,
    String poCurrencies,
  ) {
    return 'Die Rechnung enthält keine gültige Währung, und die Bestellungen hinter $poNumber lauten auf unterschiedliche Währungen ($poCurrencies) — die Beträge wurden nicht verglichen';
  }

  @override
  String invoiceWarningPoCancelled(String poNumber) {
    return 'Bestellung $poNumber ist storniert — die Rechnung wurde nicht mit ihr abgeglichen';
  }

  @override
  String invoiceWarningPoNotIssued(String poNumber) {
    return 'Bestellung $poNumber ist im ERP noch nicht freigegeben — die Rechnung wurde nicht mit ihr abgeglichen';
  }

  @override
  String invoiceWarningPoTotalUnverifiable(String poNumber) {
    return 'Die Summe der Bestellung $poNumber konnte bei der letzten Synchronisierung nicht vom ERP bestätigt werden — die Beträge wurden nicht verglichen';
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
  String invoiceWarningSelfCorrectionCurrencyConflict(
    String printedCurrency,
    String fieldCurrency,
  ) {
    return 'Die Beträge sind in $printedCurrency angegeben, die Rechnungswährung lautet jedoch $fieldCurrency. Die Beträge wurden leer gelassen – bitte aus dem Dokument übernehmen.';
  }

  @override
  String invoiceWarningSelfCorrectionCurrencyMixed(String printedCurrencies) {
    return 'Die Beträge sind in mehr als einer Währung angegeben ($printedCurrencies). Die Beträge wurden leer gelassen – bitte aus dem Dokument übernehmen.';
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
    return 'Sachkonten nicht im aktiven Kontenplan, nicht kontiert: $codes';
  }

  @override
  String invoiceWarningGlCodeStalePrior(String code) {
    return 'Das zwischengespeicherte Sachkonto „$code“ des Lieferanten ist nicht mehr im aktiven Kontenplan.';
  }

  @override
  String invoicePoMatchIssuePoNotFound(String poNumber) {
    return 'Bestellung $poNumber nicht gefunden';
  }

  @override
  String invoicePoMatchIssueCurrencyMismatch(
    String invoiceCurrency,
    String poCurrency,
  ) {
    return 'Währungsabweichung: Rechnung in $invoiceCurrency, Bestellung in $poCurrency — Beträge nicht verglichen';
  }

  @override
  String invoicePoMatchIssueCurrencyMismatchInvoiceCurrencyUnknown(
    String poCurrencies,
  ) {
    return 'Währungsabweichung: Rechnung ohne gültige Währung, Bestellungen in $poCurrencies — Beträge nicht verglichen';
  }

  @override
  String get invoicePoMatchIssuePoCancelled =>
      'Bestellung storniert — Beträge nicht verglichen';

  @override
  String get invoicePoMatchIssuePoNotIssued =>
      'Bestellung im ERP noch nicht freigegeben — Beträge nicht verglichen';

  @override
  String get invoicePoMatchIssuePoTotalUnverifiable =>
      'Bestellsumme bei der letzten Synchronisierung nicht vom ERP bestätigt — Beträge nicht verglichen';

  @override
  String invoicePoMatchIssueAmountMismatch(
    String invoiceAmount,
    String poTotal,
    String variancePct,
  ) {
    return 'Betragsabweichung: Rechnung $invoiceAmount, Bestellung $poTotal ($variancePct)';
  }

  @override
  String invoicePoMatchIssueAmountMismatchPoCurrencyUnknown(
    String invoiceAmount,
    String poTotal,
    String variancePct,
  ) {
    return 'Betragsabweichung: Rechnung $invoiceAmount, Bestellung $poTotal ($variancePct)';
  }

  @override
  String invoicePoMatchIssueAmountMismatchCurrencyUnknown(
    String invoiceAmount,
    String poTotal,
    String variancePct,
  ) {
    return 'Betragsabweichung: Rechnung $invoiceAmount, Bestellung $poTotal ($variancePct)';
  }

  @override
  String invoicePoMatchIssuePartialReceipt(String receivedPct) {
    return 'Teillieferung: $receivedPct der bestellten Menge erhalten';
  }

  @override
  String invoicePoMatchIssueOverReceipt(
    String receivedQuantity,
    String orderedQuantity,
    String excessQuantity,
  ) {
    return 'Überlieferung: $receivedQuantity geliefert gegenüber $orderedQuantity bestellt (+$excessQuantity)';
  }

  @override
  String get invoicePoMatchIssueInspectionFailed =>
      'Qualitätsprüfung nicht bestanden';

  @override
  String invoicePoMatchIssueInspectionFailedNotes(String notes) {
    return 'Qualitätsprüfung nicht bestanden: $notes';
  }

  @override
  String invoicePoMatchIssuePartialAcceptance(String acceptedQuantity) {
    return 'Teilannahme: $acceptedQuantity der bestellten Menge angenommen';
  }

  @override
  String get invoicePoMatchIssuePartialAcceptanceUnquantified =>
      'Teilannahme: ein Teil der bestellten Menge angenommen';

  @override
  String get invoicePoMatchIssueInspectionRequiredMissing =>
      'Qualitätsprüfung erforderlich, aber nicht vorhanden';

  @override
  String get exceptionDescriptionMissingDataAfterExtraction =>
      'Pflichtfelder fehlen nach der Extraktion';

  @override
  String exceptionDescriptionPriceVarianceFindings(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Positionen',
      one: '$count Position',
    );
    return 'Preisabweichung gegenüber der Lieferantenhistorie bei $_temp0';
  }

  @override
  String exceptionDescriptionContractNoncompliantFindings(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Befunde zur Vertragskonformität',
      one: '$count Befund zur Vertragskonformität',
    );
    return '$_temp0';
  }

  @override
  String exceptionDescriptionErpPaidUnrecordedBlockingException(String erp) {
    return '$erp meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Eine andere Ausnahme zu dieser Rechnung blockiert die Zahlung. Lösen Sie diese Ausnahme, dann erfasst FeohLedger die Zahlung automatisch.';
  }

  @override
  String get exceptionDescriptionErpPaidUnrecordedBlockingExceptionUnnamed =>
      'Ihr ERP meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Eine andere Ausnahme zu dieser Rechnung blockiert die Zahlung. Lösen Sie diese Ausnahme, dann erfasst FeohLedger die Zahlung automatisch.';

  @override
  String exceptionDescriptionErpPaidUnrecordedInRun(String erp) {
    return '$erp meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Die Rechnung ist auch in einem FeohLedger-Zahlungslauf, sodass der Lieferant doppelt bezahlt werden könnte. Nehmen Sie sie aus dem Lauf oder brechen Sie den Lauf ab, und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme.';
  }

  @override
  String get exceptionDescriptionErpPaidUnrecordedInRunUnnamed =>
      'Ihr ERP meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Die Rechnung ist auch in einem FeohLedger-Zahlungslauf, sodass der Lieferant doppelt bezahlt werden könnte. Nehmen Sie sie aus dem Lauf oder brechen Sie den Lauf ab, und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme.';

  @override
  String exceptionDescriptionErpPaidUnrecordedPaymentLive(String erp) {
    return '$erp meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: FeohLedger hat für diese Rechnung eine eigene Zahlung in Bearbeitung, sodass der Lieferant doppelt bezahlt werden könnte. Stornieren Sie diese Zahlung, wenn die Zahlung aus dem ERP die tatsächliche ist, und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme.';
  }

  @override
  String get exceptionDescriptionErpPaidUnrecordedPaymentLiveUnnamed =>
      'Ihr ERP meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: FeohLedger hat für diese Rechnung eine eigene Zahlung in Bearbeitung, sodass der Lieferant doppelt bezahlt werden könnte. Stornieren Sie diese Zahlung, wenn die Zahlung aus dem ERP die tatsächliche ist, und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme.';

  @override
  String exceptionDescriptionErpPaidUnrecordedCardLive(String erp) {
    return '$erp meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Für diese Rechnung ist noch eine virtuelle Karte aktiv, sodass der Lieferant doppelt bezahlt werden könnte. Sperren Sie die Karte und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme.';
  }

  @override
  String get exceptionDescriptionErpPaidUnrecordedCardLiveUnnamed =>
      'Ihr ERP meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Für diese Rechnung ist noch eine virtuelle Karte aktiv, sodass der Lieferant doppelt bezahlt werden könnte. Sperren Sie die Karte und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme.';

  @override
  String exceptionDescriptionErpPaidUnrecordedCreditConflict(String erp) {
    return '$erp meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Eine angewendete Gutschrift passt nicht mehr zu Lieferant oder Währung dieser Rechnung, daher lässt sich der offene Betrag nicht bestimmen. Korrigieren Sie die Gutschrift und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme.';
  }

  @override
  String get exceptionDescriptionErpPaidUnrecordedCreditConflictUnnamed =>
      'Ihr ERP meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Eine angewendete Gutschrift passt nicht mehr zu Lieferant oder Währung dieser Rechnung, daher lässt sich der offene Betrag nicht bestimmen. Korrigieren Sie die Gutschrift und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme.';

  @override
  String exceptionDescriptionErpPaidUnrecordedNothingToPay(String erp) {
    return '$erp meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Gutschriften decken bereits die gesamte Rechnung ab, es gibt also keine Zahlung zu erfassen. Prüfen Sie die Rechnung im ERP und lösen Sie dann diese Ausnahme.';
  }

  @override
  String get exceptionDescriptionErpPaidUnrecordedNothingToPayUnnamed =>
      'Ihr ERP meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Gutschriften decken bereits die gesamte Rechnung ab, es gibt also keine Zahlung zu erfassen. Prüfen Sie die Rechnung im ERP und lösen Sie dann diese Ausnahme.';

  @override
  String exceptionDescriptionErpPaidUnrecordedNotPayable(String erp) {
    return '$erp meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Die Rechnung hat keinen Status, in dem sie als bezahlt markiert werden kann. Prüfen Sie die Rechnung und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme oder lösen Sie sie.';
  }

  @override
  String get exceptionDescriptionErpPaidUnrecordedNotPayableUnnamed =>
      'Ihr ERP meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Die Rechnung hat keinen Status, in dem sie als bezahlt markiert werden kann. Prüfen Sie die Rechnung und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme oder lösen Sie sie.';

  @override
  String exceptionDescriptionErpPaidUnrecordedAmountMismatch(String erp) {
    return '$erp meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Der dort als bezahlt ausgewiesene Betrag entspricht nicht dem, was diese Rechnung in FeohLedger nach Gutschriften und einem etwaigen Skonto schuldet. Prüfen Sie die Rechnung im ERP und in FeohLedger, korrigieren Sie die fehlerhafte und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme oder lösen Sie sie.';
  }

  @override
  String get exceptionDescriptionErpPaidUnrecordedAmountMismatchUnnamed =>
      'Ihr ERP meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: Der dort als bezahlt ausgewiesene Betrag entspricht nicht dem, was diese Rechnung in FeohLedger nach Gutschriften und einem etwaigen Skonto schuldet. Prüfen Sie die Rechnung im ERP und in FeohLedger, korrigieren Sie die fehlerhafte und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme oder lösen Sie sie.';

  @override
  String exceptionDescriptionErpPaidUnrecordedOther(String erp) {
    return '$erp meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: FeohLedger hat die Erfassung abgelehnt. Prüfen Sie die Rechnung und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme oder lösen Sie sie.';
  }

  @override
  String get exceptionDescriptionErpPaidUnrecordedOtherUnnamed =>
      'Ihr ERP meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht erfassen: FeohLedger hat die Erfassung abgelehnt. Prüfen Sie die Rechnung und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme oder lösen Sie sie.';

  @override
  String exceptionDescriptionErpPaidWithdrawn(String erp) {
    return '$erp hat diese Rechnung als bezahlt gemeldet, zeigt sie aber nicht mehr als bezahlt an. FeohLedger hat diese Zahlung nicht erfasst. Prüfen Sie die Rechnung im ERP: Ist sie noch offen, lösen Sie diese Ausnahme und bezahlen Sie sie wie gewohnt.';
  }

  @override
  String get exceptionDescriptionErpPaidWithdrawnUnnamed =>
      'Ihr ERP hat diese Rechnung als bezahlt gemeldet, zeigt sie aber nicht mehr als bezahlt an. FeohLedger hat diese Zahlung nicht erfasst. Prüfen Sie die Rechnung im ERP: Ist sie noch offen, lösen Sie diese Ausnahme und bezahlen Sie sie wie gewohnt.';

  @override
  String exceptionDescriptionErpPaidRetryFailed(String erp) {
    return '$erp meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht automatisch erfassen. Prüfen Sie die Angaben und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme.';
  }

  @override
  String get exceptionDescriptionErpPaidRetryFailedUnnamed =>
      'Ihr ERP meldet diese Rechnung als bezahlt, aber FeohLedger konnte diese Zahlung nicht automatisch erfassen. Prüfen Sie die Angaben und erfassen Sie dann die Zahlung aus dem ERP über diese Ausnahme.';

  @override
  String exceptionDescriptionErpSettledShort(String erp) {
    return '$erp meldet diese Rechnung als bezahlt, aber die eigene Zahlung von FeohLedger wurde über weniger als den von der Kreditorenbuchhaltung freigegebenen Betrag abgewickelt, daher bleibt die Rechnung im Status „Zahlung geplant“. Öffnen Sie die Zahlung und akzeptieren Sie entweder den abgewickelten Betrag oder stornieren Sie die Zahlung.';
  }

  @override
  String get exceptionDescriptionErpSettledShortUnnamed =>
      'Ihr ERP meldet diese Rechnung als bezahlt, aber die eigene Zahlung von FeohLedger wurde über weniger als den von der Kreditorenbuchhaltung freigegebenen Betrag abgewickelt, daher bleibt die Rechnung im Status „Zahlung geplant“. Öffnen Sie die Zahlung und akzeptieren Sie entweder den abgewickelten Betrag oder stornieren Sie die Zahlung.';

  @override
  String exceptionDescriptionErpVoidedAfterPost(String erp) {
    return '$erp meldet diese Rechnung als storniert oder annulliert, aber FeohLedger hatte sie bereits zur Zahlung weitergegeben, daher wurde sie hier nicht storniert. Prüfen Sie, ob eine Zahlung an den Lieferanten unterwegs ist, und stoppen oder holen Sie sie zurück, wenn die Rechnung wirklich ungültig ist.';
  }

  @override
  String get exceptionDescriptionErpVoidedAfterPostUnnamed =>
      'Ihr ERP meldet diese Rechnung als storniert oder annulliert, aber FeohLedger hatte sie bereits zur Zahlung weitergegeben, daher wurde sie hier nicht storniert. Prüfen Sie, ob eine Zahlung an den Lieferanten unterwegs ist, und stoppen oder holen Sie sie zurück, wenn die Rechnung wirklich ungültig ist.';

  @override
  String get exceptionDescriptionErpCreditSettlement =>
      'Laut ERP ist diese Rechnung zumindest teilweise durch eine dort angewendete Lieferantengutschrift beglichen. FeohLedger bezahlt sie erst, wenn jemand bestätigt, was noch offen ist.';

  @override
  String get exceptionDescriptionErpPaidWhileScheduled =>
      'Laut ERP wurde diese Rechnung durch eine Zahlung beglichen, die nicht von FeohLedger stammt, während in FeohLedger eine Zahlung dafür geplant ist. Stoppen oder stornieren Sie eine der beiden, bevor beide beim Lieferanten ankommen.';

  @override
  String get exceptionDescriptionErpPaidTwice =>
      'FeohLedger hat diese Rechnung bezahlt, und laut ERP wurde sie zusätzlich durch eine Zahlung beglichen, die nicht von FeohLedger stammt – der Lieferant wurde möglicherweise doppelt bezahlt. Holen Sie eine der Zahlungen zurück.';

  @override
  String get exceptionDescriptionErpPartlyPaidOutside =>
      'Laut ERP wurde ein Teil dieser Rechnung außerhalb von FeohLedger bezahlt oder gutgeschrieben; eine FeohLedger-Zahlung über den vollen Betrag würde sie überzahlen. Klären Sie vor der Zahlung, was noch offen ist.';

  @override
  String get exceptionDescriptionErpRecordedPaymentWithdrawn =>
      'Das ERP hat diese Rechnung als bezahlt gemeldet und FeohLedger hat diese Zahlung erfasst, aber das ERP zeigt sie nicht mehr als bezahlt an (die Zahlung wurde dort gelöscht oder storniert). Bestätigen Sie, ob sie noch offen ist.';

  @override
  String get exceptionDescriptionErpBillDeletedBlocking =>
      'Der Beleg, als der diese Rechnung im ERP gebucht wurde, wurde dort gelöscht oder storniert; FeohLedger bezahlt sie erst, wenn jemand bestätigt, dass sie noch offen ist.';

  @override
  String get exceptionDescriptionErpBillDeleted =>
      'Der Beleg, als der diese Rechnung im ERP gebucht wurde, wurde dort gelöscht oder storniert. Bestätigen Sie, dass die Bücher noch übereinstimmen.';

  @override
  String get exceptionDescriptionErpBillUnlinkedNoDocument =>
      'FeohLedger kann den Beleg dieser Rechnung im ERP nicht prüfen, weil das ERP beim Buchen nicht angegeben hat, welchen Beleg es angelegt hat, daher bezahlt FeohLedger sie nicht. Prüfen Sie im ERP, ob sie noch offen ist, und erfassen Sie sie dann als außerhalb von FeohLedger bezahlt oder buchen Sie sie erneut.';

  @override
  String get exceptionDescriptionErpBillUnlinkedNoCompany =>
      'FeohLedger kann den Beleg dieser Rechnung im ERP nicht prüfen, weil bei der Buchung nicht festgehalten wurde, an welches ERP-Unternehmen sie ging, daher bezahlt FeohLedger sie nicht. Prüfen Sie im ERP, ob sie noch offen ist, und erfassen Sie sie dann als außerhalb von FeohLedger bezahlt oder buchen Sie sie erneut.';

  @override
  String get exceptionDescriptionErpBillUnlinkedOtherCompany =>
      'FeohLedger kann den Beleg dieser Rechnung im ERP nicht prüfen, weil sie an ein anderes ERP-Unternehmen gebucht wurde als das jetzt verbundene, daher bezahlt FeohLedger sie nicht. Prüfen Sie im ERP, ob sie noch offen ist, und erfassen Sie sie dann als außerhalb von FeohLedger bezahlt oder buchen Sie sie erneut.';

  @override
  String get exceptionDescriptionErpDispatchStoppedPaid =>
      'FeohLedger war im Begriff, diese Rechnung zu bezahlen, aber laut ERP ist ihr Beleg bereits bezahlt. Die Zahlung wurde gestoppt, bevor Geld bewegt wurde. Bestätigen Sie vor der Zahlung, was noch offen ist; wurde für diese Rechnung bereits eine virtuelle Karte ausgegeben, sperren Sie sie.';

  @override
  String get exceptionDescriptionErpDispatchStoppedPartiallyPaid =>
      'FeohLedger war im Begriff, diese Rechnung zu bezahlen, aber laut ERP ist ihr Beleg teilweise bezahlt. Die Zahlung wurde gestoppt, bevor Geld bewegt wurde. Bestätigen Sie vor der Zahlung, was noch offen ist; wurde für diese Rechnung bereits eine virtuelle Karte ausgegeben, sperren Sie sie.';

  @override
  String get exceptionDescriptionErpDispatchStoppedDeleted =>
      'FeohLedger war im Begriff, diese Rechnung zu bezahlen, aber laut ERP ist ihr Beleg gelöscht oder storniert. Die Zahlung wurde gestoppt, bevor Geld bewegt wurde. Bestätigen Sie vor der Zahlung, was noch offen ist; wurde für diese Rechnung bereits eine virtuelle Karte ausgegeben, sperren Sie sie.';

  @override
  String get exceptionDescriptionErpDispatchStoppedUnconfirmed =>
      'FeohLedger war im Begriff, diese Rechnung zu bezahlen, aber das ERP konnte nicht bestätigen, dass ihr Beleg noch offen und unbezahlt ist. Die Zahlung wurde gestoppt, bevor Geld bewegt wurde. Bestätigen Sie vor der Zahlung, was noch offen ist; wurde für diese Rechnung bereits eine virtuelle Karte ausgegeben, sperren Sie sie.';

  @override
  String get exceptionDescriptionErpReportedPaymentVoided =>
      'Eine vom ERP gemeldete Zahlung wurde in FeohLedger storniert, aber das ERP zeigt die Rechnung möglicherweise noch als bezahlt an. FeohLedger bezahlt sie erst wieder, wenn jemand bestätigt, dass die Zahlung im ERP rückgängig gemacht ist.';

  @override
  String exceptionDescriptionErpPaymentWritebackFailed(String erp) {
    return '$erp hat die Zahlung dieser Rechnung nicht erfasst, daher wird die Rechnung dort weiterhin als unbezahlt angezeigt, obwohl das Geld gezahlt wurde. Beheben Sie die Ursache und verwenden Sie dann „ERP-Abgleich wiederholen“ beim Zahlungslauf dieser Zahlung unter Zahlungen.';
  }

  @override
  String get exceptionDescriptionErpPaymentWritebackFailedUnnamed =>
      'Ihr ERP hat die Zahlung dieser Rechnung nicht erfasst, daher wird die Rechnung dort weiterhin als unbezahlt angezeigt, obwohl das Geld gezahlt wurde. Beheben Sie die Ursache und verwenden Sie dann „ERP-Abgleich wiederholen“ beim Zahlungslauf dieser Zahlung unter Zahlungen.';

  @override
  String exceptionDescriptionErpPaymentWritebackBalanceOpen(
    String erp,
    String amount,
  ) {
    return '$erp hat die Zahlung dieser Rechnung erfasst, aber dort sind noch $amount offen (ein in FeohLedger abgezogenes Skonto oder eine Gutschrift). Gleichen Sie die Differenz im ERP aus.';
  }

  @override
  String exceptionDescriptionErpPaymentWritebackBalanceOpenUnnamed(
    String amount,
  ) {
    return 'Ihr ERP hat die Zahlung dieser Rechnung erfasst, aber dort sind noch $amount offen (ein in FeohLedger abgezogenes Skonto oder eine Gutschrift). Gleichen Sie die Differenz im ERP aus.';
  }

  @override
  String exceptionDescriptionErpPaymentVoidWritebackFailed(String erp) {
    return '$erp zeigt diese Rechnung weiterhin als bezahlt an – durch eine Zahlung, die in FeohLedger storniert wurde, deren Stornierung im ERP aber fehlgeschlagen ist. Stornieren Sie diese Zahlung im ERP, damit die Bücher übereinstimmen.';
  }

  @override
  String get exceptionDescriptionErpPaymentVoidWritebackFailedUnnamed =>
      'Ihr ERP zeigt diese Rechnung weiterhin als bezahlt an – durch eine Zahlung, die in FeohLedger storniert wurde, deren Stornierung im ERP aber fehlgeschlagen ist. Stornieren Sie diese Zahlung im ERP, damit die Bücher übereinstimmen.';

  @override
  String get exceptionDescriptionErpSyncBackFailed =>
      'Die Zahlung dieser Rechnung wurde abgewickelt, aber FeohLedger konnte die Erfassung nicht abschließen, daher steht die Rechnung noch nicht auf „Bezahlt“, obwohl das Geld geflossen ist. Nichts wiederholt dies automatisch: Verwenden Sie „ERP-Abgleich wiederholen“ beim Zahlungslauf dieser Zahlung unter Zahlungen.';

  @override
  String exceptionDescriptionExtractionFailed(String errorClass) {
    return 'Extraktion fehlgeschlagen: $errorClass';
  }

  @override
  String exceptionDescriptionPositivePayAlteredAmount(String checkNumber) {
    return 'Positive-Pay-Rückmeldung: Scheck $checkNumber mit verändertem Betrag';
  }

  @override
  String exceptionDescriptionPositivePayNotOnFile(String checkNumber) {
    return 'Positive-Pay-Rückmeldung: Scheck $checkNumber nicht in der Ausstellungsdatei';
  }

  @override
  String get exceptionDescriptionVendorBankChanged =>
      'Bankverbindung des Lieferanten geändert; vor der Zahlung prüfen.';

  @override
  String get exceptionDescriptionPayeeChangedSincePaid =>
      'Zahlungsempfänger seit der letzten Zahlung dieser Rechnung geändert; vor erneuter Zahlung prüfen.';

  @override
  String get exceptionDescriptionPayeeUnrecordedAfterMerge =>
      'Lieferant seit der letzten Zahlung dieser Rechnung zusammengeführt, und diese Zahlung hat keinen Zahlungsempfänger erfasst; vor erneuter Zahlung prüfen.';

  @override
  String exceptionDescriptionComplianceHoldFindings(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Gründe',
      one: '$count Grund',
    );
    return 'Zahlung zur Compliance-Prüfung angehalten ($_temp0)';
  }

  @override
  String get exceptionDescriptionComplianceNoScreenableVendor =>
      'Kein prüfbarer Lieferant auf der Rechnung';

  @override
  String exceptionDescriptionComplianceProviderUnavailable(String provider) {
    return 'Sanktionsprüfung konnte nicht ausgeführt werden: kein Adapter für den konfigurierten Anbieter „$provider“';
  }

  @override
  String exceptionDescriptionComplianceReviewRequired(
    String provider,
    String matchedList,
  ) {
    return 'Lieferantenprüfung über $provider erfordert eine Überprüfung ($matchedList)';
  }

  @override
  String exceptionDescriptionComplianceReviewRequiredUnlisted(String provider) {
    return 'Lieferantenprüfung über $provider erfordert eine Überprüfung (siehe Prüfprotokoll)';
  }

  @override
  String exceptionDescriptionComplianceUnrecognisedResult(
    String provider,
    String result,
  ) {
    return 'Lieferantenprüfung über $provider lieferte ein unbekanntes Ergebnis „$result“; wird als prüfungsbedürftig behandelt';
  }

  @override
  String exceptionDescriptionComplianceAdverseMedia(String provider) {
    return 'Lieferantenprüfung über $provider ergab negative Medienberichte; Prüfung durch die Kreditorenbuchhaltung erforderlich';
  }

  @override
  String get exceptionDescriptionComplianceAdverseMediaUnnamedProvider =>
      'Lieferantenprüfung ergab negative Medienberichte; Prüfung durch die Kreditorenbuchhaltung erforderlich';

  @override
  String exceptionDescriptionComplianceAmlSpend(
    String projected,
    String threshold,
  ) {
    return 'Die Ausgaben bei diesem Lieferanten in den letzten 12 Monaten würden $projected erreichen (Schwellenwert $threshold); Prüfung durch die Kreditorenbuchhaltung erforderlich';
  }

  @override
  String exceptionDescriptionComplianceAmlSpendPartial(
    String projected,
    String threshold,
    int excluded,
  ) {
    String _temp0 = intl.Intl.pluralLogic(
      excluded,
      locale: localeName,
      other: '$excluded Zahlungen wurden',
      one: '$excluded Zahlung wurde',
    );
    return 'Die Ausgaben bei diesem Lieferanten in den letzten 12 Monaten würden mindestens $projected erreichen (Schwellenwert $threshold); Prüfung durch die Kreditorenbuchhaltung erforderlich. $_temp0 ohne Betrag in der Heimatwährung nicht berücksichtigt, die tatsächliche Summe ist also höher';
  }

  @override
  String exceptionDescriptionPaymentAgedOut(
    String paymentId,
    String hours,
    String invoiceStatus,
  ) {
    return 'Die Zahlung $paymentId war nach $hours h noch unterwegs und wurde vom Abgleich als fehlgeschlagen markiert; der Zahlungsweg hat nie bestätigt, das Geld kann also geflossen sein oder nicht. Die Rechnung steht auf „$invoiceStatus“. Klären Sie es mit dem Zahlungsdienstleister und stornieren Sie oder zahlen Sie erneut – bis dahin ist diese Rechnung für neue Zahlungsläufe gesperrt.';
  }

  @override
  String exceptionDescriptionPaymentDispatchInDoubt(
    String paymentId,
    String failureCode,
  ) {
    return 'Die Zahlung $paymentId ist beim Versand fehlgeschlagen ($failureCode), nachdem der Zahlungsdienstleister sie möglicherweise schon angenommen hatte; das Geld kann also geflossen sein oder nicht. Klären Sie es mit dem Zahlungsdienstleister und stornieren Sie oder zahlen Sie erneut – bis dahin ist diese Rechnung für neue Zahlungsläufe gesperrt.';
  }

  @override
  String exceptionDescriptionPaymentDispatchInterruptedFailed(
    String paymentId,
  ) {
    return 'Die Zahlung $paymentId ist fehlgeschlagen, aber ein früherer Sendeversuch wurde unterbrochen, nachdem sie den Zahlungsdienstleister möglicherweise schon erreicht hatte; das Geld kann also geflossen sein oder nicht. Klären Sie es mit dem Zahlungsdienstleister und stornieren Sie oder zahlen Sie erneut – bis dahin ist diese Rechnung für neue Zahlungsläufe gesperrt.';
  }

  @override
  String exceptionDescriptionPaymentDispatchInterruptedCancelled(
    String paymentId,
  ) {
    return 'Die Zahlung $paymentId wurde abgebrochen, aber ein früherer Sendeversuch wurde unterbrochen, nachdem sie den Zahlungsdienstleister möglicherweise schon erreicht hatte; das Geld kann also geflossen sein oder nicht. Klären Sie es mit dem Zahlungsdienstleister und stornieren Sie oder zahlen Sie erneut – bis dahin ist diese Rechnung für neue Zahlungsläufe gesperrt.';
  }

  @override
  String exceptionDescriptionSettlementCurrencyMismatch(
    String settledAmount,
    String settledCurrency,
    String authorizedAmount,
    String authorizedCurrency,
  ) {
    return 'Abweichende Abrechnungswährung: Der Zahlungsdienstleister meldete $settledAmount $settledCurrency, diese Zahlung genehmigt aber $authorizedAmount $authorizedCurrency. Geld wurde in einer nicht genehmigten Währung bewegt – gleichen Sie es mit dem Zahlungsdienstleister ab, bevor Sie diese Rechnung freigeben.';
  }

  @override
  String exceptionDescriptionSettlementAmountOver(
    String settledAmount,
    String settledCurrency,
    String authorizedAmount,
    String authorizedCurrency,
    String variance,
  ) {
    return 'Abweichender Abrechnungsbetrag: Der Zahlungsdienstleister meldete $settledAmount $settledCurrency, MEHR als die genehmigten $authorizedAmount $authorizedCurrency (Abweichung $variance). Geld wurde in nicht genehmigter Höhe bewegt – gleichen Sie es mit dem Zahlungsdienstleister ab, bevor Sie diese Rechnung freigeben.';
  }

  @override
  String exceptionDescriptionSettlementAmountUnder(
    String settledAmount,
    String settledCurrency,
    String authorizedAmount,
    String authorizedCurrency,
    String variance,
  ) {
    return 'Abweichender Abrechnungsbetrag: Der Zahlungsdienstleister meldete $settledAmount $settledCurrency, WENIGER als die genehmigten $authorizedAmount $authorizedCurrency (Abweichung $variance). Geld wurde in nicht genehmigter Höhe bewegt – gleichen Sie es mit dem Zahlungsdienstleister ab, bevor Sie diese Rechnung freigeben.';
  }

  @override
  String get warningsPoMatchStatusMatched => 'Abgeglichen';

  @override
  String get warningsPoMatchStatusMismatch => 'Abweichung';

  @override
  String get warningsPoMatchStatusPartial => 'Teilweise';

  @override
  String get warningsPoMatchStatusNoPo => 'Keine Bestellung';

  @override
  String warningsPoMatchAnnouncement(String summary) {
    return 'Bestellabgleich: $summary';
  }

  @override
  String get codedRefusalInvoiceNotInReview =>
      'Diese Rechnung wartet nicht auf Prüfung und kann daher weder genehmigt noch abgelehnt werden.';

  @override
  String get codedRefusalInvoiceBulkNotFound =>
      'Diese Rechnung wurde in der ausgewählten Einheit nicht gefunden.';

  @override
  String codedRefusalInvoiceStatusImmutable(String status) {
    return 'Diese Rechnung hat den Status $status und kann nicht mehr geändert werden.';
  }

  @override
  String get codedRefusalInvoiceRecallOneAtATime =>
      'Eine Genehmigung wird je Rechnung einzeln und mit Begründung zurückgenommen: Verwenden Sie „Genehmigung zurücknehmen“ an der Rechnung.';

  @override
  String codedRefusalInvoiceBulkRefused(String status) {
    return 'Diese Rechnung konnte nicht auf $status gesetzt werden.';
  }

  @override
  String codedRefusalInvoiceRequiredFieldsMissing(String fields) {
    return 'Pflichtfelder fehlen: $fields';
  }

  @override
  String codedRefusalInvoiceTransitionInvalid(String from, String to) {
    return 'Diese Rechnung kann nicht von $from zu $to wechseln.';
  }

  @override
  String codedRefusalInvoiceTransitionRecallOnly(String from, String to) {
    return 'Diese Rechnung kann nur durch Zurücknehmen ihrer Genehmigung von $from zu $to wechseln, eine Rechnung nach der anderen: Verwenden Sie „Genehmigung zurücknehmen“ an der Rechnung.';
  }

  @override
  String get codedRefusalInvoiceOnceApprovedNotReread =>
      'Diese Rechnung wurde bereits genehmigt, daher kann ihr Beleg nicht erneut ausgelesen werden. Nehmen Sie stattdessen ihre Genehmigung zurück und korrigieren Sie sie.';

  @override
  String get invoicesBulkSkipNoReason => 'Kein Grund angegeben';

  @override
  String get invoicesBulkSkipNoNumber => 'Rechnung (ohne Nummer)';

  @override
  String get reviewVerdictsUnavailable =>
      'Es konnte nicht geprüft werden, was Sie hier tun dürfen – zum Wiederholen nach unten ziehen';

  @override
  String get reviewVerdictsChecking =>
      'Es wird geprüft, was Sie hier tun dürfen …';

  @override
  String get payRecordOnlyBanner =>
      'FeohLedger erfasst Zahlungen, versendet sie aber nie: Zahlen Sie über Ihre Bank oder Ihr ERP und erfassen Sie jede Zahlung hier (die Bankdatei eines Laufs finden Sie in der Web-App).';

  @override
  String payRecordPrefilledFromErp(String erp) {
    return 'Aus der von $erp gemeldeten Zahlung übernommen. Prüfen Sie die Angaben, bevor Sie sie erfassen.';
  }

  @override
  String payRecordErpAmount(String erp) {
    return 'Laut $erp gezahlter Betrag';
  }

  @override
  String payRecordSplitTitle(String erp) {
    return 'So wurde laut $erp gezahlt';
  }

  @override
  String get payRecordRailNotStated => 'Zahlungsart nicht angegeben';

  @override
  String get payRecordSplitMethod =>
      'Aufgeteilt auf die oben genannten Zahlungsarten';

  @override
  String get payRecordSplitStands =>
      'Diese Zahlung erfolgte mit mehr als einer Zahlungsart und wird deshalb mit dieser Aufteilung erfasst. Eine einzelne Zahlungsart kann sie nicht beschreiben.';

  @override
  String get payRecordSplitDropped =>
      'Diese Teilbeträge ergeben nicht den gezahlten Betrag, daher wird die Aufteilung nicht übernommen. Die gewählte Zahlungsart wird für die gesamte Zahlung erfasst.';

  @override
  String get payRecordSplitUnjudged =>
      'Ob diese Aufteilung übernommen wird, wird beim Erfassen der Zahlung entschieden. Wird sie nicht übernommen, wird die gewählte Zahlungsart für die gesamte Zahlung erfasst.';

  @override
  String get payRecordAction => 'Als bezahlt erfassen';

  @override
  String payRecordActionLabel(String invoice) {
    return 'Als bezahlt erfassen — Rechnung $invoice';
  }

  @override
  String get payRecordTitle =>
      'Außerhalb von FeohLedger geleistete Zahlung erfassen';

  @override
  String get payRecordIntro =>
      'Verwenden Sie dies, wenn Sie den Lieferanten bereits über Ihre eigene Bank oder Ihr ERP bezahlt haben. FeohLedger erfasst die Zahlung und markiert die Rechnung als bezahlt. Es wird kein Geld versendet.';

  @override
  String get payRecordAmountOwed => 'Offener Betrag';

  @override
  String get payRecordAmountHint =>
      'Erfasst wird der volle offene Betrag, abzüglich angewendeter Gutschriften und eines angenommenen Skontos, das am Zahlungsdatum noch gilt. Teilzahlungen können nicht erfasst werden.';

  @override
  String get payRecordMethod => 'Zahlungsart';

  @override
  String get payRecordReference => 'Referenz';

  @override
  String get payRecordReferenceHint =>
      'Die Schecknummer oder die Bestätigungsnummer Ihrer Bank. Der Bankabgleich ordnet darüber zu.';

  @override
  String get payRecordPaidOn => 'Bezahlt am';

  @override
  String get payRecordSubmit => 'Als bezahlt erfassen';

  @override
  String get payRecordSubmitting => 'Wird erfasst…';

  @override
  String payRecordRecorded(String invoice) {
    return 'Rechnung $invoice als bezahlt erfasst';
  }

  @override
  String get payRecordFailed => 'Die Zahlung konnte nicht erfasst werden';

  @override
  String get payRecordUnavailable =>
      'Diese Rechnung kann noch nicht als bezahlt erfasst werden.';

  @override
  String get payMethodBacs => 'BACS';

  @override
  String get payMethodFasterPayments => 'Faster Payments';

  @override
  String get payMethodChaps => 'CHAPS';

  @override
  String get payMethodCardOutside => 'Karte (außerhalb von FeohLedger)';

  @override
  String get payRunActionRecord => 'Lauf als bezahlt erfassen…';

  @override
  String get payRunRecordTitle => 'Lauf als bezahlt erfassen';

  @override
  String get payRunRecordIntro =>
      'Sobald Ihre Bank die Zahlungen angenommen hat, erfassen Sie sie hier. Jede Zahlung des Laufs wird als abgeschlossen und ihre Rechnung als bezahlt markiert. Es wird nichts versendet.';

  @override
  String get payRunRecordReference => 'Referenz des Bankauftrags';

  @override
  String get payRunRecordReferenceHint =>
      'Die Bestätigungsnummer, die Ihre Bank für den Auftrag vergeben hat. Der Bankabgleich ordnet darüber zu.';

  @override
  String payRunRecordSubmit(String amount) {
    return 'Als bezahlt erfassen · $amount';
  }

  @override
  String payRunRecordRecorded(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count Zahlungen als bezahlt erfasst',
      one: '$count Zahlung als bezahlt erfasst',
    );
    return '$_temp0';
  }

  @override
  String get payRunRecordFailed => 'Der Lauf konnte nicht erfasst werden';

  @override
  String get payRunRecordLoadFailed =>
      'Zahlungslauf konnte nicht geladen werden';

  @override
  String get payRunStatusExported => 'Exportiert';

  @override
  String get codedRefusalPaymentsRecordOnly =>
      'FeohLedger versendet für diese Organisation keine Zahlungen. Bezahlen Sie den Lieferanten über Ihre Bank oder Ihr ERP und erfassen Sie die Zahlung dann als außerhalb von FeohLedger bezahlt.';

  @override
  String codedRefusalExternalPaymentForInvoice(String invoice, String reason) {
    return 'Rechnung $invoice: $reason';
  }

  @override
  String codedRefusalExternalPaymentNotPayable(String status) {
    return 'Nur eine freigegebene, noch nicht bezahlte Rechnung kann als bezahlt erfasst werden (diese hat den Status $status).';
  }

  @override
  String get codedRefusalExternalPaymentSegregation =>
      'Funktionstrennung: Sie haben diese Rechnung erstellt oder mitgestaltet, daher muss ein anderer Benutzer ihre Zahlung erfassen.';

  @override
  String get codedRefusalExternalPaymentCardLive =>
      'Für diese Rechnung ist eine aktive virtuelle Karte ausgestellt. Stornieren Sie die Karte, bevor Sie eine anders geleistete Zahlung erfassen.';

  @override
  String get codedRefusalExternalPaymentCreditConflict =>
      'Eine angewendete Gutschrift passt nicht mehr zum Lieferanten oder zur Währung dieser Rechnung, daher lässt sich der offene Betrag nicht bestimmen. Korrigieren Sie zuerst die Rechnung.';

  @override
  String get codedRefusalExternalPaymentNothingToPay =>
      'Diese Rechnung ist vollständig durch angewendete Gutschriften gedeckt, es gibt also nichts als bezahlt zu erfassen.';

  @override
  String codedRefusalExternalPaymentPaymentLive(String status) {
    return 'Für diese Rechnung läuft bereits eine Zahlung oder sie ist abgeschlossen ($status). Stornieren Sie sie, bevor Sie eine anders geleistete Zahlung erfassen.';
  }

  @override
  String codedRefusalExternalPaymentInRun(String run) {
    return 'Diese Rechnung ist Teil des Zahlungslaufs $run. Erfassen Sie den ganzen Lauf als außerhalb von FeohLedger bezahlt oder stornieren Sie den Lauf zuerst.';
  }

  @override
  String codedRefusalExternalPaymentAmountMismatch(String amount) {
    return 'Der Betrag muss dem offenen Betrag der Rechnung entsprechen ($amount), abzüglich angewendeter Gutschriften und eines angenommenen Skontos. Teilzahlungen können nicht erfasst werden.';
  }

  @override
  String codedRefusalExternalPaymentAmountChanged(
    String staged,
    String amount,
  ) {
    return 'Der offene Betrag dieser Rechnung hat sich seit dem Anlegen des Laufs geändert ($staged im Lauf, jetzt $amount offen), wegen einer Gutschrift oder eines Skontos. Klären Sie die Differenz mit dem Lieferanten und erfassen Sie die Rechnung dann einzeln.';
  }

  @override
  String get codedRefusalExternalPaymentErpSplit =>
      'Laut ERP wurde diese Rechnung mit mehr als einer Zahlungsart bezahlt. Erfassen Sie sie über ihre Ausnahme, die die Aufteilung der Zahlung beibehält.';

  @override
  String codedRefusalExternalPaymentErpSplitInRun(String run) {
    return 'Diese Rechnung ist Teil des Zahlungslaufs $run, und laut ERP wurde sie mit mehr als einer Zahlungsart bezahlt, was nur ihre Ausnahme erfassen kann. Stornieren Sie zuerst den Lauf und erfassen Sie die Rechnung dann über ihre Ausnahme.';
  }

  @override
  String get codedRefusalExternalPaymentPaidOnFuture =>
      'Das Zahlungsdatum darf nicht in der Zukunft liegen.';

  @override
  String get codedRefusalPaymentRunSegregation =>
      'Funktionstrennung: Wer diesen Zahlungslauf erstellt hat, darf ihn nicht auch genehmigen oder ausführen. Das muss eine andere Person tun.';

  @override
  String get codedRefusalPaymentRunSegregationRecord =>
      'Funktionstrennung: Wer diesen Zahlungslauf erstellt hat, darf ihn nicht auch als bezahlt erfassen. Das muss eine andere Person tun.';

  @override
  String get codedRefusalPaymentRunSegregationExport =>
      'Funktionstrennung: Wer diesen Zahlungslauf erstellt hat, darf nicht auch seine Bankdatei exportieren. Das muss eine andere Person tun.';

  @override
  String get codedRefusalPaymentRunAwaitingCfo =>
      'Dieser Lauf überschreitet die CFO-Freigabeschwelle der Organisation und wartet auf die Freigabe durch einen Benutzer mit der CFO-Rolle.';

  @override
  String get codedRefusalMfaCodeInvalid =>
      'Dieser Code ist ungültig. Prüfen Sie Ihre Authenticator-App und versuchen Sie es erneut.';

  @override
  String get codedRefusalMfaEmailCodeInvalid =>
      'Dieser Code ist ungültig oder abgelaufen. Fordern Sie einen neuen Code an und versuchen Sie es erneut.';

  @override
  String get codedRefusalPaymentRunNotRecordable =>
      'Nur ein Lauf im Entwurf oder einer, dessen Bankdatei exportiert wurde, kann als bezahlt erfasst werden.';

  @override
  String get codedRefusalPaymentRunNothingToRecord =>
      'Dieser Lauf hat keine Zahlungen mehr, die erfasst werden könnten.';

  @override
  String get codedRefusalExternalPaymentBlockingException =>
      'Für diese Rechnung besteht eine ungelöste Ausnahme, die die Zahlung sperrt. Lösen Sie sie, bevor Sie die Rechnung als bezahlt erfassen.';

  @override
  String get grRecordTitle => 'Wareneingang erfassen';

  @override
  String get grRecordIntro =>
      'Erfassen Sie, was zu einer Bestellung geliefert wurde. Rechnungen, die diese Bestellung angeben, werden beim Speichern sofort neu abgeglichen.';

  @override
  String get grRecordSearchPo => 'Bestellung suchen';

  @override
  String get grRecordSearchPlaceholder => 'Bestellnummer';

  @override
  String get grRecordPo => 'Bestellung';

  @override
  String get grRecordNoPos => 'Keine passenden Bestellungen';

  @override
  String get grRecordLoadPosFailed =>
      'Bestellungen konnten nicht geladen werden';

  @override
  String get grRecordLoadPoFailed =>
      'Die Bestellung konnte nicht geladen werden';

  @override
  String grRecordPoCancelled(String number) {
    return 'Bestellung $number ist storniert, daher kann dazu nichts erfasst werden.';
  }

  @override
  String get grRecordOrdered => 'Bestellt';

  @override
  String get grRecordAlreadyReceived => 'Bisher erhalten';

  @override
  String get grRecordOutstanding => 'Offen';

  @override
  String get grRecordThisDelivery => 'Diese Lieferung';

  @override
  String grRecordQuantityFor(String line) {
    return 'Erhaltene Menge für $line';
  }

  @override
  String get grRecordOverReceipt =>
      'Mehr als offen ist. Der Abgleich markiert dies als Mehrlieferung.';

  @override
  String get grRecordLinesHint =>
      'Lassen Sie eine Position leer, wenn sie nicht Teil dieser Lieferung war. Geben Sie 0 ein, wenn sie erwartet, aber nicht geliefert wurde.';

  @override
  String get grRecordNoPoLines =>
      'Diese Bestellung hat keine Positionen. Beschreiben Sie daher, was geliefert wurde.';

  @override
  String get grRecordAddLine => 'Position hinzufügen';

  @override
  String grRecordRemoveLine(int n) {
    return 'Position $n entfernen';
  }

  @override
  String grRecordLineDescription(int n) {
    return 'Beschreibung, Position $n';
  }

  @override
  String grRecordLineQuantity(int n) {
    return 'Erhaltene Menge, Position $n';
  }

  @override
  String get grRecordReceivedDate => 'Erhalten am';

  @override
  String get grRecordNumber => 'Lieferscheinnummer';

  @override
  String get grRecordNumberHint =>
      'Optional. Die Lieferschein- oder Packzettelnummer des Lieferanten.';

  @override
  String grRecordNumberPlaceholder(String po) {
    return 'Leer: GR-$po-n';
  }

  @override
  String get grRecordSaving => 'Wird erfasst…';

  @override
  String get grRecordSubmit => 'Wareneingang erfassen';

  @override
  String grRecordRecorded(String number) {
    return 'Wareneingang $number erfasst';
  }

  @override
  String get grRecordFailed => 'Wareneingang konnte nicht erfasst werden';

  @override
  String get settingsGoodsReceipt => 'Wareneingang erfassen';

  @override
  String codedRefusalGoodsReceiptPoCancelled(String poNumber) {
    return 'Bestellung $poNumber ist storniert; dazu kann nichts erfasst werden.';
  }

  @override
  String codedRefusalGoodsReceiptNumberTaken(String grNumber) {
    return 'Ein Wareneingang mit der Nummer $grNumber existiert bereits.';
  }

  @override
  String get codedRefusalGoodsReceiptLineNotOnPo =>
      'Jede erhaltene Position muss eine Position dieser Bestellung sein.';

  @override
  String get codedRefusalGoodsReceiptLineDuplicated =>
      'Eine Bestellposition darf auf einem Wareneingang nur einmal vorkommen.';

  @override
  String get codedRefusalGoodsReceiptLineRequired =>
      'Beschreiben Sie für jede Position, was geliefert wurde.';

  @override
  String get codedRefusalGoodsReceiptNothingReceived =>
      'Ein Wareneingang muss mindestens eine erhaltene Einheit erfassen.';

  @override
  String get codedRefusalGoodsReceiptIdempotencyReused =>
      'Diese Anfrage wurde bereits für eine andere Bestellung verwendet. Öffnen Sie das Formular erneut und versuchen Sie es noch einmal.';

  @override
  String get settingsGoodsReceiptHint =>
      'Eine Lieferung zu einer Bestellung erfassen';

  @override
  String get grRecordSegregationNote =>
      'Funktionstrennung: Sobald Sie diesen Wareneingang erfassen, können Sie keine Rechnung zu dieser Bestellung freigeben, und ein Wareneingang, den Sie nach einer Freigabe erfassen, gibt diese Rechnung nicht frei.';

  @override
  String get grRecordReceiveLines =>
      'Geben Sie für mindestens eine Position eine Menge ein';
}
