import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/utils/dates.dart';

/// Modal bottom-sheet form for the invoice advanced search — vendor, PO number,
/// amount range and due-date range (parity with the web `AdvancedSearchModal`).
/// Pure UI: it returns an [InvoiceSearchFilters] on Apply (or
/// [InvoiceSearchFilters.empty] on Clear), and null on dismiss; the caller
/// applies it to the [InvoiceStore]. Seeded with the currently-active filters
/// so re-opening shows what's in effect.
class AdvancedSearchSheet extends StatefulWidget {
  final InvoiceSearchFilters initial;

  const AdvancedSearchSheet({super.key, required this.initial});

  @override
  State<AdvancedSearchSheet> createState() => _AdvancedSearchSheetState();
}

class _AdvancedSearchSheetState extends State<AdvancedSearchSheet> {
  final _formKey = GlobalKey<FormState>();
  late final TextEditingController _vendor;
  late final TextEditingController _poNumber;
  late final TextEditingController _amountMin;
  late final TextEditingController _amountMax;
  DateTime? _dueDateFrom;
  DateTime? _dueDateTo;

  @override
  void initState() {
    super.initState();
    final f = widget.initial;
    _vendor = TextEditingController(text: f.vendor ?? '');
    _poNumber = TextEditingController(text: f.poNumber ?? '');
    _amountMin =
        TextEditingController(text: f.amountMin != null ? _num(f.amountMin!) : '');
    _amountMax =
        TextEditingController(text: f.amountMax != null ? _num(f.amountMax!) : '');
    _dueDateFrom = f.dueDateFrom;
    _dueDateTo = f.dueDateTo;
  }

  @override
  void dispose() {
    _vendor.dispose();
    _poNumber.dispose();
    _amountMin.dispose();
    _amountMax.dispose();
    super.dispose();
  }

  static String _num(double v) {
    final s = v.toString();
    return s.endsWith('.0') ? s.substring(0, s.length - 2) : s;
  }

  String? _validateAmount(String? raw) {
    final v = (raw ?? '').trim();
    if (v.isEmpty) return null;
    if (!RegExp(r'^\d+(\.\d+)?$').hasMatch(v)) {
      return AppLocalizations.of(context).advSearchInvalidAmount;
    }
    return null;
  }

  String? _validateRange(String? _) {
    final min = double.tryParse(_amountMin.text.trim());
    final max = double.tryParse(_amountMax.text.trim());
    if (min != null && max != null && min > max) {
      return AppLocalizations.of(context).advSearchMinMaxError;
    }
    return null;
  }

  InvoiceSearchFilters _build() {
    final vendor = _vendor.text.trim();
    final po = _poNumber.text.trim();
    return InvoiceSearchFilters(
      vendor: vendor.isEmpty ? null : vendor,
      poNumber: po.isEmpty ? null : po,
      amountMin: double.tryParse(_amountMin.text.trim()),
      amountMax: double.tryParse(_amountMax.text.trim()),
      dueDateFrom: _dueDateFrom,
      dueDateTo: _dueDateTo,
    );
  }

  void _apply() {
    if (!(_formKey.currentState?.validate() ?? false)) return;
    Navigator.of(context).pop(_build());
  }

  void _clear() => Navigator.of(context).pop(InvoiceSearchFilters.empty);

