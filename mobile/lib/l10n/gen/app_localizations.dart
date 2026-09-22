import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:intl/intl.dart' as intl;

import 'app_localizations_de.dart';
import 'app_localizations_en.dart';
import 'app_localizations_es.dart';
import 'app_localizations_fr.dart';
import 'app_localizations_ja.dart';
import 'app_localizations_pt.dart';

// ignore_for_file: type=lint

/// Callers can lookup localized strings with an instance of AppLocalizations
/// returned by `AppLocalizations.of(context)`.
///
/// Applications need to include `AppLocalizations.delegate()` in their app's
/// `localizationDelegates` list, and the locales they support in the app's
/// `supportedLocales` list. For example:
///
/// ```dart
/// import 'gen/app_localizations.dart';
///
/// return MaterialApp(
///   localizationsDelegates: AppLocalizations.localizationsDelegates,
///   supportedLocales: AppLocalizations.supportedLocales,
///   home: MyApplicationHome(),
/// );
/// ```
///
/// ## Update pubspec.yaml
///
/// Please make sure to update your pubspec.yaml to include the following
/// packages:
///
/// ```yaml
/// dependencies:
///   # Internationalization support.
///   flutter_localizations:
///     sdk: flutter
///   intl: any # Use the pinned version from flutter_localizations
///
///   # Rest of dependencies
/// ```
///
/// ## iOS Applications
///
/// iOS applications define key application metadata, including supported
/// locales, in an Info.plist file that is built into the application bundle.
/// To configure the locales supported by your app, you’ll need to edit this
/// file.
///
/// First, open your project’s ios/Runner.xcworkspace Xcode workspace file.
/// Then, in the Project Navigator, open the Info.plist file under the Runner
/// project’s Runner folder.
///
/// Next, select the Information Property List item, select Add Item from the
/// Editor menu, then select Localizations from the pop-up menu.
///
/// Select and expand the newly-created Localizations item then, for each
/// locale your application supports, add a new item and select the locale
/// you wish to add from the pop-up menu in the Value field. This list should
/// be consistent with the languages listed in the AppLocalizations.supportedLocales
/// property.
abstract class AppLocalizations {
  AppLocalizations(String locale)
    : localeName = intl.Intl.canonicalizedLocale(locale.toString());

  final String localeName;

  static AppLocalizations of(BuildContext context) {
    return Localizations.of<AppLocalizations>(context, AppLocalizations)!;
  }

  static const LocalizationsDelegate<AppLocalizations> delegate =
      _AppLocalizationsDelegate();

  /// A list of this localizations delegate along with the default localizations
  /// delegates.
  ///
  /// Returns a list of localizations delegates containing this delegate along with
  /// GlobalMaterialLocalizations.delegate, GlobalCupertinoLocalizations.delegate,
  /// and GlobalWidgetsLocalizations.delegate.
  ///
  /// Additional delegates can be added by appending to this list in
  /// MaterialApp. This list does not have to be used at all if a custom list
  /// of delegates is preferred or required.
  static const List<LocalizationsDelegate<dynamic>> localizationsDelegates =
      <LocalizationsDelegate<dynamic>>[
        delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
      ];

  /// A list of this localizations delegate's supported locales.
  static const List<Locale> supportedLocales = <Locale>[
    Locale('de'),
    Locale('en'),
    Locale('es'),
    Locale('fr'),
    Locale('ja'),
    Locale('pt'),
    Locale('pt', 'BR'),
  ];

  /// No description provided for @navDashboard.
  ///
  /// In en, this message translates to:
  /// **'Dashboard'**
  String get navDashboard;

  /// No description provided for @navInvoices.
  ///
  /// In en, this message translates to:
  /// **'Invoices'**
  String get navInvoices;

  /// No description provided for @navContracts.
  ///
  /// In en, this message translates to:
  /// **'Contracts'**
  String get navContracts;

  /// No description provided for @navApprovals.
  ///
  /// In en, this message translates to:
  /// **'Approvals'**
  String get navApprovals;

  /// No description provided for @navExceptions.
  ///
  /// In en, this message translates to:
  /// **'Exceptions'**
  String get navExceptions;

  /// No description provided for @navVendors.
  ///
  /// In en, this message translates to:
  /// **'Vendors'**
  String get navVendors;

  /// No description provided for @navPay.
  ///
  /// In en, this message translates to:
  /// **'Pay'**
  String get navPay;

  /// No description provided for @navPayments.
  ///
  /// In en, this message translates to:
  /// **'Payments'**
  String get navPayments;

  /// No description provided for @navSettings.
  ///
  /// In en, this message translates to:
  /// **'Settings'**
  String get navSettings;

  /// No description provided for @shellAppName.
  ///
  /// In en, this message translates to:
  /// **'FeohLedger'**
  String get shellAppName;

  /// No description provided for @commonSave.
  ///
  /// In en, this message translates to:
  /// **'Save'**
  String get commonSave;

  /// No description provided for @commonSaving.
  ///
  /// In en, this message translates to:
  /// **'Saving…'**
  String get commonSaving;

  /// No description provided for @commonCancel.
  ///
  /// In en, this message translates to:
  /// **'Cancel'**
  String get commonCancel;

  /// No description provided for @commonLoading.
  ///
  /// In en, this message translates to:
  /// **'Loading…'**
  String get commonLoading;

  /// No description provided for @commonRetry.
  ///
  /// In en, this message translates to:
  /// **'Retry'**
  String get commonRetry;

  /// No description provided for @commonAll.
  ///
  /// In en, this message translates to:
  /// **'All'**
  String get commonAll;

  /// No description provided for @commonSearch.
  ///
  /// In en, this message translates to:
  /// **'Search'**
  String get commonSearch;

  /// No description provided for @commonClear.
  ///
  /// In en, this message translates to:
  /// **'Clear'**
  String get commonClear;

  /// No description provided for @commonApply.
  ///
  /// In en, this message translates to:
  /// **'Apply'**
  String get commonApply;

  /// No description provided for @commonClose.
  ///
  /// In en, this message translates to:
  /// **'Close'**
  String get commonClose;

  /// No description provided for @settingsTitle.
  ///
  /// In en, this message translates to:
  /// **'Settings'**
  String get settingsTitle;

  /// No description provided for @settingsTenant.
  ///
  /// In en, this message translates to:
  /// **'Tenant'**
  String get settingsTenant;

  /// No description provided for @settingsTenantNotSet.
  ///
  /// In en, this message translates to:
  /// **'Not set'**
  String get settingsTenantNotSet;

  /// No description provided for @settingsApiServer.
  ///
  /// In en, this message translates to:
  /// **'API Server'**
  String get settingsApiServer;

  /// No description provided for @settingsBiometricUnlock.
  ///
  /// In en, this message translates to:
  /// **'Biometric Unlock'**
  String get settingsBiometricUnlock;

  /// No description provided for @settingsBiometricHint.
  ///
  /// In en, this message translates to:
  /// **'Use fingerprint or face to unlock'**
  String get settingsBiometricHint;

  /// No description provided for @settingsSignOut.
  ///
  /// In en, this message translates to:
  /// **'Sign Out'**
  String get settingsSignOut;

  /// No description provided for @settingsLanguage.
  ///
  /// In en, this message translates to:
  /// **'Language'**
  String get settingsLanguage;

  /// No description provided for @settingsLanguageHint.
  ///
  /// In en, this message translates to:
  /// **'Choose the language used across the app. Your choice is saved on this device.'**
  String get settingsLanguageHint;

  /// No description provided for @settingsLanguageSystem.
  ///
  /// In en, this message translates to:
  /// **'System default'**
  String get settingsLanguageSystem;

  /// No description provided for @dashboardTitle.
  ///
  /// In en, this message translates to:
  /// **'Dashboard'**
  String get dashboardTitle;

  /// No description provided for @dashboardTotalInvoices.
  ///
  /// In en, this message translates to:
  /// **'Total Invoices'**
  String get dashboardTotalInvoices;

  /// No description provided for @dashboardUpcoming.
  ///
  /// In en, this message translates to:
  /// **'Upcoming'**
  String get dashboardUpcoming;

  /// No description provided for @dashboardForReview.
  ///
  /// In en, this message translates to:
  /// **'For Review'**
  String get dashboardForReview;

  /// No description provided for @dashboardApproved.
  ///
  /// In en, this message translates to:
  /// **'Approved'**
  String get dashboardApproved;

  /// No description provided for @dashboardAging.
  ///
  /// In en, this message translates to:
  /// **'Invoice Aging'**
  String get dashboardAging;

  /// No description provided for @dashboardTopVendors.
  ///
  /// In en, this message translates to:
  /// **'Top Vendors'**
  String get dashboardTopVendors;

  /// No description provided for @dashboardAgingCurrent.
  ///
  /// In en, this message translates to:
  /// **'Current'**
  String get dashboardAgingCurrent;

  /// No description provided for @dashboardAgingDays30.
  ///
  /// In en, this message translates to:
  /// **'30 Days'**
  String get dashboardAgingDays30;

  /// No description provided for @dashboardAgingDays60.
  ///
  /// In en, this message translates to:
  /// **'60 Days'**
  String get dashboardAgingDays60;

  /// No description provided for @dashboardAgingDays90plus.
  ///
  /// In en, this message translates to:
  /// **'90+'**
  String get dashboardAgingDays90plus;

  /// No description provided for @dashboardCachedBanner.
  ///
  /// In en, this message translates to:
  /// **'Showing cached data — couldn\'t reach the server'**
  String get dashboardCachedBanner;

  /// No description provided for @dashboardErrorPrefix.
  ///
  /// In en, this message translates to:
  /// **'Error: {error}'**
  String dashboardErrorPrefix(String error);

  /// No description provided for @dashboardInvoiceCount.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} invoice} other {{count} invoices}}'**
  String dashboardInvoiceCount(int count);

  /// No description provided for @partialConversionCurrencyFallback.
  ///
  /// In en, this message translates to:
  /// **'the reporting currency'**
  String get partialConversionCurrencyFallback;

  /// No description provided for @dashboardKpiUnconverted.
  ///
  /// In en, this message translates to:
  /// **'{label}: partial — {count, plural, one {{count} invoice} other {{count} invoices}} with no exchange rate into {currency}, counted at face value, so the total mixes currencies by that much.'**
  String dashboardKpiUnconverted(String label, int count, String currency);

  /// No description provided for @dashboardAgingUnconverted.
  ///
  /// In en, this message translates to:
  /// **'Partial: {count, plural, one {{count} invoice} other {{count} invoices}} with no exchange rate into {currency}, counted at face value — the bands below mix currencies by that much.'**
  String dashboardAgingUnconverted(int count, String currency);

  /// No description provided for @dashboardTopVendorsUnconverted.
  ///
  /// In en, this message translates to:
  /// **'Partial: {count, plural, one {{count} invoice} other {{count} invoices}} with no exchange rate into {currency}, counted at face value — so these vendors are not all ranked in the same currency. Book the missing rate before acting on the order.'**
  String dashboardTopVendorsUnconverted(int count, String currency);

  /// No description provided for @dashboardVendorFaceValueCount.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} at face value} other {{count} at face value}}'**
  String dashboardVendorFaceValueCount(int count);

  /// No description provided for @invoicesTitle.
  ///
  /// In en, this message translates to:
  /// **'Invoices'**
  String get invoicesTitle;

  /// No description provided for @invoicesSearchHint.
  ///
  /// In en, this message translates to:
  /// **'Search invoices...'**
  String get invoicesSearchHint;

  /// No description provided for @invoicesSearchAria.
  ///
  /// In en, this message translates to:
  /// **'Search invoices'**
  String get invoicesSearchAria;

  /// No description provided for @invoicesAdvancedSearch.
  ///
  /// In en, this message translates to:
  /// **'Advanced Search'**
  String get invoicesAdvancedSearch;

  /// No description provided for @invoicesAdvancedSearchActive.
  ///
  /// In en, this message translates to:
  /// **'Advanced search, filters active'**
  String get invoicesAdvancedSearchActive;

  /// No description provided for @invoicesCaptureInvoice.
  ///
  /// In en, this message translates to:
  /// **'Capture Invoice'**
  String get invoicesCaptureInvoice;

  /// No description provided for @invoicesCaptureInvoiceLabel.
  ///
  /// In en, this message translates to:
  /// **'Capture invoice'**
  String get invoicesCaptureInvoiceLabel;

  /// No description provided for @invoicesEmpty.
  ///
  /// In en, this message translates to:
  /// **'No invoices found'**
  String get invoicesEmpty;

  /// No description provided for @invoicesFilterAll.
  ///
  /// In en, this message translates to:
  /// **'All'**
  String get invoicesFilterAll;

  /// No description provided for @invoicesFilterNew.
  ///
  /// In en, this message translates to:
  /// **'New'**
  String get invoicesFilterNew;

  /// No description provided for @invoicesFilterPending.
  ///
  /// In en, this message translates to:
  /// **'Pending'**
  String get invoicesFilterPending;

  /// No description provided for @invoicesFilterReview.
  ///
  /// In en, this message translates to:
  /// **'Review'**
  String get invoicesFilterReview;

  /// No description provided for @invoicesFilterApproved.
  ///
  /// In en, this message translates to:
  /// **'Approved'**
  String get invoicesFilterApproved;

  /// No description provided for @invoicesFilterRejected.
  ///
  /// In en, this message translates to:
  /// **'Rejected'**
  String get invoicesFilterRejected;

  /// No description provided for @invoicesFilterPaid.
  ///
  /// In en, this message translates to:
  /// **'Paid'**
  String get invoicesFilterPaid;

  /// No description provided for @invoicesColInvoiceNumber.
  ///
  /// In en, this message translates to:
  /// **'Invoice #'**
  String get invoicesColInvoiceNumber;

  /// No description provided for @invoicesColVendor.
  ///
  /// In en, this message translates to:
  /// **'Vendor'**
  String get invoicesColVendor;

  /// No description provided for @invoicesColAmount.
  ///
  /// In en, this message translates to:
  /// **'Amount'**
  String get invoicesColAmount;

  /// No description provided for @invoicesColDueDate.
  ///
  /// In en, this message translates to:
  /// **'Due Date'**
  String get invoicesColDueDate;

  /// No description provided for @invoicesColStatus.
  ///
  /// In en, this message translates to:
  /// **'Status'**
  String get invoicesColStatus;

  /// No description provided for @notificationsTitle.
  ///
  /// In en, this message translates to:
  /// **'Notifications'**
  String get notificationsTitle;

  /// No description provided for @notificationsMarkAllRead.
  ///
  /// In en, this message translates to:
  /// **'Mark all read'**
  String get notificationsMarkAllRead;

  /// No description provided for @notificationsMarkAllReadLabel.
  ///
  /// In en, this message translates to:
  /// **'Mark all notifications read'**
  String get notificationsMarkAllReadLabel;

  /// No description provided for @notificationsFilterUnread.
  ///
  /// In en, this message translates to:
  /// **'Unread'**
  String get notificationsFilterUnread;

  /// No description provided for @notificationsAllMarkedRead.
  ///
  /// In en, this message translates to:
  /// **'All notifications marked read'**
  String get notificationsAllMarkedRead;

