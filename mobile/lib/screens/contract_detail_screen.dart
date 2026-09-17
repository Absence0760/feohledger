import 'package:flutter/material.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/api/endpoints.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/contract.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/contract_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/widgets/contract_status_badge.dart';
import 'package:feohledger_mobile/widgets/kpi_card.dart';

class ContractDetailScreen extends StatefulWidget {
  final String contractId;

  const ContractDetailScreen({super.key, required this.contractId});

  @override
  State<ContractDetailScreen> createState() => _ContractDetailScreenState();
}

class _ContractDetailScreenState extends State<ContractDetailScreen> {
  Contract? _contract;
  bool _loading = true;
  String? _error;
  // True while a lifecycle network call is in flight — guards against a
  // double-tap firing the transition POST twice and disables the buttons.
  bool _submitting = false;

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
      final contract = await ContractApi.getById(widget.contractId);
      // The route can be popped while a slow GET (or its 10s timeout) is still
      // in flight — setState after dispose throws.
      if (!mounted) return;
      setState(() {
        _contract = contract;
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

  Future<void> _activate() async {
    if (_submitting) return;
    final l = AppLocalizations.of(context);
    setState(() => _submitting = true);
    try {
      final success =
          await ContractStore.instance.activate(widget.contractId);
      if (!mounted) return;
      if (success) {
        await _load();
        _showSnack(l.contractActivated);
      } else {
        _showSnack(l.contractActivateFailed);
      }
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _terminate() async {
    final l = AppLocalizations.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(l.contractTerminateTitle),
        content: Text(l.contractTerminateBody),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: Text(l.commonCancel),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: Text(l.contractTerminate),
          ),
        ],
      ),
    );

    if (confirmed != true || _submitting) return;

    setState(() => _submitting = true);
    try {
      final success =
          await ContractStore.instance.terminate(widget.contractId);
      if (!mounted) return;
      if (success) {
        await _load();
        _showSnack(l.contractTerminated);
      } else {
        _showSnack(l.contractTerminateFailed);
      }
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  void _showSnack(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message)),
    );
    // Mirror the toast to assistive tech (WCAG 4.1.3).
    A11y.announce(context, message);
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.contractDetailTitle)),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null
              ? _buildError(l)
              : _buildDetail(l),
      bottomNavigationBar: _buildActions(l),
    );
  }

  Widget _buildError(AppLocalizations l) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.error_outline, size: 48, color: Colors.red.shade400),
          const SizedBox(height: 12),
          Text(l.contractDetailErrorPrefix(_error ?? ''),
              textAlign: TextAlign.center),
          const SizedBox(height: 16),
          FilledButton.icon(
            onPressed: _load,
            icon: const Icon(Icons.refresh),
            label: Text(l.commonRetry),
          ),
        ],
      ),
    );
  }

  Widget _buildDetail(AppLocalizations l) {
    final c = _contract!;
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          // Header
          Row(
            children: [
              Expanded(
                child: Text(
                  c.title ?? c.vendorName ?? l.contractDetailUntitled,
                  style: const TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ),
              ContractStatusBadge(status: c.status),
            ],
          ),
          if (c.totalValue != null) ...[
            const SizedBox(height: 8),
            Text(
              formatMoney(c.totalValue, currency: c.currency),
              style: const TextStyle(
                fontSize: 28,
                fontWeight: FontWeight.bold,
              ),
            ),
          ],

          const SizedBox(height: 24),

          // Details
          _detailRow(l.contractDetailFieldContractNumber, c.contractNumber),
          _detailRow(l.contractDetailFieldVendor, c.vendorName),
          _detailRow(l.contractDetailFieldType, c.contractType.label),
          _detailRow(l.contractDetailFieldCurrency, c.currency),
          _detailRow(
            l.contractDetailFieldSpendLimit,
            c.spendLimit != null
                ? '${formatMoney(c.spendLimit, currency: c.currency)}'
                    '${c.notToExceed ? l.contractDetailNotToExceed : ''}'
                : null,
          ),
          _detailRow(
            l.contractDetailFieldStartDate,
            c.startDate != null ? formatDate(c.startDate!) : null,
          ),
          _detailRow(
            l.contractDetailFieldEndDate,
            c.endDate != null ? formatDate(c.endDate!) : null,
          ),
          _detailRow(
            l.contractDetailFieldSigned,
            c.signedDate != null ? formatDate(c.signedDate!) : null,
          ),
          _detailRow(
            l.contractDetailFieldAutoRenew,
            c.autoRenew ? l.contractDetailYes : l.contractDetailNo,
          ),
          _detailRow(
            l.contractDetailFieldRenewalTerm,
            c.renewalTermMonths != null
                ? l.contractDetailRenewalTermMonths(c.renewalTermMonths!)
                : null,
          ),
          _detailRow(
            l.contractDetailFieldRenewalNotice,
            c.renewalNoticeDays != null
                ? l.contractDetailRenewalNoticeDays(c.renewalNoticeDays!)
                : null,
          ),
          _detailRow(l.contractDetailFieldPaymentTerms, c.paymentTerms),
          _detailRow(l.contractDetailFieldDescription, c.description),
          _detailRow(
              l.contractDetailFieldCreated, formatDate(c.createdAt)),

          // Spend summary
          if (c.spend != null) ...[
            const SizedBox(height: 24),
            _sectionTitle(l.contractDetailSectionSpend),
            const SizedBox(height: 12),
            _buildSpend(l, c.spend!, currency: c.currency),
          ],

          // Line items
          if (c.lineItems.isNotEmpty) ...[
            const SizedBox(height: 24),
            _sectionTitle(l.contractDetailSectionLineItems),
            const SizedBox(height: 8),
            ...c.lineItems
                .map((item) => _buildLineItem(l, item, currency: c.currency)),
          ],
        ],
      ),
    );
  }

  /// Every figure in the spend summary is a rollup of invoices booked against
  /// THIS contract, so it is denominated in the contract's own currency — the
  /// one the `Currency` detail row above already prints.
  Widget _buildSpend(
    AppLocalizations l,
    ContractSpend spend, {
    required String? currency,
  }) {
    return Column(
      children: [
        Row(
          children: [
            Expanded(
              child: KpiCard(
                title: l.contractDetailSpendInvoiced,
                value: formatMoney(spend.invoicedTotal, currency: currency),
                subtitle: l.contractDetailSpendInvoiceCount(spend.invoiceCount),
                icon: Icons.receipt_long,
                color: Colors.blue,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: KpiCard(
                title: spend.overLimit
                    ? l.contractDetailSpendOverLimit
                    : l.contractDetailSpendRemaining,
                value: formatMoney(spend.remaining, currency: currency),
                subtitle: spend.spendLimit != null
                    ? l.contractDetailSpendOfLimit(
                        formatMoney(spend.spendLimit, currency: currency))
                    : l.contractDetailSpendNoLimit,
                icon: spend.overLimit
                    ? Icons.warning_amber
                    : Icons.account_balance_wallet,
                color: spend.overLimit ? Colors.red : Colors.green,
              ),
            ),
          ],
        ),
      ],
    );
  }

  Widget _buildLineItem(
    AppLocalizations l,
    ContractLineItem item, {
    required String? currency,
  }) {
    final subtitleParts = <String>[
      if (item.quantity != null)
        l.contractDetailLineQty(item.quantity.toString()),
      if (item.unitPrice != null)
        l.contractDetailLineUnitPrice(
            formatMoney(item.unitPrice, currency: currency)),
      if (item.glAccount != null) l.contractDetailLineGl(item.glAccount!),
    ];
    return ListTile(
      contentPadding: EdgeInsets.zero,
      dense: true,
      leading: item.lineNumber != null
          ? CircleAvatar(
              radius: 14,
              child: Text(
                '${item.lineNumber}',
                style: const TextStyle(fontSize: 12),
              ),
            )
          : null,
      title: Text(
        item.description ?? item.itemCode ?? l.contractDetailLineItemFallback,
        style: const TextStyle(fontSize: 14),
      ),
      subtitle:
          subtitleParts.isNotEmpty ? Text(subtitleParts.join('  ·  ')) : null,
      trailing: item.total != null
          ? Text(
              formatMoney(item.total, currency: currency),
              style: const TextStyle(fontWeight: FontWeight.w600),
            )
          : null,
    );
  }

  Widget _sectionTitle(String title) {
    return Text(
      title,
      style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
    );
  }

  Widget _detailRow(String label, String? value) {
    if (value == null || value.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 120,
            child: Text(
              label,
              style: TextStyle(
                color: Colors.grey.shade600,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
          Expanded(child: Text(value)),
        ],
      ),
    );
  }

  Widget? _buildActions(AppLocalizations l) {
    final c = _contract;
    if (c == null) return null;
    if (!c.status.isActionable) return null;
    if (!AuthStore.instance.canApprove) return null;

    final canActivate = c.status == ContractStatus.draft;

    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Expanded(
              child: OutlinedButton.icon(
                onPressed: _submitting ? null : _terminate,
                icon: Icon(Icons.block, color: Colors.red.shade700),
                label: Text(
                  l.contractTerminate,
                  // shade700 keeps the destructive label at AA contrast.
                  style: TextStyle(color: Colors.red.shade700),
                ),
                style: OutlinedButton.styleFrom(
                  padding: const EdgeInsets.symmetric(vertical: 14),
                  side: BorderSide(color: Colors.red.shade700),
                ),
              ),
            ),
            if (canActivate) ...[
              const SizedBox(width: 16),
              Expanded(
                child: FilledButton.icon(
                  onPressed: _submitting ? null : _activate,
                  icon: const Icon(Icons.check),
                  label: Text(l.contractActivate),
                  style: FilledButton.styleFrom(
                    padding: const EdgeInsets.symmetric(vertical: 14),
                    backgroundColor: Colors.green,
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