  Future<void> _pickDate({required bool from}) async {
    final current = from ? _dueDateFrom : _dueDateTo;
    final picked = await showDatePicker(
      context: context,
      initialDate: current ?? DateTime.now(),
      firstDate: DateTime(2000),
      lastDate: DateTime(2100),
    );
    if (picked == null || !mounted) return;
    setState(() {
      if (from) {
        _dueDateFrom = picked;
      } else {
        _dueDateTo = picked;
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final bottomInset = MediaQuery.of(context).viewInsets.bottom;
    return Padding(
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
                        l.advSearchTitle,
                        style: const TextStyle(
                          fontSize: 18,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    ),
                    Semantics(
                      label: l.advSearchClose,
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
                _field(_vendor, l.advSearchVendor,
                    textInputAction: TextInputAction.next),
                _field(_poNumber, l.advSearchPoNumber),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(
                      child: _field(
                        _amountMin,
                        l.advSearchMinAmount,
                        keyboardType: const TextInputType.numberWithOptions(
                          decimal: true,
                        ),
                        inputFormatters: [
                          FilteringTextInputFormatter.allow(RegExp(r'[\d.]')),
                        ],
                        validator: (v) =>
                            _validateAmount(v) ?? _validateRange(v),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: _field(
                        _amountMax,
                        l.advSearchMaxAmount,
                        keyboardType: const TextInputType.numberWithOptions(
                          decimal: true,
                        ),
                        inputFormatters: [
                          FilteringTextInputFormatter.allow(RegExp(r'[\d.]')),
                        ],
                        validator: _validateAmount,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Row(
                  children: [
                    Expanded(
                      child: _dateField(
                        label: l.advSearchDueFrom,
                        value: _dueDateFrom,
                        onTap: () => _pickDate(from: true),
                        onClear: () => setState(() => _dueDateFrom = null),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: _dateField(
                        label: l.advSearchDueTo,
                        value: _dueDateTo,
                        onTap: () => _pickDate(from: false),
                        onClear: () => setState(() => _dueDateTo = null),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 20),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton(
                        onPressed: _clear,
                        style: OutlinedButton.styleFrom(
                          padding: const EdgeInsets.symmetric(vertical: 14),
                        ),
                        child: Text(l.commonClear),
                      ),
                    ),
                    const SizedBox(width: 16),
                    Expanded(
                      child: FilledButton(
                        onPressed: _apply,
                        style: FilledButton.styleFrom(
                          padding: const EdgeInsets.symmetric(vertical: 14),
                        ),
                        child: Text(l.commonApply),
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

  Widget _field(
    TextEditingController controller,
    String label, {
    TextInputType? keyboardType,
    List<TextInputFormatter>? inputFormatters,
    String? Function(String?)? validator,
    TextInputAction? textInputAction,
  }) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: TextFormField(
        controller: controller,
        keyboardType: keyboardType,
        inputFormatters: inputFormatters,
        validator: validator,
        textInputAction: textInputAction,
        decoration: InputDecoration(
          labelText: label,
          border: const OutlineInputBorder(),
        ),
      ),
    );
  }

  Widget _dateField({
    required String label,
    required DateTime? value,
    required VoidCallback onTap,
    required VoidCallback onClear,
  }) {
    final l = AppLocalizations.of(context);
    final text =
        value != null ? formatDate(value) : l.advSearchAny;
    return Semantics(
      label: l.advSearchDateFieldHint(label, text),
      button: true,
      child: InkWell(
        onTap: onTap,
        child: InputDecorator(
          decoration: InputDecoration(
            labelText: label,
            border: const OutlineInputBorder(),
          ),
          child: Row(
            children: [
              Expanded(
                child: Text(text, overflow: TextOverflow.ellipsis),
              ),
              if (value != null)
                Semantics(
                  label: l.advSearchClearField(label),
                  button: true,
                  child: IconButton(
                    tooltip: l.commonClear,
                    icon: const Icon(Icons.clear, size: 18),
                    onPressed: onClear,
                  ),
                )
              else
                const Icon(Icons.calendar_today, size: 18),
            ],
          ),
        ),
      ),
    );
  }
}

/// Open the advanced-search sheet seeded with [initial]; resolves to the chosen
/// [InvoiceSearchFilters] on Apply, [InvoiceSearchFilters.empty] on Clear, or
/// null if dismissed.
Future<InvoiceSearchFilters?> showAdvancedSearchSheet(
  BuildContext context,
  InvoiceSearchFilters initial,
) {
  return showModalBottomSheet<InvoiceSearchFilters>(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (_) => AdvancedSearchSheet(initial: initial),
  );
}