  /// No description provided for @notificationsCouldNotMarkAll.
  ///
  /// In en, this message translates to:
  /// **'Could not mark all read'**
  String get notificationsCouldNotMarkAll;

  /// No description provided for @notificationsEmptyUnread.
  ///
  /// In en, this message translates to:
  /// **'No unread notifications'**
  String get notificationsEmptyUnread;

  /// No description provided for @notificationsEmpty.
  ///
  /// In en, this message translates to:
  /// **'No notifications'**
  String get notificationsEmpty;

  /// No description provided for @notificationsCaughtUp.
  ///
  /// In en, this message translates to:
  /// **'You\'re all caught up'**
  String get notificationsCaughtUp;

  /// No description provided for @notificationsNothingYet.
  ///
  /// In en, this message translates to:
  /// **'Nothing here yet'**
  String get notificationsNothingYet;

  /// No description provided for @notificationsLoadError.
  ///
  /// In en, this message translates to:
  /// **'Couldn\'t load notifications'**
  String get notificationsLoadError;

  /// No description provided for @vendorsTitle.
  ///
  /// In en, this message translates to:
  /// **'Vendors'**
  String get vendorsTitle;

  /// No description provided for @vendorsSyncErp.
  ///
  /// In en, this message translates to:
  /// **'Sync from ERP'**
  String get vendorsSyncErp;

  /// No description provided for @vendorsSyncErpLabel.
  ///
  /// In en, this message translates to:
  /// **'Sync vendors from ERP'**
  String get vendorsSyncErpLabel;

  /// No description provided for @vendorsSearchHint.
  ///
  /// In en, this message translates to:
  /// **'Search vendors...'**
  String get vendorsSearchHint;

  /// No description provided for @vendorsFilterUnverified.
  ///
  /// In en, this message translates to:
  /// **'Unverified'**
  String get vendorsFilterUnverified;

  /// No description provided for @vendorsFilterActive.
  ///
  /// In en, this message translates to:
  /// **'Active'**
  String get vendorsFilterActive;

  /// No description provided for @vendorsFilterInactive.
  ///
  /// In en, this message translates to:
  /// **'Inactive'**
  String get vendorsFilterInactive;

  /// No description provided for @vendorsFilterRejected.
  ///
  /// In en, this message translates to:
  /// **'Rejected'**
  String get vendorsFilterRejected;

  /// No description provided for @vendorsEmpty.
  ///
  /// In en, this message translates to:
  /// **'No vendors found'**
  String get vendorsEmpty;

  /// No description provided for @vendorsLoadError.
  ///
  /// In en, this message translates to:
  /// **'Could not load vendors'**
  String get vendorsLoadError;

  /// No description provided for @vendorActionVerify.
  ///
  /// In en, this message translates to:
  /// **'Verify'**
  String get vendorActionVerify;

  /// No description provided for @vendorActionReject.
  ///
  /// In en, this message translates to:
  /// **'Reject'**
  String get vendorActionReject;

  /// No description provided for @vendorUnverifiedLabel.
  ///
  /// In en, this message translates to:
  /// **'Unverified vendor'**
  String get vendorUnverifiedLabel;

  /// No description provided for @vendorVerifyHint.
  ///
  /// In en, this message translates to:
  /// **'Make eligible for payment'**
  String get vendorVerifyHint;

  /// No description provided for @vendorRejectHint.
  ///
  /// In en, this message translates to:
  /// **'Mark as invalid / duplicate'**
  String get vendorRejectHint;

  /// No description provided for @vendorVerified.
  ///
  /// In en, this message translates to:
  /// **'Vendor verified'**
  String get vendorVerified;

  /// No description provided for @vendorRejected.
  ///
  /// In en, this message translates to:
  /// **'Vendor rejected'**
  String get vendorRejected;

  /// No description provided for @vendorActionFailed.
  ///
  /// In en, this message translates to:
  /// **'Action failed'**
  String get vendorActionFailed;

  /// No description provided for @vendorSyncFailed.
  ///
  /// In en, this message translates to:
  /// **'ERP sync failed: {error}'**
  String vendorSyncFailed(String error);

  /// No description provided for @exceptionsTitle.
  ///
  /// In en, this message translates to:
  /// **'Exceptions'**
  String get exceptionsTitle;

  /// No description provided for @exceptionsFilterOpen.
  ///
  /// In en, this message translates to:
  /// **'Open'**
  String get exceptionsFilterOpen;

  /// No description provided for @exceptionsFilterEscalated.
  ///
  /// In en, this message translates to:
  /// **'Escalated'**
  String get exceptionsFilterEscalated;

  /// No description provided for @exceptionsFilterResolved.
  ///
  /// In en, this message translates to:
  /// **'Resolved'**
  String get exceptionsFilterResolved;

  /// No description provided for @exceptionsFilterDismissed.
  ///
  /// In en, this message translates to:
  /// **'Dismissed'**
  String get exceptionsFilterDismissed;

  /// No description provided for @exceptionsEmpty.
  ///
  /// In en, this message translates to:
  /// **'No exceptions'**
  String get exceptionsEmpty;

  /// No description provided for @exceptionsQueueClear.
  ///
  /// In en, this message translates to:
  /// **'The exception queue is clear'**
  String get exceptionsQueueClear;

  /// No description provided for @exceptionActionResolve.
  ///
  /// In en, this message translates to:
  /// **'Resolve'**
  String get exceptionActionResolve;

  /// No description provided for @exceptionActionEscalate.
  ///
  /// In en, this message translates to:
  /// **'Escalate'**
  String get exceptionActionEscalate;

  /// No description provided for @exceptionActionDismiss.
  ///
  /// In en, this message translates to:
  /// **'Dismiss'**
  String get exceptionActionDismiss;

  /// No description provided for @exceptionResolved.
  ///
  /// In en, this message translates to:
  /// **'Exception resolved'**
  String get exceptionResolved;

  /// No description provided for @exceptionEscalated.
  ///
  /// In en, this message translates to:
  /// **'Exception escalated'**
  String get exceptionEscalated;

  /// No description provided for @exceptionDismissed.
  ///
  /// In en, this message translates to:
  /// **'Exception dismissed'**
  String get exceptionDismissed;

  /// No description provided for @exceptionActionFailed.
  ///
  /// In en, this message translates to:
  /// **'Action failed'**
  String get exceptionActionFailed;

  /// No description provided for @paymentsTitle.
  ///
  /// In en, this message translates to:
  /// **'Payments'**
  String get paymentsTitle;

  /// No description provided for @paymentsEmpty.
  ///
  /// In en, this message translates to:
  /// **'No payments'**
  String get paymentsEmpty;

  /// No description provided for @paymentsErrorPrefix.
  ///
  /// In en, this message translates to:
  /// **'Error: {error}'**
  String paymentsErrorPrefix(String error);

  /// No description provided for @paymentStatusPending.
  ///
  /// In en, this message translates to:
  /// **'Pending'**
  String get paymentStatusPending;

  /// No description provided for @paymentStatusProcessing.
  ///
  /// In en, this message translates to:
  /// **'Processing'**
  String get paymentStatusProcessing;

  /// No description provided for @paymentStatusCompleted.
  ///
  /// In en, this message translates to:
  /// **'Completed'**
  String get paymentStatusCompleted;

  /// No description provided for @paymentStatusFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed'**
  String get paymentStatusFailed;

  /// No description provided for @paymentStatusCancelled.
  ///
  /// In en, this message translates to:
  /// **'Cancelled'**
  String get paymentStatusCancelled;

  /// No description provided for @approvalsTitle.
  ///
  /// In en, this message translates to:
  /// **'Pending Approvals'**
  String get approvalsTitle;

  /// No description provided for @approvalsAllCaughtUp.
  ///
  /// In en, this message translates to:
  /// **'All caught up!'**
  String get approvalsAllCaughtUp;

  /// No description provided for @approvalsNoneWaiting.
  ///
  /// In en, this message translates to:
  /// **'No invoices waiting for approval'**
  String get approvalsNoneWaiting;

  /// No description provided for @approvalsLoadError.
  ///
  /// In en, this message translates to:
  /// **'Could not load pending approvals'**
  String get approvalsLoadError;

  /// No description provided for @approvalsPendingCount.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} invoice pending} other {{count} invoices pending}}'**
  String approvalsPendingCount(int count);

  /// No description provided for @approvalActionApprove.
  ///
  /// In en, this message translates to:
  /// **'Approve'**
  String get approvalActionApprove;

  /// No description provided for @approvalActionReject.
  ///
  /// In en, this message translates to:
  /// **'Reject'**
  String get approvalActionReject;

  /// No description provided for @approvalApproved.
  ///
  /// In en, this message translates to:
  /// **'Invoice approved'**
  String get approvalApproved;

  /// No description provided for @captureTitle.
  ///
  /// In en, this message translates to:
  /// **'Capture Invoice'**
  String get captureTitle;

  /// No description provided for @captureChange.
  ///
  /// In en, this message translates to:
  /// **'Change'**
  String get captureChange;

  /// No description provided for @captureUpload.
  ///
  /// In en, this message translates to:
  /// **'Upload'**
  String get captureUpload;

  /// No description provided for @captureUploading.
  ///
  /// In en, this message translates to:
  /// **'Uploading…'**
  String get captureUploading;

  /// No description provided for @captureEmptyPrompt.
  ///
  /// In en, this message translates to:
  /// **'Take a photo, choose from gallery, or pick a file'**
  String get captureEmptyPrompt;

  /// No description provided for @captureCamera.
  ///
  /// In en, this message translates to:
  /// **'Camera'**
  String get captureCamera;

  /// No description provided for @captureGallery.
  ///
  /// In en, this message translates to:
  /// **'Gallery'**
  String get captureGallery;

  /// No description provided for @captureChooseFile.
  ///
  /// In en, this message translates to:
  /// **'Choose file'**
  String get captureChooseFile;

  /// No description provided for @captureSupportedFormats.
  ///
  /// In en, this message translates to:
  /// **'Supports PDF, PNG, JPG and TIFF'**
  String get captureSupportedFormats;

  /// No description provided for @captureUploadSuccess.
  ///
  /// In en, this message translates to:
  /// **'Invoice uploaded successfully'**
  String get captureUploadSuccess;

  /// No description provided for @captureUploadFailedStatus.
  ///
  /// In en, this message translates to:
  /// **'Upload failed ({status}): {message}'**
  String captureUploadFailedStatus(int status, String message);

  /// No description provided for @captureUploadFailed.
  ///
  /// In en, this message translates to:
  /// **'Upload failed: {error}'**
  String captureUploadFailed(String error);

  /// No description provided for @captureSelectedDocument.
  ///
  /// In en, this message translates to:
  /// **'Selected document: {name}'**
  String captureSelectedDocument(String name);

  /// No description provided for @capturePdfReady.
  ///
  /// In en, this message translates to:
  /// **'PDF document ready to upload'**
  String get capturePdfReady;

  /// No description provided for @advSearchTitle.
  ///
  /// In en, this message translates to:
  /// **'Advanced Search'**
  String get advSearchTitle;

  /// No description provided for @advSearchClose.
  ///
  /// In en, this message translates to:
  /// **'Close advanced search'**
  String get advSearchClose;

  /// No description provided for @advSearchVendor.
  ///
  /// In en, this message translates to:
  /// **'Vendor'**
  String get advSearchVendor;

  /// No description provided for @advSearchPoNumber.
  ///
  /// In en, this message translates to:
  /// **'PO Number'**
  String get advSearchPoNumber;

  /// No description provided for @advSearchMinAmount.
  ///
  /// In en, this message translates to:
  /// **'Min amount'**
  String get advSearchMinAmount;

  /// No description provided for @advSearchMaxAmount.
  ///
  /// In en, this message translates to:
  /// **'Max amount'**
  String get advSearchMaxAmount;

  /// No description provided for @advSearchDueFrom.
  ///
  /// In en, this message translates to:
  /// **'Due from'**
  String get advSearchDueFrom;

  /// No description provided for @advSearchDueTo.
  ///
  /// In en, this message translates to:
  /// **'Due to'**
  String get advSearchDueTo;

  /// No description provided for @advSearchAny.
  ///
  /// In en, this message translates to:
  /// **'Any'**
  String get advSearchAny;

  /// No description provided for @advSearchInvalidAmount.
  ///
  /// In en, this message translates to:
  /// **'Enter a valid amount (e.g. 1000)'**
  String get advSearchInvalidAmount;

  /// No description provided for @advSearchMinMaxError.
  ///
  /// In en, this message translates to:
  /// **'Min must not exceed max'**
  String get advSearchMinMaxError;

  /// No description provided for @advSearchClearField.
  ///
  /// In en, this message translates to:
  /// **'Clear {label}'**
  String advSearchClearField(String label);

  /// No description provided for @advSearchDateFieldHint.
  ///
  /// In en, this message translates to:
  /// **'{label}, currently {value}. Double tap to change.'**
  String advSearchDateFieldHint(String label, String value);

  /// No description provided for @invoiceDetailTitle.
  ///
  /// In en, this message translates to:
  /// **'Invoice Detail'**
  String get invoiceDetailTitle;

  /// No description provided for @invoiceDetailEdit.
  ///
  /// In en, this message translates to:
  /// **'Edit'**
  String get invoiceDetailEdit;

  /// No description provided for @invoiceDetailEditLabel.
  ///
  /// In en, this message translates to:
  /// **'Edit invoice'**
  String get invoiceDetailEditLabel;

  /// No description provided for @invoiceDetailRetry.
  ///
  /// In en, this message translates to:
  /// **'Retry'**
  String get invoiceDetailRetry;

  /// No description provided for @invoiceDetailErrorPrefix.
  ///
  /// In en, this message translates to:
  /// **'Error: {error}'**
  String invoiceDetailErrorPrefix(String error);

  /// No description provided for @invoiceDetailNoChanges.
  ///
  /// In en, this message translates to:
  /// **'No changes to save'**
  String get invoiceDetailNoChanges;

  /// No description provided for @invoiceDetailUpdated.
  ///
  /// In en, this message translates to:
  /// **'Invoice updated'**
  String get invoiceDetailUpdated;

  /// No description provided for @invoiceDetailUpdateFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not save changes: {error}'**
  String invoiceDetailUpdateFailed(String error);

  /// No description provided for @invoiceDetailApproved.
  ///
  /// In en, this message translates to:
  /// **'Invoice approved'**
  String get invoiceDetailApproved;

  /// No description provided for @invoiceDetailApproveFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not approve invoice — please try again'**
  String get invoiceDetailApproveFailed;

  /// No description provided for @invoiceDetailRejected.
  ///
  /// In en, this message translates to:
  /// **'Invoice rejected'**
  String get invoiceDetailRejected;

  /// No description provided for @invoiceDetailRejectFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not reject invoice — please try again'**
  String get invoiceDetailRejectFailed;

  /// No description provided for @invoiceDetailRejectTitle.
  ///
  /// In en, this message translates to:
  /// **'Reject Invoice'**
  String get invoiceDetailRejectTitle;

  /// No description provided for @invoiceDetailRejectReason.
  ///
  /// In en, this message translates to:
  /// **'Reason'**
  String get invoiceDetailRejectReason;

