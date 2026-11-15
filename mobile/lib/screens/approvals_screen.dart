import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/l10n/coded_refusal_messages.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/action_verdict.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/screens/invoice_detail_screen.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/widgets/invoice_list_tile.dart';

class ApprovalsScreen extends StatefulWidget {
  const ApprovalsScreen({super.key});

  @override
  State<ApprovalsScreen> createState() => _ApprovalsScreenState();
}

class _ApprovalsScreenState extends State<ApprovalsScreen> {
  // The server's `approve` / `reject` verdicts per queued row
  // (`GET /api/invoices/{id}/review-actions`), keyed by id and the version
  // they judged, so an edited row is asked again. Swipe-to-approve and the
  // reject swipe are offered from these alone — the queue's own status filter
  // says nothing about segregation of duties or the CFO / max-amount gates —
  // and a row not yet answered (or whose read failed) offers neither.
  final Map<String, ({ActionVerdict approve, ActionVerdict reject})>
      _verdicts = {};
  final Set<String> _asking = {};

  String _verdictKey(Invoice invoice) =>
      '${invoice.id}@${invoice.updatedAt ?? ''}';

  /// Ask the server for every queued row's verdict not yet held or in flight.
  void _askVerdicts(List<Invoice> pending) {
    for (final invoice in pending) {
      final key = _verdictKey(invoice);
      if (_verdicts.containsKey(key) || !_asking.add(key)) continue;
      InvoiceApi.reviewActions(invoice.id).then(
        (v) => (
          approve: ActionVerdict.fromVerdicts(v, 'approve'),
          reject: ActionVerdict.fromVerdicts(v, 'reject'),
        ),
        onError: (Object _) => (
          approve: ActionVerdict.unanswered,
          reject: ActionVerdict.unanswered,
        ),
      ).then((verdict) {
        _asking.remove(key);
        if (!mounted) return;
        setState(() => _verdicts[key] = verdict);
      });
    }
  }

  /// Refetch the queue and re-ask every verdict.
  Future<void> _refresh() {
    setState(_verdicts.clear);
    return InvoiceStore.instance.fetchPending();
  }

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

          if (store.pendingLoading && pending.isEmpty) {
            return const Center(child: CircularProgressIndicator());
          }

          if (store.pendingError != null && pending.isEmpty) {
            // Never fall through to "All caught up!" on a failed load — an
            // empty approvals queue and an unreachable one look identical to a
            // reviewer, and only one of them means there is nothing to do.
            return Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.error_outline,
                      size: 48, color: Colors.red.shade700),
                  const SizedBox(height: 12),
                  Text(l.approvalsLoadError),
                  const SizedBox(height: 12),
                  FilledButton(
                    onPressed: _refresh,
                    child: Text(l.commonRetry),
                  ),
                ],
              ),
            );
          }

          if (pending.isEmpty) {
            return Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.check_circle, size: 64, color: Colors.green),
                  const SizedBox(height: 16),
                  Text(
                    l.approvalsAllCaughtUp,
                    style: const TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(l.approvalsNoneWaiting),
                ],
              ),
            );
          }

          _askVerdicts(pending);
          return RefreshIndicator(
            onRefresh: _refresh,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
                  child: Text(
                    l.approvalsPendingCount(pending.length),
                    style: TextStyle(
                      // shade700 clears AA contrast (shade600 is 4.38:1 at 14px).
                      color: Colors.grey.shade700,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ),
                Expanded(
                  child: ListView.separated(
                    itemCount: pending.length,
                    separatorBuilder: (_, _) => const Divider(height: 1),
                    itemBuilder: (context, index) {
                      final invoice = pending[index];
                      final verdict = _verdicts[_verdictKey(invoice)];
                      const hidden = {'invoice_not_in_review'};
                      final approve =
                          verdict?.approve ?? ActionVerdict.unanswered;
                      final reject =
                          verdict?.reject ?? ActionVerdict.unanswered;
                      final canSwipeApprove = approve.allowed;
                      final canSwipeReject = reject.allowed;
                      // Why Approve is refused, beside the row, for a reason
                      // the reader can act on (a colleague must approve it).
                      final refusal = approve.isOffered(hiddenCodes: hidden) &&
                              !approve.allowed
                          ? (localizeCodedRefusal(l, approve.refusal) ??
                              _message(approve))
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
                            if (ok && context.mounted) {
                              // Announce the result for assistive tech — the
                              // row vanishing is not announced on its own
                              // (WCAG 4.1.3).
                              A11y.announce(context, l.approvalApproved);
                            } else if (!ok && context.mounted) {
                              // A refused swipe used to snap the row back
                              // with no word of why. A coded refusal (SoD,
                              // CFO / max gate, named approver) says so in
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
                                padding:
                                    const EdgeInsets.fromLTRB(16, 0, 16, 12),
                                child: Text(
                                  refusal,
                                  key: ValueKey('approve-refusal-${invoice.id}'),
                                  style: TextStyle(
                                    color: Theme.of(context)
                                        .colorScheme
                                        .onSurfaceVariant,
                                  ),
                                ),
                              ),
                          ],
                        ),
                      );
                    },
                  ),
                ),
              ],
            ),
          );
        },
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
