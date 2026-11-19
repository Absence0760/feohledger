import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/widgets/load_error_state.dart';

/// The body of a list screen backed by a store: the four async states, in the
/// one order every list needs, so no screen hand-builds them.
///
/// 1. **Loading, nothing to show yet** — a spinner.
/// 2. **First load failed, nothing to show** — [LoadErrorState] with
///    [errorMessage] and a Retry that calls [onRefresh]. A failed load is
///    never rendered as the empty state: "Queue clear" on a dropped
///    connection tells an approver there is nothing to do when there may be.
/// 3. **Loaded, no rows** — [filteredEmpty] when the screen passes one (a
///    filter chip or search query is narrowing the list), else [empty]; inside
///    the [RefreshIndicator] and scrollable, so it can be pulled to refresh
///    like a populated list. "No exceptions" under the Escalated chip would
///    claim the whole queue is clear, so a filtered list says what filtered
///    it and offers the way out ([FilteredEmptyState]).
/// 4. **Rows** — one [itemBuilder] call per item, divided by a hairline.
///
/// Above 3 and 4 a [StaleDataBanner] says when the rows are not live: served
/// from the offline cache ([fromCache]), or the last list loaded after a
/// refresh failed ([hasError] with rows still on screen). The banner carries a
/// Retry button wired to [onRefresh], so retrying never depends on finding the
/// pull gesture.
class AsyncListBody<T> extends StatelessWidget {
  final List<T> items;
  final bool loading;

  /// The store's last **fetch** failed (its `loadError` is non-null). Never a
  /// failed action: a refused resolve over a loaded list leaves the rows live,
  /// and saying "couldn't refresh" there would be false.
  final bool hasError;

  /// The rows came from the offline cache, not the server.
  final bool fromCache;

  /// Localized sentence for a first load that failed — what happened and what
  /// to do. Never an exception's `toString()`.
  final String errorMessage;

  final Future<void> Function() onRefresh;
  final Widget empty;

  /// Shown instead of [empty] when the screen's filter or search leaves no
  /// rows. The screen passes it only while a filter is set; null otherwise.
  final Widget? filteredEmpty;
  final Widget Function(BuildContext context, T item) itemBuilder;

  const AsyncListBody({
    super.key,
    required this.items,
    required this.loading,
    required this.hasError,
    required this.fromCache,
    required this.errorMessage,
    required this.onRefresh,
    required this.empty,
    this.filteredEmpty,
    required this.itemBuilder,
  });

  @override
  Widget build(BuildContext context) {
    if (items.isEmpty) {
      if (loading) return const Center(child: CircularProgressIndicator());
      if (hasError) {
        return LoadErrorState(message: errorMessage, onRetry: onRefresh);
      }
    }

    final l = AppLocalizations.of(context);
    final String? banner = fromCache
        ? l.listShowingSavedCopy
        : hasError
        ? l.listRefreshFailed
        : null;

    return RefreshIndicator(
      onRefresh: onRefresh,
      child: CustomScrollView(
        // Always scrollable: a short list, or the empty state, must still
        // take the pull gesture.
        physics: const AlwaysScrollableScrollPhysics(),
        slivers: [
          if (banner != null)
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
              sliver: SliverToBoxAdapter(
                child: StaleDataBanner(
                  icon: fromCache ? Icons.cloud_off : Icons.sync_problem,
                  message: banner,
                  onRetry: onRefresh,
                ),
              ),
            ),
          if (items.isEmpty)
            // Fills the viewport so the empty copy sits centred, and grows
            // past it (scrolling) at a large text scale instead of
            // overflowing.
            SliverFillRemaining(
              hasScrollBody: false,
              child: Center(
                child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: filteredEmpty ?? empty,
                ),
              ),
            )
          else
            SliverList.separated(
              itemCount: items.length,
              separatorBuilder: (_, _) => const Divider(height: 1),
              itemBuilder: (context, index) =>
                  itemBuilder(context, items[index]),
            ),
        ],
      ),
    );
  }
}

/// The empty state of a list a filter or search has narrowed to nothing:
/// what filtered it, and a "Show all" action that clears the filter.
class FilteredEmptyState extends StatelessWidget {
  final String message;
  final VoidCallback onShowAll;

  const FilteredEmptyState({
    super.key,
    required this.message,
    required this.onShowAll,
  });

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        ExcludeSemantics(
          child: Icon(
            Icons.filter_alt_off,
            size: 48,
            color: Theme.of(context).colorScheme.onSurfaceVariant,
          ),
        ),
        const SizedBox(height: 12),
        Text(
          message,
          textAlign: TextAlign.center,
          style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 12),
        OutlinedButton(onPressed: onShowAll, child: Text(l.commonShowAll)),
      ],
    );
  }
}

/// An amber notice that the data on screen is not live — cached, or the last
/// copy loaded before a refresh failed — with a Retry button when [onRetry] is
/// given, so a retry never depends on discovering pull-to-refresh.
///
/// The message is a live region, so a screen-reader user hears it when it
/// appears (WCAG 4.1.3); the button stays its own focusable node. Text is
/// `brown.shade700` on `amber.shade50` (#5D4037 on #FFF8E1, 8.8:1) — the
/// earlier `amber.shade900` read 2.6:1 and failed AA (1.4.3).
class StaleDataBanner extends StatelessWidget {
  final IconData icon;
  final String message;
  final Future<void> Function()? onRetry;

  const StaleDataBanner({
    super.key,
    required this.icon,
    required this.message,
    this.onRetry,
  });

  @override
  Widget build(BuildContext context) {
    final retry = onRetry;
    return Container(
      padding: EdgeInsets.fromLTRB(12, 4, retry == null ? 12 : 4, 4),
      constraints: const BoxConstraints(minHeight: 44),
      decoration: BoxDecoration(
        color: Colors.amber.shade50,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: Colors.amber.shade200),
      ),
      child: Row(
        children: [
          ExcludeSemantics(
            child: Icon(icon, size: 18, color: Colors.brown.shade700),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Padding(
              padding: const EdgeInsets.symmetric(vertical: 6),
              child: Semantics(
                container: true,
                liveRegion: true,
                label: message,
                excludeSemantics: true,
                child: Text(
                  message,
                  style: TextStyle(color: Colors.brown.shade700, fontSize: 13),
                ),
              ),
            ),
          ),
          if (retry != null)
            TextButton(
              onPressed: retry,
              style: TextButton.styleFrom(
                foregroundColor: Colors.brown.shade800,
                // WCAG 2.5.8 / Material's 48dp touch target.
                minimumSize: const Size(48, 48),
              ),
              child: Text(AppLocalizations.of(context).commonRetry),
            ),
        ],
      ),
    );
  }
}
