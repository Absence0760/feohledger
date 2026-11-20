/// The localized name of every model enum a shared widget renders — one
/// exhaustive switch per enum, so a new enum value fails to compile here
/// instead of rendering English.
///
/// The models carry no English names of their own: every screen and widget
/// reads these (`test/l10n/enum_labels_test.dart`,
/// `test/widgets/shared_widget_l10n_test.dart`).
library;

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/contract.dart';
import 'package:feohledger_mobile/models/exception.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/models/vendor.dart';

export 'package:feohledger_mobile/l10n/invoice_status_labels.dart'
    show invoiceStatusLabel;

String vendorStatusLabel(AppLocalizations l, VendorStatus status) =>
    switch (status) {
      VendorStatus.active => l.vendorStatusActive,
      VendorStatus.unverified => l.vendorStatusUnverified,
      VendorStatus.inactive => l.vendorStatusInactive,
      VendorStatus.rejected => l.vendorStatusRejected,
    };

/// Where a vendor record came from. The wire value is free-form, so a source
/// this build does not know is named as such rather than echoed raw.
String vendorSourceLabel(AppLocalizations l, String source) =>
    switch (source) {
      'erp_sync' => l.vendorSourceErp,
      'ai_extracted' => l.vendorSourceAi,
      'manual' => l.vendorSourceManual,
      _ => l.vendorSourceOther,
    };

String contractStatusLabel(AppLocalizations l, ContractStatus status) =>
    switch (status) {
      ContractStatus.draft => l.contractStatusDraft,
      ContractStatus.active => l.contractStatusActive,
      ContractStatus.expired => l.contractStatusExpired,
      ContractStatus.terminated => l.contractStatusTerminated,
      ContractStatus.cancelled => l.contractStatusCancelled,
    };

String contractTypeLabel(AppLocalizations l, ContractType type) =>
    switch (type) {
      ContractType.purchase => l.contractTypePurchase,
      ContractType.service => l.contractTypeService,
      ContractType.subscription => l.contractTypeSubscription,
      ContractType.lease => l.contractTypeLease,
      ContractType.sla => l.contractTypeSla,
      ContractType.msa => l.contractTypeMsa,
      ContractType.sow => l.contractTypeSow,
      ContractType.other => l.contractTypeOther,
    };

String exceptionStatusLabel(AppLocalizations l, ApExceptionStatus status) =>
    switch (status) {
      ApExceptionStatus.open => l.exceptionStatusOpen,
      ApExceptionStatus.escalated => l.exceptionStatusEscalated,
      ApExceptionStatus.resolved => l.exceptionStatusResolved,
      ApExceptionStatus.dismissed => l.exceptionStatusDismissed,
    };

String exceptionSeverityLabel(
  AppLocalizations l,
  ApExceptionSeverity severity,
) =>
    switch (severity) {
      ApExceptionSeverity.error => l.exceptionSeverityError,
      ApExceptionSeverity.warning => l.exceptionSeverityWarning,
      ApExceptionSeverity.info => l.exceptionSeverityInfo,
    };

String paymentMethodLabel(AppLocalizations l, PaymentMethod method) =>
    switch (method) {
      PaymentMethod.ach => l.payMethodAch,
      PaymentMethod.wire => l.payMethodWire,
      PaymentMethod.check => l.payMethodCheck,
      PaymentMethod.virtualCard => l.payMethodVirtualCard,
    };

String paymentStatusLabel(AppLocalizations l, PaymentStatus status) =>
    switch (status) {
      PaymentStatus.pending => l.paymentStatusPending,
      PaymentStatus.processing => l.paymentStatusProcessing,
      PaymentStatus.completed => l.paymentStatusCompleted,
      PaymentStatus.failed => l.paymentStatusFailed,
      PaymentStatus.cancelled => l.paymentStatusCancelled,
    };

/// The originating event of a notification. An event type this build does
/// not know gets a generic name: the wire value is a code, not copy.
String notificationEventLabel(AppLocalizations l, String eventType) =>
    switch (eventType) {
      'invoice_assigned' => l.notificationEventInvoiceAssigned,
      'invoice_approved' => l.notificationEventInvoiceApproved,
      'invoice_rejected' => l.notificationEventInvoiceRejected,
      'invoice_paid' => l.notificationEventInvoicePaid,
      'contract_renewal_due' => l.notificationEventContractRenewalDue,
      'chat_message' => l.notificationEventChatMessage,
      _ => l.notificationEventOther,
    };