  /// No description provided for @invoiceDetailReject.
  ///
  /// In en, this message translates to:
  /// **'Reject'**
  String get invoiceDetailReject;

  /// No description provided for @invoiceDetailApprove.
  ///
  /// In en, this message translates to:
  /// **'Approve'**
  String get invoiceDetailApprove;

  /// No description provided for @invoiceDetailUnknownVendor.
  ///
  /// In en, this message translates to:
  /// **'Unknown Vendor'**
  String get invoiceDetailUnknownVendor;

  /// No description provided for @invoiceDetailFieldInvoiceNumber.
  ///
  /// In en, this message translates to:
  /// **'Invoice #'**
  String get invoiceDetailFieldInvoiceNumber;

  /// No description provided for @invoiceDetailFieldPoNumber.
  ///
  /// In en, this message translates to:
  /// **'PO Number'**
  String get invoiceDetailFieldPoNumber;

  /// No description provided for @invoiceDetailFieldCurrency.
  ///
  /// In en, this message translates to:
  /// **'Currency'**
  String get invoiceDetailFieldCurrency;

  /// No description provided for @invoiceDetailFieldInvoiceDate.
  ///
  /// In en, this message translates to:
  /// **'Invoice Date'**
  String get invoiceDetailFieldInvoiceDate;

  /// No description provided for @invoiceDetailFieldDueDate.
  ///
  /// In en, this message translates to:
  /// **'Due Date'**
  String get invoiceDetailFieldDueDate;

  /// No description provided for @invoiceDetailFieldDescription.
  ///
  /// In en, this message translates to:
  /// **'Description'**
  String get invoiceDetailFieldDescription;

  /// No description provided for @invoiceDetailFieldGlAccount.
  ///
  /// In en, this message translates to:
  /// **'GL Account'**
  String get invoiceDetailFieldGlAccount;

  /// No description provided for @invoiceDetailFieldCreated.
  ///
  /// In en, this message translates to:
  /// **'Created'**
  String get invoiceDetailFieldCreated;

  /// No description provided for @invoiceDetailActivity.
  ///
  /// In en, this message translates to:
  /// **'Activity'**
  String get invoiceDetailActivity;

  /// No description provided for @invoiceDetailActivityError.
  ///
  /// In en, this message translates to:
  /// **'Could not load activity'**
  String get invoiceDetailActivityError;

  /// No description provided for @invoiceDetailFilePdfLabel.
  ///
  /// In en, this message translates to:
  /// **'Invoice PDF. Double tap to view full screen.'**
  String get invoiceDetailFilePdfLabel;

  /// No description provided for @invoiceDetailFileLabel.
  ///
  /// In en, this message translates to:
  /// **'Invoice file. Double tap to view full screen.'**
  String get invoiceDetailFileLabel;

  /// No description provided for @invoiceDetailTapToViewPdf.
  ///
  /// In en, this message translates to:
  /// **'Tap to view PDF'**
  String get invoiceDetailTapToViewPdf;

  /// No description provided for @invoiceDetailTapToViewFile.
  ///
  /// In en, this message translates to:
  /// **'Tap to view file'**
  String get invoiceDetailTapToViewFile;

  /// No description provided for @invoiceEditTitle.
  ///
  /// In en, this message translates to:
  /// **'Edit Invoice'**
  String get invoiceEditTitle;

  /// No description provided for @invoiceEditClose.
  ///
  /// In en, this message translates to:
  /// **'Close edit form'**
  String get invoiceEditClose;

  /// No description provided for @invoiceEditVendor.
  ///
  /// In en, this message translates to:
  /// **'Vendor'**
  String get invoiceEditVendor;

  /// No description provided for @invoiceEditInvoiceNumber.
  ///
  /// In en, this message translates to:
  /// **'Invoice #'**
  String get invoiceEditInvoiceNumber;

  /// No description provided for @invoiceEditAmount.
  ///
  /// In en, this message translates to:
  /// **'Amount'**
  String get invoiceEditAmount;

  /// No description provided for @invoiceEditPoNumber.
  ///
  /// In en, this message translates to:
  /// **'PO Number'**
  String get invoiceEditPoNumber;

  /// No description provided for @invoiceEditGlAccount.
  ///
  /// In en, this message translates to:
  /// **'GL Account'**
  String get invoiceEditGlAccount;

  /// No description provided for @invoiceEditDescription.
  ///
  /// In en, this message translates to:
  /// **'Description'**
  String get invoiceEditDescription;

  /// No description provided for @invoiceEditDueDate.
  ///
  /// In en, this message translates to:
  /// **'Due Date'**
  String get invoiceEditDueDate;

  /// No description provided for @invoiceEditNotSet.
  ///
  /// In en, this message translates to:
  /// **'Not set'**
  String get invoiceEditNotSet;

  /// No description provided for @invoiceEditInvalidAmount.
  ///
  /// In en, this message translates to:
  /// **'Enter a valid amount (e.g. 1234.56)'**
  String get invoiceEditInvalidAmount;

  /// No description provided for @invoiceEditClearDueDate.
  ///
  /// In en, this message translates to:
  /// **'Clear due date'**
  String get invoiceEditClearDueDate;

  /// No description provided for @invoiceEditLockedNotice.
  ///
  /// In en, this message translates to:
  /// **'Approved — the payee and amount are frozen. To change them, reject the invoice, correct it, then approve it again.'**
  String get invoiceEditLockedNotice;

  /// No description provided for @invoiceEditLockedHelper.
  ///
  /// In en, this message translates to:
  /// **'Frozen after approval'**
  String get invoiceEditLockedHelper;

  /// No description provided for @invoiceEditDueDateHint.
  ///
  /// In en, this message translates to:
  /// **'Due date, currently {value}. Double tap to change.'**
  String invoiceEditDueDateHint(String value);

  /// No description provided for @warningsSectionTitle.
  ///
  /// In en, this message translates to:
  /// **'Warnings & fraud flags'**
  String get warningsSectionTitle;

  /// No description provided for @warningsPoMatchTitle.
  ///
  /// In en, this message translates to:
  /// **'PO Match'**
  String get warningsPoMatchTitle;

  /// No description provided for @warningsSeverityError.
  ///
  /// In en, this message translates to:
  /// **'Error'**
  String get warningsSeverityError;

  /// No description provided for @warningsSeverityWarning.
  ///
  /// In en, this message translates to:
  /// **'Warning'**
  String get warningsSeverityWarning;

  /// No description provided for @warningsSeverityInfo.
  ///
  /// In en, this message translates to:
  /// **'Info'**
  String get warningsSeverityInfo;

  /// No description provided for @warningsPoLabel.
  ///
  /// In en, this message translates to:
  /// **'PO'**
  String get warningsPoLabel;

  /// No description provided for @warningsMatchLabel.
  ///
  /// In en, this message translates to:
  /// **'{type} match'**
  String warningsMatchLabel(String type);

  /// No description provided for @warningsVarianceLabel.
  ///
  /// In en, this message translates to:
  /// **'{value}% variance'**
  String warningsVarianceLabel(String value);

  /// No description provided for @erpStatusTitle.
  ///
  /// In en, this message translates to:
  /// **'ERP Status'**
  String get erpStatusTitle;

  /// No description provided for @erpStatusReference.
  ///
  /// In en, this message translates to:
  /// **'ERP Reference'**
  String get erpStatusReference;

  /// No description provided for @erpStatusDocumentId.
  ///
  /// In en, this message translates to:
  /// **'Document ID'**
  String get erpStatusDocumentId;

  /// No description provided for @erpStatusError.
  ///
  /// In en, this message translates to:
  /// **'Error'**
  String get erpStatusError;

  /// No description provided for @erpStatusLastUpdate.
  ///
  /// In en, this message translates to:
  /// **'Last update'**
  String get erpStatusLastUpdate;

  /// No description provided for @erpStatusStatus.
  ///
  /// In en, this message translates to:
  /// **'Status'**
  String get erpStatusStatus;

  /// No description provided for @fileViewerPdfTitle.
  ///
  /// In en, this message translates to:
  /// **'Invoice PDF'**
  String get fileViewerPdfTitle;

  /// No description provided for @fileViewerImageTitle.
  ///
  /// In en, this message translates to:
  /// **'Invoice Image'**
  String get fileViewerImageTitle;

  /// No description provided for @fileViewerPdfError.
  ///
  /// In en, this message translates to:
  /// **'Unable to load PDF'**
  String get fileViewerPdfError;

  /// No description provided for @fileViewerImageError.
  ///
  /// In en, this message translates to:
  /// **'Unable to load image'**
  String get fileViewerImageError;

  /// No description provided for @fileViewerRetry.
  ///
  /// In en, this message translates to:
  /// **'Retry'**
  String get fileViewerRetry;

  /// No description provided for @timelineNoActivity.
  ///
  /// In en, this message translates to:
  /// **'No activity yet'**
  String get timelineNoActivity;

  /// No description provided for @payTitle.
  ///
  /// In en, this message translates to:
  /// **'Pay'**
  String get payTitle;

  /// No description provided for @payTabQueue.
  ///
  /// In en, this message translates to:
  /// **'Queue'**
  String get payTabQueue;

  /// No description provided for @payTabRuns.
  ///
  /// In en, this message translates to:
  /// **'Runs'**
  String get payTabRuns;

  /// No description provided for @paySummaryTotalPaid.
  ///
  /// In en, this message translates to:
  /// **'Total Paid'**
  String get paySummaryTotalPaid;

  /// No description provided for @paySummaryPending.
  ///
  /// In en, this message translates to:
  /// **'Pending'**
  String get paySummaryPending;

  /// No description provided for @paySummaryInQueue.
  ///
  /// In en, this message translates to:
  /// **'In Queue'**
  String get paySummaryInQueue;

  /// No description provided for @paySummaryCardRebates.
  ///
  /// In en, this message translates to:
  /// **'Card Rebates'**
  String get paySummaryCardRebates;

  /// No description provided for @paySummaryPaymentsSubtitle.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} payment} other {{count} payments}}'**
  String paySummaryPaymentsSubtitle(int count);

  /// No description provided for @payQueueEmpty.
  ///
  /// In en, this message translates to:
  /// **'No invoices awaiting payment'**
  String get payQueueEmpty;

  /// No description provided for @payQueueError.
  ///
  /// In en, this message translates to:
  /// **'Could not load the payment queue'**
  String get payQueueError;

  /// No description provided for @payQueueRetry.
  ///
  /// In en, this message translates to:
  /// **'Retry'**
  String get payQueueRetry;

  /// No description provided for @payQueueDue.
  ///
  /// In en, this message translates to:
  /// **'Due {date}'**
  String payQueueDue(String date);

  /// No description provided for @payQueueNoDueDate.
  ///
  /// In en, this message translates to:
  /// **'No due date'**
  String get payQueueNoDueDate;

  /// No description provided for @payQueueDiscount.
  ///
  /// In en, this message translates to:
  /// **'discount {amount}'**
  String payQueueDiscount(String amount);

  /// No description provided for @payQueueOverdue.
  ///
  /// In en, this message translates to:
  /// **'overdue'**
  String get payQueueOverdue;

  /// No description provided for @payQueueSelected.
  ///
  /// In en, this message translates to:
  /// **'selected'**
  String get payQueueSelected;

  /// No description provided for @payQueueBlockedDuplicate.
  ///
  /// In en, this message translates to:
  /// **'Possible duplicate — unresolved'**
  String get payQueueBlockedDuplicate;

  /// No description provided for @payQueueBlockedFraudFlag.
  ///
  /// In en, this message translates to:
  /// **'Fraud flag — unresolved'**
  String get payQueueBlockedFraudFlag;

  /// No description provided for @payQueueBlockedLineTotalMismatch.
  ///
  /// In en, this message translates to:
  /// **'Line totals don’t match — unresolved'**
  String get payQueueBlockedLineTotalMismatch;

  /// No description provided for @payQueueBlockedPaymentReconciliation.
  ///
  /// In en, this message translates to:
  /// **'Earlier payment unreconciled — may still be in flight'**
  String get payQueueBlockedPaymentReconciliation;

  /// No description provided for @payQueueBlockedFullyCredited.
  ///
  /// In en, this message translates to:
  /// **'Fully covered by credit memos — nothing to pay'**
  String get payQueueBlockedFullyCredited;

  /// No description provided for @payQueueBlockedLiveVirtualCard.
  ///
  /// In en, this message translates to:
  /// **'A live virtual card claims this invoice — pay it by card'**
  String get payQueueBlockedLiveVirtualCard;

  /// No description provided for @payQueueBlockedGeneric.
  ///
  /// In en, this message translates to:
  /// **'Unresolved exception blocks payment'**
  String get payQueueBlockedGeneric;

  /// No description provided for @payQueueBlockedAnnounce.
  ///
  /// In en, this message translates to:
  /// **'can’t be paid: {reason}'**
  String payQueueBlockedAnnounce(String reason);

  /// No description provided for @payQueuePinnedMethod.
  ///
  /// In en, this message translates to:
  /// **'Pay by {method}'**
  String payQueuePinnedMethod(String method);

  /// No description provided for @payMethodLabel.
  ///
  /// In en, this message translates to:
  /// **'Payment method for {invoiceNumber}'**
  String payMethodLabel(String invoiceNumber);

  /// No description provided for @payMethodAch.
  ///
  /// In en, this message translates to:
  /// **'ACH'**
  String get payMethodAch;

  /// No description provided for @payMethodWire.
  ///
  /// In en, this message translates to:
  /// **'Wire'**
  String get payMethodWire;

  /// No description provided for @payMethodCheck.
  ///
  /// In en, this message translates to:
  /// **'Check'**
  String get payMethodCheck;

  /// No description provided for @payMethodVirtualCard.
  ///
  /// In en, this message translates to:
  /// **'Virtual Card'**
  String get payMethodVirtualCard;

  /// No description provided for @paySelectedCount.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} invoice selected} other {{count} invoices selected}}'**
  String paySelectedCount(int count);

  /// No description provided for @payClear.
  ///
  /// In en, this message translates to:
  /// **'Clear'**
  String get payClear;

  /// No description provided for @payCreateRun.
  ///
  /// In en, this message translates to:
  /// **'Create Run'**
  String get payCreateRun;

  /// No description provided for @payCreateRunFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to create run: {error}'**
  String payCreateRunFailed(String error);

  /// No description provided for @payRunsEmpty.
  ///
  /// In en, this message translates to:
  /// **'No payment runs'**
  String get payRunsEmpty;

  /// No description provided for @payRunSubtitle.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} payment} other {{count} payments}} • {date}'**
  String payRunSubtitle(int count, String date);

  /// No description provided for @payRunCfoRequiredSuffix.
  ///
  /// In en, this message translates to:
  /// **' • CFO approval required'**
  String get payRunCfoRequiredSuffix;

  /// No description provided for @payRunAnnounce.
  ///
  /// In en, this message translates to:
  /// **'Run {amount}, {status}, {subtitle}'**
  String payRunAnnounce(String amount, String status, String subtitle);

  /// No description provided for @payRunActions.
  ///
  /// In en, this message translates to:
  /// **'Run actions'**
  String get payRunActions;

