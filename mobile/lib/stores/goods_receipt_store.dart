import 'dart:math';

import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/models/goods_receipt.dart';
import 'package:feohledger_mobile/utils/sequenced_fetch.dart';

/// The record-a-goods-receipt form's state: the PO picker, the picked PO's
/// lines, and the POST. The mobile half of the web `RecordReceiptModal`.
///
/// Two request sequences, as the web form keeps: the PO search (the mixin's
/// token) and the PO detail ([_poSeq]), so a slow search answering late can't
/// overwrite a newer one, and a slow PO load can't replace the lines of the PO
/// picked after it.
///
/// **Not offline-cached.** It exists to be written to, and a receipt is a fact
/// about what arrived — a stale "received so far" would pre-fill the wrong
/// outstanding quantity.
class GoodsReceiptStore extends ChangeNotifier with SequencedFetch {
  static final GoodsReceiptStore instance = GoodsReceiptStore._();
  GoodsReceiptStore._();

  final RequestSequence _poSeq = RequestSequence();

  List<ReceivablePO> _options = [];
  bool _searching = false;
  Object? _searchError;
  String? _pickedId;
  ReceivablePODetail? _po;
  bool _poLoading = false;
  Object? _poError;
  bool _saving = false;
  Object? _submitError;
  String _idempotencyKey = _mintKey();

  List<ReceivablePO> get options => _options;
  bool get searching => _searching;
  Object? get searchError => _searchError;
  String? get pickedId => _pickedId;
  ReceivablePODetail? get po => _po;
  bool get poLoading => _poLoading;
  Object? get poError => _poError;
  bool get saving => _saving;

  /// The server's refusal of the last submit — the raw error, so the screen
  /// can state a coded refusal in the reader's language.
  Object? get submitError => _submitError;

  /// The key the next submit carries. Exposed for tests.
  @visibleForTesting
  String get idempotencyKey => _idempotencyKey;

  static String _mintKey() {
    final rand = Random.secure();
    return List.generate(
      16,
      (_) => rand.nextInt(256).toRadixString(16).padLeft(2, '0'),
    ).join();
  }

  /// Start a fresh form: clear the last one and mint a new idempotency key —
  /// one per opened form, so a retry of THIS form replays and a new form books
  /// a new receipt.
  void openForm() {
    reset();
    _idempotencyKey = _mintKey();
  }

  void reset() {
    _options = [];
    _searching = false;
    _searchError = null;
    _pickedId = null;
    _po = null;
    _poLoading = false;
    _poError = null;
    _saving = false;
    _submitError = null;
    debugResetSequence();
    _poSeq.reset();
  }

  /// Search POs by number. The pick must stay one of the options on screen: a
  /// search that no longer lists the picked PO moves the pick to its first
  /// result, so the form never receives against a PO the picker isn't showing.
  Future<void> search(String text) async {
    final token = nextRequestToken();
    _searching = true;
    notifyListeners();
    try {
      final rows = await GoodsReceiptApi.searchPurchaseOrders(text);
      if (!isCurrentRequest(token)) return;
      _options = rows;
      _searchError = null;
      _searching = false;
      if (!rows.any((o) => o.id == _pickedId)) {
        await pick(rows.isEmpty ? null : rows.first.id);
      } else {
        notifyListeners();
      }
    } catch (e) {
      if (!isCurrentRequest(token)) return;
      _searching = false;
      _searchError = e;
      notifyListeners();
    }
  }

  /// Pick a PO and load its lines.
  Future<void> pick(String? id) async {
    _pickedId = id;
    _submitError = null;
    final token = _poSeq.next();
    if (id == null) {
      _po = null;
      _poLoading = false;
      _poError = null;
      notifyListeners();
      return;
    }
    _poLoading = true;
    _poError = null;
    notifyListeners();
    try {
      final detail = await GoodsReceiptApi.getPurchaseOrder(id);
      if (!_poSeq.isCurrent(token)) return;
      _po = detail;
      _poLoading = false;
      notifyListeners();
    } catch (e) {
      if (!_poSeq.isCurrent(token)) return;
      _po = null;
      _poLoading = false;
      _poError = e;
      notifyListeners();
    }
  }

  /// Record the receipt. Returns its number on success, or null with
  /// [submitError] set.
  Future<String?> submit(GoodsReceiptDraft draft) async {
    if (_saving) return null;
    _saving = true;
    _submitError = null;
    notifyListeners();
    try {
      final number = await GoodsReceiptApi.create(
        draft,
        idempotencyKey: _idempotencyKey,
      );
      _saving = false;
      notifyListeners();
      return number;
    } catch (e) {
      _saving = false;
      _submitError = e;
      notifyListeners();
      return null;
    }
  }
}
