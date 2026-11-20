import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/invoice.dart';

/// The localized name of an invoice [status] — the one mapping from the
/// 12-state enum to reader-facing text.
///
/// The shared `StatusBadge` and the bulk status sheet both read this function
/// (re-exported from `enum_labels.dart`), so they cannot name a state
/// differently. The switch is exhaustive: a new enum value fails to compile
/// here instead of rendering English.
String invoiceStatusLabel(AppLocalizations l, InvoiceStatus status) =>
    switch (status) {
      InvoiceStatus.newStatus => l.invoiceStatusNew,
      InvoiceStatus.pending => l.invoiceStatusPending,
      InvoiceStatus.readyForReview => l.invoiceStatusReadyForReview,
      InvoiceStatus.approved => l.invoiceStatusApproved,
      InvoiceStatus.rejected => l.invoiceStatusRejected,
      InvoiceStatus.sendingToErp => l.invoiceStatusSendingToErp,
      InvoiceStatus.sentToErp => l.invoiceStatusSentToErp,
      InvoiceStatus.postedInErp => l.invoiceStatusPostedInErp,
      InvoiceStatus.paymentScheduled => l.invoiceStatusPaymentScheduled,
      InvoiceStatus.paid => l.invoiceStatusPaid,
      InvoiceStatus.done => l.invoiceStatusDone,
      InvoiceStatus.failed => l.invoiceStatusFailed,
    };