  /// No description provided for @payRunActionExecute.
  ///
  /// In en, this message translates to:
  /// **'Execute'**
  String get payRunActionExecute;

  /// No description provided for @payRunActionCancel.
  ///
  /// In en, this message translates to:
  /// **'Cancel'**
  String get payRunActionCancel;

  /// No description provided for @payRunActionApprove.
  ///
  /// In en, this message translates to:
  /// **'Approve as CFO'**
  String get payRunActionApprove;

  /// No description provided for @payRunApproveTitle.
  ///
  /// In en, this message translates to:
  /// **'Approve payment run?'**
  String get payRunApproveTitle;

  /// No description provided for @payRunApproveBody.
  ///
  /// In en, this message translates to:
  /// **'Sign off the run created {date} — {count, plural, one {{count} payment} other {{count} payments}} totalling {amount}. This authorizes execution; it moves no money.'**
  String payRunApproveBody(String date, int count, String amount);

  /// No description provided for @payRunApproveConfirm.
  ///
  /// In en, this message translates to:
  /// **'Approve'**
  String get payRunApproveConfirm;

  /// No description provided for @payRunApproveFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to approve: {error}'**
  String payRunApproveFailed(String error);

  /// No description provided for @payRunCfoBlocked.
  ///
  /// In en, this message translates to:
  /// **'This run needs CFO approval before it can be executed.'**
  String get payRunCfoBlocked;

  /// No description provided for @payRunExecuteTitle.
  ///
  /// In en, this message translates to:
  /// **'Execute payment run?'**
  String get payRunExecuteTitle;

  /// No description provided for @payRunExecuteBody.
  ///
  /// In en, this message translates to:
  /// **'This sends {amount} via the configured payment processor.'**
  String payRunExecuteBody(String amount);

  /// No description provided for @payRunExecuteFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to execute: {error}'**
  String payRunExecuteFailed(String error);

  /// No description provided for @payRunCancelFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to cancel: {error}'**
  String payRunCancelFailed(String error);

  /// No description provided for @payRunStatusDraft.
  ///
  /// In en, this message translates to:
  /// **'Draft'**
  String get payRunStatusDraft;

  /// No description provided for @payRunStatusCompleted.
  ///
  /// In en, this message translates to:
  /// **'Completed'**
  String get payRunStatusCompleted;

  /// No description provided for @payRunStatusSubmitted.
  ///
  /// In en, this message translates to:
  /// **'Submitted'**
  String get payRunStatusSubmitted;

  /// No description provided for @payRunStatusPartial.
  ///
  /// In en, this message translates to:
  /// **'Partial'**
  String get payRunStatusPartial;

  /// No description provided for @payRunStatusFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed'**
  String get payRunStatusFailed;

  /// No description provided for @payRunStatusCancelled.
  ///
  /// In en, this message translates to:
  /// **'Cancelled'**
  String get payRunStatusCancelled;

  /// No description provided for @payConfirmCancel.
  ///
  /// In en, this message translates to:
  /// **'Cancel'**
  String get payConfirmCancel;

  /// No description provided for @payConfirmExecute.
  ///
  /// In en, this message translates to:
  /// **'Execute'**
  String get payConfirmExecute;

  /// No description provided for @loginAppName.
  ///
  /// In en, this message translates to:
  /// **'FeohLedger'**
  String get loginAppName;

  /// No description provided for @loginTagline.
  ///
  /// In en, this message translates to:
  /// **'Accounts Payable, Made Simple'**
  String get loginTagline;

  /// No description provided for @loginTenant.
  ///
  /// In en, this message translates to:
  /// **'Tenant'**
  String get loginTenant;

  /// No description provided for @loginEmail.
  ///
  /// In en, this message translates to:
  /// **'Email'**
  String get loginEmail;

  /// No description provided for @loginPassword.
  ///
  /// In en, this message translates to:
  /// **'Password'**
  String get loginPassword;

  /// No description provided for @loginShowPassword.
  ///
  /// In en, this message translates to:
  /// **'Show password'**
  String get loginShowPassword;

  /// No description provided for @loginHidePassword.
  ///
  /// In en, this message translates to:
  /// **'Hide password'**
  String get loginHidePassword;

  /// No description provided for @loginRequired.
  ///
  /// In en, this message translates to:
  /// **'Required'**
  String get loginRequired;

  /// No description provided for @loginSignIn.
  ///
  /// In en, this message translates to:
  /// **'Sign In'**
  String get loginSignIn;

  /// No description provided for @mfaTitle.
  ///
  /// In en, this message translates to:
  /// **'Two-factor authentication'**
  String get mfaTitle;

  /// No description provided for @mfaHeading.
  ///
  /// In en, this message translates to:
  /// **'Verify it\'s you'**
  String get mfaHeading;

  /// No description provided for @mfaPromptEmail.
  ///
  /// In en, this message translates to:
  /// **'Enter the 6-digit code we emailed you.'**
  String get mfaPromptEmail;

  /// No description provided for @mfaPromptTotp.
  ///
  /// In en, this message translates to:
  /// **'Enter the 6-digit code from your authenticator app.'**
  String get mfaPromptTotp;

  /// No description provided for @mfaEnforcedNotice.
  ///
  /// In en, this message translates to:
  /// **'Your organization requires two-factor authentication. Verify with an email code now, then finish setting up an authenticator app in the web app.'**
  String get mfaEnforcedNotice;

  /// No description provided for @mfaCode.
  ///
  /// In en, this message translates to:
  /// **'Code'**
  String get mfaCode;

  /// No description provided for @mfaCodeRequired.
  ///
  /// In en, this message translates to:
  /// **'Required'**
  String get mfaCodeRequired;

  /// No description provided for @mfaCodeTooShort.
  ///
  /// In en, this message translates to:
  /// **'Enter at least 6 digits'**
  String get mfaCodeTooShort;

  /// No description provided for @mfaVerify.
  ///
  /// In en, this message translates to:
  /// **'Verify'**
  String get mfaVerify;

  /// No description provided for @mfaSending.
  ///
  /// In en, this message translates to:
  /// **'Sending…'**
  String get mfaSending;

  /// No description provided for @mfaResendEmailCode.
  ///
  /// In en, this message translates to:
  /// **'Resend email code'**
  String get mfaResendEmailCode;

  /// No description provided for @mfaSendEmailCode.
  ///
  /// In en, this message translates to:
  /// **'Send email code'**
  String get mfaSendEmailCode;

  /// No description provided for @mfaUseEmailInstead.
  ///
  /// In en, this message translates to:
  /// **'Use an email code instead'**
  String get mfaUseEmailInstead;

  /// No description provided for @mfaUseAuthenticatorInstead.
  ///
  /// In en, this message translates to:
  /// **'Use authenticator app instead'**
  String get mfaUseAuthenticatorInstead;

  /// No description provided for @mfaEmailedAnnounce.
  ///
  /// In en, this message translates to:
  /// **'A sign-in code was emailed to you.'**
  String get mfaEmailedAnnounce;

  /// No description provided for @adminUsersTitle.
  ///
  /// In en, this message translates to:
  /// **'User Management'**
  String get adminUsersTitle;

  /// No description provided for @adminUsersSearchHint.
  ///
  /// In en, this message translates to:
  /// **'Search by name or email'**
  String get adminUsersSearchHint;

  /// No description provided for @adminUsersEmpty.
  ///
  /// In en, this message translates to:
  /// **'No users found'**
  String get adminUsersEmpty;

  /// No description provided for @adminUsersLoadError.
  ///
  /// In en, this message translates to:
  /// **'Could not load users'**
  String get adminUsersLoadError;

  /// No description provided for @adminUsersEditRoles.
  ///
  /// In en, this message translates to:
  /// **'Edit roles'**
  String get adminUsersEditRoles;

  /// No description provided for @adminUsersNoRoles.
  ///
  /// In en, this message translates to:
  /// **'No roles'**
  String get adminUsersNoRoles;

  /// No description provided for @adminUsersDeactivate.
  ///
  /// In en, this message translates to:
  /// **'Deactivate user'**
  String get adminUsersDeactivate;

  /// No description provided for @adminUsersActivate.
  ///
  /// In en, this message translates to:
  /// **'Activate user'**
  String get adminUsersActivate;

  /// No description provided for @adminUsersCannotDeactivateSelf.
  ///
  /// In en, this message translates to:
  /// **'You can\'t deactivate your own account'**
  String get adminUsersCannotDeactivateSelf;

  /// No description provided for @adminUsersDeactivateHint.
  ///
  /// In en, this message translates to:
  /// **'Signs them out and blocks sign-in'**
  String get adminUsersDeactivateHint;

  /// No description provided for @adminUsersActivateHint.
  ///
  /// In en, this message translates to:
  /// **'Restores sign-in access'**
  String get adminUsersActivateHint;

  /// No description provided for @adminUsersRoleActive.
  ///
  /// In en, this message translates to:
  /// **'active'**
  String get adminUsersRoleActive;

  /// No description provided for @adminUsersRoleInactive.
  ///
  /// In en, this message translates to:
  /// **'inactive'**
  String get adminUsersRoleInactive;

  /// No description provided for @adminUsersInactiveBadge.
  ///
  /// In en, this message translates to:
  /// **'Inactive'**
  String get adminUsersInactiveBadge;

  /// No description provided for @adminUsersRolesUpdated.
  ///
  /// In en, this message translates to:
  /// **'Updated roles for {name}'**
  String adminUsersRolesUpdated(String name);

  /// No description provided for @adminUsersRolesUpdateFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to update roles: {error}'**
  String adminUsersRolesUpdateFailed(String error);

  /// No description provided for @adminUsersActivated.
  ///
  /// In en, this message translates to:
  /// **'Activated {name}'**
  String adminUsersActivated(String name);

  /// No description provided for @adminUsersDeactivated.
  ///
  /// In en, this message translates to:
  /// **'Deactivated {name}'**
  String adminUsersDeactivated(String name);

  /// No description provided for @adminUsersUpdateFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to update {name}: {error}'**
  String adminUsersUpdateFailed(String name, String error);

  /// No description provided for @adminUsersCreateUser.
  ///
  /// In en, this message translates to:
  /// **'Create user'**
  String get adminUsersCreateUser;

  /// No description provided for @adminUsersCreateTitle.
  ///
  /// In en, this message translates to:
  /// **'New user'**
  String get adminUsersCreateTitle;

  /// No description provided for @adminUsersFieldFullName.
  ///
  /// In en, this message translates to:
  /// **'Full name'**
  String get adminUsersFieldFullName;

  /// No description provided for @adminUsersFieldEmail.
  ///
  /// In en, this message translates to:
  /// **'Email'**
  String get adminUsersFieldEmail;

  /// No description provided for @adminUsersFieldRoles.
  ///
  /// In en, this message translates to:
  /// **'Roles'**
  String get adminUsersFieldRoles;

  /// No description provided for @adminUsersValidationNameRequired.
  ///
  /// In en, this message translates to:
  /// **'Full name is required'**
  String get adminUsersValidationNameRequired;

  /// No description provided for @adminUsersValidationEmailRequired.
  ///
  /// In en, this message translates to:
  /// **'Email is required'**
  String get adminUsersValidationEmailRequired;

  /// No description provided for @adminUsersValidationEmailInvalid.
  ///
  /// In en, this message translates to:
  /// **'Enter a valid email address'**
  String get adminUsersValidationEmailInvalid;

  /// No description provided for @adminUsersCreateSubmit.
  ///
  /// In en, this message translates to:
  /// **'Create'**
  String get adminUsersCreateSubmit;

  /// No description provided for @adminUsersCreating.
  ///
  /// In en, this message translates to:
  /// **'Creating…'**
  String get adminUsersCreating;

  /// No description provided for @adminUsersCreated.
  ///
  /// In en, this message translates to:
  /// **'Created {name}'**
  String adminUsersCreated(String name);

  /// No description provided for @adminUsersCreateFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to create user: {error}'**
  String adminUsersCreateFailed(String error);

  /// No description provided for @adminUsersTempPasswordTitle.
  ///
  /// In en, this message translates to:
  /// **'User created'**
  String get adminUsersTempPasswordTitle;

  /// No description provided for @adminUsersTempPasswordBody.
  ///
  /// In en, this message translates to:
  /// **'Share this one-time password with {name}. They\'ll be asked to change it on first sign-in. It won\'t be shown again.'**
  String adminUsersTempPasswordBody(String name);

  /// No description provided for @adminUsersDelete.
  ///
  /// In en, this message translates to:
  /// **'Delete user'**
  String get adminUsersDelete;

  /// No description provided for @adminUsersDeleteHint.
  ///
  /// In en, this message translates to:
  /// **'Permanently removes this account'**
  String get adminUsersDeleteHint;

  /// No description provided for @adminUsersCannotDeleteSelf.
  ///
  /// In en, this message translates to:
  /// **'You can\'t delete your own account'**
  String get adminUsersCannotDeleteSelf;

  /// No description provided for @adminUsersDeleteConfirmTitle.
  ///
  /// In en, this message translates to:
  /// **'Delete {name}?'**
  String adminUsersDeleteConfirmTitle(String name);

  /// No description provided for @adminUsersDeleteConfirmBody.
  ///
  /// In en, this message translates to:
  /// **'This permanently removes {name} ({email}). This can\'t be undone.'**
  String adminUsersDeleteConfirmBody(String name, String email);

  /// No description provided for @adminUsersDeleted.
  ///
  /// In en, this message translates to:
  /// **'Deleted {name}'**
  String adminUsersDeleted(String name);

  /// No description provided for @adminUsersDeleteFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to delete {name}: {error}'**
  String adminUsersDeleteFailed(String name, String error);

  /// No description provided for @orgSettingsTitle.
  ///
  /// In en, this message translates to:
  /// **'Organization Settings'**
  String get orgSettingsTitle;

  /// No description provided for @orgSettingsNoSettings.
  ///
  /// In en, this message translates to:
  /// **'No settings'**
  String get orgSettingsNoSettings;

  /// No description provided for @orgSettingsLoadError.
  ///
  /// In en, this message translates to:
  /// **'Could not load settings'**
  String get orgSettingsLoadError;

  /// No description provided for @orgSettingsSectionCompany.
  ///
  /// In en, this message translates to:
  /// **'Company'**
  String get orgSettingsSectionCompany;

  /// No description provided for @orgSettingsSectionInvoiceDefaults.
  ///
  /// In en, this message translates to:
  /// **'Invoice defaults'**
  String get orgSettingsSectionInvoiceDefaults;

  /// No description provided for @orgSettingsName.
  ///
  /// In en, this message translates to:
  /// **'Organization name'**
  String get orgSettingsName;

  /// No description provided for @orgSettingsAddress.
  ///
  /// In en, this message translates to:
  /// **'Address'**
  String get orgSettingsAddress;

  /// No description provided for @orgSettingsPhone.
  ///
  /// In en, this message translates to:
  /// **'Phone'**
  String get orgSettingsPhone;

  /// No description provided for @orgSettingsWebsite.
  ///
  /// In en, this message translates to:
  /// **'Website'**
  String get orgSettingsWebsite;

  /// No description provided for @orgSettingsTaxId.
  ///
  /// In en, this message translates to:
  /// **'Tax ID'**
  String get orgSettingsTaxId;

