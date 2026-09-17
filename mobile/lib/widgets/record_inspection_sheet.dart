import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/goods_receipt.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/widgets/inspection_result_badge.dart';

/// A quantity is kept as the RAW TEXT the inspector typed and validated by
/// shape, never round-tripped through a `double`: the column is
/// `Numeric(12, 4)` and the API takes the string, so parsing here would
/// introduce a float in the one place the digits are meant to survive
/// untouched. Mirrors the web modal's `QUANTITY_SHAPE`.
final _quantityShape = RegExp(r'^\d{1,8}(\.\d{1,4})?$');

/// Record a quality inspection against a goods receipt — the 4th leg of 4-way
/// matching, and the one leg a warehouse phone is the natural place to enter.
///
/// Returns an [InspectionDraft] (Record) or null (Cancel / dismiss); the caller
/// owns the POST so the sheet has no network state of its own.
///
/// **A receipt is required here even though the API allows a bare row.**
/// `POST /api/inspections` accepts a body with neither `gr_id` nor `po_id` — the
/// QMS sync writes exactly that when it can resolve neither number — but
/// `po_matching` only ever reads an inspection through the matched receipt's
/// `gr_id`, else a PO-level row whose `gr_id` IS NULL. An unlinked row is
/// invisible to the match it was recorded for, so offering that as a form option
/// would let someone record a failed inspection, see it listed, and watch the
/// invoice pay anyway. The receipt carries the PO, so both ids go up together.
/// Same call as the web `RecordInspectionModal`.
class RecordInspectionSheet extends StatefulWidget {
  /// The receipts the picker offers. One page, newest first — `GET
  /// /api/goods-receipts` has no search, so [totalReceipts] is shown whenever it
  /// exceeds what is listed rather than implying the picker holds everything.
  final List<GoodsReceipt> receipts;

  /// How many receipts the tenant has, from the list envelope's `total`. When it
  /// exceeds [receipts] the form says so and points at the web app, rather than
  /// letting an inspector conclude their delivery does not exist.
  final int totalReceipts;

  const RecordInspectionSheet({
    super.key,
    required this.receipts,
    required this.totalReceipts,
  });

  @override
  State<RecordInspectionSheet> createState() => _RecordInspectionSheetState();
}

class _RecordInspectionSheetState extends State<RecordInspectionSheet> {
  final _formKey = GlobalKey<FormState>();
  final _numberController = TextEditingController();
  final _inspectorController = TextEditingController();
  final _acceptedController = TextEditingController();
  final _rejectedController = TextEditingController();
  final _notesController = TextEditingController();

  GoodsReceipt? _receipt;
  InspectionResult _result = InspectionResult.pass;
  DateTime? _inspectedDate;

  /// True once the inspector edits the number, so changing the selected receipt
  /// stops overwriting what they typed.
  bool _numberTouched = false;

  @override
  void initState() {
    super.initState();
    _receipt = widget.receipts.isEmpty ? null : widget.receipts.first;
    _suggestNumber();
  }

  @override
  void dispose() {
    _numberController.dispose();
    _inspectorController.dispose();
    _acceptedController.dispose();
    _rejectedController.dispose();
    _notesController.dispose();
    super.dispose();
  }

  /// Suggest `QI-<receipt>` until the inspector types their own.
  /// `inspection_number` has no uniqueness constraint on the create path (the
  /// QMS sync's upsert key is the only place it is treated as a natural key), so
  /// this is a convenience, not a guarantee.
  void _suggestNumber() {
    if (_numberTouched) return;
    final gr = _receipt;
    _numberController.text = gr == null ? '' : 'QI-${gr.grNumber}';
  }

  /// Quantities are only meaningful once something was refused or part-accepted.
  bool get _quantitiesShown => _result != InspectionResult.pass;

  /// `accepted_quantity` is what the matcher renders into its partial-acceptance
  /// issue, so a `partial` without one produces "Partial acceptance: part of
  /// ordered quantity accepted" — true, and useless to whoever reads it.
  bool get _acceptedRequired => _result == InspectionResult.partial;

