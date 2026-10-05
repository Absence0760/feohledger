import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/invoice.dart';

/// The localized name of an invoice [status] — the one mapping from the
/// 12-state enum to reader-facing text.
///
/// `InvoiceStatus.label` is the English the model has always carried, and the
/// shared `StatusBadge` still renders it until the badge enum maps get their
/// own extraction turn (`mobile/docs/i18n.md` § String coverage). When that
/// turn comes, the badge reads this function rather than growing a second
/// map, so the bulk status sheet and the badge cannot name a state
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
