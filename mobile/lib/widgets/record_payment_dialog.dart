import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/coded_refusal_messages.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/action_verdict.dart';
import 'package:feohledger_mobile/models/payment_queue.dart';
import 'package:feohledger_mobile/stores/payment_queue_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/utils/money.dart';

// Recording a payment made OUTSIDE FeohLedger — the no-rail pilot (issue #517,
// backend decisions §251), the mobile half of the web payments page's
// "Record as paid" dialog and the run dialog's "Record run as paid". Neither
// moves money: the customer already paid from its own bank or ERP, and the
// server books a completed `external` payment and walks the invoice to `paid`.
//
// The server owns every rule. The invoice dialog is opened only from a queue
// row whose `record_outside_refusal` is null; the run dialog offers its form
// only on the run detail's `actions.record_outside` verdict. A refusal that
// still comes back (segregation of duties depends on who records; a run's
// per-invoice refusals need the invoice locked) is stated beside the form in
// the reader's language, and only there — one error, one signal.

/// The localized label for a raw rail code from [kExternalPaymentMethods].
String externalMethodLabel(AppLocalizations l, String code) => switch (code) {
      'ach' => l.payMethodAch,
      'wire' => l.payMethodWire,
      'check' => l.payMethodCheck,
      'bacs' => l.payMethodBacs,
      'faster_payments' => l.payMethodFasterPayments,
      'chaps' => l.payMethodChaps,
      'card' => l.payMethodCardOutside,
      _ => code,
    };

/// Today on the reader's own calendar — the latest paid-on date offered. The
/// server allows one day past the UTC date (`api/payments.
/// _refuse_future_paid_on`) precisely so a reader east of UTC can record
/// "today"; a later local date is in the future wherever the reader is.
DateTime _localToday() {
  final now = DateTime.now();
  return DateTime(now.year, now.month, now.day);
}

/// The sentence for a refused record: the coded refusal in the reader's
/// language, else the server's own sentence, else [fallback].
String _refusalText(
  AppLocalizations l,
  Object? detail,
  String? message,
  String fallback,
) =>
    localizeCodedRefusal(l, detail) ??
    ((message != null && message.trim().isNotEmpty) ? message : fallback);

/// Record one queue row as paid outside FeohLedger. Resolves `true` when it
/// was recorded (the store has already re-read the queue).
Future<bool?> showRecordInvoicePaymentDialog(
  BuildContext context,
  PaymentQueueItem item,
) =>
    showDialog<bool>(
      context: context,
      builder: (_) => _RecordInvoicePaymentDialog(item: item),
    );

/// Record a whole run as paid outside FeohLedger. Resolves to the number of
/// payments recorded, or `null` when nothing was.
Future<int?> showRecordRunPaymentDialog(BuildContext context, PaymentRun run) =>
    showDialog<int>(
      context: context,
      builder: (_) => _RecordRunPaymentDialog(run: run),
    );

/// The paid-on field: the date as the reader writes it, a date picker capped
/// at today, and a semantics label naming both.
class _PaidOnField extends StatelessWidget {
  final DateTime value;
  final ValueChanged<DateTime> onChanged;
  final bool enabled;

  const _PaidOnField({
    required this.value,
    required this.onChanged,
    required this.enabled,
  });

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final shown = formatDate(value);
    return Semantics(
      button: true,
      label: '${l.payRecordPaidOn}, $shown',
      excludeSemantics: true,
      onTap: enabled ? () => _pick(context) : null,
      child: InkWell(
        onTap: enabled ? () => _pick(context) : null,
        child: InputDecorator(
          decoration: InputDecoration(
            labelText: l.payRecordPaidOn,
            suffixIcon: const Icon(Icons.calendar_today),
          ),
          child: Text(shown),
        ),
      ),
    );
  }

  Future<void> _pick(BuildContext context) async {
    final today = _localToday();
    final picked = await showDatePicker(
      context: context,
      initialDate: value.isAfter(today) ? today : value,
      firstDate: DateTime(2000),
      lastDate: today,
    );
    if (picked != null) onChanged(picked);
  }
}

