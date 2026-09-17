// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for English (`en`).
class AppLocalizationsEn extends AppLocalizations {
  AppLocalizationsEn([String locale = 'en']) : super(locale);

  @override
  String get navDashboard => 'Dashboard';

  @override
  String get navInvoices => 'Invoices';

  @override
  String get navContracts => 'Contracts';

  @override
  String get navApprovals => 'Approvals';

  @override
  String get navExceptions => 'Exceptions';

  @override
  String get navVendors => 'Vendors';

  @override
  String get navPay => 'Pay';

  @override
  String get navPayments => 'Payments';

  @override
  String get navSettings => 'Settings';

  @override
  String get shellAppName => 'FeohLedger';

  @override
  String get commonSave => 'Save';

  @override
  String get commonSaving => 'Saving…';

  @override
  String get commonCancel => 'Cancel';

  @override
  String get commonLoading => 'Loading…';

  @override
  String get commonRetry => 'Retry';

  @override
  String get commonAll => 'All';

  @override
  String get commonSearch => 'Search';

  @override
  String get commonClear => 'Clear';

  @override
  String get commonApply => 'Apply';

  @override
  String get commonClose => 'Close';

  @override
  String get settingsTitle => 'Settings';

  @override
  String get settingsTenant => 'Tenant';

  @override
  String get settingsTenantNotSet => 'Not set';

  @override
  String get settingsApiServer => 'API Server';

  @override
  String get settingsBiometricUnlock => 'Biometric Unlock';

  @override
  String get settingsBiometricHint => 'Use fingerprint or face to unlock';

  @override
  String get settingsSignOut => 'Sign Out';

  @override
  String get settingsLanguage => 'Language';

  @override
  String get settingsLanguageHint =>
      'Choose the language used across the app. Your choice is saved on this device.';

  @override
  String get settingsLanguageSystem => 'System default';

  @override
  String get dashboardTitle => 'Dashboard';

  @override
  String get dashboardTotalInvoices => 'Total Invoices';

  @override
  String get dashboardUpcoming => 'Upcoming';

  @override
  String get dashboardForReview => 'For Review';

  @override
  String get dashboardApproved => 'Approved';

  @override
  String get dashboardAging => 'Invoice Aging';

  @override
  String get dashboardTopVendors => 'Top Vendors';

  @override
  String get dashboardAgingCurrent => 'Current';

  @override
  String get dashboardAgingDays30 => '30 Days';

  @override
  String get dashboardAgingDays60 => '60 Days';

  @override
  String get dashboardAgingDays90plus => '90+';

  @override
  String get dashboardCachedBanner =>
      'Showing cached data — couldn\'t reach the server';

  @override
  String dashboardErrorPrefix(String error) {
    return 'Error: $error';
  }

