import 'package:flutter/material.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/audit_entry.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/utils/dates.dart';

/// ERP-sync facts derived from an invoice's audit log, mirroring the web
/// modal's `erpInfo`: the latest `invoice.erp_*` / `invoice.completed` entry
/// carries the ERP reference / document id and any send error. Pure value
/// object — no network.
class ErpInfo {
  final String? erpReference;
  final String? erpDocumentId;
  final String? lastError;
  final String actionLabel;
  final String? actor;
  final DateTime time;

  const ErpInfo({
    this.erpReference,
    this.erpDocumentId,
    this.lastError,
    required this.actionLabel,
    this.actor,
    required this.time,
  });

  /// Build the ERP summary from an invoice's audit entries (oldest-first), or
  /// null when no ERP action has happened yet. Matches the web derivation:
  /// the most recent entry whose action starts with `invoice.erp_` or
  /// `invoice.completed` wins.
  static ErpInfo? fromAuditLog(List<AuditEntry> entries) {
    AuditEntry? latest;
    for (final e in entries) {
      if (e.action.startsWith('invoice.erp_') ||
          e.action.startsWith('invoice.completed')) {
        latest = e; // entries are oldest-first, so the last match is newest
      }
    }
    if (latest == null) return null;
    final details = latest.details ?? const {};
    return ErpInfo(
      erpReference: details['erp_reference'] as String?,
      erpDocumentId: details['erp_document_id'] as String?,
      lastError: details['error'] as String?,
      actionLabel: latest.actionLabel,
      actor: latest.actorName,
      time: latest.createdAt,
    );
  }
}

/// Detail-screen panel showing the invoice's ERP integration status — the
/// latest ERP action, reference / document id, and any send error. Shown when
/// the invoice is en route to / posted in the ERP (or failed with ERP context).
class ErpStatusPanel extends StatelessWidget {
  final Invoice invoice;
  final ErpInfo? erpInfo;

  const ErpStatusPanel({
    super.key,
    required this.invoice,
    required this.erpInfo,
  });

  static const _erpStatuses = {
    InvoiceStatus.sendingToErp,
    InvoiceStatus.sentToErp,
    InvoiceStatus.postedInErp,
  };

  /// Whether to render at all — an ERP-bound status, or a failure that carries
  /// ERP context (mirrors the web `isErpStatus || (status==='failed' && erpInfo)`).
  bool get visible =>
      _erpStatuses.contains(invoice.status) ||
      (invoice.status == InvoiceStatus.failed && erpInfo != null);

  @override
  Widget build(BuildContext context) {
    if (!visible) return const SizedBox.shrink();
    final l = AppLocalizations.of(context);
    final info = erpInfo;

    final rows = <Widget>[];
    if (info?.erpReference != null && info!.erpReference!.isNotEmpty) {
      rows.add(_kv(l.erpStatusReference, info.erpReference!));
    }
    if (info?.erpDocumentId != null && info!.erpDocumentId!.isNotEmpty) {
      rows.add(_kv(l.erpStatusDocumentId, info.erpDocumentId!));
    }
    if (info?.lastError != null && info!.lastError!.isNotEmpty) {
      rows.add(_kv(l.erpStatusError, info.lastError!, error: true));
    }
    if (info != null) {
      final by = info.actor != null ? ' by ${info.actor}' : '';
      rows.add(_kv(l.erpStatusLastUpdate,
          '${info.actionLabel}$by · ${formatDateTime(info.time)}'));
    }
    if (rows.isEmpty) {
      // ERP-bound status but no audit detail yet (e.g. sending in flight).
      rows.add(_kv(l.erpStatusStatus, invoice.status.label));
    }

    // One merged announcement for the whole panel.
    final summary = [
      l.erpStatusTitle,
      if (info?.erpReference != null) '${l.erpStatusReference} ${info!.erpReference}',
      if (info?.erpDocumentId != null) '${l.erpStatusDocumentId} ${info!.erpDocumentId}',
      if (info?.lastError != null) '${l.erpStatusError} ${info!.lastError}',
    ].join(', ');

    return Semantics(
      label: summary,
      excludeSemantics: true,
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: Colors.indigo.withValues(alpha: 0.08),
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: Colors.indigo.withValues(alpha: 0.3)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.sync_alt, size: 18, color: Colors.indigo.shade700),
                const SizedBox(width: 8),
                Text(
                  l.erpStatusTitle,
                  style: TextStyle(
                    color: Colors.indigo.shade700,
                    fontSize: 14,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
            ...rows,
          ],
        ),
      ),
    );
  }

  Widget _kv(String label, String value, {bool error = false}) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 110,
            child: Text(
              label,
              style: TextStyle(
                color: Colors.grey.shade700,
                fontSize: 13,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
          Expanded(
            child: Text(
              value,
              style: TextStyle(
                fontSize: 13,
                color: error ? Colors.red.shade900 : null,
                fontWeight: error ? FontWeight.w600 : null,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
