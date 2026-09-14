import 'package:flutter/material.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/payment.dart';
import 'package:feohledger_mobile/utils/money.dart';

class PaymentsScreen extends StatefulWidget {
  const PaymentsScreen({super.key});

  @override
  State<PaymentsScreen> createState() => _PaymentsScreenState();
}

class _PaymentsScreenState extends State<PaymentsScreen> {
  List<Payment> _payments = [];
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final payments = await PaymentApi.list();
      if (!mounted) return;
      setState(() {
        _payments = payments;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = describeApiError(e);
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.paymentsTitle)),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null
              ? Center(child: Text(l.paymentsErrorPrefix(_error!)))
              : RefreshIndicator(
                  onRefresh: _load,
                  child: _payments.isEmpty
                      ? Center(child: Text(l.paymentsEmpty))
                      : ListView.separated(
                          itemCount: _payments.length,
                          separatorBuilder: (_, _) =>
                              const Divider(height: 1),
                          itemBuilder: (context, index) {
                            final p = _payments[index];
                            final reference =
                                p.reference ?? p.id.substring(0, 8);
                            // The AUTHORIZED figure, in the currency the
                            // joined invoice carries. `payments` has no
                            // currency column of its own, and a null code
                            // renders the bare figure — the org's reporting
                            // currency is not what this payment moved.
                            final amount = formatMoney(
                              p.amount,
                              currency: p.currency,
                            );
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
                                  style: const TextStyle(
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                                subtitle: Text(
                                  '${p.method.label} • $reference',
                                ),
                                trailing: _statusChip(l, p.status),
                              ),
                            );
                          },
                        ),
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