/// The refusal / failure line beside a form. Announced when it appears.
class _ErrorLine extends StatelessWidget {
  final String text;
  const _ErrorLine(this.text);

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 12),
        child: Semantics(
          liveRegion: true,
          child: Text(
            text,
            // Darkened red for AA contrast on the dialog surface.
            style: TextStyle(color: Colors.red.shade900, fontSize: 13),
          ),
        ),
      );
}

TextStyle _hintStyle() => TextStyle(color: Colors.grey.shade700, fontSize: 12);

class _RecordInvoicePaymentDialog extends StatefulWidget {
  final PaymentQueueItem item;
  const _RecordInvoicePaymentDialog({required this.item});

  @override
  State<_RecordInvoicePaymentDialog> createState() =>
      _RecordInvoicePaymentDialogState();
}

class _RecordInvoicePaymentDialogState
    extends State<_RecordInvoicePaymentDialog> {
  final _reference = TextEditingController();
  String _method = kExternalPaymentMethods.first;
  DateTime _paidOn = _localToday();
  bool _submitting = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _reference.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _reference.dispose();
    super.dispose();
  }

  bool get _canSubmit => !_submitting && _reference.text.trim().isNotEmpty;

  Future<void> _submit() async {
    final l = AppLocalizations.of(context);
    setState(() {
      _submitting = true;
      _error = null;
    });
    final outcome = await PaymentQueueStore.instance.recordOutside(
      widget.item,
      method: _method,
      reference: _reference.text,
      paidOn: formatIsoDate(_paidOn),
    );
    if (!mounted) return;
    if (outcome.ok) {
      Navigator.of(context).pop(true);
      return;
    }
    final text =
        _refusalText(l, outcome.detail, outcome.message, l.payRecordFailed);
    setState(() {
      _submitting = false;
      _error = text;
    });
    A11y.announce(context, text);
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final item = widget.item;
    final owed = item.payableAmountDisplay;
    return AlertDialog(
      title: Text(l.payRecordTitle),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(l.payRecordIntro),
            const SizedBox(height: 12),
            Text('${item.vendorName} · ${item.invoiceNumber}',
                style: const TextStyle(fontWeight: FontWeight.w600)),
            // What the server will book, in the invoice's own currency. A
            // backend that predates `payable_amount` shows no figure rather
            // than the gross amount, which is not what gets recorded.
            if (owed != null) ...[
              const SizedBox(height: 8),
              Text(
                '${l.payRecordAmountOwed}: '
                '${formatMoneyString(owed, currency: item.currency)}',
              ),
              const SizedBox(height: 4),
              Text(l.payRecordAmountHint, style: _hintStyle()),
            ],
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              initialValue: _method,
              decoration: InputDecoration(labelText: l.payRecordMethod),
              items: [
                for (final code in kExternalPaymentMethods)
                  DropdownMenuItem(
                    value: code,
                    child: Text(externalMethodLabel(l, code)),
                  ),
              ],
              onChanged: _submitting
                  ? null
                  : (v) {
                      if (v != null) setState(() => _method = v);
                    },
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _reference,
              enabled: !_submitting,
              maxLength: 255,
              decoration: InputDecoration(
                labelText: l.payRecordReference,
                helperText: l.payRecordReferenceHint,
                helperMaxLines: 3,
              ),
            ),
            const SizedBox(height: 12),
            _PaidOnField(
              value: _paidOn,
              enabled: !_submitting,
              onChanged: (d) => setState(() => _paidOn = d),
            ),
            if (_error != null) _ErrorLine(_error!),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: _submitting ? null : () => Navigator.of(context).pop(),
          child: Text(l.payConfirmCancel),
        ),
        FilledButton(
          onPressed: _canSubmit ? _submit : null,
          child: Text(_submitting ? l.payRecordSubmitting : l.payRecordSubmit),
        ),
      ],
    );
  }
}

