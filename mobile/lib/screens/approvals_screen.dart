import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/coded_refusal_messages.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
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
                    onPressed: store.fetchPending,
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

          return RefreshIndicator(
            onRefresh: store.fetchPending,
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
                      return Dismissible(
                        key: ValueKey(invoice.id),
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
                            final ok = await InvoiceStore.instance
                                .approve(invoice.id);
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
                        child: InvoiceListTile(
                          invoice: invoice,
                          onTap: () => _openDetail(invoice),
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

  void _openDetail(Invoice invoice) {
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => InvoiceDetailScreen(invoiceId: invoice.id),
      ),
    );
  }
}
