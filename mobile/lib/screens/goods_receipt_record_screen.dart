import 'package:material_ui/material_ui.dart';
import 'package:flutter/services.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/coded_refusal_messages.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/goods_receipt.dart';
import 'package:feohledger_mobile/stores/goods_receipt_store.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/utils/debouncer.dart';
import 'package:feohledger_mobile/utils/numbers.dart';

/// A quantity is kept as the RAW TEXT typed and validated by shape, never
/// parsed into a `double`: the column is `Numeric(12, 4)` and the API takes the
/// string. The same rule as the inspection sheet's.
final _quantityShape = RegExp(r'^\d{1,8}(\.\d{1,4})?$');

bool _positive(String raw) =>
    _quantityShape.hasMatch(raw) && RegExp(r'[1-9]').hasMatch(raw);

/// Record a delivery against a purchase order — the 3-way matching leg. The
/// mobile half of the web `RecordReceiptModal`, on the same endpoints:
/// `GET /api/purchase-orders` (picker), `GET /api/purchase-orders/{id}` (lines)
/// and `POST /api/goods-receipts` with one `Idempotency-Key` per opened form.
///
/// Each PO line shows ordered / received so far / outstanding, and "this
/// delivery" is pre-filled with what is outstanding, because the common case is
/// "it all came". A line with nothing outstanding starts BLANK: a pre-filled 0
/// would be posted as "expected but short", a claim the user never made. A PO
/// the ERP synced with no lines takes free-text lines instead.
///
/// Pops with the recorded receipt's number.
class GoodsReceiptRecordScreen extends StatefulWidget {
  const GoodsReceiptRecordScreen({super.key});

  @override
  State<GoodsReceiptRecordScreen> createState() =>
      _GoodsReceiptRecordScreenState();
}

class _FreeLine {
  final description = TextEditingController();
  final quantity = TextEditingController();
  void dispose() {
    description.dispose();
    quantity.dispose();
  }
}

class _GoodsReceiptRecordScreenState extends State<GoodsReceiptRecordScreen> {
  final _store = GoodsReceiptStore.instance;
  final _formKey = GlobalKey<FormState>();
  final _search = TextEditingController();
  final _number = TextEditingController();
  final _debouncer = Debouncer(delay: const Duration(milliseconds: 250));

  /// Per PO line id: the "this delivery" field.
  final Map<String, TextEditingController> _quantities = {};
  List<_FreeLine> _freeLines = [_FreeLine()];

  /// The PO whose lines [_quantities] was filled for.
  String? _filledFor;
  DateTime _receivedDate = DateTime.now();
  bool _showLinesError = false;

  @override
  void initState() {
    super.initState();
    _store.openForm();
    _store.addListener(_onStore);
    _store.search('');
  }

  @override
  void dispose() {
    _store.removeListener(_onStore);
    _debouncer.cancel();
    _search.dispose();
    _number.dispose();
    for (final c in _quantities.values) {
      c.dispose();
    }
    for (final l in _freeLines) {
      l.dispose();
    }
    super.dispose();
  }

  /// Re-seed the line fields when a different PO's lines arrive.
  void _onStore() {
    final po = _store.po;
    if (po == null || po.id == _filledFor) return;
    _filledFor = po.id;
    for (final c in _quantities.values) {
      c.dispose();
    }
    _quantities
      ..clear()
      ..addEntries(
        po.lines.map((li) {
          final left = li.outstandingScaled;
          return MapEntry(
            li.id,
            TextEditingController(text: left > 0 ? quantityString(left) : ''),
          );
        }),
      );
    for (final l in _freeLines) {
      l.dispose();
    }
    _freeLines = [_FreeLine()];
    _showLinesError = false;
  }

  bool get _hasPoLines => (_store.po?.lines.isNotEmpty ?? false);

  bool _over(ReceivablePOLine li) {
    final raw = _quantities[li.id]?.text.trim() ?? '';
    if (li.orderedScaled == null || !_quantityShape.hasMatch(raw)) return false;
    final parts = raw.split('.');
    final frac = (parts.length > 1 ? parts[1] : '').padRight(4, '0');
    final scaled = int.parse(parts[0]) * 10000 + int.parse(frac);
    return scaled > li.outstandingScaled;
  }

  List<GoodsReceiptLineDraft> _lines() {
    final po = _store.po!;
    if (_hasPoLines) {
      return [
        for (final li in po.lines)
          if ((_quantities[li.id]?.text.trim() ?? '').isNotEmpty)
            GoodsReceiptLineDraft(
              poLineItemId: li.id,
              quantityReceived: _quantities[li.id]!.text.trim(),
            ),
      ];
    }
    return [
      for (final l in _freeLines)
        if (l.description.text.trim().isNotEmpty ||
            l.quantity.text.trim().isNotEmpty)
          GoodsReceiptLineDraft(
            description: l.description.text.trim(),
            quantityReceived: l.quantity.text.trim(),
          ),
    ];
  }

