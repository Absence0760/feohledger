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
// the reader's language, and only there — one error, one signal. It is spoken
// once, by `A11y.announce` where it is set: the line itself is not a live
// region, which made TalkBack say it twice (pinned in
// `test/a11y/accessibility_test.dart`).
//
// A queue row the ERP reported paid (`erp_reported_payment`, a live
// `paid_not_recorded` exception) opens pre-filled from that report, as the
// web's dialog does (`erpPrefill`), and shows the ERP's figure and its
// rail-by-rail split beside the form. Where the server says the split stands,
// the record goes through the exception's own one-click
// (`record-erp-payment`), naming no rail: `record-outside` would refuse it
// (`external_payment_erp_split`, decisions §426).

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

/// The refusal / failure line beside a form. Deliberately NOT a live region:
/// whoever sets the text announces it once through [A11y.announce], and a
/// live region on top of that spoke every refusal twice.
class _ErrorLine extends StatelessWidget {
  final String text;
  const _ErrorLine(this.text);

  @override
  Widget build(BuildContext context) => Padding(
        key: const Key('record-error'),
        padding: const EdgeInsets.only(top: 12),
        child: Text(
          text,
          // Darkened red for AA contrast on the dialog surface.
          style: TextStyle(color: Colors.red.shade900, fontSize: 13),
        ),
      );
}

/// The ERP's breakdown of the payment it reported, rail by rail, and what the
/// record does with it — the web's `ErpPaymentSplit`. Nothing is summed here;
/// whether the parts add up is the server's verdict ([stands]).
class _ErpSplit extends StatelessWidget {
  final QueueErpReportedPayment reported;
  final String erp;
  const _ErpSplit({required this.reported, required this.erp});

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final note = switch (reported.splitStands) {
      true => l.payRecordSplitStands,
      false => l.payRecordSplitDropped,
      null => l.payRecordSplitUnjudged,
    };
    return Column(
      key: const Key('record-erp-split'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const SizedBox(height: 12),
        Semantics(
          header: true,
          child: Text(l.payRecordSplitTitle(erp),
              style: const TextStyle(fontWeight: FontWeight.w600)),
        ),
        for (final part in reported.parts)
          MergeSemantics(
            child: Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Row(
                children: [
                  Expanded(
                    child: Text(part.rail == null
                        ? l.payRecordRailNotStated
                        : externalMethodLabel(l, part.rail!)),
                  ),
                  Text(formatMoneyString(part.amount,
                      currency: reported.amountCurrency)),
                ],
              ),
            ),
          ),
        const SizedBox(height: 4),
        Text(note, style: _hintStyle()),
      ],
    );
  }
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

  /// `null` = not chosen: the ERP named no rail a payment made outside
  /// FeohLedger can be recorded on, and guessing one (the dialog used to start
  /// on ACH whatever the ERP said) is wrong for a cheque.
  String? _method = kExternalPaymentMethods.first;
  DateTime _paidOn = _localToday();
  bool _submitting = false;
  String? _error;

  QueueErpReportedPayment? get _reported => widget.item.erpReportedPayment;

  /// Where the ERP's split is the record: no rail is asked for or sent.
  bool get _splitIsTheRecord => _reported?.splitIsTheRecord ?? false;

  @override
  void initState() {
    super.initState();
    final reported = _reported;
    if (reported != null) {
      // What the ERP already said — rail, reference, date — so nobody
      // re-keys it.
      final prefill = reported.prefill(_localToday());
      _method = prefill.method;
      _reference.text = prefill.reference;
      _paidOn = prefill.paidOn;
    }
    _reference.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _reference.dispose();
    super.dispose();
  }

  bool get _canSubmit =>
      !_submitting &&
      _reference.text.trim().isNotEmpty &&
      (_splitIsTheRecord || _method != null);

  Future<void> _submit() async {
    final l = AppLocalizations.of(context);
    setState(() {
      _submitting = true;
      _error = null;
    });
    final store = PaymentQueueStore.instance;
    final paidOn = formatIsoDate(_paidOn);
    final reported = _reported;
    final outcome = _splitIsTheRecord && reported != null
        ? await store.recordErpSplit(
            widget.item,
            reported,
            reference: _reference.text,
            paidOn: paidOn,
          )
        : await store.recordOutside(
            widget.item,
            method: _method!,
            reference: _reference.text,
            paidOn: paidOn,
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
    final reported = _reported;
    final erp = reported == null
        ? null
        : (reported.erpName ?? l.exceptionDetailYourErp);
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
            // What the ERP says was paid, in the code it stated: rendered,
            // never compared here (the server refuses a figure that differs).
            if (reported != null && erp != null) ...[
              if (reported.amount != null) ...[
                const SizedBox(height: 8),
                Text(
                  key: const Key('record-erp-amount'),
                  '${l.payRecordErpAmount(erp)}: '
                  '${formatMoneyString(reported.amount!, currency: reported.amountCurrency)}',
                ),
              ],
              const SizedBox(height: 8),
              Text(l.payRecordPrefilledFromErp(erp), style: _hintStyle()),
              if (reported.showsSplit) _ErpSplit(reported: reported, erp: erp),
            ],
            const SizedBox(height: 12),
            if (_splitIsTheRecord)
              InputDecorator(
                key: const Key('record-method-split'),
                decoration: InputDecoration(labelText: l.payRecordMethod),
                child: Text(l.payRecordSplitMethod),
              )
            else
              DropdownButtonFormField<String>(
                key: const Key('record-method'),
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
              key: const Key('record-reference'),
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
          key: const Key('record-submit'),
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
      // A refusal replaces the form, so it is spoken once as it appears.
      if (!_verdict.allowed) A11y.announce(context, _verdictText());
    } catch (_) {
      if (!mounted) return;
      // Fail closed: with no verdict there is no form.
      setState(() {
        _loading = false;
        _loadFailed = true;
      });
      A11y.announce(context, AppLocalizations.of(context).payRunRecordLoadFailed);
    }
  }

  /// The server's verdict for THIS reader, as the sentence shown for it.
  String _verdictText() {
    final l = AppLocalizations.of(context);
    final refusal = _verdict.refusal;
    return _refusalText(
      l,
      refusal,
      refusal?['message'] as String?,
      l.payRunRecordFailed,
    );
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
      body = _ErrorLine(_verdictText());
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