class _RecordRunPaymentDialog extends StatefulWidget {
  final PaymentRun run;
  const _RecordRunPaymentDialog({required this.run});

  @override
  State<_RecordRunPaymentDialog> createState() =>
      _RecordRunPaymentDialogState();
}

class _RecordRunPaymentDialogState extends State<_RecordRunPaymentDialog> {
  final _reference = TextEditingController();
  DateTime _paidOn = _localToday();
  PaymentRunRecordDetail? _detail;
  bool _loading = true;
  bool _loadFailed = false;
  bool _submitting = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _reference.addListener(() => setState(() {}));
    _load();
  }

  @override
  void dispose() {
    _reference.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final detail =
          await PaymentQueueStore.instance.loadRunRecordDetail(widget.run.id);
      if (!mounted) return;
      setState(() {
        _detail = detail;
        _loading = false;
      });
    } catch (_) {
      if (!mounted) return;
      // Fail closed: with no verdict there is no form.
      setState(() {
        _loading = false;
        _loadFailed = true;
      });
    }
  }

  ActionVerdict get _verdict =>
      _detail?.recordOutside ?? ActionVerdict.unanswered;

  bool get _canSubmit =>
      _verdict.allowed && !_submitting && _reference.text.trim().isNotEmpty;

  Future<void> _submit() async {
    final l = AppLocalizations.of(context);
    setState(() {
      _submitting = true;
      _error = null;
    });
    final outcome = await PaymentQueueStore.instance.recordRunOutside(
      widget.run.id,
      reference: _reference.text,
      paidOn: formatIsoDate(_paidOn),
    );
    if (!mounted) return;
    if (outcome.ok) {
      Navigator.of(context).pop(outcome.paymentCount ?? 0);
      return;
    }
    // A coded refusal names the invoice that stopped the WHOLE run (nothing
    // was recorded), so it stays beside the form.
    final text =
        _refusalText(l, outcome.detail, outcome.message, l.payRunRecordFailed);
    setState(() {
      _submitting = false;
      _error = text;
    });
    A11y.announce(context, text);
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final run = _detail?.run ?? widget.run;
    final total = formatMoneyString(run.totalAmountDisplay, currency: run.currency);
    final Widget body;
    if (_loading) {
      body = const Padding(
        padding: EdgeInsets.symmetric(vertical: 24),
        child: Center(child: CircularProgressIndicator()),
      );
    } else if (_loadFailed) {
      body = _ErrorLine(l.payRunRecordLoadFailed);
    } else if (!_verdict.allowed) {
      // The server's verdict for THIS reader: maker-checker, CFO sign-off, a
      // run that is not draft / exported, nothing left to record. Stated,
      // never a form that could only be refused.
      final refusal = _verdict.refusal;
      body = _ErrorLine(_refusalText(
        l,
        refusal,
        refusal?['message'] as String?,
        l.payRunRecordFailed,
      ));
    } else {
      body = Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(l.payRunRecordIntro),
          const SizedBox(height: 12),
          TextField(
            controller: _reference,
            enabled: !_submitting,
            maxLength: 255,
            decoration: InputDecoration(
              labelText: l.payRunRecordReference,
              helperText: l.payRunRecordReferenceHint,
              helperMaxLines: 3,
            ),
          ),
          const SizedBox(height: 12),
          _PaidOnField(
            value: _paidOn,
            enabled: !_submitting,
            onChanged: (d) => setState(() => _paidOn = d),
          ),
          if (_error != null) _ErrorLine(_error!),
        ],
      );
    }
    return AlertDialog(
      title: Text(l.payRunRecordTitle),
      content: SingleChildScrollView(child: body),
      actions: [
        TextButton(
          onPressed: _submitting ? null : () => Navigator.of(context).pop(),
          child: Text(l.payConfirmCancel),
        ),
        if (_verdict.allowed)
          FilledButton(
            onPressed: _canSubmit ? _submit : null,
            child: Text(_submitting
                ? l.payRecordSubmitting
                : l.payRunRecordSubmit(total)),
          ),
      ],
    );
  }
}