  /// No description provided for @orgSettingsCurrency.
  ///
  /// In en, this message translates to:
  /// **'Default currency'**
  String get orgSettingsCurrency;

  /// No description provided for @orgSettingsPaymentTerms.
  ///
  /// In en, this message translates to:
  /// **'Payment terms'**
  String get orgSettingsPaymentTerms;

  /// No description provided for @orgSettingsNumberPrefix.
  ///
  /// In en, this message translates to:
  /// **'Invoice number prefix'**
  String get orgSettingsNumberPrefix;

  /// No description provided for @orgSettingsGlAccount.
  ///
  /// In en, this message translates to:
  /// **'Default GL account'**
  String get orgSettingsGlAccount;

  /// No description provided for @orgSettingsCostCenter.
  ///
  /// In en, this message translates to:
  /// **'Default cost center'**
  String get orgSettingsCostCenter;

  /// No description provided for @orgSettingsSave.
  ///
  /// In en, this message translates to:
  /// **'Save changes'**
  String get orgSettingsSave;

  /// No description provided for @orgSettingsSaving.
  ///
  /// In en, this message translates to:
  /// **'Saving…'**
  String get orgSettingsSaving;

  /// No description provided for @orgSettingsFieldRequired.
  ///
  /// In en, this message translates to:
  /// **'{label} is required'**
  String orgSettingsFieldRequired(String label);

  /// No description provided for @orgSettingsSaved.
  ///
  /// In en, this message translates to:
  /// **'Organization settings saved'**
  String get orgSettingsSaved;

  /// No description provided for @orgSettingsSaveFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed to save: {error}'**
  String orgSettingsSaveFailed(String error);

  /// No description provided for @workflowsTitle.
  ///
  /// In en, this message translates to:
  /// **'Workflows'**
  String get workflowsTitle;

  /// No description provided for @workflowsEmpty.
  ///
  /// In en, this message translates to:
  /// **'No workflows found'**
  String get workflowsEmpty;

  /// No description provided for @workflowsLoadError.
  ///
  /// In en, this message translates to:
  /// **'Could not load workflows'**
  String get workflowsLoadError;

  /// No description provided for @workflowsStatusActive.
  ///
  /// In en, this message translates to:
  /// **'Active'**
  String get workflowsStatusActive;

  /// No description provided for @workflowsStatusInactive.
  ///
  /// In en, this message translates to:
  /// **'Inactive'**
  String get workflowsStatusInactive;

  /// No description provided for @workflowsDefault.
  ///
  /// In en, this message translates to:
  /// **'Default'**
  String get workflowsDefault;

  /// No description provided for @workflowsStepCount.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} step} other {{count} steps}}'**
  String workflowsStepCount(int count);

  /// No description provided for @workflowDetailFallbackTitle.
  ///
  /// In en, this message translates to:
  /// **'Workflow'**
  String get workflowDetailFallbackTitle;

  /// No description provided for @workflowDetailLoadError.
  ///
  /// In en, this message translates to:
  /// **'Could not load workflow'**
  String get workflowDetailLoadError;

  /// No description provided for @workflowDetailNoSteps.
  ///
  /// In en, this message translates to:
  /// **'This workflow has no steps.'**
  String get workflowDetailNoSteps;

  /// No description provided for @workflowDetailDefaultWorkflow.
  ///
  /// In en, this message translates to:
  /// **'Default workflow'**
  String get workflowDetailDefaultWorkflow;

  /// No description provided for @workflowDetailStepNumber.
  ///
  /// In en, this message translates to:
  /// **'Step {number}'**
  String workflowDetailStepNumber(int number);

  /// No description provided for @workflowDetailStepEnabled.
  ///
  /// In en, this message translates to:
  /// **'Enabled'**
  String get workflowDetailStepEnabled;

  /// No description provided for @workflowDetailStepDisabled.
  ///
  /// In en, this message translates to:
  /// **'Disabled'**
  String get workflowDetailStepDisabled;

  /// No description provided for @workflowDetailApproverCount.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} approver} other {{count} approvers}}'**
  String workflowDetailApproverCount(int count);

  /// No description provided for @workflowDetailDelaySummary.
  ///
  /// In en, this message translates to:
  /// **'Delay {hours} h'**
  String workflowDetailDelaySummary(String hours);

  /// No description provided for @workflowDetailConditionSummary.
  ///
  /// In en, this message translates to:
  /// **'On {field}'**
  String workflowDetailConditionSummary(String field);

  /// No description provided for @cashFlowTitle.
  ///
  /// In en, this message translates to:
  /// **'Cash Flow Forecast'**
  String get cashFlowTitle;

  /// No description provided for @cashFlowErrorPrefix.
  ///
  /// In en, this message translates to:
  /// **'Error: {error}'**
  String cashFlowErrorPrefix(String error);

  /// No description provided for @cashFlowHorizonDays.
  ///
  /// In en, this message translates to:
  /// **'{days} days'**
  String cashFlowHorizonDays(int days);

  /// No description provided for @cashFlowLowBalanceAlert.
  ///
  /// In en, this message translates to:
  /// **'Low balance alert'**
  String get cashFlowLowBalanceAlert;

  /// No description provided for @cashFlowBreachSingle.
  ///
  /// In en, this message translates to:
  /// **'Projected to fall below the {threshold} balance in {period} (shortfall {shortfall}).'**
  String cashFlowBreachSingle(
    String threshold,
    String period,
    String shortfall,
  );

  /// No description provided for @cashFlowBreachMultiple.
  ///
  /// In en, this message translates to:
  /// **'{count} periods are projected to fall below the minimum balance. Worst: {period}, shortfall {shortfall}.'**
  String cashFlowBreachMultiple(int count, String period, String shortfall);

  /// No description provided for @cashFlowMinimum.
  ///
  /// In en, this message translates to:
  /// **'minimum'**
  String get cashFlowMinimum;

  /// No description provided for @cashFlowOpeningBalance.
  ///
  /// In en, this message translates to:
  /// **'Opening Balance'**
  String get cashFlowOpeningBalance;

  /// No description provided for @cashFlowProjectedEnd.
  ///
  /// In en, this message translates to:
  /// **'Projected End'**
  String get cashFlowProjectedEnd;

  /// No description provided for @cashFlowProjectedEndSubtitle.
  ///
  /// In en, this message translates to:
  /// **'in {days} days'**
  String cashFlowProjectedEndSubtitle(int days);

  /// No description provided for @cashFlowCommittedOut.
  ///
  /// In en, this message translates to:
  /// **'Committed Out'**
  String get cashFlowCommittedOut;

  /// No description provided for @cashFlowCommittedSubtitle.
  ///
  /// In en, this message translates to:
  /// **'firm commitments'**
  String get cashFlowCommittedSubtitle;

  /// No description provided for @cashFlowPendingOut.
  ///
  /// In en, this message translates to:
  /// **'Pending Out'**
  String get cashFlowPendingOut;

  /// No description provided for @cashFlowPendingSubtitle.
  ///
  /// In en, this message translates to:
  /// **'in-flight pipeline'**
  String get cashFlowPendingSubtitle;

  /// No description provided for @cashFlowOpeningSourceProvider.
  ///
  /// In en, this message translates to:
  /// **'synced from bank'**
  String get cashFlowOpeningSourceProvider;

  /// No description provided for @cashFlowOpeningSourceSettings.
  ///
  /// In en, this message translates to:
  /// **'saved balance'**
  String get cashFlowOpeningSourceSettings;

  /// No description provided for @cashFlowOpeningSourceQuery.
  ///
  /// In en, this message translates to:
  /// **'manual'**
  String get cashFlowOpeningSourceQuery;

  /// No description provided for @cashFlowOpeningSourceUnset.
  ///
  /// In en, this message translates to:
  /// **'set a balance'**
  String get cashFlowOpeningSourceUnset;

  /// No description provided for @cashFlowProjectedOutflows.
  ///
  /// In en, this message translates to:
  /// **'Projected Outflows'**
  String get cashFlowProjectedOutflows;

  /// No description provided for @cashFlowNoOutflows.
  ///
  /// In en, this message translates to:
  /// **'No projected outflows in this horizon.'**
  String get cashFlowNoOutflows;

  /// No description provided for @cashFlowInvoiceCount.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} invoice} other {{count} invoices}}'**
  String cashFlowInvoiceCount(int count);

  /// No description provided for @cashFlowCommittedAmount.
  ///
  /// In en, this message translates to:
  /// **'committed {amount}'**
  String cashFlowCommittedAmount(String amount);

  /// No description provided for @cashFlowPendingAmount.
  ///
  /// In en, this message translates to:
  /// **'pending {amount}'**
  String cashFlowPendingAmount(String amount);

  /// No description provided for @cashFlowPosition.
  ///
  /// In en, this message translates to:
  /// **'Cash Position'**
  String get cashFlowPosition;

  /// No description provided for @cashFlowNoPosition.
  ///
  /// In en, this message translates to:
  /// **'No cash-position projection for this horizon.'**
  String get cashFlowNoPosition;

  /// No description provided for @cashFlowOutAmount.
  ///
  /// In en, this message translates to:
  /// **'out {amount}'**
  String cashFlowOutAmount(String amount);

  /// No description provided for @cashFlowForecastRowLabel.
  ///
  /// In en, this message translates to:
  /// **'{period}: scheduled {scheduled}, committed {committed}, pending {pending}, {count} invoices'**
  String cashFlowForecastRowLabel(
    String period,
    String scheduled,
    String committed,
    String pending,
    int count,
  );

  /// No description provided for @cashFlowPositionRowLabel.
  ///
  /// In en, this message translates to:
  /// **'{period}: opening {opening}, outflow {outflow}, closing {closing}'**
  String cashFlowPositionRowLabel(
    String period,
    String opening,
    String outflow,
    String closing,
  );

  /// No description provided for @cashFlowBelowThresholdSuffix.
  ///
  /// In en, this message translates to:
  /// **', below threshold'**
  String get cashFlowBelowThresholdSuffix;

  /// No description provided for @cashFlowLowBalanceAlertLabel.
  ///
  /// In en, this message translates to:
  /// **'Low balance alert. {message}'**
  String cashFlowLowBalanceAlertLabel(String message);

  /// No description provided for @cashFlowForecastUnconverted.
  ///
  /// In en, this message translates to:
  /// **'Partial: {count, plural, one {{count} invoice} other {{count} invoices}} with no exchange rate into {currency}, counted at face value — these outflows, and the committed and pending totals above, mix currencies by that much.'**
  String cashFlowForecastUnconverted(int count, String currency);

  /// No description provided for @cashFlowPositionUnconverted.
  ///
  /// In en, this message translates to:
  /// **'Unconverted: {count, plural, one {{count} invoice} other {{count} invoices}} with no exchange rate into {currency}, counted at face value — so the running balance below, and the projected end balance above, mix currencies. Book the missing rate before acting on these figures.'**
  String cashFlowPositionUnconverted(int count, String currency);

  /// No description provided for @contractsTitle.
  ///
  /// In en, this message translates to:
  /// **'Contracts'**
  String get contractsTitle;

  /// No description provided for @contractsSearchHint.
  ///
  /// In en, this message translates to:
  /// **'Search contracts...'**
  String get contractsSearchHint;

  /// No description provided for @contractsEmpty.
  ///
  /// In en, this message translates to:
  /// **'No contracts found'**
  String get contractsEmpty;

  /// No description provided for @contractsFilterDraft.
  ///
  /// In en, this message translates to:
  /// **'Draft'**
  String get contractsFilterDraft;

  /// No description provided for @contractsFilterActive.
  ///
  /// In en, this message translates to:
  /// **'Active'**
  String get contractsFilterActive;

  /// No description provided for @contractsFilterExpired.
  ///
  /// In en, this message translates to:
  /// **'Expired'**
  String get contractsFilterExpired;

  /// No description provided for @contractsFilterTerminated.
  ///
  /// In en, this message translates to:
  /// **'Terminated'**
  String get contractsFilterTerminated;

  /// No description provided for @contractsFilterCancelled.
  ///
  /// In en, this message translates to:
  /// **'Cancelled'**
  String get contractsFilterCancelled;

  /// No description provided for @contractDetailTitle.
  ///
  /// In en, this message translates to:
  /// **'Contract Detail'**
  String get contractDetailTitle;

  /// No description provided for @contractDetailErrorPrefix.
  ///
  /// In en, this message translates to:
  /// **'Error: {error}'**
  String contractDetailErrorPrefix(String error);

  /// No description provided for @contractDetailUntitled.
  ///
  /// In en, this message translates to:
  /// **'Untitled Contract'**
  String get contractDetailUntitled;

  /// No description provided for @contractDetailFieldContractNumber.
  ///
  /// In en, this message translates to:
  /// **'Contract #'**
  String get contractDetailFieldContractNumber;

  /// No description provided for @contractDetailFieldVendor.
  ///
  /// In en, this message translates to:
  /// **'Vendor'**
  String get contractDetailFieldVendor;

  /// No description provided for @contractDetailFieldType.
  ///
  /// In en, this message translates to:
  /// **'Type'**
  String get contractDetailFieldType;

  /// No description provided for @contractDetailFieldCurrency.
  ///
  /// In en, this message translates to:
  /// **'Currency'**
  String get contractDetailFieldCurrency;

  /// No description provided for @contractDetailFieldSpendLimit.
  ///
  /// In en, this message translates to:
  /// **'Spend Limit'**
  String get contractDetailFieldSpendLimit;

  /// No description provided for @contractDetailNotToExceed.
  ///
  /// In en, this message translates to:
  /// **' (not to exceed)'**
  String get contractDetailNotToExceed;

  /// No description provided for @contractDetailFieldStartDate.
  ///
  /// In en, this message translates to:
  /// **'Start Date'**
  String get contractDetailFieldStartDate;

  /// No description provided for @contractDetailFieldEndDate.
  ///
  /// In en, this message translates to:
  /// **'End Date'**
  String get contractDetailFieldEndDate;

  /// No description provided for @contractDetailFieldSigned.
  ///
  /// In en, this message translates to:
  /// **'Signed'**
  String get contractDetailFieldSigned;

  /// No description provided for @contractDetailFieldAutoRenew.
  ///
  /// In en, this message translates to:
  /// **'Auto-Renew'**
  String get contractDetailFieldAutoRenew;

  /// No description provided for @contractDetailYes.
  ///
  /// In en, this message translates to:
  /// **'Yes'**
  String get contractDetailYes;

  /// No description provided for @contractDetailNo.
  ///
  /// In en, this message translates to:
  /// **'No'**
  String get contractDetailNo;

  /// No description provided for @contractDetailFieldRenewalTerm.
  ///
  /// In en, this message translates to:
  /// **'Renewal Term'**
  String get contractDetailFieldRenewalTerm;

  /// No description provided for @contractDetailRenewalTermMonths.
  ///
  /// In en, this message translates to:
  /// **'{months} months'**
  String contractDetailRenewalTermMonths(int months);

  /// No description provided for @contractDetailFieldRenewalNotice.
  ///
  /// In en, this message translates to:
  /// **'Renewal Notice'**
  String get contractDetailFieldRenewalNotice;

