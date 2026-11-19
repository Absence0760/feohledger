/// Request-sequencing guard for `ChangeNotifier` list stores whose `fetch()`
/// is re-triggered by a live search box or filter change (see e.g.
/// [InvoiceStore], [VendorStore]).
///
/// Without a guard, firing a new `fetch()` before the previous one's response
/// has landed can let an earlier, slower request resolve AFTER a later one and
/// clobber the store with stale results that no longer match the current
/// search/filter — with no error surfaced to the user (issue #182).
///
/// Usage — capture a token at the very start of `fetch()`, before the first
/// `await`, and guard every place the response's data gets applied:
///
/// ```dart
/// class MyStore extends ChangeNotifier with SequencedFetch {
///   Future<void> fetch() async {
///     final token = nextRequestToken();
///     _loading = true;
///     notifyListeners();
///     try {
///       final result = await MyApi.list(...);
///       if (!isCurrentRequest(token)) return; // a newer fetch beat us — discard
///       _items = result;
///       _loading = false;
///       notifyListeners();
///     } catch (e) {
///       if (!isCurrentRequest(token)) return;
///       _loading = false;
///       _error = e.toString();
///       notifyListeners();
///     }
///   }
/// }
/// ```
/// One monotonic request counter.
///
/// [SequencedFetch] mixes in a single instance for a store's primary `fetch()`.
/// A store that owns a **second**, independent list (e.g. `InvoiceStore`'s
/// approvals queue, which is fetched on its own filter and must not be
/// cancelled by — or cancel — the main list's requests) holds another instance
/// of its own rather than sharing the mixin's counter.
class RequestSequence {
  int _seq = 0;

  /// Call once at the start of a new request, before any `await`.
  int next() => ++_seq;

  /// True if [token] is still the latest request issued on this sequence.
  bool isCurrent(int token) => token == _seq;

  /// The latest token issued, without issuing a new one — for a follow-up
  /// request (a next page) that must be discarded if a fresh [next] request
  /// starts, but must not itself supersede the one it extends.
  int get current => _seq;

  /// Test seam: reset the counter so tests aren't coupled to prior state.
  void reset() {
    _seq = 0;
  }
}

mixin SequencedFetch {
  final RequestSequence _requestSeq = RequestSequence();

  /// The store's primary sequence, for a helper that extends its requests
  /// (`PagedFetch`'s next-page reads) without taking a token of its own.
  RequestSequence get requestSequence => _requestSeq;

  /// Call once at the start of a new request, before any `await`. Returns a
  /// token to check when that request's response resolves.
  int nextRequestToken() => _requestSeq.next();

  /// True if [token] is still the latest request issued — i.e. no newer
  /// `fetch()` call has started since. False means a newer request has
  /// already superseded this one; its response should be silently discarded
  /// rather than applied to store state.
  bool isCurrentRequest(int token) => _requestSeq.isCurrent(token);

  /// Test seam: reset the sequence counter. Stores' own `debugReset()` should
  /// call this so tests aren't coupled to prior singleton state.
  void debugResetSequence() {
    _requestSeq.reset();
  }
}