  String? _validateQuantity(String? raw, {required bool required}) {
    final l = AppLocalizations.of(context);
    final value = (raw ?? '').trim();
    if (value.isEmpty) {
      return required ? l.inspectionRecordAcceptedRequired : null;
    }
    return _quantityShape.hasMatch(value)
        ? null
        : l.inspectionRecordInvalidQuantity;
  }

  Future<void> _pickDate() async {
    final picked = await showDatePicker(
      context: context,
      initialDate: _inspectedDate ?? DateTime.now(),
      firstDate: DateTime(2000),
      lastDate: DateTime(2100),
    );
    if (picked != null && mounted) setState(() => _inspectedDate = picked);
  }

  void _submit() {
    final gr = _receipt;
    if (gr == null) return;
    if (!(_formKey.currentState?.validate() ?? false)) return;
    Navigator.of(context).pop(
      InspectionDraft(
        inspectionNumber: _numberController.text.trim(),
        grId: gr.id,
        poId: gr.poId,
        result: _result,
        inspectedDate: _inspectedDate,
        inspector: _inspectorController.text.trim(),
        acceptedQuantity:
            _quantitiesShown ? _acceptedController.text.trim() : null,
        rejectedQuantity:
            _quantitiesShown ? _rejectedController.text.trim() : null,
        deviationNotes: _notesController.text.trim(),
      ),
    );
  }

