import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/coded_refusal_messages.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/load_error_messages.dart';
import 'package:feohledger_mobile/models/action_verdict.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/screens/invoice_detail_screen.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/widgets/async_list_body.dart';
import 'package:feohledger_mobile/widgets/invoice_list_tile.dart';

class ApprovalsScreen extends StatefulWidget {
  const ApprovalsScreen({super.key});

  @override
  State<ApprovalsScreen> createState() => _ApprovalsScreenState();
}

class _ApprovalsScreenState extends State<ApprovalsScreen> {
  // Swipe-to-approve and the reject swipe are offered from each row's own
  // `approve` / `reject` verdict ([Invoice.reviewActions]), which the queue's
  // list read carries (`GET /api/invoices?include=review_actions`) — the
  // queue's own status filter says nothing about segregation of duties or the
  // CFO / max-amount gates. A row without one (served from the offline cache,
  // or by a backend that does not carry them) offers neither, and the screen
  // says it could not check rather than looking like "you may not approve".

  // Whether the "couldn't check" notice has been announced since it appeared,
  // so a rebuild does not repeat it to a screen reader.
  bool _announcedUnavailable = false;

  /// Refetch the queue, verdicts included.
  Future<void> _refresh() => InvoiceStore.instance.fetchPending();

  @override
  void initState() {
    super.initState();
    SchedulerBinding.instance.addPostFrameCallback((_) {
      // The approvals queue is its own server-filtered request — NOT the
      // Invoices tab's list. Both screens are children of one IndexedStack, so
      // this initState fires once for the app's lifetime; every later refresh
      // comes from the RefreshIndicator or a mutation.
      InvoiceStore.instance.fetchPending();
    });
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.approvalsTitle)),
      body: ListenableBuilder(
        listenable: InvoiceStore.instance,
        builder: (context, _) {
          final store = InvoiceStore.instance;
          final pending = store.pending;
          final loadError = store.pendingLoadError;

          final verdictsUnavailable = pending.isNotEmpty &&
              pending.any((invoice) => invoice.reviewActions == null);
          if (!verdictsUnavailable) {
            _announcedUnavailable = false;
          } else if (!_announcedUnavailable) {
            _announcedUnavailable = true;
            // A cached or verdict-less queue must not read as "nothing you
            // may do": say so, to a screen reader too (WCAG 4.1.3).
            SchedulerBinding.instance.addPostFrameCallback((_) {
              if (mounted) A11y.announce(context, l.reviewVerdictsUnavailable);
            });
          }

          return AsyncListBody<Invoice>(
            items: pending,
            loading: store.pendingLoading,
            // A failed load is never "All caught up!" — an empty approvals
            // queue and an unreachable one look identical to a reviewer, and
            // only one of them means there is nothing to do. A refused swipe
            // is an action failure and raises nothing here.
            hasError: loadError != null,
            fromCache: store.pendingFromCache,
            errorMessage: loadError == null
                ? ''
                : describeLoadError(l, loadError, l.approvalsLoadErrorPrefix),
            onRefresh: _refresh,
            // The queue pages like any list: the count is the server's total
            // for every invoice awaiting review, and Load more reaches the
            // rows past the first page.
            onLoadMore: store.loadMorePending,
            hasMore: store.pendingHasMore,
            loadingMore: store.pendingLoadingMore,
            loadMoreFailed: store.pendingLoadMoreError != null,
            total: store.pendingTotal,
            header: _header(l, store.pendingTotal, verdictsUnavailable),
            empty: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const ExcludeSemantics(
                  child: Icon(Icons.check_circle, size: 64, color: Colors.green),
                ),
                const SizedBox(height: 16),
                Text(
                  l.approvalsAllCaughtUp,
                  textAlign: TextAlign.center,
                  style: const TextStyle(
                    fontSize: 18,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 4),
                Text(l.approvalsNoneWaiting, textAlign: TextAlign.center),
              ],
            ),
            itemBuilder: (context, invoice) => _buildRow(l, invoice),
          );
        },
      ),
    );
  }

  /// The queue's count — the server's total, not the rows loaded so far — and,
  /// when the rows carry no verdicts, the notice that none could be checked.
  Widget _header(AppLocalizations l, int total, bool verdictsUnavailable) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
          child: Text(
            l.approvalsPendingCount(total),
            style: TextStyle(
              // shade700 clears AA contrast (shade600 is 4.38:1 at 14px).
              color: Colors.grey.shade700,
              fontWeight: FontWeight.w500,
            ),
          ),
        ),
        if (verdictsUnavailable)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
            child: Row(
              children: [
                ExcludeSemantics(
                  child: Icon(
                    Icons.cloud_off,
                    size: 18,
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    l.reviewVerdictsUnavailable,
                    key: const ValueKey('review-verdicts-unavailable'),
                    style: TextStyle(
                      color: Theme.of(context).colorScheme.onSurfaceVariant,
                    ),
                  ),
                ),
              ],
            ),
          ),
      ],
    );
  }

  Widget _buildRow(AppLocalizations l, Invoice invoice) {
    final verdicts = invoice.reviewActions ?? const {};
    const hidden = {'invoice_not_in_review'};
    final approve = ActionVerdict.fromVerdicts(verdicts, 'approve');
    final reject = ActionVerdict.fromVerdicts(verdicts, 'reject');
    final canSwipeApprove = approve.allowed;
    final canSwipeReject = reject.allowed;
    // Why Approve is refused, beside the row, for a reason the reader can act
    // on (a colleague must approve it).
    final refusal = approve.isOffered(hiddenCodes: hidden) && !approve.allowed
        ? (localizeCodedRefusal(l, approve.refusal) ?? _message(approve))
        : null;
    return Dismissible(
      key: ValueKey(invoice.id),
      direction: canSwipeApprove && canSwipeReject
          ? DismissDirection.horizontal
          : canSwipeApprove
              ? DismissDirection.startToEnd
              : canSwipeReject
                  ? DismissDirection.endToStart
                  : DismissDirection.none,
      background: _swipeBackground(
        Colors.green.shade700,
        Icons.check,
        Alignment.centerLeft,
        l.approvalActionApprove,
      ),
      secondaryBackground: _swipeBackground(
        Colors.red.shade700,
        Icons.close,
        Alignment.centerRight,
        l.approvalActionReject,
      ),
      confirmDismiss: (direction) async {
        if (direction == DismissDirection.startToEnd) {
          final ok = await InvoiceStore.instance.approve(
            invoice.id,
            expectedUpdatedAt: invoice.updatedAt,
          );
          if (ok && mounted) {
            // Announce the result for assistive tech — the row vanishing is
            // not announced on its own (WCAG 4.1.3).
            A11y.announce(context, l.approvalApproved);
          } else if (!ok && mounted) {
            // A refused swipe used to snap the row back with no word of why.
            // A coded refusal (SoD, CFO / max gate, named approver) says so in
            // the reader's language.
            final reason = localizeCodedRefusal(
              l,
              InvoiceStore.instance.approveErrorDetail,
            );
            if (reason != null) {
              A11y.announce(context, reason);
              ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(content: Text(reason)),
              );
            }
          }
          return ok;
        }
        return false; // Reject needs a reason — open detail
      },
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          InvoiceListTile(
            invoice: invoice,
            onTap: () => _openDetail(invoice),
          ),
          if (refusal != null)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
              child: Text(
                refusal,
                key: ValueKey('approve-refusal-${invoice.id}'),
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ),
            ),
        ],
      ),
    );
  }

  Widget _swipeBackground(
    Color color,
    IconData icon,
    Alignment alignment,
    String action,
  ) {
    // Label the swipe affordance so the icon isn't an unlabelled glyph
    // (WCAG 1.1.1); the action is also surfaced visually as text.
    return Container(
      color: color.withValues(alpha: 0.15),
      alignment: alignment,
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, color: color),
          const SizedBox(width: 8),
          Text(
            action,
            style: TextStyle(color: color, fontWeight: FontWeight.w600),
          ),
        ],
      ),
    );
  }

  static String? _message(ActionVerdict verdict) {
    final message = verdict.refusal?['message'];
    return message is String && message.isNotEmpty ? message : null;
  }

  void _openDetail(Invoice invoice) {
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => InvoiceDetailScreen(invoiceId: invoice.id),
      ),
    );
  }
}
