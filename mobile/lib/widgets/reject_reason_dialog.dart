import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';

/// Ask for the reason an invoice (or a batch of them) is being rejected.
///
/// Resolves to the trimmed reason, or `null` when the user cancels. The
/// confirm button stays disabled until the reason has non-blank text: every
/// reject route the app calls refuses a missing one — `POST
/// /api/invoices/bulk/status` 422s a reasonless `rejected` target outright, and
/// `review.reject_invoice` writes the reason onto the audit row and the
/// `review_rejected` exception the supplier corrects from, so a blank one
/// leaves them nothing to act on.
///
/// Shared by the invoice-detail reject and the invoices-list bulk reject so the
/// two can't drift on what counts as a reason — and by the invoice-detail
/// recall of an approval (`POST /recall-approval`, backend decisions §374),
/// whose reason the server requires on the same terms. [message] explains the
/// action above the field; [reasonLabel] and [confirmLabel] default to the
/// rejection's.
Future<String?> showRejectReasonDialog(
  BuildContext context, {
  required String title,
  String? message,
  String? reasonLabel,
  String? confirmLabel,
}) {
  return showDialog<String>(
    context: context,
    builder: (_) => _RejectReasonDialog(
      title: title,
      message: message,
      reasonLabel: reasonLabel,
      confirmLabel: confirmLabel,
    ),
  );
}

class _RejectReasonDialog extends StatefulWidget {
  final String title;
  final String? message;
  final String? reasonLabel;
  final String? confirmLabel;

  const _RejectReasonDialog({
    required this.title,
    this.message,
    this.reasonLabel,
    this.confirmLabel,
  });

  @override
  State<_RejectReasonDialog> createState() => _RejectReasonDialogState();
}

class _RejectReasonDialogState extends State<_RejectReasonDialog> {
  final _controller = TextEditingController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final reason = _controller.text.trim();
    return AlertDialog(
      // The recall's explanation can outgrow a small screen beside the keyboard.
      scrollable: true,
      title: Text(widget.title),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (widget.message != null) ...[
            Text(widget.message!),
            const SizedBox(height: 16),
          ],
          TextField(
            controller: _controller,
            autofocus: true,
            decoration: InputDecoration(
              labelText: widget.reasonLabel ?? l.invoiceDetailRejectReason,
              border: const OutlineInputBorder(),
            ),
            maxLines: 3,
            onChanged: (_) => setState(() {}),
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: Text(l.commonCancel),
        ),
        FilledButton(
          onPressed: reason.isEmpty ? null : () => Navigator.pop(context, reason),
          child: Text(widget.confirmLabel ?? l.invoiceDetailReject),
        ),
      ],
    );
  }
}