  String _resultHint(AppLocalizations l) => switch (_result) {
    InspectionResult.pass => l.inspectionsHintPass,
    InspectionResult.fail => l.inspectionsHintFail,
    InspectionResult.partial => l.inspectionsHintPartial,
    InspectionResult.unknown => '',
  };

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final bottomInset = MediaQuery.of(context).viewInsets.bottom;
    return Padding(
      // Lift the sheet above the keyboard.
      padding: EdgeInsets.only(bottom: bottomInset),
      child: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 16),
          child: Form(
            key: _formKey,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        l.inspectionRecordTitle,
                        style: const TextStyle(
                          fontSize: 18,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    ),
                    Semantics(
                      label: l.inspectionRecordClose,
                      button: true,
                      child: IconButton(
                        tooltip: l.commonClose,
                        icon: const Icon(Icons.close),
                        onPressed: () => Navigator.of(context).pop(),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                if (widget.receipts.isEmpty)
                  Text(
                    l.inspectionRecordNoReceipts,
                    style: TextStyle(color: Colors.grey.shade700),
                  )
                else ...[
                  _receiptPicker(l),
                  const SizedBox(height: 12),
                  TextFormField(
                    controller: _numberController,
                    maxLength: 100,
                    textInputAction: TextInputAction.next,
                    decoration: InputDecoration(
                      labelText: l.inspectionRecordNumber,
                      counterText: '',
                    ),
                    onChanged: (_) => _numberTouched = true,
                    validator: (v) => (v == null || v.trim().isEmpty)
                        ? l.inspectionRecordNumberRequired
                        : null,
                  ),
                  const SizedBox(height: 16),
                  _resultPicker(l),
                  if (_quantitiesShown) ...[
                    const SizedBox(height: 12),
                    _quantityField(
                      controller: _acceptedController,
                      label: l.inspectionRecordAcceptedQuantity,
                      required: _acceptedRequired,
                    ),
                    const SizedBox(height: 12),
                    _quantityField(
                      controller: _rejectedController,
                      label: l.inspectionRecordRejectedQuantity,
                      required: false,
                    ),
                  ],
                  const SizedBox(height: 12),
                  _dateField(l),
                  const SizedBox(height: 12),
                  TextFormField(
                    controller: _inspectorController,
                    maxLength: 255,
                    decoration: InputDecoration(
                      labelText: l.inspectionRecordInspector,
                      counterText: '',
                    ),
                  ),
                  const SizedBox(height: 12),
                  TextFormField(
                    controller: _notesController,
                    maxLines: 3,
                    decoration: InputDecoration(
                      labelText: l.inspectionRecordNotes,
                      // The matcher quotes these notes verbatim into the
                      // invoice's "Failed quality inspection: …" issue, so they
                      // are read by whoever works the resulting quality hold.
                      helperText: l.inspectionRecordNotesHint,
                      helperMaxLines: 3,
                    ),
                  ),
                ],
                const SizedBox(height: 20),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton(
                        onPressed: () => Navigator.of(context).pop(),
                        style: OutlinedButton.styleFrom(
                          padding: const EdgeInsets.symmetric(vertical: 14),
                        ),
                        child: Text(l.commonCancel),
                      ),
                    ),
                    const SizedBox(width: 16),
                    Expanded(
                      child: FilledButton(
                        onPressed: widget.receipts.isEmpty ? null : _submit,
                        style: FilledButton.styleFrom(
                          padding: const EdgeInsets.symmetric(vertical: 14),
                        ),
                        child: Text(l.inspectionRecordSubmit),
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _receiptPicker(AppLocalizations l) {
    final bounded = widget.totalReceipts > widget.receipts.length;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        DropdownButtonFormField<String>(
          initialValue: _receipt?.id,
          isExpanded: true,
          decoration: InputDecoration(
            labelText: l.inspectionRecordReceipt,
            helperText: l.inspectionRecordReceiptHint,
            helperMaxLines: 4,
          ),
          items: [
            for (final gr in widget.receipts)
              DropdownMenuItem<String>(
                value: gr.id,
                child: Text(gr.label, overflow: TextOverflow.ellipsis),
              ),
          ],
          onChanged: (id) => setState(() {
            _receipt = widget.receipts.firstWhere((gr) => gr.id == id);
            _suggestNumber();
          }),
        ),
        if (bounded)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(
              l.inspectionRecordReceiptsBounded(
                widget.receipts.length,
                widget.totalReceipts,
              ),
              style: TextStyle(color: Colors.grey.shade700, fontSize: 12),
            ),
          ),
      ],
    );
  }

  /// The three outcomes are one choice, so they are a segmented control with the
  /// selected outcome's consequence spelled out underneath — a `fail` blocks
  /// payment, and the form should say so before it is recorded.
  Widget _resultPicker(AppLocalizations l) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          l.inspectionRecordResult,
          style: TextStyle(
            color: Colors.grey.shade700,
            fontSize: 13,
            fontWeight: FontWeight.w600,
          ),
        ),
        const SizedBox(height: 8),
        SegmentedButton<InspectionResult>(
          segments: [
            for (final option in InspectionResult.selectable)
              ButtonSegment<InspectionResult>(
                value: option,
                label: Text(inspectionResultLabel(l, option)),
              ),
          ],
          selected: {_result},
          showSelectedIcon: false,
          onSelectionChanged: (selected) =>
              setState(() => _result = selected.first),
        ),
        const SizedBox(height: 8),
        Text(
          _resultHint(l),
          style: TextStyle(color: Colors.grey.shade700, fontSize: 12),
        ),
      ],
    );
  }

  Widget _quantityField({
    required TextEditingController controller,
    required String label,
    required bool required,
  }) {
    return TextFormField(
      controller: controller,
      keyboardType: const TextInputType.numberWithOptions(decimal: true),
      inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[\d.]'))],
      decoration: InputDecoration(labelText: label),
      validator: (v) => _validateQuantity(v, required: required),
    );
  }

  Widget _dateField(AppLocalizations l) {
    final date = _inspectedDate;
    return Row(
      children: [
        Expanded(
          child: InkWell(
            onTap: _pickDate,
            child: InputDecorator(
              decoration: InputDecoration(
                labelText: l.inspectionRecordInspectedDate,
              ),
              child: Text(
                date == null
                    ? l.inspectionRecordDateNotSet
                    : formatDate(date),
              ),
            ),
          ),
        ),
        if (date != null)
          Semantics(
            label: l.inspectionRecordClearDate,
            button: true,
            child: IconButton(
              tooltip: l.commonClear,
              icon: const Icon(Icons.clear),
              onPressed: () => setState(() => _inspectedDate = null),
            ),
          ),
      ],
    );
  }
}
