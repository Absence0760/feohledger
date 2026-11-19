import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/l10n/load_error_messages.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/stores/payment_store.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/widgets/async_list_body.dart';

/// Payment history. The list, its offline copy and its request ordering live
/// in [PaymentStore]; the body is [AsyncListBody], so a failed first load
/// offers Retry and a failed refresh over a loaded list says so.
class PaymentsScreen extends StatefulWidget {
  const PaymentsScreen({super.key});

  @override
  State<PaymentsScreen> createState() => _PaymentsScreenState();
}

class _PaymentsScreenState extends State<PaymentsScreen> {
  @override
  void initState() {
    super.initState();
    // After the first frame: fetch() notifies, which must not happen while
    // the tree is still building.
    SchedulerBinding.instance.addPostFrameCallback((_) {
      PaymentStore.instance.fetch();
    });
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.paymentsTitle)),
      body: ListenableBuilder(
        listenable: PaymentStore.instance,
        builder: (context, _) {
          final store = PaymentStore.instance;
          final loadError = store.loadError;
          return AsyncListBody<Payment>(
            items: store.payments,
            loading: store.loading,
            hasError: loadError != null,
            fromCache: store.fromCache,
            errorMessage: loadError == null
                ? ''
                : describeLoadError(l, loadError, l.paymentsLoadErrorPrefix),
            onRefresh: store.fetch,
            empty: Text(l.paymentsEmpty, textAlign: TextAlign.center),
            itemBuilder: (context, p) => _row(l, p),
          );
        },
      ),
    );
  }

  Widget _row(AppLocalizations l, Payment p) {
    final reference = p.reference ?? p.id.substring(0, 8);
    // The AUTHORIZED figure, in the currency the joined invoice carries.
    // `payments` has no currency column of its own, and a null code renders
    // the bare figure — the org's reporting currency is not what this
    // payment moved. Formatted from the served string: never a double.
    final amount = formatMoneyString(p.amountDisplay, currency: p.currency);
    // One merged announcement for the whole payment row.
    return Semantics(
      label:
          '$amount, '
          '${p.method.label}, $reference, '
          '${_statusLabel(l, p.status)}',
      excludeSemantics: true,
      child: ListTile(
        leading: _methodIcon(p.method),
        title: Text(
          amount,
          style: const TextStyle(fontWeight: FontWeight.w600),
        ),
        subtitle: Text('${p.method.label} • $reference'),
        trailing: _statusChip(l, p.status),
      ),
    );
  }

  Widget _methodIcon(PaymentMethod method) {
    final (icon, color) = switch (method) {
      PaymentMethod.ach => (Icons.account_balance, Colors.blue),
      PaymentMethod.wire => (Icons.bolt, Colors.orange),
      PaymentMethod.check => (Icons.description, Colors.grey),
      PaymentMethod.virtualCard => (Icons.credit_card, Colors.purple),
    };
    return CircleAvatar(
      backgroundColor: color.withValues(alpha: 0.15),
      child: Icon(icon, color: color, size: 20),
    );
  }

  String _statusLabel(AppLocalizations l, PaymentStatus status) =>
      switch (status) {
        PaymentStatus.pending => l.paymentStatusPending,
        PaymentStatus.processing => l.paymentStatusProcessing,
        PaymentStatus.completed => l.paymentStatusCompleted,
        PaymentStatus.failed => l.paymentStatusFailed,
        PaymentStatus.cancelled => l.paymentStatusCancelled,
      };

  Widget _statusChip(AppLocalizations l, PaymentStatus status) {
    // Tint drives the background/border; text uses a darkened variant so the
    // 12px bold label clears AA contrast (≥4.5:1) over the pale tint
    // (WCAG 1.4.3). Orange can't reach 4.5:1 as a true orange, so `pending`
    // uses a dark brown that reads as deep amber.
    final (color, textColor) = switch (status) {
      PaymentStatus.pending => (Colors.orange, Colors.brown.shade800),
      PaymentStatus.processing => (Colors.blue, Colors.blue.shade800),
      PaymentStatus.completed => (Colors.green, Colors.green.shade900),
      PaymentStatus.failed => (Colors.red, Colors.red.shade900),
      PaymentStatus.cancelled => (Colors.grey, Colors.grey.shade800),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Text(
        _statusLabel(l, status),
        style: TextStyle(
          color: textColor,
          fontSize: 12,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}