  @override
  String dashboardInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count invoices',
      one: '$count invoice',
    );
    return '$_temp0';
  }

  @override
  String get invoicesTitle => 'Invoices';

  @override
  String get invoicesSearchHint => 'Search invoices...';

  @override
  String get invoicesSearchAria => 'Search invoices';

  @override
  String get invoicesAdvancedSearch => 'Advanced Search';

  @override
  String get invoicesAdvancedSearchActive => 'Advanced search, filters active';

  @override
  String get invoicesCaptureInvoice => 'Capture Invoice';

  @override
  String get invoicesCaptureInvoiceLabel => 'Capture invoice';

  @override
  String get invoicesEmpty => 'No invoices found';

  @override
  String get invoicesFilterAll => 'All';

  @override
  String get invoicesFilterNew => 'New';

  @override
  String get invoicesFilterPending => 'Pending';

  @override
  String get invoicesFilterReview => 'Review';

  @override
  String get invoicesFilterApproved => 'Approved';

  @override
  String get invoicesFilterRejected => 'Rejected';

  @override
  String get invoicesFilterPaid => 'Paid';

  @override
  String get invoicesColInvoiceNumber => 'Invoice #';

  @override
  String get invoicesColVendor => 'Vendor';

  @override
  String get invoicesColAmount => 'Amount';

  @override
  String get invoicesColDueDate => 'Due Date';

  @override
  String get invoicesColStatus => 'Status';

  @override
  String get notificationsTitle => 'Notifications';

  @override
  String get notificationsMarkAllRead => 'Mark all read';

  @override
  String get notificationsMarkAllReadLabel => 'Mark all notifications read';

  @override
  String get notificationsFilterUnread => 'Unread';

  @override
  String get notificationsAllMarkedRead => 'All notifications marked read';

  @override
  String get notificationsCouldNotMarkAll => 'Could not mark all read';

  @override
  String get notificationsEmptyUnread => 'No unread notifications';

  @override
  String get notificationsEmpty => 'No notifications';

  @override
  String get notificationsCaughtUp => 'You\'re all caught up';

  @override
  String get notificationsNothingYet => 'Nothing here yet';

  @override
  String get notificationsLoadError => 'Couldn\'t load notifications';

  @override
  String get vendorsTitle => 'Vendors';

  @override
  String get vendorsSyncErp => 'Sync from ERP';

  @override
  String get vendorsSyncErpLabel => 'Sync vendors from ERP';

  @override
  String get vendorsSearchHint => 'Search vendors...';

  @override
  String get vendorsFilterUnverified => 'Unverified';

  @override
  String get vendorsFilterActive => 'Active';

  @override
  String get vendorsFilterInactive => 'Inactive';

  @override
  String get vendorsFilterRejected => 'Rejected';

  @override
  String get vendorsEmpty => 'No vendors found';

  @override
  String get vendorsLoadError => 'Could not load vendors';

  @override
  String get vendorActionVerify => 'Verify';

  @override
  String get vendorActionReject => 'Reject';

  @override
  String get vendorUnverifiedLabel => 'Unverified vendor';

  @override
  String get vendorVerifyHint => 'Make eligible for payment';

  @override
  String get vendorRejectHint => 'Mark as invalid / duplicate';

  @override
  String get vendorVerified => 'Vendor verified';

  @override
  String get vendorRejected => 'Vendor rejected';

  @override
  String get vendorActionFailed => 'Action failed';

  @override
  String vendorSyncFailed(String error) {
    return 'ERP sync failed: $error';
  }

  @override
  String get exceptionsTitle => 'Exceptions';

  @override
  String get exceptionsFilterOpen => 'Open';

  @override
  String get exceptionsFilterEscalated => 'Escalated';

  @override
  String get exceptionsFilterResolved => 'Resolved';

  @override
  String get exceptionsFilterDismissed => 'Dismissed';

  @override
  String get exceptionsEmpty => 'No exceptions';

  @override
  String get exceptionsQueueClear => 'The exception queue is clear';

  @override
  String get exceptionActionResolve => 'Resolve';

  @override
  String get exceptionActionEscalate => 'Escalate';

  @override
  String get exceptionActionDismiss => 'Dismiss';

  @override
  String get exceptionResolved => 'Exception resolved';

  @override
  String get exceptionEscalated => 'Exception escalated';

  @override
  String get exceptionDismissed => 'Exception dismissed';

  @override
  String get exceptionActionFailed => 'Action failed';

  @override
  String get paymentsTitle => 'Payments';

  @override
  String get paymentsEmpty => 'No payments';

  @override
  String paymentsErrorPrefix(String error) {
    return 'Error: $error';
  }

  @override
  String get paymentStatusPending => 'Pending';

  @override
  String get paymentStatusProcessing => 'Processing';

  @override
  String get paymentStatusCompleted => 'Completed';

  @override
  String get paymentStatusFailed => 'Failed';

  @override
  String get paymentStatusCancelled => 'Cancelled';

  @override
  String get approvalsTitle => 'Pending Approvals';

  @override
  String get approvalsAllCaughtUp => 'All caught up!';

  @override
  String get approvalsNoneWaiting => 'No invoices waiting for approval';

  @override
  String get approvalsLoadError => 'Could not load pending approvals';

  @override
  String approvalsPendingCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count invoices pending',
      one: '$count invoice pending',
    );
    return '$_temp0';
  }

  @override
  String get approvalActionApprove => 'Approve';

  @override
  String get approvalActionReject => 'Reject';

  @override
  String get approvalApproved => 'Invoice approved';

  @override
  String get captureTitle => 'Capture Invoice';

  @override
  String get captureChange => 'Change';

  @override
  String get captureUpload => 'Upload';

  @override
  String get captureUploading => 'Uploading…';

  @override
  String get captureEmptyPrompt =>
      'Take a photo, choose from gallery, or pick a file';

  @override
  String get captureCamera => 'Camera';

  @override
  String get captureGallery => 'Gallery';

  @override
  String get captureChooseFile => 'Choose file';

  @override
  String get captureSupportedFormats => 'Supports PDF, PNG, JPG and TIFF';

  @override
  String get captureUploadSuccess => 'Invoice uploaded successfully';

  @override
  String captureUploadFailedStatus(int status, String message) {
    return 'Upload failed ($status): $message';
  }

  @override
  String captureUploadFailed(String error) {
    return 'Upload failed: $error';
  }

  @override
  String captureSelectedDocument(String name) {
    return 'Selected document: $name';
  }

  @override
  String get capturePdfReady => 'PDF document ready to upload';

  @override
  String get advSearchTitle => 'Advanced Search';

  @override
  String get advSearchClose => 'Close advanced search';

  @override
  String get advSearchVendor => 'Vendor';

  @override
  String get advSearchPoNumber => 'PO Number';

  @override
  String get advSearchMinAmount => 'Min amount';

  @override
  String get advSearchMaxAmount => 'Max amount';

  @override
  String get advSearchDueFrom => 'Due from';

  @override
  String get advSearchDueTo => 'Due to';

  @override
  String get advSearchAny => 'Any';

  @override
  String get advSearchInvalidAmount => 'Enter a valid amount (e.g. 1000)';

  @override
  String get advSearchMinMaxError => 'Min must not exceed max';

  @override
  String advSearchClearField(String label) {
    return 'Clear $label';
  }

  @override
  String advSearchDateFieldHint(String label, String value) {
    return '$label, currently $value. Double tap to change.';
  }

  @override
  String get invoiceDetailTitle => 'Invoice Detail';

  @override
  String get invoiceDetailEdit => 'Edit';

  @override
  String get invoiceDetailEditLabel => 'Edit invoice';

  @override
  String get invoiceDetailRetry => 'Retry';

  @override
  String invoiceDetailErrorPrefix(String error) {
    return 'Error: $error';
  }

  @override
  String get invoiceDetailNoChanges => 'No changes to save';

  @override
  String get invoiceDetailUpdated => 'Invoice updated';

  @override
  String get invoiceDetailUpdateFailed =>
      'Could not save changes — please try again';

  @override
  String get invoiceDetailApproved => 'Invoice approved';

  @override
  String get invoiceDetailApproveFailed =>
      'Could not approve invoice — please try again';

  @override
  String get invoiceDetailRejected => 'Invoice rejected';

  @override
  String get invoiceDetailRejectFailed =>
      'Could not reject invoice — please try again';

  @override
  String get invoiceDetailRejectTitle => 'Reject Invoice';

  @override
  String get invoiceDetailRejectReason => 'Reason';

  @override
  String get invoiceDetailReject => 'Reject';

  @override
  String get invoiceDetailApprove => 'Approve';

  @override
  String get invoiceDetailUnknownVendor => 'Unknown Vendor';

  @override
  String get invoiceDetailFieldInvoiceNumber => 'Invoice #';

  @override
  String get invoiceDetailFieldPoNumber => 'PO Number';

  @override
  String get invoiceDetailFieldCurrency => 'Currency';

  @override
  String get invoiceDetailFieldInvoiceDate => 'Invoice Date';

  @override
  String get invoiceDetailFieldDueDate => 'Due Date';

  @override
  String get invoiceDetailFieldDescription => 'Description';

  @override
  String get invoiceDetailFieldGlAccount => 'GL Account';

  @override
  String get invoiceDetailFieldCreated => 'Created';

  @override
  String get invoiceDetailActivity => 'Activity';

  @override
  String get invoiceDetailActivityError => 'Could not load activity';

  @override
  String get invoiceDetailFilePdfLabel =>
      'Invoice PDF. Double tap to view full screen.';

  @override
  String get invoiceDetailFileLabel =>
      'Invoice file. Double tap to view full screen.';

  @override
  String get invoiceDetailTapToViewPdf => 'Tap to view PDF';

  @override
  String get invoiceDetailTapToViewFile => 'Tap to view file';

  @override
  String get invoiceEditTitle => 'Edit Invoice';

  @override
  String get invoiceEditClose => 'Close edit form';

  @override
  String get invoiceEditVendor => 'Vendor';

  @override
  String get invoiceEditInvoiceNumber => 'Invoice #';

  @override
  String get invoiceEditAmount => 'Amount';

  @override
  String get invoiceEditPoNumber => 'PO Number';

  @override
  String get invoiceEditGlAccount => 'GL Account';

  @override
  String get invoiceEditDescription => 'Description';

  @override
  String get invoiceEditDueDate => 'Due Date';

  @override
  String get invoiceEditNotSet => 'Not set';

  @override
  String get invoiceEditInvalidAmount => 'Enter a valid amount (e.g. 1234.56)';

  @override
  String get invoiceEditClearDueDate => 'Clear due date';

  @override
  String get invoiceEditLockedNotice =>
      'Approved — the payee and amount are frozen. To change them, reject the invoice, correct it, then approve it again.';

  @override
  String get invoiceEditLockedHelper => 'Frozen after approval';

  @override
  String invoiceEditDueDateHint(String value) {
    return 'Due date, currently $value. Double tap to change.';
  }

  @override
  String get warningsSectionTitle => 'Warnings & fraud flags';

  @override
  String get warningsPoMatchTitle => 'PO Match';

  @override
  String get warningsSeverityError => 'Error';

  @override
  String get warningsSeverityWarning => 'Warning';

  @override
  String get warningsSeverityInfo => 'Info';

  @override
  String get warningsPoLabel => 'PO';

  @override
  String warningsMatchLabel(String type) {
    return '$type match';
  }

  @override
  String warningsVarianceLabel(String value) {
    return '$value% variance';
  }

  @override
  String get erpStatusTitle => 'ERP Status';

  @override
  String get erpStatusReference => 'ERP Reference';

  @override
  String get erpStatusDocumentId => 'Document ID';

  @override
  String get erpStatusError => 'Error';

  @override
  String get erpStatusLastUpdate => 'Last update';

  @override
  String get erpStatusStatus => 'Status';

  @override
  String get fileViewerPdfTitle => 'Invoice PDF';

  @override
  String get fileViewerImageTitle => 'Invoice Image';

  @override
  String get fileViewerPdfError => 'Unable to load PDF';

  @override
  String get fileViewerImageError => 'Unable to load image';

  @override
  String get fileViewerRetry => 'Retry';

  @override
  String get timelineNoActivity => 'No activity yet';

  @override
  String get payTitle => 'Pay';

  @override
  String get payTabQueue => 'Queue';

  @override
  String get payTabRuns => 'Runs';

  @override
  String get paySummaryTotalPaid => 'Total Paid';

  @override
  String get paySummaryPending => 'Pending';

  @override
  String get paySummaryInQueue => 'In Queue';

  @override
  String get paySummaryCardRebates => 'Card Rebates';

  @override
  String paySummaryPaymentsSubtitle(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count payments',
      one: '$count payment',
    );
    return '$_temp0';
  }

  @override
  String get payQueueEmpty => 'No invoices awaiting payment';

  @override
  String get payQueueError => 'Could not load the payment queue';

  @override
  String get payQueueRetry => 'Retry';

  @override
  String payQueueDue(String date) {
    return 'Due $date';
  }

  @override
  String get payQueueNoDueDate => 'No due date';

  @override
  String payQueueDiscount(String amount) {
    return 'discount $amount';
  }

  @override
  String get payQueueOverdue => 'overdue';

  @override
  String get payQueueSelected => 'selected';

  @override
  String get payQueueBlockedDuplicate => 'Possible duplicate — unresolved';

  @override
  String get payQueueBlockedFraudFlag => 'Fraud flag — unresolved';

  @override
  String get payQueueBlockedLineTotalMismatch =>
      'Line totals don’t match — unresolved';

  @override
  String get payQueueBlockedPaymentReconciliation =>
      'Earlier payment unreconciled — may still be in flight';

  @override
  String get payQueueBlockedFullyCredited =>
      'Fully covered by credit memos — nothing to pay';

  @override
  String get payQueueBlockedLiveVirtualCard =>
      'A live virtual card claims this invoice — pay it by card';

  @override
  String get payQueueBlockedGeneric => 'Unresolved exception blocks payment';

  @override
  String payQueueBlockedAnnounce(String reason) {
    return 'can’t be paid: $reason';
  }

  @override
  String payQueuePinnedMethod(String method) {
    return 'Pay by $method';
  }

  @override
  String payMethodLabel(String invoiceNumber) {
    return 'Payment method for $invoiceNumber';
  }

  @override
  String get payMethodAch => 'ACH';

  @override
  String get payMethodWire => 'Wire';

  @override
  String get payMethodCheck => 'Check';

  @override
  String get payMethodVirtualCard => 'Virtual Card';

  @override
  String paySelectedCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count invoices selected',
      one: '$count invoice selected',
    );
    return '$_temp0';
  }

  @override
  String get payClear => 'Clear';

  @override
  String get payCreateRun => 'Create Run';

  @override
  String payCreateRunFailed(String error) {
    return 'Failed to create run: $error';
  }

  @override
  String get payRunsEmpty => 'No payment runs';

  @override
  String payRunSubtitle(int count, String date) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count payments',
      one: '$count payment',
    );
    return '$_temp0 • $date';
  }

  @override
  String get payRunCfoRequiredSuffix => ' • CFO approval required';

  @override
  String payRunAnnounce(String amount, String status, String subtitle) {
    return 'Run $amount, $status, $subtitle';
  }

  @override
  String get payRunActions => 'Run actions';

  @override
  String get payRunActionExecute => 'Execute';

  @override
  String get payRunActionCancel => 'Cancel';

  @override
  String get payRunActionApprove => 'Approve as CFO';

  @override
  String get payRunApproveTitle => 'Approve payment run?';

  @override
  String payRunApproveBody(String date, int count, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count payments',
      one: '$count payment',
    );
    return 'Sign off the run created $date — $_temp0 totalling $amount. This authorizes execution; it moves no money.';
  }

  @override
  String get payRunApproveConfirm => 'Approve';

  @override
  String payRunApproveFailed(String error) {
    return 'Failed to approve: $error';
  }

  @override
  String get payRunCfoBlocked =>
      'This run needs CFO approval before it can be executed.';

  @override
  String get payRunExecuteTitle => 'Execute payment run?';

  @override
  String payRunExecuteBody(String amount) {
    return 'This sends $amount via the configured payment processor.';
  }

  @override
  String payRunExecuteFailed(String error) {
    return 'Failed to execute: $error';
  }

  @override
  String payRunCancelFailed(String error) {
    return 'Failed to cancel: $error';
  }

  @override
  String get payRunStatusDraft => 'Draft';

  @override
  String get payRunStatusCompleted => 'Completed';

  @override
  String get payRunStatusSubmitted => 'Submitted';

  @override
  String get payRunStatusPartial => 'Partial';

  @override
  String get payRunStatusFailed => 'Failed';

  @override
  String get payRunStatusCancelled => 'Cancelled';

  @override
  String get payConfirmCancel => 'Cancel';

  @override
  String get payConfirmExecute => 'Execute';

  @override
  String get loginAppName => 'FeohLedger';

  @override
  String get loginTagline => 'Accounts Payable, Made Simple';

  @override
  String get loginTenant => 'Tenant';

  @override
  String get loginEmail => 'Email';

  @override
  String get loginPassword => 'Password';

  @override
  String get loginShowPassword => 'Show password';

  @override
  String get loginHidePassword => 'Hide password';

  @override
  String get loginRequired => 'Required';

  @override
  String get loginSignIn => 'Sign In';

  @override
  String get mfaTitle => 'Two-factor authentication';

  @override
  String get mfaHeading => 'Verify it\'s you';

  @override
  String get mfaPromptEmail => 'Enter the 6-digit code we emailed you.';

  @override
  String get mfaPromptTotp =>
      'Enter the 6-digit code from your authenticator app.';

  @override
  String get mfaEnforcedNotice =>
      'Your organization requires two-factor authentication. Verify with an email code now, then finish setting up an authenticator app in the web app.';

  @override
  String get mfaCode => 'Code';

  @override
  String get mfaCodeRequired => 'Required';

  @override
  String get mfaCodeTooShort => 'Enter at least 6 digits';

  @override
  String get mfaVerify => 'Verify';

  @override
  String get mfaSending => 'Sending…';

  @override
  String get mfaResendEmailCode => 'Resend email code';

  @override
  String get mfaSendEmailCode => 'Send email code';

  @override
  String get mfaUseEmailInstead => 'Use an email code instead';

  @override
  String get mfaUseAuthenticatorInstead => 'Use authenticator app instead';

  @override
  String get mfaEmailedAnnounce => 'A sign-in code was emailed to you.';

  @override
  String get adminUsersTitle => 'User Management';

  @override
  String get adminUsersSearchHint => 'Search by name or email';

  @override
  String get adminUsersEmpty => 'No users found';

  @override
  String get adminUsersLoadError => 'Could not load users';

  @override
  String get adminUsersEditRoles => 'Edit roles';

  @override
  String get adminUsersNoRoles => 'No roles';

  @override
  String get adminUsersDeactivate => 'Deactivate user';

  @override
  String get adminUsersActivate => 'Activate user';

  @override
  String get adminUsersCannotDeactivateSelf =>
      'You can\'t deactivate your own account';

  @override
  String get adminUsersDeactivateHint => 'Signs them out and blocks sign-in';

  @override
  String get adminUsersActivateHint => 'Restores sign-in access';

  @override
  String get adminUsersRoleActive => 'active';

  @override
  String get adminUsersRoleInactive => 'inactive';

  @override
  String get adminUsersInactiveBadge => 'Inactive';

  @override
  String adminUsersRolesUpdated(String name) {
    return 'Updated roles for $name';
  }

  @override
  String adminUsersRolesUpdateFailed(String error) {
    return 'Failed to update roles: $error';
  }

  @override
  String adminUsersActivated(String name) {
    return 'Activated $name';
  }

  @override
  String adminUsersDeactivated(String name) {
    return 'Deactivated $name';
  }

  @override
  String adminUsersUpdateFailed(String name, String error) {
    return 'Failed to update $name: $error';
  }

  @override
  String get adminUsersCreateUser => 'Create user';

  @override
  String get adminUsersCreateTitle => 'New user';

  @override
  String get adminUsersFieldFullName => 'Full name';

  @override
  String get adminUsersFieldEmail => 'Email';

  @override
  String get adminUsersFieldRoles => 'Roles';

  @override
  String get adminUsersValidationNameRequired => 'Full name is required';

  @override
  String get adminUsersValidationEmailRequired => 'Email is required';

  @override
  String get adminUsersValidationEmailInvalid => 'Enter a valid email address';

  @override
  String get adminUsersCreateSubmit => 'Create';

  @override
  String get adminUsersCreating => 'Creating…';

  @override
  String adminUsersCreated(String name) {
    return 'Created $name';
  }

  @override
  String adminUsersCreateFailed(String error) {
    return 'Failed to create user: $error';
  }

  @override
  String get adminUsersTempPasswordTitle => 'User created';

  @override
  String adminUsersTempPasswordBody(String name) {
    return 'Share this one-time password with $name. They\'ll be asked to change it on first sign-in. It won\'t be shown again.';
  }

  @override
  String get adminUsersDelete => 'Delete user';

  @override
  String get adminUsersDeleteHint => 'Permanently removes this account';

  @override
  String get adminUsersCannotDeleteSelf => 'You can\'t delete your own account';

  @override
  String adminUsersDeleteConfirmTitle(String name) {
    return 'Delete $name?';
  }

  @override
  String adminUsersDeleteConfirmBody(String name, String email) {
    return 'This permanently removes $name ($email). This can\'t be undone.';
  }

  @override
  String adminUsersDeleted(String name) {
    return 'Deleted $name';
  }

  @override
  String adminUsersDeleteFailed(String name, String error) {
    return 'Failed to delete $name: $error';
  }

  @override
  String get orgSettingsTitle => 'Organization Settings';

  @override
  String get orgSettingsNoSettings => 'No settings';

  @override
  String get orgSettingsLoadError => 'Could not load settings';

  @override
  String get orgSettingsSectionCompany => 'Company';

  @override
  String get orgSettingsSectionInvoiceDefaults => 'Invoice defaults';

  @override
  String get orgSettingsName => 'Organization name';

  @override
  String get orgSettingsAddress => 'Address';

  @override
  String get orgSettingsPhone => 'Phone';

  @override
  String get orgSettingsWebsite => 'Website';

  @override
  String get orgSettingsTaxId => 'Tax ID';

  @override
  String get orgSettingsCurrency => 'Default currency';

  @override
  String get orgSettingsPaymentTerms => 'Payment terms';

  @override
  String get orgSettingsNumberPrefix => 'Invoice number prefix';

  @override
  String get orgSettingsGlAccount => 'Default GL account';

  @override
  String get orgSettingsCostCenter => 'Default cost center';

  @override
  String get orgSettingsSave => 'Save changes';

  @override
  String get orgSettingsSaving => 'Saving…';

  @override
  String orgSettingsFieldRequired(String label) {
    return '$label is required';
  }

  @override
  String get orgSettingsSaved => 'Organization settings saved';

  @override
  String orgSettingsSaveFailed(String error) {
    return 'Failed to save: $error';
  }

  @override
  String get workflowsTitle => 'Workflows';

  @override
  String get workflowsEmpty => 'No workflows found';

  @override
  String get workflowsLoadError => 'Could not load workflows';

  @override
  String get workflowsStatusActive => 'Active';

  @override
  String get workflowsStatusInactive => 'Inactive';

  @override
  String get workflowsDefault => 'Default';

  @override
  String workflowsStepCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count steps',
      one: '$count step',
    );
    return '$_temp0';
  }

  @override
  String get workflowDetailFallbackTitle => 'Workflow';

  @override
  String get workflowDetailLoadError => 'Could not load workflow';

  @override
  String get workflowDetailNoSteps => 'This workflow has no steps.';

  @override
  String get workflowDetailDefaultWorkflow => 'Default workflow';

  @override
  String workflowDetailStepNumber(int number) {
    return 'Step $number';
  }

  @override
  String get workflowDetailStepEnabled => 'Enabled';

  @override
  String get workflowDetailStepDisabled => 'Disabled';

  @override
  String workflowDetailApproverCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count approvers',
      one: '$count approver',
    );
    return '$_temp0';
  }

  @override
  String workflowDetailDelaySummary(String hours) {
    return 'Delay $hours h';
  }

  @override
  String workflowDetailConditionSummary(String field) {
    return 'On $field';
  }

  @override
  String get cashFlowTitle => 'Cash Flow Forecast';

  @override
  String cashFlowErrorPrefix(String error) {
    return 'Error: $error';
  }

  @override
  String cashFlowHorizonDays(int days) {
    return '$days days';
  }

  @override
  String get cashFlowLowBalanceAlert => 'Low balance alert';

  @override
  String cashFlowBreachSingle(
    String threshold,
    String period,
    String shortfall,
  ) {
    return 'Projected to fall below the $threshold balance in $period (shortfall $shortfall).';
  }

  @override
  String cashFlowBreachMultiple(int count, String period, String shortfall) {
    return '$count periods are projected to fall below the minimum balance. Worst: $period, shortfall $shortfall.';
  }

  @override
  String get cashFlowMinimum => 'minimum';

  @override
  String get cashFlowOpeningBalance => 'Opening Balance';

  @override
  String get cashFlowProjectedEnd => 'Projected End';

  @override
  String cashFlowProjectedEndSubtitle(int days) {
    return 'in $days days';
  }

  @override
  String get cashFlowCommittedOut => 'Committed Out';

  @override
  String get cashFlowCommittedSubtitle => 'firm commitments';

  @override
  String get cashFlowPendingOut => 'Pending Out';

  @override
  String get cashFlowPendingSubtitle => 'in-flight pipeline';

  @override
  String get cashFlowOpeningSourceProvider => 'synced from bank';

  @override
  String get cashFlowOpeningSourceSettings => 'saved balance';

  @override
  String get cashFlowOpeningSourceQuery => 'manual';

  @override
  String get cashFlowOpeningSourceUnset => 'set a balance';

  @override
  String get cashFlowProjectedOutflows => 'Projected Outflows';

  @override
  String get cashFlowNoOutflows => 'No projected outflows in this horizon.';

  @override
  String cashFlowInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count invoices',
      one: '$count invoice',
    );
    return '$_temp0';
  }

  @override
  String cashFlowCommittedAmount(String amount) {
    return 'committed $amount';
  }

  @override
  String cashFlowPendingAmount(String amount) {
    return 'pending $amount';
  }

  @override
  String get cashFlowPosition => 'Cash Position';

  @override
  String get cashFlowNoPosition =>
      'No cash-position projection for this horizon.';

  @override
  String cashFlowOutAmount(String amount) {
    return 'out $amount';
  }

  @override
  String cashFlowForecastRowLabel(
    String period,
    String scheduled,
    String committed,
    String pending,
    int count,
  ) {
    return '$period: scheduled $scheduled, committed $committed, pending $pending, $count invoices';
  }

  @override
  String cashFlowPositionRowLabel(
    String period,
    String opening,
    String outflow,
    String closing,
  ) {
    return '$period: opening $opening, outflow $outflow, closing $closing';
  }

  @override
  String get cashFlowBelowThresholdSuffix => ', below threshold';

  @override
  String cashFlowLowBalanceAlertLabel(String message) {
    return 'Low balance alert. $message';
  }

  @override
  String get contractsTitle => 'Contracts';

  @override
  String get contractsSearchHint => 'Search contracts...';

  @override
  String get contractsEmpty => 'No contracts found';

  @override
  String get contractsFilterDraft => 'Draft';

  @override
  String get contractsFilterActive => 'Active';

  @override
  String get contractsFilterExpired => 'Expired';

  @override
  String get contractsFilterTerminated => 'Terminated';

  @override
  String get contractsFilterCancelled => 'Cancelled';

  @override
  String get contractDetailTitle => 'Contract Detail';

  @override
  String contractDetailErrorPrefix(String error) {
    return 'Error: $error';
  }

  @override
  String get contractDetailUntitled => 'Untitled Contract';

  @override
  String get contractDetailFieldContractNumber => 'Contract #';

  @override
  String get contractDetailFieldVendor => 'Vendor';

  @override
  String get contractDetailFieldType => 'Type';

  @override
  String get contractDetailFieldCurrency => 'Currency';

  @override
  String get contractDetailFieldSpendLimit => 'Spend Limit';

  @override
  String get contractDetailNotToExceed => ' (not to exceed)';

  @override
  String get contractDetailFieldStartDate => 'Start Date';

  @override
  String get contractDetailFieldEndDate => 'End Date';

  @override
  String get contractDetailFieldSigned => 'Signed';

  @override
  String get contractDetailFieldAutoRenew => 'Auto-Renew';

  @override
  String get contractDetailYes => 'Yes';

  @override
  String get contractDetailNo => 'No';

  @override
  String get contractDetailFieldRenewalTerm => 'Renewal Term';

  @override
  String contractDetailRenewalTermMonths(int months) {
    return '$months months';
  }

  @override
  String get contractDetailFieldRenewalNotice => 'Renewal Notice';

  @override
  String contractDetailRenewalNoticeDays(int days) {
    return '$days days';
  }

  @override
  String get contractDetailFieldPaymentTerms => 'Payment Terms';

  @override
  String get contractDetailFieldDescription => 'Description';

  @override
  String get contractDetailFieldCreated => 'Created';

  @override
  String get contractDetailSectionSpend => 'Spend';

  @override
  String get contractDetailSectionLineItems => 'Line Items';

  @override
  String get contractDetailSpendInvoiced => 'Invoiced';

  @override
  String contractDetailSpendInvoiceCount(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count invoices',
      one: '$count invoice',
    );
    return '$_temp0';
  }

  @override
  String get contractDetailSpendOverLimit => 'Over Limit';

  @override
  String get contractDetailSpendRemaining => 'Remaining';

  @override
  String contractDetailSpendOfLimit(String limit) {
    return 'of $limit';
  }

  @override
  String get contractDetailSpendNoLimit => 'no limit set';

  @override
  String get contractDetailLineItemFallback => 'Line item';

  @override
  String contractDetailLineQty(String quantity) {
    return 'Qty $quantity';
  }

  @override
  String contractDetailLineUnitPrice(String price) {
    return '@ $price';
  }

  @override
  String contractDetailLineGl(String account) {
    return 'GL $account';
  }

  @override
  String get contractActivate => 'Activate';

  @override
  String get contractActivated => 'Contract activated';

  @override
  String get contractActivateFailed =>
      'Could not activate contract — please try again';

  @override
  String get contractTerminate => 'Terminate';

  @override
  String get contractTerminateTitle => 'Terminate Contract';

  @override
  String get contractTerminateBody =>
      'This ends the contract early. This cannot be undone. Continue?';

  @override
  String get contractTerminated => 'Contract terminated';

  @override
  String get contractTerminateFailed =>
      'Could not terminate contract — please try again';

  @override
  String get exceptionDetailTitle => 'Exception';

  @override
  String get exceptionDetailNotFound => 'Exception not found';

  @override
  String get exceptionDetailOverdue => 'Overdue';

  @override
  String get exceptionDetailSectionDescription => 'Description';

  @override
  String get exceptionDetailSectionInvoice => 'Invoice';

  @override
  String get exceptionDetailNoLinkedInvoice => 'No linked invoice';

  @override
  String get exceptionDetailFieldNumber => 'Number';

  @override
  String get exceptionDetailFieldVendor => 'Vendor';

  @override
  String get exceptionDetailFieldAmount => 'Amount';

  @override
  String get exceptionDetailFieldSeverity => 'Severity';

  @override
  String get exceptionDetailSectionSla => 'SLA';

  @override
  String get exceptionDetailFieldCreated => 'Created';

  @override
  String get exceptionDetailFieldDue => 'Due';

  @override
  String get exceptionDetailNoSla => 'No SLA set';

  @override
  String get exceptionDetailFieldStatus => 'Status';

  @override
  String get exceptionDetailOnTrack => 'On track';

  @override
  String get exceptionDetailResolvedIn => 'Resolved in';

  @override
  String exceptionDetailResolvedInHours(String hours) {
    return '$hours h';
  }

  @override
  String get exceptionDetailSectionAssignee => 'Assignee';

  @override
  String get exceptionDetailUnassigned => 'Unassigned';

  @override
  String get exceptionDetailAssign => 'Assign';

  @override
  String get exceptionDetailReassign => 'Reassign';

  @override
  String get exceptionDetailSectionResolution => 'Resolution';

  @override
  String get exceptionDetailResolutionNote => 'Note';

  @override
  String get exceptionDetailResolutionBy => 'By';

  @override
  String get exceptionDetailResolutionAt => 'At';

  @override
  String get exceptionDetailActionResolved => 'Exception resolved';

  @override
  String get exceptionDetailActionEscalated => 'Exception escalated';

  @override
  String get exceptionDetailActionDismissed => 'Exception dismissed';

  @override
  String get exceptionDetailActionResolveFailed =>
      'Could not resolve the exception';

  @override
  String get exceptionDetailActionEscalateFailed =>
      'Could not escalate the exception';

  @override
  String get exceptionDetailActionDismissFailed =>
      'Could not dismiss the exception';

  @override
  String get exceptionDetailAssignTo => 'Assign to';

  @override
  String get exceptionDetailUnassign => 'Unassign';

  @override
  String exceptionDetailLoadUsersFailed(String error) {
    return 'Could not load users: $error';
  }

  @override
  String get exceptionDetailAssigneeUpdateFailed =>
      'Could not update the assignee';

  @override
  String get exceptionDetailUnassigned2 => 'Exception unassigned';

  @override
  String exceptionDetailAssignedTo(String name) {
    return 'Assigned to $name';
  }

  @override
  String get settingsProcurement => 'Procurement';

  @override
  String get settingsInspections => 'Quality Inspections';

  @override
  String get settingsInspectionsHint =>
      'Record and review 4-way match inspections';

  @override
  String get settingsAdministration => 'Administration';

  @override
  String get settingsAdminUsers => 'User Management';

  @override
  String get settingsAdminUsersHint => 'Roles, activate / deactivate users';

  @override
  String get settingsAdminOrg => 'Organization Settings';

  @override
  String get settingsAdminOrgHint => 'Company profile, invoice defaults';

  @override
  String get settingsAdminWorkflows => 'Workflows';

  @override
  String get settingsAdminWorkflowsHint =>
      'View workflow definitions and steps';

  @override
  String get settingsAdaptive => 'Adaptive Workflows';

  @override
  String get settingsAdaptiveHint =>
      'Approval patterns, anomalies, suggestions';

  @override
  String get inspectionsTitle => 'Quality Inspections';

  @override
  String get inspectionsRecord => 'Record inspection';

  @override
  String get inspectionsEmpty => 'No quality inspections recorded.';

  @override
  String get inspectionsEmptyFiltered => 'No inspections with this outcome.';

  @override
  String get inspectionsLoadError => 'Could not load inspections';

  @override
  String get inspectionsResultPass => 'Pass';

  @override
  String get inspectionsResultFail => 'Fail';

  @override
  String get inspectionsResultPartial => 'Partial acceptance';

  @override
  String get inspectionsResultUnknown => 'Unknown result';

  @override
  String inspectionsResultAnnounce(String result) {
    return 'Result: $result';
  }

  @override
  String get inspectionsNotLinked => 'Not linked';

  @override
  String get inspectionsNotLinkedHint =>
      'This inspection is tied to no goods receipt and no purchase order, so PO matching will never read it.';

  @override
  String get inspectionsReceiptUnnamed => 'Receipt not named';

  @override
  String get inspectionsHintPass => 'Goods accepted — the match is unchanged.';

  @override
  String get inspectionsHintFail =>
      'Goods refused — the invoice drops to mismatch and a quality hold blocks payment.';

  @override
  String get inspectionsHintPartial =>
      'Some goods accepted — the match drops to partial and the accepted quantity is flagged.';

  @override
  String get inspectionsHintUnknown =>
      'This outcome is outside the pass / fail / partial vocabulary, so its effect on the match cannot be stated.';

  @override
  String inspectionsRecorded(String number) {
    return 'Inspection $number recorded';
  }

  @override
  String inspectionsRecordFailed(String error) {
    return 'Could not record the inspection: $error';
  }

  @override
  String inspectionsReceiptsLoadFailed(String error) {
    return 'Could not load goods receipts: $error';
  }

  @override
  String get inspectionRecordTitle => 'Record Quality Inspection';

  @override
  String get inspectionRecordClose => 'Close the inspection form';

  @override
  String get inspectionRecordReceipt => 'Goods receipt';

  @override
  String get inspectionRecordReceiptHint =>
      'PO matching only reads an inspection through its goods receipt, so an inspection has to name the delivery it covers.';

  @override
  String inspectionRecordReceiptsBounded(int shown, int total) {
    return 'Showing the $shown most recent of $total receipts. Record from the web app if the delivery you need is not listed.';
  }

  @override
  String get inspectionRecordNoReceipts =>
      'No goods receipts yet — an inspection covers a delivery, so there is nothing to record against.';

  @override
  String get inspectionRecordNumber => 'Inspection #';

  @override
  String get inspectionRecordNumberRequired => 'Enter an inspection number';

  @override
  String get inspectionRecordResult => 'Result';

  @override
  String get inspectionRecordAcceptedQuantity => 'Accepted quantity';

  @override
  String get inspectionRecordRejectedQuantity => 'Rejected quantity';

  @override
  String get inspectionRecordAcceptedRequired =>
      'Required for a partial acceptance';

  @override
  String get inspectionRecordInvalidQuantity =>
      'Use up to 8 digits and 4 decimals';

  @override
  String get inspectionRecordInspectedDate => 'Inspected date';

  @override
  String get inspectionRecordDateNotSet => 'Not set';

  @override
  String get inspectionRecordClearDate => 'Clear the inspected date';

  @override
  String get inspectionRecordInspector => 'Inspector';

  @override
  String get inspectionRecordNotes => 'Deviation notes';

  @override
  String get inspectionRecordNotesHint =>
      'Quoted into the invoice’s match issue on a failed inspection, so whoever works the quality hold reads them.';

  @override
  String get inspectionRecordSubmit => 'Record inspection';

  @override
  String get inspectionDetailTitle => 'Inspection';

  @override
  String get inspectionDetailNotFound => 'Inspection not found';

  @override
  String inspectionDetailErrorPrefix(String error) {
    return 'Could not load the inspection: $error';
  }

  @override
  String get inspectionDetailFieldReceipt => 'Goods receipt';

  @override
  String get inspectionDetailFieldInspectedDate => 'Inspected';

  @override
  String get inspectionDetailFieldInspector => 'Inspector';

  @override
  String get inspectionDetailFieldAccepted => 'Accepted quantity';

  @override
  String get inspectionDetailFieldRejected => 'Rejected quantity';

  @override
  String get inspectionDetailFieldStatus => 'Status';

  @override
  String get inspectionDetailFieldCreated => 'Created';

  @override
  String get inspectionDetailSectionNotes => 'Deviation notes';

  @override
  String get adaptiveTitle => 'Adaptive Workflows';

  @override
  String get adaptiveTabSuggestions => 'Suggestions';

  @override
  String get adaptiveTabPatterns => 'Approval patterns';

  @override
  String get adaptiveTabAnomalies => 'Anomalies';

  @override
  String get adaptiveAdvisoryNote =>
      'Everything here is advisory. Nothing on this screen has changed a workflow — a recommendation only takes effect when someone with the right role applies it in the web app, through the same audited path a manual edit uses.';

  @override
  String get adaptiveSuggestionsEmpty =>
      'No suggestions — not enough consistent approval history yet.';

  @override
  String get adaptiveSuggestionsError => 'Could not load the suggestions.';

  @override
  String get adaptiveSuggestionsShowOpen => 'Open';

  @override
  String get adaptiveSuggestionsShowAll => 'All';

  @override
  String adaptiveSuggestionsConfidence(String pct) {
    return 'Confidence $pct%';
  }

  @override
  String get adaptiveSuggestionsDismiss => 'Dismiss';

  @override
  String get adaptiveSuggestionsDismissTitle => 'Dismiss this suggestion?';

  @override
  String get adaptiveSuggestionsDismissBody =>
      'It stays dismissed across recomputation, and no workflow changes either way.';

  @override
  String get adaptiveSuggestionsDismissed => 'Suggestion dismissed.';

  @override
  String adaptiveSuggestionsDismissFailed(String error) {
    return 'Could not dismiss that suggestion: $error';
  }

  @override
  String get adaptiveSuggestionStatusOpen => 'Open';

  @override
  String get adaptiveSuggestionStatusDismissed => 'Dismissed';

  @override
  String get adaptiveSuggestionStatusApplied => 'Applied';

  @override
  String get adaptiveSuggestionStatusStale => 'Stale';

  @override
  String get adaptiveSuggestionStatusUnknown => 'Unknown status';

  @override
  String adaptiveSuggestionStatusAnnounce(String status) {
    return 'Status: $status';
  }

  @override
  String get adaptivePatternsError => 'Could not load the approval patterns.';

  @override
  String get adaptivePatternsEmptyApprovers =>
      'No approval decisions in this window yet.';

  @override
  String get adaptivePatternsEmptyVendors =>
      'No supplier approval history in this window yet.';

  @override
  String get adaptivePatternsSectionApprovers => 'By approver';

  @override
  String get adaptivePatternsSectionVendors => 'By vendor';

  @override
  String adaptivePatternsLookback(int days) {
    return 'Deterministic statistics over the last $days days of this tenant’s own approval history — no model, recomputed every time you look.';
  }

  @override
  String get adaptivePatternsCurrencyNote =>
      'Amounts are in your organization’s reporting currency.';

  @override
  String get adaptivePatternsUnknownApprover => 'Unknown approver';

  @override
  String adaptivePatternsApproverSummary(int approved, int rejected) {
    return '$approved approved · $rejected rejected';
  }

  @override
  String adaptivePatternsApproverTiming(String rate, String days) {
    return '$rate% approval rate · median $days days';
  }

  @override
  String adaptivePatternsVendorConsistency(String pct) {
    return '$pct% approved unedited';
  }

  @override
  String adaptivePatternsVendorMoney(String median, String avg) {
    return 'Median $median · average $avg';
  }

  @override
  String adaptivePatternsUnconverted(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other:
          '$count approvals could not be expressed in the reporting currency and are excluded from the amounts above — the sample still counts them',
      one:
          '$count approval could not be expressed in the reporting currency and is excluded from the amounts above — the sample still counts it',
    );
    return '$_temp0';
  }

  @override
  String get adaptiveAnomaliesIntro =>
      'Invoices in review that sit outside their own supplier’s established pattern — on amount, on approver, or on how long they have been waiting. Read-only: flagging here raises nothing and blocks nothing.';

  @override
  String get adaptiveAnomaliesEmpty =>
      'Nothing in review is outside its supplier’s normal pattern.';

  @override
  String get adaptiveAnomaliesError => 'Could not load the anomaly scan.';

  @override
  String adaptiveAnomaliesScanned(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count invoices in review scanned.',
      one: '$count invoice in review scanned.',
    );
    return '$_temp0';
  }

  @override
  String adaptiveAnomaliesAmount(String amount, String currency) {
    return '$amount $currency';
  }

  @override
  String get adaptiveAnomaliesInsufficient =>
      'Not enough history for this supplier yet.';

  @override
  String get invoiceWarningMissingVendorName => 'Missing vendor name';

  @override
  String get invoiceWarningMissingInvoiceNumber => 'Missing invoice number';

  @override
  String get invoiceWarningMissingAmount => 'Missing or zero amount';

  @override
  String get invoiceWarningDuplicateInvoiceNumber =>
      'Duplicate invoice number for this vendor';

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
          ' (plus $crossEntityCount near-identical invoices under another entity)',
      one:
          ' (plus $crossEntityCount near-identical invoice under another entity)',
      zero: '',
    );
    return 'Potential duplicate: $similarity match to $invoiceNumber from $vendorName$_temp0';
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
          ' (plus $crossEntityCount near-identical invoices under another entity)',
      one:
          ' (plus $crossEntityCount near-identical invoice under another entity)',
      zero: '',
    );
    return 'Potential duplicate: $similarity match to $invoiceNumber$_temp0';
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
          ' (plus $crossEntityCount near-identical invoices under another entity)',
      one:
          ' (plus $crossEntityCount near-identical invoice under another entity)',
      zero: '',
    );
    return 'Potential duplicate: $similarity match to another invoice from $vendorName$_temp0';
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
          ' (plus $crossEntityCount near-identical invoices under another entity)',
      one:
          ' (plus $crossEntityCount near-identical invoice under another entity)',
      zero: '',
    );
    return 'Potential duplicate: $similarity match to another invoice$_temp0';
  }

  @override
  String invoiceWarningDuplicateSimilarCrossEntity(String similarity) {
    return 'Potential duplicate: $similarity match to a near-identical invoice under another entity';
  }

  @override
  String invoiceWarningRoundAmount(String amount) {
    return 'Round amount: $amount';
  }

  @override
  String get invoiceWarningFutureInvoiceDate => 'Invoice date is in the future';

  @override
  String invoiceWarningRushPayment(int days) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days days',
      one: '$days day',
    );
    return 'Rush payment: due within $_temp0 of the invoice date';
  }

  @override
  String get invoiceWarningPastDue => 'Invoice is past due';

  @override
  String get invoiceWarningUnverifiedVendor => 'Vendor is unverified';

  @override
  String invoiceWarningPersonalEmailDomain(String domain) {
    return 'Vendor email uses personal domain: $domain';
  }

  @override
  String invoiceWarningNewVendorLargeAmount(int days, String amount) {
    String _temp0 = intl.Intl.pluralLogic(
      days,
      locale: localeName,
      other: '$days days',
      one: '$days day',
    );
    return 'New vendor (created $_temp0 ago) submitting large invoice $amount';
  }

  @override
  String get invoiceWarningRemitToChanged =>
      'Remit-to address changed since the last approved invoice for this vendor';

  @override
  String invoiceWarningAmountAboveVendorMean(
    String amount,
    String sigma,
    String mean,
  ) {
    return 'Amount $amount is $sigmaσ above this vendor\'s historical mean ($mean)';
  }

  @override
  String invoiceWarningLlmAnomaly(String reason) {
    return 'AI-flagged anomaly: $reason';
  }

  @override
  String invoiceWarningLineTotalMismatch(
    String lineItemsTotal,
    String headerAmount,
  ) {
    return 'Line items total $lineItemsTotal but the invoice amount is $headerAmount';
  }

  @override
  String invoiceWarningPriceVarianceOver(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Unit price $deltaPct over this vendor\'s baseline for $item ($unitPrice vs $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPriceVarianceUnder(
    String deltaPct,
    String item,
    String unitPrice,
    String baselineUnitPrice,
  ) {
    return 'Unit price $deltaPct under this vendor\'s baseline for $item ($unitPrice vs $baselineUnitPrice)';
  }

  @override
  String invoiceWarningPoNotFound(String poNumber) {
    return 'PO $poNumber not found';
  }

  @override
  String invoiceWarningPoAmountVariance(
    String variancePct,
    String poNumber,
    String invoiceAmount,
    String poTotal,
  ) {
    return 'Amount variance $variancePct vs PO $poNumber (invoice $invoiceAmount vs PO $poTotal)';
  }

  @override
  String invoiceWarningPoPartialReceipt(String matchType, String poNumber) {
    return 'Partial 3-way match — $matchType match against PO $poNumber, but only part of the ordered quantity has been received';
  }

  @override
  String invoiceWarningPoOverReceipt(
    String receivedQuantity,
    String orderedQuantity,
    String excessQuantity,
    String poNumber,
  ) {
    return 'Over-receipt: $receivedQuantity received against $orderedQuantity ordered (+$excessQuantity) on PO $poNumber';
  }

  @override
  String invoiceWarningPoOverReceiptUnquantified(String poNumber) {
    return 'More goods received than ordered on PO $poNumber';
  }

  @override
  String invoiceWarningQualityInspectionFailed(String poNumber) {
    return 'Failed quality inspection for PO $poNumber';
  }

  @override
  String invoiceWarningQualityInspectionFailedNotes(
    String poNumber,
    String notes,
  ) {
    return 'Failed quality inspection for PO $poNumber: $notes';
  }

  @override
  String invoiceWarningQualityInspectionMissing(String poNumber) {
    return 'Quality inspection required but missing for PO $poNumber';
  }

  @override
  String invoiceWarningQualityPartialAcceptance(
    String acceptedQuantity,
    String poNumber,
  ) {
    return 'Partial acceptance: $acceptedQuantity of the ordered quantity accepted on PO $poNumber';
  }

  @override
  String invoiceWarningQualityPartialAcceptanceUnquantified(String poNumber) {
    return 'Partial quality acceptance on inspection for PO $poNumber';
  }

  @override
  String invoiceWarningRecurringVarianceOver(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'Amount $amount is $deltaPct over the recurring template “$templateName” expected amount $expectedAmount';
  }

  @override
  String invoiceWarningRecurringVarianceUnder(
    String amount,
    String deltaPct,
    String templateName,
    String expectedAmount,
  ) {
    return 'Amount $amount is $deltaPct under the recurring template “$templateName” expected amount $expectedAmount';
  }

  @override
  String invoiceWarningContractExpired(
    String invoiceDate,
    String contractNumber,
    String endDate,
  ) {
    return 'Invoice dated $invoiceDate is after contract $contractNumber expired ($endDate)';
  }

  @override
  String invoiceWarningContractNotStarted(
    String invoiceDate,
    String contractNumber,
    String startDate,
  ) {
    return 'Invoice dated $invoiceDate predates contract $contractNumber start ($startDate)';
  }

  @override
  String invoiceWarningContractTerminated(String contractNumber) {
    return 'Spend recorded against terminated contract $contractNumber';
  }

  @override
  String invoiceWarningContractCancelled(String contractNumber) {
    return 'Spend recorded against cancelled contract $contractNumber';
  }

  @override
  String invoiceWarningContractVendorMismatch(String contractNumber) {
    return 'Invoice vendor does not match contract $contractNumber vendor';
  }

  @override
  String invoiceWarningContractSpendLimitExceeded(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'Cumulative spend $cumulativeSpend exceeds contract $contractNumber limit $spendLimit';
  }

  @override
  String invoiceWarningContractSpendLimitExceededNotToExceed(
    String cumulativeSpend,
    String contractNumber,
    String spendLimit,
  ) {
    return 'Cumulative spend $cumulativeSpend exceeds contract $contractNumber limit $spendLimit (not-to-exceed)';
  }

  @override
  String invoiceWarningContractGlNotAllowed(
    String glAccount,
    String contractNumber,
  ) {
    return 'GL account $glAccount is outside contract $contractNumber allowed accounts';
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
    return 'Amounts don\'t add up: subtotal ($subtotal) + tax ($tax) + shipping ($shipping) − discount ($discount) = $expected, but total is $amount.';
  }

  @override
  String invoiceWarningSelfCorrectionDateOrdering(
    String dueDate,
    String invoiceDate,
  ) {
    return 'Due date ($dueDate) is before invoice date ($invoiceDate).';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemsSum(
    String lineItemsTotal,
    String amount,
  ) {
    return 'Line items total ($lineItemsTotal) doesn\'t match invoice amount ($amount).';
  }

  @override
  String invoiceWarningSelfCorrectionLineItemMath(
    String lineNumber,
    String quantity,
    String unitPrice,
    String expected,
    String total,
  ) {
    return 'Line $lineNumber: $quantity × $unitPrice = $expected, but total is $total.';
  }

  @override
  String invoiceWarningGlCodesNotInChart(String codes) {
    return 'AI suggested GL code(s) not in active chart: $codes';
  }

  @override
  String invoiceWarningGlCodeStalePrior(String code) {
    return 'Cached vendor GL code “$code” is no longer in the active chart of accounts.';
  }
}