  Future<void> _submit() async {
    final po = _store.po;
    if (po == null || po.isCancelled || _store.saving) return;
    final valid = _formKey.currentState?.validate() ?? false;
    final lines = _lines();
    final anyPositive = lines.any((l) => _positive(l.quantityReceived));
    setState(() => _showLinesError = !anyPositive);
    if (!valid || !anyPositive) return;
    final number = await _store.submit(
      GoodsReceiptDraft(
        poId: po.id,
        receivedDate: _receivedDate,
        grNumber: _number.text,
        lines: lines,
      ),
    );
    if (number != null && mounted) Navigator.of(context).pop(number);
  }

  Future<void> _pickDate() async {
    final picked = await showDatePicker(
      context: context,
      initialDate: _receivedDate,
      firstDate: DateTime(2000),
      // The server allows up to a day past its UTC date (east of UTC, "today"
      // is already tomorrow there); the picker allows the device's today.
      lastDate: DateTime.now(),
    );
    if (picked != null && mounted) setState(() => _receivedDate = picked);
  }

  String? _validateQuantity(String? raw, AppLocalizations l) {
    final v = (raw ?? '').trim();
    if (v.isEmpty) return null;
    return _quantityShape.hasMatch(v) ? null : l.inspectionRecordInvalidQuantity;
  }

