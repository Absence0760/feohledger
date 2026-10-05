import 'package:flutter/material.dart';

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
/// two can't drift on what counts as a reason.
Future<String?> showRejectReasonDialog(
  BuildContext context, {
  required String title,
}) {
  return showDialog<String>(
    context: context,
    builder: (_) => _RejectReasonDialog(title: title),
  );
}

class _RejectReasonDialog extends StatefulWidget {
  final String title;

  const _RejectReasonDialog({required this.title});

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
      title: Text(widget.title),
      content: TextField(
        controller: _controller,
        autofocus: true,
        decoration: InputDecoration(
          labelText: l.invoiceDetailRejectReason,
          border: const OutlineInputBorder(),
        ),
        maxLines: 3,
        onChanged: (_) => setState(() {}),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: Text(l.commonCancel),
        ),
        FilledButton(
          onPressed: reason.isEmpty ? null : () => Navigator.pop(context, reason),
          child: Text(l.invoiceDetailReject),
        ),
      ],
    );
  }
}
