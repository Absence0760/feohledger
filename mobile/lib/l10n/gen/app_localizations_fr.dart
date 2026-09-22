// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for French (`fr`).
class AppLocalizationsFr extends AppLocalizations {
  AppLocalizationsFr([String locale = 'fr']) : super(locale);

  @override
  String get navDashboard => 'Tableau de bord';

  @override
  String get navInvoices => 'Factures';

  @override
  String get navContracts => 'Contrats';

  @override
  String get navApprovals => 'Approbations';

  @override
  String get navExceptions => 'Exceptions';

  @override
  String get navVendors => 'Fournisseurs';

  @override
  String get navPay => 'Payer';

  @override
  String get navPayments => 'Paiements';

  @override
  String get navSettings => 'Paramètres';

  @override
  String get shellAppName => 'FeohLedger';

  @override
  String get commonSave => 'Enregistrer';

  @override
  String get commonSaving => 'Enregistrement…';

  @override
  String get commonCancel => 'Annuler';

  @override
  String get commonLoading => 'Chargement…';

  @override
  String get commonRetry => 'Réessayer';

  @override
  String get commonAll => 'Toutes';

  @override
  String get commonSearch => 'Rechercher';

  @override
  String get commonClear => 'Effacer';

  @override
  String get commonApply => 'Appliquer';

  @override
  String get commonClose => 'Fermer';

  @override
  String get settingsTitle => 'Paramètres';

  @override
  String get settingsTenant => 'Locataire';

  @override
  String get settingsTenantNotSet => 'Non défini';

  @override
  String get settingsApiServer => 'Serveur API';

  @override
  String get settingsBiometricUnlock => 'Déverrouillage biométrique';

  @override
  String get settingsBiometricHint =>
      'Utiliser l’empreinte ou le visage pour déverrouiller';

  @override
  String get settingsSignOut => 'Se déconnecter';

  @override
  String get settingsLanguage => 'Langue';

  @override
  String get settingsLanguageHint =>
      'Choisissez la langue utilisée dans toute l’application. Votre choix est enregistré sur cet appareil.';

  @override
  String get settingsLanguageSystem => 'Paramètre du système';

  @override
  String get dashboardTitle => 'Tableau de bord';

  @override
  String get dashboardTotalInvoices => 'Total des factures';

  @override
  String get dashboardUpcoming => 'À venir';

  @override
  String get dashboardForReview => 'À examiner';

  @override
  String get dashboardApproved => 'Approuvées';

  @override
  String get dashboardAging => 'Ancienneté des factures';

  @override
  String get dashboardTopVendors => 'Principaux fournisseurs';

  @override
  String get dashboardAgingCurrent => 'À jour';

  @override
  String get dashboardAgingDays30 => '30 jours';

  @override
  String get dashboardAgingDays60 => '60 jours';

  @override
  String get dashboardAgingDays90plus => '90 et +';

  @override
  String get dashboardCachedBanner => 'Données en cache — serveur injoignable';

  @override
  String dashboardErrorPrefix(String error) {
    return 'Erreur : $error';
  }