  /// No description provided for @contractDetailRenewalNoticeDays.
  ///
  /// In en, this message translates to:
  /// **'{days} days'**
  String contractDetailRenewalNoticeDays(int days);

  /// No description provided for @contractDetailFieldPaymentTerms.
  ///
  /// In en, this message translates to:
  /// **'Payment Terms'**
  String get contractDetailFieldPaymentTerms;

  /// No description provided for @contractDetailFieldDescription.
  ///
  /// In en, this message translates to:
  /// **'Description'**
  String get contractDetailFieldDescription;

  /// No description provided for @contractDetailFieldCreated.
  ///
  /// In en, this message translates to:
  /// **'Created'**
  String get contractDetailFieldCreated;

  /// No description provided for @contractDetailSectionSpend.
  ///
  /// In en, this message translates to:
  /// **'Spend'**
  String get contractDetailSectionSpend;

  /// No description provided for @contractDetailSectionLineItems.
  ///
  /// In en, this message translates to:
  /// **'Line Items'**
  String get contractDetailSectionLineItems;

  /// No description provided for @contractDetailSpendInvoiced.
  ///
  /// In en, this message translates to:
  /// **'Invoiced'**
  String get contractDetailSpendInvoiced;

  /// No description provided for @contractDetailSpendInvoiceCount.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} invoice} other {{count} invoices}}'**
  String contractDetailSpendInvoiceCount(int count);

  /// No description provided for @contractDetailSpendOverLimit.
  ///
  /// In en, this message translates to:
  /// **'Over Limit'**
  String get contractDetailSpendOverLimit;

  /// No description provided for @contractDetailSpendRemaining.
  ///
  /// In en, this message translates to:
  /// **'Remaining'**
  String get contractDetailSpendRemaining;

  /// No description provided for @contractDetailSpendOfLimit.
  ///
  /// In en, this message translates to:
  /// **'of {limit}'**
  String contractDetailSpendOfLimit(String limit);

  /// No description provided for @contractDetailSpendNoLimit.
  ///
  /// In en, this message translates to:
  /// **'no limit set'**
  String get contractDetailSpendNoLimit;

  /// No description provided for @contractDetailLineItemFallback.
  ///
  /// In en, this message translates to:
  /// **'Line item'**
  String get contractDetailLineItemFallback;

  /// No description provided for @contractDetailLineQty.
  ///
  /// In en, this message translates to:
  /// **'Qty {quantity}'**
  String contractDetailLineQty(String quantity);

  /// No description provided for @contractDetailLineUnitPrice.
  ///
  /// In en, this message translates to:
  /// **'@ {price}'**
  String contractDetailLineUnitPrice(String price);

  /// No description provided for @contractDetailLineGl.
  ///
  /// In en, this message translates to:
  /// **'GL {account}'**
  String contractDetailLineGl(String account);

  /// No description provided for @contractActivate.
  ///
  /// In en, this message translates to:
  /// **'Activate'**
  String get contractActivate;

  /// No description provided for @contractActivated.
  ///
  /// In en, this message translates to:
  /// **'Contract activated'**
  String get contractActivated;

  /// No description provided for @contractActivateFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not activate contract — please try again'**
  String get contractActivateFailed;

  /// No description provided for @contractTerminate.
  ///
  /// In en, this message translates to:
  /// **'Terminate'**
  String get contractTerminate;

  /// No description provided for @contractTerminateTitle.
  ///
  /// In en, this message translates to:
  /// **'Terminate Contract'**
  String get contractTerminateTitle;

  /// No description provided for @contractTerminateBody.
  ///
  /// In en, this message translates to:
  /// **'This ends the contract early. This cannot be undone. Continue?'**
  String get contractTerminateBody;

  /// No description provided for @contractTerminated.
  ///
  /// In en, this message translates to:
  /// **'Contract terminated'**
  String get contractTerminated;

  /// No description provided for @contractTerminateFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not terminate contract — please try again'**
  String get contractTerminateFailed;

  /// No description provided for @exceptionDetailTitle.
  ///
  /// In en, this message translates to:
  /// **'Exception'**
  String get exceptionDetailTitle;

  /// No description provided for @exceptionDetailNotFound.
  ///
  /// In en, this message translates to:
  /// **'Exception not found'**
  String get exceptionDetailNotFound;

  /// No description provided for @exceptionDetailOverdue.
  ///
  /// In en, this message translates to:
  /// **'Overdue'**
  String get exceptionDetailOverdue;

  /// No description provided for @exceptionDetailSectionDescription.
  ///
  /// In en, this message translates to:
  /// **'Description'**
  String get exceptionDetailSectionDescription;

  /// No description provided for @exceptionDetailSectionInvoice.
  ///
  /// In en, this message translates to:
  /// **'Invoice'**
  String get exceptionDetailSectionInvoice;

  /// No description provided for @exceptionDetailNoLinkedInvoice.
  ///
  /// In en, this message translates to:
  /// **'No linked invoice'**
  String get exceptionDetailNoLinkedInvoice;

  /// No description provided for @exceptionDetailFieldNumber.
  ///
  /// In en, this message translates to:
  /// **'Number'**
  String get exceptionDetailFieldNumber;

  /// No description provided for @exceptionDetailFieldVendor.
  ///
  /// In en, this message translates to:
  /// **'Vendor'**
  String get exceptionDetailFieldVendor;

  /// No description provided for @exceptionDetailFieldAmount.
  ///
  /// In en, this message translates to:
  /// **'Amount'**
  String get exceptionDetailFieldAmount;

  /// No description provided for @exceptionDetailFieldSeverity.
  ///
  /// In en, this message translates to:
  /// **'Severity'**
  String get exceptionDetailFieldSeverity;

  /// No description provided for @exceptionDetailSectionSla.
  ///
  /// In en, this message translates to:
  /// **'SLA'**
  String get exceptionDetailSectionSla;

  /// No description provided for @exceptionDetailFieldCreated.
  ///
  /// In en, this message translates to:
  /// **'Created'**
  String get exceptionDetailFieldCreated;

  /// No description provided for @exceptionDetailFieldDue.
  ///
  /// In en, this message translates to:
  /// **'Due'**
  String get exceptionDetailFieldDue;

  /// No description provided for @exceptionDetailNoSla.
  ///
  /// In en, this message translates to:
  /// **'No SLA set'**
  String get exceptionDetailNoSla;

  /// No description provided for @exceptionDetailFieldStatus.
  ///
  /// In en, this message translates to:
  /// **'Status'**
  String get exceptionDetailFieldStatus;

  /// No description provided for @exceptionDetailOnTrack.
  ///
  /// In en, this message translates to:
  /// **'On track'**
  String get exceptionDetailOnTrack;

  /// No description provided for @exceptionDetailResolvedIn.
  ///
  /// In en, this message translates to:
  /// **'Resolved in'**
  String get exceptionDetailResolvedIn;

  /// No description provided for @exceptionDetailResolvedInHours.
  ///
  /// In en, this message translates to:
  /// **'{hours} h'**
  String exceptionDetailResolvedInHours(String hours);

  /// No description provided for @exceptionDetailSectionAssignee.
  ///
  /// In en, this message translates to:
  /// **'Assignee'**
  String get exceptionDetailSectionAssignee;

  /// No description provided for @exceptionDetailUnassigned.
  ///
  /// In en, this message translates to:
  /// **'Unassigned'**
  String get exceptionDetailUnassigned;

  /// No description provided for @exceptionDetailAssign.
  ///
  /// In en, this message translates to:
  /// **'Assign'**
  String get exceptionDetailAssign;

  /// No description provided for @exceptionDetailReassign.
  ///
  /// In en, this message translates to:
  /// **'Reassign'**
  String get exceptionDetailReassign;

  /// No description provided for @exceptionDetailSectionResolution.
  ///
  /// In en, this message translates to:
  /// **'Resolution'**
  String get exceptionDetailSectionResolution;

  /// No description provided for @exceptionDetailResolutionNote.
  ///
  /// In en, this message translates to:
  /// **'Note'**
  String get exceptionDetailResolutionNote;

  /// No description provided for @exceptionDetailResolutionBy.
  ///
  /// In en, this message translates to:
  /// **'By'**
  String get exceptionDetailResolutionBy;

  /// No description provided for @exceptionDetailResolutionAt.
  ///
  /// In en, this message translates to:
  /// **'At'**
  String get exceptionDetailResolutionAt;

  /// No description provided for @exceptionDetailActionResolved.
  ///
  /// In en, this message translates to:
  /// **'Exception resolved'**
  String get exceptionDetailActionResolved;

  /// No description provided for @exceptionDetailActionEscalated.
  ///
  /// In en, this message translates to:
  /// **'Exception escalated'**
  String get exceptionDetailActionEscalated;

  /// No description provided for @exceptionDetailActionDismissed.
  ///
  /// In en, this message translates to:
  /// **'Exception dismissed'**
  String get exceptionDetailActionDismissed;

  /// No description provided for @exceptionDetailActionResolveFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not resolve the exception'**
  String get exceptionDetailActionResolveFailed;

  /// No description provided for @exceptionDetailActionEscalateFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not escalate the exception'**
  String get exceptionDetailActionEscalateFailed;

  /// No description provided for @exceptionDetailActionDismissFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not dismiss the exception'**
  String get exceptionDetailActionDismissFailed;

  /// No description provided for @exceptionDetailAssignTo.
  ///
  /// In en, this message translates to:
  /// **'Assign to'**
  String get exceptionDetailAssignTo;

  /// No description provided for @exceptionDetailUnassign.
  ///
  /// In en, this message translates to:
  /// **'Unassign'**
  String get exceptionDetailUnassign;

  /// No description provided for @exceptionDetailLoadUsersFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not load users: {error}'**
  String exceptionDetailLoadUsersFailed(String error);

  /// No description provided for @exceptionDetailAssigneeUpdateFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not update the assignee'**
  String get exceptionDetailAssigneeUpdateFailed;

  /// No description provided for @exceptionDetailUnassigned2.
  ///
  /// In en, this message translates to:
  /// **'Exception unassigned'**
  String get exceptionDetailUnassigned2;

  /// No description provided for @exceptionDetailAssignedTo.
  ///
  /// In en, this message translates to:
  /// **'Assigned to {name}'**
  String exceptionDetailAssignedTo(String name);

  /// No description provided for @settingsProcurement.
  ///
  /// In en, this message translates to:
  /// **'Procurement'**
  String get settingsProcurement;

  /// No description provided for @settingsInspections.
  ///
  /// In en, this message translates to:
  /// **'Quality Inspections'**
  String get settingsInspections;

  /// No description provided for @settingsInspectionsHint.
  ///
  /// In en, this message translates to:
  /// **'Record and review 4-way match inspections'**
  String get settingsInspectionsHint;

  /// No description provided for @settingsAdministration.
  ///
  /// In en, this message translates to:
  /// **'Administration'**
  String get settingsAdministration;

  /// No description provided for @settingsAdminUsers.
  ///
  /// In en, this message translates to:
  /// **'User Management'**
  String get settingsAdminUsers;

  /// No description provided for @settingsAdminUsersHint.
  ///
  /// In en, this message translates to:
  /// **'Roles, activate / deactivate users'**
  String get settingsAdminUsersHint;

  /// No description provided for @settingsAdminOrg.
  ///
  /// In en, this message translates to:
  /// **'Organization Settings'**
  String get settingsAdminOrg;

  /// No description provided for @settingsAdminOrgHint.
  ///
  /// In en, this message translates to:
  /// **'Company profile, invoice defaults'**
  String get settingsAdminOrgHint;

  /// No description provided for @settingsAdminWorkflows.
  ///
  /// In en, this message translates to:
  /// **'Workflows'**
  String get settingsAdminWorkflows;

  /// No description provided for @settingsAdminWorkflowsHint.
  ///
  /// In en, this message translates to:
  /// **'View workflow definitions and steps'**
  String get settingsAdminWorkflowsHint;

  /// No description provided for @settingsAdaptive.
  ///
  /// In en, this message translates to:
  /// **'Adaptive Workflows'**
  String get settingsAdaptive;

  /// No description provided for @settingsAdaptiveHint.
  ///
  /// In en, this message translates to:
  /// **'Approval patterns, anomalies, suggestions'**
  String get settingsAdaptiveHint;

  /// No description provided for @inspectionsTitle.
  ///
  /// In en, this message translates to:
  /// **'Quality Inspections'**
  String get inspectionsTitle;

  /// No description provided for @inspectionsRecord.
  ///
  /// In en, this message translates to:
  /// **'Record inspection'**
  String get inspectionsRecord;

  /// No description provided for @inspectionsEmpty.
  ///
  /// In en, this message translates to:
  /// **'No quality inspections recorded.'**
  String get inspectionsEmpty;

  /// No description provided for @inspectionsEmptyFiltered.
  ///
  /// In en, this message translates to:
  /// **'No inspections with this outcome.'**
  String get inspectionsEmptyFiltered;

  /// No description provided for @inspectionsLoadError.
  ///
  /// In en, this message translates to:
  /// **'Could not load inspections'**
  String get inspectionsLoadError;

  /// No description provided for @inspectionsResultPass.
  ///
  /// In en, this message translates to:
  /// **'Pass'**
  String get inspectionsResultPass;

  /// No description provided for @inspectionsResultFail.
  ///
  /// In en, this message translates to:
  /// **'Fail'**
  String get inspectionsResultFail;

  /// No description provided for @inspectionsResultPartial.
  ///
  /// In en, this message translates to:
  /// **'Partial acceptance'**
  String get inspectionsResultPartial;

  /// No description provided for @inspectionsResultUnknown.
  ///
  /// In en, this message translates to:
  /// **'Unknown result'**
  String get inspectionsResultUnknown;

  /// No description provided for @inspectionsResultAnnounce.
  ///
  /// In en, this message translates to:
  /// **'Result: {result}'**
  String inspectionsResultAnnounce(String result);

  /// No description provided for @inspectionsNotLinked.
  ///
  /// In en, this message translates to:
  /// **'Not linked'**
  String get inspectionsNotLinked;

  /// No description provided for @inspectionsNotLinkedHint.
  ///
  /// In en, this message translates to:
  /// **'This inspection is tied to no goods receipt and no purchase order, so PO matching will never read it.'**
  String get inspectionsNotLinkedHint;

  /// No description provided for @inspectionsReceiptUnnamed.
  ///
  /// In en, this message translates to:
  /// **'Receipt not named'**
  String get inspectionsReceiptUnnamed;

  /// No description provided for @inspectionsHintPass.
  ///
  /// In en, this message translates to:
  /// **'Goods accepted — the match is unchanged.'**
  String get inspectionsHintPass;

  /// No description provided for @inspectionsHintFail.
  ///
  /// In en, this message translates to:
  /// **'Goods refused — the invoice drops to mismatch and a quality hold blocks payment.'**
  String get inspectionsHintFail;

  /// No description provided for @inspectionsHintPartial.
  ///
  /// In en, this message translates to:
  /// **'Some goods accepted — the match drops to partial and the accepted quantity is flagged.'**
  String get inspectionsHintPartial;