  String _errorText(AppLocalizations l, Object error, String fallback) {
    if (error is ApiException) {
      return localizeCodedRefusal(l, error.detail) ??
          (error.message.isNotEmpty ? error.message : fallback);
    }
    return fallback;
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.grRecordTitle)),
      body: ListenableBuilder(
        listenable: _store,
        builder: (context, _) => Form(
          key: _formKey,
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Text(l.grRecordIntro),
              const SizedBox(height: 16),
              TextField(
                key: const Key('gr-po-search'),
                controller: _search,
                decoration: InputDecoration(
                  labelText: l.grRecordSearchPo,
                  hintText: l.grRecordSearchPlaceholder,
                  prefixIcon: const Icon(Icons.search),
                ),
                onChanged: (v) => _debouncer.run(() => _store.search(v)),
              ),
              const SizedBox(height: 12),
              _poPicker(l),
              ..._poBody(l),
              if (_store.submitError != null) ...[
                const SizedBox(height: 12),
                _errorBox(
                  _errorText(l, _store.submitError!, l.grRecordFailed),
                  key: const Key('gr-submit-error'),
                ),
              ],
              const SizedBox(height: 20),
              FilledButton(
                key: const Key('gr-submit'),
                onPressed:
                    _store.po == null ||
                        _store.po!.isCancelled ||
                        _store.poLoading ||
                        _store.saving
                    ? null
                    : _submit,
                style: FilledButton.styleFrom(
                  padding: const EdgeInsets.symmetric(vertical: 14),
                ),
                child: Text(
                  _store.saving ? l.grRecordSaving : l.grRecordSubmit,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _poPicker(AppLocalizations l) {
    if (_store.searchError != null) {
      return _errorBox(
        _errorText(l, _store.searchError!, l.grRecordLoadPosFailed),
      );
    }
    if (_store.options.isEmpty) {
      return Semantics(
        liveRegion: true,
        child: Text(
          _store.searching ? l.commonLoading : l.grRecordNoPos,
          key: const Key('gr-no-pos'),
        ),
      );
    }
    return DropdownButtonFormField<String>(
      key: ValueKey('gr-po-${_store.pickedId}'),
      initialValue: _store.pickedId,
      isExpanded: true,
      decoration: InputDecoration(labelText: l.grRecordPo),
      items: [
        for (final o in _store.options)
          DropdownMenuItem(
            value: o.id,
            child: Text(o.label, overflow: TextOverflow.ellipsis),
          ),
      ],
      onChanged: (id) => _store.pick(id),
    );
  }

  List<Widget> _poBody(AppLocalizations l) {
    if (_store.poLoading) {
      return [
        const Padding(
          padding: EdgeInsets.all(16),
          child: Center(child: CircularProgressIndicator()),
        ),
      ];
    }
    if (_store.poError != null) {
      return [
        const SizedBox(height: 12),
        _errorBox(_errorText(l, _store.poError!, l.grRecordLoadPoFailed)),
      ];
    }
    final po = _store.po;
    if (po == null) return const [];
    if (po.isCancelled) {
      return [
        const SizedBox(height: 12),
        Semantics(
          liveRegion: true,
          child: Text(
            l.grRecordPoCancelled(po.poNumber),
            key: const Key('gr-po-cancelled'),
          ),
        ),
      ];
    }
    return [
      const SizedBox(height: 16),
      if (_hasPoLines) ...[
        for (final li in po.lines) _poLine(l, li),
        Text(
          l.grRecordLinesHint,
          style: TextStyle(color: Colors.grey.shade700, fontSize: 12),
        ),
      ] else ...[
        Text(l.grRecordNoPoLines),
        for (var i = 0; i < _freeLines.length; i++) _freeLine(l, i),
        Align(
          alignment: AlignmentDirectional.centerStart,
          child: TextButton.icon(
            icon: const Icon(Icons.add),
            label: Text(l.grRecordAddLine),
            onPressed: () => setState(() => _freeLines.add(_FreeLine())),
          ),
        ),
      ],
      if (_showLinesError)
        Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Semantics(
            liveRegion: true,
            child: Text(
              l.grRecordReceiveLines,
              key: const Key('gr-lines-error'),
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ),
        ),
      const SizedBox(height: 16),
      InkWell(
        onTap: _pickDate,
        child: InputDecorator(
          decoration: InputDecoration(labelText: l.grRecordReceivedDate),
          child: Text(formatDate(_receivedDate)),
        ),
      ),
      const SizedBox(height: 12),
      TextFormField(
        key: const Key('gr-number'),
        controller: _number,
        maxLength: 100,
        decoration: InputDecoration(
          labelText: l.grRecordNumber,
          hintText: l.grRecordNumberPlaceholder(po.poNumber),
          helperText: l.grRecordNumberHint,
          helperMaxLines: 2,
          counterText: '',
        ),
      ),
      const SizedBox(height: 12),
      // Receiving is a duty segregated from approval (backend decisions §267,
      // §389): say so before the receipt is recorded, not at the approval
      // door afterwards.
      Card(
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Icon(Icons.info_outline, size: 20),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  l.grRecordSegregationNote,
                  key: const Key('gr-segregation-note'),
                  style: const TextStyle(fontSize: 12),
                ),
              ),
            ],
          ),
        ),
      ),
    ];
  }

  String _qty(int? scaled) =>
      scaled == null ? '—' : formatDecimalString(quantityString(scaled));

  Widget _poLine(AppLocalizations l, ReceivablePOLine li) {
    final controller = _quantities[li.id]!;
    final name = li.description ?? '—';
    final muted = TextStyle(color: Colors.grey.shade700, fontSize: 12);
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(
            li.sku == null ? name : '$name · ${li.sku}',
            style: const TextStyle(fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 4),
          Wrap(
            spacing: 16,
            children: [
              Text('${l.grRecordOrdered}: ${_qty(li.orderedScaled)}', style: muted),
              Text(
                '${l.grRecordAlreadyReceived}: ${_qty(li.receivedScaled)}',
                style: muted,
              ),
              Text(
                '${l.grRecordOutstanding}: '
                '${li.orderedScaled == null ? '—' : _qty(li.outstandingScaled)}',
                style: muted,
              ),
            ],
          ),
          const SizedBox(height: 4),
          TextFormField(
            key: Key('gr-line-qty-${li.id}'),
            controller: controller,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            inputFormatters: [
              FilteringTextInputFormatter.allow(RegExp(r'[\d.]')),
            ],
            decoration: InputDecoration(
              labelText: l.grRecordThisDelivery,
              helperText: _over(li) ? l.grRecordOverReceipt : null,
              helperMaxLines: 2,
            ),
            onChanged: (_) => setState(() {}),
            validator: (v) => _validateQuantity(v, l),
          ),
        ],
      ),
    );
  }

  Widget _freeLine(AppLocalizations l, int i) {
    final line = _freeLines[i];
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            flex: 3,
            child: TextFormField(
              key: Key('gr-free-desc-$i'),
              controller: line.description,
              maxLength: 2000,
              decoration: InputDecoration(
                labelText: l.grRecordLineDescription(i + 1),
                counterText: '',
              ),
              validator: (v) =>
                  line.quantity.text.trim().isNotEmpty &&
                      (v ?? '').trim().isEmpty
                  ? l.codedRefusalGoodsReceiptLineRequired
                  : null,
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            flex: 2,
            child: TextFormField(
              key: Key('gr-free-qty-$i'),
              controller: line.quantity,
              keyboardType: const TextInputType.numberWithOptions(
                decimal: true,
              ),
              inputFormatters: [
                FilteringTextInputFormatter.allow(RegExp(r'[\d.]')),
              ],
              decoration: InputDecoration(
                labelText: l.grRecordLineQuantity(i + 1),
              ),
              validator: (v) => line.description.text.trim().isNotEmpty &&
                      (v ?? '').trim().isEmpty
                  ? l.inspectionRecordInvalidQuantity
                  : _validateQuantity(v, l),
            ),
          ),
          if (_freeLines.length > 1)
            IconButton(
              tooltip: l.grRecordRemoveLine(i + 1),
              icon: const Icon(Icons.close),
              onPressed: () => setState(() {
                _freeLines.removeAt(i).dispose();
              }),
            ),
        ],
      ),
    );
  }

  Widget _errorBox(String text, {Key? key}) {
    final scheme = Theme.of(context).colorScheme;
    return Semantics(
      liveRegion: true,
      child: Container(
        key: key,
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          border: Border.all(color: scheme.error),
          borderRadius: BorderRadius.circular(6),
        ),
        child: Text(text, style: TextStyle(color: scheme.onSurface)),
      ),
    );
  }
}
