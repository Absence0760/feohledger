/// Server paging for a `ChangeNotifier` list store: the rows loaded so far,
/// the server's `total` for the whole request, and a [PagedList.loadMore] that
/// appends the next page.
///
/// Every list endpoint pages (`app/api/pagination.py`, 20 rows by default). A
/// store that keeps only the first response shows 20 of 57 waiting invoices,
/// counts them as 20, and leaves the other 37 unreachable. [PagedFetch] is the
/// one place that state lives, so a list screen can offer "Load more" and say
/// "Showing all N" from the server's count, not from what it happens to hold.
///
/// Sequencing builds on [SequencedFetch]: the store's first-page `fetch()`
/// takes a token as before; a next-page read reuses the current token without
/// issuing one, so a new fetch (a filter change, a refresh) discards a page
/// still in flight, while a page landing never cancels the fetch it extends.
library;

import 'package:flutter/foundation.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/utils/sequenced_fetch.dart';

/// The paging state of one server-paged list, bound to the [RequestSequence]
/// that owns its first-page request.
///
/// [PagedFetch] holds one for a store's primary list. A store with a second,
/// independent list (`InvoiceStore`'s approvals queue) holds another
/// instance on that list's own sequence.
class PagedList<T> {
  PagedList(this._seq, {this._keyOf});

  final RequestSequence _seq;

  /// A row's identity, to drop a row a later page repeats (rows shift between
  /// page reads when another user's action reorders the list). Null keeps
  /// every row.
  final Object? Function(T item)? _keyOf;

  List<T> _items = const [];
  int _total = 0;
  int _page = 0;
  bool _loadingMore = false;
  Object? _loadMoreError;

  List<T> get items => _items;

  /// Every row the request matches, per the server — not `items.length`.
  int get total => _total;

  /// The last page loaded (1-based); 0 when nothing paged is held (empty, or
  /// rows served from the offline cache, which cannot be paged).
  int get page => _page;

  /// A next page exists on the server.
  bool get hasMore => _page > 0 && _items.length < _total;

  bool get loadingMore => _loadingMore;

  /// The last next-page read's failure, as caught. The rows already shown are
  /// still live, so this never drives the list's load-error state.
  Object? get loadMoreError => _loadMoreError;

  /// A new first-page request has started: a next-page read in flight is now
  /// stale, so stop reporting it.
  void restart() {
    _loadingMore = false;
    _loadMoreError = null;
  }

  /// The first page of a fresh request has landed.
  void setFirstPage(PagedResult<T> result) {
    _items = List.unmodifiable(result.items);
    _total = result.total < result.items.length
        ? result.items.length
        : result.total;
    _page = 1;
    _loadingMore = false;
    _loadMoreError = null;
  }

  /// Rows served from the offline cache: shown as they are, with no paging.
  void setCached(List<T> items) {
    _items = List.unmodifiable(items);
    _total = items.length;
    _page = 0;
    _loadingMore = false;
    _loadMoreError = null;
  }

  void clear() {
    _items = const [];
    _total = 0;
    _page = 0;
    _loadingMore = false;
    _loadMoreError = null;
  }

  /// Read page `page + 1` with [fetchPage] and append it. A no-op without a
  /// next page or while one is already loading. [notify] is the store's
  /// `notifyListeners`. Returns whether rows were appended.
  Future<bool> loadMore(
    Future<PagedResult<T>> Function(int page) fetchPage,
    VoidCallback notify,
  ) async {
    if (!hasMore || _loadingMore) return false;
    final token = _seq.current;
    _loadingMore = true;
    _loadMoreError = null;
    notify();
    try {
      final result = await fetchPage(_page + 1);
      if (!_seq.isCurrent(token)) return false; // a fresh fetch replaced us
      final keyOf = _keyOf;
      final List<T> added;
      if (keyOf == null) {
        added = result.items;
      } else {
        final seen = _items.map(keyOf).toSet();
        added = result.items.where((item) => seen.add(keyOf(item))).toList();
      }
      _items = List.unmodifiable([..._items, ...added]);
      _page += 1;
      // An empty page means the server holds no more than we do, whatever an
      // earlier count said — stop offering a next page that returns nothing.
      _total = result.items.isEmpty
          ? _items.length
          : (result.total < _items.length ? _items.length : result.total);
      _loadingMore = false;
      notify();
      return added.isNotEmpty;
    } catch (e) {
      if (!_seq.isCurrent(token)) return false;
      _loadingMore = false;
      _loadMoreError = e;
      notify();
      return false;
    }
  }
}

/// A [SequencedFetch] store's primary list, paged. The store implements
/// [fetchPage] (its list endpoint at the current filters, for one page), calls
/// `pages.restart()` when its `fetch()` starts and `pages.setFirstPage(…)` /
/// `pages.setCached(…)` when it lands, and exposes `pages.items` as its rows.
mixin PagedFetch<T> on ChangeNotifier, SequencedFetch {
  late final PagedList<T> pages = PagedList<T>(
    requestSequence,
    keyOf: pageItemKey,
  );

  /// One page of the list at the store's current filters.
  @protected
  Future<PagedResult<T>> fetchPage(int page);

  /// A row's identity for de-duplicating across pages; null keeps every row.
  @protected
  Object? pageItemKey(T item) => null;

  int get page => pages.page;
  int get total => pages.total;
  bool get hasMore => pages.hasMore;
  bool get loadingMore => pages.loadingMore;
  Object? get loadMoreError => pages.loadMoreError;

  /// Append the next page. A no-op when there is none or one is loading.
  Future<void> loadMore() => pages.loadMore(fetchPage, notifyListeners);
}
