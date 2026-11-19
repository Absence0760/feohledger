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
/// 3. **Loaded, no rows** — [empty], inside the [RefreshIndicator] and
///    scrollable, so it can be pulled to refresh like a populated list.
/// 4. **Rows** — one [itemBuilder] call per item, divided by a hairline.
///
/// Above 3 and 4 a [StaleDataBanner] says when the rows are not live: served
/// from the offline cache ([fromCache]), or the last list loaded after a
/// refresh failed ([hasError] with rows still on screen).
class AsyncListBody<T> extends StatelessWidget {
  final List<T> items;
  final bool loading;

  /// The store's last fetch failed (its `error` is non-null).
  final bool hasError;

  /// The rows came from the offline cache, not the server.
  final bool fromCache;

  /// Localized sentence for a first load that failed — what happened and what
  /// to do. Never an exception's `toString()`.
  final String errorMessage;

  final Future<void> Function() onRefresh;
  final Widget empty;
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
                  child: empty,
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

/// An amber notice that the data on screen is not live — cached, or the last
/// copy loaded before a refresh failed. Announced as a live region so a
/// screen-reader user hears it when it appears (WCAG 4.1.3).
class StaleDataBanner extends StatelessWidget {
  final IconData icon;
  final String message;

  const StaleDataBanner({super.key, required this.icon, required this.message});

  @override
  Widget build(BuildContext context) {
    return Semantics(
      liveRegion: true,
      label: message,
      excludeSemantics: true,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: Colors.amber.shade50,
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: Colors.amber.shade200),
        ),
        child: Row(
          children: [
            Icon(icon, size: 18, color: Colors.amber.shade900),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                message,
                style: TextStyle(color: Colors.amber.shade900, fontSize: 13),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
