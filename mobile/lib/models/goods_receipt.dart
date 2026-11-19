import 'package:feohledger_mobile/utils/dates.dart';

/// One goods receipt, as `backend/app/api/goods_receipts.py::list_goods_receipts`
/// returns it.
///
/// Mobile reads receipts for the record-inspection picker (an inspection has to
/// name the delivery it covers) and records new ones
/// (`screens/goods_receipt_record_screen.dart`). The receipts list and the
/// 3-way-match detail stay on the web.
class GoodsReceipt {
  final String id;
  final String grNumber;
  final String? poId;

  /// The PO's human-readable number, resolved server-side for the rendered page.
  /// `null` when the receipt is against no PO.
  final String? poNumber;
  final DateTime? receivedDate;
  final String status;
  final int lineCount;

  const GoodsReceipt({
    required this.id,
    required this.grNumber,
    this.poId,
    this.poNumber,
    this.receivedDate,
    required this.status,
    required this.lineCount,
  });

  /// `GR-123 → PO-2026-005`, or the receipt number alone when it has no PO —
  /// the label the picker shows, matching the web modal's option text.
  String get label => poNumber == null ? grNumber : '$grNumber → $poNumber';

  factory GoodsReceipt.fromJson(Map<String, dynamic> json) {
    return GoodsReceipt(
      id: json['id'] as String? ?? '',
      grNumber: json['gr_number'] as String? ?? '',
      poId: json['po_id'] as String?,
      poNumber: json['po_number'] as String?,
      receivedDate: DateTime.tryParse(json['received_date'] as String? ?? ''),
      status: json['status'] as String? ?? '',
      lineCount: (json['line_count'] as num?)?.toInt() ?? 0,
    );
  }
}

/// A count of goods in ten-thousandths, the scale of the `Numeric(12, 4)`
/// quantity columns. Worked as an integer so `10 - 9.3` is `0.7`, not
/// `0.6999999999999993` — this figure pre-fills a field the user submits.
/// Mirrors the web `types/goodsReceipt.ts::remainingQuantity`.
int _scaled(num? value) => value == null ? 0 : (value * 10000).round();

/// `scaled` ten-thousandths as the plain decimal string the API accepts
/// (`12.5`, `3`), with no float on the way.
String quantityString(int scaled) {
  final negative = scaled < 0;
  final abs = scaled.abs();
  final whole = abs ~/ 10000;
  final frac = (abs % 10000).toString().padLeft(4, '0').replaceAll(
    RegExp(r'0+$'),
    '',
  );
  final body = frac.isEmpty ? '$whole' : '$whole.$frac';
  return negative ? '-$body' : body;
}

/// PO statuses nothing can be received against — the backend's
/// `CANCELLED_PO_STATUSES`, which refuses them with
/// `goods_receipt_po_cancelled`; the form says so before the user types.
const _cancelledPoStatuses = {'cancelled', 'voided'};

/// One purchase order as the record-receipt picker lists it
/// (`GET /api/purchase-orders`).
class ReceivablePO {
  final String id;
  final String poNumber;
  final String? vendorName;
  final String status;

  const ReceivablePO({
    required this.id,
    required this.poNumber,
    this.vendorName,
    required this.status,
  });

  /// `PO-1 — Acme`, the web picker's option text.
  String get label => vendorName == null ? poNumber : '$poNumber — $vendorName';

  bool get isCancelled =>
      _cancelledPoStatuses.contains(status.trim().toLowerCase());

  factory ReceivablePO.fromJson(Map<String, dynamic> json) => ReceivablePO(
    id: json['id'] as String? ?? '',
    poNumber: json['po_number'] as String? ?? '',
    vendorName: json['vendor_name'] as String?,
    status: json['status'] as String? ?? '',
  );
}

/// A PO line with what has already arrived against it
/// (`GET /api/purchase-orders/{id}` → `line_items`). The API sends these
/// counts as JSON numbers; they are held as ten-thousandths, never shown or
/// submitted as a `double`.
class ReceivablePOLine {
  final String id;
  final String? description;
  final String? sku;

  /// Ordered quantity in ten-thousandths; null when the PO line names none.
  final int? orderedScaled;
  final int receivedScaled;

  const ReceivablePOLine({
    required this.id,
    this.description,
    this.sku,
    this.orderedScaled,
    required this.receivedScaled,
  });

  /// What is still outstanding — never negative (an over-receipt leaves
  /// nothing outstanding), and 0 when nothing was ordered.
  int get outstandingScaled {
    final ordered = orderedScaled;
    if (ordered == null) return 0;
    final left = ordered - receivedScaled;
    return left < 0 ? 0 : left;
  }

  factory ReceivablePOLine.fromJson(Map<String, dynamic> json) {
    final ordered = json['quantity'];
    return ReceivablePOLine(
      id: json['id'] as String? ?? '',
      description: json['description'] as String?,
      sku: json['sku'] as String?,
      orderedScaled: ordered is num ? _scaled(ordered) : null,
      receivedScaled: _scaled(json['quantity_received'] as num?),
    );
  }
}

class ReceivablePODetail extends ReceivablePO {
  final List<ReceivablePOLine> lines;

  const ReceivablePODetail({
    required super.id,
    required super.poNumber,
    super.vendorName,
    required super.status,
    required this.lines,
  });

  factory ReceivablePODetail.fromJson(Map<String, dynamic> json) {
    final head = ReceivablePO.fromJson(json);
    return ReceivablePODetail(
      id: head.id,
      poNumber: head.poNumber,
      vendorName: head.vendorName,
      status: head.status,
      lines: ((json['line_items'] as List?) ?? const [])
          .map((e) => ReceivablePOLine.fromJson(e as Map<String, dynamic>))
          .toList(),
    );
  }
}

/// One line of a receipt being recorded. Exactly one of [poLineItemId] (a PO
/// that has lines) or [description] (a PO synced with none) is set.
class GoodsReceiptLineDraft {
  final String? poLineItemId;
  final String? description;

  /// The quantity as the dot-decimal STRING the user typed — never a double,
  /// so the digits reach the `Numeric(12, 4)` column without a float round trip.
  final String quantityReceived;

  const GoodsReceiptLineDraft({
    this.poLineItemId,
    this.description,
    required this.quantityReceived,
  });

  Map<String, dynamic> toJson() => {
    if (poLineItemId != null) 'po_line_item_id': poLineItemId,
    if (description != null) 'description': description,
    'quantity_received': quantityReceived,
  };
}

/// The `POST /api/goods-receipts` body (`schemas/goods_receipt.py`).
class GoodsReceiptDraft {
  final String poId;
  final DateTime receivedDate;
  final String? grNumber;
  final List<GoodsReceiptLineDraft> lines;

  const GoodsReceiptDraft({
    required this.poId,
    required this.receivedDate,
    this.grNumber,
    required this.lines,
  });

  Map<String, dynamic> toJson() => {
    'po_id': poId,
    // The user's own calendar date; the server allows a day past its UTC
    // date for exactly this reason.
    'received_date': formatIsoDate(receivedDate),
    if (grNumber != null && grNumber!.trim().isNotEmpty)
      'gr_number': grNumber!.trim(),
    'lines': [for (final l in lines) l.toJson()],
  };
}