  @override
  String dashboardInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count factures',
      one: '$count facture',
    );
    return '$_temp0';
  }

  @override
  String get partialConversionCurrencyFallback => 'la devise de reporting';

  @override
  String dashboardKpiUnconverted(String label, int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count factures',
      one: '$count facture',
    );
    return '$label : partiel — $_temp0 sans taux de change vers $currency, comptabilisées à leur valeur faciale ; le total mélange donc les devises d’autant.';
  }

  @override
  String dashboardAgingUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count factures',
      one: '$count facture',
    );
    return 'Partiel : $_temp0 sans taux de change vers $currency, comptabilisées à leur valeur faciale — les tranches ci-dessous mélangent les devises d’autant.';
  }

  @override
  String dashboardTopVendorsUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count factures',
      one: '$count facture',
    );
    return 'Partiel : $_temp0 sans taux de change vers $currency, comptabilisées à leur valeur faciale — ces fournisseurs ne sont donc pas tous classés dans la même devise. Enregistrez le taux manquant avant de vous fier au classement.';
  }

  @override
  String dashboardVendorFaceValueCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count à valeur faciale',
      one: '$count à valeur faciale',
    );
    return '$_temp0';
  }

  @override
  String get invoicesTitle => 'Factures';

  @override
  String get invoicesSearchHint => 'Rechercher des factures…';

  @override
  String get invoicesSearchAria => 'Rechercher des factures';

  @override
  String get invoicesAdvancedSearch => 'Recherche avancée';

  @override
  String get invoicesAdvancedSearchActive =>
      'Recherche avancée, filtres actifs';

  @override
  String get invoicesCaptureInvoice => 'Capturer une facture';

  @override
  String get invoicesCaptureInvoiceLabel => 'Capturer une facture';

  @override
  String get invoicesEmpty => 'Aucune facture trouvée';

  @override
  String get invoicesFilterAll => 'Toutes';

  @override
  String get invoicesFilterNew => 'Nouvelles';

  @override
  String get invoicesFilterPending => 'En attente';

  @override
  String get invoicesFilterReview => 'Examen';

  @override
  String get invoicesFilterApproved => 'Approuvées';

  @override
  String get invoicesFilterRejected => 'Rejetées';

  @override
  String get invoicesFilterPaid => 'Payées';

  @override
  String get invoicesColInvoiceNumber => 'N° de facture';

  @override
  String get invoicesColVendor => 'Fournisseur';

  @override
  String get invoicesColAmount => 'Montant';

  @override
  String get invoicesColDueDate => 'Échéance';

  @override
  String get invoicesColStatus => 'Statut';

  @override
  String get notificationsTitle => 'Notifications';

  @override
  String get notificationsMarkAllRead => 'Tout marquer comme lu';

  @override
  String get notificationsMarkAllReadLabel =>
      'Marquer toutes les notifications comme lues';

  @override
  String get notificationsFilterUnread => 'Non lues';

  @override
  String get notificationsAllMarkedRead =>
      'Toutes les notifications marquées comme lues';

  @override
  String get notificationsCouldNotMarkAll =>
      'Impossible de tout marquer comme lu';

  @override
  String get notificationsEmptyUnread => 'Aucune notification non lue';

  @override
  String get notificationsEmpty => 'Aucune notification';

  @override
  String get notificationsCaughtUp => 'Vous êtes à jour';

  @override
  String get notificationsNothingYet => 'Rien pour le moment';

  @override
  String get notificationsLoadError =>
      'Impossible de charger les notifications';

  @override
  String get vendorsTitle => 'Fournisseurs';

  @override
  String get vendorsSyncErp => 'Synchroniser depuis l\'ERP';

  @override
  String get vendorsSyncErpLabel =>
      'Synchroniser les fournisseurs depuis l\'ERP';

  @override
  String get vendorsSearchHint => 'Rechercher des fournisseurs…';

  @override
  String get vendorsFilterUnverified => 'Non vérifiés';

  @override
  String get vendorsFilterActive => 'Actifs';

  @override
  String get vendorsFilterInactive => 'Inactifs';

  @override
  String get vendorsFilterRejected => 'Rejetés';

  @override
  String get vendorsEmpty => 'Aucun fournisseur trouvé';

  @override
  String get vendorsLoadError => 'Impossible de charger les fournisseurs';

  @override
  String get vendorActionVerify => 'Vérifier';

  @override
  String get vendorActionReject => 'Rejeter';

  @override
  String get vendorUnverifiedLabel => 'Fournisseur non vérifié';

  @override
  String get vendorVerifyHint => 'Rendre éligible au paiement';

  @override
  String get vendorRejectHint => 'Marquer comme non valide / en double';

  @override
  String get vendorVerified => 'Fournisseur vérifié';

  @override
  String get vendorRejected => 'Fournisseur rejeté';

  @override
  String get vendorActionFailed => 'Échec de l\'action';

  @override
  String vendorSyncFailed(String error) {
    return 'Échec de la synchronisation ERP : $error';
  }

  @override
  String get exceptionsTitle => 'Exceptions';

  @override
  String get exceptionsFilterOpen => 'Ouvertes';

  @override
  String get exceptionsFilterEscalated => 'Escaladées';

  @override
  String get exceptionsFilterResolved => 'Résolues';

  @override
  String get exceptionsFilterDismissed => 'Ignorées';

  @override
  String get exceptionsEmpty => 'Aucune exception';

  @override
  String get exceptionsQueueClear =>
      'La file d\'attente des exceptions est vide';

  @override
  String get exceptionActionResolve => 'Résoudre';

  @override
  String get exceptionActionEscalate => 'Escalader';

  @override
  String get exceptionActionDismiss => 'Ignorer';

  @override
  String get exceptionResolved => 'Exception résolue';

  @override
  String get exceptionEscalated => 'Exception escaladée';

  @override
  String get exceptionDismissed => 'Exception ignorée';

  @override
  String get exceptionActionFailed => 'Échec de l\'action';

  @override
  String get paymentsTitle => 'Paiements';

  @override
  String get paymentsEmpty => 'Aucun paiement';

  @override
  String paymentsErrorPrefix(String error) {
    return 'Erreur : $error';
  }

  @override
  String get paymentStatusPending => 'En attente';

  @override
  String get paymentStatusProcessing => 'En cours';

  @override
  String get paymentStatusCompleted => 'Terminé';

  @override
  String get paymentStatusFailed => 'Échoué';

  @override
  String get paymentStatusCancelled => 'Annulé';

  @override
  String get approvalsTitle => 'Approbations en attente';

  @override
  String get approvalsAllCaughtUp => 'Tout est à jour !';

  @override
  String get approvalsNoneWaiting => 'Aucune facture en attente d\'approbation';

  @override
  String get approvalsLoadError =>
      'Impossible de charger les approbations en attente';

  @override
  String approvalsPendingCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count factures en attente',
      one: '$count facture en attente',
    );
    return '$_temp0';
  }

  @override
  String get approvalActionApprove => 'Approuver';

  @override
  String get approvalActionReject => 'Rejeter';

  @override
  String get approvalApproved => 'Facture approuvée';

  @override
  String get captureTitle => 'Capturer une facture';

  @override
  String get captureChange => 'Modifier';

  @override
  String get captureUpload => 'Téléverser';

  @override
  String get captureUploading => 'Téléversement…';

  @override
  String get captureEmptyPrompt =>
      'Prenez une photo, choisissez dans la galerie ou sélectionnez un fichier';

  @override
  String get captureCamera => 'Appareil photo';

  @override
  String get captureGallery => 'Galerie';

  @override
  String get captureChooseFile => 'Choisir un fichier';

  @override
  String get captureSupportedFormats => 'Prend en charge PDF, PNG, JPG et TIFF';

  @override
  String get captureUploadSuccess => 'Facture téléversée avec succès';

  @override
  String captureUploadFailedStatus(int status, String message) {
    return 'Échec du téléversement ($status) : $message';
  }

  @override
  String captureUploadFailed(String error) {
    return 'Échec du téléversement : $error';
  }

  @override
  String captureSelectedDocument(String name) {
    return 'Document sélectionné : $name';
  }

  @override
  String get capturePdfReady => 'Document PDF prêt à être téléversé';

  @override
  String get advSearchTitle => 'Recherche avancée';

  @override
  String get advSearchClose => 'Fermer la recherche avancée';

  @override
  String get advSearchVendor => 'Fournisseur';

  @override
  String get advSearchPoNumber => 'N° de commande';

  @override
  String get advSearchMinAmount => 'Montant min.';

  @override
  String get advSearchMaxAmount => 'Montant max.';

  @override
  String get advSearchDueFrom => 'Échéance à partir du';

  @override
  String get advSearchDueTo => 'Échéance jusqu\'au';

  @override
  String get advSearchAny => 'Toute';

  @override
  String get advSearchInvalidAmount =>
      'Saisissez un montant valide (par ex. 1000)';

  @override
  String get advSearchMinMaxError =>
      'Le minimum ne doit pas dépasser le maximum';

  @override
  String advSearchClearField(String label) {
    return 'Effacer $label';
  }

  @override
  String advSearchDateFieldHint(String label, String value) {
    return '$label, actuellement $value. Appuyez deux fois pour modifier.';
  }

  @override
  String get invoiceDetailTitle => 'Détail de la facture';

  @override
  String get invoiceDetailEdit => 'Modifier';

  @override
  String get invoiceDetailEditLabel => 'Modifier la facture';

  @override
  String get invoiceDetailRetry => 'Réessayer';

  @override
  String invoiceDetailErrorPrefix(String error) {
    return 'Erreur : $error';
  }

  @override
  String get invoiceDetailNoChanges => 'Aucune modification à enregistrer';

  @override
  String get invoiceDetailUpdated => 'Facture mise à jour';

  @override
  String get invoiceDetailUpdateFailed =>
      'Impossible d\'enregistrer les modifications — veuillez réessayer';

  @override
  String get invoiceDetailApproved => 'Facture approuvée';

  @override
  String get invoiceDetailApproveFailed =>
      'Impossible d\'approuver la facture — veuillez réessayer';

  @override
  String get invoiceDetailRejected => 'Facture rejetée';

  @override
  String get invoiceDetailRejectFailed =>
      'Impossible de rejeter la facture — veuillez réessayer';

  @override
  String get invoiceDetailRejectTitle => 'Rejeter la facture';

  @override
  String get invoiceDetailRejectReason => 'Motif';

  @override
  String get invoiceDetailReject => 'Rejeter';

  @override
  String get invoiceDetailApprove => 'Approuver';

  @override
  String get invoiceDetailUnknownVendor => 'Fournisseur inconnu';

  @override
  String get invoiceDetailFieldInvoiceNumber => 'N° de facture';

  @override
  String get invoiceDetailFieldPoNumber => 'N° de commande';

  @override
  String get invoiceDetailFieldCurrency => 'Devise';

  @override
  String get invoiceDetailFieldInvoiceDate => 'Date de facture';

  @override
  String get invoiceDetailFieldDueDate => 'Date d\'échéance';

  @override
  String get invoiceDetailFieldDescription => 'Description';

  @override
  String get invoiceDetailFieldGlAccount => 'Compte comptable';

  @override
  String get invoiceDetailFieldCreated => 'Créée';

  @override
  String get invoiceDetailActivity => 'Activité';

  @override
  String get invoiceDetailActivityError => 'Impossible de charger l\'activité';

  @override
  String get invoiceDetailFilePdfLabel =>
      'PDF de la facture. Appuyez deux fois pour afficher en plein écran.';

  @override
  String get invoiceDetailFileLabel =>
      'Fichier de la facture. Appuyez deux fois pour afficher en plein écran.';

  @override
  String get invoiceDetailTapToViewPdf => 'Appuyez pour afficher le PDF';

  @override
  String get invoiceDetailTapToViewFile => 'Appuyez pour afficher le fichier';

  @override
  String get invoiceEditTitle => 'Modifier la facture';

  @override
  String get invoiceEditClose => 'Fermer le formulaire de modification';

  @override
  String get invoiceEditVendor => 'Fournisseur';

  @override
  String get invoiceEditInvoiceNumber => 'N° de facture';

  @override
  String get invoiceEditAmount => 'Montant';

  @override
  String get invoiceEditPoNumber => 'N° de commande';

  @override
  String get invoiceEditGlAccount => 'Compte comptable';

  @override
  String get invoiceEditDescription => 'Description';

  @override
  String get invoiceEditDueDate => 'Date d\'échéance';

  @override
  String get invoiceEditNotSet => 'Non défini';

  @override
  String get invoiceEditInvalidAmount =>
      'Saisissez un montant valide (par ex. 1234,56)';

  @override
  String get invoiceEditClearDueDate => 'Effacer la date d\'échéance';

  @override
  String get invoiceEditLockedNotice =>
      'Approuvée — le bénéficiaire et le montant sont figés. Pour les modifier, rejetez la facture, corrigez-la puis approuvez-la de nouveau.';

  @override
  String get invoiceEditLockedHelper => 'Figé après approbation';

  @override
  String invoiceEditDueDateHint(String value) {
    return 'Date d\'échéance, actuellement $value. Appuyez deux fois pour modifier.';
  }

  @override
  String get warningsSectionTitle => 'Avertissements et alertes de fraude';

  @override
  String get warningsPoMatchTitle => 'Rapprochement de commande';

  @override
  String get warningsSeverityError => 'Erreur';

  @override
  String get warningsSeverityWarning => 'Avertissement';

  @override
  String get warningsSeverityInfo => 'Info';

  @override
  String get warningsPoLabel => 'Commande';

  @override
  String warningsMatchLabel(String type) {
    return 'Rapprochement $type';
  }

  @override
  String warningsVarianceLabel(String value) {
    return '$value % d\'écart';
  }

  @override
  String get erpStatusTitle => 'Statut ERP';

  @override
  String get erpStatusReference => 'Référence ERP';

  @override
  String get erpStatusDocumentId => 'ID du document';

  @override
  String get erpStatusError => 'Erreur';

  @override
  String get erpStatusLastUpdate => 'Dernière mise à jour';

  @override
  String get erpStatusStatus => 'Statut';

  @override
  String get fileViewerPdfTitle => 'PDF de la facture';

  @override
  String get fileViewerImageTitle => 'Image de la facture';

  @override
  String get fileViewerPdfError => 'Impossible de charger le PDF';

  @override
  String get fileViewerImageError => 'Impossible de charger l\'image';

  @override
  String get fileViewerRetry => 'Réessayer';

  @override
  String get timelineNoActivity => 'Aucune activité pour l\'instant';

  @override
  String get payTitle => 'Payer';

  @override
  String get payTabQueue => 'File d\'attente';

  @override
  String get payTabRuns => 'Lots';

  @override
  String get paySummaryTotalPaid => 'Total payé';

  @override
  String get paySummaryPending => 'En attente';

  @override
  String get paySummaryInQueue => 'En file';

  @override
  String get paySummaryCardRebates => 'Remises sur carte';

  @override
  String paySummaryPaymentsSubtitle(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count paiements',
      one: '$count paiement',
    );
    return '$_temp0';
  }

  @override
  String get payQueueEmpty => 'Aucune facture en attente de paiement';

  @override
  String get payQueueError => 'Impossible de charger la file de paiement';

  @override
  String get payQueueRetry => 'Réessayer';

  @override
  String payQueueDue(String date) {
    return 'Échéance $date';
  }

  @override
  String get payQueueNoDueDate => 'Aucune date d\'échéance';

  @override
  String payQueueDiscount(String amount) {
    return 'remise $amount';
  }

  @override
  String get payQueueOverdue => 'en retard';

  @override
  String get payQueueSelected => 'sélectionnée';

  @override
  String get payQueueBlockedDuplicate => 'Doublon possible — non résolu';

  @override
  String get payQueueBlockedFraudFlag => 'Signalement de fraude — non résolu';

  @override
  String get payQueueBlockedLineTotalMismatch =>
      'Les totaux des lignes ne concordent pas — non résolu';

  @override
  String get payQueueBlockedPaymentReconciliation =>
      'Paiement antérieur non rapproché — peut-être encore en cours';

  @override
  String get payQueueBlockedFullyCredited =>
      'Entièrement couverte par des avoirs — rien à payer';

  @override
  String get payQueueBlockedLiveVirtualCard =>
      'Une carte virtuelle active couvre cette facture — payez-la par carte';

  @override
  String get payQueueBlockedGeneric =>
      'Une exception non résolue bloque le paiement';

  @override
  String payQueueBlockedAnnounce(String reason) {
    return 'ne peut pas être payée : $reason';
  }

  @override
  String payQueuePinnedMethod(String method) {
    return 'Payer par $method';
  }

  @override
  String payMethodLabel(String invoiceNumber) {
    return 'Mode de paiement pour $invoiceNumber';
  }

  @override
  String get payMethodAch => 'ACH';

  @override
  String get payMethodWire => 'Virement';

  @override
  String get payMethodCheck => 'Chèque';

  @override
  String get payMethodVirtualCard => 'Carte virtuelle';

  @override
  String paySelectedCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count factures sélectionnées',
      one: '$count facture sélectionnée',
    );
    return '$_temp0';
  }

  @override
  String get payClear => 'Effacer';

  @override
  String get payCreateRun => 'Créer un lot';

  @override
  String payCreateRunFailed(String error) {
    return 'Échec de la création du lot : $error';
  }

  @override
  String get payRunsEmpty => 'Aucun lot de paiement';

  @override
  String payRunSubtitle(int count, String date) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count paiements',
      one: '$count paiement',
    );
    return '$_temp0 • $date';
  }

  @override
  String get payRunCfoRequiredSuffix =>
      ' • Approbation du directeur financier requise';

  @override
  String payRunAnnounce(String amount, String status, String subtitle) {
    return 'Lot $amount, $status, $subtitle';
  }

  @override
  String get payRunActions => 'Actions du lot';

  @override
  String get payRunActionExecute => 'Exécuter';

  @override
  String get payRunActionCancel => 'Annuler';

  @override
  String get payRunActionApprove => 'Approuver en tant que directeur financier';

  @override
  String get payRunApproveTitle => 'Approuver le lot de paiement ?';

  @override
  String payRunApproveBody(String date, int count, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count paiements',
      one: '$count paiement',
    );
    return 'Validation du lot créé le $date — $_temp0 pour un total de $amount. Cela autorise l’exécution ; aucun fonds n’est déplacé.';
  }

  @override
  String get payRunApproveConfirm => 'Approuver';

  @override
  String payRunApproveFailed(String error) {
    return 'Échec de l’approbation : $error';
  }

  @override
  String get payRunCfoBlocked =>
      'Ce lot nécessite l\'approbation du directeur financier avant de pouvoir être exécuté.';

  @override
  String get payRunExecuteTitle => 'Exécuter le lot de paiement ?';

  @override
  String payRunExecuteBody(String amount) {
    return 'Cette action envoie $amount via le processeur de paiement configuré.';
  }

  @override
  String payRunExecuteFailed(String error) {
    return 'Échec de l\'exécution : $error';
  }

  @override
  String payRunCancelFailed(String error) {
    return 'Échec de l\'annulation : $error';
  }

  @override
  String get payRunStatusDraft => 'Brouillon';

  @override
  String get payRunStatusCompleted => 'Terminé';

  @override
  String get payRunStatusSubmitted => 'Soumis';

  @override
  String get payRunStatusPartial => 'Partiel';

  @override
  String get payRunStatusFailed => 'Échoué';

  @override
  String get payRunStatusCancelled => 'Annulé';

  @override
  String get payConfirmCancel => 'Annuler';

  @override
  String get payConfirmExecute => 'Exécuter';

  @override
  String get loginAppName => 'FeohLedger';

  @override
  String get loginTagline => 'La comptabilité fournisseurs, simplifiée';

  @override
  String get loginTenant => 'Locataire';

  @override
  String get loginEmail => 'E-mail';

  @override
  String get loginPassword => 'Mot de passe';

  @override
  String get loginShowPassword => 'Afficher le mot de passe';

  @override
  String get loginHidePassword => 'Masquer le mot de passe';

  @override
  String get loginRequired => 'Obligatoire';

  @override
  String get loginSignIn => 'Se connecter';

  @override
  String get mfaTitle => 'Authentification à deux facteurs';

  @override
  String get mfaHeading => 'Vérifiez votre identité';

  @override
  String get mfaPromptEmail =>
      'Saisissez le code à 6 chiffres que nous vous avons envoyé par e-mail.';

  @override
  String get mfaPromptTotp =>
      'Saisissez le code à 6 chiffres de votre application d\'authentification.';

  @override
  String get mfaEnforcedNotice =>
      'Votre organisation exige une authentification à deux facteurs. Vérifiez maintenant avec un code par e-mail, puis terminez la configuration d\'une application d\'authentification dans l\'application web.';

  @override
  String get mfaCode => 'Code';

  @override
  String get mfaCodeRequired => 'Obligatoire';

  @override
  String get mfaCodeTooShort => 'Saisissez au moins 6 chiffres';

  @override
  String get mfaVerify => 'Vérifier';

  @override
  String get mfaSending => 'Envoi…';

  @override
  String get mfaResendEmailCode => 'Renvoyer le code par e-mail';

  @override
  String get mfaSendEmailCode => 'Envoyer le code par e-mail';

  @override
  String get mfaUseEmailInstead => 'Utiliser plutôt un code par e-mail';

  @override
  String get mfaUseAuthenticatorInstead =>
      'Utiliser plutôt l\'application d\'authentification';

  @override
  String get mfaEmailedAnnounce =>
      'Un code de connexion vous a été envoyé par e-mail.';

  @override
  String get adminUsersTitle => 'Gestion des utilisateurs';

  @override
  String get adminUsersSearchHint => 'Rechercher par nom ou e-mail';

  @override
  String get adminUsersEmpty => 'Aucun utilisateur trouvé';

  @override
  String get adminUsersLoadError => 'Impossible de charger les utilisateurs';

  @override
  String get adminUsersEditRoles => 'Modifier les rôles';

  @override
  String get adminUsersNoRoles => 'Aucun rôle';

  @override
  String get adminUsersDeactivate => 'Désactiver l\'utilisateur';

  @override
  String get adminUsersActivate => 'Activer l\'utilisateur';

  @override
  String get adminUsersCannotDeactivateSelf =>
      'Vous ne pouvez pas désactiver votre propre compte';

  @override
  String get adminUsersDeactivateHint =>
      'Les déconnecte et bloque la connexion';

  @override
  String get adminUsersActivateHint => 'Restaure l\'accès à la connexion';

  @override
  String get adminUsersRoleActive => 'actif';

  @override
  String get adminUsersRoleInactive => 'inactif';

  @override
  String get adminUsersInactiveBadge => 'Inactif';

  @override
  String adminUsersRolesUpdated(String name) {
    return 'Rôles mis à jour pour $name';
  }

  @override
  String adminUsersRolesUpdateFailed(String error) {
    return 'Échec de la mise à jour des rôles : $error';
  }

  @override
  String adminUsersActivated(String name) {
    return '$name activé';
  }

  @override
  String adminUsersDeactivated(String name) {
    return '$name désactivé';
  }

  @override
  String adminUsersUpdateFailed(String name, String error) {
    return 'Échec de la mise à jour de $name : $error';
  }

  @override
  String get adminUsersCreateUser => 'Créer un utilisateur';

  @override
  String get adminUsersCreateTitle => 'Nouvel utilisateur';

  @override
  String get adminUsersFieldFullName => 'Nom complet';

  @override
  String get adminUsersFieldEmail => 'E-mail';

  @override
  String get adminUsersFieldRoles => 'Rôles';

  @override
  String get adminUsersValidationNameRequired => 'Le nom complet est requis';

  @override
  String get adminUsersValidationEmailRequired => 'L\'e-mail est requis';

  @override
  String get adminUsersValidationEmailInvalid =>
      'Saisissez une adresse e-mail valide';

  @override
  String get adminUsersCreateSubmit => 'Créer';

  @override
  String get adminUsersCreating => 'Création…';

  @override
  String adminUsersCreated(String name) {
    return '$name créé';
  }

  @override
  String adminUsersCreateFailed(String error) {
    return 'Échec de la création de l\'utilisateur : $error';
  }

  @override
  String get adminUsersTempPasswordTitle => 'Utilisateur créé';

  @override
  String adminUsersTempPasswordBody(String name) {
    return 'Communiquez ce mot de passe à usage unique à $name. Il devra le changer à la première connexion. Il ne sera plus affiché.';
  }

  @override
  String get adminUsersDelete => 'Supprimer l\'utilisateur';

  @override
  String get adminUsersDeleteHint => 'Supprime définitivement ce compte';

  @override
  String get adminUsersCannotDeleteSelf =>
      'Vous ne pouvez pas supprimer votre propre compte';

  @override
  String adminUsersDeleteConfirmTitle(String name) {
    return 'Supprimer $name ?';
  }

  @override
  String adminUsersDeleteConfirmBody(String name, String email) {
    return 'Cette action supprime définitivement $name ($email). Elle est irréversible.';
  }

  @override
  String adminUsersDeleted(String name) {
    return '$name supprimé';
  }

  @override
  String adminUsersDeleteFailed(String name, String error) {
    return 'Échec de la suppression de $name : $error';
  }

  @override
  String get orgSettingsTitle => 'Paramètres de l\'organisation';

  @override
  String get orgSettingsNoSettings => 'Aucun paramètre';

  @override
  String get orgSettingsLoadError => 'Impossible de charger les paramètres';

  @override
  String get orgSettingsSectionCompany => 'Entreprise';

  @override
  String get orgSettingsSectionInvoiceDefaults =>
      'Valeurs par défaut des factures';

  @override
  String get orgSettingsName => 'Nom de l\'organisation';

  @override
  String get orgSettingsAddress => 'Adresse';

  @override
  String get orgSettingsPhone => 'Téléphone';

  @override
  String get orgSettingsWebsite => 'Site web';

  @override
  String get orgSettingsTaxId => 'Numéro fiscal';

  @override
  String get orgSettingsCurrency => 'Devise par défaut';

  @override
  String get orgSettingsPaymentTerms => 'Conditions de paiement';

  @override
  String get orgSettingsNumberPrefix => 'Préfixe de numéro de facture';

  @override
  String get orgSettingsGlAccount => 'Compte général par défaut';

  @override
  String get orgSettingsCostCenter => 'Centre de coûts par défaut';

  @override
  String get orgSettingsSave => 'Enregistrer les modifications';

  @override
  String get orgSettingsSaving => 'Enregistrement…';

  @override
  String orgSettingsFieldRequired(String label) {
    return '$label est obligatoire';
  }

  @override
  String get orgSettingsSaved => 'Paramètres de l\'organisation enregistrés';

  @override
  String orgSettingsSaveFailed(String error) {
    return 'Échec de l\'enregistrement : $error';
  }

  @override
  String get workflowsTitle => 'Flux de travail';

  @override
  String get workflowsEmpty => 'Aucun flux de travail trouvé';

  @override
  String get workflowsLoadError => 'Impossible de charger les flux de travail';

  @override
  String get workflowsStatusActive => 'Actif';

  @override
  String get workflowsStatusInactive => 'Inactif';

  @override
  String get workflowsDefault => 'Par défaut';

  @override
  String workflowsStepCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count étapes',
      one: '$count étape',
    );
    return '$_temp0';
  }

  @override
  String get workflowDetailFallbackTitle => 'Flux de travail';

  @override
  String get workflowDetailLoadError =>
      'Impossible de charger le flux de travail';

  @override
  String get workflowDetailNoSteps => 'Ce flux de travail n\'a aucune étape.';

  @override
  String get workflowDetailDefaultWorkflow => 'Flux de travail par défaut';

  @override
  String workflowDetailStepNumber(int number) {
    return 'Étape $number';
  }

  @override
  String get workflowDetailStepEnabled => 'Activé';

  @override
  String get workflowDetailStepDisabled => 'Désactivé';

  @override
  String workflowDetailApproverCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count approbateurs',
      one: '$count approbateur',
    );
    return '$_temp0';
  }

  @override
  String workflowDetailDelaySummary(String hours) {
    return 'Délai $hours h';
  }

  @override
  String workflowDetailConditionSummary(String field) {
    return 'Sur $field';
  }

  @override
  String get cashFlowTitle => 'Prévision de trésorerie';

  @override
  String cashFlowErrorPrefix(String error) {
    return 'Erreur : $error';
  }

  @override
  String cashFlowHorizonDays(int days) {
    return '$days jours';
  }

  @override
  String get cashFlowLowBalanceAlert => 'Alerte de solde faible';

  @override
  String cashFlowBreachSingle(
    String threshold,
    String period,
    String shortfall,
  ) {
    return 'Risque de passer sous le solde $threshold en $period (déficit $shortfall).';
  }

  @override
  String cashFlowBreachMultiple(int count, String period, String shortfall) {
    return '$count périodes risquent de passer sous le solde minimum. Pire : $period, déficit $shortfall.';
  }

  @override
  String get cashFlowMinimum => 'minimum';

  @override
  String get cashFlowOpeningBalance => 'Solde d’ouverture';

  @override
  String get cashFlowProjectedEnd => 'Fin prévue';

  @override
  String cashFlowProjectedEndSubtitle(int days) {
    return 'dans $days jours';
  }

  @override
  String get cashFlowCommittedOut => 'Sorties engagées';

  @override
  String get cashFlowCommittedSubtitle => 'engagements fermes';

  @override
  String get cashFlowPendingOut => 'Sorties en attente';

  @override
  String get cashFlowPendingSubtitle => 'pipeline en cours';

  @override
  String get cashFlowOpeningSourceProvider => 'synchronisé depuis la banque';

  @override
  String get cashFlowOpeningSourceSettings => 'solde enregistré';

  @override
  String get cashFlowOpeningSourceQuery => 'manuel';

  @override
  String get cashFlowOpeningSourceUnset => 'définir un solde';

  @override
  String get cashFlowProjectedOutflows => 'Sorties prévues';

  @override
  String get cashFlowNoOutflows => 'Aucune sortie prévue sur cet horizon.';

  @override
  String cashFlowInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count factures',
      one: '$count facture',
    );
    return '$_temp0';
  }

  @override
  String cashFlowCommittedAmount(String amount) {
    return 'engagé $amount';
  }

  @override
  String cashFlowPendingAmount(String amount) {
    return 'en attente $amount';
  }

  @override
  String get cashFlowPosition => 'Position de trésorerie';

  @override
  String get cashFlowNoPosition =>
      'Aucune prévision de trésorerie pour cet horizon.';

  @override
  String cashFlowOutAmount(String amount) {
    return 'sortie $amount';
  }

  @override
  String cashFlowForecastRowLabel(
    String period,
    String scheduled,
    String committed,
    String pending,
    int count,
  ) {
    return '$period : prévu $scheduled, engagé $committed, en attente $pending, $count factures';
  }

  @override
  String cashFlowPositionRowLabel(
    String period,
    String opening,
    String outflow,
    String closing,
  ) {
    return '$period : ouverture $opening, sortie $outflow, clôture $closing';
  }

  @override
  String get cashFlowBelowThresholdSuffix => ', sous le seuil';

  @override
  String cashFlowLowBalanceAlertLabel(String message) {
    return 'Alerte de solde faible. $message';
  }

  @override
  String cashFlowForecastUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count factures',
      one: '$count facture',
    );
    return 'Partiel : $_temp0 sans taux de change vers $currency, comptabilisées à leur valeur faciale — ces sorties, ainsi que les totaux engagés et en attente ci-dessus, mélangent les devises d’autant.';
  }

  @override
  String cashFlowPositionUnconverted(int count, String currency) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count factures',
      one: '$count facture',
    );
    return 'Non converti : $_temp0 sans taux de change vers $currency, comptabilisées à leur valeur faciale — le solde courant ci-dessous et la fin prévue ci-dessus mélangent donc des devises. Enregistrez le taux manquant avant d’agir sur ces chiffres.';
  }

  @override
  String get contractsTitle => 'Contrats';

  @override
  String get contractsSearchHint => 'Rechercher des contrats…';

  @override
  String get contractsEmpty => 'Aucun contrat trouvé';

  @override
  String get contractsFilterDraft => 'Brouillon';

  @override
  String get contractsFilterActive => 'Actif';

  @override
  String get contractsFilterExpired => 'Expiré';

  @override
  String get contractsFilterTerminated => 'Résilié';

  @override
  String get contractsFilterCancelled => 'Annulé';

  @override
  String get contractDetailTitle => 'Détail du contrat';

  @override
  String contractDetailErrorPrefix(String error) {
    return 'Erreur : $error';
  }

  @override
  String get contractDetailUntitled => 'Contrat sans titre';

  @override
  String get contractDetailFieldContractNumber => 'N° de contrat';

  @override
  String get contractDetailFieldVendor => 'Fournisseur';

  @override
  String get contractDetailFieldType => 'Type';

  @override
  String get contractDetailFieldCurrency => 'Devise';

  @override
  String get contractDetailFieldSpendLimit => 'Plafond de dépenses';

  @override
  String get contractDetailNotToExceed => ' (à ne pas dépasser)';

  @override
  String get contractDetailFieldStartDate => 'Date de début';

  @override
  String get contractDetailFieldEndDate => 'Date de fin';

  @override
  String get contractDetailFieldSigned => 'Signé';

  @override
  String get contractDetailFieldAutoRenew => 'Renouvellement auto.';

  @override
  String get contractDetailYes => 'Oui';

  @override
  String get contractDetailNo => 'Non';

  @override
  String get contractDetailFieldRenewalTerm => 'Durée de renouvellement';

  @override
  String contractDetailRenewalTermMonths(int months) {
    return '$months mois';
  }

  @override
  String get contractDetailFieldRenewalNotice => 'Préavis de renouvellement';

  @override
  String contractDetailRenewalNoticeDays(int days) {
    return '$days jours';
  }

  @override
  String get contractDetailFieldPaymentTerms => 'Conditions de paiement';

  @override
  String get contractDetailFieldDescription => 'Description';

  @override
  String get contractDetailFieldCreated => 'Créé';

  @override
  String get contractDetailSectionSpend => 'Dépenses';

  @override
  String get contractDetailSectionLineItems => 'Lignes';

  @override
  String get contractDetailSpendInvoiced => 'Facturé';

  @override
  String contractDetailSpendInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count factures',
      one: '$count facture',
    );
    return '$_temp0';
  }

  @override
  String get contractDetailSpendOverLimit => 'Dépassé';

  @override
  String get contractDetailSpendRemaining => 'Restant';

  @override
  String contractDetailSpendOfLimit(String limit) {
    return 'sur $limit';
  }

  @override
  String get contractDetailSpendNoLimit => 'aucun plafond défini';

  @override
  String get contractDetailLineItemFallback => 'Ligne';

  @override
  String contractDetailLineQty(String quantity) {
    return 'Qté $quantity';
  }

  @override
  String contractDetailLineUnitPrice(String price) {
    return '@ $price';
  }

  @override
  String contractDetailLineGl(String account) {
    return 'Compte $account';
  }

  @override
  String get contractActivate => 'Activer';

  @override
  String get contractActivated => 'Contrat activé';

  @override
  String get contractActivateFailed =>
      'Impossible d’activer le contrat — veuillez réessayer';

  @override
  String get contractTerminate => 'Résilier';

  @override
  String get contractTerminateTitle => 'Résilier le contrat';

  @override
  String get contractTerminateBody =>
      'Cela met fin au contrat de façon anticipée. Cette action est irréversible. Continuer ?';

  @override
  String get contractTerminated => 'Contrat résilié';

  @override
  String get contractTerminateFailed =>
      'Impossible de résilier le contrat — veuillez réessayer';

  @override
  String get exceptionDetailTitle => 'Exception';

  @override
  String get exceptionDetailNotFound => 'Exception introuvable';

  @override
  String get exceptionDetailOverdue => 'En retard';

  @override
  String get exceptionDetailSectionDescription => 'Description';

  @override
  String get exceptionDetailSectionInvoice => 'Facture';

  @override
  String get exceptionDetailNoLinkedInvoice => 'Aucune facture liée';

  @override
  String get exceptionDetailFieldNumber => 'Numéro';

  @override
  String get exceptionDetailFieldVendor => 'Fournisseur';

  @override
  String get exceptionDetailFieldAmount => 'Montant';

  @override
  String get exceptionDetailFieldSeverity => 'Gravité';

  @override
  String get exceptionDetailSectionSla => 'SLA';

  @override
  String get exceptionDetailFieldCreated => 'Créée';

  @override
  String get exceptionDetailFieldDue => 'Échéance';

  @override
  String get exceptionDetailNoSla => 'Aucun SLA défini';

  @override
  String get exceptionDetailFieldStatus => 'Statut';

  @override
  String get exceptionDetailOnTrack => 'Dans les délais';

  @override
  String get exceptionDetailResolvedIn => 'Résolue en';

  @override
  String exceptionDetailResolvedInHours(String hours) {
    return '$hours h';
  }

  @override
  String get exceptionDetailSectionAssignee => 'Affectée à';

  @override
  String get exceptionDetailUnassigned => 'Non affectée';

  @override
  String get exceptionDetailAssign => 'Affecter';

  @override
  String get exceptionDetailReassign => 'Réaffecter';

  @override
  String get exceptionDetailSectionResolution => 'Résolution';

  @override
  String get exceptionDetailResolutionNote => 'Note';

  @override
  String get exceptionDetailResolutionBy => 'Par';

  @override
  String get exceptionDetailResolutionAt => 'Le';

  @override
  String get exceptionDetailActionResolved => 'Exception résolue';

  @override
  String get exceptionDetailActionEscalated => 'Exception escaladée';

  @override
  String get exceptionDetailActionDismissed => 'Exception rejetée';

  @override
  String get exceptionDetailActionResolveFailed =>
      'Impossible de résoudre l’exception';

  @override
  String get exceptionDetailActionEscalateFailed =>
      'Impossible d’escalader l’exception';

  @override
  String get exceptionDetailActionDismissFailed =>
      'Impossible de rejeter l’exception';

  @override
  String get exceptionDetailAssignTo => 'Affecter à';

  @override
  String get exceptionDetailUnassign => 'Retirer l’affectation';

  @override
  String exceptionDetailLoadUsersFailed(String error) {
    return 'Impossible de charger les utilisateurs : $error';
  }

  @override
  String get exceptionDetailAssigneeUpdateFailed =>
      'Impossible de mettre à jour l’affectation';

  @override
  String get exceptionDetailUnassigned2 => 'Affectation de l’exception retirée';

  @override
  String exceptionDetailAssignedTo(String name) {
    return 'Affectée à $name';
  }

  @override
  String get settingsProcurement => 'Approvisionnement';

  @override
  String get settingsInspections => 'Inspections qualité';

  @override
  String get settingsInspectionsHint =>
      'Enregistrer et consulter les inspections du rapprochement à 4 volets';

  @override
  String get settingsAdministration => 'Administration';

  @override
  String get settingsAdminUsers => 'Gestion des utilisateurs';

  @override
  String get settingsAdminUsersHint =>
      'Rôles, activer / désactiver des utilisateurs';

  @override
  String get settingsAdminOrg => 'Paramètres de l’organisation';

  @override
  String get settingsAdminOrgHint =>
      'Profil de l’entreprise, valeurs par défaut des factures';

  @override
  String get settingsAdminWorkflows => 'Flux de travail';

  @override
  String get settingsAdminWorkflowsHint =>
      'Consulter les définitions de workflow et leurs étapes';

  @override
  String get settingsAdaptive => 'Workflows adaptatifs';

  @override
  String get settingsAdaptiveHint =>
      'Schémas d’approbation, anomalies, suggestions';

  @override
  String get inspectionsTitle => 'Inspections qualité';

  @override
  String get inspectionsRecord => 'Enregistrer une inspection';

  @override
  String get inspectionsEmpty => 'Aucune inspection qualité enregistrée.';

  @override
  String get inspectionsEmptyFiltered => 'Aucune inspection avec ce résultat.';

  @override
  String get inspectionsLoadError => 'Échec du chargement des inspections';

  @override
  String get inspectionsResultPass => 'Conforme';

  @override
  String get inspectionsResultFail => 'Non conforme';

  @override
  String get inspectionsResultPartial => 'Acceptation partielle';

  @override
  String get inspectionsResultUnknown => 'Résultat inconnu';

  @override
  String inspectionsResultAnnounce(String result) {
    return 'Résultat : $result';
  }

  @override
  String get inspectionsNotLinked => 'Non rattachée';

  @override
  String get inspectionsNotLinkedHint =>
      'Cette inspection n’est rattachée à aucune réception ni commande : le rapprochement ne la lira jamais.';

  @override
  String get inspectionsReceiptUnnamed => 'Réception non nommée';

  @override
  String get inspectionsHintPass =>
      'Marchandise acceptée — le rapprochement est inchangé.';

  @override
  String get inspectionsHintFail =>
      'Marchandise refusée — la facture passe en écart et une retenue qualité bloque le paiement.';

  @override
  String get inspectionsHintPartial =>
      'Marchandise partiellement acceptée — le rapprochement passe en partiel et la quantité acceptée est signalée.';

  @override
  String get inspectionsHintUnknown =>
      'Ce résultat sort du vocabulaire conforme / non conforme / partiel : son effet sur le rapprochement ne peut pas être indiqué.';

  @override
  String inspectionsRecorded(String number) {
    return 'Inspection $number enregistrée';
  }

  @override
  String inspectionsRecordFailed(String error) {
    return 'Échec de l’enregistrement de l’inspection : $error';
  }

  @override
  String inspectionsReceiptsLoadFailed(String error) {
    return 'Échec du chargement des réceptions : $error';
  }

  @override
  String get inspectionRecordTitle => 'Enregistrer une inspection qualité';

  @override
  String get inspectionRecordClose => 'Fermer le formulaire d’inspection';

  @override
  String get inspectionRecordReceipt => 'Réception';

  @override
  String get inspectionRecordReceiptHint =>
      'Le rapprochement ne lit une inspection qu’au travers de sa réception : une inspection doit donc désigner la livraison qu’elle couvre.';

  @override
  String inspectionRecordReceiptsBounded(int shown, int total) {
    return 'Affichage des $shown réceptions les plus récentes sur $total. Enregistrez depuis l’application web si la livraison recherchée n’est pas listée.';
  }

  @override
  String get inspectionRecordNoReceipts =>
      'Aucune réception pour l’instant — une inspection porte sur une livraison, il n’y a donc rien à enregistrer.';

  @override
  String get inspectionRecordNumber => 'N° d’inspection';

  @override
  String get inspectionRecordNumberRequired =>
      'Saisissez un numéro d’inspection';

  @override
  String get inspectionRecordResult => 'Résultat';

  @override
  String get inspectionRecordAcceptedQuantity => 'Quantité acceptée';

  @override
  String get inspectionRecordRejectedQuantity => 'Quantité refusée';

  @override
  String get inspectionRecordAcceptedRequired =>
      'Obligatoire pour une acceptation partielle';

  @override
  String get inspectionRecordInvalidQuantity =>
      'Jusqu’à 8 chiffres et 4 décimales';

  @override
  String get inspectionRecordInspectedDate => 'Date d’inspection';

  @override
  String get inspectionRecordDateNotSet => 'Non défini';

  @override
  String get inspectionRecordClearDate => 'Effacer la date d’inspection';

  @override
  String get inspectionRecordInspector => 'Inspecteur';

  @override
  String get inspectionRecordNotes => 'Notes d’écart';

  @override
  String get inspectionRecordNotesHint =>
      'Reprises telles quelles dans l’anomalie de rapprochement de la facture en cas d’inspection non conforme, pour la personne qui traite la retenue qualité.';

  @override
  String get inspectionRecordSubmit => 'Enregistrer l’inspection';

  @override
  String get inspectionDetailTitle => 'Inspection';

  @override
  String get inspectionDetailNotFound => 'Inspection introuvable';

  @override
  String inspectionDetailErrorPrefix(String error) {
    return 'Impossible de charger l’inspection : $error';
  }

  @override
  String get inspectionDetailFieldReceipt => 'Réception';

  @override
  String get inspectionDetailFieldInspectedDate => 'Inspectée le';

  @override
  String get inspectionDetailFieldInspector => 'Inspecteur';

  @override
  String get inspectionDetailFieldAccepted => 'Quantité acceptée';

  @override
  String get inspectionDetailFieldRejected => 'Quantité refusée';

  @override
  String get inspectionDetailFieldStatus => 'Statut';

  @override
  String get inspectionDetailFieldCreated => 'Créée le';

  @override
  String get inspectionDetailSectionNotes => 'Notes d’écart';

  @override
  String get adaptiveTitle => 'Workflows adaptatifs';

  @override
  String get adaptiveTabSuggestions => 'Suggestions';

  @override
  String get adaptiveTabPatterns => 'Schémas d’approbation';

  @override
  String get adaptiveTabAnomalies => 'Anomalies';

  @override
  String get adaptiveAdvisoryNote =>
      'Tout ici est consultatif. Rien sur cet écran n’a modifié un workflow — une recommandation ne prend effet que lorsqu’une personne au rôle adéquat l’applique dans l’application web, par le même chemin audité qu’une modification manuelle.';

  @override
  String get adaptiveSuggestionsEmpty =>
      'Aucune suggestion — historique d’approbation encore trop irrégulier.';

  @override
  String get adaptiveSuggestionsError =>
      'Impossible de charger les suggestions.';

  @override
  String get adaptiveSuggestionsShowOpen => 'Ouvertes';

  @override
  String get adaptiveSuggestionsShowAll => 'Toutes';

  @override
  String adaptiveSuggestionsConfidence(String pct) {
    return 'Confiance $pct %';
  }

  @override
  String get adaptiveSuggestionsDismiss => 'Écarter';

  @override
  String get adaptiveSuggestionsDismissTitle => 'Écarter cette suggestion ?';

  @override
  String get adaptiveSuggestionsDismissBody =>
      'Elle restera écartée après recalcul, et aucun workflow n’est modifié dans un cas comme dans l’autre.';

  @override
  String get adaptiveSuggestionsDismissed => 'Suggestion écartée.';

  @override
  String adaptiveSuggestionsDismissFailed(String error) {
    return 'Impossible d’écarter cette suggestion : $error';
  }

  @override
  String get adaptiveSuggestionStatusOpen => 'Ouverte';

  @override
  String get adaptiveSuggestionStatusDismissed => 'Écartée';

  @override
  String get adaptiveSuggestionStatusApplied => 'Appliquée';

  @override
  String get adaptiveSuggestionStatusStale => 'Obsolète';

  @override
  String get adaptiveSuggestionStatusUnknown => 'Statut inconnu';

  @override
  String adaptiveSuggestionStatusAnnounce(String status) {
    return 'Statut : $status';
  }

  @override
  String get adaptivePatternsError =>
      'Impossible de charger les schémas d’approbation.';

  @override
  String get adaptivePatternsEmptyApprovers =>
      'Aucune décision d’approbation sur cette période.';

  @override
  String get adaptivePatternsEmptyVendors =>
      'Aucun historique fournisseur sur cette période.';

  @override
  String get adaptivePatternsSectionApprovers => 'Par approbateur';

  @override
  String get adaptivePatternsSectionVendors => 'Par fournisseur';

  @override
  String adaptivePatternsLookback(int days) {
    return 'Statistiques déterministes sur les $days derniers jours de l’historique d’approbation de ce locataire — aucun modèle, recalculées à chaque consultation.';
  }

  @override
  String get adaptivePatternsCurrencyNote =>
      'Les montants sont exprimés dans la devise de reporting de votre organisation.';

  @override
  String get adaptivePatternsUnknownApprover => 'Approbateur inconnu';

  @override
  String adaptivePatternsApproverSummary(int approved, int rejected) {
    return '$approved approuvées · $rejected rejetées';
  }

  @override
  String adaptivePatternsApproverTiming(String rate, String days) {
    return 'Taux d’approbation $rate % · médiane $days jours';
  }

  @override
  String adaptivePatternsVendorConsistency(String pct) {
    return '$pct % approuvées sans retouche';
  }

  @override
  String adaptivePatternsVendorMoney(String median, String avg) {
    return 'Médiane $median · moyenne $avg';
  }

  @override
  String adaptivePatternsUnconverted(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other:
          '$count approbations n’ont pas pu être exprimées dans la devise de reporting et sont exclues des montants ci-dessus — l’échantillon les compte toujours',
      one:
          '$count approbation n’a pas pu être exprimée dans la devise de reporting et est exclue des montants ci-dessus — l’échantillon la compte toujours',
    );
    return '$_temp0';
  }

  @override
  String get adaptiveAnomaliesIntro =>
      'Factures en revue qui sortent du schéma établi de leur fournisseur — montant, approbateur ou temps d’attente. Lecture seule : un signalement ici ne déclenche rien et ne bloque rien.';

  @override
  String get adaptiveAnomaliesEmpty =>
      'Rien en revue ne sort du schéma habituel de son fournisseur.';

  @override
  String get adaptiveAnomaliesError =>
      'Impossible de charger l’analyse d’anomalies.';

  @override
  String adaptiveAnomaliesScanned(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count factures en revue analysées.',
      one: '$count facture en revue analysée.',
    );
    return '$_temp0';
  }

  @override
  String adaptiveAnomaliesAmount(String amount, String currency) {
    return '$amount $currency';
  }

  @override
  String get adaptiveAnomaliesInsufficient =>
      'Historique encore insuffisant pour ce fournisseur.';

  @override
  String get invoiceWarningMissingVendorName => 'Nom du fournisseur manquant';

  @override
  String get invoiceWarningMissingInvoiceNumber => 'Numéro de facture manquant';

  @override
  String get invoiceWarningMissingAmount => 'Montant absent ou nul';

  @override
  String get invoiceWarningDuplicateInvoiceNumber =>
      'Numéro de facture en doublon pour ce fournisseur';

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
          ' (plus $crossEntityCount factures quasi identiques dans une autre entité)',
      one:
          ' (plus $crossEntityCount facture quasi identique dans une autre entité)',
      zero: '',
    );
    return 'Doublon possible : $similarity de correspondance avec $invoiceNumber de $vendorName$_temp0';
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
          ' (plus $crossEntityCount factures quasi identiques dans une autre entité)',
      one:
          ' (plus $crossEntityCount facture quasi identique dans une autre entité)',
      zero: '',
    );
    return 'Doublon possible : $similarity de correspondance avec $invoiceNumber$_temp0';
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
          ' (plus $crossEntityCount factures quasi identiques dans une autre entité)',
      one:
          ' (plus $crossEntityCount facture quasi identique dans une autre entité)',
      zero: '',
    );
    return 'Doublon possible : $similarity de correspondance avec une autre facture de $vendorName$_temp0';
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
          ' (plus $crossEntityCount factures quasi identiques dans une autre entité)',
      one:
          ' (plus $crossEntityCount facture quasi identique dans une autre entité)',
      zero: '',
    );
    return 'Doublon possible : $similarity de correspondance avec une autre facture$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarCrossEntity(String similarity) {
    return 'Doublon possible : $similarity de correspondance avec une facture quasi identique dans une autre entité';
  }

  @override
  String invoiceWarningRoundAmount(String amount) {
    return 'Montant rond : $amount';
  }

  @override
  String get invoiceWarningFutureInvoiceDate =>
      'La date de facture est dans le futur';

  @override
  String invoiceWarningRushPayment(int days) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days jours',
      one: '$days jour',
    );
    return 'Paiement urgent : échéance sous $_temp0 après la date de facture';
  }

  @override
  String get invoiceWarningPastDue => 'La facture est en retard de paiement';

  @override
  String get invoiceWarningUnverifiedVendor =>
      'Le fournisseur n’est pas vérifié';

  @override
  String invoiceWarningPersonalEmailDomain(String domain) {
    return 'L’e-mail du fournisseur utilise un domaine personnel : $domain';
  }

  @override
  String invoiceWarningNewVendorLargeAmount(int days, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days jours',
      one: '$days jour',
    );
    return 'Nouveau fournisseur (créé il y a $_temp0) présentant une facture élevée de $amount';
  }

  @override
  String get invoiceWarningRemitToChanged =>
      'L’adresse de paiement a changé depuis la dernière facture approuvée de ce fournisseur';

  @override
  String invoiceWarningAmountAboveVendorMean(
    String amount,
    String sigma,
    String mean,
  ) {
    return 'Le montant $amount est $sigmaσ au-dessus de la moyenne historique de ce fournisseur ($mean)';
  }

  @override
  String invoiceWarningLlmAnomaly(String reason) {
    return 'Anomalie signalée par l’IA : $reason';
  }

  @override
  String invoiceWarningLineTotalMismatch(
    String lineItemsTotal,
    String headerAmount,
  ) {
    return 'Le total des lignes est $lineItemsTotal alors que le montant de la facture est $headerAmount';
  }

  @override
  String invoiceWarningPriceVarianceOver(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Prix unitaire $deltaPct au-dessus de la référence de ce fournisseur pour $item ($unitPrice contre $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPriceVarianceUnder(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Prix unitaire $deltaPct au-dessous de la référence de ce fournisseur pour $item ($unitPrice contre $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPoNotFound(String poNumber) {
    return 'Commande $poNumber introuvable';
  }

  @override
  String invoiceWarningPoAmountVariance(
    String variancePct,
    String poNumber,
    String invoiceAmount,
    String poTotal,
  ) {
    return 'Écart de montant $variancePct par rapport à la commande $poNumber (facture $invoiceAmount contre commande $poTotal)';
  }

  @override
  String invoiceWarningPoPartialReceipt(String matchType, String poNumber) {
    return 'Correspondance 3 voies partielle — correspondance $matchType avec la commande $poNumber, mais seule une partie de la quantité commandée a été reçue';
  }

  @override
  String invoiceWarningPoOverReceipt(
    String receivedQuantity,
    String orderedQuantity,
    String excessQuantity,
    String poNumber,
  ) {
    return 'Sur-réception : $receivedQuantity reçues contre $orderedQuantity commandées (+$excessQuantity) sur la commande $poNumber';
  }

  @override
  String invoiceWarningPoOverReceiptUnquantified(String poNumber) {
    return 'Plus de marchandises reçues que commandées sur la commande $poNumber';
  }

  @override
  String invoiceWarningQualityInspectionFailed(String poNumber) {
    return 'Contrôle qualité non conforme pour la commande $poNumber';
  }

  @override
  String invoiceWarningQualityInspectionFailedNotes(
    String poNumber,
    String notes,
  ) {
    return 'Contrôle qualité non conforme pour la commande $poNumber : $notes';
  }

  @override
  String invoiceWarningQualityInspectionMissing(String poNumber) {
    return 'Contrôle qualité requis mais absent pour la commande $poNumber';
  }

  @override
  String invoiceWarningQualityPartialAcceptance(
    String acceptedQuantity,
    String poNumber,
  ) {
    return 'Acceptation partielle : $acceptedQuantity de la quantité commandée acceptée sur la commande $poNumber';
  }

  @override
  String invoiceWarningQualityPartialAcceptanceUnquantified(String poNumber) {
    return 'Acceptation qualité partielle lors du contrôle de la commande $poNumber';
  }

  @override
  String invoiceWarningRecurringVarianceOver(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'Le montant $amount est $deltaPct au-dessus du montant attendu $expectedAmount du modèle récurrent « $templateName »';
  }

  @override
  String invoiceWarningRecurringVarianceUnder(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'Le montant $amount est $deltaPct au-dessous du montant attendu $expectedAmount du modèle récurrent « $templateName »';
  }

  @override
  String invoiceWarningContractExpired(
    String invoiceDate,
    String contractNumber,
    String endDate,
  ) {
    return 'La facture datée du $invoiceDate est postérieure à l’expiration du contrat $contractNumber ($endDate)';
  }

  @override
  String invoiceWarningContractNotStarted(
    String invoiceDate,
    String contractNumber,
    String startDate,
  ) {
    return 'La facture datée du $invoiceDate est antérieure au début du contrat $contractNumber ($startDate)';
  }

  @override
  String invoiceWarningContractTerminated(String contractNumber) {
    return 'Dépense enregistrée sur le contrat résilié $contractNumber';
  }

  @override
  String invoiceWarningContractCancelled(String contractNumber) {
    return 'Dépense enregistrée sur le contrat annulé $contractNumber';
  }

  @override
  String invoiceWarningContractVendorMismatch(String contractNumber) {
    return 'Le fournisseur de la facture ne correspond pas à celui du contrat $contractNumber';
  }

  @override
  String invoiceWarningContractSpendLimitExceeded(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'La dépense cumulée $cumulativeSpend dépasse la limite $spendLimit du contrat $contractNumber';
  }

  @override
  String invoiceWarningContractSpendLimitExceededNotToExceed(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'La dépense cumulée $cumulativeSpend dépasse la limite $spendLimit du contrat $contractNumber (non dépassable)';
  }

  @override
  String invoiceWarningContractGlNotAllowed(
    String glAccount,
    String contractNumber,
  ) {
    return 'Le compte général $glAccount est en dehors des comptes autorisés du contrat $contractNumber';
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
    return 'Les montants ne concordent pas : sous-total ($subtotal) + taxe ($tax) + livraison ($shipping) − remise ($discount) = $expected, mais le total est $amount.';
  }

  @override
  String invoiceWarningSelfCorrectionDateOrdering(
    String dueDate,
    String invoiceDate,
  ) {
    return 'La date d’échéance ($dueDate) est antérieure à la date de facture ($invoiceDate).';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemsSum(
    String lineItemsTotal,
    String amount,
  ) {
    return 'Le total des lignes ($lineItemsTotal) ne correspond pas au montant de la facture ($amount).';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemMath(
    String lineNumber,
    String quantity,
    String unitPrice,
    String expected,
    String total,
  ) {
    return 'Ligne $lineNumber : $quantity × $unitPrice = $expected, mais le total est $total.';
  }

  @override
  String invoiceWarningGlCodesNotInChart(String codes) {
    return 'Comptes généraux suggérés par l’IA absents du plan comptable actif : $codes';
  }

  @override
  String invoiceWarningGlCodeStalePrior(String code) {
    return 'Le compte général « $code » mis en cache pour ce fournisseur n’est plus dans le plan comptable actif.';
  }
}