  /// No description provided for @inspectionsHintUnknown.
  ///
  /// In en, this message translates to:
  /// **'This outcome is outside the pass / fail / partial vocabulary, so its effect on the match cannot be stated.'**
  String get inspectionsHintUnknown;

  /// No description provided for @inspectionsRecorded.
  ///
  /// In en, this message translates to:
  /// **'Inspection {number} recorded'**
  String inspectionsRecorded(String number);

  /// No description provided for @inspectionsRecordFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not record the inspection: {error}'**
  String inspectionsRecordFailed(String error);

  /// No description provided for @inspectionsReceiptsLoadFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not load goods receipts: {error}'**
  String inspectionsReceiptsLoadFailed(String error);

  /// No description provided for @inspectionRecordTitle.
  ///
  /// In en, this message translates to:
  /// **'Record Quality Inspection'**
  String get inspectionRecordTitle;

  /// No description provided for @inspectionRecordClose.
  ///
  /// In en, this message translates to:
  /// **'Close the inspection form'**
  String get inspectionRecordClose;

  /// No description provided for @inspectionRecordReceipt.
  ///
  /// In en, this message translates to:
  /// **'Goods receipt'**
  String get inspectionRecordReceipt;

  /// No description provided for @inspectionRecordReceiptHint.
  ///
  /// In en, this message translates to:
  /// **'PO matching only reads an inspection through its goods receipt, so an inspection has to name the delivery it covers.'**
  String get inspectionRecordReceiptHint;

  /// No description provided for @inspectionRecordReceiptsBounded.
  ///
  /// In en, this message translates to:
  /// **'Showing the {shown} most recent of {total} receipts. Record from the web app if the delivery you need is not listed.'**
  String inspectionRecordReceiptsBounded(int shown, int total);

  /// No description provided for @inspectionRecordNoReceipts.
  ///
  /// In en, this message translates to:
  /// **'No goods receipts yet — an inspection covers a delivery, so there is nothing to record against.'**
  String get inspectionRecordNoReceipts;

  /// No description provided for @inspectionRecordNumber.
  ///
  /// In en, this message translates to:
  /// **'Inspection #'**
  String get inspectionRecordNumber;

  /// No description provided for @inspectionRecordNumberRequired.
  ///
  /// In en, this message translates to:
  /// **'Enter an inspection number'**
  String get inspectionRecordNumberRequired;

  /// No description provided for @inspectionRecordResult.
  ///
  /// In en, this message translates to:
  /// **'Result'**
  String get inspectionRecordResult;

  /// No description provided for @inspectionRecordAcceptedQuantity.
  ///
  /// In en, this message translates to:
  /// **'Accepted quantity'**
  String get inspectionRecordAcceptedQuantity;

  /// No description provided for @inspectionRecordRejectedQuantity.
  ///
  /// In en, this message translates to:
  /// **'Rejected quantity'**
  String get inspectionRecordRejectedQuantity;

  /// No description provided for @inspectionRecordAcceptedRequired.
  ///
  /// In en, this message translates to:
  /// **'Required for a partial acceptance'**
  String get inspectionRecordAcceptedRequired;

  /// No description provided for @inspectionRecordInvalidQuantity.
  ///
  /// In en, this message translates to:
  /// **'Use up to 8 digits and 4 decimals'**
  String get inspectionRecordInvalidQuantity;

  /// No description provided for @inspectionRecordInspectedDate.
  ///
  /// In en, this message translates to:
  /// **'Inspected date'**
  String get inspectionRecordInspectedDate;

  /// No description provided for @inspectionRecordDateNotSet.
  ///
  /// In en, this message translates to:
  /// **'Not set'**
  String get inspectionRecordDateNotSet;

  /// No description provided for @inspectionRecordClearDate.
  ///
  /// In en, this message translates to:
  /// **'Clear the inspected date'**
  String get inspectionRecordClearDate;

  /// No description provided for @inspectionRecordInspector.
  ///
  /// In en, this message translates to:
  /// **'Inspector'**
  String get inspectionRecordInspector;

  /// No description provided for @inspectionRecordNotes.
  ///
  /// In en, this message translates to:
  /// **'Deviation notes'**
  String get inspectionRecordNotes;

  /// No description provided for @inspectionRecordNotesHint.
  ///
  /// In en, this message translates to:
  /// **'Quoted into the invoice’s match issue on a failed inspection, so whoever works the quality hold reads them.'**
  String get inspectionRecordNotesHint;

  /// No description provided for @inspectionRecordSubmit.
  ///
  /// In en, this message translates to:
  /// **'Record inspection'**
  String get inspectionRecordSubmit;

  /// No description provided for @inspectionDetailTitle.
  ///
  /// In en, this message translates to:
  /// **'Inspection'**
  String get inspectionDetailTitle;

  /// No description provided for @inspectionDetailNotFound.
  ///
  /// In en, this message translates to:
  /// **'Inspection not found'**
  String get inspectionDetailNotFound;

  /// No description provided for @inspectionDetailErrorPrefix.
  ///
  /// In en, this message translates to:
  /// **'Could not load the inspection: {error}'**
  String inspectionDetailErrorPrefix(String error);

  /// No description provided for @inspectionDetailFieldReceipt.
  ///
  /// In en, this message translates to:
  /// **'Goods receipt'**
  String get inspectionDetailFieldReceipt;

  /// No description provided for @inspectionDetailFieldInspectedDate.
  ///
  /// In en, this message translates to:
  /// **'Inspected'**
  String get inspectionDetailFieldInspectedDate;

  /// No description provided for @inspectionDetailFieldInspector.
  ///
  /// In en, this message translates to:
  /// **'Inspector'**
  String get inspectionDetailFieldInspector;

  /// No description provided for @inspectionDetailFieldAccepted.
  ///
  /// In en, this message translates to:
  /// **'Accepted quantity'**
  String get inspectionDetailFieldAccepted;

  /// No description provided for @inspectionDetailFieldRejected.
  ///
  /// In en, this message translates to:
  /// **'Rejected quantity'**
  String get inspectionDetailFieldRejected;

  /// No description provided for @inspectionDetailFieldStatus.
  ///
  /// In en, this message translates to:
  /// **'Status'**
  String get inspectionDetailFieldStatus;

  /// No description provided for @inspectionDetailFieldCreated.
  ///
  /// In en, this message translates to:
  /// **'Created'**
  String get inspectionDetailFieldCreated;

  /// No description provided for @inspectionDetailSectionNotes.
  ///
  /// In en, this message translates to:
  /// **'Deviation notes'**
  String get inspectionDetailSectionNotes;

  /// No description provided for @adaptiveTitle.
  ///
  /// In en, this message translates to:
  /// **'Adaptive Workflows'**
  String get adaptiveTitle;

  /// No description provided for @adaptiveTabSuggestions.
  ///
  /// In en, this message translates to:
  /// **'Suggestions'**
  String get adaptiveTabSuggestions;

  /// No description provided for @adaptiveTabPatterns.
  ///
  /// In en, this message translates to:
  /// **'Approval patterns'**
  String get adaptiveTabPatterns;

  /// No description provided for @adaptiveTabAnomalies.
  ///
  /// In en, this message translates to:
  /// **'Anomalies'**
  String get adaptiveTabAnomalies;

  /// No description provided for @adaptiveAdvisoryNote.
  ///
  /// In en, this message translates to:
  /// **'Everything here is advisory. Nothing on this screen has changed a workflow — a recommendation only takes effect when someone with the right role applies it in the web app, through the same audited path a manual edit uses.'**
  String get adaptiveAdvisoryNote;

  /// No description provided for @adaptiveSuggestionsEmpty.
  ///
  /// In en, this message translates to:
  /// **'No suggestions — not enough consistent approval history yet.'**
  String get adaptiveSuggestionsEmpty;

  /// No description provided for @adaptiveSuggestionsError.
  ///
  /// In en, this message translates to:
  /// **'Could not load the suggestions.'**
  String get adaptiveSuggestionsError;

  /// No description provided for @adaptiveSuggestionsShowOpen.
  ///
  /// In en, this message translates to:
  /// **'Open'**
  String get adaptiveSuggestionsShowOpen;

  /// No description provided for @adaptiveSuggestionsShowAll.
  ///
  /// In en, this message translates to:
  /// **'All'**
  String get adaptiveSuggestionsShowAll;

  /// No description provided for @adaptiveSuggestionsConfidence.
  ///
  /// In en, this message translates to:
  /// **'Confidence {pct}%'**
  String adaptiveSuggestionsConfidence(String pct);

  /// No description provided for @adaptiveSuggestionsDismiss.
  ///
  /// In en, this message translates to:
  /// **'Dismiss'**
  String get adaptiveSuggestionsDismiss;

  /// No description provided for @adaptiveSuggestionsDismissTitle.
  ///
  /// In en, this message translates to:
  /// **'Dismiss this suggestion?'**
  String get adaptiveSuggestionsDismissTitle;

  /// No description provided for @adaptiveSuggestionsDismissBody.
  ///
  /// In en, this message translates to:
  /// **'It stays dismissed across recomputation, and no workflow changes either way.'**
  String get adaptiveSuggestionsDismissBody;

  /// No description provided for @adaptiveSuggestionsDismissed.
  ///
  /// In en, this message translates to:
  /// **'Suggestion dismissed.'**
  String get adaptiveSuggestionsDismissed;

  /// No description provided for @adaptiveSuggestionsDismissFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not dismiss that suggestion: {error}'**
  String adaptiveSuggestionsDismissFailed(String error);

  /// No description provided for @adaptiveSuggestionStatusOpen.
  ///
  /// In en, this message translates to:
  /// **'Open'**
  String get adaptiveSuggestionStatusOpen;

  /// No description provided for @adaptiveSuggestionStatusDismissed.
  ///
  /// In en, this message translates to:
  /// **'Dismissed'**
  String get adaptiveSuggestionStatusDismissed;

  /// No description provided for @adaptiveSuggestionStatusApplied.
  ///
  /// In en, this message translates to:
  /// **'Applied'**
  String get adaptiveSuggestionStatusApplied;

  /// No description provided for @adaptiveSuggestionStatusStale.
  ///
  /// In en, this message translates to:
  /// **'Stale'**
  String get adaptiveSuggestionStatusStale;

  /// No description provided for @adaptiveSuggestionStatusUnknown.
  ///
  /// In en, this message translates to:
  /// **'Unknown status'**
  String get adaptiveSuggestionStatusUnknown;

  /// No description provided for @adaptiveSuggestionStatusAnnounce.
  ///
  /// In en, this message translates to:
  /// **'Status: {status}'**
  String adaptiveSuggestionStatusAnnounce(String status);

  /// No description provided for @adaptivePatternsError.
  ///
  /// In en, this message translates to:
  /// **'Could not load the approval patterns.'**
  String get adaptivePatternsError;

  /// No description provided for @adaptivePatternsEmptyApprovers.
  ///
  /// In en, this message translates to:
  /// **'No approval decisions in this window yet.'**
  String get adaptivePatternsEmptyApprovers;

  /// No description provided for @adaptivePatternsEmptyVendors.
  ///
  /// In en, this message translates to:
  /// **'No supplier approval history in this window yet.'**
  String get adaptivePatternsEmptyVendors;

  /// No description provided for @adaptivePatternsSectionApprovers.
  ///
  /// In en, this message translates to:
  /// **'By approver'**
  String get adaptivePatternsSectionApprovers;

  /// No description provided for @adaptivePatternsSectionVendors.
  ///
  /// In en, this message translates to:
  /// **'By vendor'**
  String get adaptivePatternsSectionVendors;

  /// No description provided for @adaptivePatternsLookback.
  ///
  /// In en, this message translates to:
  /// **'Deterministic statistics over the last {days} days of this tenant’s own approval history — no model, recomputed every time you look.'**
  String adaptivePatternsLookback(int days);

  /// No description provided for @adaptivePatternsCurrencyNote.
  ///
  /// In en, this message translates to:
  /// **'Amounts are in your organization’s reporting currency.'**
  String get adaptivePatternsCurrencyNote;

  /// No description provided for @adaptivePatternsUnknownApprover.
  ///
  /// In en, this message translates to:
  /// **'Unknown approver'**
  String get adaptivePatternsUnknownApprover;

  /// No description provided for @adaptivePatternsApproverSummary.
  ///
  /// In en, this message translates to:
  /// **'{approved} approved · {rejected} rejected'**
  String adaptivePatternsApproverSummary(int approved, int rejected);

  /// No description provided for @adaptivePatternsApproverTiming.
  ///
  /// In en, this message translates to:
  /// **'{rate}% approval rate · median {days} days'**
  String adaptivePatternsApproverTiming(String rate, String days);

  /// No description provided for @adaptivePatternsVendorConsistency.
  ///
  /// In en, this message translates to:
  /// **'{pct}% approved unedited'**
  String adaptivePatternsVendorConsistency(String pct);

  /// No description provided for @adaptivePatternsVendorMoney.
  ///
  /// In en, this message translates to:
  /// **'Median {median} · average {avg}'**
  String adaptivePatternsVendorMoney(String median, String avg);

  /// No description provided for @adaptivePatternsUnconverted.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} approval could not be expressed in the reporting currency and is excluded from the amounts above — the sample still counts it} other {{count} approvals could not be expressed in the reporting currency and are excluded from the amounts above — the sample still counts them}}'**
  String adaptivePatternsUnconverted(int count);

  /// No description provided for @adaptiveAnomaliesIntro.
  ///
  /// In en, this message translates to:
  /// **'Invoices in review that sit outside their own supplier’s established pattern — on amount, on approver, or on how long they have been waiting. Read-only: flagging here raises nothing and blocks nothing.'**
  String get adaptiveAnomaliesIntro;

  /// No description provided for @adaptiveAnomaliesEmpty.
  ///
  /// In en, this message translates to:
  /// **'Nothing in review is outside its supplier’s normal pattern.'**
  String get adaptiveAnomaliesEmpty;

  /// No description provided for @adaptiveAnomaliesError.
  ///
  /// In en, this message translates to:
  /// **'Could not load the anomaly scan.'**
  String get adaptiveAnomaliesError;

  /// No description provided for @adaptiveAnomaliesScanned.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, one {{count} invoice in review scanned.} other {{count} invoices in review scanned.}}'**
  String adaptiveAnomaliesScanned(int count);

  /// No description provided for @adaptiveAnomaliesAmount.
  ///
  /// In en, this message translates to:
  /// **'{amount} {currency}'**
  String adaptiveAnomaliesAmount(String amount, String currency);

  /// No description provided for @adaptiveAnomaliesInsufficient.
  ///
  /// In en, this message translates to:
  /// **'Not enough history for this supplier yet.'**
  String get adaptiveAnomaliesInsufficient;

  /// No description provided for @invoiceWarningMissingVendorName.
  ///
  /// In en, this message translates to:
  /// **'Missing vendor name'**
  String get invoiceWarningMissingVendorName;

  /// No description provided for @invoiceWarningMissingInvoiceNumber.
  ///
  /// In en, this message translates to:
  /// **'Missing invoice number'**
  String get invoiceWarningMissingInvoiceNumber;

  /// No description provided for @invoiceWarningMissingAmount.
  ///
  /// In en, this message translates to:
  /// **'Missing or zero amount'**
  String get invoiceWarningMissingAmount;

  /// No description provided for @invoiceWarningDuplicateInvoiceNumber.
  ///
  /// In en, this message translates to:
  /// **'Duplicate invoice number for this vendor'**
  String get invoiceWarningDuplicateInvoiceNumber;

  /// No description provided for @invoiceWarningDuplicateSimilar.
  ///
  /// In en, this message translates to:
  /// **'Potential duplicate: {similarity} match to {invoiceNumber} from {vendorName}{crossEntityCount, plural, =0 {} one { (plus {crossEntityCount} near-identical invoice under another entity)} other { (plus {crossEntityCount} near-identical invoices under another entity)}}'**
  String invoiceWarningDuplicateSimilar(
    String similarity,
    String invoiceNumber,
    String vendorName,
    int crossEntityCount,
  );

  /// No description provided for @invoiceWarningDuplicateSimilarUnnamedVendor.
  ///
  /// In en, this message translates to:
  /// **'Potential duplicate: {similarity} match to {invoiceNumber}{crossEntityCount, plural, =0 {} one { (plus {crossEntityCount} near-identical invoice under another entity)} other { (plus {crossEntityCount} near-identical invoices under another entity)}}'**
  String invoiceWarningDuplicateSimilarUnnamedVendor(
    String similarity,
    String invoiceNumber,
    int crossEntityCount,
  );

  /// No description provided for @invoiceWarningDuplicateSimilarUnnumbered.
  ///
  /// In en, this message translates to:
  /// **'Potential duplicate: {similarity} match to another invoice from {vendorName}{crossEntityCount, plural, =0 {} one { (plus {crossEntityCount} near-identical invoice under another entity)} other { (plus {crossEntityCount} near-identical invoices under another entity)}}'**
  String invoiceWarningDuplicateSimilarUnnumbered(
    String similarity,
    String vendorName,
    int crossEntityCount,
  );

  /// No description provided for @invoiceWarningDuplicateSimilarUnnumberedUnnamedVendor.
  ///
  /// In en, this message translates to:
  /// **'Potential duplicate: {similarity} match to another invoice{crossEntityCount, plural, =0 {} one { (plus {crossEntityCount} near-identical invoice under another entity)} other { (plus {crossEntityCount} near-identical invoices under another entity)}}'**
  String invoiceWarningDuplicateSimilarUnnumberedUnnamedVendor(
    String similarity,
    int crossEntityCount,
  );

  /// No description provided for @invoiceWarningDuplicateSimilarCrossEntity.
  ///
  /// In en, this message translates to:
  /// **'Potential duplicate: {similarity} match to a near-identical invoice under another entity'**
  String invoiceWarningDuplicateSimilarCrossEntity(String similarity);

  /// No description provided for @invoiceWarningRoundAmount.
  ///
  /// In en, this message translates to:
  /// **'Round amount: {amount}'**
  String invoiceWarningRoundAmount(String amount);

  /// No description provided for @invoiceWarningFutureInvoiceDate.
  ///
  /// In en, this message translates to:
  /// **'Invoice date is in the future'**
  String get invoiceWarningFutureInvoiceDate;

  /// No description provided for @invoiceWarningRushPayment.
  ///
  /// In en, this message translates to:
  /// **'Rush payment: due within {days, plural, one {{days} day} other {{days} days}} of the invoice date'**
  String invoiceWarningRushPayment(int days);

  /// No description provided for @invoiceWarningPastDue.
  ///
  /// In en, this message translates to:
  /// **'Invoice is past due'**
  String get invoiceWarningPastDue;

  /// No description provided for @invoiceWarningUnverifiedVendor.
  ///
  /// In en, this message translates to:
  /// **'Vendor is unverified'**
  String get invoiceWarningUnverifiedVendor;

  /// No description provided for @invoiceWarningPersonalEmailDomain.
  ///
  /// In en, this message translates to:
  /// **'Vendor email uses personal domain: {domain}'**
  String invoiceWarningPersonalEmailDomain(String domain);

  /// No description provided for @invoiceWarningNewVendorLargeAmount.
  ///
  /// In en, this message translates to:
  /// **'New vendor (created {days, plural, one {{days} day} other {{days} days}} ago) submitting large invoice {amount}'**
  String invoiceWarningNewVendorLargeAmount(int days, String amount);

  /// No description provided for @invoiceWarningRemitToChanged.
  ///
  /// In en, this message translates to:
  /// **'Remit-to address changed since the last approved invoice for this vendor'**
  String get invoiceWarningRemitToChanged;

  /// No description provided for @invoiceWarningAmountAboveVendorMean.
  ///
  /// In en, this message translates to:
  /// **'Amount {amount} is {sigma}σ above this vendor\'s historical mean ({mean})'**
  String invoiceWarningAmountAboveVendorMean(
    String amount,
    String sigma,
    String mean,
  );

  /// No description provided for @invoiceWarningLlmAnomaly.
  ///
  /// In en, this message translates to:
  /// **'AI-flagged anomaly: {reason}'**
  String invoiceWarningLlmAnomaly(String reason);

  /// No description provided for @invoiceWarningLineTotalMismatch.
  ///
  /// In en, this message translates to:
  /// **'Line items total {lineItemsTotal} but the invoice amount is {headerAmount}'**
  String invoiceWarningLineTotalMismatch(
    String lineItemsTotal,
    String headerAmount,
  );

  /// No description provided for @invoiceWarningPriceVarianceOver.
  ///
  /// In en, this message translates to:
  /// **'Unit price {deltaPct} over this vendor\'s baseline for {item} ({unitPrice} vs {baselineUnitPrice})'**
  String invoiceWarningPriceVarianceOver(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  );

  /// No description provided for @invoiceWarningPriceVarianceUnder.
  ///
  /// In en, this message translates to:
  /// **'Unit price {deltaPct} under this vendor\'s baseline for {item} ({unitPrice} vs {baselineUnitPrice})'**
  String invoiceWarningPriceVarianceUnder(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  );

  /// No description provided for @invoiceWarningPoNotFound.
  ///
  /// In en, this message translates to:
  /// **'PO {poNumber} not found'**
  String invoiceWarningPoNotFound(String poNumber);

  /// No description provided for @invoiceWarningPoAmountVariance.
  ///
  /// In en, this message translates to:
  /// **'Amount variance {variancePct} vs PO {poNumber} (invoice {invoiceAmount} vs PO {poTotal})'**
  String invoiceWarningPoAmountVariance(
    String variancePct,
    String poNumber,
    String invoiceAmount,
    String poTotal,
  );

  /// No description provided for @invoiceWarningPoPartialReceipt.
  ///
  /// In en, this message translates to:
  /// **'Partial 3-way match — {matchType} match against PO {poNumber}, but only part of the ordered quantity has been received'**
  String invoiceWarningPoPartialReceipt(String matchType, String poNumber);

  /// No description provided for @invoiceWarningPoOverReceipt.
  ///
  /// In en, this message translates to:
  /// **'Over-receipt: {receivedQuantity} received against {orderedQuantity} ordered (+{excessQuantity}) on PO {poNumber}'**
  String invoiceWarningPoOverReceipt(
    String receivedQuantity,
    String orderedQuantity,
    String excessQuantity,
    String poNumber,
  );

  /// No description provided for @invoiceWarningPoOverReceiptUnquantified.
  ///
  /// In en, this message translates to:
  /// **'More goods received than ordered on PO {poNumber}'**
  String invoiceWarningPoOverReceiptUnquantified(String poNumber);

  /// No description provided for @invoiceWarningQualityInspectionFailed.
  ///
  /// In en, this message translates to:
  /// **'Failed quality inspection for PO {poNumber}'**
  String invoiceWarningQualityInspectionFailed(String poNumber);

  /// No description provided for @invoiceWarningQualityInspectionFailedNotes.
  ///
  /// In en, this message translates to:
  /// **'Failed quality inspection for PO {poNumber}: {notes}'**
  String invoiceWarningQualityInspectionFailedNotes(
    String poNumber,
    String notes,
  );

  /// No description provided for @invoiceWarningQualityInspectionMissing.
  ///
  /// In en, this message translates to:
  /// **'Quality inspection required but missing for PO {poNumber}'**
  String invoiceWarningQualityInspectionMissing(String poNumber);

  /// No description provided for @invoiceWarningQualityPartialAcceptance.
  ///
  /// In en, this message translates to:
  /// **'Partial acceptance: {acceptedQuantity} of the ordered quantity accepted on PO {poNumber}'**
  String invoiceWarningQualityPartialAcceptance(
    String acceptedQuantity,
    String poNumber,
  );

  /// No description provided for @invoiceWarningQualityPartialAcceptanceUnquantified.
  ///
  /// In en, this message translates to:
  /// **'Partial quality acceptance on inspection for PO {poNumber}'**
  String invoiceWarningQualityPartialAcceptanceUnquantified(String poNumber);

  /// No description provided for @invoiceWarningRecurringVarianceOver.
  ///
  /// In en, this message translates to:
  /// **'Amount {amount} is {deltaPct} over the recurring template “{templateName}” expected amount {expectedAmount}'**
  String invoiceWarningRecurringVarianceOver(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  );

  /// No description provided for @invoiceWarningRecurringVarianceUnder.
  ///
  /// In en, this message translates to:
  /// **'Amount {amount} is {deltaPct} under the recurring template “{templateName}” expected amount {expectedAmount}'**
  String invoiceWarningRecurringVarianceUnder(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  );

  /// No description provided for @invoiceWarningContractExpired.
  ///
  /// In en, this message translates to:
  /// **'Invoice dated {invoiceDate} is after contract {contractNumber} expired ({endDate})'**
  String invoiceWarningContractExpired(
    String invoiceDate,
    String contractNumber,
    String endDate,
  );

  /// No description provided for @invoiceWarningContractNotStarted.
  ///
  /// In en, this message translates to:
  /// **'Invoice dated {invoiceDate} predates contract {contractNumber} start ({startDate})'**
  String invoiceWarningContractNotStarted(
    String invoiceDate,
    String contractNumber,
    String startDate,
  );

  /// No description provided for @invoiceWarningContractTerminated.
  ///
  /// In en, this message translates to:
  /// **'Spend recorded against terminated contract {contractNumber}'**
  String invoiceWarningContractTerminated(String contractNumber);

  /// No description provided for @invoiceWarningContractCancelled.
  ///
  /// In en, this message translates to:
  /// **'Spend recorded against cancelled contract {contractNumber}'**
  String invoiceWarningContractCancelled(String contractNumber);

  /// No description provided for @invoiceWarningContractVendorMismatch.
  ///
  /// In en, this message translates to:
  /// **'Invoice vendor does not match contract {contractNumber} vendor'**
  String invoiceWarningContractVendorMismatch(String contractNumber);

  /// No description provided for @invoiceWarningContractSpendLimitExceeded.
  ///
  /// In en, this message translates to:
  /// **'Cumulative spend {cumulativeSpend} exceeds contract {contractNumber} limit {spendLimit}'**
  String invoiceWarningContractSpendLimitExceeded(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  );

  /// No description provided for @invoiceWarningContractSpendLimitExceededNotToExceed.
  ///
  /// In en, this message translates to:
  /// **'Cumulative spend {cumulativeSpend} exceeds contract {contractNumber} limit {spendLimit} (not-to-exceed)'**
  String invoiceWarningContractSpendLimitExceededNotToExceed(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  );

  /// No description provided for @invoiceWarningContractGlNotAllowed.
  ///
  /// In en, this message translates to:
  /// **'GL account {glAccount} is outside contract {contractNumber} allowed accounts'**
  String invoiceWarningContractGlNotAllowed(
    String glAccount,
    String contractNumber,
  );

  /// No description provided for @invoiceWarningSelfCorrectionTotalReconciliation.
  ///
  /// In en, this message translates to:
  /// **'Amounts don\'t add up: subtotal ({subtotal}) + tax ({tax}) + shipping ({shipping}) − discount ({discount}) = {expected}, but total is {amount}.'**
  String invoiceWarningSelfCorrectionTotalReconciliation(
    String subtotal,
    String tax,
    String shipping,
    String discount,
    String expected,
    String amount,
  );

  /// No description provided for @invoiceWarningSelfCorrectionDateOrdering.
  ///
  /// In en, this message translates to:
  /// **'Due date ({dueDate}) is before invoice date ({invoiceDate}).'**
  String invoiceWarningSelfCorrectionDateOrdering(
    String dueDate,
    String invoiceDate,
  );

  /// No description provided for @invoiceWarningSelfCorrectionLineItemsSum.
  ///
  /// In en, this message translates to:
  /// **'Line items total ({lineItemsTotal}) doesn\'t match invoice amount ({amount}).'**
  String invoiceWarningSelfCorrectionLineItemsSum(
    String lineItemsTotal,
    String amount,
  );

  /// No description provided for @invoiceWarningSelfCorrectionLineItemMath.
  ///
  /// In en, this message translates to:
  /// **'Line {lineNumber}: {quantity} × {unitPrice} = {expected}, but total is {total}.'**
  String invoiceWarningSelfCorrectionLineItemMath(
    String lineNumber,
    String quantity,
    String unitPrice,
    String expected,
    String total,
  );

  /// No description provided for @invoiceWarningGlCodesNotInChart.
  ///
  /// In en, this message translates to:
  /// **'AI suggested GL code(s) not in active chart: {codes}'**
  String invoiceWarningGlCodesNotInChart(String codes);

  /// No description provided for @invoiceWarningGlCodeStalePrior.
  ///
  /// In en, this message translates to:
  /// **'Cached vendor GL code “{code}” is no longer in the active chart of accounts.'**
  String invoiceWarningGlCodeStalePrior(String code);
}

class _AppLocalizationsDelegate
    extends LocalizationsDelegate<AppLocalizations> {
  const _AppLocalizationsDelegate();

  @override
  Future<AppLocalizations> load(Locale locale) {
    return SynchronousFuture<AppLocalizations>(lookupAppLocalizations(locale));
  }

  @override
  bool isSupported(Locale locale) => <String>[
    'de',
    'en',
    'es',
    'fr',
    'ja',
    'pt',
  ].contains(locale.languageCode);

  @override
  bool shouldReload(_AppLocalizationsDelegate old) => false;
}

AppLocalizations lookupAppLocalizations(Locale locale) {
  // Lookup logic when language+country codes are specified.
  switch (locale.languageCode) {
    case 'pt':
      {
        switch (locale.countryCode) {
          case 'BR':
            return AppLocalizationsPtBr();
        }
        break;
      }
  }

  // Lookup logic when only language code is specified.
  switch (locale.languageCode) {
    case 'de':
      return AppLocalizationsDe();
    case 'en':
      return AppLocalizationsEn();
    case 'es':
      return AppLocalizationsEs();
    case 'fr':
      return AppLocalizationsFr();
    case 'ja':
      return AppLocalizationsJa();
    case 'pt':
      return AppLocalizationsPt();
  }

  throw FlutterError(
    'AppLocalizations.delegate failed to load unsupported locale "$locale". This is likely '
    'an issue with the localizations generation tool. Please file an issue '
    'on GitHub with a reproducible sample app and the gen-l10n configuration '
    'that was used.',
  );
}
